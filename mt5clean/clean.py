"""Repairs applied to a parsed MT5 series.

Every fix is opt-in and every fix is counted, so the report can say exactly
what was changed rather than handing back a silently rewritten file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Sequence

from .analyze import reverting_spike_indices
from .formats import Bar, copy_record


@dataclass
class CleanResult:
    """The cleaned series plus a tally of what was done to it."""

    records: list
    sorted_rows: int = 0
    dropped_duplicates: int = 0
    dropped_invalid: int = 0
    repaired_ohlc: int = 0
    filled_bars: int = 0
    unfilled_gaps: int = 0
    dropped_spikes: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.sorted_rows
            or self.dropped_duplicates
            or self.dropped_invalid
            or self.repaired_ohlc
            or self.filled_bars
            or self.dropped_spikes
        )

    def summary(self) -> dict[str, int]:
        return {
            "rows_reordered": self.sorted_rows,
            "duplicates_dropped": self.dropped_duplicates,
            "invalid_dropped": self.dropped_invalid,
            "ohlc_repaired": self.repaired_ohlc,
            "bars_filled": self.filled_bars,
            "gaps_left_unfilled": self.unfilled_gaps,
            "spikes_dropped": self.dropped_spikes,
        }


def sort_records(records: Sequence) -> tuple[list, int]:
    """Sort by timestamp, reporting how many rows were out of place."""
    ordered = sorted(records, key=lambda r: (r.ts, r.line))
    moved = sum(1 for a, b in zip(records, ordered) if a is not b)
    return ordered, moved


def dedupe(records: Sequence, keep: str = "last") -> tuple[list, int]:
    """Collapse records sharing a timestamp.

    ``keep`` is ``"last"`` (MT5 re-exports append corrections, so the later row
    is usually the better one), ``"first"``, or ``"drop"`` to remove every copy
    of a conflicting timestamp and leave a gap the fill step can handle.
    """
    if keep == "none":
        return list(records), 0

    by_ts: dict = {}
    order: list = []
    for record in records:
        if record.ts not in by_ts:
            by_ts[record.ts] = [record]
            order.append(record.ts)
        else:
            by_ts[record.ts].append(record)

    out: list = []
    removed = 0
    for ts in order:
        group = by_ts[ts]
        if len(group) == 1:
            out.append(group[0])
            continue
        removed += len(group)
        if keep == "drop":
            distinct = {record.key() for record in group}
            if len(distinct) == 1:
                # Identical rows are not a conflict; keep one.
                out.append(group[0])
                removed -= 1
            continue
        out.append(group[-1] if keep == "last" else group[0])
        removed -= 1
    return out, removed


def repair_ohlc(records: Sequence) -> tuple[list, int]:
    """Widen high/low so they bracket open and close.

    This only fixes bars where the extremes are inconsistent with the open and
    close — the sort of damage a truncated write produces. Bars with
    non-positive prices are left alone; there is nothing trustworthy to repair
    them from, so ``--drop-invalid`` is the right tool for those.
    """
    out: list = []
    repaired = 0
    for bar in records:
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(price <= 0 for price in prices):
            out.append(bar)
            continue
        high = max(prices)
        low = min(prices)
        if high != bar.high or low != bar.low:
            out.append(copy_record(bar, high=high, low=low))
            repaired += 1
        else:
            out.append(bar)
    return out, repaired


def drop_indices(records: Sequence, indices: Sequence[int]) -> tuple[list, int]:
    """Remove records at the given positions."""
    doomed = set(indices)
    out = [record for index, record in enumerate(records) if index not in doomed]
    return out, len(records) - len(out)


def fill_gaps(
    records: Sequence,
    tf_seconds: int,
    gaps: Sequence,
    max_bars: int = 5,
) -> tuple[list, int, int]:
    """Insert placeholder bars across short gaps.

    Filled bars are flat at the previous close with zero volume, which is the
    convention backtesters expect: no invented price movement, no invented
    liquidity. Gaps longer than ``max_bars`` are left alone — a multi-hour hole
    is missing history to be re-downloaded, not something to paper over.
    """
    if not gaps or tf_seconds <= 0:
        return list(records), 0, 0

    by_ts = {record.ts: record for record in records}
    step = timedelta(seconds=tf_seconds)
    additions: list[Bar] = []
    filled = 0
    skipped = 0

    for gap in gaps:
        if gap.missing > max_bars or gap.prev_ts is None:
            skipped += 1
            continue
        anchor = by_ts.get(gap.prev_ts)
        if anchor is None:
            skipped += 1
            continue
        cursor = gap.start
        while cursor <= gap.end:
            additions.append(
                Bar(
                    ts=cursor,
                    open=anchor.close,
                    high=anchor.close,
                    low=anchor.close,
                    close=anchor.close,
                    tickvol=0.0,
                    vol=0.0,
                    spread=anchor.spread,
                    line=-1,
                    synthetic=True,
                )
            )
            filled += 1
            cursor += step

    if not additions:
        return list(records), 0, skipped
    merged = sorted(list(records) + additions, key=lambda r: r.ts)
    return merged, filled, skipped


def clean(
    records: Sequence,
    kind: str,
    analysis,
    do_sort: bool = True,
    dedupe_mode: str = "last",
    drop_invalid: bool = False,
    do_repair_ohlc: bool = False,
    fill_mode: str = "none",
    fill_max_bars: int = 5,
    drop_spikes: bool = False,
    spike_z: float = 12.0,
) -> CleanResult:
    """Apply the requested repairs, in the only order that makes sense.

    Ordering matters: rows must be in time order before duplicates can be
    collapsed, duplicates must be gone before gaps are recomputed, and gap
    filling has to come last so it fills against repaired data.
    """
    result = CleanResult(records=list(records))

    if do_sort:
        result.records, result.sorted_rows = sort_records(result.records)

    if dedupe_mode != "none":
        result.records, result.dropped_duplicates = dedupe(result.records, dedupe_mode)

    if drop_invalid:
        from .analyze import check_ohlc, check_ticks

        checker = check_ohlc if kind == "bars" else check_ticks
        invalid, _ = checker(result.records)
        result.records, result.dropped_invalid = drop_indices(result.records, invalid)

    if do_repair_ohlc and kind == "bars":
        result.records, result.repaired_ohlc = repair_ohlc(result.records)

    if drop_spikes:
        # Only bars whose move reverses immediately are dropped. A one-way jump
        # is real price action, and deleting it would silently rewrite history.
        spikes = reverting_spike_indices(result.records, kind, spike_z)
        result.records, result.dropped_spikes = drop_indices(result.records, spikes)

    if fill_mode == "ffill" and kind == "bars":
        tf_seconds = analysis.timeframe_seconds
        if not tf_seconds:
            result.notes.append("gap fill skipped: timeframe unknown")
        else:
            # Recompute gaps against the repaired series — the earlier analysis
            # was measured on the raw rows, which may have shifted.
            from .analyze import find_gaps

            gaps, _, _ = find_gaps(result.records, tf_seconds)
            result.records, result.filled_bars, result.unfilled_gaps = fill_gaps(
                result.records, tf_seconds, gaps, fill_max_bars
            )
            if result.unfilled_gaps:
                result.notes.append(
                    f"{result.unfilled_gaps} gap(s) longer than {fill_max_bars} bars "
                    "left unfilled — re-download that history instead"
                )
    return result
