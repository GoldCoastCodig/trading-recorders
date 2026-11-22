"""Market breadth and cash indexes from the Schwab API (the thinkorswim data), when its secrets are set.

Adds what Yahoo doesn't have: NYSE / Nasdaq / S&P 500 advance-decline, TICK, TRIN and up/down volume, minute by
minute, plus Schwab's own 1-minute bars of the cash indexes and Treasury yield indexes. Writes next to
market_context.py's files, with source "schwab":
  * context/schwab-minute/YYYY/YYYY-MM-DD.csv.gz   symbol,ts_utc,open,high,low,close,volume (symbol without the $,
                                                   / as _: TICK_Q)
  * context/daily/context_YYYY.csv                 the same daily summary rows as market_context.py
Schwab keeps about 10 days of 1-minute bars, so a missed day is filled by the next run. The advance-decline
symbols, $VOLD and the Nasdaq /Q ones came back only up to the previous day on a midday test (2026-10-12);
the next day's run fills in any day they were missing.

Secrets (GitHub > Settings > Secrets and variables > Actions): SCHWAB_APP_KEY, SCHWAB_APP_SECRET and
SCHWAB_REFRESH_TOKEN. Schwab's refresh token runs out after 7 days: renew it weekly with schwab_login.py
(docs/schwab-api-setup.md). Symbols Schwab doesn't serve are listed as FAILED in the run log and skipped.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import market_context as mc

TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
HISTORY_URL = "https://api.schwabapi.com/marketdata/v1/pricehistory"

# thinkorswim symbols (user 2026-10-12: breadth at and just after the open, SPX, Nasdaq, bonds)
SYMBOLS = [
    # breadth: advance-decline (NYSE, Nasdaq, S&P 500), TICK, TRIN, up minus down volume, advancers/decliners
    "$ADD", "$ADQD", "$ADSPD", "$TICK", "$TRIN", "$VOLD",
    # Nasdaq TICK/TRIN (thinkorswim names; $TICKQ, $TRINQ, $VOLDQ and $VOLD/Q return nothing, checked 2026-10-12)
    "$TICK/Q", "$TRIN/Q",
    "$UVOL", "$DVOL", "$ADVN", "$DECN",
    # cash indexes and Treasury yield indexes
    "$SPX", "$NDX", "$COMPX", "$DJI", "$RUT", "$VIX", "$IRX", "$FVX", "$TNX", "$TYX",
]


class LoginExpired(RuntimeError):
    pass


def access_token():
    key, secret = os.environ["SCHWAB_APP_KEY"], os.environ["SCHWAB_APP_SECRET"]
    body = urllib.parse.urlencode({"grant_type": "refresh_token",
                                   "refresh_token": os.environ["SCHWAB_REFRESH_TOKEN"]}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Authorization": "Basic " + base64.b64encode(f"{key}:{secret}".encode()).decode(),
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())["access_token"]
    except urllib.error.HTTPError as e:
        if e.code in (400, 401):
            raise LoginExpired("Schwab login expired or invalid: run recorder/schwab_login.py and update "
                               "the SCHWAB_REFRESH_TOKEN secret (docs/schwab-api-setup.md)")
        raise


def minutes(token, symbol):
    q = urllib.parse.urlencode({"symbol": symbol, "periodType": "day", "period": 10, "frequencyType": "minute",
                                "frequency": 1, "needExtendedHoursData": "true",
                                # without endDate Schwab stops at the previous business day: ask up to now
                                "endDate": int(time.time() * 1000)})
    last = None
    for i in range(3):
        try:
            req = urllib.request.Request(HISTORY_URL + "?" + q, headers={"Authorization": "Bearer " + token,
                                                                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode())
            return [(c["datetime"] // 1000, c.get("open"), c.get("high"), c.get("low"), c.get("close"),
                     int(c.get("volume") or 0)) for c in data.get("candles", []) if c.get("close") is not None]
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                raise RuntimeError(f"not served by Schwab (HTTP {e.code})")
            last = e
        except Exception as e:
            last = e
        time.sleep(2 * (i + 1))
    raise RuntimeError(str(last))


def record(out_dir):
    out = Path(out_dir) / "context"
    token = access_token()
    report, by_day = [], {}
    for symbol in SYMBOLS:
        name = symbol.lstrip("$").replace("/", "_")   # $TICK/Q -> TICK_Q
        try:
            rows = minutes(token, symbol)
        except Exception as e:
            report.append(f"schwab {symbol}: FAILED {e}")
            continue
        report.append(f"schwab {symbol}: {len(rows)} minute bars")
        for t, o, h, l, c, v in rows:
            ny = datetime.fromtimestamp(t, mc.NY)
            by_day.setdefault(ny.date().isoformat(), {}).setdefault(name, []).append((ny, o, h, l, c, v))
    summaries = {}
    for day, syms in sorted(by_day.items()):
        path = out / "schwab-minute" / day[:4] / f"{day}.csv.gz"
        merged = mc.read_minutes(path)
        for name, bars in syms.items():
            for ny, o, h, l, c, v in bars:
                ts = ny.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                merged[(name, ts)] = {"symbol": name, "ts_utc": ts, "open": mc.r6(o), "high": mc.r6(h),
                                      "low": mc.r6(l), "close": mc.r6(c), "volume": v}
            row = mc.day_summary(day, name, sorted(bars))
            row["source"] = "schwab"
            summaries.setdefault(day[:4], []).append(row)
        mc.write_minutes(path, merged)
    for year, rows in summaries.items():
        mc.write_daily(out / "daily" / f"context_{year}.csv", rows)
    report.append(f"schwab: {len(by_day)} days of minute bars written")
    return report


QUOTES_URL = "https://api.schwabapi.com/marketdata/v1/quotes"


def index_values(names, token=None):
    """{name: (value, prev_close)} for volatility indexes (VIX, VXN, GVZ, ...) as Schwab quotes them ($VIX ...).
    Second backup for the expected-move recorder while Cboe refuses; names Schwab does not know are skipped."""
    token = token or access_token()
    q = urllib.parse.urlencode({"symbols": ",".join("$" + n for n in names), "fields": "quote"})
    req = urllib.request.Request(QUOTES_URL + "?" + q, headers={"Authorization": "Bearer " + token,
                                                                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode())
    out = {}
    for n in names:
        quote = (data.get("$" + n) or {}).get("quote") or {}
        val = quote.get("lastPrice") or quote.get("closePrice")
        if val:
            out[n] = (float(val), float(quote["closePrice"]) if quote.get("closePrice") else None)
    return out
