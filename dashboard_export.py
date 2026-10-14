#!/usr/bin/env python3
"""Write the dashboard's data to dashboard/snapshot.json.

One JSON array of rows, each tagged by "kind":
  meta    the account's headline numbers, once
  equity  the equity curve, one row per hourly tick
  trade   open and closed paper trades
  market  every live market on Liquid's dexes, with the top traders' split and any bot signal
  move    the latest positions the followed top traders opened, added, cut, closed or flipped

Usage: python3 dashboard_export.py
"""
import datetime as dt
import json
from pathlib import Path

from trader import CONFIG, ROOT, TZ, direction, equity, load_account, now, now_iso, parse_time, post, today

OUT = ROOT / "dashboard" / "snapshot.json"
COMMODITIES = {"BRENTOIL", "CL", "COPPER", "GOLD", "HO", "NATGAS", "PALLADIUM", "PLATINUM", "SILVER"}
FX = {"EUR", "GBP", "JPY"}
INDICES_ETFS = {"SP500", "XYZ100", "JP225", "KR200", "MAGS", "EWJ", "EWT", "EWY", "EWZ", "KORU", "SMH",
                "SOXL", "TLT", "XBI", "XLE", "URNM", "DRAM"}


def category(symbol, dex):
    if dex == "core":
        return "Crypto"
    name = symbol.split(":")[-1]
    if name in COMMODITIES:
        return "Commodities"
    if name in FX:
        return "FX"
    if name in INDICES_ETFS:
        return "Indices & ETFs"
    return "Stocks"


def stamp(iso):
    """Any saved time as ISO with its offset, in the bot's timezone."""
    return parse_time(iso).astimezone(TZ).isoformat(timespec="seconds") if iso else None


def fresh(path, hours):
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    at = data.get("as_of") or data.get("at")
    return data if at and now() - parse_time(at) < dt.timedelta(hours=hours) else None


def market_rows(cfg, acct):
    crowd = (fresh(ROOT / "smart_money_latest.json", 3) or {}).get("by_symbol", {})
    scan = fresh(ROOT / "last_scan.json", 2) or {}
    signals = {c["symbol"]: f"breakout {c['side']}" for c in scan.get("candidates", [])}
    signals.update({c["symbol"]: f"follow {c['side']}" for c in scan.get("smart_money_candidates", [])
                    if c["symbol"] not in signals})
    rows = []
    for dex in cfg["dexes"]:
        meta, ctxs = post({"type": "metaAndAssetCtxs", **({"dex": dex} if dex else {})})
        for asset, ctx in zip(meta["universe"], ctxs):
            if asset.get("isDelisted") or not ctx.get("markPx"):
                continue
            sym, mark = asset["name"], float(ctx["markPx"])
            prev = float(ctx.get("prevDayPx") or 0)
            c = crowd.get(sym, {})
            held = acct["positions"].get(sym)
            rows.append({
                "kind": "market", "symbol": sym, "name": sym.split(":")[-1],
                "category": category(sym, dex or "core"),
                "price": mark,
                "change_24h": round(mark / prev - 1, 5) if prev else None,
                "volume_24h": round(float(ctx["dayNtlVlm"])),
                "open_interest": round(float(ctx["openInterest"]) * mark),
                "funding_apr": round(float(ctx["funding"]) * 24 * 365, 5),
                "top_long": c.get("long_traders", 0),
                "top_short": c.get("short_traders", 0),
                "top_net": c.get("net_notional", 0),
                "top_view": c.get("consensus") or "",
                "signal": signals.get(sym, ""),
                "held": held["side"] if held else "",
            })
    return rows


def main():
    cfg = json.loads(CONFIG.read_text())
    acct = load_account(cfg)
    markets = market_rows(cfg, acct)
    marks = {m["symbol"]: m["price"] for m in markets}
    now_s = now_iso()

    trades = []
    for sym, p in acct["positions"].items():
        mark = marks.get(sym, p["entry_px"])
        risk = abs(p["entry_px"] - p["initial_stop"]) * p["qty"]
        pnl = (mark - p["entry_px"]) * p["qty"] * direction(p) - p["fees"] - p["funding"]
        trades.append({
            "kind": "trade", "id": f"{sym} {p['entry_time']}", "symbol": sym, "side": p["side"], "status": "open",
            "opened": stamp(p["entry_time"]), "closed": None, "entry": p["entry_px"], "price": mark,
            "stop": p["stop"], "initial_stop": p["initial_stop"], "notional": round(p["qty"] * mark, 2),
            "risk": round(risk, 2), "pnl": round(pnl, 2), "r": round(pnl / risk, 2) if risk else None,
            "reason": p["reason"], "exit_reason": None,
        })
    for t in acct["closed"]:
        risk = t["net_pnl"] / t["return_on_risk"] if t["return_on_risk"] else None
        trades.append({
            "kind": "trade", "id": f"{t['symbol']} {t['entry_time']}", "symbol": t["symbol"], "side": t["side"],
            "status": "closed", "opened": stamp(t["entry_time"]), "closed": stamp(t["exit_time"]),
            "entry": t["entry_px"], "price": t["exit_px"], "stop": None, "initial_stop": None,
            "notional": round(t["qty"] * t["exit_px"], 2), "risk": round(abs(risk), 2) if risk else None,
            "pnl": round(t["net_pnl"], 2), "r": round(t["return_on_risk"], 2),
            "reason": t["entry_reason"], "exit_reason": t["exit_reason"],
        })

    equity_now = round(equity(acct, {s: {"mark": m} for s, m in marks.items()}), 2)
    curve = [{"t": stamp(acct["created"]), "equity": acct["starting_equity"]}]
    curve += [{"t": stamp(e["t"]), "equity": e["equity"]} for e in acct["equity_history"]]
    curve.append({"t": stamp(now_s), "equity": equity_now})
    peak, max_dd = curve[0]["equity"], 0.0
    for e in curve:
        peak = max(peak, e["equity"])
        max_dd = max(max_dd, 1 - e["equity"] / peak)
    day = acct["days"].get(today(), {"start_equity": equity_now})
    closed = [t for t in trades if t["status"] == "closed"]
    open_risk = sum(max(0.0, (p["entry_px"] - p["stop"]) * direction(p)) * p["qty"] for p in acct["positions"].values())

    sm = fresh(ROOT / "smart_money_latest.json", 3) or {}
    meta = {
        "kind": "meta", "as_of": stamp(now_s), "starting_equity": acct["starting_equity"], "equity": equity_now,
        "total_return": round(equity_now / acct["starting_equity"] - 1, 5),
        "today_pnl": round(equity_now - day["start_equity"], 2),
        "max_drawdown": round(max_dd, 5),
        "open_positions": len(acct["positions"]), "open_risk": round(open_risk, 2),
        "gross_exposure": round(sum(t["notional"] for t in trades if t["status"] == "open"), 2),
        "closed_trades": len(closed), "wins": sum(1 for t in closed if t["pnl"] > 0),
        "win_rate": round(sum(1 for t in closed if t["pnl"] > 0) / len(closed), 4) if closed else None,
        "realized_pnl": round(sum(t["pnl"] for t in closed), 2),
        "trading_halted": acct.get("halted") or "",
        "traders_followed": sm.get("traders_followed"), "top_traders_as_of": stamp(sm.get("as_of")),
        "markets": len(markets),
    }

    log = ROOT / "smart_money_moves.json"
    moves = [{"kind": "move", "id": f"{m['at']} {m['trader']} {m['symbol']} {m['action']}", "at": stamp(m["at"]),
              "trader": m["trader"], "symbol": m["symbol"], "action": m["action"], "side": m["side"],
              "notional": m["notional"], "pct_of_account": m["pct_of_account"]}
             for m in (json.loads(log.read_text()) if log.exists() else [])[:150]]

    rows = [meta] + [dict(kind="equity", **e) for e in curve] + trades + markets + moves
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(rows, separators=(",", ":")))
    print(json.dumps({"wrote": str(OUT), "bytes": OUT.stat().st_size, "rows": len(rows),
                      "equity": equity_now, "open_positions": len(acct["positions"]),
                      "markets": len(markets), "moves": len(moves)}))


if __name__ == "__main__":
    main()
