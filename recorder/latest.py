"""Today's levels, for people: LATEST.md + latest.csv on the market-data branch, and a phone notification.

Called by em_recorder.py after each recording. Picks, per symbol, the best row of the newest capture
(tastytrade futures first, then the index/ETF proxy), and the official weekly levels from the last capture
that set them (normally Friday's: weekly_days >= 6).

Phone notification: ntfy (free app, https://ntfy.sh). The topic name comes from NTFY_TOPIC; nothing is sent
when it is empty.

Paste line (one line, every symbol), the format the chart indicators read:
    EM|2026-10-07|ES:7726,7676,7776,7688.25,7864.25;NQ:...
    = symbol:price,daily_low,daily_high,weekly_low,weekly_high (blank when unknown)
"""

import csv
import json
import urllib.request
from pathlib import Path

FUTURES = ["ES", "NQ", "RTY", "CL", "GC", "6E"]
# Micros and E-nanos (no options of their own here) use the full-size contract's levels in points.
SMALLER = {"ES": "MES, NES", "NQ": "MNQ, NNQ", "RTY": "M2K, N2K", "CL": "MCL", "GC": "MGC", "6E": "M6E"}
OTHERS = ["SPX", "NDX", "RUT", "SPY", "QQQ", "IWM", "AAPL", "SPCX", "TSLA", "MSFT", "AMZN", "NFLX",
          "GOOGL", "META", "GLD", "USO", "FXE"]
REPO_URL = "https://github.com/GoldCoastCodig/Trading-Research/blob/market-data/"


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_rows(em_dir):
    rows = []
    for p in sorted(Path(em_dir).glob("em_*.csv")):
        with open(p, newline="", encoding="utf-8") as fh:
            rows += list(csv.DictReader(fh))
    return rows


def usable(r):
    return f(r.get("price")) is not None and "FAILED" not in (r.get("note") or "") and (
        f(r.get("daily_upper")) is not None or f(r.get("weekly_upper")) is not None)


def rank(r):
    """Lower is better: real futures options, then Cboe options, then proxies."""
    return {"futures": 0, "stock": 0, "index": 0, "etf": 0, "futures-proxy": 1}.get(r.get("kind"), 2)


def contract(r):
    note = r.get("note") or ""
    if "contract /" in note:
        return note.split("contract ", 1)[1].split(" ")[0]
    return ""


def pick(rows, day):
    """{symbol: best row} for one capture date."""
    best = {}
    for r in rows:
        if r.get("date") != day or not usable(r):
            continue
        s = r["symbol"]
        if s not in best or rank(r) < rank(best[s]):
            best[s] = r
    return best


def weekly_anchor(rows, symbol, day):
    """The capture that set this week's levels (weekly_days >= 6, i.e. taken on the last day of the week before)."""
    cands = [r for r in rows if r.get("symbol") == symbol and usable(r) and r.get("date", "") <= day
             and (f(r.get("weekly_days")) or 0) >= 6 and (r.get("weekly_expiry") or "") > day]
    if not cands:
        return None
    newest = max(r["date"] for r in cands)
    return min((r for r in cands if r["date"] == newest), key=rank)


def summary(rows, anchor_rows=None):
    """Everything LATEST.md, latest.csv, the paste line and the notification need.
    anchor_rows: where to look for the week's levels (test runs use the real history)."""
    anchor_rows = anchor_rows if anchor_rows is not None else rows
    days = sorted({r["date"] for r in rows if usable(r)})
    if not days:
        return None
    day = days[-1]
    best = pick(rows, day)
    out = []
    for s in FUTURES + OTHERS:
        r = best.get(s)
        if not r:
            continue
        p = f(r["price"])
        w = weekly_anchor(anchor_rows, s, day)
        out.append({
            "symbol": s, "contract": contract(r), "source": {"futures": "tastytrade", "futures-proxy": "proxy"}.get(
                r["kind"], "cboe"), "price": p,
            "daily_move": (f(r.get("daily_upper")) - p) if f(r.get("daily_upper")) is not None else None,
            "daily_low": f(r.get("daily_lower")), "daily_high": f(r.get("daily_upper")),
            "daily_expiry": r.get("daily_expiry", ""),
            "weekly_set": w["date"] if w else "", "weekly_price": f(w["price"]) if w else None,
            "weekly_move": (f(w["weekly_upper"]) - f(w["price"])) if w and f(w.get("weekly_upper")) is not None else None,
            "weekly_low": f(w.get("weekly_lower")) if w else None, "weekly_high": f(w.get("weekly_upper")) if w else None,
            "weekly_expiry": w.get("weekly_expiry", "") if w else "",
            "late": "LATE" in (r.get("note") or ""), "capture": r.get("capture_time_mt", ""),
        })
    return {"day": day, "items": out}


def fmt(v, sym):
    if v is None:
        return ""
    return "%.5f" % v if sym == "6E" else "%.2f" % v


def paste_line(s):
    parts = []
    for it in s["items"]:
        sym = it["symbol"]
        vals = [it["price"], it["daily_low"], it["daily_high"], it["weekly_low"], it["weekly_high"]]
        parts.append(sym + ":" + ",".join(fmt(v, sym) for v in vals))
    return "EM|%s|%s" % (s["day"], ";".join(parts))


def markdown(s, test=False):
    cap = s["items"][0]["capture"] if s["items"] else ""
    late = any(i["late"] for i in s["items"])
    lines = ["# %sExpected move — levels for the session after %s" % ("TEST — " if test else "", s["day"]), "",
             "Captured %s Mountain time%s. Daily = to the next expiration; weekly = set on %s, valid to its expiry."
             % (cap, " (**LATE**: futures from the evening session)" if late else "",
                next((i["weekly_set"] for i in s["items"] if i["weekly_set"]), "—")), "",
             "Micros and nanos use the full-size levels: " + "; ".join("%s = %s" % (m, f_) for f_, m in SMALLER.items()) + ".", "",
             "| Symbol | Contract | Price | Daily ± | Daily low – high | Weekly ± | Weekly low – high | Source |",
             "|---|---|---|---|---|---|---|---|"]
    for it in s["items"]:
        sym = it["symbol"]
        lines.append("| **%s** | %s | %s | %s | %s – %s | %s | %s – %s | %s |" % (
            sym, it["contract"], fmt(it["price"], sym), fmt(it["daily_move"], sym), fmt(it["daily_low"], sym),
            fmt(it["daily_high"], sym), fmt(it["weekly_move"], sym), fmt(it["weekly_low"], sym),
            fmt(it["weekly_high"], sym), it["source"]))
    lines += ["", "## Paste line for chart indicators", "", "```", paste_line(s), "```", "",
              "Full history: `expected-move/em_YYYY.csv`. How the numbers are made: recorder/README.md on main.", ""]
    return "\n".join(lines)


def write_files(out_dir, s, test=False):
    out_dir = Path(out_dir)
    (out_dir / "LATEST.md").write_text(markdown(s, test), encoding="utf-8")
    cols = ["symbol", "contract", "source", "price", "daily_move", "daily_low", "daily_high", "daily_expiry",
            "weekly_set", "weekly_price", "weekly_move", "weekly_low", "weekly_high", "weekly_expiry", "late", "capture"]
    with open(out_dir / "latest.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for it in s["items"]:
            w.writerow({k: round(v, 6) if isinstance(v, float) else v for k, v in it.items()})


def notification_text(s):
    lines = []
    for it in s["items"]:
        sym = it["symbol"]
        if sym not in FUTURES and sym not in ("SPX", "NDX", "AAPL", "SPCX", "TSLA", "MSFT", "AMZN", "NFLX", "GOOGL", "META"):
            continue
        d = "±%s  %s–%s" % (fmt(it["daily_move"], sym), fmt(it["daily_low"], sym), fmt(it["daily_high"], sym))
        w = ("  W %s–%s" % (fmt(it["weekly_low"], sym), fmt(it["weekly_high"], sym))) if it["weekly_high"] else ""
        lines.append("%s %s%s" % (sym, d, w))
    return "\n".join(lines)


def notify(topic, s, test=False, path="LATEST.md"):
    if not topic:
        return "no NTFY_TOPIC; notification skipped"
    late = any(i["late"] for i in s["items"])
    title = "%sExpected move for session after %s%s" % ("TEST: " if test else "", s["day"], " (LATE)" if late else "")
    req = urllib.request.Request(
        "https://ntfy.sh/" + topic, data=notification_text(s).encode("utf-8"), method="POST",
        headers={"Title": title.encode("utf-8"), "Tags": "chart_with_upwards_trend",
                 "Click": REPO_URL + path, "User-Agent": "trading-research-recorder/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return "notification sent (%s)" % json.loads(r.read().decode()).get("id", r.status)


def publish(out_dir, topic, test=False):
    """Build LATEST.md/latest.csv in out_dir and send the notification; returns a status line."""
    rows = load_rows(Path(out_dir) / "expected-move")
    real = load_rows(Path(out_dir).parent / "expected-move") if test else []
    s = summary(rows, rows + real)
    if not s:
        return "nothing recorded yet; LATEST.md not written"
    write_files(out_dir, s, test)
    try:
        sent = notify(topic, s, test, "test/LATEST.md" if test else "LATEST.md")
    except Exception as e:  # never fail the recording because of the phone
        sent = "notification FAILED: %s" % e
    return "LATEST.md written for %s; %s" % (s["day"], sent)
