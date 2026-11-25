# trading-recorders

Personal market-data recorders that run on GitHub Actions. **Scripts only:** the data they collect is stored
privately, and nothing here gives trading signals or advice.

| Workflow | What it records | When (US Mountain time) |
|---|---|---|
| Record (`mode normal`) | options-implied expected move for a set of futures, indexes, ETFs and stocks; volatility indexes | weekdays 15:30 |
| Record (`mode context`) | index, bond and breadth snapshots | with the 15:30 run |
| Record (`mode calendar`) | US economic calendar: morning summary and reminders by phone (ntfy); week ahead on Sundays | weekdays 04:45, Sundays 14:00 |
| News | results of scheduled US releases (actual vs forecast), Fed speakers, Fed speeches and statements, Trump posts with a market word, White House actions, breaking market headlines (free RSS feeds) | every 5 minutes |
| XSP options | XSP same-day option quotes every 5 minutes | weekdays 07:30-10:00 |

Sources: Cboe delayed quotes (tastytrade as backup), tastytrade's read-only API, Schwab's market-data API, TradingView's economic
calendar, Forex Factory's calendar (Fed speakers), U.S. Treasury auction data, Yahoo chart data; news: federalreserve.gov RSS, trumpstruth.org RSS, whitehouse.gov RSS, MarketWatch bulletins RSS. Credentials are GitHub secrets of this repository.

Not trading advice. Provided as is, for the owner's own records.

## Cboe pause
Cboe refuses automated requests since 2026-11-21. When it fails for the first 3 symbols, the recorder writes
`cboe-status.txt` (`refused DATE`) to the data branch and stops asking Cboe; every 7 days it sends one test
request and goes back to Cboe if that works. Backups meanwhile: tastytrade, then Schwab for volatility indexes.
LATEST.md also shows "IV ±" columns: the one-standard-deviation move from at-the-money implied volatility.
