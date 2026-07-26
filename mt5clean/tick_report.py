"""Rendering for tick audits. Mirrors report.py's shape for bars."""

from __future__ import annotations

import io
import json
from typing import Dict, List, Optional

from .audit import file_size
from .model import Severity, ts_to_string
from .tick_audit import TickAuditResult, _TICK_SEVERITY

_RULE = "-" * 72

_CODE_HELP = {
    "unparseable_row": "rows that could not be read at all",
    "non_positive_quote": "no usable positive price on the tick",
    "crossed_quote": "ask below bid",
    "out_of_order": "timestamp goes backwards",
    "duplicate_conflicting": "same timestamp, different quotes",
    "duplicate_identical": "same timestamp, identical quotes",
    "wide_spread": "spread far above the median",
    "silence": "no ticks for far longer than usual, during trading hours",
    "short_history": "not enough history to infer the session reliably",
}


def _severity_of(code: str) -> str:
    return _TICK_SEVERITY.get(code, Severity.WARN)


def render_text(result: TickAuditResult, examples: int = 5) -> str:
    out = io.StringIO()
    w = out.write

    w(f"{_RULE}\nMT5 tick audit: {result.path}\n{_RULE}\n")
    size = file_size(result.path)
    w(f"file size        {size / 1_048_576:,.1f} MiB\n")
    w(f"format           {result.dialect.describe()}\n")
    w(f"price decimals   {result.digits}\n")
    w(f"ticks            {result.ticks:,}\n")
    if result.first_ts is not None:
        span_days = (result.last_ts - result.first_ts) / 1440.0
        w(f"range            {ts_to_string(result.first_ts)[:16]}  ->  "
          f"{ts_to_string(result.last_ts)[:16]}   ({span_days:,.0f} days)\n")
        w(f"median spread    {result.median_spread:.{result.digits}f}\n")
        if result.median_gap_ms:
            w(f"median inter-tick gap  {result.median_gap_ms:,.0f} ms\n")

    if result.mask is not None:
        w(f"\n{_RULE}\nTrading session (inferred from the data)\n{_RULE}\n")
        if result.mask.inferred:
            w(f"derived from {result.mask.weeks_spanned:,} weeks, "
              f"threshold {result.mask.threshold:.0%} of weeks per minute-of-week\n")
        else:
            w("NOT inferred - too little history; assuming Mon-Fri.\n")
        for line in result.mask.describe_windows():
            w(f"  {line}\n")

    w(f"\n{_RULE}\nFindings\n{_RULE}\n")
    if not result.counts:
        w("No issues found.\n")
    else:
        for code, count in sorted(
            result.counts.items(),
            key=lambda kv: (-Severity.rank(_severity_of(kv[0])), -kv[1]),
        ):
            level = _severity_of(code).upper()
            w(f"  [{level:<5}] {code:<22} {count:>10,}   {_CODE_HELP.get(code, '')}\n")

    if examples and result.issues:
        w(f"\n{_RULE}\nExamples\n{_RULE}\n")
        by_code: Dict[str, List] = {}
        for issue in result.issues:
            by_code.setdefault(issue.code, []).append(issue)
        for code in sorted(by_code, key=lambda c: -Severity.rank(_severity_of(c))):
            shown = by_code[code][:examples]
            w(f"\n{code}  ({result.counts.get(code, len(by_code[code])):,} total)\n")
            for issue in shown:
                where = f"line {issue.line_no}" if issue.line_no else "-"
                w(f"  {where:<12} {issue.message}\n")
            if result.counts.get(code, 0) > len(shown):
                w(f"  ... {result.counts[code] - len(shown):,} more\n")

    w(f"\n{_RULE}\n")
    w(_verdict(result))
    w(f"\n{_RULE}\n")
    return out.getvalue()


def _verdict(result: TickAuditResult) -> str:
    errors, warns = result.error_count, result.warn_count
    if errors:
        return f"VERDICT: {errors:,} error-level problem(s) - do not backtest on this as-is.\n"
    if warns:
        return f"VERDICT: usable, with {warns:,} warning(s) worth a look.\n"
    return "VERDICT: clean.\n"


def render_json(result: TickAuditResult) -> str:
    payload = {
        "file": result.path,
        "bytes": file_size(result.path),
        "kind": "ticks",
        "format": {
            "delimiter": result.dialect.delimiter,
            "has_header": result.dialect.has_header,
            "columns": result.dialect.columns,
        },
        "digits": result.digits,
        "ticks": result.ticks,
        "first": ts_to_string(result.first_ts)[:16] if result.first_ts is not None else None,
        "last": ts_to_string(result.last_ts)[:16] if result.last_ts is not None else None,
        "median_spread": result.median_spread,
        "median_gap_ms": result.median_gap_ms,
        "session": {
            "inferred": result.mask.inferred if result.mask else None,
            "weeks": result.mask.weeks_spanned if result.mask else 0,
            "windows": result.mask.describe_windows() if result.mask else [],
        },
        "counts": dict(result.counts),
        "errors": result.error_count,
        "warnings": result.warn_count,
        "examples": [
            {
                "code": i.code,
                "severity": i.severity,
                "line": i.line_no,
                "at": ts_to_string(i.ts)[:16] if i.ts is not None else None,
                "message": i.message,
            }
            for i in result.issues[:500]
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=False)
