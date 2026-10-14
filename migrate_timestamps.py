#!/usr/bin/env python3
"""One-time fix: give every saved timestamp its UTC offset.

Before 2026-10-15 the bot saved times without an offset. The Mac wrote Madrid time and
the cloud writes UTC, so two identical-looking strings could be two hours apart. A time
that was already in the state the Mac saved (commit f84ffae) is Madrid time; anything
added since came from the cloud and is UTC. Times that already carry an offset are left
alone, so running this again changes nothing.

Usage: python3 migrate_timestamps.py [--mac-commit f84ffae]
"""
import argparse
import datetime as dt
import json
import re
import subprocess

from trader import ROOT, TZ, now_iso

changed = []


def old_text(commit, name):
    out = subprocess.run(["git", "show", f"{commit}:{name}"], cwd=ROOT, capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else None


def old_json(commit, name):
    text = old_text(commit, name)
    return json.loads(text) if text else None


def fix_journal(commit):
    """Journal lines the cloud wrote before this fix carry UTC times; show them in Madrid time.

    Converted lines get a "(Madrid)" marker after the time, so a second run skips them."""
    path = ROOT / "journal.md"
    if not path.exists():
        return
    mac_lines = set((old_text(commit, "journal.md") or "").splitlines())
    out = []
    for line in path.read_text().splitlines():
        m = re.match(r"^- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) (?!\(Madrid\))(.*)$", line)
        if m and line not in mac_lines:
            t = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc).astimezone(TZ)
            line = f"- {t:%Y-%m-%d %H:%M} (Madrid) {m.group(2)}"
            changed.append(f"journal: {m.group(1)} (cloud, UTC) -> {t:%Y-%m-%d %H:%M}")
        out.append(line)
    path.write_text("\n".join(out) + "\n")


def fix(value, from_mac, where):
    if not value:
        return value
    t = dt.datetime.fromisoformat(value)
    if t.tzinfo:
        return value
    fixed = t.replace(tzinfo=TZ if from_mac else dt.timezone.utc).astimezone(TZ).isoformat(timespec="seconds")
    changed.append(f"{where}: {value} ({'Mac, Madrid' if from_mac else 'cloud, UTC'}) -> {fixed}")
    return fixed


def update(name, fn, compact=False):
    path = ROOT / name
    if not path.exists():
        return
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data, separators=(",", ":")) if compact else json.dumps(data, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mac-commit", default="f84ffae")
    commit = ap.parse_args().mac_commit
    account_file = ROOT / "paper_account.json"
    if json.loads(account_file.read_text()).get("timestamps_migrated"):
        print("Already migrated; nothing to do.")
        return
    old = {name: old_json(commit, name) or {} for name in [
        "paper_account.json", "smart_money_traders.json", "smart_money_latest.json", "last_scan.json", "state.json"]}
    old_moves = old_json(commit, "smart_money_moves.json") or []

    def account(a):
        o = old["paper_account.json"]
        o_pos = {s: p["entry_time"] for s, p in o.get("positions", {}).items()}
        o_hist = [e["t"] for e in o.get("equity_history", [])]
        o_exits = {(t["symbol"], t["exit_time"]) for t in o.get("closed", [])}
        a["created"] = fix(a["created"], a["created"] == o.get("created"), "account created")
        for i, e in enumerate(a["equity_history"]):
            e["t"] = fix(e["t"], i < len(o_hist) and e["t"] == o_hist[i], f"equity point {i}")
        for s, p in a["positions"].items():
            p["entry_time"] = fix(p["entry_time"], o_pos.get(s) == p["entry_time"], f"{s} entry")
        for t in a["closed"]:
            t["entry_time"] = fix(t["entry_time"], o_pos.get(t["symbol"]) == t["entry_time"], f"{t['symbol']} entry")
            t["exit_time"] = fix(t["exit_time"], (t["symbol"], t["exit_time"]) in o_exits, f"{t['symbol']} exit")

    def field(name, key):
        def run(d):
            if d.get(key):
                d[key] = fix(d[key], d[key] == old[name].get(key), f"{name} {key}")
        return run

    def state(d):
        for key in ("last_check", "failing_since"):
            if d.get(key):
                d[key] = fix(d[key], d[key] == old["state.json"].get(key), f"state.json {key}")

    def moves(log):
        o_at = {m.get("at") for m in old_moves}
        for m in log:
            m["at"] = fix(m.get("at"), m.get("at") in o_at, f"move {m.get('trader')} {m.get('symbol')}")

    update("paper_account.json", account)
    update("smart_money_traders.json", field("smart_money_traders.json", "selected_at"))
    update("smart_money_latest.json", field("smart_money_latest.json", "as_of"), compact=True)
    update("last_scan.json", field("last_scan.json", "at"))
    update("state.json", state)
    update("smart_money_moves.json", moves)
    fix_journal(commit)
    update("paper_account.json", lambda a: a.update(timestamps_migrated=now_iso()))
    print(f"{len(changed)} timestamps given an offset")
    for line in changed:
        print("  " + line)


if __name__ == "__main__":
    main()
