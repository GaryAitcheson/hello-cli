"""Auditing for MT5 tick exports.

Ticks are a different animal from bars: there is no fixed grid to check bars
against (ticks arrive whenever the market moves, sometimes several in the same
millisecond), so "gap" detection here means unusually long silences relative
to the instrument's own tempo, not missing slots on a calendar. The checks
that matter are different too: crossed quotes, a spread that has blown out,
long stretches with no size on one side of the book.

Bars and ticks share the trading-session model (`sessions.py`) since a quiet
market is a quiet market either way, but everything about *how* a series is
expected to look is separate, which is why this lives in its own module
rather than being bolted onto `audit.py`.
"""

from __future__ import annotations

from array import array
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .model import Issue, Severity, Tick, minute_of_week, tick_ts_to_string
from .audit import _median, _sample
from .reader import Dialect, detect_digits, read_ticks, sniff
from .sessions import PresenceHistogram, SessionMask

_TICK_SEVERITY = {
    "unparseable_row": Severity.ERROR,
    "crossed_quote": Severity.ERROR,
    "non_positive_quote": Severity.ERROR,
    "out_of_order": Severity.ERROR,
    "duplicate_conflicting": Severity.ERROR,
    "duplicate_identical": Severity.WARN,
    "wide_spread": Severity.WARN,
    "silence": Severity.WARN,
    "short_history": Severity.INFO,
}


@dataclass
class TickThresholds:
    session: float = 0.5
    """Same meaning as `audit.Thresholds.session`, over minute-of-week."""

    spread_mult: float = 20.0
    """Spread this many times the median spread is flagged as blown out."""

    silence_mult: float = 100.0
    """A gap this many times the median inter-tick time is flagged."""

    max_examples: int = 200


@dataclass
class TickAuditResult:
    path: str
    dialect: Dialect
    digits: int
    thresholds: TickThresholds

    ticks: int = 0
    first_ts: Optional[int] = None
    last_ts: Optional[int] = None

    issues: List[Issue] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)
    mask: Optional[SessionMask] = None

    median_spread: float = 0.0
    median_gap_ms: float = 0.0

    @property
    def error_count(self) -> int:
        return sum(
            n for code, n in self.counts.items() if _TICK_SEVERITY.get(code) == Severity.ERROR
        )

    @property
    def warn_count(self) -> int:
        return sum(
            n for code, n in self.counts.items() if _TICK_SEVERITY.get(code) == Severity.WARN
        )

    def add(self, code: str, line_no: Optional[int], ts: Optional[int], message: str) -> None:
        self.counts[code] += 1
        if self.counts[code] <= self.thresholds.max_examples:
            self.issues.append(
                Issue(code, _TICK_SEVERITY.get(code, Severity.WARN), line_no, ts, message)
            )


def audit_ticks(
    path: str,
    thresholds: Optional[TickThresholds] = None,
    dialect: Optional[Dialect] = None,
) -> TickAuditResult:
    thresholds = thresholds or TickThresholds()
    dialect = dialect or sniff(path)
    result = TickAuditResult(
        path=path, dialect=dialect, digits=detect_digits(path, dialect), thresholds=thresholds
    )

    # Absolute millisecond timestamps, so ordering and gap arithmetic do not
    # have to reason about minute/ms as two separate fields.
    moments = array("q")
    spreads = array("d")
    spread_moments = array("q")
    spread_lines = array("i")
    lines = array("i")
    histogram = PresenceHistogram()

    prev: Optional[Tick] = None
    prev_moment: Optional[int] = None
    out_of_order = False

    parse_stats: Dict[str, int] = {}
    for tick in read_ticks(path, dialect, result.issues, stats=parse_stats):
        moment = tick.ts * 60_000 + tick.ms
        _scan_tick(result, tick, prev, moment, prev_moment)

        if prev_moment is not None and moment < prev_moment:
            out_of_order = True

        moments.append(moment)
        if tick.bid > 0 and tick.ask > 0:
            spreads.append(tick.spread)
            spread_moments.append(moment)
            spread_lines.append(tick.line_no)
        lines.append(tick.line_no)
        histogram.add(tick.ts)
        prev, prev_moment = tick, moment

    result.ticks = len(moments)
    if parse_stats.get("unparseable_row"):
        result.counts["unparseable_row"] = parse_stats["unparseable_row"]
    if result.ticks == 0:
        result.add("unparseable_row", None, None, "no readable ticks in file")
        return result

    ordered = sorted(moments) if out_of_order else moments
    result.first_ts = ordered[0] // 60_000
    result.last_ts = ordered[-1] // 60_000

    result.mask = histogram.build(thresholds.session)
    if not result.mask.inferred:
        result.add(
            "short_history", None, None,
            f"file spans only {result.mask.weeks_spanned} week(s); session model fell back "
            "to Mon-Fri",
        )

    result.median_spread = _median(_sample(spreads, 200_000))
    _find_wide_spreads(result, spreads, spread_moments, spread_lines)
    _find_silences(result, ordered, lines)
    return result


def _find_wide_spreads(result: TickAuditResult, spreads, moments, lines) -> None:
    scale = result.median_spread
    if scale <= 0:
        return
    limit = scale * result.thresholds.spread_mult
    for i in range(len(spreads)):
        if spreads[i] > limit:
            ts = moments[i] // 60_000
            ms = moments[i] % 60_000
            result.add(
                "wide_spread", lines[i], ts,
                f"{tick_ts_to_string(ts, ms)} spread={spreads[i]:.6g} "
                f"({spreads[i] / scale:,.0f}x median)",
            )


def _scan_tick(
    result: TickAuditResult,
    tick: Tick,
    prev: Optional[Tick],
    moment: int,
    prev_moment: Optional[int],
) -> None:
    has_bid_ask = tick.bid > 0 and tick.ask > 0
    if not has_bid_ask and tick.last <= 0:
        result.add(
            "non_positive_quote", tick.line_no, tick.ts,
            f"{tick_ts_to_string(tick.ts, tick.ms)} bid={tick.bid} ask={tick.ask} last={tick.last}",
        )
    elif has_bid_ask and tick.ask < tick.bid:
        result.add(
            "crossed_quote", tick.line_no, tick.ts,
            f"{tick_ts_to_string(tick.ts, tick.ms)} bid={tick.bid} > ask={tick.ask}",
        )

    if prev is None or prev_moment is None:
        return
    if moment < prev_moment:
        result.add(
            "out_of_order", tick.line_no, tick.ts,
            f"{tick_ts_to_string(tick.ts, tick.ms)} follows {tick_ts_to_string(prev.ts, prev.ms)}",
        )
    elif moment == prev_moment:
        same = tick.bid == prev.bid and tick.ask == prev.ask and tick.last == prev.last
        code = "duplicate_identical" if same else "duplicate_conflicting"
        result.add(code, tick.line_no, tick.ts, tick_ts_to_string(tick.ts, tick.ms))


def _find_silences(result: TickAuditResult, ordered, lines) -> None:
    """Flag unusually long stretches with no ticks, inside trading hours.

    Unlike bars, ticks have no calendar to check against, so "expected" is
    defined relative to the file's own tempo: a silence many times longer than
    the median inter-tick gap, while the session mask says the market should
    be open, is a feed outage rather than a quiet moment.
    """
    if len(ordered) < 50:
        return
    gaps = array("q")
    for i in range(1, len(ordered)):
        gaps.append(ordered[i] - ordered[i - 1])
    result.median_gap_ms = _median(_sample(gaps, 200_000))
    if result.median_gap_ms <= 0:
        return

    limit_ms = result.median_gap_ms * result.thresholds.silence_mult
    mask = result.mask
    for i in range(1, len(ordered)):
        gap = ordered[i] - ordered[i - 1]
        if gap < limit_ms:
            continue
        start_min = ordered[i - 1] // 60_000
        end_min = ordered[i] // 60_000
        # Only the *interior* of the gap was silent - the ticks bracketing it
        # are, by definition, always in session. Checking start_min/end_min
        # themselves would make almost every overnight or weekend gap look
        # "in session" just because trading resumed at its far edge.
        interior = range(start_min + 1, end_min)
        if mask is not None and not any(mask.in_session[minute_of_week(m)] for m in interior):
            continue  # the market was simply closed
        result.add(
            "silence", lines[i], end_min,
            f"{gap / 1000:,.1f}s with no ticks before "
            f"{tick_ts_to_string(ordered[i] // 60_000, ordered[i] % 60_000)} "
            f"({gap / result.median_gap_ms:,.0f}x the typical gap)",
        )
