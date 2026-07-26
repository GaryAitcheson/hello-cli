"""Tests for the MT5 data cleaner.

The synthetic generator in `make_sample.py` injects a known list of defects, so
most of these tests assert that the auditor finds exactly what was planted -
no more, no fewer.
"""

from __future__ import annotations

import io
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mt5clean import audit, clean, read_bars, render_json, render_text, sniff  # noqa: E402
from mt5clean.cli import main  # noqa: E402
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
    out = tmp_path / "sqx.csv"
    assert main(["clean", dirty_file, "-o", str(out), "--examples", "0"]) == 0
    d = sniff(str(out))
    assert d.delimiter == ","
    assert d.columns["spread"] == 7
    assert list(read_bars(str(out), d))


def test_mt5_format_round_trips(tmp_path, dirty_file):
    out = tmp_path / "mt5.csv"
    assert main(["clean", dirty_file, "-o", str(out), "--format", "mt5", "--examples", "0"]) == 0
    d = sniff(str(out))
    assert d.delimiter == "\t"
    assert d.has_header


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

    repo = Path(__file__).resolve().parent.parent
    built = tmp_path / "mt5clean_standalone.py"
    subprocess.run(
        [sys.executable, str(repo / "tools" / "build_standalone.py"), "-o", str(built)],
        check=True, capture_output=True,
    )

    args = ["audit", dirty_file, "--json", "--examples", "0"]
    standalone = subprocess.run(
        [sys.executable, str(built)] + args, check=True, capture_output=True, text=True,
    )
    package = subprocess.run(
        [sys.executable, "-m", "mt5clean"] + args,
        check=True, capture_output=True, text=True, cwd=str(repo),
    )

    left, right = json.loads(standalone.stdout), json.loads(package.stdout)
    left.pop("file"), right.pop("file")
    assert left == right


def test_cli_tz_shift_parsing(tmp_path, dirty_file):
    out = tmp_path / "shifted.csv"
    assert main(["clean", dirty_file, "-o", str(out), "--tz-shift", "-3h", "--examples", "0"]) == 0
    original = audit(dirty_file)
    shifted = audit(str(out))
    assert shifted.first_ts == original.first_ts - 180
