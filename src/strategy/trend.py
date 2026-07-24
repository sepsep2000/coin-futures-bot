"""src/strategy/trend.py — SPEC 2.2 TREND 레짐: 돈치안 브레이크아웃 (15m).

순수함수(CLAUDE.md 규칙 2): 확정 15m 봉 DataFrame -> 시그널 DataFrame.
포지션 보유 중 트레일링/시간청산 판정에 쓰는 헬퍼도 순수함수로 분리해
src/backtest/engine.py(Phase 2)와 src/live/runner.py(Phase 4)가 동일 함수를
재사용하게 한다 (SPEC 3절 "백테스트≠실전 괴리 원천 차단" 원칙).
"""

from __future__ import annotations

import pandas as pd

from src.strategy.indicators import atr, chandelier_stop, donchian_lower, donchian_upper, ema


def generate_trend_signals(df_15m: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """cfg: config.yaml의 `trend:` 섹션.

    진입(Long): 종가 > Donchian(20) 상단 + 종가 > EMA(50) + ATR(14) >= 최근 96봉
    ATR 중위값(저변동 휩쏘 필터). 진입(Short): 대칭(종가 < 하단, 종가 < EMA).
    초기 스탑: 진입가 ∓ stop_atr_mult × ATR(14).

    ★ `require_confirmation_bar`(2026-07-24 재설계, Phase 2 G1 결과 반영): 돌파
    봉 즉시 진입은 중앙값 보유 11봉짜리 휩쏘가 68%였다(BACKTEST_BASELINE.md 참조).
    True면 돌파 봉(raw 조건 성립) 다음 1봉이 "그 돌파 봉이 만든 채널 상단/하단"
    위/아래에서 마감할 때만 실제 진입한다 — 확인봉에서 EMA/저변동 필터를 다시
    검사하지는 않는다(돌파 봉에서 이미 검증됨). 이 값 자체를 이번 전체구간
    성과로 튜닝하지 않는다 — Phase 3 워크포워드 학습구간에서만 검증한다.
    """
    high, low, close = df_15m["high"], df_15m["low"], df_15m["close"]

    atr14 = atr(high, low, close, cfg["atr_period"])
    atr_median = atr14.rolling(cfg["atr_median_window"]).median()
    don_upper = donchian_upper(high, cfg["donchian_period"])
    don_lower = donchian_lower(low, cfg["donchian_period"])
    ema_line = ema(close, cfg["ema_period"])

    if cfg.get("low_vol_filter", True):
        low_vol_ok = atr14 >= atr_median
    else:
        low_vol_ok = pd.Series(True, index=close.index)

    # 지표 웜업 구간(NaN)에서는 절대 진입하지 않는다(근사치로 채우지 않음, CLAUDE.md 규칙 4).
    warmup = don_upper.isna() | don_lower.isna() | ema_line.isna() | atr14.isna() | atr_median.isna()

    raw_long = (close > don_upper) & (close > ema_line) & low_vol_ok & ~warmup
    raw_short = (close < don_lower) & (close < ema_line) & low_vol_ok & ~warmup

    if cfg.get("require_confirmation_bar", False):
        entry_long = raw_long.shift(1).fillna(False) & (close > don_upper.shift(1))
        entry_short = raw_short.shift(1).fillna(False) & (close < don_lower.shift(1))
    else:
        entry_long = raw_long
        entry_short = raw_short

    return pd.DataFrame({
        "entry_long": entry_long.fillna(False),
        "entry_short": entry_short.fillna(False),
        "atr": atr14,
        "donchian_upper": don_upper,
        "donchian_lower": don_lower,
        "ema": ema_line,
        "initial_stop_long": close - cfg["stop_atr_mult"] * atr14,
        "initial_stop_short": close + cfg["stop_atr_mult"] * atr14,
    }, index=df_15m.index)


def trailing_stop(df_15m_since_entry: pd.DataFrame, cfg: dict, direction: str) -> pd.Series:
    """진입 이후 구간(df_15m_since_entry)에 대한 Chandelier 트레일링 스탑 시계열.
    SPEC 2.2: 1.5R 도달 시 25% 부분청산 + 스탑을 본전으로, 이후 Chandelier(22,3.0) 트레일.
    본전 이동/부분청산 자체는 포지션 상태(진입가·R 도달 여부)가 필요해 백테스트/라이브
    엔진(Phase 2/4)의 상태 머신이 담당하고, 이 함수는 순수 트레일링 스탑 값만 낸다."""
    return chandelier_stop(
        df_15m_since_entry["high"], df_15m_since_entry["low"], df_15m_since_entry["close"],
        cfg["chandelier_period"], cfg["chandelier_atr_mult"], direction,
    )


def time_exit_triggered(bars_held: int, r_multiple_reached: float, cfg: dict) -> bool:
    """SPEC 2.2 시간 청산: 96봉(24h) 내 1R 미달 시 시장가 청산."""
    return bars_held >= cfg["time_exit_bars"] and r_multiple_reached < cfg["time_exit_min_r"]
