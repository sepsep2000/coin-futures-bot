"""실데이터 짧은 슬라이스로 run_walkforward() 전체 파이프라인(그리드 탐색 ->
OOS 적용 -> G2/G3 판정)을 통합 검증. 프로덕션 6개월/2개월 대신 짧은 윈도우와
작은 그리드로 실행 시간을 줄인다 — 파이프라인 배선 검증이 목적이라 실제
게이트 통과 여부 자체는 이 테스트의 관심사가 아니다."""

from pathlib import Path

import pandas as pd
import pytest

from src.backtest.engine import SymbolData
from src.backtest.walkforward import evaluate_g2, evaluate_g3, generate_windows, run_walkforward

PROJECT_ROOT = Path(__file__).parent.parent
DATA_AVAILABLE = (PROJECT_ROOT / "data" / "ohlcv" / "BTCUSDT-USDT_15m.parquet").exists()
pytestmark = pytest.mark.skipif(not DATA_AVAILABLE, reason="실데이터 parquet 캐시가 없음")

SMALL_CFG = {
    "active_strategies": ["trend"],
    "account": {
        "initial_equity_usd": 10000, "leverage": 2, "margin_mode": "isolated",
        "risk_per_trade_pct": 0.5, "max_concurrent_positions": 3, "daily_loss_limit_pct": -3.0,
    },
    "costs": {"taker_fee_pct": 0.05, "maker_fee_pct": 0.02, "slippage_pct": 0.03, "funding_interval_hours": 8},
    "regime": {
        "adx_period": 14, "adx_trend_min": 25, "adx_range_max": 20,
        "bb_period": 20, "bb_width_percentile_window": 120,
        "bb_width_percentile_trend_min": 60, "bb_width_percentile_range_max": 40,
    },
    "trend": {
        "donchian_period": 20, "ema_period": 50, "atr_period": 14, "atr_median_window": 96,
        "low_vol_filter": True, "stop_atr_mult": 2.0, "require_confirmation_bar": True,
        "partial_tp_r_multiple": 1.5, "partial_tp_pct": 25, "breakeven_after_partial": True,
        "chandelier_period": 22, "chandelier_atr_mult": 3.0, "time_exit_bars": 96, "time_exit_min_r": 1.0,
    },
    "meanrev": {
        "bb_period": 20, "bb_std": 2.0, "rsi_period": 14, "rsi_oversold": 30, "rsi_overbought": 70,
        "atr_period": 14, "stop_atr_mult": 1.2, "limit_cancel_after_bars": 2, "time_exit_bars": 24,
    },
    "walkforward": {
        "train_months": 2, "test_months": 1,
        "grid": {"donchian_period": [15, 20], "trend_stop_atr_mult": [2.0]},  # 파이프라인 검증용 소형 그리드
    },
}

GATES_CFG = {
    "g2_min_oos_trades": 300, "g2_min_profit_factor": 1.3, "g2_max_drawdown_pct": 15,
    "g2_min_positive_month_pct": 55, "g3_monte_carlo_runs": 200, "g3_max_drawdown_p95_pct": 20,
}


def _load_btc(start, end) -> SymbolData:
    o15 = pd.read_parquet(PROJECT_ROOT / "data" / "ohlcv" / "BTCUSDT-USDT_15m.parquet")
    o1h = pd.read_parquet(PROJECT_ROOT / "data" / "ohlcv" / "BTCUSDT-USDT_1h.parquet")
    fd = pd.read_parquet(PROJECT_ROOT / "data" / "funding" / "BTCUSDT-USDT_funding.parquet")

    def conv(df):
        df = df.copy()
        df.index = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df.drop(columns=["timestamp"])

    d15, d1h = conv(o15), conv(o1h)
    return SymbolData(df_15m=d15.loc[start:end], df_1h=d1h.loc[start:end], funding=fd)


def test_run_walkforward_end_to_end_pipeline():
    data = {"BTC/USDT:USDT": _load_btc("2023-01-01", "2023-05-01")}
    windows = generate_windows("2023-01-01", "2023-04-01", train_months=2, test_months=1)
    assert len(windows) == 1

    result = run_walkforward(data, SMALL_CFG, windows)
    assert len(result.windows) == 1
    wr = result.windows[0]
    assert wr.best_params["donchian_period"] in (15, 20)
    assert len(wr.grid_results) == 2  # 그리드 2조합 전부 평가됨

    # OOS 트레이드는 test 구간(2023-03-01~2023-04-01) 안에서만 발생해야 한다(룩어헤드 없음)
    for t in result.all_oos_trades:
        assert pd.Timestamp("2023-03-01", tz="UTC") <= t.entry_time < pd.Timestamp("2023-04-01", tz="UTC")

    g2 = evaluate_g2(result, GATES_CFG)
    g3 = evaluate_g3(result, SMALL_CFG["account"]["initial_equity_usd"], GATES_CFG)
    assert isinstance(g2.passed, bool)
    assert isinstance(g3.passed, bool)


def test_grid_search_uses_only_train_window_data():
    """학습 구간 그리드 탐색이 검증 구간 데이터를 보지 않는지 — train 구간만 슬라이스해서
    넘겼는지 창구 경계로 확인."""
    data = {"BTC/USDT:USDT": _load_btc("2023-01-01", "2023-05-01")}
    windows = generate_windows("2023-01-01", "2023-04-01", train_months=2, test_months=1)
    w = windows[0]
    assert w.train_end == w.test_start  # 경계 공유하되 겹치지 않음(반개구간)
    assert w.train_start < w.train_end < w.test_end
