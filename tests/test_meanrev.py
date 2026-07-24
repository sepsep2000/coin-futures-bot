import numpy as np
import pandas as pd
import pytest

from src.strategy.meanrev import (
    generate_meanrev_signals,
    regime_flip_exit_triggered,
    time_exit_triggered,
)

CFG = {
    "bb_period": 20, "bb_std": 2.0, "rsi_period": 14,
    "rsi_oversold": 30, "rsi_overbought": 70,
    "atr_period": 14, "stop_atr_mult": 1.2, "time_exit_bars": 24,
}


def _decline_df() -> pd.DataFrame:
    n = 60
    rng = np.random.default_rng(4)
    close = 100 + rng.normal(0, 0.3, n)
    close[-10:] = close[-11] - np.arange(1, 11) * 1.5  # 급락 -> RSI 과매도 + BB 하단 이탈
    high, low = close + 0.1, close - 0.1
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close})


def _rally_df() -> pd.DataFrame:
    n = 60
    rng = np.random.default_rng(4)
    close = 100 + rng.normal(0, 0.3, n)
    close[-10:] = close[-11] + np.arange(1, 11) * 1.5  # 급등 -> RSI 과매수 + BB 상단 이탈
    high, low = close + 0.1, close - 0.1
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close})


# --- BB + RSI 진입 조건 골든케이스 ---

def test_signal_long_appears_when_both_conditions_meet():
    df = _decline_df()
    sig = generate_meanrev_signals(df, CFG)
    # 급락 시작 직후(index -9)부터 RSI<=30과 BB 하단 이탈이 동시 성립 (사전 계산 확인됨)
    assert bool(sig["signal_long"].iloc[-9]) is True
    assert bool(sig["signal_long"].iloc[-10]) is False  # 급락 첫 봉엔 RSI 아직 30 초과
    assert not sig["signal_short"].any()


def test_signal_long_requires_both_bb_and_rsi_not_either_alone():
    df = _decline_df()
    sig = generate_meanrev_signals(df, CFG)
    below_bb_only = (df["close"] < sig["bb_lower"]) & (sig["rsi"] > CFG["rsi_oversold"])
    assert not (sig["signal_long"] & below_bb_only).any()  # BB만 이탈하고 RSI는 과매도 아니면 신호 없음


def test_signal_short_appears_when_both_conditions_meet():
    df = _rally_df()
    sig = generate_meanrev_signals(df, CFG)
    assert bool(sig["signal_short"].iloc[-9]) is True
    assert bool(sig["signal_short"].iloc[-10]) is False
    assert not sig["signal_long"].any()


def test_target_price_is_bb_center_line():
    df = _decline_df()
    sig = generate_meanrev_signals(df, CFG)
    pd.testing.assert_series_equal(sig["target_price"], sig["sma"], check_names=False)


def test_initial_stop_known_answer():
    df = _decline_df()
    sig = generate_meanrev_signals(df, CFG)
    expected = df["close"] - CFG["stop_atr_mult"] * sig["atr"]
    pd.testing.assert_series_equal(sig["initial_stop_long"], expected, check_names=False)


def test_signal_false_during_warmup():
    df = _decline_df().iloc[:15]  # bb_period=20에 못 미침
    sig = generate_meanrev_signals(df, CFG)
    assert not sig["signal_long"].any()
    assert not sig["signal_short"].any()


# --- 룩어헤드 방지 ---

def test_generate_meanrev_signals_is_lookahead_free():
    df = _decline_df()
    full = generate_meanrev_signals(df, CFG)
    truncated = generate_meanrev_signals(df.iloc[:-1], CFG)
    pd.testing.assert_frame_equal(full.iloc[:-1], truncated, check_dtype=False)


# --- 시간 청산 / 레짐 전환 청산 ---

@pytest.mark.parametrize("bars_held,expected", [(24, True), (23, False), (30, True)])
def test_time_exit_triggered_boundaries(bars_held, expected):
    assert time_exit_triggered(bars_held, CFG) is expected


def test_regime_flip_exit_triggers_on_trend():
    assert regime_flip_exit_triggered("long", "TREND") is True


def test_regime_flip_exit_does_not_trigger_on_range_or_neutral():
    assert regime_flip_exit_triggered("long", "RANGE") is False
    assert regime_flip_exit_triggered("short", "NEUTRAL") is False
