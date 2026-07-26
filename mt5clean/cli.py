"""Command-line interface for the MT5 data cleaner."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .analyze import TIMEFRAMES, analyze
from .clean import clean
from .formats import ParseError, read_file, write_file
from .report import dump_json, render_json, render_text

EPILOG = """\
examples:
  # Check a file and print a report (never modifies anything)
  mt5clean EURUSD_M1.csv

  # Check, then write a repaired copy with short gaps filled
  mt5clean EURUSD_M1.csv --out EURUSD_M1_clean.csv --fill-gaps ffill --repair-ohlc

  # Batch-clean a folder of exports into a separate directory
  mt5clean data/*.csv --out-dir cleaned/ --drop-invalid --fill-gaps ffill

  # Machine-readable output for a pipeline, failing the build on errors
  mt5clean EURUSD_M1.csv --json report.json --fail-on-error

  # Broker server time is UTC+2; shift it back to UTC
  mt5clean EURUSD_M1.csv --shift-minutes -120 --out EURUSD_M1_utc.csv
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mt5clean",
        description=(
            "Check MetaTrader 5 exports for gaps, duplicates and bad bars, "
            "and optionally write a repaired copy."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("inputs", nargs="+", metavar="FILE", help="exported CSV/TSV file(s)")
    parser.add_argument("--version", action="version", version=f"mt5clean {__version__}")

    read = parser.add_argument_group("reading")
    read.add_argument(
        "--kind",
        choices=("auto", "bars", "ticks"),
        default="auto",
        help="record type (default: auto-detect from the header)",
    )
    read.add_argument(
        "-t",
        "--timeframe",
        default=None,
        metavar="TF",
        help=(
            "bar timeframe (%s); inferred from the data when omitted"
            % ", ".join(TIMEFRAMES)
        ),
    )
    read.add_argument(
        "--shift-minutes",
        type=int,
        default=0,
        metavar="N",
        help="shift every timestamp by N minutes (e.g. -120 for a UTC+2 broker)",
    )

    detect = parser.add_argument_group("detection tuning")
    detect.add_argument(
        "--session-threshold",
        type=float,
        default=0.5,
        metavar="F",
        help=(
            "fraction of a weekday/hour bucket that must be populated for it to "
            "count as trading hours (default: 0.5)"
        ),
    )
    detect.add_argument(
        "--no-sessions",
        action="store_true",
        help="treat every timeframe step as expected, including weekends",
    )
    detect.add_argument(
        "--no-gap-check",
        action="store_true",
        help="skip gap detection entirely",
    )
    detect.add_argument(
        "--spike-z",
        type=float,
        default=12.0,
        metavar="Z",
        help="robust z-score above which a price move is a suspected spike (default: 12)",
    )

    fix = parser.add_argument_group("repairs (all off unless requested)")
    fix.add_argument(
        "-o", "--out", default=None, metavar="PATH", help="write the cleaned file here"
    )
    fix.add_argument(
        "--out-dir",
        default=None,
        metavar="DIR",
        help="write cleaned files into this directory, keeping their names",
    )
    fix.add_argument(
        "--suffix",
        default="_clean",
        help="filename suffix used with --out-dir (default: _clean)",
    )
    fix.add_argument(
        "--no-sort", action="store_true", help="do not reorder rows by timestamp"
    )
    fix.add_argument(
        "--dedupe",
        choices=("last", "first", "drop", "none"),
        default="last",
        help=(
            "how to resolve duplicate timestamps: keep the last row (default), "
            "the first, drop all conflicting copies, or leave them alone"
        ),
    )
    fix.add_argument(
        "--drop-invalid", action="store_true", help="remove bars/ticks that fail validation"
    )
    fix.add_argument(
        "--repair-ohlc",
        action="store_true",
        help="widen high/low so they bracket open and close",
    )
    fix.add_argument(
        "--fill-gaps",
        choices=("none", "ffill"),
        default="none",
        help="fill short gaps with flat zero-volume bars at the previous close",
    )
    fix.add_argument(
        "--fill-max-bars",
        type=int,
        default=5,
        metavar="N",
        help="never fill a gap longer than N bars (default: 5)",
    )
    fix.add_argument(
        "--drop-spikes", action="store_true", help="remove bars flagged as price spikes"
    )
    fix.add_argument(
        "--mark-synthetic",
        action="store_true",
        help="add a <SYNTHETIC> column marking filled bars in the output",
    )
    fix.add_argument(
        "--digits",
        type=int,
        default=None,
        metavar="N",
        help="price decimals in the output (default: inferred from the input)",
    )

    out = parser.add_argument_group("output")
    out.add_argument("--report", default=None, metavar="PATH", help="write the text report here")
    out.add_argument("--json", default=None, metavar="PATH", help="write a JSON report here")
    out.add_argument("-q", "--quiet", action="store_true", help="only print on failure")
    out.add_argument(
        "--max-examples",
        type=int,
        default=5,
        metavar="N",
        help="examples shown per finding type (default: 5)",
    )
    out.add_argument(
        "--fail-on-error",
        action="store_true",
        help="exit non-zero when any error-level finding is present",
    )
    out.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="exit non-zero when any finding at all is present",
    )
    return parser


def _output_path(args, input_path: str) -> str | None:
    if args.out:
        return args.out
    if args.out_dir:
        base = os.path.basename(input_path)
        stem, ext = os.path.splitext(base)
        return os.path.join(args.out_dir, f"{stem}{args.suffix}{ext or '.csv'}")
    return None


def process_file(path: str, args) -> tuple[str, dict, int, int]:
    """Check (and optionally clean) one file.

    Returns ``(text_report, json_payload, error_count, warning_count)``.
    """
    records, bad_rows, dialect = read_file(
        path, kind=args.kind, shift_minutes=args.shift_minutes
    )
    analysis = analyze(
        records,
        dialect.kind,
        timeframe=args.timeframe,
        session_threshold=args.session_threshold,
        spike_z=args.spike_z,
        respect_sessions=not args.no_sessions,
        check_gaps=not args.no_gap_check,
    )

    out_path = _output_path(args, path)
    clean_result = None
    if out_path:
        clean_result = clean(
            records,
            dialect.kind,
            analysis,
            do_sort=not args.no_sort,
            dedupe_mode=args.dedupe,
            drop_invalid=args.drop_invalid,
            do_repair_ohlc=args.repair_ohlc,
            fill_mode=args.fill_gaps,
            fill_max_bars=args.fill_max_bars,
            drop_spikes=args.drop_spikes,
            spike_z=args.spike_z,
        )
        directory = os.path.dirname(os.path.abspath(out_path))
        os.makedirs(directory, exist_ok=True)
        write_file(
            out_path,
            clean_result.records,
            dialect,
            digits=args.digits,
            include_synthetic_flag=args.mark_synthetic,
        )

    text = render_text(
        path,
        analysis,
        dialect,
        bad_rows,
        clean_result=clean_result,
        out_path=out_path,
        max_examples=args.max_examples,
    )
    payload = render_json(
        path, analysis, dialect, bad_rows, clean_result=clean_result, out_path=out_path
    )
    errors = analysis.error_count + len(bad_rows)
    return text, payload, errors, analysis.warning_count


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.out and len(args.inputs) > 1:
        parser.error("--out takes a single input file; use --out-dir for several")
    if args.fill_max_bars < 0:
        parser.error("--fill-max-bars must be >= 0")

    reports: list[str] = []
    payloads: list[dict] = []
    total_errors = 0
    total_warnings = 0
    failed = False

    for path in args.inputs:
        try:
            text, payload, errors, warnings = process_file(path, args)
        except (OSError, ParseError, ValueError) as exc:
            failed = True
            message = f"{path}: {exc}"
            reports.append(f"ERROR  {message}")
            payloads.append({"file": path, "error": str(exc)})
            print(f"mt5clean: {message}", file=sys.stderr)
            continue
        reports.append(text)
        payloads.append(payload)
        total_errors += errors
        total_warnings += warnings

    report_text = "\n\n".join(reports)
    if not args.quiet:
        print(report_text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(report_text + "\n")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(dump_json(payloads) + "\n")

    if failed:
        return 2
    if args.fail_on_error and total_errors:
        return 1
    if args.fail_on_warning and (total_errors or total_warnings):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
