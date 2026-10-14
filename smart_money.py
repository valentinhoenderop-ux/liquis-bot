#!/usr/bin/env python3
"""Follow the best traders on Hyperliquid, the exchange behind Liquid.

Once a day, picks proven and currently profitable directional traders from
Hyperliquid's public leaderboard. Each run, snapshots their open positions
(crypto plus xyz stocks/indices/commodities) and reports where they agree and
what they changed since the last snapshot.

Usage:
  python3 smart_money.py snapshot   # refresh the trader list if it's a day old, then snapshot positions
  python3 smart_money.py traders    # show who is being followed
"""
import argparse
import datetime as dt
import json
import sys
import time
import urllib.request
from pathlib import Path

from trader import CONFIG, ROOT, now, now_iso, parse_time, post

TRADERS = ROOT / "smart_money_traders.json"
LATEST = ROOT / "smart_money_latest.json"
MOVES_LOG = ROOT / "smart_money_moves.json"
MOVES_KEPT = 300
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


def select_traders(sm):
    req = urllib.request.Request(LEADERBOARD_URL, headers={"User-Agent": "liquid-bot"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        rows = json.loads(resp.read())["leaderboardRows"]
    picked = []
    for r in rows:
        value = float(r["accountValue"])
        if value < sm["min_account_value"]:
            continue
        w = {name: {k: float(v) for k, v in perf.items()} for name, perf in r["windowPerformances"]}
        month, all_time = w["month"], w["allTime"]
        turnover = month["vlm"] / value
        # Proven over time, profitable this month, and actually trading: not a passive
        # holder (turnover near 0), not a market maker (huge turnover), and not a
        # deposit artifact (absurd ROI).
        if all_time["pnl"] < sm["min_all_time_pnl"] or month["pnl"] <= 0:
            continue
        if not sm["min_turnover"] <= turnover <= sm["max_turnover"] or month["roi"] > sm["max_month_roi"]:
            continue
        picked.append({
            "address": r["ethAddress"], "name": r.get("displayName"), "account_value": round(value),
            "month_pnl": round(month["pnl"]), "month_roi_pct": round(month["roi"] * 100, 1),
            "all_time_pnl": round(all_time["pnl"]), "monthly_turnover": round(turnover, 1),
        })
    picked.sort(key=lambda t: -t["month_pnl"])
    return {"selected_at": now_iso(), "traders": picked[:sm["traders"]]}


def trader_positions(address, dexes):
    positions, value = {}, 0.0
    for dex in dexes:
        state = post({"type": "clearinghouseState", "user": address, **({"dex": dex} if dex else {})})
        value += float(state["marginSummary"]["accountValue"])
        for ap in state["assetPositions"]:
            p = ap["position"]
            size = float(p["szi"])
            if size:
                positions[p["coin"]] = {"size": size, "notional": round(float(p["positionValue"]))}
    return positions, value


def label(t):
    return t["name"] or t["address"][:10]


def diff(before, after):
    """What one trader changed between two snapshots."""
    moves = []
    for coin in set(before) | set(after):
        b, a = before.get(coin), after.get(coin)
        if b and not a:
            moves.append((coin, "closed", "long" if b["size"] > 0 else "short", b["notional"]))
        elif a and not b:
            moves.append((coin, "opened", "long" if a["size"] > 0 else "short", a["notional"]))
        elif (a["size"] > 0) != (b["size"] > 0):
            moves.append((coin, "flipped to", "long" if a["size"] > 0 else "short", a["notional"]))
        elif abs(a["size"]) >= abs(b["size"]) * 1.25:
            moves.append((coin, "added to", "long" if a["size"] > 0 else "short", a["notional"]))
        elif abs(a["size"]) <= abs(b["size"]) * 0.5:
            moves.append((coin, "cut", "long" if a["size"] > 0 else "short", a["notional"]))
    return moves


def snapshot(cfg):
    sm = cfg["smart_money"]
    roster = json.loads(TRADERS.read_text()) if TRADERS.exists() else None
    age_h = (now() - parse_time(roster["selected_at"])).total_seconds() / 3600 if roster else None
    if roster is None or age_h >= sm["refresh_hours"]:
        roster = select_traders(sm)
        TRADERS.write_text(json.dumps(roster, indent=2))

    prev = json.loads(LATEST.read_text()) if LATEST.exists() else {"positions": {}}
    books, by_symbol, moves, errors = {}, {}, [], []
    for t in roster["traders"]:
        try:
            positions, value = trader_positions(t["address"], cfg["dexes"])
        except Exception as e:
            errors.append(f"{label(t)}: {e}")
            continue
        time.sleep(0.05)
        books[t["address"]] = positions
        for coin, p in positions.items():
            s = by_symbol.setdefault(coin, {"symbol": coin, "long_traders": 0, "short_traders": 0,
                                            "long_notional": 0.0, "short_notional": 0.0})
            side = "long" if p["size"] > 0 else "short"
            s[f"{side}_traders"] += 1
            s[f"{side}_notional"] += p["notional"]
        if t["address"] in prev["positions"]:
            for coin, action, side, notional in diff(prev["positions"][t["address"]], positions):
                moves.append({"trader": label(t), "symbol": coin, "action": action, "side": side,
                              "notional": round(notional),
                              "pct_of_account": round(notional / value * 100, 1) if value else None,
                              "trader_month_pnl": t["month_pnl"]})

    consensus = []
    for s in by_symbol.values():
        n = s["long_traders"] + s["short_traders"]
        share = s["long_traders"] / n
        net = s["long_notional"] - s["short_notional"]
        side = "long" if share >= sm["min_consensus_share"] and net > 0 else \
               "short" if share <= 1 - sm["min_consensus_share"] and net < 0 else None
        s.update(traders=n, long_share_pct=round(share * 100), net_notional=round(net),
                 long_notional=round(s["long_notional"]), short_notional=round(s["short_notional"]),
                 consensus=side if n >= sm["min_traders"] and abs(net) >= sm["min_net_notional"] else None)
        if s["consensus"]:
            consensus.append(s)
    consensus.sort(key=lambda s: -abs(s["net_notional"]))
    moves.sort(key=lambda m: -m["notional"])

    out = {
        "as_of": now_iso(),
        "traders_followed": len(roster["traders"]),
        "traders_with_positions": sum(1 for b in books.values() if b),
        "consensus": consensus,
        "by_symbol": by_symbol,
        "positions": books,
        "fresh_moves": moves,
        "errors": errors,
    }
    tmp = LATEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, separators=(",", ":")))
    tmp.replace(LATEST)
    if moves:
        log = json.loads(MOVES_LOG.read_text()) if MOVES_LOG.exists() else []
        log = [dict(m, at=out["as_of"]) for m in moves] + log
        MOVES_LOG.write_text(json.dumps(log[:MOVES_KEPT], indent=2))
    return out


def load_consensus(max_age_hours=3):
    """{symbol: by_symbol entry} from a recent snapshot, or {} when there isn't one."""
    if not LATEST.exists():
        return {}
    snap = json.loads(LATEST.read_text())
    age = now() - parse_time(snap["as_of"])
    return snap["by_symbol"] if age < dt.timedelta(hours=max_age_hours) else {}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["snapshot", "traders"])
    args = ap.parse_args()
    cfg = json.loads(CONFIG.read_text())
    if args.cmd == "traders":
        print(TRADERS.read_text() if TRADERS.exists() else "No trader list yet; run snapshot first.")
        return
    out = snapshot(cfg)
    print(json.dumps({k: out[k] for k in ["as_of", "traders_followed", "traders_with_positions", "errors"]}
                     | {"consensus": out["consensus"][:15], "fresh_moves": out["fresh_moves"][:20]}, indent=2))


if __name__ == "__main__":
    main()
