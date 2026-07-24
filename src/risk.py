"""src/risk.py — SPEC 2.4 공통 리스크 규칙.

순수함수(사이징/킬스위치/포지션 한도 판정)와, 라이브 전용 검증 헬퍼(스탑 존재
확인 등, Phase 4에서 실제 주문 상태와 함께 쓰임)를 함께 둔다. 백테스트
엔진(Phase 2)과 라이브 러너(Phase 4)가 이 모듈을 공용으로 재사용해
"동일 사이징/킬스위치 로직" 원칙을 지킨다 (SPEC 3절).
"""

from __future__ import annotations

import math
from typing import Optional


def position_size(
    account_equity_usd: float,
    entry_price: float,
    stop_price: float,
    risk_per_trade_pct: float,
    max_leverage: Optional[float] = None,
    qty_step: Optional[float] = None,
    min_qty: Optional[float] = None,
) -> float:
    """SPEC 2.4: 수량 = (계좌 × risk_per_trade_pct%) / |진입가 − 스탑가|.

    ★ SPEC 1 "레버리지 최대 2x" 캡을 여기서 강제한다 — 스탑이 가격 대비 아주
    좁은 구간(저변동기)에서는 리스크 기준 사이징만으로는 notional이 레버리지
    한도를 넘어설 수 있다(2026-07-24 실측: 캡 없이 백테스트 시 순간 2.8x까지
    발생). max_leverage가 주어지면 notional=qty×entry_price가
    account_equity_usd×max_leverage를 넘지 않도록 두 사이징 중 더 작은 쪽을 쓴다.

    qty_step이 주어지면 거래소 최소 단위로 내림. min_qty 미만이면 0(진입 불가)."""
    if account_equity_usd <= 0:
        raise ValueError("account_equity_usd는 0보다 커야 합니다.")
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        raise ValueError("entry_price와 stop_price가 같으면 사이징할 수 없습니다(0으로 나눔).")

    risk_amount = account_equity_usd * risk_per_trade_pct / 100
    qty = risk_amount / stop_distance

    if max_leverage is not None and max_leverage > 0:
        max_qty_by_leverage = (account_equity_usd * max_leverage) / entry_price
        qty = min(qty, max_qty_by_leverage)

    if qty_step is not None and qty_step > 0:
        qty = math.floor(qty / qty_step) * qty_step
    if min_qty is not None and qty < min_qty:
        return 0.0
    return qty


def daily_loss_limit_breached(daily_pnl_usd: float, account_equity_usd: float, daily_loss_limit_pct: float) -> bool:
    """SPEC 1: 일일 손실 -3% 도달 시 당일 신규 진입 금지(킬스위치).
    daily_loss_limit_pct는 음수(예: -3.0)로 넘긴다."""
    threshold = account_equity_usd * daily_loss_limit_pct / 100
    return daily_pnl_usd <= threshold


def can_open_new_position(open_symbols: set[str], symbol: str, max_concurrent: int) -> bool:
    """SPEC 1: 동시 최대 포지션 3(페어당 1). 이미 그 심볼에 포지션이 있으면 항상 거부."""
    if symbol in open_symbols:
        return False
    return len(open_symbols) < max_concurrent


def verify_stop_exists(has_open_position: bool, has_stop_order: bool) -> bool:
    """SPEC 2.4 라이브 전용: 매 루프 포지션 있는데 스탑 없으면 재배치 대상.
    True를 반환하면 '이상 없음(스탑 존재 또는 포지션 자체가 없음)'."""
    if not has_open_position:
        return True
    return has_stop_order


def consecutive_error_kill_switch_triggered(consecutive_error_count: int, threshold: int = 5) -> bool:
    """SPEC 2.4: 연속 5회 API 오류 시 전 포지션 청산 후 봇 정지."""
    return consecutive_error_count >= threshold
