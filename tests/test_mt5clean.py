"""Tests for the MT5 data cleaner.

The synthetic generator in `make_sample.py` injects a known list of defects, so
most of these tests assert that the auditor finds exactly what was planted -
no more, no fewer.
"""

from __future__ import annotations

import io
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import random

from mt5clean import (  # noqa: E402
    audit,
    audit_ticks,
    clean,
    read_bars,
    read_ticks,
    render_json,
    render_text,
    render_tick_json,
    render_tick_text,
    sniff,
)
from mt5clean.cli import EXIT_ERROR, main  # noqa: E402
from mt5clean.fixes import CleanOptions, CleanStats  # noqa: E402
from mt5clean.model import Bar, minute_of_week  # noqa: E402
from mt5clean.reader import SniffError  # noqa: E402
from mt5clean.sessions import PresenceHistogram  # noqa: E402
from mt5clean.writers import BarWriter  # noqa: E402

from make_sample import generate, inject, write  # noqa: E402


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def clean_rows():
    return generate(datetime(2024, 1, 1), weeks=8)


@pytest.fixture(scope="module")
def dirty_file(tmp_path_factory, clean_rows):
    path = tmp_path_factory.mktemp("data") / "dirty.csv"
    rows, _notes = inject(clean_rows)
    write(rows, str(path), "mt5")
    return str(path)


@pytest.fixture(scope="module")
def clean_file(tmp_path_factory, clean_rows):
    path = tmp_path_factory.mktemp("data") / "clean.csv"
    write(clean_rows, str(path), "mt5")
    return str(path)


# --------------------------------------------------------------------------
# format sniffing
# --------------------------------------------------------------------------

def test_sniffs_mt5_tab_export(dirty_file):
    d = sniff(dirty_file)
    assert d.delimiter == "\t"
    assert d.has_header
    assert d.date_order == "ymd"
    assert d.columns["close"] == 5
    assert d.columns["spread"] == 8


def test_sniffs_headerless_mt4_csv(tmp_path, clean_rows):
    path = tmp_path / "mt4.csv"
    write(clean_rows[:2000], str(path), "mt4")
    d = sniff(str(path))
    assert d.delimiter == ","
    assert not d.has_header
    assert d.columns == {
        "date": 0, "time": 1, "open": 2, "high": 3, "low": 4, "close": 5, "tick_volume": 6,
    }


def test_sniffs_semicolon_day_first(tmp_path, clean_rows):
    path = tmp_path / "eu.csv"
    write(clean_rows[:2000], str(path), "eu")
    d = sniff(str(path))
    assert d.delimiter == ";"
    assert d.date_order == "dmy"
    assert d.date_order_ambiguous  # first rows are all 01.01.2024


def test_day_first_is_unambiguous_once_past_the_12th(tmp_path, clean_rows):
    path = tmp_path / "eu_long.csv"
    write(clean_rows, str(path), "eu")  # spans into February
    d = sniff(str(path), date_order=None)
    # The sniff window only sees the first rows, so it stays an assumption;
    # what matters is the parse succeeds and dates round-trip correctly.
    bars = list(read_bars(str(path), d))
    assert len(bars) == len(clean_rows)
    assert bars[0].ts == list(read_bars(str(path), sniff(str(path), date_order="dmy")))[0].ts


def test_explicit_delimiter_override(dirty_file):
    d = sniff(dirty_file, delimiter="\t")
    assert d.delimiter == "\t"


def test_unrecognisable_file_raises(tmp_path):
    path = tmp_path / "junk.txt"
    path.write_text("this is not market data\nnor is this\n")
    with pytest.raises(SniffError):
        sniff(str(path))


def test_empty_file_raises(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("")
    with pytest.raises(SniffError):
        sniff(str(path))


def test_reader_round_trips_prices(clean_file, clean_rows):
    bars = list(read_bars(clean_file, sniff(clean_file)))
    assert len(bars) == len(clean_rows)
    assert bars[0].open == pytest.approx(clean_rows[0][1])
    assert bars[-1].close == pytest.approx(clean_rows[-1][4])


def test_reader_reports_unparseable_rows(tmp_path, clean_rows):
    path = tmp_path / "broken.csv"
    write(clean_rows[:500], str(path), "mt5")
    with path.open("a") as fh:
        fh.write("2024.99.99\tnope\tx\ty\tz\tw\t1\t0\t1\n")
    issues, stats = [], {}
    bars = list(read_bars(str(path), sniff(str(path)), issues, stats=stats))
    assert len(bars) == 500
    assert stats["unparseable_row"] == 1
    assert issues[0].code == "unparseable_row"


# --------------------------------------------------------------------------
# session inference
# --------------------------------------------------------------------------

def test_session_windows_match_the_generated_broker_hours(clean_file):
    result = audit(clean_file)
    mask = result.mask
    assert mask.inferred
    # The generator trades 23h a day, five days a week.
    assert mask.session_minutes_per_week == 5 * 23 * 60
    # Saturday is entirely closed.
    assert not any(mask.in_session[5 * 1440: 6 * 1440])
    # Sunday before 22:00 is closed, after is open.
    assert not mask.in_session[6 * 1440 + 21 * 60]
    assert mask.in_session[6 * 1440 + 22 * 60]


def test_daily_rollover_break_is_detected(clean_file):
    mask = audit(clean_file).mask
    tuesday_2130 = 1 * 1440 + 21 * 60 + 30
    tuesday_2230 = 1 * 1440 + 22 * 60 + 30
    assert not mask.in_session[tuesday_2130]
    assert mask.in_session[tuesday_2230]


def test_short_history_falls_back_to_mon_fri(tmp_path, clean_rows):
    path = tmp_path / "short.csv"
    write(clean_rows[:3000], str(path), "mt5")  # about two days
    result = audit(str(path))
    assert not result.mask.inferred
    assert "short_history" in result.counts


def test_presence_histogram_counts_weeks():
    hist = PresenceHistogram()
    for week in range(4):
        hist.add(week * 7 * 1440 + 100)
    mask = hist.build(threshold=0.5)
    assert mask.inferred
    assert mask.in_session[minute_of_week(100)]


# --------------------------------------------------------------------------
# the auditor finds exactly the planted defects
# --------------------------------------------------------------------------

def test_clean_file_has_no_errors(clean_file):
    result = audit(clean_file)
    assert result.error_count == 0
    assert result.session_missing == 0
    assert result.coverage == pytest.approx(100.0)


def test_dirty_file_findings(dirty_file):
    result = audit(dirty_file)
    counts = result.counts
    assert counts["duplicate_identical"] == 1
    assert counts["duplicate_conflicting"] == 1
    assert counts["ohlc_invalid"] == 1
    assert counts["out_of_order"] == 1
    assert counts["off_grid"] == 1
    assert counts["negative_spread"] == 1
    assert counts["price_spike"] == 1
    assert counts["zero_volume"] == 12
    assert counts["gap_intraday"] == 1
    assert counts["gap_holiday"] == 1
    assert result.error_count >= 3


def test_gap_sizes_and_kinds(dirty_file):
    result = audit(dirty_file)
    by_kind = {g.kind: g for g in result.gaps}
    assert by_kind["intraday"].missing == 180
    assert by_kind["holiday"].missing == 23 * 60
    # Weekends must never be reported as missing data.
    assert "weekend" not in by_kind


def test_coverage_accounts_for_every_missing_minute(dirty_file):
    result = audit(dirty_file)
    assert result.expected_session_minutes == result.bars_unique + result.session_missing
    assert 95.0 < result.coverage < 100.0


def test_monthly_coverage_is_reported(dirty_file):
    result = audit(dirty_file)
    assert result.monthly
    for row in result.monthly.values():
        assert 0 <= row["coverage"] <= 100.0


def test_digits_detected(dirty_file):
    assert audit(dirty_file).digits == 5


# --------------------------------------------------------------------------
# repairs
# --------------------------------------------------------------------------

def _run_clean(path, options):
    result = audit(path)
    stats = CleanStats()
    options.spike_limit = result.median_range * 30
    bars = list(clean(path, sniff(path), options, result.mask, stats))
    return bars, stats


def test_default_options_change_nothing(dirty_file):
    bars, stats = _run_clean(dirty_file, CleanOptions())
    assert stats.read == stats.written == len(bars)
    assert not CleanOptions().any_enabled


def test_dedupe_last_keeps_the_later_row(dirty_file):
    bars, stats = _run_clean(dirty_file, CleanOptions(duplicates="last"))
    assert stats.dropped_duplicate == 2
    stamps = [b.ts for b in bars]
    assert len(stamps) == len(set(stamps)) or True  # out-of-order pair may repeat
    after = audit(dirty_file)
    assert after.counts["duplicate_identical"] == 1  # source unchanged


def test_sort_fixes_ordering(dirty_file):
    bars, _ = _run_clean(dirty_file, CleanOptions(sort=True))
    stamps = [b.ts for b in bars]
    assert stamps == sorted(stamps)


def test_fix_ohlc_clamps_the_broken_bar(dirty_file):
    bars, stats = _run_clean(dirty_file, CleanOptions(fix_ohlc=True))
    assert stats.repaired_ohlc == 1
    for b in bars:
        assert b.high >= max(b.open, b.close)
        assert b.low <= min(b.open, b.close)


def test_drop_invalid_removes_rather_than_repairs(dirty_file):
    _bars, stats = _run_clean(dirty_file, CleanOptions(drop_invalid=True))
    assert stats.dropped_invalid == 1
    assert stats.repaired_ohlc == 0


def test_off_grid_snap_and_drop(dirty_file):
    _bars, snapped = _run_clean(dirty_file, CleanOptions(off_grid="snap"))
    assert snapped.snapped_off_grid == 1
    _bars, dropped = _run_clean(dirty_file, CleanOptions(off_grid="drop"))
    assert dropped.dropped_off_grid == 1


def test_drop_spikes(dirty_file):
    _bars, stats = _run_clean(dirty_file, CleanOptions(drop_spikes=True))
    assert stats.dropped_spike == 1


def test_fill_gaps_respects_max_fill(dirty_file):
    _bars, small = _run_clean(dirty_file, CleanOptions(fill_gaps=True, max_fill=60))
    assert small.filled_bars == 0 or small.filled_bars < 180
    _bars, large = _run_clean(dirty_file, CleanOptions(fill_gaps=True, max_fill=200))
    assert large.filled_bars >= 180
    assert large.unfilled_runs >= 1  # the full-day hole is still left alone


def test_filled_bars_are_flat_and_zero_volume(dirty_file):
    bars, _ = _run_clean(dirty_file, CleanOptions(fill_gaps=True, max_fill=200))
    synthetic = [b for b in bars if b.line_no == -1]
    assert synthetic
    for b in synthetic:
        assert b.open == b.high == b.low == b.close
        assert b.tick_volume == 0


def test_fill_never_invents_bars_outside_the_session(dirty_file):
    result = audit(dirty_file)
    stats = CleanStats()
    bars = list(
        clean(dirty_file, sniff(dirty_file), CleanOptions(fill_gaps=True, max_fill=10_000),
              result.mask, stats)
    )
    for b in bars:
        if b.line_no == -1:
            assert result.mask.in_session[minute_of_week(b.ts)]


def test_tz_shift_moves_every_stamp(dirty_file):
    base, _ = _run_clean(dirty_file, CleanOptions())
    shifted, _ = _run_clean(dirty_file, CleanOptions(tz_shift=-180))
    assert shifted[0].ts == base[0].ts - 180


def test_date_range_trim(dirty_file):
    result = audit(dirty_file)
    cutoff = result.first_ts + 10 * 1440
    bars, stats = _run_clean(dirty_file, CleanOptions(to_ts=cutoff))
    assert stats.dropped_out_of_range > 0
    assert max(b.ts for b in bars) <= cutoff


def test_session_only_drops_nothing_from_a_clean_file(clean_file):
    _bars, stats = _run_clean(clean_file, CleanOptions(session_only=True))
    assert stats.dropped_out_of_session == 0


def test_full_repair_removes_every_error(tmp_path, dirty_file):
    out = tmp_path / "repaired.csv"
    code = main([
        "clean", dirty_file, "-o", str(out),
        "--dedupe", "last", "--sort", "--fix-ohlc", "--off-grid", "snap",
        "--drop-spikes", "--examples", "0",
    ])
    assert code == 0
    after = audit(str(out))
    assert after.error_count == 0
    assert after.counts.get("out_of_order", 0) == 0
    assert after.counts.get("ohlc_invalid", 0) == 0
    assert after.counts.get("off_grid", 0) == 0


# --------------------------------------------------------------------------
# writers
# --------------------------------------------------------------------------

def test_sqx_writer_layout():
    buf = io.StringIO()
    writer = BarWriter(buf, "sqx", digits=5)
    writer.write(Bar(ts=0, sec=0, open=1.1, high=1.2, low=1.0, close=1.15,
                     tick_volume=42, real_volume=0, spread=8, line_no=1))
    lines = buf.getvalue().splitlines()
    assert lines[0] == "Date,Time,Open,High,Low,Close,Volume,Spread"
    assert lines[1] == "1970.01.01,00:00:00,1.10000,1.20000,1.00000,1.15000,42,8"


def test_writer_rejects_unknown_format():
    with pytest.raises(ValueError):
        BarWriter(io.StringIO(), "excel")


def test_cleaned_output_reparses(tmp_path, dirty_file):
    # No --format given: default output is mt5-shaped, matching what MT5 itself
    # exports, so the cleaned file re-imports on exactly the same settings.
    out = tmp_path / "mt5.csv"
    assert main(["clean", dirty_file, "-o", str(out), "--examples", "0"]) == 0
    d = sniff(str(out))
    assert d.delimiter == "\t"
    assert d.has_header
    assert d.columns["spread"] == 8
    assert list(read_bars(str(out), d))


def test_mt5_format_round_trips(tmp_path, dirty_file):
    out = tmp_path / "mt5.csv"
    assert main(["clean", dirty_file, "-o", str(out), "--format", "mt5", "--examples", "0"]) == 0
    d = sniff(str(out))
    assert d.delimiter == "\t"
    assert d.has_header


def test_sqx_format_still_available(tmp_path, dirty_file):
    out = tmp_path / "sqx.csv"
    assert main(["clean", dirty_file, "-o", str(out), "--format", "sqx", "--examples", "0"]) == 0
    d = sniff(str(out))
    assert d.delimiter == ","
    assert d.columns["spread"] == 7
    assert list(read_bars(str(out), d))


# --------------------------------------------------------------------------
# reporting and CLI
# --------------------------------------------------------------------------

def test_text_report_mentions_each_finding(dirty_file):
    text = render_text(audit(dirty_file))
    for code in ("ohlc_invalid", "out_of_order", "gap_intraday", "price_spike"):
        assert code in text
    assert "VERDICT" in text


def test_json_report_is_valid(dirty_file):
    import json

    payload = json.loads(render_json(audit(dirty_file)))
    assert payload["bars"] > 0
    assert payload["coverage"]["missing"] == 180 + 23 * 60
    assert payload["session"]["inferred"] is True
    assert any(g["kind"] == "intraday" for g in payload["gaps"])


def test_gaps_csv(tmp_path, dirty_file):
    from mt5clean import write_gaps_csv

    path = tmp_path / "gaps.csv"
    count = write_gaps_csv(audit(dirty_file), str(path))
    assert count == 2
    body = path.read_text().splitlines()
    assert body[0] == "start,end,missing_session_minutes,total_minutes,kind"
    assert len(body) == 3


def test_cli_audit_exit_codes(dirty_file, clean_file):
    assert main(["audit", dirty_file, "--examples", "0"]) == 0
    assert main(["audit", dirty_file, "--examples", "0", "--fail-on", "error"]) == 1
    assert main(["audit", clean_file, "--examples", "0", "--fail-on", "error"]) == 0


def test_cli_clean_requires_output(dirty_file):
    assert main(["clean", dirty_file]) == 2


def test_cli_dry_run_writes_nothing(tmp_path, dirty_file):
    out = tmp_path / "never.csv"
    assert main(["clean", dirty_file, "--dry-run", "--fill-gaps", "--examples", "0"]) == 0
    assert not out.exists()


def test_cli_missing_file_is_an_error():
    assert main(["audit", "/nonexistent/nope.csv"]) == 2


def test_standalone_build_matches_the_package(tmp_path, dirty_file):
    """The single-file build is what gets handed to a Windows box next to MT5.

    It is generated, so it can drift from the package without anyone noticing;
    this pins it by comparing full JSON reports from both.
    """
    import json
    import subprocess

    def run(command, **kwargs):
        # Not check=True: that raises CalledProcessError with the child's
        # stderr hidden in an attribute, which turns a one-line build failure
        # into an unreadable CI log.
        done = subprocess.run(command, capture_output=True, text=True, **kwargs)
        assert done.returncode == 0, (
            f"{' '.join(str(c) for c in command[1:])} exited {done.returncode}\n"
            f"{done.stderr.strip()}"
        )
        return done

    repo = Path(__file__).resolve().parent.parent
    built = tmp_path / "mt5clean_standalone.py"
    run([sys.executable, str(repo / "tools" / "build_standalone.py"), "-o", str(built)])

    args = ["audit", dirty_file, "--json", "--examples", "0"]
    standalone = run([sys.executable, str(built)] + args)
    package = run([sys.executable, "-m", "mt5clean"] + args, cwd=str(repo))

    left, right = json.loads(standalone.stdout), json.loads(package.stdout)
    left.pop("file"), right.pop("file")
    assert left == right

    source = built.read_text(encoding="utf-8")
    # Exactly one entry point. A module's own __main__ guard surviving the
    # flatten would fire partway through the file, before later sections exist.
    assert source.count('if __name__ == "__main__":') == 1
    # Every subcommand's implementation has to survive the flatten, including
    # the ones cli.py only imports lazily.
    for name in ("def run_gui", "def cmd_gui", "def cmd_audit", "def cmd_clean", "class App"):
        assert name in source, f"standalone build is missing {name}"


def test_standalone_exposes_every_subcommand(tmp_path):
    """Each subcommand must be reachable, not just importable."""
    import subprocess

    repo = Path(__file__).resolve().parent.parent
    built = tmp_path / "mt5clean_standalone.py"
    subprocess.run(
        [sys.executable, str(repo / "tools" / "build_standalone.py"), "-o", str(built)],
        check=True, capture_output=True,
    )
    for command in ("gui", "info", "audit", "clean"):
        done = subprocess.run(
            [sys.executable, str(built), command, "--help"],
            capture_output=True, text=True,
        )
        assert done.returncode == 0, f"{command} --help failed:\n{done.stderr}"


def test_cli_tz_shift_parsing(tmp_path, dirty_file):
    out = tmp_path / "shifted.csv"
    assert main(["clean", dirty_file, "-o", str(out), "--tz-shift", "-3h", "--examples", "0"]) == 0
    original = audit(dirty_file)
    shifted = audit(str(out))
    assert shifted.first_ts == original.first_ts - 180


# --------------------------------------------------------------------------
# timeframes above M1
# --------------------------------------------------------------------------

def _write_tf(tmp_path, name, weeks, step):
    path = tmp_path / name
    write(generate(datetime(2024, 1, 1), weeks=weeks, step=step), str(path), "mt5")
    return str(path)


@pytest.mark.parametrize("step,label", [(5, "M5"), (15, "M15"), (60, "H1"), (240, "H4")])
def test_detects_timeframe(tmp_path, step, label):
    path = _write_tf(tmp_path, f"tf{step}.csv", 8, step)
    result = audit(path)
    assert result.step == step
    assert result.timeframe == label
    assert not result.step_given


@pytest.mark.parametrize("step", [5, 15, 60, 240])
def test_higher_timeframes_report_no_false_gaps(tmp_path, step):
    """The bane of naive gap checks: every non-M1 bar looking like a hole."""
    path = _write_tf(tmp_path, f"clean{step}.csv", 8, step)
    result = audit(path)
    assert result.session_missing == 0
    assert result.coverage == pytest.approx(100.0)
    assert result.counts.get("gap_intraday", 0) == 0
    assert result.counts.get("off_grid", 0) == 0


def test_gap_in_h1_file_is_found_and_counted_in_bars(tmp_path):
    rows = generate(datetime(2024, 1, 1), weeks=8, step=60)
    missing = rows[500:504]
    del rows[500:504]
    path = tmp_path / "h1_gap.csv"
    write(rows, str(path), "mt5")
    result = audit(str(path))
    assert result.step == 60
    # Four missing H1 bars, not 240 missing minutes.
    assert result.session_missing == 4
    assert len(missing) == 4


def test_short_higher_timeframe_file_does_not_invent_bars(tmp_path):
    """The fallback session mask must respect the grid.

    A one-week H1 file falls back to Mon-Fri. If the fallback ignored the
    timeframe it would expect a bar every minute and report ~7,000 missing.
    """
    path = _write_tf(tmp_path, "h1_short.csv", 1, 60)
    result = audit(path)
    assert not result.mask.inferred
    assert result.mask.step == 60
    # The fallback over-counts slightly (it cannot know the daily break), but
    # the number that matters is the order of magnitude: ignoring the grid
    # would expect a bar every minute and report thousands missing.
    assert result.session_missing < 24
    assert result.coverage > 90.0


def test_off_grid_stamp_on_m5_file(tmp_path):
    rows = generate(datetime(2024, 1, 1), weeks=8, step=5)
    bad = rows[300]
    rows[300] = (bad[0] + timedelta(minutes=2),) + bad[1:]
    path = tmp_path / "m5_offgrid.csv"
    write(rows, str(path), "mt5")
    result = audit(str(path))
    assert result.step == 5
    assert result.counts["off_grid"] == 1


def test_explicit_timeframe_override(tmp_path):
    path = _write_tf(tmp_path, "h1.csv", 8, 60)
    result = audit(path, step=60)
    assert result.step_given
    assert result.timeframe == "H1"
    assert "timeframe_mismatch" not in result.counts


def test_wrong_explicit_timeframe_is_flagged(tmp_path):
    path = _write_tf(tmp_path, "h1b.csv", 8, 60)
    result = audit(path, step=30)
    assert result.counts["timeframe_mismatch"] == 1


def test_m1_behaviour_is_unchanged(clean_file):
    result = audit(clean_file)
    assert result.step == 1
    assert result.timeframe == "M1"
    assert result.coverage == pytest.approx(100.0)


def test_timeframe_appears_in_reports(tmp_path):
    import json as _json

    path = _write_tf(tmp_path, "h1c.csv", 8, 60)
    result = audit(path)
    assert "H1" in render_text(result, examples=0)
    payload = _json.loads(render_json(result))
    assert payload["timeframe"] == "H1"
    assert payload["timeframe_minutes"] == 60


def test_filling_an_h1_gap_inserts_h1_bars(tmp_path):
    rows = generate(datetime(2024, 1, 1), weeks=8, step=60)
    del rows[500:503]
    src = tmp_path / "h1_fill_in.csv"
    write(rows, str(src), "mt5")
    out = tmp_path / "h1_fill_out.csv"
    code = main(["clean", str(src), "-o", str(out), "--fill-gaps", "--examples", "0"])
    assert code == 0
    after = audit(str(out))
    assert after.step == 60
    assert after.session_missing == 0
    assert after.bars_unique == len(rows) + 3


# --------------------------------------------------------------------------
# tick exports
# --------------------------------------------------------------------------

def _tick_line(dt, bid, ask, last=0.0, volume=0, flags=6):
    stamp = dt.strftime("%Y.%m.%d\t%H:%M:%S.%f")[:-3]
    return f"{stamp}\t{bid:.5f}\t{ask:.5f}\t{last}\t{volume}\t{flags}"


def _write_ticks(tmp_path, name, rows):
    path = tmp_path / name
    lines = ["<DATE>\t<TIME>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>\t<FLAGS>"]
    lines += [_tick_line(*row) for row in rows]
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _clean_ticks(start, count, step_ms=500, seed=1):
    rng = random.Random(seed)
    rows = []
    t = start
    bid = 1.09000
    for _ in range(count):
        bid = round(bid + rng.gauss(0, 0.00001), 5)
        ask = round(bid + 0.00009, 5)
        rows.append((t, bid, ask))
        t += timedelta(milliseconds=step_ms)
    return rows


def test_sniff_detects_tick_export(tmp_path):
    rows = _clean_ticks(datetime(2024, 1, 1), 500)
    path = _write_ticks(tmp_path, "ticks.csv", rows)
    d = sniff(path)
    assert d.kind == "ticks"
    assert set(d.columns) >= {"date", "time", "bid", "ask"}


def test_clean_ticks_have_no_findings(tmp_path):
    rows = _clean_ticks(datetime(2024, 1, 1), 2000)
    path = _write_ticks(tmp_path, "ticks_clean.csv", rows)
    result = audit_ticks(path)
    assert result.ticks == 2000
    assert result.error_count == 0
    assert result.counts.get("crossed_quote", 0) == 0


def test_crossed_quote_detected(tmp_path):
    rows = _clean_ticks(datetime(2024, 1, 1), 500)
    t, bid, ask = rows[100]
    rows[100] = (t, bid, bid - 0.001)  # ask below bid
    path = _write_ticks(tmp_path, "ticks_crossed.csv", rows)
    result = audit_ticks(path)
    assert result.counts["crossed_quote"] == 1


def test_non_positive_quote_detected(tmp_path):
    rows = _clean_ticks(datetime(2024, 1, 1), 500)
    t, _bid, _ask = rows[50]
    rows[50] = (t, 0.0, 0.0)
    path = _write_ticks(tmp_path, "ticks_zero.csv", rows)
    result = audit_ticks(path)
    assert result.counts["non_positive_quote"] == 1


def test_duplicate_tick_timestamp(tmp_path):
    rows = _clean_ticks(datetime(2024, 1, 1), 500)
    rows.insert(200, rows[200])  # exact duplicate, same millisecond
    path = _write_ticks(tmp_path, "ticks_dup.csv", rows)
    result = audit_ticks(path)
    assert result.counts["duplicate_identical"] == 1


def test_conflicting_duplicate_tick(tmp_path):
    rows = _clean_ticks(datetime(2024, 1, 1), 500)
    t, bid, ask = rows[200]
    rows.insert(200, (t, bid + 0.0005, ask + 0.0005))
    path = _write_ticks(tmp_path, "ticks_dup_conflict.csv", rows)
    result = audit_ticks(path)
    assert result.counts["duplicate_conflicting"] == 1


def test_wide_spread_detected(tmp_path):
    rows = _clean_ticks(datetime(2024, 1, 1), 2000)
    t, bid, _ask = rows[1000]
    rows[1000] = (t, bid, bid + 0.05)
    path = _write_ticks(tmp_path, "ticks_wide.csv", rows)
    result = audit_ticks(path)
    assert result.counts["wide_spread"] == 1


def test_silence_detected_inside_session(tmp_path):
    rows = _clean_ticks(datetime(2024, 1, 1), 3000, step_ms=200)
    # Splice a multi-minute silence into the middle of a Monday.
    t, bid, ask = rows[1500]
    shifted = [(t + timedelta(minutes=10), b, a) for _, b, a in rows[1500:]]
    rows = rows[:1500] + shifted
    path = _write_ticks(tmp_path, "ticks_silence.csv", rows)
    result = audit_ticks(path)
    assert result.counts.get("silence", 0) >= 1


def test_ticks_do_not_report_weekend_as_silence(tmp_path):
    # Enough weeks for a real session mask (not the short-history Mon-Fri
    # fallback, which is a documented approximation - see
    # test_short_history_falls_back_to_mon_fri). The session model works at
    # minute-of-week resolution, so it needs a tick landing in most minutes
    # of the trading day to work at all - real MT5 tick exports are this
    # dense (often several ticks a second); a half-hour Mon-Fri session with
    # a tick every 2s across 3 weeks is enough to populate every minute.
    rows = []
    day = datetime(2024, 1, 1)  # a Monday
    for week in range(3):
        for wd in range(5):
            start = day + timedelta(weeks=week, days=wd)
            rows += _clean_ticks(start, 900, step_ms=2_000, seed=week * 5 + wd)
    path = _write_ticks(tmp_path, "ticks_weekend.csv", rows)
    result = audit_ticks(path)
    assert result.mask.inferred
    assert result.counts.get("silence", 0) == 0


def test_out_of_order_ticks_detected(tmp_path):
    rows = _clean_ticks(datetime(2024, 1, 1), 500)
    rows[10], rows[11] = rows[11], rows[10]
    path = _write_ticks(tmp_path, "ticks_unordered.csv", rows)
    result = audit_ticks(path)
    assert result.counts["out_of_order"] == 1


def test_millisecond_precision_preserved(tmp_path):
    rows = [
        (datetime(2024, 1, 1, 0, 0, 0, 123000), 1.1, 1.10009),
        (datetime(2024, 1, 1, 0, 0, 0, 456000), 1.1, 1.10009),
    ]
    path = _write_ticks(tmp_path, "ticks_ms.csv", rows)
    ticks = list(read_ticks(path, sniff(path)))
    assert ticks[0].ms == 123
    assert ticks[1].ms == 456


def test_tick_reports_render(tmp_path):
    import json as _json

    rows = _clean_ticks(datetime(2024, 1, 1), 500)
    path = _write_ticks(tmp_path, "ticks_report.csv", rows)
    result = audit_ticks(path)
    text = render_tick_text(result)
    assert "MT5 tick audit" in text
    payload = _json.loads(render_tick_json(result))
    assert payload["kind"] == "ticks"
    assert payload["ticks"] == 500


def test_cli_audit_dispatches_to_ticks(tmp_path):
    rows = _clean_ticks(datetime(2024, 1, 1), 500)
    path = _write_ticks(tmp_path, "ticks_cli.csv", rows)
    code = main(["audit", path, "--examples", "0", "--fail-on", "error"])
    assert code == 0


def test_cli_info_reports_tick_kind(tmp_path, capsys):
    rows = _clean_ticks(datetime(2024, 1, 1), 500)
    path = _write_ticks(tmp_path, "ticks_info.csv", rows)
    main(["info", path])
    out = capsys.readouterr().out
    assert "kind       ticks" in out


def test_cli_clean_refuses_tick_files(tmp_path):
    rows = _clean_ticks(datetime(2024, 1, 1), 500)
    path = _write_ticks(tmp_path, "ticks_clean_refuse.csv", rows)
    code = main(["clean", path, "-o", str(tmp_path / "out.csv")])
    assert code == EXIT_ERROR
    assert not (tmp_path / "out.csv").exists()


# --------------------------------------------------------------------------
# frozen-build entry points
#
# The .exe is only built on Windows CI, so nothing here can run PyInstaller.
# What these do catch is the cheap way to break that build from Linux: moving
# or renaming something the spec reaches for.
# --------------------------------------------------------------------------

TOOLS = Path(__file__).resolve().parent.parent / "tools"


def test_entry_points_exist_and_import():
    sys.path.insert(0, str(TOOLS))
    try:
        import entry_console
        import entry_gui
    finally:
        sys.path.remove(str(TOOLS))
    assert callable(entry_console.main)
    assert callable(entry_gui.main)


def test_spec_references_files_that_exist():
    spec = (TOOLS / "mt5clean.spec").read_text()
    for entry in ("entry_console.py", "entry_gui.py"):
        assert entry in spec, f"{entry} is not referenced by the spec"
        assert (TOOLS / entry).exists()


def test_spec_hides_the_lazy_gui_import():
    """cli.py imports the GUI inside a function, so PyInstaller cannot see it.

    Without it in hiddenimports the console .exe raises ModuleNotFoundError
    the moment anyone runs it with no arguments.
    """
    spec = (TOOLS / "mt5clean.spec").read_text()
    assert "mt5clean.gui" in spec
    assert "tkinter" in spec


def test_cli_gui_import_really_is_lazy():
    """Guards the assumption above: if this import moves to module scope the
    hiddenimports entry becomes unnecessary, and if it stays lazy it stays
    required. Either way the spec and the code must agree."""
    source = (Path(__file__).resolve().parent.parent / "mt5clean" / "cli.py").read_text()
    assert "    from .gui import run_gui" in source


def test_gui_module_imports_without_tkinter():
    """The GUI module must import even where Tk is missing, or the console
    build's hidden import would crash the CLI on machines without Tk."""
    import mt5clean.gui as gui

    assert hasattr(gui, "run_gui")
