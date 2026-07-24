"""src/strategy/meanrev.py — SPEC 2.3 RANGE 레짐: 볼린저 평균회귀 (15m).

순수함수(CLAUDE.md 규칙 2). 실제 체결 방식(다음 봉 시가 지정가, 2봉 미체결 취소)은
주문 상태가 필요해 src/backtest/engine.py·src/live/executor.py(이후 Phase)가
처리하고, 이 모듈은 "이 봉에 진입 조건이 성립했는가"만 판정한다.
"""

from __future__ import annotations

import pandas as pd

from src.strategy.indicators import atr, bollinger_bands, rsi


def generate_meanrev_signals(df_15m: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """cfg: config.yaml의 `meanrev:` 섹션.

    진입(Long): 종가 <= BB(20, bb_std) 하단 AND RSI(14) <= rsi_oversold(30).
    진입(Short): 종가 >= BB 상단 AND RSI >= rsi_overbought(70).
    목표가: BB 중심선(SMA20). 스탑: 진입가 ∓ stop_atr_mult × ATR(atr_period).
    """
    close = df_15m["close"]
    high, low = df_15m["high"], df_15m["low"]

    sma, upper, lower = bollinger_bands(close, cfg["bb_period"], cfg["bb_std"])
    rsi_line = rsi(close, cfg["rsi_period"])
    atr_line = atr(high, low, close, cfg["atr_period"])

    warmup = sma.isna() | rsi_line.isna() | atr_line.isna()

    signal_long = (close <= lower) & (rsi_line <= cfg["rsi_oversold"]) & ~warmup
    signal_short = (close >= upper) & (rsi_line >= cfg["rsi_overbought"]) & ~warmup

    return pd.DataFrame({
        "signal_long": signal_long.fillna(False),
        "signal_short": signal_short.fillna(False),
        "sma": sma, "bb_upper": upper, "bb_lower": lower, "rsi": rsi_line, "atr": atr_line,
        "target_price": sma,
        "initial_stop_long": close - cfg["stop_atr_mult"] * atr_line,
        "initial_stop_short": close + cfg["stop_atr_mult"] * atr_line,
    }, index=df_15m.index)


def time_exit_triggered(bars_held: int, cfg: dict) -> bool:
    """SPEC 2.3 시간 청산: 24봉(6h) 내 목표가 미도달 시 시장가 청산."""
    return bars_held >= cfg["time_exit_bars"]


def regime_flip_exit_triggered(position_direction: str, current_regime: str) -> bool:
    """SPEC 2.3 금지 규칙: RANGE 진입 후 레짐이 TREND로 전환되고 포지션이 역방향이면
    즉시 청산. position_direction='long'인데 TREND 레짐에서 하락 방향이 우세하거나
    (또는 그 반대) 판단은 호출부(엔진)가 방향 정보를 함께 넘겨 처리한다 — 여기선
    'RANGE 포지션이 TREND 레짐과 마주쳤다'는 최소 조건만 판정한다."""
    return current_regime == "TREND"
