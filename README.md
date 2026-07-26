# hello-cli

[![tests](https://github.com/GaryAitcheson/hello-cli/actions/workflows/tests.yml/badge.svg)](https://github.com/GaryAitcheson/hello-cli/actions/workflows/tests.yml)

A simple command-line greeter written in Python, plus **mt5clean** — a data
cleaner for MetaTrader 5 M1 bar exports.

## Installation

```bash
pip install -e .
```

No third-party dependencies. `mt5clean` is stdlib-only, so it runs on whatever
Python is already sitting next to your MT5 install.

## hello

```bash
python hello.py
python hello.py --name Alice
```

---

# mt5clean

MT5 exports lie to you in quiet ways: minutes go missing over a broker outage,
a bar arrives with its high below its close, the same timestamp shows up twice
after a re-download, a feed glitch prints a 200-pip candle that never traded.
Backtest on that and the equity curve is fiction.

`mt5clean` reads the export, tells you exactly what is wrong with it, and — only
when you ask — writes a repaired copy ready for StrategyQuant X.

```bash
mt5clean                               # open the window and pick a file
mt5clean info   EURUSD_M1.csv          # what format is this file?
mt5clean audit  EURUSD_M1.csv          # what is wrong with it?  (writes nothing)
mt5clean clean  EURUSD_M1.csv -o EURUSD_sqx.csv --dedupe last --fix-ohlc
```

## The window

Run `mt5clean` with no arguments (or double-click the single-file build) and you
get a file picker instead of a command line:

- **Browse** to any MT5 export; the detected format appears under the path, so
  you can catch a misread delimiter or date order before running anything.
- **Audit** runs on a worker thread — a decade of M1 takes a while, and the
  window stays responsive with a progress bar rather than appearing hung.
- Findings land in tiles across the top (bars, coverage, errors, warnings,
  gaps) with the full text report underneath, errors in red and warnings amber.
- **Repair...** opens the fix list with the boxes **already ticked to match
  what this file actually needs**, each annotated with how many instances were
  found. Untick anything you would rather leave alone.
- **Save report** and **Export gaps CSV** write the same artefacts as the CLI's
  `--json-out` and `--gaps-csv`.

The repair dialog refuses to overwrite your original export. It is built on
tkinter, which ships with the python.org installer on Windows and macOS; on
Debian/Ubuntu it needs `sudo apt install python3-tk`. Without Tk the CLI is
unaffected.

## The default is to report, not to rewrite

`audit` never writes a file. Repairs happen only under `clean`, only for the
flags you pass, and every one of them is counted in the summary. Silently
altering price history is how you end up trusting a backtest you shouldn't.

## What it checks

**Structure**

| Finding | Meaning |
| --- | --- |
| `unparseable_row` | the row could not be read at all |
| `out_of_order` | timestamp goes backwards |
| `duplicate_conflicting` | same timestamp, *different* prices — a real problem |
| `duplicate_identical` | same timestamp, same prices — usually a double export |
| `off_grid` | an M1 stamp that isn't on a whole minute |

**Bar integrity**

| Finding | Meaning |
| --- | --- |
| `ohlc_invalid` | high/low don't contain open/close, or high < low |
| `non_positive_price` | zero or negative prices |
| `price_spike` | bar range far above the median (default: 30x) |
| `price_jump` | one-minute close-to-close move far above the median range |
| `flat_bar` | open == high == low == close |
| `frozen_feed` | a long run of byte-identical bars |
| `zero_volume` / `negative_volume` | no ticks, or an impossible count |
| `negative_spread` | spread below zero |

**Coverage**

Missing minutes, classified as `intraday`, `holiday`, `weekend` or
`session_break`, plus a month-by-month coverage table.

## How gaps are judged

The hard part of gap detection is knowing when the market was *supposed* to be
open. Hard-coding "FX trades Sunday 22:00 to Friday 22:00" is wrong for indices
and metals with daily breaks, wrong for brokers on a different GMT offset, and
wrong twice a year when DST moves.

So `mt5clean` doesn't assume. It builds a presence histogram over the 10,080
minutes of a week and treats a minute as tradable when bars turn up there in at
least half the weeks the file spans (tune with `--session-threshold`). The
session it derived is printed in every report:

```
Trading session (inferred from the data)
------------------------------------------------------------------------
derived from 13 weeks, threshold 50% of weeks per minute-of-week
6,900 tradable minutes per week
  Mon 22:00 -> Tue 20:59  (23.0h)
  ...
  Sun 22:00 -> Mon 20:59  (23.0h)

A gap is only counted against you when it lands inside these windows.
```

Weekends and the nightly rollover break are therefore never counted against
your coverage — only holes that fall inside hours the instrument actually
trades. Files spanning under three weeks are too short to infer anything
reliable; those fall back to Mon–Fri and say so.

## Repair flags

| Flag | Effect |
| --- | --- |
| `--dedupe first\|last` | collapse repeated timestamps |
| `--sort` | fix a non-monotonic file (buffers it in memory) |
| `--fix-ohlc` | clamp high/low so they contain open and close |
| `--drop-invalid` | drop broken bars instead of repairing them |
| `--off-grid snap\|drop` | handle stamps that aren't on a whole minute |
| `--fill-gaps` | insert flat zero-volume bars across short in-session holes |
| `--max-fill N` | longest gap `--fill-gaps` will invent (default 60 min) |
| `--drop-spikes` | remove bars past the spike threshold |
| `--session-only` | drop bars outside the detected session |
| `--tz-shift -3h` | re-base broker time to another offset |
| `--from` / `--to` | trim to a date range |

Gap filling is deliberately conservative: it only invents bars **inside** the
detected session, only for runs no longer than `--max-fill`, and the bars it
writes are flat with zero volume so they're obvious later. A missing holiday or
a multi-hour outage is left as a hole, because inventing a day of prices is
worse than having none.

Use `--dry-run` to see the plan without writing, and `--verify` to re-audit the
output and confirm the repairs landed.

## Output for StrategyQuant X

`--format sqx` (the default) writes what SQX's import wizard wants, and prints
the settings to type into it:

```
Date,Time,Open,High,Low,Close,Volume,Spread
2024.01.02,00:00:00,1.10432,1.10445,1.10430,1.10441,52,8
```

```
StrategyQuant X  ->  Data -> Import data -> from CSV file:
  Column separator : Comma (,)
  Date format      : yyyy.MM.dd
  Time format      : HH:mm:ss
  First row        : header (skip 1 line)
  Columns          : Date, Time, Open, High, Low, Close, Volume, Spread
  Price decimals   : 5  (set the symbol's point value to match)
  Timezone         : whatever your MT5 server used - the file is not converted
```

`--format mt5` writes the tab-separated layout MT5 itself produces, and
`--format csv` a generic ISO-8601 one.

Timestamps are never converted. MT5 gives you server time, and only you know
what offset your broker runs — use `--tz-shift` if you want it moved, and keep
the same choice across every symbol you import.

## Input formats understood

Delimiter, header style, column order, encoding and date order are all sniffed:

- `<DATE>	<TIME>	<OPEN>…` — the MT5 chart export
- `Date,Time,Open,High,Low,Close,Volume` — plain header
- `2024.01.02,00:00,1.10432,…` — headerless, MT4 style
- semicolon-delimited, comma-decimal, UTF-16 and BOM'd files
- `DD.MM.YYYY` dates, with `--date-order` to settle the ambiguous cases

Check what it decided with `mt5clean info` before trusting a big run.

## Scripting

`audit --json` emits the whole report as JSON, `--gaps-csv` writes every gap as
a row, and `--fail-on error` exits non-zero so a data refresh can gate on it:

```bash
mt5clean audit EURUSD_M1.csv --json-out audit.json --gaps-csv gaps.csv --fail-on error
```

Exit codes: `0` clean, `1` findings at or above `--fail-on`, `2` the file could
not be read.

As a library:

```python
from mt5clean import audit, render_text

result = audit("EURUSD_M1.csv")
print(f"{result.coverage:.2f}% of session minutes present")
for gap in result.gaps:
    if gap.kind == "intraday":
        print(gap.start_ts, gap.missing)
```

## Performance

Single sequential read, integer-minute arithmetic, and about 20 bytes of state
per bar. Five years of M1 (1.8M bars, 102 MB) audits in ~19 s using 65 MB of
RAM. Only `--sort` needs the file in memory.

## Sample data

`samples/EURUSD_M1_dirty.csv` is a synthetic 4-week M1 export with a known set
of defects baked in — a 3-hour hole, a missing day, duplicates, a broken bar, a
spike, an off-grid stamp and an out-of-order pair. Regenerate it with
`python tests/make_sample.py <path>`.

## Single-file build

For a machine that just needs to run the tool — a Windows box next to MT5, with
no install and nothing to keep in a folder together:

```bash
python tools/build_standalone.py -o mt5clean.py
python mt5clean.py audit XAUUSD_M1.csv
python mt5clean.py                        # or just double-click it: opens the GUI
```

That flattens the package into one stdlib-only script, GUI included. Tests
assert the build produces identical reports to the package, that it keeps
exactly one entry point, and that every subcommand survives the flatten — so
the two can't quietly diverge.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

CI runs the suite on Python 3.9 through 3.13 on Linux, plus one Windows job
(MT5 lives on Windows, and the reader does its own encoding and newline
handling), and a smoke test that audits the sample file and checks the repairs
clear every error-level finding.

## Licence

MIT
