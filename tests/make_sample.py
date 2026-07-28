"""Generate a synthetic MT5 M1 export with known, deliberate defects.

Used by the test suite and by `samples/EURUSD_M1_dirty.csv` so the tool has
something to demonstrate on. The defect list is the point: every one of these
is something the auditor is expected to catch.
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta
from typing import List, Tuple

# Broker session used for the sample: opens Sunday 22:00, closes Friday 22:00,
# with a one-hour daily rollover break at 21:00-22:00. That shape is common for
# GMT+2/+3 FX servers and exercises the session inference.
WEEK_OPEN = (6, 22 * 60)      # Sunday 22:00
WEEK_CLOSE = (4, 22 * 60)     # Friday 22:00
DAILY_BREAK = (21 * 60, 22 * 60)


def in_session(dt: datetime) -> bool:
    weekday = dt.weekday()          # Mon=0 .. Sun=6
    minutes = dt.hour * 60 + dt.minute

    if weekday == 5:                                  # Saturday
        return False
    if weekday == 6:                                  # Sunday
        return minutes >= WEEK_OPEN[1]
    if weekday == 4 and minutes >= WEEK_CLOSE[1]:     # Friday close
        return False
    if DAILY_BREAK[0] <= minutes < DAILY_BREAK[1]:    # nightly rollover
        return False
    return True


def generate(
    start: datetime, weeks: int, seed: int = 7, step: int = 1
) -> List[Tuple[datetime, float, float, float, float, int, int]]:
    """Generate a clean series. `step` is the bar timeframe in minutes."""
    rng = random.Random(seed)
    price = 1.10000
    rows = []
    cursor = start
    end = start + timedelta(weeks=weeks)

    while cursor < end:
        if in_session(cursor):
            drift = rng.gauss(0, 0.00012)
            o = price
            c = round(o + drift, 5)
            wick = abs(rng.gauss(0, 0.00008))
            h = round(max(o, c) + wick, 5)
            l = round(min(o, c) - wick, 5)
            volume = max(1, int(rng.gauss(40, 15)))
            spread = rng.choice([6, 7, 8, 9, 12])
            rows.append((cursor, o, h, l, c, volume, spread))
            price = c
        cursor += timedelta(minutes=step)
    return rows


def inject(rows, seed: int = 11):
    """Deliberately corrupt the clean series. Returns (rows, description)."""
    rng = random.Random(seed)
    rows = list(rows)
    notes = []

    # 1. A three-hour hole in the middle of a Wednesday session.
    target = next(
        i for i, r in enumerate(rows)
        if r[0].weekday() == 2 and r[0].hour == 10 and r[0].minute == 0
    )
    del rows[target:target + 180]
    notes.append("180-minute intraday gap on a Wednesday morning")

    # 2. A full missing trading day (holiday-style outage).
    day = rows[len(rows) // 2][0].date()
    before = len(rows)
    rows = [r for r in rows if r[0].date() != day]
    notes.append(f"whole day removed ({day}, {before - len(rows)} bars)")

    # 3. Duplicate timestamps: one identical, one conflicting.
    rows.insert(500, rows[500])
    dup = rows[900]
    rows.insert(901, (dup[0], dup[1], dup[2] + 0.0004, dup[3], dup[4] + 0.0002, dup[5], dup[6]))
    notes.append("2 duplicate timestamps (1 identical, 1 conflicting)")

    # 4. Broken OHLC: high below close.
    bad = rows[1500]
    rows[1500] = (bad[0], bad[1], round(bad[4] - 0.0003, 5), bad[3], bad[4], bad[5], bad[6])
    notes.append("1 bar with high < close")

    # 5. A price spike of ~200 pips on one bar.
    sp = rows[2200]
    rows[2200] = (sp[0], sp[1], round(sp[2] + 0.0200, 5), sp[3], sp[4], sp[5], sp[6])
    notes.append("1 bar with a 200-pip range spike")

    # 6. Zero-volume bars (dead feed minutes).
    for idx in rng.sample(range(3000, 3500), 12):
        r = rows[idx]
        rows[idx] = (r[0], r[1], r[2], r[3], r[4], 0, r[6])
    notes.append("12 zero-volume bars")

    # 7. An off-grid timestamp with a stray 30 seconds.
    og = rows[4000]
    rows[4000] = (og[0].replace(second=30), og[1], og[2], og[3], og[4], og[5], og[6])
    notes.append("1 timestamp landing at :30 seconds")

    # 8. Two bars swapped, so the file is not monotonic.
    rows[5000], rows[5001] = rows[5001], rows[5000]
    notes.append("1 out-of-order pair")

    # 9. A negative spread.
    ns = rows[6000]
    rows[6000] = (ns[0], ns[1], ns[2], ns[3], ns[4], ns[5], -3)
    notes.append("1 negative spread")

    return rows, notes


def write(rows, path: str, style: str = "mt5") -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        if style == "mt5":
            fh.write("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n")
            for dt, o, h, l, c, v, s in rows:
                fh.write(
                    f"{dt:%Y.%m.%d}\t{dt:%H:%M:%S}\t{o:.5f}\t{h:.5f}\t{l:.5f}\t{c:.5f}"
                    f"\t{v}\t0\t{s}\n"
                )
        elif style == "mt4":
            for dt, o, h, l, c, v, s in rows:
                fh.write(f"{dt:%Y.%m.%d},{dt:%H:%M},{o:.5f},{h:.5f},{l:.5f},{c:.5f},{v}\n")
        elif style == "eu":
            fh.write("Date;Time;Open;High;Low;Close;Volume\n")
            for dt, o, h, l, c, v, s in rows:
                fh.write(
                    f"{dt:%d.%m.%Y};{dt:%H:%M};{o:.5f};{h:.5f};{l:.5f};{c:.5f};{v}\n".replace(
                        ".", ",", 0
                    )
                )
        else:
            raise ValueError(style)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output")
    parser.add_argument("--weeks", type=int, default=12)
    parser.add_argument("--style", choices=("mt5", "mt4", "eu"), default="mt5")
    parser.add_argument("--clean", action="store_true", help="skip the defect injection")
    args = parser.parse_args()

    rows = generate(datetime(2024, 1, 1), args.weeks)
    if not args.clean:
        rows, notes = inject(rows)
        for note in notes:
            print(f"injected: {note}")
    write(rows, args.output, args.style)
    print(f"wrote {len(rows):,} bars to {args.output}")


if __name__ == "__main__":
    main()
