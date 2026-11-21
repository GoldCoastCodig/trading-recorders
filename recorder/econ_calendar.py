"""US economic calendar: schedule, phone reminders and history (for ES/MES/NQ/MNQ trading).

Sources (free, no login), tried in this order:
  1. TradingView's economic calendar (the same data the user's old subscription widget showed): time,
     importance (3 levels), actual, forecast, previous; 2 weeks back and ahead.
  2. Forex Factory's weekly feed (this week + next week), if TradingView fails: no actuals.
  3. U.S. Treasury (api.fiscaldata.treasury.gov): coupon auctions (2..30-year notes/bonds, TIPS); results at
     13:00 New York = 11:00 Mountain. Added when the calendar above does not list that auction.

Writes (market-data branch):  calendar/events_YYYY.csv  every event seen, updated when actuals arrive
                               CALENDAR.md               last week + this week + next week, Mountain time
Phone (ntfy, same topic as the expected move):
  - morning (mode calendar, ~04:45 MT Mon-Fri): today's events; Sunday: the week ahead
  - 30 and 5 minutes before each high-importance event and each 10/20/30-year auction (scheduled in the
    morning with ntfy's delayed delivery, so they arrive on time even when GitHub is late)
Importance: 3 = high (TradingView's three blue bars), 2 = medium, 1 = low.
"""

import argparse
import csv
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

MT = ZoneInfo("America/Denver")
UA = "Mozilla/5.0 (personal research recorder)"
COLUMNS = ["key", "time_utc", "date_mt", "time_mt", "title", "importance", "actual", "forecast", "previous",
           "unit", "category", "source", "updated_utc"]
KEY_AUCTIONS = ("10-Year", "20-Year", "30-Year")
PREOPEN = ("05:30", "07:30")          # the user's favourite window before the 07:30 MT cash open
REPO_URL = "https://github.com/GoldCoastCodig/Trading-Research/blob/market-data/"


def http_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def num_text(v):
    if v is None or v == "":
        return ""
    if isinstance(v, float):
        return ("%.4f" % v).rstrip("0").rstrip(".")
    return str(v)


# Raised to high: shown as high by the user's old calendar widget, or always market-moving (FOMC, Fed chair).
BOOST = re.compile(r"^Initial Jobless Claims$|^ADP Employment Change$|S&P Global Composite PMI|"
                   r"Fed Interest Rate Decision|FOMC|Fed Chair", re.I)
SUBROW = re.compile(r"allotted|bid-to-cover|total accepted|high yield|tail", re.I)


def event(time_utc, title, importance, source, actual="", forecast="", previous="", unit="", category=""):
    if BOOST.search(title.strip()):
        importance = 3
    t = time_utc.astimezone(MT)
    iso = time_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"key": "%s|%s" % (iso, re.sub(r"\s+", " ", title.strip().lower())), "time_utc": iso,
            "date_mt": t.date().isoformat(), "time_mt": t.strftime("%H:%M"), "title": title.strip(),
            "importance": importance, "actual": num_text(actual), "forecast": num_text(forecast),
            "previous": num_text(previous), "unit": unit or "", "category": category or "", "source": source}


# ---------------------------------------------------------------- sources

def from_tradingview(start, end):
    url = ("https://economic-calendar.tradingview.com/events?from=%sT00:00:00.000Z&to=%sT00:00:00.000Z"
           "&countries=US" % (start.isoformat(), end.isoformat()))
    j = http_json(url, {"Origin": "https://www.tradingview.com", "Referer": "https://www.tradingview.com/"})
    out = []
    for e in j.get("result", []):
        t = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
        imp = {1: 3, 0: 2, -1: 1}.get(e.get("importance"), 1)
        out.append(event(t, e.get("title", ""), imp, "tradingview", e.get("actual"), e.get("forecast"),
                         e.get("previous"), (e.get("unit") or "") + (e.get("scale") or ""), e.get("category")))
    if not out:
        raise RuntimeError("TradingView returned no events")
    return out


def from_forexfactory():
    out = []
    for week in ("thisweek", "nextweek"):
        try:
            rows = http_json("https://nfs.faireconomy.media/ff_calendar_%s.json" % week)
        except Exception:
            if week == "thisweek":
                raise
            continue
        for e in rows:
            if e.get("country") != "USD" or e.get("impact") == "Holiday":
                continue
            t = datetime.fromisoformat(e["date"])
            imp = {"High": 3, "Medium": 2, "Low": 1}.get(e.get("impact"), 1)
            out.append(event(t, e.get("title", ""), imp, "forexfactory", "", e.get("forecast"), e.get("previous")))
    return out


def from_treasury(start, end):
    url = ("https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/upcoming_auctions"
           "?sort=auction_date&page[size]=200")
    out = []
    for a in http_json(url).get("data", []):
        if a.get("security_type") not in ("Note", "Bond", "TIPS"):
            continue
        d = date.fromisoformat(a["auction_date"])
        if not (start <= d <= end):
            continue
        t = datetime(d.year, d.month, d.day, 13, 0, tzinfo=ZoneInfo("America/New_York"))
        term = a.get("security_term", "")
        name = "%s %s Auction" % (term, "TIPS" if a["security_type"] == "TIPS" else a["security_type"])
        imp = 2 if term in KEY_AUCTIONS else 1
        out.append(event(t, name, imp, "treasury", category="Bond auction"))
    return out


TERM = re.compile(r"(\d+)[- ]?(?:year|yr|y)\b", re.I)


def auction_term(e):
    """'10-Year Note Auction' / '10-y Bond Auction' -> '10'; '' when not a coupon auction."""
    if "auction" not in e["title"].lower() or SUBROW.search(e["title"]):
        return ""                  # not an auction, or a detail row (bid-to-cover etc.) of one
    m = TERM.search(e["title"])
    return m.group(1) if m else ""


def is_key_auction(e):
    return auction_term(e) in ("10", "20", "30")


def alert_worthy(e):
    return int(e["importance"]) >= 3 or is_key_auction(e)


def fetch(today):
    """All US events from about two weeks back to two weeks ahead, plus notes on which sources worked."""
    start, end = today - timedelta(days=15), today + timedelta(days=22)
    events, notes = [], []
    try:
        events = from_tradingview(start, end)
        notes.append("TradingView: %d events" % len(events))
    except Exception as e:
        notes.append("TradingView FAILED (%s)" % e)
        try:
            events = from_forexfactory()
            notes.append("Forex Factory: %d events" % len(events))
        except Exception as e2:
            notes.append("Forex Factory FAILED (%s)" % e2)
    try:
        auctions = from_treasury(start, end)
        listed = {(e["date_mt"], auction_term(e)) for e in events if auction_term(e)}
        added = [a for a in auctions if (a["date_mt"], auction_term(a)) not in listed]
        events += added
        notes.append("Treasury: %d coupon auctions (%d added)" % (len(auctions), len(added)))
    except Exception as e:
        notes.append("Treasury FAILED (%s)" % e)
    return sorted(events, key=lambda e: e["time_utc"]), notes


# ---------------------------------------------------------------- storage and pages

def store(cal_dir, events):
    """Upsert into calendar/events_YYYY.csv (actuals/forecasts are updated when they change)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    by_year = {}
    for e in events:
        by_year.setdefault(e["date_mt"][:4], []).append(e)
    cal_dir.mkdir(parents=True, exist_ok=True)
    for year, evs in by_year.items():
        path = cal_dir / ("events_%s.csv" % year)
        rows = {}
        if path.exists():
            with open(path, newline="", encoding="utf-8") as f:
                rows = {r["key"]: r for r in csv.DictReader(f)}
        for e in evs:
            old = rows.get(e["key"])
            new = {**e, "updated_utc": now}
            if old:   # keep a known actual if a later fetch has none
                for k in ("actual", "forecast", "previous"):
                    if not new[k] and old.get(k):
                        new[k] = old[k]
                if all(str(old.get(k, "")) == str(new[k]) for k in COLUMNS if k != "updated_utc"):
                    new["updated_utc"] = old.get("updated_utc", now)
            rows[e["key"]] = new
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            for r in sorted(rows.values(), key=lambda r: r["time_utc"]):
                w.writerow({k: r.get(k, "") for k in COLUMNS})


def bars(e):
    return {3: "●●●", 2: "●●○", 1: "●○○"}.get(int(e["importance"]), "")


def mark_shown(events):
    """Which events the page and the phone show: high importance, note/bond auctions (one row each), and medium
    items in the pre-open window when no high-importance release comes out at the same minute (then they are
    only details of it, or noise). Everything is still stored."""
    high = {(e["date_mt"], e["time_mt"]) for e in events if int(e["importance"]) >= 3}
    for e in events:
        imp = int(e["importance"])
        e["_show"] = (imp >= 3 or bool(auction_term(e)) or
                      (imp == 2 and PREOPEN[0] <= e["time_mt"] <= PREOPEN[1] and (e["date_mt"], e["time_mt"]) not in high))
    return events


def shown(e):
    return e.get("_show", int(e["importance"]) >= 3)


def week_start(d):
    return d - timedelta(days=d.weekday())


def paste_line(events, day):
    """EV|date|HHMM:importance:title;...  (Mountain time) -- what the chart indicators read (part 2)."""
    parts = ["%s:%s:%s" % (e["time_mt"].replace(":", ""), 3 if alert_worthy(e) else e["importance"],
                           re.sub(r"[;|:]", " ", e["title"])) for e in events if e["date_mt"] == day.isoformat()
             and shown(e)]
    return "EV|%s|%s" % (day.isoformat(), ";".join(parts))


def calendar_md(events, today, notes, test=False):
    mark_shown(events)
    ws = week_start(today) if today.weekday() < 5 else today + timedelta(days=7 - today.weekday())  # weekends look ahead
    lines = ["# %sUS economic calendar (Mountain time)" % ("TEST — " if test else ""), "",
             "Updated %s MT. ●●● = high importance (alerts 30 and 5 min before); ●●○ medium, shown only in the "
             "pre-open window; everything else is kept in `calendar/events_YYYY.csv`. ⏰ = in the pre-open window "
             "%s–%s. Bond auctions: results at 11:00." % (datetime.now(MT).strftime("%Y-%m-%d %H:%M"), *PREOPEN), ""]
    for label, start in (("Last week", ws - timedelta(days=7)), ("This week", ws), ("Next week", ws + timedelta(days=7))):
        lines += ["## %s (%s)" % (label, start.strftime("%b %d")), ""]
        any_rows = False
        for i in range(7):
            d = start + timedelta(days=i)
            evs = [e for e in events if e["date_mt"] == d.isoformat() and shown(e)]
            if not evs:
                continue
            any_rows = True
            lines += ["**%s%s**" % (d.strftime("%a %b %d"), "  ← today" if d == today else ""), "",
                      "| Time | | Event | Actual | Forecast | Previous |", "|---|---|---|---|---|---|"]
            for e in evs:
                pre = " ⏰" if PREOPEN[0] <= e["time_mt"] <= PREOPEN[1] else ""
                title = "**%s**" % e["title"] if alert_worthy(e) else e["title"]
                lines.append("| %s%s | %s | %s | %s | %s | %s |" % (e["time_mt"], pre, bars(e), title, e["actual"],
                                                                     e["forecast"], e["previous"]))
            lines.append("")
        if not any_rows:
            lines += ["No high-importance US events listed.", ""]
    nxt = today if today.weekday() < 5 else today + timedelta(days=7 - today.weekday())
    lines += ["## Paste line for chart indicators (%s)" % nxt.isoformat(), "", "```", paste_line(events, nxt), "```", "",
              "Sources: " + "; ".join(notes) + ".", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- phone

def ntfy(topic, title, body, at=None, priority=None, click=None):
    if not topic:
        return "no NTFY_TOPIC"
    headers = {"Title": title.encode("utf-8"), "Tags": "calendar", "User-Agent": "trading-research-recorder/1.0"}
    if at:
        headers["At"] = str(int(at.timestamp()))
    if priority:
        headers["Priority"] = str(priority)
    if click:
        headers["Click"] = click
    req = urllib.request.Request("https://ntfy.sh/" + topic, data=body.encode("utf-8"), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode()).get("id", "sent")


def day_text(events, d, only_alerts=False):
    evs = [e for e in events if e["date_mt"] == d.isoformat() and (alert_worthy(e) if only_alerts else shown(e))]
    out = []
    for e in evs:
        mark = "‼ " if alert_worthy(e) else ""
        extra = " (f %s, p %s)" % (e["forecast"] or "-", e["previous"] or "-") if (e["forecast"] or e["previous"]) else ""
        out.append("%s %s%s%s%s" % (e["time_mt"], mark, e["title"], extra,
                                    " ⏰" if PREOPEN[0] <= e["time_mt"] <= PREOPEN[1] else ""))
    return out


def morning(topic, events, d, test=False, path="CALENDAR.md"):
    lines = day_text(events, d)
    pre = [e for e in events if e["date_mt"] == d.isoformat() and shown(e) and PREOPEN[0] <= e["time_mt"] <= PREOPEN[1]]
    head = ("Pre-open %s–%s: %s" % (PREOPEN[0], PREOPEN[1], ", ".join("%s %s" % (e["time_mt"], e["title"]) for e in pre))
            if pre else "Pre-open %s–%s: no scheduled US releases" % PREOPEN)
    body = head + "\n\n" + ("\n".join(lines) if lines else "No medium- or high-importance US events today.")
    big = [e for e in events if e["date_mt"] == d.isoformat() and alert_worthy(e)]
    title = "%s%s: %s" % ("TEST: " if test else "", d.strftime("%a %b %d"),
                          ", ".join("%s %s" % (e["title"], e["time_mt"]) for e in big[:3]) if big else "no high-impact news")
    return ntfy(topic, title, body, click=REPO_URL + path)


def sunday(topic, events, today, test=False, path="CALENDAR.md"):
    mon = today + timedelta(days=7 - today.weekday()) if today.weekday() >= 5 else week_start(today)
    lines = []
    for i in range(5):
        d = mon + timedelta(days=i)
        evs = day_text(events, d, only_alerts=True)
        lines.append("%s: %s" % (d.strftime("%a %b %d"), "; ".join(x.replace("‼ ", "") for x in evs) if evs else "—"))
    return ntfy(topic, "%sWeek ahead (from %s): high-impact US news" % ("TEST: " if test else "", mon.strftime("%b %d")),
                "\n".join(lines), click=REPO_URL + path)


def schedule_alerts(topic, events, d, now):
    """One reminder per release time (all events at that minute together), 30 and 5 minutes before."""
    slots = {}
    for e in events:
        if e["date_mt"] == d.isoformat() and alert_worthy(e):
            slots.setdefault(e["time_utc"], []).append(e)
    sent = []
    for iso, evs in sorted(slots.items()):
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        names = ", ".join(e["title"] for e in evs)
        detail = "\n".join("%s: forecast %s, previous %s" % (e["title"], e["forecast"] or "-", e["previous"] or "-")
                            for e in evs)
        for mins in (30, 5):
            at = t - timedelta(minutes=mins)
            if at <= now + timedelta(minutes=1):
                continue
            title = "%s in %d min (%s MT)" % (names if len(names) < 60 else "%s +%d more" % (evs[0]["title"], len(evs) - 1),
                                              mins, evs[0]["time_mt"])
            body = detail + ("\nNo-trade zone starts now: 5 minutes before until 5 minutes after." if mins == 5
                             else "\nTime to plan your exit.")
            ntfy(topic, title, body, at=at, priority=4 if mins == 5 else 3)
            sent.append("%s %s -%dm" % (evs[0]["time_mt"], names, mins))
    return sent


# ---------------------------------------------------------------- entry points

def refresh(out_dir, today=None, test=False):
    """Fetch, store, write CALENDAR.md. Returns (events, notes). Used by the 15:30 run to pick up actuals."""
    today = today or datetime.now(MT).date()
    base = Path(out_dir) / ("test" if test else "")
    events, notes = fetch(today)
    mark_shown(events)
    if events:
        store(base / "calendar", events)
        (base / "CALENDAR.md").write_text(calendar_md(events, today, notes, test), encoding="utf-8")
    return events, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="market-data branch checkout")
    ap.add_argument("--notify", action="store_true", help="send the morning/Sunday message and schedule reminders")
    ap.add_argument("--test", action="store_true", help="write under test/, TEST messages, one demo reminder in 2 min")
    a = ap.parse_args()
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    now_utc = datetime.now(timezone.utc)
    now = now_utc.astimezone(MT)
    today = now.date()
    events, notes = refresh(a.out, today, a.test)
    print("; ".join(notes))
    if not events:
        print("No events; nothing sent.")
        return 1
    if not (a.notify or a.test):
        return 0
    base = Path(a.out) / ("test" if a.test else "")
    sent_file = base / "calendar" / "sent" / ("%s.txt" % today.isoformat())
    done = set(sent_file.read_text().split()) if sent_file.exists() else set()
    path = "test/CALENDAR.md" if a.test else "CALENDAR.md"
    log = []
    if a.test:
        d = today if today.weekday() < 5 else today + timedelta(days=7 - today.weekday())
        log.append("morning %s" % morning(topic, events, d, True, path))
        log.append("week %s" % sunday(topic, events, today, True, path))
        log.append("demo reminder %s" % ntfy(topic, "TEST reminder: this is how a 30/5-minute warning arrives",
                                            "Real ones: e.g. 'CPI in 5 min (06:30 MT)'.", at=now_utc + timedelta(minutes=2),
                                            priority=4))
    else:
        if today.weekday() == 6 and "sunday" not in done:
            log.append("week %s" % sunday(topic, events, today, False, path))
            done.add("sunday")
        if today.weekday() < 5:
            if "morning" not in done and now.hour < 9:      # a late backup run does not send a stale morning list
                log.append("morning %s" % morning(topic, events, today, False, path))
                done.add("morning")
            if "alerts" not in done:
                log.append("alerts: %s" % (", ".join(schedule_alerts(topic, events, today, now_utc)) or "none left today"))
                done.add("alerts")
        sent_file.parent.mkdir(parents=True, exist_ok=True)
        sent_file.write_text("\n".join(sorted(done)) + "\n")
    print("\n".join(log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
