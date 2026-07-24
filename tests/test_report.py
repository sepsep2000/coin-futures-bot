import math

import pandas as pd
import pytest

from src.backtest.engine import BacktestResult, Trade
from src.backtest.report import (
    compute_avg_r_multiple,
    compute_max_drawdown_pct,
    compute_median_r_multiple,
    compute_metrics,
    compute_monthly_returns_pct,
    compute_profit_factor,
    compute_sharpe_annualized,
    compute_win_rate_pct,
    render_report_html,
)


def _trade(pnl, r_multiple=None, entry_time=None, exit_time=None) -> Trade:
    return Trade(
        symbol="BTC/USDT:USDT", strategy="trend", direction="long",
        entry_time=entry_time or pd.Timestamp("2024-01-01"),
        exit_time=exit_time or pd.Timestamp("2024-01-02"),
        entry_price=100.0, exit_price=100.0 + pnl, qty=1.0,
        pnl_usd=pnl, r_multiple=r_multiple if r_multiple is not None else pnl / 10,
        exit_reason="stop_loss", fees_usd=0.0, funding_usd=0.0,
    )


# --- profit factor ---

def test_profit_factor_known_answer():
    trades = [_trade(100), _trade(200), _trade(-50), _trade(-50)]
    assert compute_profit_factor(trades) == pytest.approx(3.0)


def test_profit_factor_inf_when_no_losses():
    assert compute_profit_factor([_trade(100), _trade(50)]) == math.inf


def test_profit_factor_none_when_no_trades():
    assert compute_profit_factor([]) is None


def test_profit_factor_zero_when_all_losses():
    assert compute_profit_factor([_trade(-10), _trade(-20)]) == pytest.approx(0.0)


# --- win rate ---

def test_win_rate_known_answer():
    trades = [_trade(10), _trade(-5), _trade(20), _trade(-1)]
    assert compute_win_rate_pct(trades) == pytest.approx(50.0)


def test_win_rate_none_when_empty():
    assert compute_win_rate_pct([]) is None


# --- avg / median R ---

def test_avg_and_median_r_multiple():
    trades = [_trade(0, r_multiple=1.0), _trade(0, r_multiple=2.0), _trade(0, r_multiple=3.0)]
    assert compute_avg_r_multiple(trades) == pytest.approx(2.0)
    assert compute_median_r_multiple(trades) == pytest.approx(2.0)


def test_avg_r_multiple_none_when_empty():
    assert compute_avg_r_multiple([]) is None
    assert compute_median_r_multiple([]) is None


# --- max drawdown ---

def test_max_drawdown_known_answer():
    curve = [(pd.Timestamp("2024-01-01"), 100), (pd.Timestamp("2024-01-02"), 120),
             (pd.Timestamp("2024-01-03"), 90), (pd.Timestamp("2024-01-04"), 110)]
    assert compute_max_drawdown_pct(curve) == pytest.approx(25.0)  # (120-90)/120*100


def test_max_drawdown_none_when_empty():
    assert compute_max_drawdown_pct([]) is None


def test_max_drawdown_zero_when_monotonic_increase():
    curve = [(pd.Timestamp("2024-01-01"), 100), (pd.Timestamp("2024-01-02"), 110)]
    assert compute_max_drawdown_pct(curve) == pytest.approx(0.0)


# --- sharpe ---

def test_sharpe_annualized_matches_manual_formula():
    curve = [(pd.Timestamp("2024-01-01") + pd.Timedelta(hours=i), v) for i, v in enumerate([100, 101, 99, 103, 102])]
    result = compute_sharpe_annualized(curve, bars_per_year=365)
    equities = pd.Series([v for _, v in curve])
    returns = equities.pct_change().dropna()
    expected = returns.mean() / returns.std(ddof=0) * math.sqrt(365)
    assert result == pytest.approx(expected)


def test_sharpe_none_when_insufficient_data():
    assert compute_sharpe_annualized([(pd.Timestamp("2024-01-01"), 100)]) is None


def test_sharpe_none_when_zero_volatility():
    curve = [(pd.Timestamp("2024-01-01"), 100), (pd.Timestamp("2024-01-02"), 100)]
    assert compute_sharpe_annualized(curve) is None


# --- 월별 수익률 ---

def test_monthly_returns_pct_known_answer():
    curve = [
        (pd.Timestamp("2024-01-01"), 100.0),
        (pd.Timestamp("2024-01-31"), 110.0),
        (pd.Timestamp("2024-02-28"), 121.0),
    ]
    monthly = compute_monthly_returns_pct(curve)
    assert monthly.iloc[0] == pytest.approx(10.0)   # 100->110
    assert monthly.iloc[1] == pytest.approx(10.0)   # 110->121


def test_monthly_returns_pct_empty_when_no_curve():
    assert compute_monthly_returns_pct([]).empty


# --- 통합 + HTML 렌더링 ---

def test_compute_metrics_and_render_html_smoke():
    trades = [_trade(100, r_multiple=1.5), _trade(-50, r_multiple=-0.5)]
    curve = [(pd.Timestamp("2024-01-01"), 10000), (pd.Timestamp("2024-01-02"), 10050)]
    result = BacktestResult(trades=trades, equity_curve=curve, final_equity=10050)
    metrics = compute_metrics(result)
    assert metrics.n_trades == 2

    monthly = compute_monthly_returns_pct(curve)
    html = render_report_html(result, metrics, monthly, title="Test Report")
    assert "Test Report" in html
    assert "거래수" in html
    assert "Profit Factor" in html
    assert "<svg" in html
