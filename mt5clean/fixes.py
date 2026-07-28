"""Opt-in repairs.

Nothing here runs unless you ask for it. The default `audit` command never
writes a file, because silently rewriting price history is how backtests end up
lying to you. Every repair that does run is counted and reported.

The pipeline streams, so memory stays flat - except when `--sort` is needed to
rescue an out-of-order export, which buffers the file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, List, Optional

from .model import Bar, minute_of_week, ts_to_string
from .reader import Dialect, read_bars
from .sessions import SessionMask


@dataclass
class CleanOptions:
    duplicates: str = "none"          # none | first | last
    sort: bool = False
    fix_ohlc: bool = False
    drop_invalid: bool = False
    off_grid: str = "keep"            # keep | snap | drop
    fill_gaps: bool = False
    max_fill: int = 60                # longest run of bars we will invent
    min_fill: int = 1                 # shortest run worth filling; below this a
                                      # hole is a bar nobody traded, not lost data
    drop_spikes: bool = False
    spike_limit: float = 0.0          # absolute range limit, set from the audit
    session_only: bool = False
    tz_shift: int = 0                 # minutes added to every stamp
    from_ts: Optional[int] = None
    to_ts: Optional[int] = None

    @property
    def any_enabled(self) -> bool:
        return any(
            [
                self.duplicates != "none",
                self.sort,
                self.fix_ohlc,
                self.drop_invalid,
                self.off_grid != "keep",
                self.fill_gaps,
                self.drop_spikes,
                self.session_only,
                self.tz_shift,
                self.from_ts is not None,
                self.to_ts is not None,
            ]
        )


@dataclass
class CleanStats:
    read: int = 0
    written: int = 0
    dropped_duplicate: int = 0
    dropped_invalid: int = 0
    dropped_off_grid: int = 0
    dropped_spike: int = 0
    dropped_out_of_session: int = 0
    dropped_out_of_range: int = 0
    snapped_off_grid: int = 0
    repaired_ohlc: int = 0
    filled_bars: int = 0
    filled_runs: int = 0
    unfilled_runs: int = 0
    unfilled_short_runs: int = 0
    notes: List[str] = field(default_factory=list)

    def summary(self) -> List[str]:
        rows = [
            ("bars read", self.read),
            ("bars written", self.written),
            ("duplicates dropped", self.dropped_duplicate),
            ("invalid bars dropped", self.dropped_invalid),
            ("off-grid bars dropped", self.dropped_off_grid),
            ("off-grid bars snapped", self.snapped_off_grid),
            ("spikes dropped", self.dropped_spike),
            ("out-of-session bars dropped", self.dropped_out_of_session),
            ("bars outside date range dropped", self.dropped_out_of_range),
            ("OHLC bars repaired", self.repaired_ohlc),
            ("synthetic bars inserted", self.filled_bars),
            ("gap runs filled", self.filled_runs),
            ("gap runs left alone (too long)", self.unfilled_runs),
            ("gap runs left alone (too short)", self.unfilled_short_runs),
        ]
        always = {"bars read", "bars written"}
        return [
            f"{label:<34} {value:>12,}"
            for label, value in rows
            if value or label in always
        ]


def _is_invalid(bar: Bar) -> bool:
    o, h, l, c = bar.open, bar.high, bar.low, bar.close
    if min(o, h, l, c) <= 0:
        return True
    return h < l or h < max(o, c) or l > min(o, c)


def _repair_ohlc(bar: Bar) -> Bar:
    high = max(bar.open, bar.high, bar.low, bar.close)
    low = min(bar.open, bar.high, bar.low, bar.close)
    return bar._replace(high=high, low=low)


def clean(
    path: str,
    dialect: Dialect,
    options: CleanOptions,
    mask: Optional[SessionMask],
    stats: CleanStats,
) -> Iterator[Bar]:
    """Yield repaired bars according to `options`."""
    source = read_bars(path, dialect)
    if options.sort:
        buffered = list(source)
        buffered.sort(key=lambda b: b.ts)
        stats.notes.append(f"buffered and sorted {len(buffered):,} bars in memory")
        source = iter(buffered)

    pending: Optional[Bar] = None   # held back when de-duplicating with "last"
    last_emitted: Optional[Bar] = None

    def emit(bar: Bar) -> Iterator[Bar]:
        nonlocal last_emitted
        if options.fill_gaps and last_emitted is not None:
            yield from _fill(last_emitted, bar, options, mask, stats)
        last_emitted = bar
        stats.written += 1
        yield bar

    for bar in source:
        stats.read += 1

        if options.tz_shift:
            bar = bar._replace(ts=bar.ts + options.tz_shift)

        if options.from_ts is not None and bar.ts < options.from_ts:
            stats.dropped_out_of_range += 1
            continue
        if options.to_ts is not None and bar.ts > options.to_ts:
            stats.dropped_out_of_range += 1
            continue

        if bar.sec:
            if options.off_grid == "drop":
                stats.dropped_off_grid += 1
                continue
            if options.off_grid == "snap":
                stats.snapped_off_grid += 1
                bar = bar._replace(sec=0)

        if _is_invalid(bar):
            if options.drop_invalid:
                stats.dropped_invalid += 1
                continue
            if options.fix_ohlc and min(bar.open, bar.high, bar.low, bar.close) > 0:
                bar = _repair_ohlc(bar)
                stats.repaired_ohlc += 1

        if options.drop_spikes and options.spike_limit > 0:
            if bar.high - bar.low > options.spike_limit:
                stats.dropped_spike += 1
                continue

        if options.session_only and mask is not None:
            if not mask.in_session[minute_of_week(bar.ts)]:
                stats.dropped_out_of_session += 1
                continue

        if options.duplicates == "none":
            yield from emit(bar)
            continue

        if pending is None:
            pending = bar
            continue
        if bar.ts == pending.ts:
            stats.dropped_duplicate += 1
            if options.duplicates == "last":
                pending = bar
            continue
        yield from emit(pending)
        pending = bar

    if pending is not None:
        yield from emit(pending)


def _fill(
    prev: Bar,
    nxt: Bar,
    options: CleanOptions,
    mask: Optional[SessionMask],
    stats: CleanStats,
) -> Iterator[Bar]:
    """Insert flat bars across a gap, but only inside the trading session."""
    step = nxt.ts - prev.ts
    if step <= 1:
        return

    candidates = [
        ts
        for ts in range(prev.ts + 1, nxt.ts)
        if mask is None or mask.in_session[minute_of_week(ts)]
    ]
    if not candidates:
        return
    if len(candidates) > options.max_fill:
        stats.unfilled_runs += 1
        return
    if len(candidates) < options.min_fill:
        stats.unfilled_short_runs += 1
        return

    stats.filled_runs += 1
    price = prev.close
    for ts in candidates:
        stats.filled_bars += 1
        stats.written += 1
        yield Bar(
            ts=ts,
            sec=0,
            open=price,
            high=price,
            low=price,
            close=price,
            tick_volume=0.0,
            real_volume=0.0,
            spread=prev.spread,
            line_no=-1,
        )


def suggest_options(result) -> CleanOptions:
    """Pick the repairs an audit's findings actually call for.

    Shared by the CLI's "suggested repair" line and the GUI's pre-ticked
    checkboxes, so the two can never recommend different things.
    """
    counts = result.counts
    options = CleanOptions()
    if counts.get("duplicate_conflicting") or counts.get("duplicate_identical"):
        options.duplicates = "last"
    if counts.get("out_of_order"):
        options.sort = True
    if counts.get("ohlc_invalid"):
        options.fix_ohlc = True
    if counts.get("off_grid"):
        options.off_grid = "snap"
    if counts.get("gap_intraday"):
        options.fill_gaps = True
    if counts.get("price_spike"):
        options.drop_spikes = True
    return options


def options_to_flags(options: CleanOptions) -> List[str]:
    """Render options as the CLI flags that would reproduce them."""
    flags: List[str] = []
    if options.duplicates != "none":
        flags += ["--dedupe", options.duplicates]
    if options.sort:
        flags.append("--sort")
    if options.drop_invalid:
        flags.append("--drop-invalid")
    elif options.fix_ohlc:
        flags.append("--fix-ohlc")
    if options.off_grid != "keep":
        flags += ["--off-grid", options.off_grid]
    if options.fill_gaps:
        flags.append("--fill-gaps")
        if options.max_fill != 60:
            flags += ["--max-fill", str(options.max_fill)]
        if options.min_fill != 1:
            flags += ["--min-fill", str(options.min_fill)]
    if options.drop_spikes:
        flags.append("--drop-spikes")
    if options.session_only:
        flags.append("--session-only")
    if options.tz_shift:
        flags += ["--tz-shift", str(options.tz_shift)]
    return flags


def describe_plan(options: CleanOptions) -> List[str]:
    """Human-readable list of what the current options will actually do."""
    plan = []
    if options.tz_shift:
        plan.append(f"shift every timestamp by {options.tz_shift:+d} minutes")
    if options.from_ts is not None or options.to_ts is not None:
        lo = ts_to_string(options.from_ts) if options.from_ts is not None else "start"
        hi = ts_to_string(options.to_ts) if options.to_ts is not None else "end"
        plan.append(f"keep bars from {lo} to {hi}")
    if options.sort:
        plan.append("sort by timestamp (buffers the file in memory)")
    if options.duplicates != "none":
        plan.append(f"collapse duplicate timestamps, keeping the {options.duplicates}")
    if options.off_grid != "keep":
        plan.append(f"{options.off_grid} bars that are not on a whole minute")
    if options.drop_invalid:
        plan.append("drop bars that violate OHLC ordering")
    elif options.fix_ohlc:
        plan.append("clamp high/low so they contain open and close")
    if options.drop_spikes:
        plan.append("drop bars whose range exceeds the spike threshold")
    if options.session_only:
        plan.append("drop bars outside the detected trading session")
    if options.fill_gaps:
        span = (f"of {options.min_fill} to {options.max_fill} bars"
                if options.min_fill > 1 else f"of up to {options.max_fill} bars")
        plan.append(f"fill in-session gaps {span} with flat bars (volume 0)")
    return plan or ["no repairs enabled - output will match the input"]
