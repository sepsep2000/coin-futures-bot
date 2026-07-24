import numpy as np
import pandas as pd
import pytest

from src.strategy.indicators import (
    adx,
    atr,
    bb_width_pct,
    bollinger_bands,
    chandelier_stop,
    donchian_lower,
    donchian_upper,
    ema,
    rolling_percentile,
    rsi,
    true_range,
    wilder_smooth,
)


def _bars(closes, spread=0.1):
    close = pd.Series(closes, dtype=float)
    high = close + spread
    low = close - spread
    return high, low, close


# --- true_range / wilder_smooth 기본 동작 ---

def test_true_range_uses_prev_close_for_gaps():
    high = pd.Series([10.0, 12.0])
    low = pd.Series([9.0, 11.5])
    close = pd.Series([9.5, 11.8])
    tr = true_range(high, low, close)
    # index0: prev_close가 없어 |high-prev|/|low-prev|가 NaN -> max(skipna)가 high-low(1.0)로 귀결
    assert tr.iloc[0] == pytest.approx(1.0)
    # index1: max(12-11.5=0.5, |12-9.5|=2.5, |11.5-9.5|=2.0) = 2.5
    assert tr.iloc[1] == pytest.approx(2.5)


def test_wilder_smooth_seed_is_simple_average_of_first_period():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = wilder_smooth(s, period=3)
    assert pd.isna(out.iloc[0]) and pd.isna(out.iloc[1])
    assert out.iloc[2] == pytest.approx((1 + 2 + 3) / 3)  # 단순평균 시드
    assert out.iloc[3] == pytest.approx((out.iloc[2] * 2 + 4) / 3)  # 이후 재귀식
    assert out.iloc[4] == pytest.approx((out.iloc[3] * 2 + 5) / 3)


# --- ATR: 수기 계산 ---

def test_atr_known_answer_constant_true_range():
    # high-low가 항상 2.0, gap 없음(종가가 다음 시가와 이어짐) -> TR이 항상 2.0
    # -> ATR도 항상 2.0 (평균의 평균은 상수 그대로)
    close = pd.Series([100.0 + i for i in range(20)])
    high = close + 1.0
    low = close - 1.0
    result = atr(high, low, close, period=14)
    assert result.iloc[13] == pytest.approx(2.0)
    assert result.iloc[-1] == pytest.approx(2.0)


# --- ADX: 극단(단조추세) 골든케이스 — 손으로 검증 가능 ---

def test_adx_pure_uptrend_converges_to_100():
    """매 봉이 일정하게 상승하고 변동폭이 고정이면 -DM은 항상 0이라 DX=100이
    매 시점 성립하고, DX가 상수 100이면 Wilder 평균도 100으로 수렴한다(수기 계산)."""
    high, low, close = _bars([100 + i for i in range(60)])
    result = adx(high, low, close, period=14)
    assert result.iloc[-1] == pytest.approx(100.0, abs=1e-6)


def test_adx_pure_downtrend_converges_to_100():
    high, low, close = _bars([200 - i for i in range(60)])
    result = adx(high, low, close, period=14)
    assert result.iloc[-1] == pytest.approx(100.0, abs=1e-6)  # 방향 무관, ADX는 추세 강도(비방향)


def test_adx_flat_price_is_zero():
    """가격이 전혀 안 움직이면 +DM=-DM=0, TR도 0 -> DX 정의상 0/0을 0으로 처리(무추세)."""
    high, low, close = _bars([100.0] * 40, spread=0.0)
    result = adx(high, low, close, period=14)
    assert result.iloc[-1] == pytest.approx(0.0)


# --- RSI: 수기 계산 ---

def test_rsi_all_gains_is_100():
    close = pd.Series([100.0 + i for i in range(30)])
    result = rsi(close, period=14)
    assert result.iloc[-1] == pytest.approx(100.0)


def test_rsi_all_losses_is_0():
    close = pd.Series([200.0 - i for i in range(30)])
    result = rsi(close, period=14)
    assert result.iloc[-1] == pytest.approx(0.0)


def test_rsi_flat_price_is_50():
    close = pd.Series([100.0] * 30)
    result = rsi(close, period=14)
    assert result.iloc[-1] == pytest.approx(50.0)


# --- EMA: 수기 계산 (표준 EMA 공식) ---

def test_ema_known_answer():
    close = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = ema(close, period=3)
    alpha = 2 / (3 + 1)
    # adjust=False 재귀식: e0=x0, e_i = alpha*x_i + (1-alpha)*e_{i-1}
    e0 = 1.0
    e1 = alpha * 2.0 + (1 - alpha) * e0
    e2 = alpha * 3.0 + (1 - alpha) * e1
    e3 = alpha * 4.0 + (1 - alpha) * e2
    e4 = alpha * 5.0 + (1 - alpha) * e3
    assert result.iloc[4] == pytest.approx(e4)


# --- Donchian: 수기 계산 + 자기참조 배제 ---

def test_donchian_upper_excludes_current_bar():
    high = pd.Series([10.0, 20.0, 15.0, 12.0, 11.0])  # index1이 최고가(20)
    result = donchian_upper(high, period=3)
    # index4의 채널: 직전 3봉(index1,2,3) = [20,15,12] 중 최고 = 20 (index4 자신의 값(11)은 미포함)
    assert result.iloc[4] == pytest.approx(20.0)


def test_donchian_lower_excludes_current_bar():
    low = pd.Series([10.0, 5.0, 8.0, 9.0, 20.0])
    result = donchian_lower(low, period=3)
    assert result.iloc[4] == pytest.approx(5.0)  # 직전 3봉[5,8,9] 중 최저


# --- Bollinger Bands: 수기 계산 ---

def test_bollinger_bands_known_answer():
    close = pd.Series([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
    sma, upper, lower = bollinger_bands(close, period=8, std_mult=2.0)
    expected_mean = close.mean()
    expected_std = close.std(ddof=0)
    assert sma.iloc[-1] == pytest.approx(expected_mean)
    assert upper.iloc[-1] == pytest.approx(expected_mean + 2 * expected_std)
    assert lower.iloc[-1] == pytest.approx(expected_mean - 2 * expected_std)


def test_bb_width_pct_known_answer():
    close = pd.Series([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
    width = bb_width_pct(close, period=8, std_mult=2.0)
    sma, upper, lower = bollinger_bands(close, period=8, std_mult=2.0)
    assert width.iloc[-1] == pytest.approx((upper.iloc[-1] - lower.iloc[-1]) / sma.iloc[-1] * 100)


# --- rolling_percentile: 수기 계산 ---

def test_rolling_percentile_known_answer():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = rolling_percentile(s, window=5)
    # 창 [1,2,3,4,5] 중 현재값 5 이하인 개수 5개 / 5 * 100 = 100
    assert result.iloc[4] == pytest.approx(100.0)


def test_rolling_percentile_middle_value():
    s = pd.Series([5.0, 1.0, 2.0, 4.0, 3.0])
    result = rolling_percentile(s, window=5)
    # 창 [5,1,2,4,3], 현재값 3 이하인 값: 1,2,3 -> 3개/5*100=60
    assert result.iloc[4] == pytest.approx(60.0)


# --- Chandelier stop: 수기 계산 ---

def test_chandelier_stop_long_known_answer():
    high, low, close = _bars([100 + i for i in range(30)], spread=1.0)
    result = chandelier_stop(high, low, close, period=22, atr_mult=3.0, direction="long")
    expected_highest = high.rolling(22).max().iloc[-1]
    expected_atr = atr(high, low, close, 22).iloc[-1]
    assert result.iloc[-1] == pytest.approx(expected_highest - 3.0 * expected_atr)


def test_chandelier_stop_rejects_invalid_direction():
    high, low, close = _bars([100.0] * 30)
    with pytest.raises(ValueError):
        chandelier_stop(high, low, close, direction="sideways")


# --- 룩어헤드 방지: 마지막 봉을 잘라도 이전 값들은 변하지 않아야 한다 ---

@pytest.mark.parametrize("indicator_fn", [
    lambda h, l, c: atr(h, l, c, 14),
    lambda h, l, c: adx(h, l, c, 14),
    lambda h, l, c: rsi(c, 14),
    lambda h, l, c: ema(c, 10),
    lambda h, l, c: donchian_upper(h, 20),
    lambda h, l, c: bb_width_pct(c, 20),
    lambda h, l, c: chandelier_stop(h, l, c, 22, 3.0, "long"),
])
def test_indicators_are_lookahead_free(indicator_fn):
    rng = np.random.default_rng(7)
    n = 100
    close = pd.Series(100 + np.cumsum(rng.normal(0, 0.5, n)))
    high = close + rng.random(n) * 0.5
    low = close - rng.random(n) * 0.5

    full = indicator_fn(high, low, close)
    truncated = indicator_fn(high.iloc[:-1], low.iloc[:-1], close.iloc[:-1])

    pd.testing.assert_series_equal(full.iloc[:-1], truncated, check_names=False)


def test_rolling_percentile_is_lookahead_free():
    rng = np.random.default_rng(3)
    s = pd.Series(rng.random(80))
    full = rolling_percentile(s, window=20)
    truncated = rolling_percentile(s.iloc[:-1], window=20)
    pd.testing.assert_series_equal(full.iloc[:-1], truncated, check_names=False)
