"""src/strategy/regime.py — SPEC 2.1 레짐 판별 (1h 봉, 매 1h 마감 시 갱신).

순수함수: 확정 1h 봉 DataFrame(open/high/low/close) -> 시점별 레짐 Series.
CLAUDE.md 규칙 2/3 — API 호출·시간 조회 없음, 룩어헤드 없음(전부 그 시점까지의
데이터만 사용). 파라미터는 config.yaml의 `regime:` 섹션에서만 주입한다.
"""

from __future__ import annotations

import pandas as pd

from src.strategy.indicators import adx, bb_width_pct, rolling_percentile

TREND = "TREND"
RANGE = "RANGE"
NEUTRAL = "NEUTRAL"


def classify_regime(df_1h: pd.DataFrame, cfg: dict) -> pd.Series:
    """df_1h: open/high/low/close 컬럼, 오래된 순 정렬된 확정 1h 봉.
    cfg: config.yaml의 `regime:` 섹션 dict.

    반환: 각 시점의 레짐("TREND"|"RANGE"|"NEUTRAL") 또는 None(지표 웜업 전,
    데이터 부족 — 근사치로 채우지 않는다).

    SPEC 2.1:
        ADX(14) >= 25 AND BB폭 백분위(120봉) >= 60% -> TREND
        ADX(14) < 20  AND BB폭 백분위 < 40%         -> RANGE
        그 외                                        -> NEUTRAL
    """
    adx_series = adx(df_1h["high"], df_1h["low"], df_1h["close"], cfg["adx_period"])
    width = bb_width_pct(df_1h["close"], cfg["bb_period"])
    width_pct = rolling_percentile(width, cfg["bb_width_percentile_window"])

    return pd.Series(
        [classify_from_indicators(a, p, cfg) for a, p in zip(adx_series, width_pct)],
        index=df_1h.index, name="regime",
    )


def classify_from_indicators(adx_value: float, width_percentile: float, cfg: dict) -> object:
    """단일 시점의 ADX/BB폭 퍼센타일 값만으로 레짐을 판정하는 경계값 로직.
    classify_regime()이 시계열 전체에 대해 이 함수를 반복 호출한다 — 경계값
    테스트(SPEC 2.1: ADX 25/20, BB폭 60/40)는 지표 계산과 분리해 이 함수를
    직접 호출하는 방식으로 검증한다."""
    if pd.isna(adx_value) or pd.isna(width_percentile):
        return None
    if adx_value >= cfg["adx_trend_min"] and width_percentile >= cfg["bb_width_percentile_trend_min"]:
        return TREND
    if adx_value < cfg["adx_range_max"] and width_percentile < cfg["bb_width_percentile_range_max"]:
        return RANGE
    return NEUTRAL
