import math
import os
import tempfile

from databank import (
    DailyRow,
    _cagr,
    _equity_curve,
    _max_drawdown,
    build_yearly_summary,
    load,
)
from datetime import date


# ---------------------------------------------------------------------------
# Unit tests – pure helpers
# ---------------------------------------------------------------------------

def test_equity_curve_flat():
    curve = _equity_curve([0.0, 0.0, 0.0])
    assert curve == [1.0, 1.0, 1.0, 1.0]


def test_equity_curve_compounding():
    curve = _equity_curve([0.1, 0.1])
    assert abs(curve[-1] - 1.21) < 1e-9


def test_max_drawdown_no_loss():
    assert _max_drawdown([1.0, 1.1, 1.2]) == 0.0


def test_max_drawdown_simple():
    # Peak 1.2 -> trough 0.6 => 50 % drawdown
    dd = _max_drawdown([1.0, 1.2, 0.6])
    assert abs(dd - 0.5) < 1e-9


def test_cagr_one_year():
    # +10 % over 252 days -> CAGR should equal 0.10
    equity = [1.0, 1.10]
    assert abs(_cagr(equity, 252) - 0.10) < 1e-9


def test_cagr_zero_days():
    assert _cagr([1.0, 1.1], 0) == 0.0


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

def _write_csv(rows: list[tuple[str, float]], path: str) -> None:
    with open(path, "w") as fh:
        fh.write("date,daily_return\n")
        for d, r in rows:
            fh.write(f"{d},{r}\n")


def test_load_and_summary_single_year():
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "strategy.csv")
        # Build 252 flat-return days in 2023 giving ~0 % drawdown
        days = [f"2023-01-{d:02d}" for d in range(1, 29)]  # just use Jan for simplicity
        rows = [(d, 0.001) for d in days]
        _write_csv(rows, csv_path)

        data = load(csv_path)
        summary = build_yearly_summary(data)

        assert len(summary) == 1
        assert summary[0].year == 2023
        assert summary[0].cagr > 0
        assert summary[0].max_drawdown == 0.0
        assert summary[0].return_drawdown_ratio == math.inf


def test_summary_multi_year():
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "strategy.csv")
        rows = [
            ("2022-06-01", 0.005),
            ("2022-06-02", -0.010),
            ("2023-03-01", 0.008),
            ("2023-03-02", 0.002),
        ]
        _write_csv(rows, csv_path)

        summary = build_yearly_summary(load(csv_path))

        assert [s.year for s in summary] == [2022, 2023]
        # 2022 has a drawdown so ratio must be finite
        assert math.isfinite(summary[0].return_drawdown_ratio)
        # 2023 is all positive so drawdown == 0 -> ratio is inf
        assert summary[1].return_drawdown_ratio == math.inf


def test_return_drawdown_ratio_value():
    """Ratio = CAGR / max_drawdown; verify the arithmetic end-to-end."""
    rows = [
        DailyRow(date(2021, 1, 1), 0.02),
        DailyRow(date(2021, 1, 2), -0.01),
    ]
    summary = build_yearly_summary(rows)
    s = summary[0]
    expected = s.cagr / s.max_drawdown
    assert abs(s.return_drawdown_ratio - round(expected, 4)) < 1e-6


def test_load_sorted_regardless_of_csv_order():
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "strategy.csv")
        _write_csv([("2023-03-01", 0.01), ("2023-01-01", 0.02)], csv_path)
        data = load(csv_path)
        assert data[0].date < data[1].date
