"""Generate a sample MT5 M1 export with realistic defects, for trying the tool.

    python examples/make_sample.py examples/EURUSD_M1_sample.csv

The generated file deliberately contains: a weekend market close (which is NOT
a defect and should not be reported), an interior gap, a long gap, duplicate
timestamps, a row out of order, a broken OHLC bar, an isolated bad print and a
corrupt row.
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta

HEADER = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"


def build_rows(weeks: int = 2, seed: int = 7):
    rng = random.Random(seed)
    price = 1.09000
    rows = []
    cursor = datetime(2024, 3, 4)  # a Monday
    end = cursor + timedelta(weeks=weeks)
    while cursor < end:
        # FX week: Monday 00:00 to Friday 23:59, plus a nightly broker break.
        trading = cursor.weekday() < 5 and cursor.hour != 22
        if trading:
            drift = rng.gauss(0, 0.00012)
            open_ = price
            close = round(open_ + drift, 5)
            high = round(max(open_, close) + abs(rng.gauss(0, 0.00006)), 5)
            low = round(min(open_, close) - abs(rng.gauss(0, 0.00006)), 5)
            rows.append(
                {
                    "ts": cursor,
                    "o": open_,
                    "h": high,
                    "l": low,
                    "c": close,
                    "tv": rng.randint(8, 180),
                    "v": 0,
                    "s": rng.choice([8, 9, 10, 10, 11, 12]),
                }
            )
            price = close
        cursor += timedelta(minutes=1)
    return rows


def inject(rows):
    """Add the defects a real broker export tends to contain."""
    # 1. A short interior gap (4 missing minutes) on the first Tuesday.
    del rows[1500:1504]
    # 2. A long gap (90 minutes) mid-week — missing history, not fillable.
    del rows[4000:4090]
    # 3. A duplicate timestamp with conflicting values (a re-export artefact).
    duplicate = dict(rows[600])
    duplicate["c"] = round(duplicate["c"] + 0.00030, 5)
    rows.insert(601, duplicate)
    # 4. An exact duplicate row.
    rows.insert(900, dict(rows[900]))
    # 5. A bar whose high sits below its close.
    rows[2200]["h"] = round(rows[2200]["l"] - 0.00002, 5)
    # 6. An isolated bad print that reverts immediately.
    rows[3000]["c"] = round(rows[3000]["c"] * 1.02, 5)
    rows[3000]["h"] = rows[3000]["c"]
    # 7. A stuck feed: 15 flat bars.
    stuck = rows[5000]["c"]
    for row in rows[5000:5015]:
        row["o"] = row["h"] = row["l"] = row["c"] = stuck
        row["tv"] = 0
    # 8. A blown-out spread.
    rows[5500]["s"] = 340
    return rows


def render(rows) -> str:
    lines = [HEADER]
    for row in rows:
        lines.append(
            "\t".join(
                [
                    row["ts"].strftime("%Y.%m.%d"),
                    row["ts"].strftime("%H:%M:%S"),
                    f"{row['o']:.5f}",
                    f"{row['h']:.5f}",
                    f"{row['l']:.5f}",
                    f"{row['c']:.5f}",
                    str(row["tv"]),
                    str(row["v"]),
                    str(row["s"]),
                ]
            )
        )
    # 9. Two rows swapped out of chronological order.
    lines[2001], lines[2002] = lines[2002], lines[2001]
    # 10. A truncated/corrupt row, as produced by an interrupted write.
    lines.insert(3500, "2024.03.06\t14:00:00\t1.09\tCORRUPT")
    return "\n".join(lines) + "\n"


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "examples/EURUSD_M1_sample.csv"
    text = render(inject(build_rows()))
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    print(f"wrote {path} ({text.count(chr(10))} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
