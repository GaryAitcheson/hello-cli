"""Infer the instrument's trading session from the data itself.

Hard-coding "forex trades Sunday 22:00 to Friday 22:00 UTC" is wrong for half
the instruments people export from MT5: indices and futures CFDs have daily
breaks, brokers sit on different GMT offsets, and those offsets move twice a
year with DST. So instead of assuming, we build a presence histogram over the
10,080 minutes of the week and let the file tell us when it trades.

A minute-of-week slot counts as in-session when bars show up there in at least
`threshold` of the weeks the file spans. Gaps inside those slots are real
holes; gaps outside them are the weekend, the daily break, or a holiday.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from .model import MINUTES_PER_WEEK, describe_minute_of_week, minute_of_week

MIN_WEEKS_FOR_INFERENCE = 3


@dataclass
class SessionMask:
    in_session: List[bool]
    weeks_spanned: int
    threshold: float
    inferred: bool
    """False when the file was too short and a Mon-Fri fallback was used."""

    def __contains__(self, ts: int) -> bool:
        return self.in_session[minute_of_week(ts)]

    @property
    def session_minutes_per_week(self) -> int:
        return sum(self.in_session)

    def windows(self) -> List[Tuple[int, int]]:
        """Contiguous in-session runs as (start, end) minute-of-week pairs.

        Runs that straddle the Sunday/Monday boundary are joined, which is the
        normal case for FX: the week opens Sunday evening.
        """
        mask = self.in_session
        if all(mask):
            return [(0, MINUTES_PER_WEEK - 1)]
        if not any(mask):
            return []

        start_offset = 0
        while mask[start_offset]:  # rotate to a closed minute so runs don't wrap
            start_offset += 1

        runs: List[Tuple[int, int]] = []
        run_start: Optional[int] = None
        for step in range(MINUTES_PER_WEEK):
            mow = (start_offset + step) % MINUTES_PER_WEEK
            if mask[mow]:
                if run_start is None:
                    run_start = mow
            elif run_start is not None:
                runs.append((run_start, (mow - 1) % MINUTES_PER_WEEK))
                run_start = None
        if run_start is not None:
            runs.append((run_start, (start_offset - 1) % MINUTES_PER_WEEK))
        return runs

    def describe_windows(self) -> List[str]:
        out = []
        for start, end in self.windows():
            length = (end - start) % MINUTES_PER_WEEK + 1
            hours = length / 60.0
            out.append(
                f"{describe_minute_of_week(start)} -> {describe_minute_of_week(end)}"
                f"  ({hours:,.1f}h)"
            )
        return out


class PresenceHistogram:
    """Accumulates which minutes of the week ever carry a bar."""

    def __init__(self) -> None:
        self.counts = [0] * MINUTES_PER_WEEK
        self.weeks = set()

    def add(self, ts: int) -> None:
        self.counts[minute_of_week(ts)] += 1
        self.weeks.add(ts // MINUTES_PER_WEEK)

    def build(self, threshold: float = 0.5) -> SessionMask:
        weeks = max(1, len(self.weeks))
        if weeks < MIN_WEEKS_FOR_INFERENCE:
            # Too little history to infer anything trustworthy: assume the FX
            # default of Monday-to-Friday and say so loudly in the report.
            mask = [(mow // 1440) < 5 for mow in range(MINUTES_PER_WEEK)]
            return SessionMask(mask, weeks, threshold, inferred=False)

        needed = threshold * weeks
        mask = [count >= needed for count in self.counts]
        return SessionMask(mask, weeks, threshold, inferred=True)


def classify_gap(start_ts: int, end_ts: int, session_missing: int) -> str:
    """Label a run of missing minutes.

    `start_ts`/`end_ts` are the first and last missing minute inclusive.
    """
    total = end_ts - start_ts + 1
    if session_missing == 0:
        # Entirely outside the session: weekend if it spans a Saturday, a
        # routine daily break otherwise.
        if total >= 12 * 60:
            return "weekend"
        return "session_break"
    if session_missing >= 20 * 60:
        return "holiday"
    return "intraday"
