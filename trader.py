#!/usr/bin/env python3
"""Paper trader for Liquid's markets: Hyperliquid perps, crypto plus the xyz stocks/indices/commodities.

Keeps its own simulated account in paper_account.json, using live Hyperliquid
prices (the same markets and prices Liquid shows) and Liquid's fee rates.
Nothing here touches real money or the Liquid app.

Strategy: trade 1h breakouts in the direction of the trend. Each trade risks a
fixed share of equity to a 2x ATR stop, moves to breakeven after 1 ATR, then
trails 3 ATR behind the best price.

Usage:
  python3 trader.py tick                  # mark to market, run stop/trailing/time exits, charge funding
  python3 trader.py scan [--top N]        # rank breakout signals across liquid markets
  python3 trader.py open SYMBOL long|short [--size-mult X] --reason "..."
  python3 trader.py close SYMBOL --reason "..."
  python3 trader.py auto                  # tick, then open the best signals with no AI review
  python3 trader.py status
  python3 trader.py report [--date YYYY-MM-DD]
  python3 trader.py resume                # clear a max-drawdown halt
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("TRADER_HOME") or Path(__file__).resolve().parent)
CONFIG = Path(__file__).resolve().parent / "trader_config.json"
ACCOUNT = ROOT / "paper_account.json"
JOURNAL = ROOT / "journal.md"
REPORTS = ROOT / "reports"
LOCK = ROOT / ".trader.lock"
API = "https://api.hyperliquid.xyz/info"
# Every timestamp carries its offset and days follow this zone, so the Mac (Madrid time)
# and the cloud (UTC) record the same moment the same way.
TZ = ZoneInfo(json.loads(CONFIG.read_text()).get("timezone", "Europe/Madrid"))
HOUR_MS = 3_600_000


# --- market data -------------------------------------------------------------

def post(body, retries=5):
    data = json.dumps(body).encode()
    for attempt in range(retries):
        req = urllib.request.Request(API, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            if attempt == retries - 1 or (isinstance(e, urllib.error.HTTPError) and e.code not in (429, 500, 502, 503)):
                raise
            time.sleep(2 ** attempt)


def fetch_markets(cfg):
    """Every live market on the configured dexes, keyed by Liquid symbol (BTC, xyz:NVDA)."""
    out = {}
    for dex in cfg["dexes"]:
        meta, ctxs = post({"type": "metaAndAssetCtxs", **({"dex": dex} if dex else {})})
        for asset, ctx in zip(meta["universe"], ctxs):
            if asset.get("isDelisted") or not ctx.get("markPx"):
                continue
            out[asset["name"]] = {
                "symbol": asset["name"],
                "dex": dex or "core",
                "mid": float(ctx.get("midPx") or ctx["markPx"]),
                "mark": float(ctx["markPx"]),
                "funding": float(ctx["funding"]),  # hourly rate; longs pay when positive
                "volume": float(ctx["dayNtlVlm"]),
                "max_leverage": asset.get("maxLeverage", 1),
            }
    return out


def fetch_candles(symbol, interval, start_ms):
    rows = post({"type": "candleSnapshot", "req": {"coin": symbol, "interval": interval, "startTime": int(start_ms)}})
    return [{"t": r["t"], "T": r["T"], "o": float(r["o"]), "h": float(r["h"]),
             "l": float(r["l"]), "c": float(r["c"])} for r in rows]


def hourly_bars(symbol, hours=110):
    """Closed 1h bars, oldest first."""
    now = time.time() * 1000
    return [b for b in fetch_candles(symbol, "1h", now - hours * HOUR_MS) if b["T"] < now]


# --- indicators --------------------------------------------------------------

def ema(values, n):
    k, e = 2 / (n + 1), values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def atr(bars, n=14):
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(bars, bars[1:])]
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def analyze(bars, cfg):
    """Trend and breakout state of the latest closed bar, or None without enough history."""
    look = cfg["breakout_lookback_hours"]
    if len(bars) < max(60, look + 16):
        return None
    closes = [b["c"] for b in bars]
    last, prior = bars[-1], bars[-1 - look:-1]
    return {
        "close": last["c"],
        "ema20": ema(closes, 20),
        "ema50": ema(closes, 50),
        "atr": atr(bars),
        "high": max(b["h"] for b in prior),
        "low": min(b["l"] for b in prior),
    }


def returns(bars, n=72):
    """Hourly returns keyed by bar open time, for correlation checks."""
    tail = bars[-(n + 1):]
    return {b["t"]: b["c"] / a["c"] - 1 for a, b in zip(tail, tail[1:])}


def correlation(r1, r2):
    keys = sorted(set(r1) & set(r2))
    if len(keys) < 24:
        return 0.0
    xs, ys = [r1[k] for k in keys], [r2[k] for k in keys]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx, vy = sum((x - mx) ** 2 for x in xs), sum((y - my) ** 2 for y in ys)
    return cov / (vx * vy) ** 0.5 if vx and vy else 0.0


def signal(mkt, ind, cfg):
    """A breakout in the trend's direction, scored by strength, trend and funding cost."""
    a, c = ind["atr"], ind["close"]
    if not cfg["min_atr_pct"] <= a / c <= cfg["max_atr_pct"]:
        return None
    if c > ind["high"] and ind["ema20"] > ind["ema50"] and c > ind["ema50"]:
        side, strength = "long", (c - ind["high"]) / a
    elif c < ind["low"] and ind["ema20"] < ind["ema50"] and c < ind["ema50"]:
        side, strength = "short", (ind["low"] - c) / a
    else:
        return None
    if strength > cfg["max_breakout_atr"]:
        return None  # already ran too far past the level to chase
    trend = abs(ind["ema20"] - ind["ema50"]) / a
    funding_cost = mkt["funding"] if side == "long" else -mkt["funding"]
    score = min(strength, 2) + 0.5 * min(trend, 3) - max(funding_cost, 0) / 0.0001 * 0.5
    return {
        "symbol": mkt["symbol"], "side": side, "score": round(score, 3),
        "price": mkt["mid"], "atr": a, "atr_pct": round(a / c * 100, 3),
        "stop": mkt["mid"] - cfg["atr_stop_mult"] * a if side == "long" else mkt["mid"] + cfg["atr_stop_mult"] * a,
        "breakout_level": ind["high"] if side == "long" else ind["low"],
        "trend_atr": round(trend, 2), "breakout_atr": round(strength, 2),
        "funding_hourly_pct": round(mkt["funding"] * 100, 5),
        "volume_24h": round(mkt["volume"]),
    }


# --- account -----------------------------------------------------------------

def now():
    return dt.datetime.now(TZ)


def now_iso():
    return now().isoformat(timespec="seconds")


def today():
    return now().date().isoformat()


def parse_time(iso):
    """ISO time as an aware datetime; a time saved without an offset is UTC (the cloud's clock)."""
    t = dt.datetime.fromisoformat(iso)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def load_account(cfg):
    if ACCOUNT.exists():
        return json.loads(ACCOUNT.read_text())
    eq = cfg["starting_equity"]
    return {"created": now_iso(), "starting_equity": eq, "cash": eq, "peak_equity": eq, "halted": None,
            "positions": {}, "closed": [], "days": {}, "equity_history": []}


def save_account(acct):
    tmp = ACCOUNT.with_suffix(".tmp")
    tmp.write_text(json.dumps(acct, indent=2))
    tmp.replace(ACCOUNT)


@contextlib.contextmanager
def locked():
    ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def direction(p):
    return 1 if p["side"] == "long" else -1


def unrealized(p, price):
    return (price - p["entry_px"]) * p["qty"] * direction(p)


def equity(acct, mkts):
    return acct["cash"] + sum(unrealized(p, mkts[s]["mark"] if s in mkts else p["entry_px"])
                              for s, p in acct["positions"].items())


def gross_notional(acct, mkts):
    return sum(p["qty"] * (mkts[s]["mark"] if s in mkts else p["entry_px"]) for s, p in acct["positions"].items())


def fee_rate(cfg, dex):
    return cfg["liquid_fee"] + cfg["exchange_taker_fee"].get(dex, max(cfg["exchange_taker_fee"].values()))


def journal(line):
    new = not JOURNAL.exists()
    with JOURNAL.open("a") as fh:
        if new:
            fh.write("# Paper trading journal\n\n")
        fh.write(f"- {now():%Y-%m-%d %H:%M} {line}\n")


def fmt_px(x):
    return f"{x:,.2f}" if x >= 100 else f"{x:,.4f}" if x >= 1 else f"{x:.6g}"


def roll_day(acct, eq):
    """Track each local day's opening equity, for the daily loss limit and reports."""
    today_key = today()
    if today_key not in acct["days"]:
        for d in acct["days"].values():
            d.setdefault("end_equity", eq)
        acct["days"][today_key] = {"start_equity": eq}
    acct["peak_equity"] = max(acct["peak_equity"], eq)
    return acct["days"][today_key]


def entry_block(acct, cfg, eq):
    """Why new trades are not allowed right now, or None."""
    if acct["halted"]:
        return acct["halted"]
    day = roll_day(acct, eq)
    if eq <= day["start_equity"] * (1 - cfg["daily_loss_limit"]):
        return f"daily loss limit hit (down {1 - eq / day['start_equity']:.1%} today); no new trades until tomorrow"
    if eq <= acct["peak_equity"] * (1 - cfg["max_drawdown"]):
        acct["halted"] = (f"max drawdown hit ({1 - eq / acct['peak_equity']:.1%} below peak) on {now_iso()}; "
                          "run `python3 trader.py resume` to allow new trades again")
        journal(f"**HALTED**: {acct['halted']}")
        return acct["halted"]
    return None


# --- trading -----------------------------------------------------------------

def open_position(acct, cfg, mkts, symbol, side, reason, size_mult=1.0, bars=None):
    if symbol not in mkts:
        raise SystemExit(f"{symbol} is not a live market on {cfg['dexes']}")
    mkt = mkts[symbol]
    if symbol in acct["positions"]:
        raise SystemExit(f"already holding {symbol}")
    if len(acct["positions"]) >= cfg["max_positions"]:
        raise SystemExit(f"max positions ({cfg['max_positions']}) reached")
    if mkt["volume"] < cfg["min_volume_usd_manual"]:
        raise SystemExit(f"{symbol} trades only ${mkt['volume']:,.0f}/day, below the liquidity floor")
    eq = equity(acct, mkts)
    block = entry_block(acct, cfg, eq)
    if block:
        raise SystemExit(f"new trades blocked: {block}")
    size_mult = max(0.1, min(float(size_mult), 1.0))

    ind = analyze(bars or hourly_bars(symbol), cfg)
    if not ind:
        raise SystemExit(f"not enough price history for {symbol}")
    a, d = ind["atr"], (1 if side == "long" else -1)
    px = mkt["mid"] * (1 + d * cfg["slippage"])
    stop = px - d * cfg["atr_stop_mult"] * a

    qty = eq * cfg["risk_per_trade"] * size_mult / (cfg["atr_stop_mult"] * a)
    cap = eq * min(cfg["max_position_leverage"], mkt["max_leverage"])
    room = eq * cfg["max_gross_leverage"] - gross_notional(acct, mkts)
    qty = min(qty, cap / px, max(room, 0) / px)
    if qty * px < cfg["min_notional"]:
        raise SystemExit(f"no room for {symbol}: would be ${qty * px:,.2f} notional")

    fee = qty * px * fee_rate(cfg, mkt["dex"])
    acct["cash"] -= fee
    now_ms = int(time.time() * 1000)
    acct["positions"][symbol] = {
        "side": side, "qty": qty, "entry_px": px, "entry_time": now_iso(), "entry_ms": now_ms,
        "stop": stop, "initial_stop": stop, "atr": a, "extreme": px, "dex": mkt["dex"],
        "fees": fee, "funding": 0.0, "checked_ms": now_ms, "funding_ms": now_ms, "reason": reason,
    }
    journal(f"OPEN {side.upper()} {symbol} {qty:.6g} @ {fmt_px(px)} (${qty * px:,.0f} notional, "
            f"stop {fmt_px(stop)}, risk ${abs(px - stop) * qty:,.0f}): {reason}")
    return acct["positions"][symbol]


def close_position(acct, cfg, symbol, raw_px, reason):
    p = acct["positions"].pop(symbol)
    px = raw_px * (1 - direction(p) * cfg["slippage"])
    fee = p["qty"] * px * fee_rate(cfg, p["dex"])
    gross = (px - p["entry_px"]) * p["qty"] * direction(p)
    acct["cash"] += gross - fee
    net = gross - p["fees"] - fee - p["funding"]
    trade = {"symbol": symbol, "side": p["side"], "qty": p["qty"], "entry_px": p["entry_px"], "exit_px": px,
             "entry_time": p["entry_time"], "exit_time": now_iso(), "gross_pnl": gross,
             "fees": p["fees"] + fee, "funding": p["funding"], "net_pnl": net,
             "return_on_risk": net / (abs(p["entry_px"] - p["initial_stop"]) * p["qty"]),
             "entry_reason": p["reason"], "exit_reason": reason}
    acct["closed"].append(trade)
    journal(f"CLOSE {p['side'].upper()} {symbol} @ {fmt_px(px)}: net {net:+,.2f} USD "
            f"({trade['return_on_risk']:+.2f}R, fees {trade['fees']:,.2f}, funding {p['funding']:+,.2f}). {reason}")
    return trade


def walk_bars(p, bars, cfg):
    """Replay closed 5m bars since the last check. Returns (exit_price, reason) or None.

    The stop in force before each bar is checked first, then the bar's best price
    ratchets the stop, so a bar never stops out on a level it set itself.
    """
    d = direction(p)
    for b in bars:
        if b["t"] < p["checked_ms"]:
            continue
        hit = b["l"] <= p["stop"] if d > 0 else b["h"] >= p["stop"]
        if hit:
            gap = b["o"] < p["stop"] if d > 0 else b["o"] > p["stop"]
            kind = "trailing stop" if (p["stop"] - p["initial_stop"]) * d > 0 else "stop-loss"
            return (b["o"] if gap else p["stop"]), f"{kind} hit" + (" (gapped through)" if gap else "")
        p["extreme"] = max(p["extreme"], b["h"]) if d > 0 else min(p["extreme"], b["l"])
        if (p["extreme"] - p["entry_px"]) * d >= cfg["breakeven_atr"] * p["atr"]:
            p["stop"] = max(p["stop"], p["entry_px"]) if d > 0 else min(p["stop"], p["entry_px"])
        trail = p["extreme"] - d * cfg["trail_atr_mult"] * p["atr"]
        p["stop"] = max(p["stop"], trail) if d > 0 else min(p["stop"], trail)
        p["checked_ms"] = b["T"] + 1
    return None


def tick(acct, cfg, mkts):
    now_ms = time.time() * 1000
    events = []
    for symbol, p in list(acct["positions"].items()):
        mkt = mkts.get(symbol)
        if not mkt:
            events.append(f"{symbol}: market missing from the exchange feed; left open")
            continue
        hours = (now_ms - p["funding_ms"]) / HOUR_MS
        pay = p["qty"] * mkt["mark"] * mkt["funding"] * hours * direction(p)
        acct["cash"] -= pay
        p["funding"] += pay
        p["funding_ms"] = now_ms

        bars = [b for b in fetch_candles(symbol, "5m", p["checked_ms"]) if b["T"] < now_ms]
        hit = walk_bars(p, bars, cfg)
        if hit:
            t = close_position(acct, cfg, symbol, hit[0], hit[1])
            events.append(f"closed {symbol}: {hit[1]}, net {t['net_pnl']:+,.2f}")
        elif now_ms - p["entry_ms"] > cfg["max_hold_hours"] * HOUR_MS:
            t = close_position(acct, cfg, symbol, mkt["mid"], f"time exit after {cfg['max_hold_hours']}h")
            events.append(f"closed {symbol}: time exit, net {t['net_pnl']:+,.2f}")
    eq = equity(acct, mkts)
    block = entry_block(acct, cfg, eq)
    acct["equity_history"].append({"t": now_iso(), "equity": round(eq, 2)})
    return events, eq, block


def crowd_view(c, side):
    """How the followed top traders sit relative to a trade on this side."""
    if not c:
        return None, "no followed trader holds it"
    text = f"{c['long_traders']}L/{c['short_traders']}S top traders, net ${c['net_notional'] / 1e6:+.1f}M"
    if c["consensus"] == side:
        return "agrees", text
    if c["consensus"] and c["consensus"] != side:
        return "opposes", text
    return "mixed", text


def scan(acct, cfg, mkts, top):
    from smart_money import load_consensus  # imported here: smart_money imports this module
    crowd, sm = load_consensus(), cfg["smart_money"]
    universe = sorted((m for m in mkts.values() if m["volume"] >= cfg["min_volume_usd"]),
                      key=lambda m: -m["volume"])[:cfg["max_markets"]]
    symbols = [m["symbol"] for m in universe]
    symbols += [s for s in acct["positions"] if s not in symbols]
    # Markets the top traders agree on get scanned too, if they're liquid enough to trade.
    symbols += [s for s, c in crowd.items() if c["consensus"] and s not in symbols
                and s in mkts and mkts[s]["volume"] >= cfg["min_volume_usd_manual"]]

    candidates, follows, exits, errors, rets = [], [], [], [], {}
    for symbol in symbols:
        try:
            bars = hourly_bars(symbol)
            ind = analyze(bars, cfg)
        except Exception as e:  # one bad market shouldn't sink the scan
            errors.append(f"{symbol}: {e}")
            continue
        rets[symbol] = returns(bars)
        time.sleep(0.05)
        if not ind or symbol not in mkts:
            continue
        p = acct["positions"].get(symbol)
        if p:
            against = ind["close"] < ind["ema50"] if p["side"] == "long" else ind["close"] > ind["ema50"]
            view, crowd_text = crowd_view(crowd.get(symbol), "short" if p["side"] == "long" else "long")
            if against or view == "agrees":
                exits.append({"symbol": symbol, "side": p["side"], "why": "; ".join(
                    ([f"last close {fmt_px(ind['close'])} is through the 50h EMA {fmt_px(ind['ema50'])}"] if against else [])
                    + ([f"top traders are on the other side ({crowd_text})"] if view == "agrees" else []))})
            continue
        sig = signal(mkts[symbol], ind, cfg)
        c = crowd.get(symbol)
        if sig:
            sig["smart_money_view"], sig["smart_money"] = crowd_view(c, sig["side"])
            if sig["smart_money_view"] == "agrees":
                sig["score"] = round(sig["score"] + sm["score_agree"], 3)
            elif sig["smart_money_view"] == "opposes":
                sig["score"] = round(sig["score"] - sm["score_oppose"], 3)
            candidates.append(sig)
        elif c and c["consensus"]:
            side = c["consensus"]
            with_trend = ind["close"] > ind["ema50"] if side == "long" else ind["close"] < ind["ema50"]
            if with_trend and cfg["min_atr_pct"] <= ind["atr"] / ind["close"] <= cfg["max_atr_pct"]:
                agree = c["long_traders"] if side == "long" else c["short_traders"]
                follows.append({
                    "symbol": symbol, "side": side, "price": mkts[symbol]["mid"],
                    "score": round(min(abs(c["net_notional"]) / 1e7, 1) + agree / 10, 3),
                    "smart_money": crowd_view(c, side)[1],
                    "atr_pct": round(ind["atr"] / ind["close"] * 100, 3),
                    "trend": f"close {fmt_px(ind['close'])} vs 50h EMA {fmt_px(ind['ema50'])}",
                    "funding_hourly_pct": round(mkts[symbol]["funding"] * 100, 5),
                    "volume_24h": round(mkts[symbol]["volume"]),
                })
    candidates.sort(key=lambda s: -s["score"])
    follows.sort(key=lambda s: -s["score"])

    # Several setups in one theme are really one bet: keep the best, skip the rest.
    book = [(s, p["side"]) for s, p in acct["positions"].items() if s in rets]
    picked, picked_follows, correlated = [], [], []
    for group, out in ((candidates, picked), (follows, picked_follows)):
        for c in group:
            overlap = [(s, round(correlation(rets[c["symbol"]], rets[s]), 2)) for s, side in book if side == c["side"]]
            overlap = [(s, r) for s, r in overlap if r > cfg["max_correlation"]]
            if overlap:
                correlated.append({"symbol": c["symbol"], "side": c["side"], "score": c["score"],
                                   "moves_with": [f"{s} (corr {r})" for s, r in overlap]})
            else:
                out.append(c)
                book.append((c["symbol"], c["side"]))
    return {"markets_scanned": len(symbols), "smart_money_snapshot": bool(crowd),
            "candidates": picked[:top], "smart_money_candidates": picked_follows[:top],
            "skipped_as_correlated": correlated, "exit_suggestions": exits, "errors": errors}


# --- reporting ---------------------------------------------------------------

def account_summary(acct, cfg, mkts):
    eq = equity(acct, mkts)
    block = entry_block(acct, cfg, eq)
    day = acct["days"][today()]
    closed = acct["closed"]
    wins = [t for t in closed if t["net_pnl"] > 0]
    return {
        "equity": round(eq, 2),
        "cash": round(acct["cash"], 2),
        "total_return_pct": round((eq / acct["starting_equity"] - 1) * 100, 2),
        "today_pnl": round(eq - day["start_equity"], 2),
        "drawdown_from_peak_pct": round((1 - eq / acct["peak_equity"]) * 100, 2),
        "gross_leverage": round(gross_notional(acct, mkts) / eq, 2) if eq > 0 else None,
        "open_positions": len(acct["positions"]),
        "free_slots": cfg["max_positions"] - len(acct["positions"]),
        "new_trades_blocked": block,
        "closed_trades": len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "positions": [{
            "symbol": s, "side": p["side"], "entry": p["entry_px"], "mark": mkts.get(s, {}).get("mark"),
            "stop": round(p["stop"], 6), "notional": round(p["qty"] * mkts.get(s, {}).get("mark", p["entry_px"]), 2),
            "unrealized": round(unrealized(p, mkts[s]["mark"]) if s in mkts else 0, 2),
            "opened": p["entry_time"], "reason": p["reason"],
        } for s, p in acct["positions"].items()],
    }


def report(acct, cfg, mkts, date):
    s = account_summary(acct, cfg, mkts)
    day = acct["days"].get(date)
    if not day:
        return f"No trading activity recorded for {date}."
    end = day.get("end_equity", s["equity"])
    closed = [t for t in acct["closed"] if t["exit_time"].startswith(date)]
    opened = [t for t in acct["closed"] if t["entry_time"].startswith(date)] + \
             [dict(p, symbol=sym) for sym, p in acct["positions"].items() if p["entry_time"].startswith(date)]
    lines = [
        f"# Paper trading report: {date}", "",
        f"- Equity: ${end:,.2f} (day {end - day['start_equity']:+,.2f}, "
        f"{(end / day['start_equity'] - 1) * 100:+.2f}%)",
        f"- Since start: {s['total_return_pct']:+.2f}% on ${acct['starting_equity']:,.0f}; "
        f"{s['drawdown_from_peak_pct']:.2f}% below peak",
        f"- Trades opened: {len(opened)}; closed: {len(closed)}, net {sum(t['net_pnl'] for t in closed):+,.2f}",
        f"- All-time win rate: {s['win_rate_pct'] if s['win_rate_pct'] is not None else 'n/a'}% "
        f"over {s['closed_trades']} closed trades",
    ]
    if s["new_trades_blocked"]:
        lines.append(f"- New trades blocked: {s['new_trades_blocked']}")
    if closed:
        lines += ["", "## Closed", ""]
        lines += [f"- {t['side']} {t['symbol']}: {fmt_px(t['entry_px'])} → {fmt_px(t['exit_px'])}, "
                  f"net {t['net_pnl']:+,.2f} ({t['return_on_risk']:+.2f}R). {t['exit_reason']}" for t in closed]
    if s["positions"]:
        lines += ["", "## Open now", ""]
        lines += [f"- {p['side']} {p['symbol']} from {fmt_px(p['entry'])}, mark {fmt_px(p['mark'] or 0)}, "
                  f"stop {fmt_px(p['stop'])}, unrealized {p['unrealized']:+,.2f}" for p in s["positions"]]
    text = "\n".join(lines) + "\n"
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"{date}.md").write_text(text)
    return text


# --- CLI ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tick")
    sp = sub.add_parser("scan")
    sp.add_argument("--top", type=int, default=10)
    op = sub.add_parser("open")
    op.add_argument("symbol")
    op.add_argument("side", choices=["long", "short"])
    op.add_argument("--size-mult", type=float, default=1.0, help="0.1 to 1: scale the standard risk down")
    op.add_argument("--reason", required=True)
    cp = sub.add_parser("close")
    cp.add_argument("symbol")
    cp.add_argument("--reason", required=True)
    sub.add_parser("auto")
    sub.add_parser("status")
    rp = sub.add_parser("report")
    rp.add_argument("--date", default=today())
    sub.add_parser("resume")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text())
    with locked():
        acct = load_account(cfg)
        mkts = fetch_markets(cfg)
        roll_day(acct, equity(acct, mkts))
        out = None
        if args.cmd == "tick":
            events, eq, block = tick(acct, cfg, mkts)
            out = {"events": events, "account": account_summary(acct, cfg, mkts)}
        elif args.cmd == "scan":
            res = scan(acct, cfg, mkts, args.top)
            (ROOT / "last_scan.json").write_text(json.dumps({"at": now_iso(), **res}, indent=2, default=str))
            out = {"account": account_summary(acct, cfg, mkts), **res}
        elif args.cmd == "open":
            p = open_position(acct, cfg, mkts, args.symbol, args.side, args.reason, args.size_mult)
            out = {"opened": args.symbol, "side": p["side"], "qty": p["qty"], "entry": p["entry_px"],
                   "notional": round(p["qty"] * p["entry_px"], 2), "stop": p["stop"], "fee": round(p["fees"], 4)}
        elif args.cmd == "close":
            if args.symbol not in acct["positions"]:
                raise SystemExit(f"no open position in {args.symbol}")
            out = close_position(acct, cfg, args.symbol, mkts[args.symbol]["mid"], args.reason)
        elif args.cmd == "auto":
            events, eq, block = tick(acct, cfg, mkts)
            res = scan(acct, cfg, mkts, cfg["max_new_per_run"])
            opened = []
            for sig in res["candidates"]:
                if block or len(acct["positions"]) >= cfg["max_positions"]:
                    break
                if sig["smart_money_view"] == "opposes":
                    events.append(f"skipped {sig['symbol']}: top traders are on the other side")
                    continue
                try:
                    open_position(acct, cfg, mkts, sig["symbol"], sig["side"],
                                  f"auto: {sig['breakout_atr']} ATR breakout, trend {sig['trend_atr']} ATR, "
                                  f"score {sig['score']}")
                    opened.append(sig["symbol"])
                except SystemExit as e:
                    events.append(f"skipped {sig['symbol']}: {e}")
            out = {"events": events, "opened": opened, "account": account_summary(acct, cfg, mkts)}
        elif args.cmd == "status":
            out = account_summary(acct, cfg, mkts)
        elif args.cmd == "report":
            print(report(acct, cfg, mkts, args.date))
        elif args.cmd == "resume":
            acct["halted"] = None
            acct["peak_equity"] = equity(acct, mkts)
            journal("Resumed: drawdown halt cleared, peak reset to current equity")
            out = {"resumed": True}
        save_account(acct)
    if out is not None:
        print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
