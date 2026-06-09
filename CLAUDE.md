# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`hello-cli` is a minimal Python command-line greeter. It has a single entry point (`hello.py`) with no external dependencies.

## Running the CLI

```bash
python hello.py
python hello.py --name Alice
```

## Code Structure

**`hello.py`** — original CLI greeter.  
- `greet(name: str) -> str` — pure function that returns the greeting string
- `main()` — parses `--name` (default: `"World"`) via `argparse` and prints the result

**`databank.py`** — Strategy Quant x Databank: reads a CSV of daily strategy returns and produces a per-year summary with a `return_drawdown_ratio` column.

### Databank: CSV format

```
date,daily_return
2023-01-03,0.0042
2023-01-04,-0.0018
```

`date` is ISO-8601; `daily_return` is a decimal (0.01 = +1 %).

### Databank: public API

```python
from databank import load, build_yearly_summary, print_summary

rows    = load("strategy.csv")          # list[DailyRow], sorted by date
summary = build_yearly_summary(rows)    # list[YearlySummary]
print_summary(summary)
```

`YearlySummary` fields: `year`, `cagr`, `max_drawdown`, `return_drawdown_ratio`.

### Databank: metric definitions

| Field | Definition |
|---|---|
| `cagr` | Compound annual growth rate assuming 252 trading days/year |
| `max_drawdown` | Maximum peak-to-trough drawdown within the calendar year (positive decimal) |
| `return_drawdown_ratio` | `cagr / max_drawdown`; `math.inf` when drawdown is zero |

## Running tests

```bash
python3 -m pytest test_databank.py -v
```
