#!/usr/bin/env python3
"""Watch House members' Periodic Transaction Reports (STOCK Act filings).

Checks the House Clerk's yearly disclosure index (a conditional request, so an
unchanged index costs one tiny 304 response), finds PTR filings for the members
named in config.json ("*" for every member), and parses the transactions out of
each new filing's PDF. Filings with stock or option trades at or above
min_trade_usd are logged to trades_log.md and queued in pending.json until the
trading bot has looked at them.

Usage:
  python3 watch_disclosures.py                    # check now, print the result as JSON
  python3 watch_disclosures.py --notify --quiet   # what the background job runs every minute
  python3 watch_disclosures.py --pending          # show queued filings and when the last check ran
  python3 watch_disclosures.py --done DOC_ID ...  # remove handled filings from the queue
  python3 watch_disclosures.py --latest N         # show the member's last N filings, change nothing
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import io
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from trader import now, now_iso, parse_time

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
STATE = ROOT / "state.json"
PDF_DIR = ROOT / "filings"
LOG = ROOT / "trades_log.md"
PENDING = ROOT / "pending.json"
LOCK = ROOT / ".lock"

INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
USER_AGENT = "liquid-bot disclosure watcher"

# macOS PDFKit via JavaScript for Automation, so no third-party PDF library is needed.
# Returns every text line with its page, left edge and top edge (PDF points, y grows upward).
PDF_LINES_JXA = """
ObjC.import("PDFKit");
function run(argv) {
  const doc = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]));
  const out = [];
  if (!doc || doc.isNil()) return "[]";
  for (let p = 0; p < doc.pageCount; p++) {
    try {
      const page = doc.pageAtIndex(p);
      const lines = page.selectionForRect(page.boundsForBox($.kPDFDisplayBoxMediaBox)).selectionsByLine;
      for (let i = 0; i < lines.count; i++) {
        const sel = lines.objectAtIndex(i), b = sel.boundsForPage(page);
        out.push({page: p, x: b.origin.x, top: b.origin.y + b.size.height, text: sel.string.js});
      }
    } catch (e) {}  // pages with no text layer
  }
  return JSON.stringify(out);
}
"""

ASSET_RE = re.compile(r"(?:\((?P<ticker>[A-Z][A-Z0-9.\-]{0,6})\)\s*)?\[(?P<code>[A-Z]{2})\]")
TX_RE = re.compile(
    r"(?<![A-Za-z])(?P<type>P|S \(partial\)|S|E)\s+"
    r"(?P<tx_date>\d{2}/\d{2}/\d{4})\s+(?P<notified>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amount>\$[\d,]+(?:\s*-(?:\s*\$[\d,]+)?)?|Spouse/DC Over(?:\s*\$[\d,]+)?)"
)
# An amount cell on its own: one figure, a range, or "Spouse/DC Over $1,000,000".
AMOUNT_CONT_RE = re.compile(r"^(?:\$[\d,]+(?:\s*-\s*(?:\$[\d,]+)?)?|Spouse/DC Over(?:\s*\$[\d,]+)?)$")
# What's left of a transaction when PDFKit splits it into separate runs on one line.
TX_PIECE_RE = re.compile(r"(?:^|\s)(?:P|S \(partial\)|S|E)$|\d{2}/\d{2}/\d{4}\s+\d{2}/\d{2}/\d{4}(?:\s.*)?$")
OWNER_RE = re.compile(r"^(?:(?:SP|JT|DC)\b\s*)+")
LABEL_RE = re.compile(r"^(?:[A-Z] ?){1,2}:\s*")  # "F S: New", "S O: ...", "D: ..."
TX_TYPES = {"P": "purchase", "S": "sale", "S (partial)": "partial sale", "E": "exchange"}
# Table header cells, as PDFKit (whole header rows) or pdfplumber (word groups) return them.
HEADER_RE = re.compile(r"^(?:ID|ID Owner.*|Owner(?: Asset)?|Asset|Transaction.*|Notification.*|Type|Date.*|"
                       r"Amount.*|Cap\.?|Gains >|\$200\?)$")
FOOTER_RE = re.compile(r"^\* For the complete list")
ROW_TOL = 3  # points of slack when comparing line tops


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def fetch_index(year, etag=None):
    """Return (zip bytes, etag), or (None, etag) when the index hasn't changed."""
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(INDEX_URL.format(year=year), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read(), resp.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        raise


def member_matcher(cfg):
    """Match the members named in config.json; "*" means every House member."""
    if cfg["members"] == "*":
        return lambda first, last: True
    names = [n.lower().split() for n in cfg["members"]]
    return lambda first, last: any(last.lower() == n[-1] and first.lower().startswith(n[0]) for n in names)


def amount_low(amount):
    """Lower bound of a disclosed amount range, in dollars."""
    if "Over" in amount:
        return 1_000_001
    m = re.search(r"\$([\d,]+)", amount)
    return int(m.group(1).replace(",", "")) if m else 0


def member_filings(cfg, years, etags=None):
    """Return the members' PTR filings from every index that changed, plus the new etags."""
    matches = member_matcher(cfg)
    etags = dict(etags or {})
    filings = []
    for year in years:
        try:
            data, etags[str(year)] = fetch_index(year, etags.get(str(year)))
        except Exception as e:
            if year == years[0]:
                raise
            print(f"warning: could not fetch {year} index: {e}", file=sys.stderr)
            continue
        if data is None:
            continue
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            root = ET.fromstring(zf.read(f"{year}FD.xml"))
        for m in root.iter("Member"):
            if (m.findtext("FilingType") or "") != "P":
                continue
            first, last = (m.findtext("First") or "").strip(), (m.findtext("Last") or "").strip()
            if not matches(first, last):
                continue
            doc_id = m.findtext("DocID").strip()
            filings.append({
                "doc_id": doc_id,
                "member": f"{first} {last}",
                "year": int(m.findtext("Year")),
                "filing_date": dt.datetime.strptime(m.findtext("FilingDate"), "%m/%d/%Y").date().isoformat(),
                "url": PTR_URL.format(year=m.findtext("Year"), doc_id=doc_id),
            })
    filings.sort(key=lambda f: (f["filing_date"], f["doc_id"]))
    return filings, etags


def pdf_lines(path):
    """Text lines with their page, left edge and top edge (PDF points, y grows upward)."""
    if shutil.which("osascript"):
        return pdf_lines_pdfkit(path)
    return pdf_lines_pdfplumber(path)


def pdf_lines_pdfplumber(path):
    """Linux version of pdf_lines (the cloud runs Ubuntu): pdfplumber words grouped into
    lines, then split into runs at wide gaps so each table column is its own run,
    as PDFKit returns them."""
    import pdfplumber  # only needed off macOS; see requirements.txt

    out = []
    with pdfplumber.open(path) as pdf:
        for page_no, page in enumerate(pdf.pages):
            words = sorted(page.extract_words(x_tolerance=1.5, y_tolerance=2), key=lambda w: (w["top"], w["x0"]))
            rows = []
            for w in words:
                if rows and abs(rows[-1][0]["top"] - w["top"]) <= 2:
                    rows[-1].append(w)
                else:
                    rows.append([w])
            for row in rows:
                row.sort(key=lambda w: w["x0"])
                runs = [[row[0]]]
                for w in row[1:]:
                    if w["x0"] - runs[-1][-1]["x1"] > 12:
                        runs.append([w])
                    else:
                        runs[-1].append(w)
                for run in runs:
                    text = re.sub(r"\(cid:\d+\)", "", " ".join(w["text"] for w in run)).replace("\x00", "")
                    text = " ".join(text.split())
                    if text:
                        out.append({"page": page_no, "x": run[0]["x0"],
                                    "top": page.height - min(w["top"] for w in run), "text": text})
    return out


def pdf_lines_pdfkit(path):
    out = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", PDF_LINES_JXA, str(path)],
        capture_output=True, text=True, timeout=120,
    )
    lines = json.loads(out.stdout or "[]")
    for line in lines:
        # Field labels in these PDFs come out with NUL padding ("D\x00\x00...: text").
        line["text"] = " ".join(line["text"].replace("\x00", "").split())
    return [line for line in lines if line["text"]]


def visual_lines(lines):
    """Join text runs that sit on the same line of the page, left to right."""
    merged = []
    for line in sorted(lines, key=lambda l: (-l["top"], l["x"])):
        if merged and merged[-1]["top"] - line["top"] <= 1.5:
            merged[-1]["text"] += " " + line["text"]
        else:
            merged.append(dict(line))
    return merged


def parse_transactions(lines):
    """Rebuild the PTR table from line positions.

    Text order in these PDFs interleaves the columns unpredictably, but each
    transaction line is top-aligned with the first line of its asset, so a row
    runs from its transaction line's top down to the next transaction line.
    A row at the bottom of a page can finish at the top of the next page.
    Returns (transactions, problems).
    """
    txs, problems = [], []

    def finish(row):
        tx, top = row["tx"], row["top"]
        amount = " ".join(tx.group("amount").split())
        if amount.endswith(("-", "Over")):
            cont = [l for l in row["lines"] if l["page"] == row["page"]
                    and AMOUNT_CONT_RE.match(l["text"]) and l["top"] < top]
            if cont:
                amount += " " + cont[0]["text"]

        # The asset column: everything in the row except transaction and amount cells.
        cells = []
        for line in row["lines"]:
            text = TX_RE.sub("", line["text"]).strip()
            if abs(line["top"] - top) <= ROW_TOL and line["page"] == row["page"]:
                text = TX_PIECE_RE.sub("", text).strip()
            if text and not AMOUNT_CONT_RE.match(text):
                cells.append(dict(line, text=OWNER_RE.sub("", text).strip()))
        col = "\n".join(c["text"] for c in cells)
        marker = ASSET_RE.search(col)
        if not marker:
            problems.append(f"page {row['page'] + 1}: transaction on {tx.group('tx_date')} has no asset code")
            return
        name_lines = [t for t in col[:marker.start()].split("\n") if t and not LABEL_RE.match(t)]
        name = " ".join(" ".join(name_lines).split())

        description = ""
        d_idx = next((k for k, c in enumerate(cells) if re.match(r"^D ?:", c["text"])), None)
        if d_idx is not None:
            parts, prev = [LABEL_RE.sub("", cells[d_idx]["text"])], cells[d_idx]
            for c in cells[d_idx + 1:]:
                if c["page"] != prev["page"] or prev["top"] - c["top"] > 15 or LABEL_RE.match(c["text"]):
                    break
                parts.append(c["text"])
                prev = c
            description = " ".join(parts)

        option = None
        if marker.group("code") == "OP":
            low = description.lower()
            option = "call" if "call" in low else "put" if "put" in low else "unknown"
        txs.append({
            "ticker": marker.group("ticker"),
            "asset": name,
            "asset_type": marker.group("code"),
            "option": option,
            "type": TX_TYPES[tx.group("type")],
            "tx_date": dt.datetime.strptime(tx.group("tx_date"), "%m/%d/%Y").date().isoformat(),
            "amount": amount,
            "description": description,
        })

    def order(line):
        return (line["page"], -round(line["top"]), line["x"])

    pages = sorted({line["page"] for line in lines})
    markers = tx_count = 0
    pending = None  # a row whose asset continues onto the next page
    for page in pages:
        on_page = [l for l in lines if l["page"] == page]
        # Drop the asset-type footnote and everything under it (signature, trust details).
        footer = max((l["top"] for l in on_page if FOOTER_RE.match(l["text"])), default=float("-inf"))
        on_page = [l for l in on_page if l["top"] > footer and not HEADER_RE.match(l["text"])]
        markers += sum(len(ASSET_RE.findall(l["text"])) for l in on_page)
        rows = sorted(((v, m) for v in visual_lines(on_page) for m in [TX_RE.search(v["text"])] if m),
                      key=lambda vm: -vm[0]["top"])
        tx_count += len(rows)

        first_top = rows[0][0]["top"] if rows else float("-inf")
        lead = [l for l in on_page if l["top"] > first_top + ROW_TOL]
        if pending:
            pending["lines"] = sorted(pending["lines"] + lead, key=order)
            finish(pending)
            pending = None

        for i, (tx_line, tx) in enumerate(rows):
            top = tx_line["top"]
            bottom = rows[i + 1][0]["top"] if i + 1 < len(rows) else float("-inf")
            row = {"page": page, "tx": tx, "top": top, "lines": sorted(
                (l for l in on_page if bottom + ROW_TOL < l["top"] <= top + ROW_TOL), key=order)}
            last_on_page = i == len(rows) - 1 and page != pages[-1]
            if last_on_page and not any(ASSET_RE.search(l["text"]) for l in row["lines"]):
                pending = row
            else:
                finish(row)
    if pending:
        finish(pending)
    if markers != tx_count:
        problems.append(f"{markers} assets but {tx_count} transaction lines")
    return txs, problems


def enrich(filing, cfg):
    PDF_DIR.mkdir(exist_ok=True)
    pdf = PDF_DIR / f"{filing['doc_id']}.pdf"
    if not pdf.exists():
        pdf.write_bytes(fetch(filing["url"]))
    filing["transactions"], filing["problems"] = parse_transactions(pdf_lines(pdf))
    if not filing["transactions"]:
        # Paper filings are scanned images with no text layer.
        filing["problems"].append("no transactions could be read from the PDF")
    filing["parsed"] = not filing["problems"]
    # Tradeable signals: stock or option trades big enough to mean something.
    filing["signals"] = [t for t in filing["transactions"] if t["ticker"] and t["asset_type"] in ("ST", "OP")
                         and t["type"] != "exchange" and amount_low(t["amount"]) >= cfg["min_trade_usd"]]
    return filing


def append_log(cfg, filings):
    found = now().strftime("%Y-%m-%d %H:%M")
    lines = []
    if not LOG.exists():
        lines.append("# Disclosed congressional trades\n")
    for f in filings:
        lines.append(f"\n## {f['member']}: filing {f['doc_id']}, filed {f['filing_date']} (found {found})\n")
        lines.append(f"[PDF]({f['url']})\n")
        for problem in f["problems"]:
            lines.append(f"- Parsing problem ({problem}). Check the PDF by hand.\n")
        for t in f["signals"]:
            kind = f"{t['option']} options" if t["option"] else t["asset_type"]
            ticker = t["ticker"] or "no ticker"
            lines.append(
                f"- {t['tx_date']} **{t['type']}** {t['asset']} ({ticker}, {kind}), {t['amount']}"
                + (f": {t['description']}" if t["description"] else "") + "\n"
            )
    with LOG.open("a") as fh:
        fh.writelines(lines)


def mac_notification(message, subtitle):
    if not shutil.which("osascript"):
        return  # not on a Mac (the cloud): the bot sends its own push notification
    subprocess.run(
        ["osascript", "-e", "on run argv", "-e",
         'display notification (item 1 of argv) with title "Liquid bot" subtitle (item 2 of argv) sound name "Glass"',
         "-e", "end run", message[:240], subtitle],
        capture_output=True, timeout=30,
    )


def notify(cfg, filings):
    parts = []
    for f in filings:
        buys, sells = [], []
        for t in f["signals"]:
            label = t["ticker"] + (" puts" if t["option"] == "put" else "")
            bucket = sells if "sale" in t["type"] else buys
            if label not in bucket:
                bucket.append(label)
        words = (["BUY " + ", ".join(buys)] if buys else []) + (["SELL " + ", ".join(sells)] if sells else [])
        parts.append(f"{f['member'].split()[-1]}: {' / '.join(words)}")
    mac_notification("; ".join(parts), "New congressional trades")


def record_failure(error, notify_failures):
    """Warn once when checks have been failing for over an hour, instead of on every blip."""
    with locked():
        if not STATE.exists():
            return
        state = json.loads(STATE.read_text())
        current = now()
        since = parse_time(state.setdefault("failing_since", now_iso()))
        if notify_failures and not state.get("failure_notified") and current - since > dt.timedelta(hours=1):
            mac_notification(f"Checks have failed since {since:%H:%M}: {error}", "Disclosure watcher is failing")
            state["failure_notified"] = True
        write_json(STATE, state)


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


@contextlib.contextmanager
def locked():
    # The background job and the Claude task both touch state.json and pending.json.
    with LOCK.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def check(cfg, years, notify_new):
    state = json.loads(STATE.read_text()) if STATE.exists() else None
    if state is None or state.get("members") != cfg["members"]:
        # First run, or a different set of members: treat everything already filed
        # as seen, so only future filings count.
        filings, etags = member_filings(cfg, years)
        write_json(STATE, {"members": cfg["members"], "seen": [f["doc_id"] for f in filings], "etags": etags,
                           "last_check": now_iso()})
        return {"status": "initialized", "baseline_filings": len(filings),
                "latest_filing_date": filings[-1]["filing_date"] if filings else None}

    seen = set(state["seen"])
    filings, etags = member_filings(cfg, years, state.get("etags"))
    # If a PDF can't be fetched or read yet, this raises before anything is saved,
    # so the next run retries the same filing.
    new = [enrich(f, cfg) for f in filings if f["doc_id"] not in seen]
    notable = [{k: f[k] for k in ("doc_id", "member", "filing_date", "url", "signals", "problems")}
               for f in new if f["signals"]]
    if notable:
        append_log(cfg, notable)
        pending = json.loads(PENDING.read_text()) if PENDING.exists() else []
        write_json(PENDING, pending + notable)
    state.update(seen=sorted(seen | {f["doc_id"] for f in new}), etags=etags,
                 last_check=now_iso())
    state.pop("failing_since", None)
    state.pop("failure_notified", None)
    write_json(STATE, state)
    if notable and notify_new:
        notify(cfg, notable)
    if notable:
        return {"status": "new_filings", "filings": notable, "other_new_filings": len(new) - len(notable)}
    return {"status": "no_new_filings", "checked_at": state["last_check"], "filings_without_signals": len(new)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--latest", type=int, help="show the last N filings without updating state")
    ap.add_argument("--pending", action="store_true", help="show filings waiting for the Liquid step")
    ap.add_argument("--done", nargs="+", metavar="DOC_ID", help="remove handled filings from the queue")
    ap.add_argument("--notify", action="store_true", help="send a macOS notification for new filings")
    ap.add_argument("--quiet", action="store_true", help="print nothing when there's nothing new")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text())
    today = now().date()
    # Filings are indexed by the year they're filed; early January can still see last year's.
    years = [today.year] + ([today.year - 1] if today.month == 1 else [])

    if args.latest:
        filings, _ = member_filings(cfg, years)
        out = [enrich(f, cfg) for f in filings[-args.latest:]]
        print(json.dumps({"member": cfg, "filings": out}, indent=2))
        return

    if not (args.pending or args.done):
        try:
            with locked():
                result = check(cfg, years, args.notify)
        except Exception as e:
            record_failure(e, args.notify)
            raise
        if not (args.quiet and result["status"] == "no_new_filings"):
            print(json.dumps(result, indent=2))
        return

    with locked():
        pending = json.loads(PENDING.read_text()) if PENDING.exists() else []
        if args.done:
            pending = [f for f in pending if f["doc_id"] not in set(args.done)]
            write_json(PENDING, pending)
        last = json.loads(STATE.read_text()).get("last_check") if STATE.exists() else None
    age = (now() - parse_time(last)).total_seconds() / 60 if last else None
    print(json.dumps({"member": cfg, "pending": pending, "last_check": last,
                      "minutes_since_last_check": round(age) if age is not None else None}, indent=2))


if __name__ == "__main__":
    main()
