"""The analysis engine: read once, then answer everything from compact arrays.

One pass over the file fills a handful of `array` buffers (about 20 bytes per
bar, so a decade of M1 costs well under 100 MB) and a minute-of-week presence
histogram. Gaps, spikes and coverage are then derived in memory, which keeps
the file I/O to a single sequential read.
"""

from __future__ import annotations

import os
from array import array
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .model import (
    Bar,
    Gap,
    Issue,
    Severity,
    minute_of_week,
    ts_to_datetime,
    ts_to_string,
)
from .reader import Dialect, detect_digits, read_bars, sniff
from .sessions import PresenceHistogram, SessionMask, classify_gap


@dataclass
class Thresholds:
    session: float = 0.5
    """Fraction of weeks a minute-of-week must appear in to count as tradable."""

    spike_range_mult: float = 30.0
    """Bar range this many times the median range is flagged as a spike."""

    spike_jump_mult: float = 30.0
    """Close-to-close move this many median ranges is flagged as a jump."""

    max_examples: int = 200
    """Cap on stored per-row issue examples, to keep reports readable."""


@dataclass
class AuditResult:
    path: str
    dialect: Dialect
    digits: int
    thresholds: Thresholds

    bars: int = 0
    first_ts: Optional[int] = None
    last_ts: Optional[int] = None

    issues: List[Issue] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)
    gaps: List[Gap] = field(default_factory=list)
    mask: Optional[SessionMask] = None

    monthly: Dict[str, Dict[str, float]] = field(default_factory=dict)
    price_min: float = 0.0
    price_max: float = 0.0
    median_range: float = 0.0
    spread_min: float = 0.0
    spread_max: float = 0.0
    longest_frozen_run: int = 0
    longest_frozen_at: Optional[int] = None

    @property
    def session_missing(self) -> int:
        return sum(g.missing for g in self.gaps)

    @property
    def expected_session_minutes(self) -> int:
        return self.bars_unique + self.session_missing

    bars_unique: int = 0

    @property
    def coverage(self) -> float:
        expected = self.expected_session_minutes
        return 100.0 * self.bars_unique / expected if expected else 0.0

    @property
    def error_count(self) -> int:
        return sum(n for code, n in self.counts.items() if _SEVERITY.get(code) == Severity.ERROR)

    @property
    def warn_count(self) -> int:
        return sum(n for code, n in self.counts.items() if _SEVERITY.get(code) == Severity.WARN)

    def add(self, code: str, line_no: Optional[int], ts: Optional[int], message: str) -> None:
        self.counts[code] += 1
        if self.counts[code] <= self.thresholds.max_examples:
            self.issues.append(
                Issue(code, _SEVERITY.get(code, Severity.WARN), line_no, ts, message)
            )


_SEVERITY = {
    "unparseable_row": Severity.ERROR,
    "ohlc_invalid": Severity.ERROR,
    "non_positive_price": Severity.ERROR,
    "out_of_order": Severity.ERROR,
    "duplicate_conflicting": Severity.ERROR,
    "duplicate_identical": Severity.WARN,
    "off_grid": Severity.WARN,
    "gap_intraday": Severity.WARN,
    "gap_holiday": Severity.INFO,
    "zero_volume": Severity.WARN,
    "negative_volume": Severity.ERROR,
    "flat_bar": Severity.INFO,
    "negative_spread": Severity.WARN,
    "price_spike": Severity.WARN,
    "price_jump": Severity.WARN,
    "frozen_feed": Severity.WARN,
    "short_history": Severity.INFO,
}


def _median(values: array) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def audit(
    path: str,
    thresholds: Optional[Thresholds] = None,
    dialect: Optional[Dialect] = None,
) -> AuditResult:
    thresholds = thresholds or Thresholds()
    dialect = dialect or sniff(path)
    result = AuditResult(
        path=path,
        dialect=dialect,
        digits=detect_digits(path, dialect),
        thresholds=thresholds,
    )

    timestamps = array("i")
    closes = array("d")
    ranges = array("d")
    lines = array("i")
    histogram = PresenceHistogram()

    prev: Optional[Bar] = None
    out_of_order = False
    frozen_run = 1
    price_min = float("inf")
    price_max = float("-inf")
    spread_min = float("inf")
    spread_max = float("-inf")
    month_present: Counter = Counter()

    parse_stats: Dict[str, int] = {}
    for bar in read_bars(path, dialect, result.issues, stats=parse_stats):
        _scan_bar(result, bar, prev)

        if prev is not None:
            if bar.ts < prev.ts:
                out_of_order = True
            if (
                bar.open == prev.open
                and bar.high == prev.high
                and bar.low == prev.low
                and bar.close == prev.close
            ):
                frozen_run += 1
                if frozen_run > result.longest_frozen_run:
                    result.longest_frozen_run = frozen_run
                    result.longest_frozen_at = bar.ts
            else:
                frozen_run = 1

        timestamps.append(bar.ts)
        closes.append(bar.close)
        ranges.append(bar.high - bar.low)
        lines.append(bar.line_no)
        histogram.add(bar.ts)
        month_present[_month_key(bar.ts)] += 1

        price_min = min(price_min, bar.low)
        price_max = max(price_max, bar.high)
        spread_min = min(spread_min, bar.spread)
        spread_max = max(spread_max, bar.spread)
        prev = bar

    result.bars = len(timestamps)
    if parse_stats.get("unparseable_row"):
        result.counts["unparseable_row"] = parse_stats["unparseable_row"]
    if result.bars == 0:
        result.add("unparseable_row", None, None, "no readable bars in file")
        return result

    result.first_ts = min(timestamps) if out_of_order else timestamps[0]
    result.last_ts = max(timestamps) if out_of_order else timestamps[-1]
    result.price_min = price_min
    result.price_max = price_max
    result.spread_min = spread_min if spread_min != float("inf") else 0.0
    result.spread_max = spread_max if spread_max != float("-inf") else 0.0

    result.mask = histogram.build(thresholds.session)
    if not result.mask.inferred:
        result.add(
            "short_history",
            None,
            None,
            f"file spans only {result.mask.weeks_spanned} week(s); session model fell back "
            "to Mon-Fri and gap classification is approximate",
        )

    result.median_range = _median(_sample(ranges, 200_000))
    _find_spikes(result, timestamps, closes, ranges, lines)

    ordered = sorted(timestamps) if out_of_order else timestamps
    result.gaps, result.bars_unique = _find_gaps(result, ordered)
    _coverage_by_month(result, month_present)

    if result.longest_frozen_run >= 60:
        result.add(
            "frozen_feed",
            None,
            result.longest_frozen_at,
            f"{result.longest_frozen_run} consecutive identical OHLC bars ending "
            f"{ts_to_string(result.longest_frozen_at)} - looks like a stalled feed",
        )
    return result


def _month_key(ts: int) -> str:
    dt = ts_to_datetime(ts)
    return f"{dt.year:04d}-{dt.month:02d}"


def _sample(values: array, limit: int) -> array:
    if len(values) <= limit:
        return values
    step = len(values) // limit
    return values[::step]


def _scan_bar(result: AuditResult, bar: Bar, prev: Optional[Bar]) -> None:
    o, h, l, c = bar.open, bar.high, bar.low, bar.close

    if min(o, h, l, c) <= 0:
        result.add(
            "non_positive_price", bar.line_no, bar.ts,
            f"{ts_to_string(bar.ts, bar.sec)} O={o} H={h} L={l} C={c}",
        )
    elif h < l or h < max(o, c) or l > min(o, c):
        result.add(
            "ohlc_invalid", bar.line_no, bar.ts,
            f"{ts_to_string(bar.ts, bar.sec)} O={o} H={h} L={l} C={c}",
        )
    elif o == h == l == c:
        result.add("flat_bar", bar.line_no, bar.ts, ts_to_string(bar.ts, bar.sec))

    if bar.sec:
        result.add(
            "off_grid", bar.line_no, bar.ts,
            f"{ts_to_string(bar.ts, bar.sec)} is not on a whole minute",
        )

    if bar.tick_volume < 0:
        result.add(
            "negative_volume", bar.line_no, bar.ts,
            f"{ts_to_string(bar.ts)} volume={bar.tick_volume}",
        )
    elif bar.tick_volume == 0:
        result.add("zero_volume", bar.line_no, bar.ts, ts_to_string(bar.ts))

    if bar.spread < 0:
        result.add(
            "negative_spread", bar.line_no, bar.ts,
            f"{ts_to_string(bar.ts)} spread={bar.spread}",
        )

    if prev is None:
        return
    if bar.ts < prev.ts:
        result.add(
            "out_of_order", bar.line_no, bar.ts,
            f"{ts_to_string(bar.ts, bar.sec)} follows {ts_to_string(prev.ts, prev.sec)}",
        )
    elif bar.ts == prev.ts:
        same = (
            bar.open == prev.open
            and bar.high == prev.high
            and bar.low == prev.low
            and bar.close == prev.close
        )
        code = "duplicate_identical" if same else "duplicate_conflicting"
        result.add(code, bar.line_no, bar.ts, ts_to_string(bar.ts, bar.sec))


def _find_spikes(
    result: AuditResult,
    timestamps: array,
    closes: array,
    ranges: array,
    lines: array,
) -> None:
    scale = result.median_range
    if scale <= 0:
        return
    range_limit = scale * result.thresholds.spike_range_mult
    jump_limit = scale * result.thresholds.spike_jump_mult

    for i in range(len(timestamps)):
        if ranges[i] > range_limit:
            result.add(
                "price_spike", lines[i], timestamps[i],
                f"{ts_to_string(timestamps[i])} range={ranges[i]:.6g} "
                f"({ranges[i] / scale:,.0f}x median)",
            )
        if i and timestamps[i] - timestamps[i - 1] == 1:
            move = abs(closes[i] - closes[i - 1])
            if move > jump_limit:
                result.add(
                    "price_jump", lines[i], timestamps[i],
                    f"{ts_to_string(timestamps[i])} close moved {move:.6g} "
                    f"({move / scale:,.0f}x median range) in one minute",
                )


def _find_gaps(result: AuditResult, ordered) -> Tuple[List[Gap], int]:
    mask = result.mask
    gaps: List[Gap] = []
    unique = 0
    prev_ts: Optional[int] = None

    for ts in ordered:
        if prev_ts is None:
            prev_ts, unique = ts, 1
            continue
        if ts == prev_ts:
            continue
        unique += 1
        step = ts - prev_ts
        if step > 1:
            start, end = prev_ts + 1, ts - 1
            session_missing = sum(
                1 for t in range(start, end + 1) if mask.in_session[minute_of_week(t)]
            )
            kind = classify_gap(start, end, session_missing)
            gap = Gap(start, end, session_missing, end - start + 1, kind)
            if session_missing:
                gaps.append(gap)
                result.counts[f"gap_{kind}"] += 1
                if len(gaps) <= result.thresholds.max_examples and kind in ("intraday", "holiday"):
                    result.issues.append(
                        Issue(
                            f"gap_{kind}",
                            _SEVERITY.get(f"gap_{kind}", Severity.WARN),
                            None,
                            start,
                            f"{ts_to_string(start)} -> {ts_to_string(end)}  "
                            f"{session_missing:,} session minute(s) missing",
                        )
                    )
        prev_ts = ts

    return gaps, unique


def _coverage_by_month(result: AuditResult, present: Counter) -> None:
    mask = result.mask
    first, last = result.first_ts, result.last_ts
    expected: Counter = Counter()

    ts = first
    while ts <= last:
        if mask.in_session[minute_of_week(ts)]:
            expected[_month_key(ts)] += 1
        ts += 1

    for key in sorted(set(expected) | set(present)):
        exp = expected.get(key, 0)
        got = present.get(key, 0)
        result.monthly[key] = {
            "bars": got,
            "expected": exp,
            "coverage": (100.0 * got / exp) if exp else 0.0,
        }


def file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
