"""Daily closing values of the Cboe volatility indexes, as far back as Cboe publishes them (free).

Used by the database to ESTIMATE the expected move on days before real recording started (2026-10-01):
    estimated daily move = futures close x index / 100 x sqrt(1/252)
Index per product: ES/MES/NES -> VIX, NQ/MNQ/NNQ -> VXN, RTY/M2K/N2K -> RVX, GC/MGC -> GVZ, CL/MCL -> OVX; 6E/M6E has no index any more (EVZ
discontinued), so the database uses 6E's own 20-session realized volatility.

Writes vol-history/<INDEX>.csv (date,open,high,low,close) to the market-data branch checkout.
Run by the recorder workflow (mode "history") and automatically on Fridays.
"""

import csv
import io
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

INDEXES = ["VIX", "VXN", "RVX", "GVZ", "OVX", "VIX9D", "VIX1D"]   # EVZ (euro) discontinued by Cboe
URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{}_History.csv"
UA = "Mozilla/5.0 (personal research recorder)"


def parse_date(s):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse(text, ix):
    """Cboe files are DATE,OPEN,HIGH,LOW,CLOSE, or DATE,<INDEX> (close only); other text lines are skipped."""
    rows, header = [], None
    for rec in csv.reader(io.StringIO(text)):
        if not rec:
            continue
        cells = [c.strip() for c in rec]
        if header is None:
            if cells[0].upper() == "DATE":
                header = [c.upper() for c in cells]
            continue
        d = parse_date(cells[0])
        if not d:
            continue
        rec_map = dict(zip(header, cells))
        close = rec_map.get("CLOSE") or rec_map.get(ix) or (cells[-1] if len(cells) > 1 else "")
        try:
            float(close)
        except ValueError:
            continue
        rows.append({"date": d, "open": rec_map.get("OPEN", ""), "high": rec_map.get("HIGH", ""),
                     "low": rec_map.get("LOW", ""), "close": close})
    return sorted({r["date"]: r for r in rows}.values(), key=lambda r: r["date"])


def refresh(out_dir):
    out = Path(out_dir) / "vol-history"
    out.mkdir(parents=True, exist_ok=True)
    report = []
    for ix in INDEXES:
        try:
            req = urllib.request.Request(URL.format(ix), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                rows = parse(r.read().decode("utf-8", "replace"), ix)
            if not rows:
                raise RuntimeError("no rows parsed")
            with open(out / f"{ix}.csv", "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=["date", "open", "high", "low", "close"])
                w.writeheader()
                w.writerows(rows)
            report.append(f"{ix}: {len(rows)} days {rows[0]['date']}..{rows[-1]['date']}")
        except Exception as e:  # one missing index must not stop the others
            report.append(f"{ix}: FAILED {e}")
    return report


if __name__ == "__main__":
    for line in refresh(sys.argv[1] if len(sys.argv) > 1 else "."):
        print(line)
