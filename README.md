# trading-recorders

Personal market-data recorders that run on GitHub Actions. **Scripts only:** the data they collect is stored
privately, and nothing here gives trading signals or advice.

| Workflow | What it records | When (US Mountain time) |
|---|---|---|
| Record (`mode normal`) | options-implied expected move for a set of futures, indexes, ETFs and stocks; volatility indexes | weekdays 15:30 |
| Record (`mode context`) | index, bond and breadth snapshots | with the 15:30 run |
| Record (`mode calendar`) | US economic calendar: morning summary and reminders by phone (ntfy); week ahead on Sundays | weekdays 04:45, Sundays 14:00 |
| XSP options | XSP same-day option quotes every 5 minutes | weekdays 07:30-10:00 |

Sources: Cboe delayed quotes (tastytrade as backup), tastytrade's read-only API, Schwab's market-data API, TradingView's economic
calendar, U.S. Treasury auction data, Yahoo chart data. Credentials are GitHub secrets of this repository.

Not trading advice. Provided as is, for the owner's own records.
