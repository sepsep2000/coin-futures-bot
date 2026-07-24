"""실데이터 슬라이스로 run_backtest()를 통합 검증 + G1 무결성 체크
(룩어헤드 방지, 수수료 0 vs 실비용 비교)."""

from pathlib import Path

import pandas as pd
import pytest

from src.backtest.engine import SymbolData, run_backtest

PROJECT_ROOT = Path(__file__).parent.parent
DATA_AVAILABLE = (PROJECT_ROOT / "data" / "ohlcv" / "BTCUSDT-USDT_15m.parquet").exists()

CFG = {
    # 2026-07-24 SPEC 4절 재설계: meanrev 기각 -> trend 단일.
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
        "low_vol_filter": True, "stop_atr_mult": 2.0, "require_confirmation_bar": True, "stop_grace_period_bars": 4,
        "partial_tp_r_multiple": 1.5, "partial_tp_pct": 25, "breakeven_after_partial": True,
        "chandelier_period": 22, "chandelier_atr_mult": 3.0, "time_exit_bars": 96, "time_exit_min_r": 1.0,
    },
    "meanrev": {
        "bb_period": 20, "bb_std": 2.0, "rsi_period": 14, "rsi_oversold": 30, "rsi_overbought": 70,
        "atr_period": 14, "stop_atr_mult": 1.2, "limit_cancel_after_bars": 2, "time_exit_bars": 24,
    },
}


def _load_symbol_data(symbol_file: str, start: str, end: str) -> SymbolData:
    ohlcv_15m = pd.read_parquet(PROJECT_ROOT / "data" / "ohlcv" / f"{symbol_file}_15m.parquet")
    ohlcv_1h = pd.read_parquet(PROJECT_ROOT / "data" / "ohlcv" / f"{symbol_file}_1h.parquet")
    funding = pd.read_parquet(PROJECT_ROOT / "data" / "funding" / f"{symbol_file}_funding.parquet")

    def _to_dt_index(df):
        df = df.copy()
        df.index = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df.drop(columns=["timestamp"])

    df_15m = _to_dt_index(ohlcv_15m)
    df_1h = _to_dt_index(ohlcv_1h)
    df_15m = df_15m.loc[start:end]
    df_1h = df_1h.loc[start:end]
    return SymbolData(df_15m=df_15m, df_1h=df_1h, funding=funding)


pytestmark = pytest.mark.skipif(not DATA_AVAILABLE, reason="실데이터 parquet 캐시가 없음 (scripts/collect_data.py 먼저 실행 필요)")


@pytest.fixture(scope="module")
def btc_3m():
    return _load_symbol_data("BTCUSDT-USDT", "2023-01-01", "2023-04-01")


def test_meanrev_disabled_produces_no_meanrev_trades(btc_3m):
    """SPEC 4절 기각 결정(2026-07-24) — active_strategies=["trend"]일 때
    RANGE 레짐이어도 meanrev 트레이드가 전혀 생기면 안 된다."""
    result = run_backtest({"BTC/USDT:USDT": btc_3m}, CFG)
    assert all(t.strategy != "meanrev" for t in result.trades)


def test_run_backtest_produces_valid_result_shape(btc_3m):
    result = run_backtest({"BTC/USDT:USDT": btc_3m}, CFG)
    assert len(result.equity_curve) > 0
    assert result.final_equity > 0
    for t in result.trades:
        assert t.entry_time < t.exit_time
        assert t.qty > 0
        assert t.exit_reason in ("stop_loss", "partial_tp", "time_stop", "profit_target", "regime_flip")


# --- G1: 룩어헤드 방지 ---

def test_backtest_is_lookahead_free(btc_3m):
    """데이터 뒤쪽 절반을 잘라내도, 잘라낸 시점 이전에 '완전히 종료된' 트레이드는
    두 실행에서 완전히 동일해야 한다(엔트리/청산 가격·시각·사유 전부 일치)."""
    full_result = run_backtest({"BTC/USDT:USDT": btc_3m}, CFG)

    cutoff = btc_3m.df_15m.index[len(btc_3m.df_15m) // 2]
    truncated_data = SymbolData(
        df_15m=btc_3m.df_15m.loc[:cutoff],
        df_1h=btc_3m.df_1h.loc[:cutoff],
        funding=btc_3m.funding,
    )
    truncated_result = run_backtest({"BTC/USDT:USDT": truncated_data}, CFG)

    full_closed_before_cutoff = [t for t in full_result.trades if t.exit_time <= cutoff]
    truncated_closed_before_cutoff = [t for t in truncated_result.trades if t.exit_time <= cutoff]

    assert len(full_closed_before_cutoff) == len(truncated_closed_before_cutoff)
    for a, b in zip(full_closed_before_cutoff, truncated_closed_before_cutoff):
        assert a.entry_time == b.entry_time
        assert a.exit_time == b.exit_time
        assert a.entry_price == pytest.approx(b.entry_price)
        assert a.exit_price == pytest.approx(b.exit_price)
        assert a.exit_reason == b.exit_reason


# --- G1: 수수료 0 vs 실비용 비교 ---

def test_zero_cost_backtest_outperforms_or_equals_real_cost(btc_3m):
    """비용(수수료+슬리피지)은 성과를 갉아먹기만 해야 한다 — 0비용 실행의 총 손익이
    실비용 실행보다 항상 크거나 같아야 한다."""
    zero_cost_cfg = {**CFG, "costs": {**CFG["costs"], "taker_fee_pct": 0, "maker_fee_pct": 0, "slippage_pct": 0}}

    real_result = run_backtest({"BTC/USDT:USDT": btc_3m}, CFG)
    zero_result = run_backtest({"BTC/USDT:USDT": btc_3m}, zero_cost_cfg)

    real_total_pnl = sum(t.pnl_usd for t in real_result.trades)
    zero_total_pnl = sum(t.pnl_usd for t in zero_result.trades)

    assert zero_total_pnl >= real_total_pnl
    assert all(t.fees_usd == pytest.approx(0.0) for t in zero_result.trades)
