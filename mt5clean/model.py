"""Core data types shared across the cleaner.

Timestamps are kept as integer *minutes since the Unix epoch*, in whatever
clock the broker exported (MT5 gives you server time, not UTC). Working in
integer minutes makes gap arithmetic exact and keeps memory small enough to
stream a decade of M1 bars.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import NamedTuple, Optional

EPOCH_ORD = date(1970, 1, 1).toordinal()
MINUTES_PER_WEEK = 7 * 24 * 60
# Unix epoch fell on a Thursday; shift so minute-of-week 0 == Monday 00:00.
_MONDAY_SHIFT = 3 * 1440


class Bar(NamedTuple):
    ts: int
    """Minutes since epoch, truncated to the minute."""

    sec: int
    """Seconds component of the original stamp. Non-zero means off-grid."""

    open: float
    high: float
    low: float
    close: float
    tick_volume: float
    real_volume: float
    spread: float
    line_no: int


class Severity:
    ERROR = "error"
    WARN = "warn"
    INFO = "info"

    _ORDER = {INFO: 0, WARN: 1, ERROR: 2}

    @classmethod
    def rank(cls, level: str) -> int:
        return cls._ORDER.get(level, 0)


class Issue(NamedTuple):
    code: str
    severity: str
    line_no: Optional[int]
    ts: Optional[int]
    message: str


class Gap(NamedTuple):
    start_ts: int
    """First missing minute."""

    end_ts: int
    """Last missing minute."""

    missing: int
    """Missing minutes that fall inside the detected trading session."""

    total: int
    """Total missing minutes in the run, session or not."""

    kind: str
    """One of: weekend, session_break, holiday, intraday, unknown."""


def ts_to_datetime(ts: int) -> datetime:
    return datetime(1970, 1, 1) + timedelta(minutes=ts)


def ts_to_string(ts: int, sec: int = 0) -> str:
    dt = ts_to_datetime(ts)
    return f"{dt:%Y.%m.%d %H:%M}:{sec:02d}"


def minute_of_week(ts: int) -> int:
    return (ts + _MONDAY_SHIFT) % MINUTES_PER_WEEK


def week_index(ts: int) -> int:
    return (ts + _MONDAY_SHIFT) // MINUTES_PER_WEEK


def describe_minute_of_week(mow: int) -> str:
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    day, rem = divmod(mow, 1440)
    hour, minute = divmod(rem, 60)
    return f"{names[day]} {hour:02d}:{minute:02d}"
