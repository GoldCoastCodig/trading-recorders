"""News that HAS happened -> phone (ntfy "news" feed). Run every 5 minutes (cron-job.org -> public runner news.yml).

What it sends (topic NTFY_TOPIC_NEWS; falls back to NTFY_TOPIC when that secret is not set):
  1. Results of scheduled US releases, a few minutes after they come out: actual vs forecast and previous,
     "above / below / in line with forecast" (no opinion on what it means for the market). Same events as the
     calendar feed (econ_calendar.py, TradingView's calendar): high importance + the shown medium ones.
  2. "Speaking now": a scheduled Fed / FOMC speaker (Forex Factory's calendar) at the start time.
  3. Free news feeds (RSS), each new item once:
       fed      Federal Reserve: speeches and testimony (who + title + link to the full text when the Fed posts it)
       fomc     Federal Reserve: monetary-policy press releases (FOMC statements, minutes)
       trump    Trump's Truth Social posts (trumpstruth.org, an independent archive with an RSS feed); only posts
                with a market word (tariff, Fed, rates, China, oil ...) unless NEWS_TRUMP_ALL=1
       whitehouse  White House presidential actions (executive orders, proclamations: tariffs, sanctions)
       breaking MarketWatch bulletins (breaking market news headlines)
     The very first time a feed is read nothing is sent (everything already there is only marked as seen).
Honest limits: free feeds are minutes behind a paid squawk; how quickly depends on the feed itself plus the
5-minute run; summaries are the feed's own first lines (no AI summary); a feed can change or stop without notice
(news/SOURCES.md on market-data shows when each one last worked).

Writes (market-data branch): news/state.json (what was already sent), news/YYYY/news_YYYY-MM-DD.csv (every item
seen, sent or not), news/LATEST.md (last 60 items), news/SOURCES.md (status of every source).
"""

import argparse
import csv
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import econ_calendar as cal

MT = cal.MT
UA = "Mozilla/5.0 (personal research recorder)"

FEEDS = [  # name, label, urls (tried in order), priority
    ("fed", "Fed speech", ["https://www.federalreserve.gov/feeds/speeches_and_testimony.xml",
                           "https://www.federalreserve.gov/feeds/speeches.xml"], 4),
    ("fomc", "Fed release", ["https://www.federalreserve.gov/feeds/press_monetary.xml"], 5),
    ("trump", "Trump", ["https://www.trumpstruth.org/feed", "https://trumpstruth.org/feed"], 4),
    ("whitehouse", "White House", ["https://www.whitehouse.gov/presidential-actions/feed/"], 3),
    ("breaking", "Breaking", ["https://feeds.content.dowjones.io/public/rss/mw_bulletins",
                              "https://feeds.marketwatch.com/marketwatch/bulletins"], 3),
]
MARKET_WORDS = re.compile(
    r"tariff|trade deal|\bfed\b|federal reserve|powell|interest rate|\brates?\b|inflation|econom|stock|market|"
    r"dow\b|nasdaq|s&p|china|xi\b|japan|europe|\beu\b|mexico|canada|oil|opec|energy|gas price|iran|israel|russia|"
    r"ukraine|putin|war\b|strike|sanction|treasury|bond|debt|dollar|crypto|bitcoin|shutdown|budget|tax|jobs|"
    r"bank|gold|chips?\b|nvidia|apple|tesla|executive order", re.I)
MAX_PER_FEED = 5          # per run; the rest is one "+N more" line
MAX_AGE = timedelta(hours=24)
RESULT_WINDOW = timedelta(hours=3)
COLUMNS = ["time_utc", "source", "title", "text", "link", "sent"]


def news_topic():
    return (os.environ.get("NTFY_TOPIC_NEWS") or os.environ.get("NTFY_TOPIC") or "").strip()


def ntfy(topic, title, body, priority=3, click=None, tags="newspaper"):
    if not topic:
        return "no topic"
    headers = {"Title": title[:250].encode("utf-8"), "Tags": tags, "Priority": str(priority),
               "User-Agent": "trading-research-recorder/1.0"}
    if click:
        headers["Click"] = click
    req = urllib.request.Request("https://ntfy.sh/" + topic, data=body[:3500].encode("utf-8"), method="POST",
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode()).get("id", "sent")


# ---------------------------------------------------------------- RSS / Atom

def clean(text, limit=400):
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."


def parse_time(s):
    if not s:
        return None
    s = s.strip()
    try:
        t = parsedate_to_datetime(s)
    except Exception:
        try:
            t = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(data):
    root = ET.fromstring(data)
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        f = {}
        for c in el:
            name = local(c.tag)
            if name == "link" and c.get("href"):
                f.setdefault("link", c.get("href"))
            elif c.text and name not in f:
                f[name] = c.text
        title = clean(f.get("title", ""), 300)
        text = clean(f.get("description") or f.get("summary") or f.get("content") or f.get("encoded") or "")
        link = (f.get("link") or "").strip()
        uid = (f.get("guid") or f.get("id") or link or title).strip()
        when = parse_time(f.get("pubDate") or f.get("published") or f.get("updated") or f.get("date"))
        if uid:
            items.append({"id": uid, "title": title, "text": text, "link": link, "time": when})
    return items


def fetch_feed(urls):
    last = None
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, */*"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return parse_feed(r.read()), u
        except Exception as e:
            last = "%s: %s" % (u, e)
    raise RuntimeError(last)


# ---------------------------------------------------------------- scheduled releases

def as_num(v):
    try:
        return float(str(v).replace(",", "").rstrip("%KMB"))
    except Exception:
        return None


def verdict(e):
    a, f = as_num(e["actual"]), as_num(e["forecast"])
    if a is None or f is None:
        return ""
    return "in line" if abs(a - f) < 1e-9 else ("above forecast" if a > f else "below forecast")


def result_line(e):
    u = e.get("unit") or ""
    u = u if u in ("%",) else (" " + u if u else "")
    v = verdict(e)
    mark = {"above forecast": "▲ ", "below forecast": "▼ ", "in line": "= "}.get(v, "")
    return "%s%s: %s%s (forecast %s, previous %s)%s" % (mark, e["title"], e["actual"], u, e["forecast"] or "-",
                                                        e["previous"] or "-", " " + v if v else "")


def auction_rows(events, e):
    """Detail rows (high yield, bid-to-cover ...) of a 10/20/30-year auction at the same time."""
    term = cal.auction_term(e)
    return [x for x in events if x["time_utc"] == e["time_utc"] and x is not e and x["actual"]
            and re.search(r"\b%s[- ]?(?:year|yr|y)\b" % term, x["title"], re.I) and "auction" in x["title"].lower()]


def releases(events, state, now, first_run):
    """Messages for releases whose actual has arrived, one per release time; speakers at their start."""
    cal.mark_shown(events)
    done = set(state.setdefault("results", []))
    slots, talks, msgs = {}, [], []
    for e in events:
        t = datetime.fromisoformat(e["time_utc"].replace("Z", "+00:00"))
        if not (now - RESULT_WINDOW <= t <= now) or e["key"] in done:
            continue
        if cal.is_speaker(e):
            talks.append(e)
        elif cal.shown(e) and (e["actual"] or cal.is_key_auction(e)):
            if cal.is_key_auction(e) and not auction_rows(events, e) and not e["actual"]:
                continue     # auction results not in yet
            slots.setdefault(e["time_utc"], []).append(e)
    for iso, evs in sorted(slots.items()):
        lines = []
        for e in evs:
            if cal.is_key_auction(e):
                lines.append("%s results: %s" % (e["title"], "; ".join("%s %s%s" % (
                    re.sub(r"(?i)^.*auction\s*", "", x["title"]) or x["title"], x["actual"], x.get("unit") or "")
                    for x in auction_rows(events, e))))
            else:
                lines.append(result_line(e))
            done.add(e["key"])
        big = [e for e in evs if cal.alert_worthy(e)]
        head = big[0] if big else evs[0]
        title = "%s MT: %s %s%s" % (head["time_mt"], head["title"], head["actual"] or "",
                                    (" (f %s)" % head["forecast"]) if head["forecast"] else "")
        if len(evs) > 1:
            title += " +%d more" % (len(evs) - 1)
        msgs.append(("result", title, "\n".join(lines), 4 if big else 3, cal.REPO_URL + "CALENDAR.md", "chart_with_upwards_trend"))
    for e in talks:
        done.add(e["key"])
        if now - datetime.fromisoformat(e["time_utc"].replace("Z", "+00:00")) <= timedelta(minutes=30):
            msgs.append(("speaker", "Speaking now: %s (%s MT)" % (e["title"], e["time_mt"]),
                         "Scheduled on the calendar. The Fed posts the text of Board members' speeches in this feed "
                         "when it is published (regional Fed presidents often only on their own bank's site).",
                         3, None, "microphone"))
    state["results"] = sorted(done)[-400:]
    return [] if first_run else msgs


# ---------------------------------------------------------------- run

def load_state(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def log_items(base, rows):
    by_day = {}
    for r in rows:
        by_day.setdefault(r["time_utc"][:10], []).append(r)
    for day, rs in by_day.items():
        p = base / day[:4] / ("news_%s.csv" % day)
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists()
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            if new:
                w.writeheader()
            for r in rs:
                w.writerow({k: r.get(k, "") for k in COLUMNS})


def latest_md(base, test):
    rows = []
    for p in sorted(base.glob("*/news_*.csv"))[-3:]:
        with open(p, newline="", encoding="utf-8") as f:
            rows += list(csv.DictReader(f))
    rows = [r for r in rows if r["sent"] == "yes"][-60:]
    lines = ["# %sNews sent to the phone (Mountain time, newest first)" % ("TEST — " if test else ""), ""]
    for r in reversed(rows):
        t = datetime.fromisoformat(r["time_utc"].replace("Z", "+00:00")).astimezone(MT).strftime("%a %b %d %H:%M")
        link = " ([link](%s))" % r["link"] if r["link"] else ""
        lines.append("- **%s** %s: %s%s  \n  %s" % (t, r["source"], r["title"], link, r["text"].replace("\n", "; ")))
    (base / "LATEST.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="market-data branch checkout")
    ap.add_argument("--test", action="store_true", help="write under test/news, send TEST messages: the newest item "
                                                        "of every feed and the last release result")
    a = ap.parse_args()
    topic = news_topic()
    now = datetime.now(timezone.utc)
    base = Path(a.out) / ("test" if a.test else "") / "news"
    base.mkdir(parents=True, exist_ok=True)
    state_path = base / "state.json"
    state = load_state(state_path)
    first_run = not state
    seen = state.setdefault("seen", {})
    status = state.setdefault("sources", {})
    out, logged = [], []
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1-2. scheduled releases and speakers
    try:
        today = now.astimezone(MT).date()
        events, notes = cal.fetch(today)      # TradingView + Fed speakers (Forex Factory) + Treasury auctions
        if not events:
            raise RuntimeError("; ".join(notes))
        cal.mark_shown(events)
        if a.test:
            past = [e for e in events if e["actual"] and cal.shown(e)]
            if past:
                e = past[-1]
                out.append(("result", "TEST result: %s MT %s" % (e["time_mt"], e["title"]), result_line(e), 3, None,
                            "chart_with_upwards_trend"))
        else:
            out += releases(events, state, now, first_run)   # (actuals reach calendar/ with the 15:30 run)
        status["calendar"] = {"ok": True, "when": stamp, "note": "; ".join(notes)[:200]}
    except Exception as e:
        status["calendar"] = {"ok": False, "when": stamp, "note": str(e)[:200]}

    # 3. news feeds
    trump_all = os.environ.get("NEWS_TRUMP_ALL", "").strip() in ("1", "yes", "true")
    for name, label, urls, prio in FEEDS:
        try:
            items, used = fetch_feed(urls)
        except Exception as e:
            status[name] = {"ok": False, "when": stamp, "note": str(e)[:200],
                            "last_ok": status.get(name, {}).get("last_ok", "")}
            continue
        status[name] = {"ok": True, "when": stamp, "note": "%d items from %s" % (len(items), used), "last_ok": stamp}
        known = set(seen.get(name, []))
        primed = name in seen
        new = [i for i in items if i["id"] not in known]
        if a.test:
            new = items[:1]
        fresh = []
        for i in new:
            recent = i["time"] is None or now - i["time"] <= MAX_AGE
            wanted = name != "trump" or trump_all or MARKET_WORDS.search(i["title"] + " " + i["text"]) or a.test
            send = (primed or a.test) and (recent or a.test) and bool(wanted)
            logged.append({"time_utc": (i["time"] or now).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "source": name, "title": i["title"], "text": i["text"], "link": i["link"],
                           "sent": "yes" if send else "no"})
            if send:
                fresh.append(i)
        for i in fresh[:MAX_PER_FEED]:
            when = i["time"].astimezone(MT).strftime("%H:%M MT") if i["time"] else ""
            title = "%s%s: %s" % ("TEST " if a.test else "", label, i["title"] or i["text"][:80])
            body = (i["text"] if i["text"] and i["text"] != i["title"] else "") + ("\n" + when if when else "")
            out.append((name, title, body.strip() or label, prio, i["link"] or None, "newspaper"))
        if len(fresh) > MAX_PER_FEED:
            out.append((name, "%s: +%d more" % (label, len(fresh) - MAX_PER_FEED),
                        "\n".join(i["title"] for i in fresh[MAX_PER_FEED:])[:3000], 2, None, "newspaper"))
        if not a.test:
            seen[name] = ([i["id"] for i in items] + [x for x in seen.get(name, []) if x not in {i["id"] for i in items}])[:500]

    # send
    for src, title, body, prio, click, tags in out:
        try:
            ntfy(topic, title, body, prio, click, tags)
        except Exception as e:
            print("ntfy FAILED for %s: %s" % (title, e))
        if src in ("result", "speaker"):
            logged.append({"time_utc": stamp, "source": src, "title": title, "text": body, "link": click or "",
                           "sent": "yes"})
    if logged:
        log_items(base, logged)
        latest_md(base, a.test)
    src_lines = ["# News sources (status of the last run, UTC)", "", "| Source | OK | Checked | Last OK | Note |",
                 "|---|---|---|---|---|"]
    for k, v in sorted(status.items()):
        src_lines.append("| %s | %s | %s | %s | %s |" % (k, "yes" if v.get("ok") else "**NO**", v.get("when", ""),
                                                         v.get("last_ok", ""), v.get("note", "").replace("|", "/")))
    # SOURCES.md / state change every run; only write them when something else changed or a source failed/recovered,
    # so the data branch does not get a commit every 5 minutes
    old_ok = {k: v.get("ok") for k, v in load_state(state_path).get("sources", {}).items()}
    new_ok = {k: v.get("ok") for k, v in status.items()}
    if logged or out or first_run or old_ok != new_ok or a.test:
        (base / "SOURCES.md").write_text("\n".join(src_lines) + "\n", encoding="utf-8")
        state_path.write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")
    print("first run: everything marked as seen, nothing sent" if first_run and not a.test else
          "sent %d message(s) to %s" % (len(out), "the news topic" if topic else "nobody (no topic)"))
    for k, v in sorted(status.items()):
        print("  %-12s %s  %s" % (k, "ok " if v.get("ok") else "FAILED", v.get("note", "")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
