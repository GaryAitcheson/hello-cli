"""Rendering of analysis results as text or JSON."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Sequence

from .analyze import Analysis, SEVERITY_ORDER

BULLET = "  - "


def _fmt_duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def _fmt_ts(ts) -> str:
    if not ts:
        return "-"
    if ts.microsecond:
        return ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts.microsecond // 1000:03d}"
    return ts.strftime("%Y-%m-%d %H:%M:%S")


KIND_LABELS = {
    "gap": "Missing bars (gaps)",
    "duplicate_exact": "Duplicate timestamps (identical rows)",
    "duplicate_conflicting": "Duplicate timestamps (different values)",
    "out_of_order": "Rows out of chronological order",
    "invalid_ohlc": "Invalid OHLC relationships",
    "invalid_tick": "Invalid tick quotes",
    "price_spike": "Suspicious price spikes",
    "flat_run": "Frozen-feed runs (no price movement)",
    "zero_volume": "Bars with range but zero tick volume",
    "spread_anomaly": "Abnormal spreads",
    "empty": "Empty file",
}


def render_text(
    path: str,
    analysis: Analysis,
    dialect,
    bad_rows: Sequence,
    clean_result=None,
    out_path: str | None = None,
    max_examples: int = 5,
    max_gaps: int = 15,
) -> str:
    """Render a human-readable report for one file."""
    lines: list[str] = []
    add = lines.append

    add("=" * 72)
    add(f"MT5 data check: {path}")
    add("=" * 72)
    add(f"Format          : {dialect.describe()}")
    add(f"Records         : {analysis.count:,}")
    if bad_rows:
        add(f"Unreadable rows : {len(bad_rows):,}")
    add(f"Range           : {_fmt_ts(analysis.first_ts)} -> {_fmt_ts(analysis.last_ts)}")
    if analysis.timeframe or analysis.timeframe_seconds:
        label = analysis.timeframe or f"{analysis.timeframe_seconds}s (non-standard)"
        suffix = " (inferred)" if analysis.inferred_timeframe else ""
        add(f"Timeframe       : {label}{suffix}")
    if analysis.expected_bars:
        add(
            f"Session coverage: {analysis.session_coverage * 100:.2f}% "
            f"({analysis.count:,} of {analysis.expected_bars:,} expected bars)"
        )

    counts = analysis.counts()
    add("")
    if not counts and not bad_rows:
        add("No problems found. Data looks clean.")
    else:
        add(f"Findings: {analysis.error_count} error(s), {analysis.warning_count} warning(s)")
        add("-" * 72)
        for kind, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            label = KIND_LABELS.get(kind, kind)
            add(f"{label:<44} {count:>8,}")
        if kind_total := analysis.missing_bars:
            add(f"{'Total missing bars':<44} {kind_total:>8,}")

    if bad_rows:
        add("")
        add(f"Unreadable rows ({len(bad_rows)}):")
        for row in bad_rows[:max_examples]:
            add(f"{BULLET}line {row.line}: {row.reason}")
            add(f"      {row.raw[:100]}")
        if len(bad_rows) > max_examples:
            add(f"{BULLET}... and {len(bad_rows) - max_examples} more")

    if analysis.gaps:
        add("")
        add(f"Gaps inside trading hours ({len(analysis.gaps)}):")
        widest = sorted(analysis.gaps, key=lambda g: -g.missing)[:max_gaps]
        for gap in widest:
            add(
                f"{BULLET}{_fmt_ts(gap.start)} -> {_fmt_ts(gap.end)}  "
                f"{gap.missing:,} bar(s), {_fmt_duration(gap.duration)}"
            )
        if len(analysis.gaps) > max_gaps:
            add(f"{BULLET}... and {len(analysis.gaps) - max_gaps} more (largest shown)")

    other = [issue for issue in analysis.issues if issue.kind != "gap"]
    if other:
        add("")
        add("Examples:")
        by_kind: dict[str, list] = {}
        for issue in other:
            by_kind.setdefault(issue.kind, []).append(issue)
        for kind in sorted(by_kind, key=lambda k: SEVERITY_ORDER.get(by_kind[k][0].severity, 3)):
            issues = by_kind[kind]
            add(f"  {KIND_LABELS.get(kind, kind)} ({len(issues)}):")
            for issue in issues[:max_examples]:
                stamp = _fmt_ts(issue.ts)
                add(f"    {stamp}  {issue.message}")
            if len(issues) > max_examples:
                add(f"    ... and {len(issues) - max_examples} more")

    if clean_result is not None:
        add("")
        add("-" * 72)
        if clean_result.changed:
            add("Repairs applied:")
            for label, value in clean_result.summary().items():
                if value:
                    add(f"{BULLET}{label.replace('_', ' ')}: {value:,}")
        else:
            add("Repairs applied: none (nothing matched the enabled fixes)")
        for note in clean_result.notes:
            add(f"{BULLET}note: {note}")
        if out_path:
            add(f"Cleaned file written to: {out_path}")

    return "\n".join(lines)


def render_json(
    path: str,
    analysis: Analysis,
    dialect,
    bad_rows: Sequence,
    clean_result=None,
    out_path: str | None = None,
) -> dict:
    """Render the same information as a JSON-serialisable dict."""
    payload = {
        "file": path,
        "format": {
            "kind": dialect.kind,
            "delimiter": dialect.delimiter,
            "has_header": dialect.has_header,
            "decimal_comma": dialect.decimal_comma,
        },
        "records": analysis.count,
        "unreadable_rows": len(bad_rows),
        "first_timestamp": _fmt_ts(analysis.first_ts) if analysis.first_ts else None,
        "last_timestamp": _fmt_ts(analysis.last_ts) if analysis.last_ts else None,
        "timeframe": analysis.timeframe,
        "timeframe_seconds": analysis.timeframe_seconds,
        "timeframe_inferred": analysis.inferred_timeframe,
        "expected_bars": analysis.expected_bars,
        "session_coverage": round(analysis.session_coverage, 6),
        "errors": analysis.error_count,
        "warnings": analysis.warning_count,
        "issue_counts": analysis.counts(),
        "missing_bars": analysis.missing_bars,
        "gaps": [gap.as_dict() for gap in analysis.gaps],
        "bad_rows": [
            {"line": row.line, "reason": row.reason, "raw": row.raw[:200]}
            for row in bad_rows[:200]
        ],
    }
    if clean_result is not None:
        payload["repairs"] = clean_result.summary()
        payload["notes"] = clean_result.notes
        payload["output_file"] = out_path
    return payload


def dump_json(payloads: Sequence[dict]) -> str:
    body = payloads[0] if len(payloads) == 1 else {"files": list(payloads)}
    return json.dumps(body, indent=2)
