"""Market context: the cash indexes, bonds, financials and market breadth that traders read alongside ES/NQ.

Run by the recorder after the expected move (never makes the recording fail). Saves to the market-data branch:
  * context/minute/YYYY/YYYY-MM-DD.csv.gz   1-minute bars of every symbol below for that New York date,
                                            pre-market and after-hours included (symbol,ts_utc,open,high,low,close,volume)
  * context/daily/context_YYYY.csv          one row per symbol per day: regular-session OHLC and volume, the
                                            volume traded before the open (04:00-09:30 and 09:00-09:30 New York)
                                            and in the first 5 and 15 minutes
  * context/daily-history/<SYM>.csv         full daily history (date,open,high,low,close,volume), refreshed on
                                            Fridays and whenever a file is missing
Source: Yahoo's free delayed chart data (1-minute bars reach back about 7 days, so a missed day is filled by the
next run). Market breadth (NYSE/Nasdaq advance-decline, TICK, TRIN, up/down volume) is not on Yahoo; it comes
from the Schwab API when its secrets are set (schwab_context.py). Standard library only.
"""

import csv
import gzip
import io
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UA = "Mozilla/5.0 (personal research recorder)"

# name -> Yahoo symbol (user 2026-10-12: SPX, Nasdaq, bonds, financials, as a trader reads them before the open)
SYMBOLS = {
    # cash indexes
    "SPX": "^GSPC", "NDX": "^NDX", "COMP": "^IXIC", "DJI": "^DJI", "RUT": "^RUT",
    # Treasury yields (Cboe yield indexes: 13-week, 5-, 10-, 30-year) and bond ETFs
    "IRX": "^IRX", "FVX": "^FVX", "TNX": "^TNX", "TYX": "^TYX",
    "SHY": "SHY", "IEF": "IEF", "TLT": "TLT",
    # Treasury futures (continuous front month on Yahoo; the full contracts come from NinjaTrader)
    "ZT": "ZT=F", "ZF": "ZF=F", "ZN": "ZN=F", "TN": "TN=F", "ZB": "ZB=F", "UB": "UB=F",
    # index futures, for their volume before the cash open
    "ES": "ES=F", "NQ": "NQ=F", "YM": "YM=F", "RTY": "RTY=F",
    # financials: sector and bank ETFs, the largest banks
    "XLF": "XLF", "KBE": "KBE", "KRE": "KRE",
    "JPM": "JPM", "BAC": "BAC", "C": "C", "WFC": "WFC", "GS": "GS", "MS": "MS",
    # the big index ETFs (pre-market volume)
    "SPY": "SPY", "QQQ": "QQQ", "IWM": "IWM", "DIA": "DIA",
}
MINUTE_COLUMNS = ["symbol", "ts_utc", "open", "high", "low", "close", "volume"]
DAILY_COLUMNS = ["date", "symbol", "source", "rth_open", "rth_high", "rth_low", "rth_close", "rth_volume",
                 "pre_volume_0400_0930", "pre_volume_0900_0930", "first5_volume", "first15_volume",
                 "first15_high", "first15_low", "bars", "note"]
OPEN, CLOSE = dtime(9, 30), dtime(16, 0)


def chart(yahoo, rng, interval, prepost=False):
    # range=max silently thins daily bars to monthly/quarterly; the full daily history needs period1/period2
    # (from 1900, so SPX and DJI keep their pre-1970 years)
    span = (f"period1=-2208988800&period2={int(time.time())}" if rng == "max" else f"range={rng}")
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(yahoo) +
           f"?{span}&interval={interval}" + ("&includePrePost=true" if prepost else ""))
    last = None
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read().decode("utf-8"))["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            rows = []
            for k, t in enumerate(res.get("timestamp") or []):
                c = q["close"][k]
                if c is None:
                    continue
                rows.append((t, q["open"][k], q["high"][k], q["low"][k], c, q["volume"][k] or 0))
            return rows
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{yahoo}: {last}")


def r6(v):
    return "" if v is None else f"{v:.6f}".rstrip("0").rstrip(".")


def read_minutes(path):
    if not path.exists():
        return {}
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return {(r["symbol"], r["ts_utc"]): r for r in csv.DictReader(f)}


def write_minutes(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=MINUTE_COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(sorted(rows.values(), key=lambda r: (r["symbol"], r["ts_utc"])))
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(buf.getvalue())


def day_summary(day, sym, bars):
    """bars: [(datetime NY, o, h, l, c, v)] of one New York date."""
    rth = [b for b in bars if OPEN <= b[0].time() < CLOSE]
    pre = sum(b[5] for b in bars if dtime(4, 0) <= b[0].time() < OPEN)
    pre30 = sum(b[5] for b in bars if dtime(9, 0) <= b[0].time() < OPEN)
    first5 = [b for b in rth if b[0].time() < dtime(9, 35)]
    first15 = [b for b in rth if b[0].time() < dtime(9, 45)]
    row = {"date": day, "symbol": sym, "source": "yahoo", "bars": len(bars),
           "pre_volume_0400_0930": pre, "pre_volume_0900_0930": pre30,
           "first5_volume": sum(b[5] for b in first5), "first15_volume": sum(b[5] for b in first15)}
    if rth:
        row.update(rth_open=r6(rth[0][1]), rth_high=r6(max(b[2] for b in rth)), rth_low=r6(min(b[3] for b in rth)),
                   rth_close=r6(rth[-1][4]), rth_volume=sum(b[5] for b in rth))
    if first15:
        row.update(first15_high=r6(max(b[2] for b in first15)), first15_low=r6(min(b[3] for b in first15)))
    if not rth:
        row["note"] = "no regular-session bars"
    return row


def write_daily(path, new_rows):
    """Replace the rows of the same (date, symbol, source); keep everything else."""
    rows = {}
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            rows = {(r["date"], r["symbol"], r.get("source", "")): r for r in csv.DictReader(f)}
    for r in new_rows:   # a day at the edge of Yahoo's 7-day window comes back cut short: keep the fuller row
        key = (r["date"], r["symbol"], r["source"])
        if key not in rows or int(r["bars"]) >= int(rows[key].get("bars") or 0):
            rows[key] = r
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=DAILY_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: (r["date"], r["symbol"], r.get("source", ""))))


def refresh_history(out, sym, yahoo):
    rows = chart(yahoo, "max", "1d")
    path = out / "daily-history" / f"{sym}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        for t, o, h, l, c, v in rows:
            w.writerow([datetime.fromtimestamp(t, NY).date().isoformat(), r6(o), r6(h), r6(l), r6(c), v])
    return len(rows)


def record(out_dir, today, history=False):
    """Fetch the last 7 days of 1-minute bars for every symbol; merge them into the per-day files."""
    out = Path(out_dir) / "context"
    report, by_day = [], {}
    for sym, yahoo in SYMBOLS.items():
        try:
            rows = chart(yahoo, "7d", "1m", prepost=True)
        except Exception as e:  # one symbol missing must not stop the others
            report.append(f"{sym}: FAILED {e}")
            continue
        for t, o, h, l, c, v in rows:
            ny = datetime.fromtimestamp(t, NY)
            by_day.setdefault(ny.date().isoformat(), {}).setdefault(sym, []).append((ny, o, h, l, c, v))
        report.append(f"{sym}: {len(rows)} minute bars")
        if history or today.weekday() == 4 or not (out / "daily-history" / f"{sym}.csv").exists():
            try:
                report.append(f"{sym}: daily history {refresh_history(out, sym, yahoo)} days")
            except Exception as e:
                report.append(f"{sym}: daily history FAILED {e}")
    summaries = {}
    for day, syms in sorted(by_day.items()):
        path = out / "minute" / day[:4] / f"{day}.csv.gz"
        merged = read_minutes(path)
        for sym, bars in syms.items():
            for ny, o, h, l, c, v in bars:
                ts = ny.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                merged[(sym, ts)] = {"symbol": sym, "ts_utc": ts, "open": r6(o), "high": r6(h), "low": r6(l),
                                     "close": r6(c), "volume": v}
            summaries.setdefault(day[:4], []).append(day_summary(day, sym, sorted(bars)))
        write_minutes(path, merged)
    for year, rows in summaries.items():
        write_daily(out / "daily" / f"context_{year}.csv", rows)
    report.append(f"context: {len(by_day)} days of minute bars written ({', '.join(sorted(by_day))})")
    return report


if __name__ == "__main__":
    for line in record(sys.argv[1] if len(sys.argv) > 1 else ".", datetime.now(NY).date(), history=True):
        print(line)
