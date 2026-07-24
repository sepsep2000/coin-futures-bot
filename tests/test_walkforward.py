import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import Trade
from src.backtest.walkforward import (
    G2Result,
    WalkforwardResult,
    WindowResult,
    compute_monthly_positive_ratio,
    evaluate_g2,
    evaluate_g3,
    generate_windows,
    monte_carlo_max_drawdowns,
)


def _trade(pnl, exit_time, entry_time=None) -> Trade:
    return Trade(
        symbol="BTC/USDT:USDT", strategy="trend", direction="long",
        entry_time=entry_time or (exit_time - pd.Timedelta(hours=1)), exit_time=exit_time,
        entry_price=100.0, exit_price=100.0 + pnl, qty=1.0,
        pnl_usd=pnl, r_multiple=pnl / 10, exit_reason="stop_loss", fees_usd=0.0, funding_usd=0.0,
    )


# --- generate_windows ---

def test_generate_windows_produces_expected_boundaries():
    windows = generate_windows("2023-01-01", "2023-11-01", train_months=6, test_months=2)
    assert len(windows) == 2
    w1 = windows[0]
    assert w1.train_start == pd.Timestamp("2023-01-01", tz="UTC")
    assert w1.train_end == pd.Timestamp("2023-07-01", tz="UTC")
    assert w1.test_start == pd.Timestamp("2023-07-01", tz="UTC")
    assert w1.test_end == pd.Timestamp("2023-09-01", tz="UTC")
    w2 = windows[1]
    assert w2.train_start == pd.Timestamp("2023-03-01", tz="UTC")  # 2개월(test_months) 롤
    assert w2.test_end == pd.Timestamp("2023-11-01", tz="UTC")


def test_generate_windows_empty_when_range_too_short():
    assert generate_windows("2023-01-01", "2023-05-01", train_months=6, test_months=2) == []


def test_generate_windows_ids_are_sequential():
    windows = generate_windows("2023-01-01", "2024-03-01", train_months=6, test_months=2)
    assert [w.window_id for w in windows] == list(range(1, len(windows) + 1))


# --- compute_monthly_positive_ratio ---

def test_monthly_positive_ratio_known_answer():
    trades = [
        _trade(10, pd.Timestamp("2024-01-15", tz="UTC")),
        _trade(-5, pd.Timestamp("2024-01-20", tz="UTC")),   # 1월 합계 +5 -> 양수
        _trade(-20, pd.Timestamp("2024-02-10", tz="UTC")),  # 2월 합계 -20 -> 음수
    ]
    assert compute_monthly_positive_ratio(trades) == pytest.approx(50.0)


def test_monthly_positive_ratio_none_when_empty():
    assert compute_monthly_positive_ratio([]) is None


# --- evaluate_g2 ---

def _make_result(trades: list[Trade], initial_equity: float = 10_000) -> WalkforwardResult:
    sorted_trades = sorted(trades, key=lambda t: t.exit_time)
    equity = initial_equity
    curve = [(sorted_trades[0].entry_time, equity)] if sorted_trades else []
    for t in sorted_trades:
        equity += t.pnl_usd
        curve.append((t.exit_time, equity))
    return WalkforwardResult(windows=[], all_oos_trades=trades, stitched_equity_curve=curve)


GATES_CFG = {
    "g2_min_oos_trades": 3, "g2_min_profit_factor": 1.3, "g2_max_drawdown_pct": 15,
    "g2_min_positive_month_pct": 55, "g3_monte_carlo_runs": 200, "g3_max_drawdown_p95_pct": 20,
}


def test_g2_passes_when_all_criteria_met():
    trades = [_trade(100, pd.Timestamp(f"2024-0{m}-01", tz="UTC")) for m in range(1, 4)]
    result = _make_result(trades)
    g2 = evaluate_g2(result, GATES_CFG)
    assert g2.passed is True
    assert g2.failures == []


def test_g2_fails_on_insufficient_trades():
    trades = [_trade(100, pd.Timestamp("2024-01-01", tz="UTC"))]
    g2 = evaluate_g2(_make_result(trades), GATES_CFG)
    assert g2.passed is False
    assert any("거래수" in f for f in g2.failures)


def test_g2_fails_on_low_profit_factor():
    trades = [_trade(-100, pd.Timestamp(f"2024-0{m}-01", tz="UTC")) for m in range(1, 4)]
    g2 = evaluate_g2(_make_result(trades), GATES_CFG)
    assert g2.passed is False
    assert any("PF" in f for f in g2.failures)


def test_g2_fails_on_excessive_drawdown():
    trades = [
        _trade(1000, pd.Timestamp("2024-01-01", tz="UTC")),
        _trade(-3000, pd.Timestamp("2024-01-15", tz="UTC")),
        _trade(2500, pd.Timestamp("2024-01-20", tz="UTC")),
    ]
    g2 = evaluate_g2(_make_result(trades, initial_equity=10_000), GATES_CFG)
    assert g2.passed is False
    assert any("MaxDD" in f for f in g2.failures)


# --- monte_carlo_max_drawdowns / evaluate_g3 ---

def test_monte_carlo_reproducible_with_fixed_seed():
    trades = [_trade(p, pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(days=i)) for i, p in enumerate([50, -30, 80, -60, 40])]
    a = monte_carlo_max_drawdowns(trades, 10_000, n_runs=100, seed=42)
    b = monte_carlo_max_drawdowns(trades, 10_000, n_runs=100, seed=42)
    np.testing.assert_array_equal(a, b)


def test_monte_carlo_zero_drawdown_when_all_gains():
    trades = [_trade(p, pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(days=i)) for i, p in enumerate([10, 20, 30])]
    maxdds = monte_carlo_max_drawdowns(trades, 10_000, n_runs=50, seed=1)
    assert (maxdds == 0).all()  # 전부 이익이면 순서를 어떻게 섞어도 드로다운 없음


def test_monte_carlo_empty_when_no_trades():
    assert monte_carlo_max_drawdowns([], 10_000, n_runs=10).size == 0


def test_g3_passes_when_drawdown_within_threshold():
    trades = [_trade(p, pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(days=i)) for i, p in enumerate([50, 40, 60, 30])]
    result = WalkforwardResult(windows=[], all_oos_trades=trades, stitched_equity_curve=[])
    g3 = evaluate_g3(result, initial_equity=10_000, gates_cfg=GATES_CFG)
    assert g3.passed is True


def test_g3_fails_when_drawdown_exceeds_threshold():
    trades = [_trade(p, pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(days=i)) for i, p in enumerate([-2000, -1800, 100])]
    result = WalkforwardResult(windows=[], all_oos_trades=trades, stitched_equity_curve=[])
    g3 = evaluate_g3(result, initial_equity=10_000, gates_cfg=GATES_CFG)
    assert g3.passed is False


def test_g3_fails_when_no_trades():
    result = WalkforwardResult(windows=[], all_oos_trades=[], stitched_equity_curve=[])
    g3 = evaluate_g3(result, initial_equity=10_000, gates_cfg=GATES_CFG)
    assert g3.passed is False
