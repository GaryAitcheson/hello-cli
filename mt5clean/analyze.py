"""Quality checks for MT5 bar and tick series.

The interesting part is gap detection. A naive "every timeframe step must have
a bar" check drowns you in false positives: FX is closed at weekends, most
brokers take a daily maintenance break, and every venue has holidays. So
instead of hardcoding session hours, this module *learns* the instrument's
trading calendar from the data — a (weekday, hour) bucket that is populated
across most of the file is a trading session, one that is consistently empty
is closed time. Missing bars are only reported inside buckets that normally
trade.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Sequence

# Timeframes MT5 can export, in seconds.
TIMEFRAMES: dict[str, int] = {
    "M1": 60,
    "M2": 120,
    "M3": 180,
    "M4": 240,
    "M5": 300,
    "M6": 360,
    "M10": 600,
    "M12": 720,
    "M15": 900,
    "M20": 1200,
    "M30": 1800,
    "H1": 3600,
    "H2": 7200,
    "H3": 10800,
    "H4": 14400,
    "H6": 21600,
    "H8": 28800,
    "H12": 43200,
    "D1": 86400,
    "W1": 604800,
}

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass
class Issue:
    """One problem found in the series."""

    kind: str
    severity: str
    message: str
    index: int | None = None
    ts: datetime | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class Gap:
    """A run of missing bars inside active trading time."""

    start: datetime  # timestamp of the first missing bar
    end: datetime  # timestamp of the last missing bar
    missing: int
    prev_ts: datetime | None = None
    next_ts: datetime | None = None

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat(sep=" "),
            "end": self.end.isoformat(sep=" "),
            "missing_bars": self.missing,
            "prev_bar": self.prev_ts.isoformat(sep=" ") if self.prev_ts else None,
            "next_bar": self.next_ts.isoformat(sep=" ") if self.next_ts else None,
        }


@dataclass
class Analysis:
    """Everything the checks found."""

    kind: str
    count: int = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    timeframe: str | None = None
    timeframe_seconds: int | None = None
    inferred_timeframe: bool = False
    issues: list[Issue] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    duplicate_indices: list[int] = field(default_factory=list)
    conflicting_duplicates: int = 0
    unordered_indices: list[int] = field(default_factory=list)
    invalid_indices: list[int] = field(default_factory=list)
    spike_indices: list[int] = field(default_factory=list)
    expected_bars: int = 0
    session_coverage: float = 0.0

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for issue in self.issues:
            out[issue.kind] = out.get(issue.kind, 0) + 1
        return out

    @property
    def missing_bars(self) -> int:
        return sum(gap.missing for gap in self.gaps)

    @property
    def error_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "warning")


# --------------------------------------------------------------------------- #
# Timeframe
# --------------------------------------------------------------------------- #


def infer_timeframe(records: Sequence) -> tuple[str | None, int | None]:
    """Infer the bar timeframe from the most common gap between bars.

    Returns ``(name, seconds)``. The name is ``None`` when the modal spacing
    does not match a known MT5 timeframe (custom or irregular data), in which
    case the raw modal spacing is still returned.
    """
    if len(records) < 3:
        return None, None
    deltas: dict[int, int] = {}
    for prev, cur in zip(records, records[1:]):
        seconds = int((cur.ts - prev.ts).total_seconds())
        if seconds > 0:
            deltas[seconds] = deltas.get(seconds, 0) + 1
    if not deltas:
        return None, None
    modal = max(deltas, key=lambda s: deltas[s])
    for name, seconds in TIMEFRAMES.items():
        if seconds == modal:
            return name, seconds
    return None, modal


def timeframe_seconds(name: str) -> int:
    """Look up a timeframe name (case-insensitive), raising on unknown names."""
    key = name.strip().upper()
    if key not in TIMEFRAMES:
        raise ValueError(
            f"unknown timeframe {name!r}; expected one of {', '.join(TIMEFRAMES)}"
        )
    return TIMEFRAMES[key]


# --------------------------------------------------------------------------- #
# Session profiling and gaps
# --------------------------------------------------------------------------- #


def _bucket(ts: datetime, tf_seconds: int):
    if tf_seconds >= 86400:
        return ts.weekday()
    return (ts.weekday(), ts.hour)


def build_session_profile(
    timestamps: Sequence[datetime], tf_seconds: int, threshold: float = 0.5
) -> set:
    """Return the set of time buckets in which this instrument normally trades.

    A bucket is active when at least ``threshold`` of the bars that *could*
    exist in it actually do. Buckets seen only once or twice fall back to
    "active if we ever saw a bar there", so short files still work.
    """
    if not timestamps:
        return set()

    observed: dict = {}
    for ts in timestamps:
        key = _bucket(ts, tf_seconds)
        observed[key] = observed.get(key, 0) + 1

    expected: dict = {}
    step = timedelta(seconds=tf_seconds)
    cursor = timestamps[0]
    end = timestamps[-1]
    # Guard against pathological spans; 5M steps covers ~9 years of M1.
    limit = 5_000_000
    steps = 0
    while cursor <= end and steps < limit:
        key = _bucket(cursor, tf_seconds)
        expected[key] = expected.get(key, 0) + 1
        cursor += step
        steps += 1

    active = set()
    for key, total in expected.items():
        seen = observed.get(key, 0)
        if total <= 2:
            if seen > 0:
                active.add(key)
        elif seen / total >= threshold:
            active.add(key)
    return active


def find_gaps(
    records: Sequence,
    tf_seconds: int,
    session_threshold: float = 0.5,
    respect_sessions: bool = True,
) -> tuple[list[Gap], int, float]:
    """Find runs of missing bars.

    Returns ``(gaps, expected_bars, session_coverage)`` where ``expected_bars``
    counts the bars the active session profile says should exist and
    ``session_coverage`` is the fraction of those actually present.
    """
    if len(records) < 2 or tf_seconds <= 0:
        return [], len(records), 1.0

    timestamps = [record.ts for record in records]
    present = set(timestamps)
    active = (
        build_session_profile(timestamps, tf_seconds, session_threshold)
        if respect_sessions
        else None
    )

    step = timedelta(seconds=tf_seconds)
    gaps: list[Gap] = []
    expected_bars = 0
    run: list[datetime] = []
    last_present: datetime | None = None

    cursor = timestamps[0]
    end = timestamps[-1]
    limit = 5_000_000
    steps = 0
    while cursor <= end and steps < limit:
        in_session = active is None or _bucket(cursor, tf_seconds) in active
        if in_session:
            expected_bars += 1
            if cursor in present:
                if run:
                    gaps.append(
                        Gap(
                            start=run[0],
                            end=run[-1],
                            missing=len(run),
                            prev_ts=last_present,
                            next_ts=cursor,
                        )
                    )
                    run = []
                last_present = cursor
            else:
                run.append(cursor)
        elif cursor in present:
            # A bar outside the learned session — unusual but present, so it
            # still anchors the run.
            if run:
                gaps.append(
                    Gap(
                        start=run[0],
                        end=run[-1],
                        missing=len(run),
                        prev_ts=last_present,
                        next_ts=cursor,
                    )
                )
                run = []
            last_present = cursor
        cursor += step
        steps += 1

    if run:
        gaps.append(
            Gap(
                start=run[0], end=run[-1], missing=len(run), prev_ts=last_present, next_ts=None
            )
        )

    observed_in_session = 0
    if active is None:
        observed_in_session = len(present)
    else:
        observed_in_session = sum(
            1 for ts in timestamps if _bucket(ts, tf_seconds) in active
        )
    coverage = observed_in_session / expected_bars if expected_bars else 1.0
    return gaps, expected_bars, min(coverage, 1.0)


# --------------------------------------------------------------------------- #
# Integrity checks
# --------------------------------------------------------------------------- #


def check_ohlc(records: Sequence) -> tuple[list[int], list[Issue]]:
    """Flag bars whose OHLC values are internally inconsistent."""
    invalid: list[int] = []
    issues: list[Issue] = []
    for index, bar in enumerate(records):
        problems = []
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(price <= 0 for price in prices):
            problems.append("non-positive price")
        if bar.high < bar.low:
            problems.append(f"high {bar.high} < low {bar.low}")
        if bar.high < max(bar.open, bar.close):
            problems.append("high below open/close")
        if bar.low > min(bar.open, bar.close):
            problems.append("low above open/close")
        if bar.spread < 0:
            problems.append("negative spread")
        if problems:
            invalid.append(index)
            issues.append(
                Issue(
                    kind="invalid_ohlc",
                    severity="error",
                    message="; ".join(problems),
                    index=index,
                    ts=bar.ts,
                    detail={
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                    },
                )
            )
    return invalid, issues


def check_ticks(records: Sequence) -> tuple[list[int], list[Issue]]:
    """Flag ticks with crossed or non-positive quotes."""
    invalid: list[int] = []
    issues: list[Issue] = []
    for index, tick in enumerate(records):
        problems = []
        if tick.bid <= 0 and tick.ask <= 0 and tick.last <= 0:
            problems.append("no positive price on tick")
        if tick.bid > 0 and tick.ask > 0 and tick.ask < tick.bid:
            problems.append(f"crossed quote: ask {tick.ask} < bid {tick.bid}")
        if problems:
            invalid.append(index)
            issues.append(
                Issue(
                    kind="invalid_tick",
                    severity="error",
                    message="; ".join(problems),
                    index=index,
                    ts=tick.ts,
                    detail={"bid": tick.bid, "ask": tick.ask, "last": tick.last},
                )
            )
    return invalid, issues


def find_duplicates(records: Sequence) -> tuple[list[int], int, list[Issue]]:
    """Find records sharing a timestamp with an earlier record.

    Returns ``(duplicate_indices, conflicting_count, issues)``. A duplicate is
    "conflicting" when the values differ from the record it duplicates — that
    is a genuine data problem rather than a harmless repeated row.
    """
    seen: dict[datetime, int] = {}
    duplicates: list[int] = []
    conflicting = 0
    issues: list[Issue] = []
    for index, record in enumerate(records):
        first = seen.get(record.ts)
        if first is None:
            seen[record.ts] = index
            continue
        duplicates.append(index)
        differs = records[first].key() != record.key()
        if differs:
            conflicting += 1
        issues.append(
            Issue(
                kind="duplicate_conflicting" if differs else "duplicate_exact",
                severity="error" if differs else "warning",
                message=(
                    "duplicate timestamp with different values"
                    if differs
                    else "duplicate timestamp, identical values"
                ),
                index=index,
                ts=record.ts,
                detail={"first_index": first},
            )
        )
    return duplicates, conflicting, issues


def find_unordered(records: Sequence) -> tuple[list[int], list[Issue]]:
    """Find records whose timestamp goes backwards relative to the previous one."""
    unordered: list[int] = []
    issues: list[Issue] = []
    for index in range(1, len(records)):
        if records[index].ts < records[index - 1].ts:
            unordered.append(index)
            issues.append(
                Issue(
                    kind="out_of_order",
                    severity="error",
                    message=(
                        f"timestamp {records[index].ts} precedes "
                        f"previous {records[index - 1].ts}"
                    ),
                    index=index,
                    ts=records[index].ts,
                )
            )
    return unordered, issues


def find_spikes(
    records: Sequence, kind: str, z_threshold: float = 12.0
) -> tuple[list[int], list[Issue]]:
    """Flag implausible price jumps using a robust (median/MAD) z-score.

    Bad ticks in broker history usually show up as a single bar whose return is
    orders of magnitude larger than the instrument's normal move, followed by an
    immediate return to the previous level. Median absolute deviation is used
    rather than standard deviation because the outliers we are hunting would
    otherwise inflate the very threshold meant to catch them.
    """
    price_of = (lambda r: r.close) if kind == "bars" else (lambda r: r.bid or r.last)
    prices = [price_of(record) for record in records]
    returns: list[tuple[int, float]] = []
    for index in range(1, len(prices)):
        prev, cur = prices[index - 1], prices[index]
        if prev > 0 and cur > 0:
            returns.append((index, (cur - prev) / prev))
    if len(returns) < 30:
        return [], []

    values = [value for _, value in returns]
    median = statistics.median(values)
    deviations = [abs(value - median) for value in values]
    mad = statistics.median(deviations)
    if mad <= 0:
        return [], []
    scale = 1.4826 * mad

    candidates: dict[int, tuple[float, float]] = {}
    for index, value in returns:
        score = abs(value - median) / scale
        if score >= z_threshold:
            candidates[index] = (value, score)

    # One bad print produces two extreme returns: the jump away and the jump
    # back. Only the first bar is corrupt — the second is the price returning
    # to where it always was — so pair them up and blame the bar that moved.
    # A large move with no matching reversal is a real repricing (a weekend
    # open, a rate decision) and is reported but never treated as droppable.
    spikes: list[int] = []
    issues: list[Issue] = []
    consumed: set[int] = set()
    for index in sorted(candidates):
        if index in consumed:
            continue
        value, score = candidates[index]
        follower = candidates.get(index + 1)
        reverts = False
        if follower is not None:
            next_value = follower[0]
            opposite = value * next_value < 0
            comparable = 0.5 <= abs(next_value) / abs(value) <= 2.0 if value else False
            if opposite and comparable:
                reverts = True
                consumed.add(index + 1)
        spikes.append(index)
        issues.append(
            Issue(
                kind="price_spike",
                severity="warning",
                message=(
                    f"{value * 100:+.3f}% move is {score:.0f}x the typical bar move"
                    + (
                        " and reverses on the next bar (isolated bad print)"
                        if reverts
                        else " with no reversal (level shift, may be genuine)"
                    )
                ),
                index=index,
                ts=records[index].ts,
                detail={"return": value, "z": score, "reverts": reverts},
            )
        )
    return spikes, issues


def reverting_spike_indices(
    records: Sequence, kind: str, z_threshold: float = 12.0
) -> list[int]:
    """Indices of spikes that immediately revert — the safely droppable ones."""
    _, issues = find_spikes(records, kind, z_threshold)
    return [
        issue.index
        for issue in issues
        if issue.detail.get("reverts") and issue.index is not None
    ]


def find_flat_runs(records: Sequence, min_run: int = 10) -> list[Issue]:
    """Flag long runs of identical OHLC bars — usually a frozen or dead feed."""
    issues: list[Issue] = []
    run_start = 0
    for index in range(1, len(records) + 1):
        same = (
            index < len(records)
            and records[index].open
            == records[index].high
            == records[index].low
            == records[index].close
            and records[index].close == records[run_start].close
        )
        if not same:
            length = index - run_start
            if length >= min_run:
                flat = (
                    records[run_start].open
                    == records[run_start].high
                    == records[run_start].low
                    == records[run_start].close
                )
                if flat:
                    issues.append(
                        Issue(
                            kind="flat_run",
                            severity="warning",
                            message=(
                                f"{length} consecutive bars with no price movement "
                                f"at {records[run_start].close}"
                            ),
                            index=run_start,
                            ts=records[run_start].ts,
                            detail={"length": length},
                        )
                    )
            run_start = index
    return issues


def find_zero_volume(records: Sequence, max_report: int = 50) -> list[Issue]:
    """Flag bars that moved in price but recorded no ticks."""
    issues: list[Issue] = []
    for index, bar in enumerate(records):
        if bar.tickvol <= 0 and bar.high != bar.low:
            issues.append(
                Issue(
                    kind="zero_volume",
                    severity="warning",
                    message="bar has price range but zero tick volume",
                    index=index,
                    ts=bar.ts,
                )
            )
            if len(issues) >= max_report:
                break
    return issues


def find_spread_anomalies(records: Sequence, multiple: float = 20.0) -> list[Issue]:
    """Flag bars whose recorded spread dwarfs the instrument's typical spread."""
    spreads = [bar.spread for bar in records if bar.spread > 0]
    if len(spreads) < 30:
        return []
    median = statistics.median(spreads)
    if median <= 0:
        return []
    threshold = median * multiple
    issues: list[Issue] = []
    for index, bar in enumerate(records):
        if bar.spread > threshold:
            issues.append(
                Issue(
                    kind="spread_anomaly",
                    severity="warning",
                    message=(
                        f"spread {bar.spread:g} is {bar.spread / median:.0f}x the "
                        f"median of {median:g}"
                    ),
                    index=index,
                    ts=bar.ts,
                    detail={"spread": bar.spread, "median": median},
                )
            )
    return issues


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def analyze(
    records: Sequence,
    kind: str,
    timeframe: str | None = None,
    session_threshold: float = 0.5,
    spike_z: float = 12.0,
    respect_sessions: bool = True,
    check_gaps: bool = True,
) -> Analysis:
    """Run every applicable check over ``records``."""
    result = Analysis(kind=kind, count=len(records))
    if not records:
        result.add(Issue("empty", "error", "no parseable records in file"))
        return result

    ordered_ts = sorted(record.ts for record in records)
    result.first_ts, result.last_ts = ordered_ts[0], ordered_ts[-1]

    result.unordered_indices, issues = find_unordered(records)
    result.issues.extend(issues)

    result.duplicate_indices, result.conflicting_duplicates, issues = find_duplicates(
        records
    )
    result.issues.extend(issues)

    if kind == "bars":
        result.invalid_indices, issues = check_ohlc(records)
        result.issues.extend(issues)
        result.issues.extend(find_zero_volume(records))
        result.issues.extend(find_flat_runs(records))
        result.issues.extend(find_spread_anomalies(records))
    else:
        result.invalid_indices, issues = check_ticks(records)
        result.issues.extend(issues)

    result.spike_indices, issues = find_spikes(records, kind, spike_z)
    result.issues.extend(issues)

    if kind == "bars" and check_gaps:
        if timeframe:
            result.timeframe = timeframe.strip().upper()
            result.timeframe_seconds = timeframe_seconds(timeframe)
        else:
            name, seconds = infer_timeframe(sorted(records, key=lambda r: r.ts))
            result.timeframe, result.timeframe_seconds = name, seconds
            result.inferred_timeframe = True
        if result.timeframe_seconds:
            clean = sorted(records, key=lambda r: r.ts)
            deduped = []
            seen = set()
            for record in clean:
                if record.ts not in seen:
                    seen.add(record.ts)
                    deduped.append(record)
            result.gaps, result.expected_bars, result.session_coverage = find_gaps(
                deduped,
                result.timeframe_seconds,
                session_threshold,
                respect_sessions=respect_sessions,
            )
            for gap in result.gaps:
                result.add(
                    Issue(
                        kind="gap",
                        severity="error" if gap.missing > 1 else "warning",
                        message=(
                            f"{gap.missing} missing bar(s) from {gap.start} to {gap.end}"
                        ),
                        ts=gap.start,
                        detail=gap.as_dict(),
                    )
                )
    return result
