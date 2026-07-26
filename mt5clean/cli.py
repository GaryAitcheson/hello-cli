"""Command line interface.

    mt5clean info   FILE                 what format is this file?
    mt5clean audit  FILE                 what is wrong with it?
    mt5clean clean  FILE -o OUT.csv      fix the things you asked for
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
from datetime import datetime
from typing import List, Optional

from .audit import Thresholds, audit
from .fixes import CleanOptions, CleanStats, clean, describe_plan
from .model import EPOCH_ORD
from .reader import SniffError, sniff
from .timeframe import parse_timeframe
from .report import render_json, render_text, write_gaps_csv
from .writers import FORMATS, BarWriter, import_hint, open_output

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def _parse_date(text: str) -> int:
    """Accept YYYY-MM-DD, YYYY.MM.DD, optionally with HH:MM. Returns minutes."""
    cleaned = text.strip().replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        return (dt.date().toordinal() - EPOCH_ORD) * 1440 + dt.hour * 60 + dt.minute
    raise argparse.ArgumentTypeError(f"unrecognised date {text!r}; use YYYY-MM-DD")


def _parse_shift(text: str) -> int:
    """Accept '120', '-120', '2h', '-3h' and return minutes."""
    match = re.fullmatch(r"\s*([+-]?\d+(?:\.\d+)?)\s*([hm]?)\s*", text, re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError(f"unrecognised offset {text!r}; try '-3h' or '-180'")
    value, unit = float(match.group(1)), match.group(2).lower()
    return int(round(value * 60)) if unit == "h" else int(round(value))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mt5clean",
        description="Audit and repair MetaTrader 5 exports (bars, any timeframe, and ticks) before you backtest on them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  mt5clean info EURUSD_M1.csv\n"
            "  mt5clean audit EURUSD_M1.csv --gaps-csv gaps.csv\n"
            "  mt5clean clean EURUSD_M1.csv -o EURUSD_sqx.csv --dedupe last --fix-ohlc --fill-gaps\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("file", help="MT5 export to read")
    common.add_argument("--delimiter", help="override the sniffed delimiter (e.g. ';' or 'tab')")
    common.add_argument(
        "--date-order", choices=("ymd", "dmy", "mdy"),
        help="override the sniffed date order (MT5 itself writes ymd)",
    )

    checks = argparse.ArgumentParser(add_help=False)
    checks.add_argument(
        "--session-threshold", type=float, default=0.5, metavar="F",
        help="a minute-of-week counts as tradable when bars appear in at least this "
             "fraction of weeks (default: 0.5)",
    )
    checks.add_argument(
        "--spike-mult", type=float, default=30.0, metavar="F",
        help="flag bars whose range exceeds F x the median range (default: 30)",
    )
    checks.add_argument(
        "--jump-mult", type=float, default=30.0, metavar="F",
        help="flag one-minute close moves above F x the median range (default: 30)",
    )
    checks.add_argument(
        "--examples", type=int, default=5, metavar="N",
        help="example rows to print per finding (default: 5)",
    )
    checks.add_argument(
        "--timeframe", "-t", metavar="TF",
        help="bar timeframe (M1, M5, H1, D1, ...); inferred from the data when omitted",
    )

    p_gui = sub.add_parser("gui", help="open the window (also what a bare `mt5clean` does)")
    p_gui.add_argument("file", nargs="?", help="optionally preload this export")

    p_info = sub.add_parser("info", parents=[common], help="show the detected file format only")
    p_info.add_argument("--head", type=int, default=5, metavar="N", help="sample rows to show")

    p_audit = sub.add_parser(
        "audit", parents=[common, checks], help="report gaps and data defects (writes nothing)"
    )
    p_audit.add_argument("--json", action="store_true", help="emit JSON instead of text")
    p_audit.add_argument("--json-out", metavar="PATH", help="also write the JSON report here")
    p_audit.add_argument("--gaps-csv", metavar="PATH", help="write every gap to a CSV")
    p_audit.add_argument("--no-monthly", action="store_true", help="skip the monthly coverage table")
    p_audit.add_argument("--fail-on", choices=("none", "warn", "error"), default="none",
                         help="exit non-zero when findings reach this level (default: none)")

    p_clean = sub.add_parser(
        "clean", parents=[common, checks], help="write a repaired copy"
    )
    p_clean.add_argument("-o", "--output", metavar="PATH", help="destination file")
    p_clean.add_argument("--format", choices=FORMATS, default="sqx",
                         help="output layout (default: sqx)")
    p_clean.add_argument("--digits", type=int, metavar="N",
                         help="price decimals to write (default: detected from the input)")
    p_clean.add_argument("--dedupe", choices=("none", "first", "last"), default="none",
                         help="collapse repeated timestamps (default: none)")
    p_clean.add_argument("--sort", action="store_true",
                         help="sort by timestamp; buffers the whole file in memory")
    p_clean.add_argument("--fix-ohlc", action="store_true",
                         help="clamp high/low so they contain open and close")
    p_clean.add_argument("--drop-invalid", action="store_true",
                         help="drop bars that fail OHLC validation instead of repairing them")
    p_clean.add_argument("--off-grid", choices=("keep", "snap", "drop"), default="keep",
                         help="what to do with stamps that are not on a whole minute")
    p_clean.add_argument("--fill-gaps", action="store_true",
                         help="insert flat zero-volume bars across short in-session gaps")
    p_clean.add_argument("--max-fill", type=int, default=60, metavar="N",
                         help="longest gap, in minutes, that --fill-gaps will invent (default: 60)")
    p_clean.add_argument("--drop-spikes", action="store_true",
                         help="drop bars flagged by --spike-mult")
    p_clean.add_argument("--session-only", action="store_true",
                         help="drop bars outside the detected trading session")
    p_clean.add_argument("--tz-shift", type=_parse_shift, default=0, metavar="OFFSET",
                         help="shift every timestamp, e.g. '-3h' or '120'")
    p_clean.add_argument("--from", dest="from_ts", type=_parse_date, metavar="DATE",
                         help="drop bars before this date")
    p_clean.add_argument("--to", dest="to_ts", type=_parse_date, metavar="DATE",
                         help="drop bars after this date")
    p_clean.add_argument("--dry-run", action="store_true",
                         help="show the plan and the audit, write nothing")
    p_clean.add_argument("--report", metavar="PATH", help="write the pre-clean text report here")
    p_clean.add_argument("--verify", action="store_true",
                         help="re-audit the output file and print its verdict")

    return parser


def _resolve_delimiter(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    named = {"tab": "\t", "\\t": "\t", "comma": ",", "semicolon": ";", "space": " ", "pipe": "|"}
    return named.get(raw.lower(), raw)


def _step(args) -> Optional[int]:
    """The timeframe override in minutes, or None to infer it from the data."""
    raw = getattr(args, "timeframe", None)
    if not raw:
        return None
    try:
        return parse_timeframe(raw)
    except ValueError as exc:
        raise SystemExit(f"mt5clean: {exc}")


def _thresholds(args) -> Thresholds:
    return Thresholds(
        session=args.session_threshold,
        spike_range_mult=args.spike_mult,
        spike_jump_mult=args.jump_mult,
    )


def cmd_gui(args) -> int:
    # Imported here so the CLI keeps working on a Python built without Tk.
    from .gui import run_gui

    return run_gui(getattr(args, "file", None))


def cmd_info(args) -> int:
    dialect = sniff(args.file, _resolve_delimiter(args.delimiter), args.date_order)
    print(f"file       {args.file}")
    print(f"size       {os.path.getsize(args.file) / 1_048_576:,.2f} MiB")
    print(f"kind       {dialect.kind}")
    print(f"format     {dialect.describe()}")
    if dialect.header_line:
        print(f"header     {dialect.header_line[:120]}")
    print("sample:")
    for row in dialect.sample_rows[: args.head]:
        print("  " + " | ".join(row))
    required = {"bid"} if dialect.kind == "ticks" else {"open", "high", "low", "close"}
    missing = required - set(dialect.columns)
    if missing:
        print(f"WARNING: could not locate columns: {', '.join(sorted(missing))}")
        return EXIT_FINDINGS
    return EXIT_OK


def cmd_audit(args) -> int:
    dialect = sniff(args.file, _resolve_delimiter(args.delimiter), args.date_order)

    if dialect.kind == "ticks":
        from .tick_audit import TickThresholds, audit_ticks
        from .tick_report import render_json as render_tick_json
        from .tick_report import render_text as render_tick_text

        tick_thresholds = TickThresholds(session=args.session_threshold)
        result = audit_ticks(args.file, tick_thresholds, dialect)
        if args.json:
            print(render_tick_json(result))
        else:
            print(render_tick_text(result, examples=args.examples))
        if args.json_out:
            with io.open(args.json_out, "w", encoding="utf-8") as fh:
                fh.write(render_tick_json(result))
            print(f"JSON report written to {args.json_out}", file=sys.stderr)
        if args.gaps_csv:
            print("note: --gaps-csv has no meaning for tick files, ignoring", file=sys.stderr)
        return _exit_code(result, args.fail_on)

    result = audit(args.file, _thresholds(args), dialect, step=_step(args))

    if args.json:
        print(render_json(result))
    else:
        print(render_text(result, examples=args.examples, monthly=not args.no_monthly))

    if args.json_out:
        with io.open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(render_json(result))
        print(f"JSON report written to {args.json_out}", file=sys.stderr)

    if args.gaps_csv:
        count = write_gaps_csv(result, args.gaps_csv)
        print(f"{count:,} gaps written to {args.gaps_csv}", file=sys.stderr)

    return _exit_code(result, args.fail_on)


def _exit_code(result, fail_on: str) -> int:
    if fail_on == "error" and result.error_count:
        return EXIT_FINDINGS
    if fail_on == "warn" and (result.error_count or result.warn_count):
        return EXIT_FINDINGS
    return EXIT_OK


def cmd_clean(args) -> int:
    if not args.output and not args.dry_run:
        print("error: --output is required unless you pass --dry-run", file=sys.stderr)
        return EXIT_ERROR

    dialect = sniff(args.file, _resolve_delimiter(args.delimiter), args.date_order)
    if dialect.kind == "ticks":
        print(
            "error: `clean` does not yet repair tick exports, only bars. "
            "Use `mt5clean audit` to check a tick file.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    result = audit(args.file, _thresholds(args), dialect, step=_step(args))
    report_text = render_text(result, examples=args.examples)
    print(report_text)

    if args.report:
        with io.open(args.report, "w", encoding="utf-8") as fh:
            fh.write(report_text)
        print(f"Report written to {args.report}", file=sys.stderr)

    options = CleanOptions(
        duplicates=args.dedupe,
        sort=args.sort,
        fix_ohlc=args.fix_ohlc,
        drop_invalid=args.drop_invalid,
        off_grid=args.off_grid,
        fill_gaps=args.fill_gaps,
        max_fill=args.max_fill,
        drop_spikes=args.drop_spikes,
        spike_limit=result.median_range * args.spike_mult,
        session_only=args.session_only,
        tz_shift=args.tz_shift,
        from_ts=args.from_ts,
        to_ts=args.to_ts,
    )

    print("Repair plan:")
    for step in describe_plan(options):
        print(f"  - {step}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return EXIT_OK

    digits = args.digits if args.digits is not None else result.digits
    stats = CleanStats()
    with open_output(args.output) as fh:
        writer = BarWriter(fh, args.format, digits)
        for bar in clean(args.file, dialect, options, result.mask, stats):
            writer.write(bar)

    print(f"\nWrote {stats.written:,} bars to {args.output}")
    for line in stats.summary():
        print("  " + line)
    for note in stats.notes:
        print(f"  note: {note}")

    print()
    for line in import_hint(args.format, digits):
        print(line)

    if args.verify:
        print(f"\n{'=' * 72}\nRe-audit of the cleaned file\n{'=' * 72}")
        verified = audit(args.output, _thresholds(args), step=_step(args))
        print(render_text(verified, examples=3, monthly=False))

    return EXIT_OK


_NEGATABLE = ("--tz-shift",)


def _glue_negative_values(argv: List[str]) -> List[str]:
    """Let `--tz-shift -3h` work.

    argparse only forgives a leading dash when the value looks like a plain
    negative number, so `-3h` would otherwise be read as an unknown option.
    Rewriting it to `--tz-shift=-3h` keeps the spelling people expect.
    """
    out: List[str] = []
    skip = False
    for index, item in enumerate(argv):
        if skip:
            skip = False
            continue
        if item in _NEGATABLE and index + 1 < len(argv):
            nxt = argv[index + 1]
            if re.fullmatch(r"-\d+(?:\.\d+)?[hm]?", nxt, re.IGNORECASE):
                out.append(f"{item}={nxt}")
                skip = True
                continue
        out.append(item)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])
    if not raw:
        # Double-clicking the file on Windows passes no arguments, and a usage
        # error in a console that closes instantly helps nobody.
        raw = ["gui"]

    parser = build_parser()
    args = parser.parse_args(_glue_negative_values(raw))
    handlers = {"gui": cmd_gui, "info": cmd_info, "audit": cmd_audit, "clean": cmd_clean}
    try:
        return handlers[args.command](args)
    except SniffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except BrokenPipeError:
        return EXIT_OK
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
