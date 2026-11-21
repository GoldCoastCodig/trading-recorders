"""XSP same-day options, every 5 minutes from 09:30 to 12:00 New York (personal data recording).

XSP = Cboe Mini-SPX index options (1/10 of the S&P 500 index, cash settled, daily expiries). Option history is not
free, so this records it from now on: the same-day expiry, calls and puts within STRIKES strikes of the index, from
tastytrade's read-only API (secrets TT_*).

Each row: time, the index (XSP), expiry, strike, call/put, bid, ask, mid, last, volume and open interest as
tastytrade gives them, plus the implied volatility and delta we solve from the mid (Black-Scholes, rate 0, time to
the 16:00 New York close), for later study.

Output (market-data branch): xsp/YYYY/xsp_YYYY-MM-DD.csv (test: test/xsp/...), pushed every 30 minutes and at the
end, so a stopped run keeps what it had; a restarted run adds to the day's file and skips the times already there.

    python3 recorder/xsp_recorder.py --out data [--test]
"""
import argparse
import csv
import datetime as dt
import math
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from tastytrade_em import TastyTrade, num  # noqa: E402

NY = ZoneInfo("America/New_York")
START, END, STEP = dt.time(9, 30), dt.time(12, 0), 5          # New York time, minutes
STRIKES = 15                                                  # strikes each side of the index (1-point strikes)
COLS = ["time_utc", "time_ny", "underlying", "expiry", "strike", "type", "symbol", "bid", "ask", "mid", "last",
        "volume", "open_interest", "iv", "delta"]


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(s, k, t, vol, call):
    if t <= 0 or vol <= 0:
        return max(0.0, s - k) if call else max(0.0, k - s)
    d1 = (math.log(s / k) + vol * vol * t / 2) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    return s * ncdf(d1) - k * ncdf(d2) if call else k * ncdf(-d2) - s * ncdf(-d1)


def iv_delta(s, k, t, price, call):
    """Implied volatility (bisection) and delta from the option's mid; None where the price has no time value."""
    if not (s and price and t > 0) or price <= bs_price(s, k, t, 1e-6, call) + 1e-6:
        return None, None
    lo, hi = 1e-4, 5.0
    if bs_price(s, k, t, hi, call) < price:
        return None, None
    for _ in range(80):
        m = (lo + hi) / 2
        lo, hi = (m, hi) if bs_price(s, k, t, m, call) < price else (lo, m)
    vol = (lo + hi) / 2
    d1 = (math.log(s / k) + vol * vol * t / 2) / (vol * math.sqrt(t))
    return round(vol, 4), round(ncdf(d1) if call else ncdf(d1) - 1, 4)


def same_day_chain(tt, today):
    """{strike: (call symbol, put symbol)} of today's expiry (else the nearest one) and that expiry's date."""
    items = tt._call("GET", "/option-chains/XSP/nested")["data"]["items"]
    exps = [e for it in items for e in it.get("expirations", [])]
    exps = [e for e in exps if e.get("expiration-date", "") >= today.isoformat()]
    if not exps:
        raise RuntimeError("no XSP expirations from tastytrade")
    exp = min(exps, key=lambda e: (e["expiration-date"] != today.isoformat(), e["expiration-date"]))
    strikes = {float(s["strike-price"]): (s.get("call"), s.get("put")) for s in exp.get("strikes", [])}
    return exp["expiration-date"], strikes


def index_price(tt):
    """XSP index level; tastytrade's index quote of XSP, else SPX / 10."""
    for sym, scale in (("XSP", 1.0), ("SPX", 0.1)):
        try:
            items = tt._call("GET", "/market-data/by-type", {"index": [sym]})["data"].get("items", [])
        except RuntimeError:
            continue
        for it in items:
            p = num(it.get("last")) or num(it.get("mark")) or num(it.get("mid"))
            if p:
                return p * scale
    return None


def option_quotes(tt, symbols):
    out = {}
    for i in range(0, len(symbols), 100):
        part = symbols[i:i + 100]
        for it in tt._call("GET", "/market-data/by-type", {"equity-option": part})["data"].get("items", []):
            out[it.get("symbol")] = it
    return out


def snapshot(tt, now, expiry, strikes):
    s = index_price(tt)
    if not s:
        raise RuntimeError("no XSP / SPX index price from tastytrade")
    near = sorted(strikes, key=lambda k: abs(k - s))[:2 * STRIKES + 1]
    syms = {}
    for k in sorted(near):
        c, p = strikes[k]
        if c:
            syms[c] = (k, "C")
        if p:
            syms[p] = (k, "P")
    q = option_quotes(tt, list(syms))
    close = dt.datetime.combine(dt.date.fromisoformat(expiry), dt.time(16, 0), NY)
    t = max((close - now).total_seconds(), 0) / (365 * 86400)
    rows = []
    for sym, (k, typ) in syms.items():
        it = q.get(sym, {})
        bid, ask = num(it.get("bid")), num(it.get("ask"))
        mid = round((bid + ask) / 2, 4) if bid is not None and ask is not None and ask > 0 else num(it.get("mark"))
        iv, delta = iv_delta(s, k, t, mid, typ == "C")
        rows.append([now.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%H:%M"),
                     round(s, 3), expiry, k, typ, sym.strip(), bid, ask, mid, num(it.get("last")),
                     num(it.get("volume")), num(it.get("open-interest")), iv, delta])
    return rows


def push(out, msg):
    """Commit and push the data folder (a clone of the market-data branch); a failed push is retried next time."""
    git = ["git", "-C", str(out)]
    subprocess.run(git + ["add", "-A"], check=True)
    if subprocess.run(git + ["diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(git + ["commit", "-q", "-m", msg], check=True)
    for _ in range(3):
        if subprocess.run(git + ["push", "-q", "origin", "HEAD:market-data"]).returncode == 0:
            return
        time.sleep(5)
        subprocess.run(git + ["pull", "-q", "--rebase", "origin", "market-data"])
    print("push failed; will try again at the next save")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="clone of the market-data branch")
    ap.add_argument("--test", action="store_true", help="3 snapshots one minute apart now, under test/xsp")
    a = ap.parse_args()
    now = dt.datetime.now(NY)
    today = now.date()
    if not a.test and today.weekday() >= 5:
        print("Weekend: nothing to record.")
        return
    if not a.test and now.time() > END:
        print("After 12:00 New York: today's window is over.")
        return
    tt, born = TastyTrade(), time.time()
    expiry, strikes = same_day_chain(tt, today)
    if not a.test and expiry != today.isoformat():
        print("No XSP expiry today (%s is the next): market holiday, nothing to record." % expiry)
        return
    out = Path(a.out)
    path = out / ("test/xsp" if a.test else "xsp") / str(today.year) / ("xsp_%s.csv" % today.isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if path.exists():
        with open(path, newline="") as f:
            done = {r["time_ny"] for r in csv.DictReader(f)}
    if a.test:
        times = [now + dt.timedelta(minutes=i) for i in range(3)]
    else:
        times, t = [], dt.datetime.combine(today, START, NY)
        while t.time() <= END:
            if t >= now - dt.timedelta(minutes=1):
                times.append(t)
            t += dt.timedelta(minutes=STEP)
    print("XSP expiry %s, %d strikes listed; %d snapshots to take" % (expiry, len(strikes), len(times)))
    last_push = time.time()
    for when in times:
        wait = (when - dt.datetime.now(NY)).total_seconds()
        if wait > 0:
            time.sleep(wait)
        stamp = dt.datetime.now(NY)
        if stamp.strftime("%H:%M") in done:
            continue
        try:
            rows = snapshot(tt, stamp, expiry, strikes)
        except Exception as e:  # noqa: BLE001  (one bad snapshot must not end the morning)
            print("%s snapshot failed: %s" % (stamp.strftime("%H:%M"), e))
            try:
                tt, born = TastyTrade(), time.time()
            except Exception as e2:  # noqa: BLE001
                print("login failed: %s" % e2)
            continue
        new = not path.exists()
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(COLS)
            w.writerows(rows)
        print("%s  XSP %.2f  %d options" % (stamp.strftime("%H:%M"), rows[0][2], len(rows)))
        if time.time() - last_push > 1800:
            push(out, "XSP options %s (%s)" % (today, stamp.strftime("%H:%M")))
            last_push = time.time()
        if time.time() - born > 600:          # the access token lasts 15 minutes: log in again in time
            tt, born = TastyTrade(), time.time()
    push(out, "XSP options %s%s" % (today, " (test)" if a.test else ""))


if __name__ == "__main__":
    main()
