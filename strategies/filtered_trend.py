"""strategies/filtered_trend.py — trend + 1a(1D 모멘텀 정합) 필터, 정식 전략 코드.

FILTER_VALIDATION_1a.md에서 재현성 확인된 필터를 정식 파이프라인에 편입한다.
신규 로직 없음 — src/backtest/engine.py의 run_backtest()가 하는 정확히 같은
일(포지션 관리 → 펀딩 적용 → 신규진입 평가)을 ETH 단일심볼에 대해 그대로
반복하되, 진입 조건에 1D 모멘텀 합치 조건 하나만 AND로 추가한다. 재사용:
Position/Trade/BacktestResult/_manage_trend_position/_apply_funding
(engine.py), entry_fill_price/taker_fee(cost_model.py), position_size(risk.py),
generate_trend_signals/trailing_stop(strategy/trend.py), classify_regime
(strategy/regime.py) — 전부 기존 함수 그대로 호출, 수정 없음.

★ 이 모듈이 진단 스크립트(scripts/diag/signal_validation... 의 gen_1a)와
다른 점: gen_1a는 필터 없는 전체 backtest를 먼저 돌린 뒤 결과 트레이드를
사후 필터링했다. 이 모듈은 매 봉마다 신규진입 시점에 필터를 직접 적용한다
(진짜 진입 게이팅). 단일 심볼·포지션 슬롯 1개 환경에서는 두 방식이 트레이드
"내용"은 동일해도 "시퀀스"가 다를 수 있다 — 필터로 걸러졌던 진입이 사후
필터링에서는 여전히 그 시간 동안 슬롯을 점유해 그 뒤에 왔을 다른(원래대로면
필터를 통과했을) 신호를 원천 배제했을 수 있기 때문이다. 이 차이는
reports/OFFICIAL_GATE_RESULT.md STEP 3(교차검증)에서 실측 비교한다.
"""

from __future__ import annotations

import pandas as pd

from src.backtest.cost_model import entry_fill_price, taker_fee
from src.backtest.engine import BacktestResult, Position, SymbolData, _apply_funding, _manage_trend_position
from src.risk import can_open_new_position, daily_loss_limit_breached, position_size
from src.strategy.regime import TREND, classify_regime
from src.strategy.trend import generate_trend_signals, trailing_stop

MOMENTUM_LOOKBACK_DAYS = 5  # FILTER_VALIDATION_1a.md / signal_specs/1a.yaml와 동일, 재조정 없음
SYMBOL = "ETH/USDT:USDT"    # config.yaml exchange.pairs와 동일(단일자산)


def _momentum_agrees(direction: str, roc: float) -> bool:
    if pd.isna(roc):
        return False
    return (direction == "long" and roc > 0) or (direction == "short" and roc < 0)


def run(data: SymbolData, cfg: dict) -> BacktestResult:
    account_cfg = cfg["account"]
    cost_cfg = cfg["costs"]
    regime_cfg = cfg["regime"]
    trend_cfg = cfg["trend"]

    regime_1h = classify_regime(data.df_1h, regime_cfg)
    regime_15m = regime_1h.reindex(data.df_15m.index, method="ffill")
    trend_sig = generate_trend_signals(data.df_15m, trend_cfg)
    chand_long = trailing_stop(data.df_15m, trend_cfg, "long")
    chand_short = trailing_stop(data.df_15m, trend_cfg, "short")

    daily_close = data.df_15m["close"].resample("1D").last()
    daily_roc = daily_close.pct_change(MOMENTUM_LOOKBACK_DAYS)
    roc_15m = daily_roc.reindex(data.df_15m.index, method="ffill")  # ffill = 과거 확정 일봉만 전파, 룩어헤드 없음

    equity = float(account_cfg["initial_equity_usd"])
    result = BacktestResult()
    position: Position | None = None
    daily_pnl = 0.0
    current_day = None

    for ts in data.df_15m.index:
        if current_day is None or ts.date() != current_day:
            current_day = ts.date()
            daily_pnl = 0.0
        ts_ms = int(ts.value // 1_000_000)
        bar = data.df_15m.loc[ts]
        regime = regime_15m.loc[ts]

        if position is not None:
            trend_row = pd.Series({"chandelier_stop": (
                chand_long.loc[ts] if position.direction == "long" else chand_short.loc[ts]
            )})
            new_pos, trades, pnl = _manage_trend_position(position, bar, trend_row, trend_cfg, cost_cfg)
            if trades:
                result.trades.extend(trades)
                daily_pnl += pnl
                equity += pnl
            if new_pos is None:
                position = None
            else:
                _apply_funding(new_pos, data, ts_ms, cost_cfg)
                position = new_pos
            result.equity_curve.append((ts, equity))
            continue

        if daily_loss_limit_breached(daily_pnl, equity, account_cfg["daily_loss_limit_pct"]):
            result.equity_curve.append((ts, equity))
            continue
        if not can_open_new_position(set(), SYMBOL, account_cfg["max_concurrent_positions"]):
            result.equity_curve.append((ts, equity))
            continue

        if regime == TREND:
            trow = trend_sig.loc[ts]
            direction = "long" if bool(trow["entry_long"]) else ("short" if bool(trow["entry_short"]) else None)
            if direction is not None and _momentum_agrees(direction, roc_15m.loc[ts]):
                initial_stop = trow["initial_stop_long"] if direction == "long" else trow["initial_stop_short"]
                entry_price = entry_fill_price(bar["close"], direction, cost_cfg["slippage_pct"], "market")
                qty = position_size(
                    equity, entry_price, initial_stop, account_cfg["risk_per_trade_pct"],
                    max_leverage=account_cfg["leverage"],
                )
                if qty > 0:
                    notional = qty * entry_price
                    fee = taker_fee(notional, cost_cfg["taker_fee_pct"])
                    position = Position(
                        symbol=SYMBOL, strategy="trend", direction=direction,
                        entry_time=ts, entry_price=entry_price, qty=qty,
                        initial_stop=initial_stop, current_stop=initial_stop,
                        initial_stop_distance=abs(entry_price - initial_stop), entry_fee_usd=fee,
                    )

        result.equity_curve.append((ts, equity))

    result.final_equity = equity
    return result
