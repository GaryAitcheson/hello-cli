"""Rendering: human-readable text, machine-readable JSON, and a gap CSV."""

from __future__ import annotations

import io
import json
from typing import Dict, List, Optional

from .audit import AuditResult, file_size
from .model import Severity, ts_to_datetime, ts_to_string

_RULE = "-" * 72

_CODE_HELP = {
    "unparseable_row": "rows that could not be read at all",
    "ohlc_invalid": "high/low do not contain open/close, or high < low",
    "non_positive_price": "zero or negative prices",
    "out_of_order": "timestamp goes backwards",
    "duplicate_conflicting": "same timestamp, different prices",
    "duplicate_identical": "same timestamp, identical prices",
    "off_grid": "timestamp is not on a whole minute",
    "gap_intraday": "missing minutes inside the trading session",
    "gap_holiday": "a full session day or more missing (likely a market holiday)",
    "gap_weekend": "expected weekend closure",
    "gap_session_break": "expected daily break or rollover",
    "zero_volume": "bar with no ticks",
    "negative_volume": "negative tick volume",
    "flat_bar": "open == high == low == close",
    "negative_spread": "spread below zero",
    "price_spike": "bar range far above the median",
    "price_jump": "close-to-close move far above the median range",
    "frozen_feed": "long run of identical bars",
    "short_history": "not enough history to infer the session reliably",
}


def _fmt_ts(ts: Optional[int]) -> str:
    return ts_to_string(ts)[:16] if ts is not None else "-"


def render_text(result: AuditResult, examples: int = 5, monthly: bool = True) -> str:
    out = io.StringIO()
    w = out.write

    w(f"{_RULE}\nMT5 data audit: {result.path}\n{_RULE}\n")
    size = file_size(result.path)
    w(f"file size        {size / 1_048_576:,.1f} MiB\n")
    w(f"format           {result.dialect.describe()}\n")
    w(f"price decimals   {result.digits}\n")
    w(f"bars             {result.bars:,} rows, {result.bars_unique:,} unique minutes\n")
    if result.first_ts is not None:
        span_days = (result.last_ts - result.first_ts) / 1440.0
        w(f"range            {_fmt_ts(result.first_ts)}  ->  {_fmt_ts(result.last_ts)}"
          f"   ({span_days:,.0f} days)\n")
        w(f"price range      {result.price_min:.{result.digits}f} .. "
          f"{result.price_max:.{result.digits}f}\n")
        w(f"spread range     {result.spread_min:,.0f} .. {result.spread_max:,.0f} points\n")
        w(f"median bar range {result.median_range:.{result.digits}f}\n")

    if result.mask is not None:
        w(f"\n{_RULE}\nTrading session (inferred from the data)\n{_RULE}\n")
        if result.mask.inferred:
            w(f"derived from {result.mask.weeks_spanned:,} weeks, "
              f"threshold {result.mask.threshold:.0%} of weeks per minute-of-week\n")
        else:
            w("NOT inferred - too little history; assuming Mon-Fri. "
              "Gap classification below is approximate.\n")
        w(f"{result.mask.session_minutes_per_week:,} tradable minutes per week\n")
        for line in result.mask.describe_windows():
            w(f"  {line}\n")
        w("\nA gap is only counted against you when it lands inside these windows.\n")

    w(f"\n{_RULE}\nCoverage\n{_RULE}\n")
    w(f"session minutes expected  {result.expected_session_minutes:>12,}\n")
    w(f"session minutes present   {result.bars_unique:>12,}\n")
    w(f"session minutes missing   {result.session_missing:>12,}\n")
    w(f"coverage                  {result.coverage:>11.3f}%\n")

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

    real_gaps = [g for g in result.gaps if g.kind in ("intraday", "holiday")]
    if real_gaps:
        w(f"\n{_RULE}\nLargest gaps inside the session\n{_RULE}\n")
        w(f"{'start':<18}{'end':<18}{'missing':>10}  kind\n")
        for gap in sorted(real_gaps, key=lambda g: -g.missing)[:20]:
            w(f"{_fmt_ts(gap.start_ts):<18}{_fmt_ts(gap.end_ts):<18}"
              f"{gap.missing:>10,}  {gap.kind}\n")

    if monthly and result.monthly:
        w(f"\n{_RULE}\nMonthly coverage\n{_RULE}\n")
        w(f"{'month':<10}{'bars':>10}{'expected':>11}{'coverage':>10}\n")
        for key in sorted(result.monthly):
            row = result.monthly[key]
            flag = "  <-- thin" if row["coverage"] < 95 else ""
            w(f"{key:<10}{int(row['bars']):>10,}{int(row['expected']):>11,}"
              f"{row['coverage']:>9.2f}%{flag}\n")

    w(f"\n{_RULE}\n")
    w(_verdict(result))
    w(f"\n{_RULE}\n")
    return out.getvalue()


def _severity_of(code: str) -> str:
    from .audit import _SEVERITY

    return _SEVERITY.get(code, Severity.WARN)


def _verdict(result: AuditResult) -> str:
    errors, warns = result.error_count, result.warn_count
    lines = []
    if errors:
        lines.append(f"VERDICT: {errors:,} error-level problem(s) - do not backtest on this as-is.")
    elif warns:
        lines.append(f"VERDICT: usable, with {warns:,} warning(s) worth a look.")
    else:
        lines.append("VERDICT: clean.")

    from .fixes import options_to_flags, suggest_options

    flags = options_to_flags(suggest_options(result))
    if flags:
        lines.append(
            "Suggested repair: mt5clean clean " + " ".join(flags) + " <file> -o <out.csv>"
        )
    return "\n".join(lines) + "\n"


def render_json(result: AuditResult) -> str:
    payload = {
        "file": result.path,
        "bytes": file_size(result.path),
        "format": {
            "delimiter": result.dialect.delimiter,
            "has_header": result.dialect.has_header,
            "columns": result.dialect.columns,
            "date_order": result.dialect.date_order,
            "date_order_ambiguous": result.dialect.date_order_ambiguous,
            "decimal_comma": result.dialect.decimal_comma,
            "encoding": result.dialect.encoding,
        },
        "digits": result.digits,
        "bars": result.bars,
        "unique_minutes": result.bars_unique,
        "first": _fmt_ts(result.first_ts),
        "last": _fmt_ts(result.last_ts),
        "price_min": result.price_min,
        "price_max": result.price_max,
        "median_range": result.median_range,
        "session": {
            "inferred": result.mask.inferred if result.mask else None,
            "weeks": result.mask.weeks_spanned if result.mask else 0,
            "minutes_per_week": result.mask.session_minutes_per_week if result.mask else 0,
            "windows": result.mask.describe_windows() if result.mask else [],
        },
        "coverage": {
            "expected": result.expected_session_minutes,
            "present": result.bars_unique,
            "missing": result.session_missing,
            "percent": round(result.coverage, 4),
        },
        "counts": dict(result.counts),
        "errors": result.error_count,
        "warnings": result.warn_count,
        "gaps": [
            {
                "start": _fmt_ts(g.start_ts),
                "end": _fmt_ts(g.end_ts),
                "missing_session_minutes": g.missing,
                "total_minutes": g.total,
                "kind": g.kind,
            }
            for g in sorted(result.gaps, key=lambda g: -g.missing)[:500]
        ],
        "monthly": result.monthly,
        "examples": [
            {
                "code": i.code,
                "severity": i.severity,
                "line": i.line_no,
                "at": _fmt_ts(i.ts),
                "message": i.message,
            }
            for i in result.issues[:500]
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=False)


def write_gaps_csv(result: AuditResult, path: str) -> int:
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("start,end,missing_session_minutes,total_minutes,kind\n")
        count = 0
        for gap in sorted(result.gaps, key=lambda g: g.start_ts):
            start = ts_to_datetime(gap.start_ts)
            end = ts_to_datetime(gap.end_ts)
            fh.write(
                f"{start:%Y.%m.%d %H:%M},{end:%Y.%m.%d %H:%M},"
                f"{gap.missing},{gap.total},{gap.kind}\n"
            )
            count += 1
    return count
