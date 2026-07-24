import numpy as np
import pandas as pd
import pytest

from src.strategy.regime import RANGE, TREND, classify_from_indicators, classify_regime

CFG = {
    "adx_period": 14,
    "adx_trend_min": 25,
    "adx_range_max": 20,
    "bb_period": 20,
    "bb_width_percentile_window": 120,
    "bb_width_percentile_trend_min": 60,
    "bb_width_percentile_range_max": 40,
}


# --- 경계값(SPEC 2.1: ADX 25/20, BB폭 60/40) — 지표 계산과 분리해 직접 검증 ---

def test_classify_trend_at_exact_boundary():
    assert classify_from_indicators(25.0, 60.0, CFG) == TREND


def test_classify_neutral_just_below_trend_adx_boundary():
    assert classify_from_indicators(24.99, 60.0, CFG) == "NEUTRAL"


def test_classify_neutral_just_below_trend_pct_boundary():
    assert classify_from_indicators(25.0, 59.99, CFG) == "NEUTRAL"


def test_classify_range_just_below_boundary():
    assert classify_from_indicators(19.99, 39.99, CFG) == RANGE


def test_classify_neutral_at_range_adx_boundary():
    """ADX==20은 'ADX<20' 조건을 만족하지 못하므로 RANGE가 아니다."""
    assert classify_from_indicators(20.0, 39.99, CFG) == "NEUTRAL"


def test_classify_neutral_at_range_pct_boundary():
    assert classify_from_indicators(19.99, 40.0, CFG) == "NEUTRAL"


def test_classify_neutral_between_thresholds():
    assert classify_from_indicators(22.0, 50.0, CFG) == "NEUTRAL"


def test_classify_none_when_adx_missing():
    assert classify_from_indicators(float("nan"), 60.0, CFG) is None


def test_classify_none_when_pct_missing():
    assert classify_from_indicators(25.0, float("nan"), CFG) is None


# --- 통합: 실제 OHLCV로 TREND/RANGE 유도 (골든케이스) ---

def _make_1h_bars(n, kind: str) -> pd.DataFrame:
    """2단계 구성 — 최근 구간이 이전 이력 대비 뚜렷이 다른 변동성을 갖도록 만들어
    BB폭 퍼센타일이 경계값을 확실히 넘도록 한다(단조 등가속 상승 등 '자연스러운'
    구성은 SMA도 같이 커져서 상대폭(%)이 오히려 줄어드는 반례가 실측으로 확인됨)."""
    split = min(150, n)
    if kind == "trend":
        # 1단계: 완만한 단조 상승. 2단계: 훨씬 가파른 단조 상승(같은 방향 유지 -> ADX 고유지,
        # 변동폭 급확대 -> 최근 BB폭이 트레일링 120봉 이력 중 상위권).
        phase1 = np.arange(split, dtype=float) * 0.05
        phase2 = (phase1[-1] if split else 0.0) + np.arange(1, n - split + 1, dtype=float) * 2.0
        close = 100 + np.concatenate([phase1, phase2])
    else:  # range
        # 1단계: 넓은 진폭 진동. 2단계: 훨씬 좁은 진폭 진동 -> 방향성 없어 ADX 낮고,
        # 최근 BB폭이 이전(넓은 진폭) 이력 대비 하위권.
        phase1 = 3.0 * np.sin(np.arange(split) * 0.5)
        phase2 = 0.3 * np.sin(np.arange(n - split) * 0.5)
        close = 100 + np.concatenate([phase1, phase2])
    close = pd.Series(close)
    high = close + 0.05
    low = close - 0.05
    idx = pd.date_range("2024-01-01", periods=n, freq="h")
    return pd.DataFrame({"open": close.values, "high": high.values, "low": low.values, "close": close.values}, index=idx)


def test_classify_regime_yields_trend_for_strong_monotonic_move():
    df = _make_1h_bars(200, "trend")
    result = classify_regime(df, CFG)
    assert result.iloc[-1] == TREND


def test_classify_regime_yields_range_for_low_vol_sideways():
    df = _make_1h_bars(200, "range")
    result = classify_regime(df, CFG)
    assert result.iloc[-1] == RANGE


def test_classify_regime_none_during_warmup():
    df = _make_1h_bars(50, "trend")  # bb_width_percentile_window=120에 한참 못 미침
    result = classify_regime(df, CFG)
    assert result.iloc[-1] is None


# --- 룩어헤드 방지 ---

def test_classify_regime_is_lookahead_free():
    df = _make_1h_bars(200, "trend")
    full = classify_regime(df, CFG)
    truncated = classify_regime(df.iloc[:-1], CFG)
    assert list(full.iloc[:-1]) == list(truncated)
