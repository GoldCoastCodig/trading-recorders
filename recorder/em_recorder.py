"""Expected-move recorder.

Runs once per trading day (GitHub Actions, 15:30 US Mountain time) and saves:
  * expected-move/em_YYYY.csv   one row per symbol per day (daily and weekly expected move)
  * vol-indices/vol_YYYY.csv    VIX family closing values
  * raw/YYYY/YYYY-MM-DD/SYM.json.gz  near-term option chains as received, so any
                                     other expected-move formula can be recomputed later

Source: Cboe's free delayed option chains (no login). Futures (ES, NQ, RTY, GC, CL, 6E) are
"proxies": the expected move in percent from the matching index/ETF options, applied to the
futures price. When the tastytrade secrets are set, real futures-options rows (kind "futures") are
added for ES, NQ, RTY, CL, GC and 6E (see tastytrade_em.py).

Only the Python standard library is used, so it runs anywhere without installing anything.
"""

import argparse
import csv
import gzip
import json
import math
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MOUNTAIN = ZoneInfo("America/Denver")
UA = "Mozilla/5.0 (personal research recorder)"

# symbol -> (kind, Cboe symbol)
OPTION_SYMBOLS = {
    "AAPL": ("stock", "AAPL"), "SPCX": ("stock", "SPCX"), "TSLA": ("stock", "TSLA"),
    "MSFT": ("stock", "MSFT"), "AMZN": ("stock", "AMZN"), "NFLX": ("stock", "NFLX"),
    "GOOGL": ("stock", "GOOGL"), "META": ("stock", "META"),
    "SPX": ("index", "_SPX"), "NDX": ("index", "_NDX"), "RUT": ("index", "_RUT"),
    "SPY": ("etf", "SPY"), "QQQ": ("etf", "QQQ"), "IWM": ("etf", "IWM"),
    "GLD": ("etf", "GLD"), "USO": ("etf", "USO"), "FXE": ("etf", "FXE"),
}
# futures -> (options symbol used as proxy, Yahoo futures symbol). Micros and E-nanos use the same points.
FUTURES_PROXIES = {
    "ES": ("SPX", "ES=F"), "NQ": ("NDX", "NQ=F"), "RTY": ("RUT", "RTY=F"),
    "GC": ("GLD", "GC=F"), "CL": ("USO", "CL=F"), "6E": ("FXE", "6E=F"),
}
VOL_INDICES = ["VIX", "VIX1D", "VIX9D", "VXN", "RVX", "GVZ", "OVX", "VVIX"]

RAW_KEEP_DAYS = 35  # keep expirations up to this many days out in the raw snapshot
OCC = re.compile(r"^(?P<root>[A-Z]+\d?)(?P<exp>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")

EM_COLUMNS = [
    "date", "capture_time_mt", "source_time", "symbol", "kind", "proxy_of", "price", "prev_close",
    "daily_expiry", "daily_days", "daily_atm_strike", "daily_straddle", "daily_iv", "daily_em_iv",
    "daily_basis", "daily_upper", "daily_lower",
    "weekly_expiry", "weekly_days", "weekly_atm_strike", "weekly_straddle", "weekly_iv", "weekly_em_iv",
    "weekly_basis", "weekly_upper", "weekly_lower",
    "note",
]
VOL_COLUMNS = ["date", "capture_time_mt", "source_time", "index", "value", "prev_close"]


def fetch_json(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # network hiccup or blocked: retry, then give up
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def parse_options(data):
    """Return list of dicts: expiry(date), cp, strike, bid, ask, iv."""
    out = []
    for o in data.get("options", []):
        m = OCC.match(str(o.get("option", "")).replace(" ", ""))
        if not m:
            continue
        exp = datetime.strptime(m["exp"], "%y%m%d").date()
        out.append({
            "root": m["root"], "expiry": exp, "cp": m["cp"], "strike": int(m["strike"]) / 1000,
            "bid": num(o.get("bid")), "ask": num(o.get("ask")), "iv": num(o.get("iv")),
            "raw": o,
        })
    return out


def mid(o):
    if o and o["bid"] is not None and o["ask"] is not None and o["ask"] > 0:
        return (o["bid"] + o["ask"]) / 2
    return None


def straddle_at(opts, expiry, price):
    """At-the-money straddle for one expiry: strike nearest the price with both sides quoted."""
    by_strike = {}
    for o in opts:
        if o["expiry"] == expiry:
            by_strike.setdefault(o["strike"], {})[o["cp"]] = o
    best = None
    for k, pair in by_strike.items():
        c, p = pair.get("C"), pair.get("P")
        if mid(c) is None or mid(p) is None:
            continue
        if best is None or abs(k - price) < abs(best[0] - price):
            best = (k, c, p)
    if best is None:
        return None
    k, c, p = best
    ivs = [x["iv"] for x in (c, p) if x["iv"]]
    iv = sum(ivs) / len(ivs) if ivs else None
    if iv is not None and iv > 5:  # some feeds give percent (25.3) instead of decimal (0.253)
        iv /= 100
    return {"strike": k, "straddle": mid(c) + mid(p), "iv": iv}


def pick_expiries(expiries, today):
    """Daily = first expiry after today. Weekly = last expiry on or before the coming Friday
    (next week's Friday when today is Friday), so holiday Thursdays are handled."""
    future = sorted(e for e in set(expiries) if e > today)
    if not future:
        return None, None
    days_to_fri = (4 - today.weekday()) % 7 or 7
    friday = today + timedelta(days=days_to_fri)
    weekly = [e for e in future if e <= friday]
    return future[0], (weekly[-1] if weekly else None)


def em_block(opts, expiry, price, today, prefix):
    row = {}
    if expiry is None:
        return row
    s = straddle_at(opts, expiry, price)
    days = (expiry - today).days
    row[f"{prefix}_expiry"] = expiry.isoformat()
    row[f"{prefix}_days"] = days
    if not s:
        return row
    row[f"{prefix}_atm_strike"] = s["strike"]
    row[f"{prefix}_straddle"] = round(s["straddle"], 4)
    if s["iv"]:
        row[f"{prefix}_iv"] = round(s["iv"], 5)
        # one standard deviation; daily uses one trading day, weekly uses calendar days
        t = 1 / 252 if prefix == "daily" else days / 365
        row[f"{prefix}_em_iv"] = round(price * s["iv"] * math.sqrt(t), 4)
    # levels: the straddle, except a "daily" expiry more than one trading day away
    # (stocks with only Friday options) where the one-day IV move is used instead
    move, basis = s["straddle"], "straddle"
    if prefix == "daily" and weekdays_between(today, expiry) > 1 and row.get("daily_em_iv"):
        move, basis = row["daily_em_iv"], "iv-1day"
    row[f"{prefix}_basis"] = basis
    row[f"{prefix}_upper"] = round(price + move, 4)
    row[f"{prefix}_lower"] = round(price - move, 4)
    return row


def weekdays_between(a, b):
    return sum(1 for i in range(1, (b - a).days + 1) if (a + timedelta(days=i)).weekday() < 5)


def record_symbol(sym, kind, cboe, today, raw_dir):
    payload = fetch_json(f"https://cdn.cboe.com/api/global/delayed_quotes/options/{cboe}.json")
    data = payload.get("data", payload)
    opts = parse_options(data)
    price = num(data.get("current_price")) or num(data.get("close"))
    row = {"symbol": sym, "kind": kind, "source_time": payload.get("timestamp", ""),
           "price": price, "prev_close": num(data.get("prev_day_close"))}

    # raw snapshot: everything except far-out expirations
    keep = [o["raw"] for o in opts if (o["expiry"] - today).days <= RAW_KEEP_DAYS]
    snap = {k: v for k, v in data.items() if k != "options"}
    snap["options"] = keep
    snap["_source_timestamp"] = payload.get("timestamp")
    raw_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(raw_dir / f"{sym}.json.gz", "wt", encoding="utf-8") as f:
        json.dump(snap, f)

    if not price or not opts:
        row["note"] = f"no price or no options parsed (keys: {sorted(data)[:15]})"
        return row, None
    if kind == "index":  # SPX has AM-settled SPX and PM-settled SPXW; use the weeklies root
        roots = {o["root"] for o in opts}
        pm = {"SPX": "SPXW", "NDX": "NDXP", "RUT": "RUTW"}.get(sym)
        if pm in roots:
            opts = [o for o in opts if o["root"] == pm]
    daily, weekly = pick_expiries([o["expiry"] for o in opts], today)
    row.update(em_block(opts, daily, price, today, "daily"))
    row.update(em_block(opts, weekly, price, today, "weekly"))
    return row, price


def futures_price(yahoo):
    j = fetch_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo}?range=1d&interval=1d")
    meta = j["chart"]["result"][0]["meta"]
    return num(meta.get("regularMarketPrice")), num(meta.get("chartPreviousClose"))


def proxy_row(fut, base_sym, base, today):
    """Futures expected move = proxy's move in percent x futures price."""
    row = {"symbol": fut, "kind": "futures-proxy", "proxy_of": base_sym,
           "source_time": base.get("source_time", "")}
    try:
        price, prev = futures_price(FUTURES_PROXIES[fut][1])
    except Exception as e:
        row["note"] = f"futures price unavailable: {e}"
        return row
    row["price"], row["prev_close"] = price, prev
    bp = base.get("price")
    if not price or not bp:
        row["note"] = "no price"
        return row
    ratio = price / bp
    for p in ("daily", "weekly"):
        for k in ("expiry", "days", "iv"):
            if f"{p}_{k}" in base:
                row[f"{p}_{k}"] = base[f"{p}_{k}"]
        for k in ("straddle", "em_iv"):
            if base.get(f"{p}_{k}") is not None:
                row[f"{p}_{k}"] = round(base[f"{p}_{k}"] * ratio, 4)
        basis = base.get(f"{p}_basis")
        move = row.get(f"{p}_em_iv") if basis == "iv-1day" else row.get(f"{p}_straddle")
        if move is not None:
            row[f"{p}_basis"] = basis
            row[f"{p}_upper"] = round(price + move, 4)
            row[f"{p}_lower"] = round(price - move, 4)
    row["note"] = "proxy: futures price from Yahoo (delayed); move scaled from " + base_sym
    return row


def append_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def already_recorded(path, day):
    if not path.exists():
        return False
    with open(path, newline="", encoding="utf-8") as f:
        return any(r.get("date") == day for r in csv.DictReader(f))


def record_context(out, today, history=False):
    """Market context (market_context.py, Yahoo) and, when its secrets are set, breadth from Schwab."""
    lines = []
    try:
        import market_context
        lines += market_context.record(out, today, history)
    except Exception as e:
        lines.append(f"market context FAILED: {e}")
    if os.environ.get("SCHWAB_REFRESH_TOKEN"):
        try:
            import schwab_context
            lines += schwab_context.record(out)
        except Exception as e:
            lines.append(f"schwab FAILED: {e}")
            topic = os.environ.get("NTFY_TOPIC", "").strip()
            if topic and "expired" in str(e):   # the 7-day Schwab login ran out: tell the phone
                try:
                    urllib.request.urlopen(urllib.request.Request(
                        "https://ntfy.sh/" + topic, method="POST", data=str(e).encode("utf-8"),
                        headers={"Title": "Schwab login expired"}), timeout=30)
                except Exception:
                    pass
    else:
        lines.append("Schwab secrets not set; no breadth data (docs/schwab-api-setup.md)")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="data folder (the market-data branch checkout)")
    ap.add_argument("--test", action="store_true", help="write under test/ and ignore time checks")
    ap.add_argument("--force", action="store_true", help="record even outside 15:25-23:59 MT")
    ap.add_argument("--history", action="store_true", help="only refresh vol-history/ (Cboe index history)")
    ap.add_argument("--context", action="store_true", help="only record the market context (indexes, bonds, "
                    "financials, breadth); with --test under test/")
    a = ap.parse_args()

    if a.context:
        out = Path(a.out) / ("test" if a.test else "")
        for line in record_context(out, datetime.now(MOUNTAIN).date(), history=True):
            print(line)
        return 0

    if a.history:
        import vol_history
        for line in vol_history.refresh(Path(a.out)):
            print(line)
        return 0

    now = datetime.now(MOUNTAIN)
    today = now.date()
    out = Path(a.out) / ("test" if a.test else "")
    em_file = out / "expected-move" / f"em_{today.year}.csv"

    if not (a.test or a.force):
        if today.weekday() >= 5 or (now.hour, now.minute) < (15, 25):
            print(f"Outside recording window ({now:%a %H:%M} MT); nothing to do.")
            return 0
        if already_recorded(em_file, today.isoformat()):
            print(f"{today} already recorded; nothing to do.")
            return 0

    stamp = {"date": today.isoformat(), "capture_time_mt": now.strftime("%Y-%m-%d %H:%M")}
    raw_dir = out / "raw" / str(today.year) / today.isoformat()
    rows, by_sym, failures = [], {}, []
    for sym, (kind, cboe) in OPTION_SYMBOLS.items():
        try:
            row, _ = record_symbol(sym, kind, cboe, today, raw_dir)
            by_sym[sym] = row
            rows.append({**stamp, **row})
            print(f"{sym:6} price={row.get('price')} daily={row.get('daily_straddle')} "
                  f"weekly={row.get('weekly_straddle')} {row.get('note', '')}")
        except Exception as e:
            failures.append(sym)
            rows.append({**stamp, "symbol": sym, "kind": kind, "note": f"FAILED: {e}"})
            print(f"{sym:6} FAILED: {e}", file=sys.stderr)

    for fut, (base_sym, _) in FUTURES_PROXIES.items():
        base = by_sym.get(base_sym)
        if base and base.get("price"):
            row = proxy_row(fut, base_sym, base, today)
        else:
            row = {"symbol": fut, "kind": "futures-proxy", "proxy_of": base_sym, "note": "proxy source failed"}
        rows.append({**stamp, **row})
        print(f"{fut:6} price={row.get('price')} daily={row.get('daily_straddle')} "
              f"weekly={row.get('weekly_straddle')} {row.get('note', '')}")

    if os.environ.get("TT_REFRESH_TOKEN"):
        try:
            import tastytrade_em
            from datetime import timezone
            tt_rows = tastytrade_em.record(today, datetime.now(timezone.utc), raw_dir,
                                           pick_expiries, weekdays_between)
        except Exception as e:  # login or network problem: keep everything else
            tt_rows = [{"symbol": "tastytrade", "kind": "futures", "note": f"FAILED: {e}"}]
        for row in tt_rows:
            rows.append({**stamp, **row})
            print(f"{row['symbol']:6} TT price={row.get('price')} daily={row.get('daily_straddle')} "
                  f"weekly={row.get('weekly_straddle')} {row.get('note', '')}")
    else:
        print("tastytrade secrets not set; futures use proxies only")

    vols = []
    for ix in VOL_INDICES:
        try:
            j = fetch_json(f"https://cdn.cboe.com/api/global/delayed_quotes/quotes/_{ix}.json")
            d = j.get("data", j)
            vols.append({**stamp, "source_time": j.get("timestamp", ""), "index": ix,
                         "value": num(d.get("current_price")) or num(d.get("close")),
                         "prev_close": num(d.get("prev_day_close"))})
        except Exception as e:
            print(f"{ix} FAILED: {e}", file=sys.stderr)

    if now.hour >= 16:  # CME reopened at 16:00 MT: futures quotes are from the evening session
        for r in rows:
            r["note"] = "LATE capture after 16:00 MT; " + (r.get("note") or "")
    append_csv(em_file, EM_COLUMNS, rows)
    append_csv(out / "vol-indices" / f"vol_{today.year}.csv", VOL_COLUMNS, vols)
    ok = len(OPTION_SYMBOLS) - len(failures)
    print(f"Recorded {ok}/{len(OPTION_SYMBOLS)} option symbols, {len(vols)} vol indices -> {em_file}")
    try:  # LATEST.md + latest.csv + phone notification (never fails the recording)
        import latest
        print(latest.publish(out, os.environ.get("NTFY_TOPIC", "").strip(), a.test))
    except Exception as e:
        print(f"LATEST.md FAILED: {e}", file=sys.stderr)
    for line in record_context(out, today):   # indexes, bonds, financials, breadth (never fails the run)
        print(line)
    if not a.test:  # economic calendar: pick up today's actual results (never fails the run)
        try:
            import econ_calendar
            print("Calendar: " + "; ".join(econ_calendar.refresh(Path(a.out), today)[1]))
        except Exception as e:
            print(f"Calendar FAILED: {e}", file=sys.stderr)
    if today.weekday() == 4 and not a.test:  # Fridays: refresh the volatility-index history
        try:
            import vol_history
            for line in vol_history.refresh(Path(a.out)):
                print(line)
        except Exception as e:
            print(f"vol history FAILED: {e}", file=sys.stderr)
    return 0 if ok else 1  # fail the run (and email you) only if nothing at all worked


if __name__ == "__main__":
    sys.exit(main())
