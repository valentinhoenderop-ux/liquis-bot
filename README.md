# Liquid paper bot

An hourly paper-trading bot for Liquid's markets (Hyperliquid perpetuals: crypto plus
xyz stocks, indices and commodities). Everything is simulated; no real orders are placed.

Each hour a Claude routine:

1. `python3 trader.py tick` runs stops, trailing stops and time exits, and charges funding.
2. `python3 smart_money.py snapshot` records what the 40 followed top Hyperliquid traders hold.
3. `python3 watch_disclosures.py` checks House members' new stock-trade filings.
4. `python3 trader.py scan` finds breakouts and top-trader consensus. Claude checks them
   against Liquid's news and positioning and trades through `trader.py open` / `close`.
5. `python3 dashboard_export.py` writes the dashboard snapshot, which is uploaded to the
   Liquid Paper Desk dashboard.
6. The account and logs are committed back to this repository.

| File | What it holds |
| --- | --- |
| `trader_config.json` | Risk limits, fees, strategy and top-trader settings |
| `config.json` | Which House members to watch and the minimum trade size |
| `paper_account.json` | The paper account: cash, positions, closed trades, equity history |
| `journal.md` | Every trade with the reason it was taken |
| `reports/` | Daily reports |
| `trades_log.md` | Congressional trades that passed the filter |
