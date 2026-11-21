"""Real futures-options expected move from the tastytrade API (read-only access).

Needs three GitHub secrets, passed in as environment variables:
TT_CLIENT_ID, TT_CLIENT_SECRET, TT_REFRESH_TOKEN (the refresh token never expires).

For each product the nested futures-option chain gives the expirations; the daily and weekly
expirations are picked like the Cboe ones, the at-the-money call and put are quoted, and the
implied volatility is solved from the straddle (Black-76), so no greeks feed is needed.
"""

import gzip
import json
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.tastyworks.com"
UA = "trading-research-recorder/1.0"
PRODUCTS = ["ES", "NQ", "RTY", "CL", "GC", "6E"]


class TastyTrade:
    def __init__(self):
        body = {"grant_type": "refresh_token",
                "refresh_token": os.environ["TT_REFRESH_TOKEN"].strip(),
                "client_secret": os.environ["TT_CLIENT_SECRET"].strip()}
        if os.environ.get("TT_CLIENT_ID", "").strip():
            body["client_id"] = os.environ["TT_CLIENT_ID"].strip()
        tok = self._call("POST", "/oauth/token", body=body, auth=False)
        self.token = tok["access_token"]

    def _call(self, method, path, params=None, body=None, auth=True):
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {"User-Agent": UA, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:  # keep the server's explanation, never the secrets
            raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {e.read()[:300]!r}") from None

    def chain(self, product):
        return self._call("GET", f"/futures-option-chains/{product}/nested")["data"]

    def quotes(self, futures=(), options=()):
        out = {}
        futures, options = list(futures), list(options)
        while futures or options:  # at most 100 symbols per request
            f, futures = futures[:100], futures[100:]
            o, options = options[:100 - len(f)], options[100 - len(f):]
            params = {}
            if f:
                params["future"] = f
            if o:
                params["future-option"] = o
            for it in self._call("GET", "/market-data/by-type", params)["data"].get("items", []):
                out[it.get("symbol")] = it
        return out


def num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def mid(q):
    if not q:
        return None
    b, a = num(q.get("bid")), num(q.get("ask"))
    if b is not None and a is not None and a > 0:
        return (b + a) / 2
    return num(q.get("mid")) or num(q.get("mark"))


def black76_straddle(f, k, t, vol):
    if vol <= 0 or t <= 0:
        return abs(f - k)
    sd = vol * math.sqrt(t)
    d1 = (math.log(f / k) + sd * sd / 2) / sd
    d2 = d1 - sd
    n = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    call = f * n(d1) - k * n(d2)
    return call + (call - (f - k))  # put from put-call parity (no discounting)


def implied_vol(f, k, t, straddle):
    lo, hi = 1e-4, 5.0
    if t <= 0 or black76_straddle(f, k, t, hi) < straddle:
        return None
    for _ in range(100):
        m = (lo + hi) / 2
        if black76_straddle(f, k, t, m) > straddle:
            hi = m
        else:
            lo = m
    return (lo + hi) / 2


def expirations(data):
    out = []
    for ch in data.get("option-chains", []):
        for e in ch.get("expirations", []):
            out.append(e)
    return out


def exp_date(e):
    return datetime.strptime(e["expiration-date"], "%Y-%m-%d").date()


def years_to(e, now):
    s = e.get("expires-at")
    try:
        end = datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        end = None
    if end is None:
        d = exp_date(e)
        end = datetime(d.year, d.month, d.day, 21, 0, tzinfo=timezone.utc)
    return max((end - now).total_seconds(), 0) / (365 * 24 * 3600)


def front_contracts(data, today):
    """The current (front) futures contract, then the next one, as tastytrade flags them
    ("active-month", "next-active-month"). If the flags are missing, the unexpired contracts
    are ordered by their own expiration date - never alphabetically (/ESH7 sorts before /ESZ6)."""
    futs = [f for f in data.get("futures", []) if f.get("symbol")]
    live = [f for f in futs if not f.get("expiration-date") or f["expiration-date"] >= today.isoformat()]
    by_date = sorted(live, key=lambda f: f.get("expiration-date") or "9999")
    active = [f["symbol"] for f in futs if f.get("active-month")]
    nxt = [f["symbol"] for f in futs if f.get("next-active-month")]
    if active:
        order, how = active + nxt, "active-month flag"
    else:
        order, how = [f["symbol"] for f in by_date], "nearest expiration (no active flag)"
    seen = []
    for sym in order + [f["symbol"] for f in by_date]:
        if sym not in seen:
            seen.append(sym)
    return seen, how


def choose(exps, target):
    """Among this contract's expirations on the target date, prefer PM-settled, then weeklies."""
    same = [e for e in exps if exp_date(e) == target]
    return sorted(same, key=lambda e: (e.get("settlement-type") != "PM",
                                       e.get("expiration-type") != "Weekly",
                                       e.get("option-root-symbol", "")))[0]


def em_for(tt, e, today, now, prefix, weekdays_between):
    und = e["underlying-symbol"]
    fq = tt.quotes(futures=[und]).get(und)
    price = mid(fq)
    row = {f"{prefix}_expiry": e["expiration-date"], f"{prefix}_days": (exp_date(e) - today).days}
    snap = {"expiration": {k: v for k, v in e.items() if k != "strikes"}, "future_quote": fq}
    if not price:
        row["_note"] = f"no quote for {und}"
        return row, None, snap
    strikes = sorted(e.get("strikes", []), key=lambda s: abs(num(s["strike-price"]) - price))[:4]
    syms = [s[c] for s in strikes for c in ("call", "put") if s.get(c)]
    q = tt.quotes(options=syms)
    snap["option_quotes"] = q
    best = None
    for s in strikes:
        c, p = mid(q.get(s.get("call"))), mid(q.get(s.get("put")))
        if c is not None and p is not None:
            best = (num(s["strike-price"]), c + p)
            break
    if not best:
        row["_note"] = "no two-sided ATM quotes"
        return row, (und, fq), snap
    k, straddle = best
    iv = implied_vol(price, k, years_to(e, now), straddle)
    row[f"{prefix}_atm_strike"] = k
    row[f"{prefix}_straddle"] = round(straddle, 6)
    if iv:
        row[f"{prefix}_iv"] = round(iv, 5)
        t = 1 / 252 if prefix == "daily" else row[f"{prefix}_days"] / 365
        row[f"{prefix}_em_iv"] = round(price * iv * math.sqrt(t), 6)
    move, basis = straddle, "straddle"
    if prefix == "daily" and weekdays_between(today, exp_date(e)) > 1 and row.get("daily_em_iv"):
        move, basis = row["daily_em_iv"], "iv-1day"
    row[f"{prefix}_basis"] = basis
    row[f"{prefix}_upper"] = round(price + move, 6)
    row[f"{prefix}_lower"] = round(price - move, 6)
    return row, (und, fq), snap


def record(today, now_utc, raw_dir, pick_expiries, weekdays_between):
    """Returns CSV rows (kind 'futures'); never raises for a single product."""
    tt = TastyTrade()
    rows = []
    for prod in PRODUCTS:
        row = {"symbol": prod, "kind": "futures", "source_time": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")}
        try:
            data = tt.chain(prod)
            all_exps = [e for e in expirations(data) if e.get("strikes") and exp_date(e) > today]
            contracts, how = front_contracts(data, today)
            # use the front contract; move to the next one only if the front has no options left
            front = next((c for c in contracts if any(e.get("underlying-symbol") == c for e in all_exps)), None)
            if front is None:
                raise RuntimeError(f"no option expirations on any listed contract {contracts[:3]}")
            exps = [e for e in all_exps if e.get("underlying-symbol") == front]
            daily_d, weekly_d = pick_expiries([exp_date(e) for e in exps], today)
            snaps, notes = {"futures": data.get("futures", [])}, []
            if contracts and front != contracts[0]:
                notes.append(f"front {contracts[0]} has no options left, using {front}")
            for prefix, d in (("daily", daily_d), ("weekly", weekly_d)):
                if d is None:
                    continue
                part, fut, snaps[prefix] = em_for(tt, choose(exps, d), today, now_utc, prefix, weekdays_between)
                if "_note" in part:
                    notes.append(f"{prefix}: {part.pop('_note')}")
                row.update(part)
                if fut and fut[1] and prefix == "daily":
                    row["price"] = round(mid(fut[1]), 6)
                    row["prev_close"] = num(fut[1].get("prev-close")) or num(fut[1].get("close"))
                    notes.insert(0, f"contract {fut[0]} (front by {how})")
            if row.get("weekly_expiry") and "price" not in row:
                fq = snaps.get("weekly", {}).get("future_quote")
                row["price"] = mid(fq) if fq else None
            row["note"] = "tastytrade futures options; " + "; ".join(notes)
            raw_dir.mkdir(parents=True, exist_ok=True)
            with gzip.open(raw_dir / f"tt_{prod}.json.gz", "wt", encoding="utf-8") as f:
                json.dump(snaps, f, default=str)
        except Exception as e:
            row["note"] = f"FAILED tastytrade: {e}"
        rows.append(row)
    return rows
