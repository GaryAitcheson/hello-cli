"""Tests for the MT5 data cleaner.

Run with: python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mt5clean import analyze, clean, read_file, sniff, write_file  # noqa: E402
from mt5clean.analyze import find_gaps, infer_timeframe  # noqa: E402
from mt5clean.cli import main  # noqa: E402
from mt5clean.formats import Bar, ParseError, parse_rows, parse_timestamp  # noqa: E402

MT5_HEADER = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"


def make_bars(start: datetime, count: int, step_minutes: int = 1, price: float = 1.10000):
    """A clean synthetic M1 series with a gentle drift."""
    bars = []
    for index in range(count):
        base = price + index * 0.00001
        bars.append(
            Bar(
                ts=start + timedelta(minutes=index * step_minutes),
                open=round(base, 5),
                high=round(base + 0.00005, 5),
                low=round(base - 0.00005, 5),
                close=round(base + 0.00002, 5),
                tickvol=50 + index % 7,
                vol=0,
                spread=10,
                line=index + 2,
            )
        )
    return bars


def to_mt5_text(bars, header: str = MT5_HEADER) -> str:
    lines = [header] if header else []
    for bar in bars:
        lines.append(
            "\t".join(
                [
                    bar.ts.strftime("%Y.%m.%d"),
                    bar.ts.strftime("%H:%M:%S"),
                    f"{bar.open:.5f}",
                    f"{bar.high:.5f}",
                    f"{bar.low:.5f}",
                    f"{bar.close:.5f}",
                    str(int(bar.tickvol)),
                    str(int(bar.vol)),
                    str(int(bar.spread)),
                ]
            )
        )
    return "\n".join(lines) + "\n"


class TestParsing(unittest.TestCase):
    def test_parses_mt5_tab_export(self):
        text = to_mt5_text(make_bars(datetime(2023, 1, 2), 5))
        dialect = sniff(text)
        self.assertEqual(dialect.kind, "bars")
        self.assertEqual(dialect.delimiter, "\t")
        self.assertTrue(dialect.has_header)
        records, bad = parse_rows(text, dialect)
        self.assertEqual(len(records), 5)
        self.assertEqual(bad, [])
        self.assertEqual(records[0].ts, datetime(2023, 1, 2, 0, 0))
        self.assertAlmostEqual(records[0].open, 1.10000, places=5)

    def test_parses_headerless_mt4_csv(self):
        text = (
            "2023.01.02,00:00,1.06975,1.07010,1.06952,1.06965,123\n"
            "2023.01.02,00:01,1.06965,1.06990,1.06960,1.06980,98\n"
        )
        dialect = sniff(text)
        self.assertFalse(dialect.has_header)
        self.assertEqual(dialect.delimiter, ",")
        records, bad = parse_rows(text, dialect)
        self.assertEqual(len(records), 2)
        self.assertEqual(bad, [])
        self.assertAlmostEqual(records[0].tickvol, 123)

    def test_parses_semicolon_with_comma_decimals(self):
        text = (
            "<DATE>;<TIME>;<OPEN>;<HIGH>;<LOW>;<CLOSE>;<TICKVOL>\n"
            "2023.01.02;00:00:00;1,06975;1,07010;1,06952;1,06965;123\n"
        )
        dialect = sniff(text)
        self.assertEqual(dialect.delimiter, ";")
        self.assertTrue(dialect.decimal_comma)
        records, _ = parse_rows(text, dialect)
        self.assertAlmostEqual(records[0].open, 1.06975, places=5)

    def test_parses_tick_export(self):
        text = (
            "<DATE>\t<TIME>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>\t<FLAGS>\n"
            "2023.01.02\t00:00:00.123\t1.06975\t1.06985\t0\t0\t6\n"
        )
        dialect = sniff(text)
        self.assertEqual(dialect.kind, "ticks")
        records, _ = parse_rows(text, dialect)
        self.assertEqual(records[0].ts, datetime(2023, 1, 2, 0, 0, 0, 123000))
        self.assertAlmostEqual(records[0].ask, 1.06985, places=5)

    def test_combined_datetime_column(self):
        text = "<DATETIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>\n2023-01-02 00:00:00,1.1,1.2,1.0,1.15\n"
        dialect = sniff(text)
        records, bad = parse_rows(text, dialect)
        self.assertEqual(bad, [])
        self.assertEqual(records[0].ts, datetime(2023, 1, 2))

    def test_bad_rows_are_collected_not_fatal(self):
        text = to_mt5_text(make_bars(datetime(2023, 1, 2), 3))
        text += "not-a-date\tgarbage\t1\t2\t3\t4\t5\t6\t7\n"
        text += "2023.01.02\t00:05:00\tNOPE\t1.1\t1.0\t1.05\t1\t0\t1\n"
        dialect = sniff(text)
        records, bad = parse_rows(text, dialect)
        self.assertEqual(len(records), 3)
        self.assertEqual(len(bad), 2)
        self.assertIn("timestamp", bad[0].reason)
        self.assertIn("non-numeric", bad[1].reason)

    def test_shift_minutes(self):
        text = to_mt5_text(make_bars(datetime(2023, 1, 2, 5, 0), 2))
        dialect = sniff(text)
        records, _ = parse_rows(text, dialect, shift_minutes=-120)
        self.assertEqual(records[0].ts, datetime(2023, 1, 2, 3, 0))

    def test_empty_file_rejected(self):
        with self.assertRaises(ParseError):
            sniff("   \n\n")

    def test_parse_timestamp_variants(self):
        self.assertEqual(parse_timestamp("2023.01.02", "00:00:00"), datetime(2023, 1, 2))
        self.assertEqual(parse_timestamp("2023-01-02", "13:45"), datetime(2023, 1, 2, 13, 45))
        self.assertEqual(parse_timestamp("2023.01.02 13:45:01"), datetime(2023, 1, 2, 13, 45, 1))
        self.assertIsNone(parse_timestamp("nonsense"))


class TestTimeframe(unittest.TestCase):
    def test_infers_m1(self):
        name, seconds = infer_timeframe(make_bars(datetime(2023, 1, 2), 50))
        self.assertEqual(name, "M1")
        self.assertEqual(seconds, 60)

    def test_infers_h1_despite_gaps(self):
        bars = make_bars(datetime(2023, 1, 2), 50, step_minutes=60)
        del bars[10]
        name, _ = infer_timeframe(bars)
        self.assertEqual(name, "H1")


class TestGaps(unittest.TestCase):
    def test_detects_interior_gap(self):
        bars = make_bars(datetime(2023, 1, 2), 400)
        removed = bars[100:105]
        del bars[100:105]
        gaps, expected, coverage = find_gaps(bars, 60)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].missing, 5)
        self.assertEqual(gaps[0].start, removed[0].ts)
        self.assertEqual(gaps[0].end, removed[-1].ts)
        self.assertLess(coverage, 1.0)
        self.assertEqual(expected, 400)

    def test_no_gaps_in_clean_series(self):
        gaps, _, coverage = find_gaps(make_bars(datetime(2023, 1, 2), 500), 60)
        self.assertEqual(gaps, [])
        self.assertEqual(coverage, 1.0)

    def test_weekend_is_not_a_gap(self):
        # Three full weeks of H1 bars, Monday 00:00 through Friday 23:00.
        bars = []
        cursor = datetime(2023, 1, 2)  # a Monday
        for _ in range(3 * 7 * 24):
            if cursor.weekday() < 5:
                bars.append(
                    Bar(ts=cursor, open=1.1, high=1.1, low=1.1, close=1.1, tickvol=1)
                )
            cursor += timedelta(hours=1)
        gaps, _, coverage = find_gaps(bars, 3600, session_threshold=0.5)
        self.assertEqual(gaps, [], f"weekends reported as gaps: {gaps}")
        self.assertEqual(coverage, 1.0)

    def test_weekend_is_a_gap_when_sessions_ignored(self):
        bars = []
        cursor = datetime(2023, 1, 2)
        for _ in range(3 * 7 * 24):
            if cursor.weekday() < 5:
                bars.append(
                    Bar(ts=cursor, open=1.1, high=1.1, low=1.1, close=1.1, tickvol=1)
                )
            cursor += timedelta(hours=1)
        gaps, _, _ = find_gaps(bars, 3600, respect_sessions=False)
        self.assertGreater(len(gaps), 0)

    def test_gap_inside_trading_week_still_found_with_weekends(self):
        bars = []
        cursor = datetime(2023, 1, 2)
        for _ in range(3 * 7 * 24):
            if cursor.weekday() < 5:
                bars.append(
                    Bar(ts=cursor, open=1.1, high=1.1, low=1.1, close=1.1, tickvol=1)
                )
            cursor += timedelta(hours=1)
        target = bars[30].ts
        del bars[30]
        gaps, _, _ = find_gaps(bars, 3600, session_threshold=0.5)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].start, target)


class TestChecks(unittest.TestCase):
    def test_flags_invalid_ohlc(self):
        bars = make_bars(datetime(2023, 1, 2), 20)
        bars[5].high = bars[5].low - 0.001  # high below low
        result = analyze(bars, "bars", timeframe="M1")
        self.assertIn(5, result.invalid_indices)
        self.assertIn("invalid_ohlc", result.counts())

    def test_flags_duplicates_and_distinguishes_conflicts(self):
        bars = make_bars(datetime(2023, 1, 2), 20)
        exact = Bar(**{**bars[3].__dict__})
        conflicting = Bar(**{**bars[7].__dict__})
        conflicting.close += 0.005
        bars.extend([exact, conflicting])
        result = analyze(bars, "bars", timeframe="M1")
        counts = result.counts()
        self.assertEqual(counts.get("duplicate_exact"), 1)
        self.assertEqual(counts.get("duplicate_conflicting"), 1)
        self.assertEqual(result.conflicting_duplicates, 1)

    def test_flags_out_of_order(self):
        bars = make_bars(datetime(2023, 1, 2), 20)
        bars[5], bars[6] = bars[6], bars[5]
        result = analyze(bars, "bars", timeframe="M1")
        self.assertIn("out_of_order", result.counts())

    def test_flags_price_spike(self):
        bars = make_bars(datetime(2023, 1, 2), 200)
        bars[100].close *= 1.5
        bars[100].high = bars[100].close
        result = analyze(bars, "bars", timeframe="M1")
        self.assertIn("price_spike", result.counts())
        self.assertIn(100, result.spike_indices)

    def test_flags_flat_run(self):
        bars = make_bars(datetime(2023, 1, 2), 60)
        for bar in bars[10:30]:
            bar.open = bar.high = bar.low = bar.close = 1.10000
        result = analyze(bars, "bars", timeframe="M1")
        self.assertIn("flat_run", result.counts())

    def test_flags_crossed_tick(self):
        from mt5clean.formats import Tick

        ticks = [
            Tick(ts=datetime(2023, 1, 2, 0, 0, index), bid=1.1, ask=1.2)
            for index in range(10)
        ]
        ticks[4].ask = 1.0
        result = analyze(ticks, "ticks")
        self.assertIn("invalid_tick", result.counts())

    def test_clean_series_has_no_findings(self):
        result = analyze(make_bars(datetime(2023, 1, 2), 300), "bars", timeframe="M1")
        self.assertEqual(result.counts(), {})
        self.assertEqual(result.error_count, 0)


class TestClean(unittest.TestCase):
    def _analysis(self, bars):
        return analyze(bars, "bars", timeframe="M1")

    def test_sorts_and_dedupes(self):
        bars = make_bars(datetime(2023, 1, 2), 20)
        bars[5], bars[6] = bars[6], bars[5]
        duplicate = Bar(**{**bars[3].__dict__})
        duplicate.close += 0.001
        bars.append(duplicate)
        result = clean(bars, "bars", self._analysis(bars), dedupe_mode="last")
        stamps = [bar.ts for bar in result.records]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(set(stamps)), len(stamps))
        self.assertEqual(result.dropped_duplicates, 1)
        self.assertGreater(result.sorted_rows, 0)

    def test_dedupe_keep_first_vs_last(self):
        bars = make_bars(datetime(2023, 1, 2), 5)
        duplicate = Bar(**{**bars[2].__dict__})
        duplicate.close = 9.99999
        bars.append(duplicate)
        last = clean(bars, "bars", self._analysis(bars), dedupe_mode="last")
        first = clean(bars, "bars", self._analysis(bars), dedupe_mode="first")
        self.assertAlmostEqual(last.records[2].close, 9.99999)
        self.assertNotAlmostEqual(first.records[2].close, 9.99999)

    def test_repair_ohlc(self):
        bars = make_bars(datetime(2023, 1, 2), 10)
        bars[4].high = bars[4].close - 0.001
        result = clean(bars, "bars", self._analysis(bars), do_repair_ohlc=True)
        self.assertEqual(result.repaired_ohlc, 1)
        fixed = result.records[4]
        self.assertGreaterEqual(fixed.high, max(fixed.open, fixed.close))
        self.assertLessEqual(fixed.low, min(fixed.open, fixed.close))

    def test_drop_invalid(self):
        bars = make_bars(datetime(2023, 1, 2), 10)
        bars[4].close = -1.0
        result = clean(bars, "bars", self._analysis(bars), drop_invalid=True)
        self.assertEqual(result.dropped_invalid, 1)
        self.assertEqual(len(result.records), 9)

    def test_fill_short_gap(self):
        bars = make_bars(datetime(2023, 1, 2), 400)
        anchor_close = bars[99].close
        missing = [bar.ts for bar in bars[100:103]]
        del bars[100:103]
        analysis = self._analysis(bars)
        result = clean(bars, "bars", analysis, fill_mode="ffill", fill_max_bars=5)
        self.assertEqual(result.filled_bars, 3)
        stamps = {bar.ts for bar in result.records}
        for ts in missing:
            self.assertIn(ts, stamps)
        filled = [bar for bar in result.records if bar.synthetic]
        self.assertEqual(len(filled), 3)
        for bar in filled:
            self.assertEqual(bar.open, bar.high, bar.low)
            self.assertAlmostEqual(bar.close, anchor_close)
            self.assertEqual(bar.tickvol, 0)

    def test_long_gap_left_unfilled(self):
        bars = make_bars(datetime(2023, 1, 2), 400)
        del bars[100:150]
        analysis = self._analysis(bars)
        result = clean(bars, "bars", analysis, fill_mode="ffill", fill_max_bars=5)
        self.assertEqual(result.filled_bars, 0)
        self.assertEqual(result.unfilled_gaps, 1)
        self.assertTrue(result.notes)

    def test_drop_spikes_removes_only_the_bad_bar(self):
        bars = make_bars(datetime(2023, 1, 2), 200)
        bad_ts = bars[100].ts
        recovery_ts = bars[101].ts
        bars[100].close *= 1.5  # single bad print; bar 101 returns to normal
        result = clean(bars, "bars", self._analysis(bars), drop_spikes=True)
        self.assertEqual(result.dropped_spikes, 1)
        stamps = {bar.ts for bar in result.records}
        self.assertNotIn(bad_ts, stamps)
        self.assertIn(recovery_ts, stamps, "the recovery bar is good data")

    def test_level_shift_is_reported_but_not_dropped(self):
        # Every bar after 100 repriced upward and stays there — genuine move.
        bars = make_bars(datetime(2023, 1, 2), 200)
        for bar in bars[100:]:
            bar.open *= 1.5
            bar.high *= 1.5
            bar.low *= 1.5
            bar.close *= 1.5
        analysis = self._analysis(bars)
        self.assertIn("price_spike", analysis.counts())
        result = clean(bars, "bars", analysis, drop_spikes=True)
        self.assertEqual(result.dropped_spikes, 0)
        self.assertEqual(len(result.records), 200)

    def test_no_fixes_leaves_data_alone(self):
        bars = make_bars(datetime(2023, 1, 2), 50)
        result = clean(bars, "bars", self._analysis(bars), dedupe_mode="none", do_sort=False)
        self.assertFalse(result.changed)
        self.assertEqual(len(result.records), 50)


class TestRoundTrip(unittest.TestCase):
    def test_write_then_read_preserves_values(self):
        bars = make_bars(datetime(2023, 1, 2), 30)
        dialect = sniff(to_mt5_text(bars))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.csv")
            write_file(path, bars, dialect)
            records, bad, read_dialect = read_file(path)
        self.assertEqual(bad, [])
        self.assertEqual(len(records), 30)
        self.assertEqual(read_dialect.kind, "bars")
        for original, restored in zip(bars, records):
            self.assertEqual(original.ts, restored.ts)
            self.assertAlmostEqual(original.open, restored.open, places=5)
            self.assertAlmostEqual(original.close, restored.close, places=5)
            self.assertAlmostEqual(original.tickvol, restored.tickvol)

    def test_synthetic_column_written(self):
        bars = make_bars(datetime(2023, 1, 2), 5)
        bars[2].synthetic = True
        dialect = sniff(to_mt5_text(bars))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.csv")
            write_file(path, bars, dialect, include_synthetic_flag=True)
            with open(path) as handle:
                content = handle.read()
        self.assertIn("<SYNTHETIC>", content)
        self.assertEqual(content.count("\t1\n"), 1)


class TestCli(unittest.TestCase):
    def _write(self, tmp, bars, name="EURUSD_M1.csv"):
        path = os.path.join(tmp, name)
        with open(path, "w") as handle:
            handle.write(to_mt5_text(bars))
        return path

    def test_check_only_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, make_bars(datetime(2023, 1, 2), 100))
            before = os.path.getmtime(path)
            code = main([path, "--quiet"])
            self.assertEqual(code, 0)
            self.assertEqual(os.listdir(tmp), ["EURUSD_M1.csv"])
            self.assertEqual(os.path.getmtime(path), before)

    def test_clean_writes_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            bars = make_bars(datetime(2023, 1, 2), 400)
            del bars[100:103]
            path = self._write(tmp, bars)
            out = os.path.join(tmp, "clean.csv")
            code = main([path, "--out", out, "--fill-gaps", "ffill", "--quiet"])
            self.assertEqual(code, 0)
            records, bad, _ = read_file(out)
            self.assertEqual(bad, [])
            self.assertEqual(len(records), 400)

    def test_json_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            bars = make_bars(datetime(2023, 1, 2), 400)
            del bars[100:103]
            path = self._write(tmp, bars)
            json_path = os.path.join(tmp, "report.json")
            main([path, "--json", json_path, "--quiet"])
            with open(json_path) as handle:
                payload = json.load(handle)
        self.assertEqual(payload["records"], 397)
        self.assertEqual(payload["timeframe"], "M1")
        self.assertEqual(payload["missing_bars"], 3)
        self.assertEqual(len(payload["gaps"]), 1)

    def test_fail_on_error_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            bars = make_bars(datetime(2023, 1, 2), 400)
            del bars[100:103]
            path = self._write(tmp, bars)
            self.assertEqual(main([path, "--quiet"]), 0)
            self.assertEqual(main([path, "--quiet", "--fail-on-error"]), 1)

    def test_missing_file_returns_two(self):
        self.assertEqual(main(["/definitely/not/here.csv", "--quiet"]), 2)

    def test_out_dir_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self._write(tmp, make_bars(datetime(2023, 1, 2), 50), "A_M1.csv")
            second = self._write(tmp, make_bars(datetime(2023, 1, 2), 50), "B_M1.csv")
            out_dir = os.path.join(tmp, "cleaned")
            code = main([first, second, "--out-dir", out_dir, "--quiet"])
            self.assertEqual(code, 0)
            self.assertEqual(
                sorted(os.listdir(out_dir)), ["A_M1_clean.csv", "B_M1_clean.csv"]
            )

    def test_out_with_multiple_inputs_rejected(self):
        with self.assertRaises(SystemExit):
            main(["a.csv", "b.csv", "--out", "x.csv"])


if __name__ == "__main__":
    unittest.main()
