"""
Strategy Quant x Databank
=========================
Reads a CSV of daily strategy returns and builds a per-year summary table
that includes a ``return_drawdown_ratio`` column (CAGR / max drawdown).

Expected CSV columns
--------------------
date        : ISO-8601 date string (YYYY-MM-DD)
daily_return: decimal daily return, e.g. 0.012 for +1.2 %

Usage
-----
    from databank import load, build_yearly_summary
    df = load("strategy.csv")
    summary = build_yearly_summary(df)
    print(summary)
"""

import csv
import math
from datetime import date
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class DailyRow(NamedTuple):
    date: date
    daily_return: float


class YearlySummary(NamedTuple):
    year: int
    cagr: float          # annualised compound return (decimal)
    max_drawdown: float  # maximum peak-to-trough drawdown (positive decimal)
    return_drawdown_ratio: float  # cagr / max_drawdown; inf when drawdown == 0


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load(path: str) -> list[DailyRow]:
    """Load a CSV file and return a list of DailyRow sorted by date."""
    rows: list[DailyRow] = []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        for line in reader:
            rows.append(DailyRow(
                date=date.fromisoformat(line["date"].strip()),
                daily_return=float(line["daily_return"]),
            ))
    rows.sort(key=lambda r: r.date)
    return rows


# ---------------------------------------------------------------------------
# Core calculations
# ---------------------------------------------------------------------------

def _equity_curve(daily_returns: list[float]) -> list[float]:
    """Convert daily returns to a cumulative equity curve starting at 1.0."""
    curve = [1.0]
    for r in daily_returns:
        curve.append(curve[-1] * (1 + r))
    return curve


def _max_drawdown(equity: list[float]) -> float:
    """Maximum peak-to-trough drawdown as a positive decimal (0–1)."""
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _cagr(equity: list[float], trading_days: int) -> float:
    """Annualised compound return assuming 252 trading days per year."""
    if trading_days == 0 or equity[0] == 0:
        return 0.0
    total_return = equity[-1] / equity[0]
    years = trading_days / 252
    return total_return ** (1 / years) - 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_yearly_summary(rows: list[DailyRow]) -> list[YearlySummary]:
    """
    Group daily rows by calendar year and return one YearlySummary per year.

    return_drawdown_ratio = CAGR / max_drawdown
    When max_drawdown is 0 the ratio is math.inf (no loss ever occurred).
    """
    # Group by year
    by_year: dict[int, list[float]] = {}
    for row in rows:
        by_year.setdefault(row.date.year, []).append(row.daily_return)

    summaries: list[YearlySummary] = []
    for year in sorted(by_year):
        returns = by_year[year]
        equity = _equity_curve(returns)
        cagr = _cagr(equity, len(returns))
        mdd = _max_drawdown(equity)
        ratio = cagr / mdd if mdd > 0 else math.inf
        summaries.append(YearlySummary(
            year=year,
            cagr=round(cagr, 6),
            max_drawdown=round(mdd, 6),
            return_drawdown_ratio=round(ratio, 4) if math.isfinite(ratio) else ratio,
        ))

    return summaries


def print_summary(summaries: list[YearlySummary]) -> None:
    """Pretty-print the yearly summary table."""
    header = f"{'Year':>6}  {'CAGR':>10}  {'Max DD':>10}  {'Ret/DD':>10}"
    print(header)
    print("-" * len(header))
    for s in summaries:
        ratio_str = f"{s.return_drawdown_ratio:.4f}" if math.isfinite(s.return_drawdown_ratio) else "inf"
        print(
            f"{s.year:>6}  "
            f"{s.cagr * 100:>9.2f}%  "
            f"{s.max_drawdown * 100:>9.2f}%  "
            f"{ratio_str:>10}"
        )
