# hello-cli

A simple command-line greeter written in Python, plus **mt5clean** — a checker
and repairer for MetaTrader 5 data exports.

## Installation

```bash
pip install -e .
```

`mt5clean` has no third-party dependencies, so you can also just run it from a
checkout with `python -m mt5clean`.

## hello

```bash
python hello.py
python hello.py --name Alice
```

## mt5clean

MetaTrader history exports are rarely clean. Brokers backfill bars
inconsistently, re-exports duplicate timestamps, interrupted writes truncate
rows, and a single bad tick can leave a spike that quietly ruins a backtest.
`mt5clean` reports those problems and, when asked, writes a repaired copy.

```bash
# Check a file — this never modifies anything
mt5clean EURUSD_M1.csv

# Check, then write a repaired copy
mt5clean EURUSD_M1.csv --out EURUSD_M1_clean.csv --repair-ohlc --fill-gaps ffill

# Clean a whole folder of exports
mt5clean data/*.csv --out-dir cleaned/ --drop-invalid --fill-gaps ffill

# Fail a pipeline when the data has errors
mt5clean EURUSD_M1.csv --json report.json --fail-on-error
```

Try it against the bundled sample, which has defects deliberately baked in:

```bash
python examples/make_sample.py examples/EURUSD_M1_sample.csv
mt5clean examples/EURUSD_M1_sample.csv
```

### Input formats

Detected automatically — delimiter, header style and record type are all
sniffed, so you can point it at whatever your platform produced:

- MT5 bar exports (tab-separated, `<DATE> <TIME> <OPEN> … <SPREAD>` headers)
- MT5 tick exports (`<BID> <ASK> <LAST> <VOLUME> <FLAGS>`, millisecond stamps)
- MT4 history-center CSVs (headerless, comma-separated)
- Semicolon-separated files using comma decimal marks
- Files with a single combined datetime column

### What it checks

| Check | Meaning |
| --- | --- |
| Gaps | Missing bars **inside trading hours** (see below) |
| Duplicate timestamps | Split into identical rows vs. conflicting values |
| Out-of-order rows | Timestamps that go backwards |
| Invalid OHLC | `high < low`, extremes not bracketing open/close, non-positive prices |
| Invalid ticks | Crossed quotes (`ask < bid`), quotes with no positive price |
| Price spikes | Moves far outside the instrument's normal range |
| Frozen feed | Long runs of identical bars with no movement |
| Zero volume | Bars that moved in price but recorded no ticks |
| Spread anomalies | Spreads dwarfing the instrument's median |
| Unreadable rows | Truncated or corrupt lines, reported rather than crashed on |

### Gaps and trading hours

The hard part of gap detection is not finding holes, it is knowing which holes
matter. Naively requiring a bar at every timeframe step reports every weekend,
every nightly broker break and every holiday as a fault.

So `mt5clean` learns the instrument's calendar from the file itself: it buckets
bars by weekday and hour, and treats a bucket that is consistently populated as
trading time and one that is consistently empty as market close. Missing bars
are only reported inside buckets that normally trade. Tune the sensitivity with
`--session-threshold`, or disable it with `--no-sessions` to check against a
strict 24/7 grid.

`Session coverage` in the report is the share of expected in-session bars that
are actually present — a quick quality score for a history file.

### Repairs

No repair runs unless you ask for one, and nothing is written unless you pass
`--out` or `--out-dir`. Every change is counted in the report.

| Flag | Effect |
| --- | --- |
| *(default)* | Sort by timestamp and collapse duplicate timestamps, keeping the last |
| `--dedupe first\|drop\|none` | Keep the earlier row, drop all conflicting copies, or leave duplicates alone |
| `--repair-ohlc` | Widen high/low so they bracket open and close |
| `--drop-invalid` | Remove bars/ticks that fail validation outright |
| `--fill-gaps ffill` | Fill short gaps with flat zero-volume bars at the previous close |
| `--fill-max-bars N` | Never fill a gap longer than N bars (default 5) |
| `--drop-spikes` | Remove isolated bad prints |
| `--mark-synthetic` | Add a `<SYNTHETIC>` column marking filled bars |
| `--shift-minutes N` | Shift timestamps, e.g. `-120` to move a UTC+2 broker to UTC |

Two deliberate limits on how aggressive the repairs get:

**Long gaps are not filled.** A 90-minute hole is missing history that should
be re-downloaded, not invented. Only gaps up to `--fill-max-bars` are filled,
and filled bars are flat with zero volume — no fabricated price movement, no
fabricated liquidity.

**Only reverting spikes are dropped.** A bad print shows up as a jump away from
the price followed immediately by a jump back; the corrupt bar is the first
one, and the bar after it is good data. A large move with *no* reversal is a
real repricing — a weekend open, a rate decision — so it is reported but never
removed.

### Exit codes

`0` clean, `2` a file could not be read. Add `--fail-on-error` or
`--fail-on-warning` to also get `1` when findings are present, for CI use.

## Tests

```bash
python -m unittest discover -s tests
```

## Licence

MIT
