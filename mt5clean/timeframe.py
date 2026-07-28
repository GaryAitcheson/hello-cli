"""Bar timeframe detection.

The rest of the cleaner works in integer minutes, which covers every timeframe
MT5 exports from M1 up to W1. What changes with the timeframe is the *grid*:
which minutes are allowed to carry a bar. An M5 file has bars only on minutes
divisible by five, an H1 file only on the hour. Knowing the step lets the
auditor tell "this stamp is off the grid" from "this stamp is fine", and stops
an H1 file from being read as an M1 file with 59 missing bars an hour.

The step is inferred from the data rather than the filename, because MT5 names
exports inconsistently and a mislabelled file is exactly the sort of thing the
auditor should catch.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Optional, Tuple

from .model import minute_of_week

# MT5's standard chart periods, in minutes. MN1 is deliberately absent: month
# lengths vary, so it is not a fixed-minute grid and gap arithmetic would lie.
TIMEFRAMES = {
    "M1": 1,
    "M2": 2,
    "M3": 3,
    "M4": 4,
    "M5": 5,
    "M6": 6,
    "M10": 10,
    "M12": 12,
    "M15": 15,
    "M20": 20,
    "M30": 30,
    "H1": 60,
    "H2": 120,
    "H3": 180,
    "H4": 240,
    "H6": 360,
    "H8": 480,
    "H12": 720,
    "D1": 1440,
    "W1": 10080,
}

_BY_MINUTES = {minutes: name for name, minutes in TIMEFRAMES.items()}


def parse_timeframe(text: str) -> int:
    """Turn a timeframe name such as ``M5`` or ``H1`` into minutes."""
    key = text.strip().upper()
    if key in TIMEFRAMES:
        return TIMEFRAMES[key]
    raise ValueError(
        f"unknown timeframe {text!r}; choose from {', '.join(TIMEFRAMES)}"
    )


def name_for(minutes: int) -> str:
    """Name a step in minutes, falling back to a plain description."""
    if minutes in _BY_MINUTES:
        return _BY_MINUTES[minutes]
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def detect_step(timestamps: Iterable[int]) -> Tuple[int, bool]:
    """Infer the bar step in minutes from a sorted sequence of timestamps.

    Returns ``(minutes, is_standard)``. The modal gap between consecutive
    distinct bars is used: weekends, holidays and missing data all produce
    larger gaps, but the *common* case is one step, so the mode survives a
    surprising amount of damage.
    """
    deltas: Counter = Counter()
    prev: Optional[int] = None
    for ts in timestamps:
        if prev is not None and ts > prev:
            deltas[ts - prev] += 1
        prev = ts
    if not deltas:
        return 1, True

    step = deltas.most_common(1)[0][0]
    if step in _BY_MINUTES:
        return step, True

    # Not a standard period. If it is a clean multiple of a standard one the
    # file is probably a decimated export; otherwise report it as-is and let
    # the caller warn.
    return step, False


def on_grid(ts: int, step: int) -> bool:
    """True when ``ts`` sits on the timeframe grid.

    Anchored to Monday 00:00 rather than the Unix epoch (a Thursday), so weekly
    bars land where a trader expects. Every standard period divides the
    Monday offset evenly, so this agrees with plain ``ts % step`` for all
    intraday timeframes.
    """
    if step <= 1:
        return True
    return minute_of_week(ts) % step == 0


def grid_slots_per_week(step: int) -> int:
    """How many grid slots a week holds at this timeframe."""
    from .model import MINUTES_PER_WEEK

    return max(1, MINUTES_PER_WEEK // step)
