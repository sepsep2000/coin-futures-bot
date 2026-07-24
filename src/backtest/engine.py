"""src/backtest/engine.py — SPEC 3절 이벤트 드리븐 백테스트 엔진 (Phase 2).

15m 봉 마감 기준. 레짐(1h)에 따라 TREND(Donchian 브레이크아웃)/RANGE(BB+RSI
평균회귀) 전략을 전환하며, 수수료·슬리피지·펀딩비를 전부 반영한다. 포지션
사이징/포트폴리오 한도/킬스위치는 src/risk.py를, 비용은
src/backtest/cost_model.py를 공용으로 쓴다(SPEC 3절 "백테스트≠실전 괴리
원천 차단").

★ 일중 경로 규칙: 한 봉 안에서 스탑/레짐전환청산이 먼저 평가되고, 그 다음
부분청산/목표가/시간청산을 본다 — 여러 조건이 동시에 성립 가능하면 보수적인
쪽(worst-path)을 먼저 적용한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from src.backtest.cost_model import entry_fill_price, exit_fill_price, funding_fee, taker_fee
from src.risk import can_open_new_position, daily_loss_limit_breached, position_size
from src.strategy.meanrev import generate_meanrev_signals, regime_flip_exit_triggered, time_exit_triggered as meanrev_time_exit
from src.strategy.regime import NEUTRAL, RANGE, TREND, classify_regime
from src.strategy.trend import generate_trend_signals
from src.strategy.trend import time_exit_triggered as trend_time_exit
from src.strategy.trend import trailing_stop


@dataclass
class SymbolData:
    df_15m: pd.DataFrame          # open/high/low/close, DatetimeIndex
    df_1h: pd.DataFrame           # open/high/low/close, DatetimeIndex
    funding: pd.DataFrame         # timestamp(ms int64)/funding_rate 컬럼


@dataclass
class Position:
    symbol: str
    strategy: str          # "trend" | "meanrev"
    direction: str          # "long" | "short"
    entry_time: pd.Timestamp
    entry_price: float
    qty: float
    initial_stop: float
    current_stop: float
    initial_stop_distance: float
    target_price: Optional[float] = None
    partial_taken: bool = False
    bars_held: int = 0
    entry_fee_usd: float = 0.0
    funding_paid_usd: float = 0.0


@dataclass
class PendingOrder:
    symbol: str
    direction: str
    limit_price: float
    initial_stop: float
    target_price: float
    bars_waited: int = 0


@dataclass
class Trade:
    symbol: str
    strategy: str
    direction: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    pnl_usd: float
    r_multiple: float
    exit_reason: str
    fees_usd: float
    funding_usd: float


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[pd.Timestamp, float]] = field(default_factory=list)
    final_equity: float = 0.0
    skipped_entries: list[dict] = field(default_factory=list)  # 자본부족 등으로 진입 못 한 기록


def _current_r_multiple(position: Position, mark_price: float) -> float:
    sign = 1.0 if position.direction == "long" else -1.0
    return sign * (mark_price - position.entry_price) / position.initial_stop_distance


def _stop_hit(position: Position, bar: pd.Series) -> bool:
    if position.direction == "long":
        return bar["low"] <= position.current_stop
    return bar["high"] >= position.current_stop


def _target_hit(position: Position, bar: pd.Series) -> bool:
    if position.target_price is None:
        return False
    if position.direction == "long":
        return bar["high"] >= position.target_price
    return bar["low"] <= position.target_price


def _close_position(
    position: Position, exit_time: pd.Timestamp, exit_reference_price: float, exit_reason: str,
    qty_frac: float, cost_cfg: dict, order_type: str = "market",
) -> tuple[Trade, float]:
    """position의 qty_frac 비율만큼 청산. 반환: (Trade, 실현 pnl_usd)."""
    qty = position.qty * qty_frac
    exit_price = exit_fill_price(exit_reference_price, position.direction, cost_cfg["slippage_pct"], order_type)
    notional_exit = qty * exit_price
    fee = taker_fee(notional_exit, cost_cfg["taker_fee_pct"]) if order_type == "market" else 0.0

    sign = 1.0 if position.direction == "long" else -1.0
    gross_pnl = sign * (exit_price - position.entry_price) * qty
    entry_fee_share = position.entry_fee_usd * qty_frac
    funding_share = position.funding_paid_usd * qty_frac
    net_pnl = gross_pnl - fee - entry_fee_share - funding_share

    r_multiple = sign * (exit_price - position.entry_price) / position.initial_stop_distance
    trade = Trade(
        symbol=position.symbol, strategy=position.strategy, direction=position.direction,
        entry_time=position.entry_time, exit_time=exit_time,
        entry_price=position.entry_price, exit_price=exit_price, qty=qty,
        pnl_usd=net_pnl, r_multiple=r_multiple, exit_reason=exit_reason,
        fees_usd=fee + entry_fee_share, funding_usd=funding_share,
    )
    return trade, net_pnl


def _manage_trend_position(
    position: Position, bar: pd.Series, trend_row: pd.Series, cfg: dict, cost_cfg: dict,
) -> tuple[Optional[Position], list[Trade], float]:
    """트렌드 포지션 1봉 관리. 반환: (갱신된 포지션 또는 None(전량청산), 청산 트레이드 목록, 실현손익 합).

    ★ stop_grace_period_bars(2026-07-24 재설계, 옵션 1): 진입 후 이 봉수 동안은
    스탑 체크 자체를 건너뛴다. ETH 단독 트레이드 로그 분석 결과 stop_loss 청산의
    68.1%가 중앙값 18봉(4.5h) 뒤에 발생하고 4봉 이내 즉시 스탑은 13.5%뿐이라,
    이 값(4봉=1h, 그리드 아님·고정)은 그 최하위 구간만 겨냥한다."""
    trades: list[Trade] = []
    realized = 0.0
    position.bars_held += 1

    grace_period = cfg.get("stop_grace_period_bars", 0)
    if position.bars_held > grace_period and _stop_hit(position, bar):
        trade, pnl = _close_position(position, bar.name, position.current_stop, "stop_loss", 1.0, cost_cfg)
        return None, [trade], pnl

    if not position.partial_taken:
        r = _current_r_multiple(position, bar["high"] if position.direction == "long" else bar["low"])
        if r >= cfg["partial_tp_r_multiple"]:
            partial_price = position.entry_price + (
                cfg["partial_tp_r_multiple"] * position.initial_stop_distance
                * (1 if position.direction == "long" else -1)
            )
            trade, pnl = _close_position(position, bar.name, partial_price, "partial_tp", cfg["partial_tp_pct"] / 100, cost_cfg)
            trades.append(trade)
            realized += pnl
            position.qty *= (1 - cfg["partial_tp_pct"] / 100)
            position.partial_taken = True
            if cfg.get("breakeven_after_partial", True):
                position.current_stop = position.entry_price

    if position.partial_taken:
        trail = trend_row.get("chandelier_stop")
        if trail is not None and pd.notna(trail):
            if position.direction == "long":
                position.current_stop = max(position.current_stop, trail)
            else:
                position.current_stop = min(position.current_stop, trail)

    r_now = _current_r_multiple(position, bar["close"])
    if trend_time_exit(position.bars_held, r_now, cfg):
        trade, pnl = _close_position(position, bar.name, bar["close"], "time_stop", 1.0, cost_cfg)
        trades.append(trade)
        realized += pnl
        return None, trades, realized

    return position, trades, realized


def _manage_meanrev_position(
    position: Position, bar: pd.Series, regime: object, cfg: dict, cost_cfg: dict,
) -> tuple[Optional[Position], list[Trade], float]:
    position.bars_held += 1

    if regime_flip_exit_triggered(position.direction, regime):
        trade, pnl = _close_position(position, bar.name, bar["close"], "regime_flip", 1.0, cost_cfg)
        return None, [trade], pnl

    if _stop_hit(position, bar):
        trade, pnl = _close_position(position, bar.name, position.current_stop, "stop_loss", 1.0, cost_cfg)
        return None, [trade], pnl

    if _target_hit(position, bar):
        trade, pnl = _close_position(position, bar.name, position.target_price, "profit_target", 1.0, cost_cfg)
        return None, [trade], pnl

    if meanrev_time_exit(position.bars_held, cfg):
        trade, pnl = _close_position(position, bar.name, bar["close"], "time_stop", 1.0, cost_cfg)
        return None, [trade], pnl

    return position, [], 0.0


def _apply_funding(position: Position, symbol_data: SymbolData, bar_ts_ms: int, cost_cfg: dict) -> float:
    """이 15m 봉 시각이 펀딩 정산 시각과 일치하면 비용을 적립. 반환: 적립된 비용(+지출/-수취)."""
    match = symbol_data.funding[symbol_data.funding["timestamp"] == bar_ts_ms]
    if match.empty:
        return 0.0
    rate = float(match.iloc[0]["funding_rate"])
    notional = position.qty * position.entry_price
    cost = funding_fee(notional, rate, position.direction)
    position.funding_paid_usd += cost
    return cost


def run_backtest(data: dict[str, SymbolData], cfg: dict) -> BacktestResult:
    """cfg: config.yaml 전체(dict) — account/costs/regime/trend/meanrev 섹션 사용.
    dict 순회 순서(파이썬 3.7+ 삽입 순서 보존)로 심볼을 처리해 매 실행 재현 가능."""
    account_cfg = cfg["account"]
    cost_cfg = cfg["costs"]
    regime_cfg = cfg["regime"]
    trend_cfg = cfg["trend"]
    meanrev_cfg = cfg["meanrev"]
    # 2026-07-24 SPEC 4절 재설계 — meanrev 기각(PROGRESS.md 참조). active_strategies에
    # 없으면 해당 레짐에서 신규 진입 자체를 하지 않는다(코드/테스트는 보존).
    active_strategies = cfg["active_strategies"]

    equity = float(account_cfg["initial_equity_usd"])
    result = BacktestResult()

    precomputed: dict[str, dict] = {}
    for symbol, sd in data.items():
        regime_1h = classify_regime(sd.df_1h, regime_cfg)
        regime_15m = regime_1h.reindex(sd.df_15m.index, method="ffill")
        trend_sig = generate_trend_signals(sd.df_15m, trend_cfg)
        # 트렌드 청산용 Chandelier 트레일 시계열 — 진입 이후에만 실제로 쓰이지만 전체
        # 구간을 미리 계산해도 룩어헤드가 아니다(각 시점 t의 값은 t까지의 과거만 사용).
        chand_long = trailing_stop(sd.df_15m, trend_cfg, "long")
        chand_short = trailing_stop(sd.df_15m, trend_cfg, "short")
        meanrev_sig = generate_meanrev_signals(sd.df_15m, meanrev_cfg)
        precomputed[symbol] = {
            "regime_15m": regime_15m, "trend_sig": trend_sig, "meanrev_sig": meanrev_sig,
            "chand_long": chand_long, "chand_short": chand_short,
        }

    master_index = sorted(set().union(*[sd.df_15m.index for sd in data.values()]))

    open_positions: dict[str, Position] = {}
    pending_orders: dict[str, PendingOrder] = {}
    daily_pnl = 0.0
    current_day = None

    for ts in master_index:
        if current_day is None or ts.date() != current_day:
            current_day = ts.date()
            daily_pnl = 0.0
        ts_ms = int(ts.value // 1_000_000)

        for symbol, sd in data.items():
            if ts not in sd.df_15m.index:
                continue
            bar = sd.df_15m.loc[ts]
            pc = precomputed[symbol]
            regime = pc["regime_15m"].loc[ts]

            position = open_positions.get(symbol)
            if position is not None:
                if position.strategy == "trend":
                    trend_row = pd.Series({"chandelier_stop": (
                        pc["chand_long"].loc[ts] if position.direction == "long" else pc["chand_short"].loc[ts]
                    )})
                    new_pos, trades, pnl = _manage_trend_position(position, bar, trend_row, trend_cfg, cost_cfg)
                else:
                    new_pos, trades, pnl = _manage_meanrev_position(position, bar, regime, meanrev_cfg, cost_cfg)

                if trades:
                    result.trades.extend(trades)
                    daily_pnl += pnl
                    equity += pnl

                if new_pos is None:
                    del open_positions[symbol]
                else:
                    # 이 봉 시각이 펀딩 정산과 겹치고, 포지션이 이 봉을 넘겨 유지되는 경우에만 적립
                    # (같은 봉에 청산까지 됐으면 무시 — 발생 빈도 극히 낮은 경계 케이스, 근사 허용).
                    _apply_funding(new_pos, sd, ts_ms, cost_cfg)
                    open_positions[symbol] = new_pos
                continue  # 이번 봉엔 이미 포지션이 있었으니 신규 진입 평가 안 함

            pending = pending_orders.get(symbol)
            if pending is not None:
                filled = bar["low"] <= pending.limit_price <= bar["high"]
                pending.bars_waited += 1
                if filled:
                    if daily_loss_limit_breached(daily_pnl, equity, account_cfg["daily_loss_limit_pct"]):
                        del pending_orders[symbol]
                    elif can_open_new_position(set(open_positions.keys()), symbol, account_cfg["max_concurrent_positions"]):
                        entry_price = entry_fill_price(pending.limit_price, pending.direction, cost_cfg["slippage_pct"], "limit")
                        qty = position_size(
                            equity, entry_price, pending.initial_stop, account_cfg["risk_per_trade_pct"],
                            max_leverage=account_cfg["leverage"],
                        )
                        if qty > 0:
                            notional = qty * entry_price
                            fee = taker_fee(notional, cost_cfg["maker_fee_pct"])
                            open_positions[symbol] = Position(
                                symbol=symbol, strategy="meanrev", direction=pending.direction,
                                entry_time=ts, entry_price=entry_price, qty=qty,
                                initial_stop=pending.initial_stop, current_stop=pending.initial_stop,
                                initial_stop_distance=abs(entry_price - pending.initial_stop),
                                target_price=pending.target_price, entry_fee_usd=fee,
                            )
                        else:
                            result.skipped_entries.append({"symbol": symbol, "time": ts, "reason": "capital_too_small"})
                    del pending_orders[symbol]
                elif pending.bars_waited >= meanrev_cfg.get("limit_cancel_after_bars", 2):
                    del pending_orders[symbol]
                continue

            if daily_loss_limit_breached(daily_pnl, equity, account_cfg["daily_loss_limit_pct"]):
                continue
            if not can_open_new_position(set(open_positions.keys()), symbol, account_cfg["max_concurrent_positions"]):
                continue

            if regime == TREND and "trend" in active_strategies:
                trow = pc["trend_sig"].loc[ts]
                direction = "long" if bool(trow["entry_long"]) else ("short" if bool(trow["entry_short"]) else None)
                if direction is not None:
                    initial_stop = trow["initial_stop_long"] if direction == "long" else trow["initial_stop_short"]
                    entry_price = entry_fill_price(bar["close"], direction, cost_cfg["slippage_pct"], "market")
                    qty = position_size(
                        equity, entry_price, initial_stop, account_cfg["risk_per_trade_pct"],
                        max_leverage=account_cfg["leverage"],
                    )
                    if qty > 0:
                        notional = qty * entry_price
                        fee = taker_fee(notional, cost_cfg["taker_fee_pct"])
                        open_positions[symbol] = Position(
                            symbol=symbol, strategy="trend", direction=direction,
                            entry_time=ts, entry_price=entry_price, qty=qty,
                            initial_stop=initial_stop, current_stop=initial_stop,
                            initial_stop_distance=abs(entry_price - initial_stop), entry_fee_usd=fee,
                        )
                    else:
                        result.skipped_entries.append({"symbol": symbol, "time": ts, "reason": "capital_too_small"})
            elif regime == RANGE and "meanrev" in active_strategies:
                mrow = pc["meanrev_sig"].loc[ts]
                direction = "long" if bool(mrow["signal_long"]) else ("short" if bool(mrow["signal_short"]) else None)
                if direction is not None:
                    initial_stop = mrow["initial_stop_long"] if direction == "long" else mrow["initial_stop_short"]
                    pending_orders[symbol] = PendingOrder(
                        symbol=symbol, direction=direction, limit_price=bar["close"],
                        initial_stop=initial_stop, target_price=mrow["target_price"],
                    )
            # NEUTRAL 또는 규명 안 됨(None) -> 신규 진입 없음

        result.equity_curve.append((ts, equity))

    result.final_equity = equity
    return result
