"""src/strategy/indicators.py — 순수함수 기술지표 (SPEC.md 2절 계산용).

CLAUDE.md 규칙 2: API 호출/시간 조회/전역 상태 접근 금지. 입력은 확정 봉
DataFrame(open/high/low/close 컬럼, 오래된 순 정렬), 출력은 pandas Series/DataFrame.

★ 룩어헤드 방지: 모든 rolling/smoothing은 인덱스 i 시점에서 i를 포함한 '과거'만
사용한다(pandas rolling의 기본 동작 그대로 — shift 없이도 미래를 보지 않음).
Donchian 채널만 예외적으로 직전 N봉(현재 봉 제외) 기준이 되도록 shift(1)을 쓴다
— 그렇지 않으면 오늘 고가가 오늘의 '돌파 기준선'에 포함되어 자기 자신과 비교하는
꼴이 된다(브레이크아웃 정의상 항상 참이 되어버림).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr


def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing(RMA). 첫 유효값은 단순평균, 이후 재귀식
    `out[i] = (out[i-1]*(period-1) + series[i]) / period`. ADX/ATR/RSI 공통 기반."""
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) < period:
        return pd.Series(out, index=series.index)
    # 첫 period개 중 NaN이 있으면(시계열 시작부) 그만큼 뒤로 밀어 단순평균 시작점을 찾는다.
    first_valid = series.first_valid_index()
    if first_valid is None:
        return pd.Series(out, index=series.index)
    start = series.index.get_loc(first_valid)
    if len(values) - start < period:
        return pd.Series(out, index=series.index)
    seed_idx = start + period - 1
    out[seed_idx] = np.nanmean(values[start:start + period])
    for i in range(seed_idx + 1, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return pd.Series(out, index=series.index)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return wilder_smooth(true_range(high, low, close), period)


def ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = wilder_smooth(gain, period)
    avg_loss = wilder_smooth(loss, period)
    rs = avg_gain / avg_loss
    result = 100 - (100 / (1 + rs))
    # avg_loss가 0이면 rs=inf -> result=100 (과매수 극단, 수학적으로 옳음). avg_gain도 0(무변동)이면 0/0=NaN -> RSI=50 관행.
    result = result.where(~(avg_gain == 0) | ~(avg_loss == 0), 50.0)
    return result


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index)

    tr_smooth = wilder_smooth(true_range(high, low, close), period)
    plus_dm_smooth = wilder_smooth(plus_dm, period)
    minus_dm_smooth = wilder_smooth(minus_dm, period)

    plus_di = 100 * plus_dm_smooth / tr_smooth
    minus_di = 100 * minus_dm_smooth / tr_smooth
    # tr_smooth가 정확히 0(가격이 전혀 안 움직이는 구간)이면 0/0=NaN이 되어 전파된다 —
    # 방향성 정보 자체가 없는 무추세 상태이므로 DI를 0으로 둔다. tr_smooth 자체가
    # NaN인 웜업 구간은 이 마스크에 걸리지 않으므로(NaN==0은 False) 그대로 NaN 유지.
    zero_tr = tr_smooth == 0
    plus_di = plus_di.where(~zero_tr, 0.0)
    minus_di = minus_di.where(~zero_tr, 0.0)
    di_sum = plus_di + minus_di
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    dx = dx.where(di_sum != 0, 0.0)
    return wilder_smooth(dx, period)


def donchian_upper(high: pd.Series, period: int = 20) -> pd.Series:
    """직전 N봉(현재 봉 제외)의 최고가 — 오늘 종가의 '돌파' 여부를 판정하는 기준선이라
    오늘 고가를 포함하면 자기참조가 된다."""
    return high.rolling(period).max().shift(1)


def donchian_lower(low: pd.Series, period: int = 20) -> pd.Series:
    return low.rolling(period).min().shift(1)


def bollinger_bands(close: pd.Series, period: int = 20, std_mult: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    sma = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = sma + std_mult * std
    lower = sma - std_mult * std
    return sma, upper, lower


def bb_width_pct(close: pd.Series, period: int = 20, std_mult: float = 2.0) -> pd.Series:
    """(상단-하단)/중심선 × 100 — 레짐 판별용 변동성 압축/확장 지표(SPEC 2.1)."""
    sma, upper, lower = bollinger_bands(close, period, std_mult)
    return (upper - lower) / sma * 100


def rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    """각 시점 i의 값이 [i-window+1, i] 구간(과거 포함, 미래 없음) 내에서 몇 %ile인지."""
    def _pct(x: np.ndarray) -> float:
        return float((x <= x[-1]).sum()) / len(x) * 100

    return series.rolling(window).apply(_pct, raw=True)


def chandelier_stop(
    high: pd.Series, low: pd.Series, close: pd.Series,
    period: int = 22, atr_mult: float = 3.0, direction: str = "long",
) -> pd.Series:
    """SPEC 2.2 트레일링 스탑. direction='long' -> 최근 period봉 최고가 - mult*ATR(period).
    'short' -> 최근 period봉 최저가 + mult*ATR(period)."""
    atr_series = atr(high, low, close, period)
    if direction == "long":
        return high.rolling(period).max() - atr_mult * atr_series
    if direction == "short":
        return low.rolling(period).min() + atr_mult * atr_series
    raise ValueError(f"direction은 'long' 또는 'short'여야 합니다: {direction!r}")
