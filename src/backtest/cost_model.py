"""src/backtest/cost_model.py — SPEC 1 비용 모델 (수수료/슬리피지/펀딩비).

순수함수. 백테스트 엔진(Phase 2)과 향후 라이브 실행기(Phase 4)가 공용으로 쓴다
(SPEC 3절 "백테스트≠실전 괴리 원천 차단" 원칙).
"""

from __future__ import annotations


def taker_fee(notional_usd: float, taker_fee_pct: float) -> float:
    return notional_usd * taker_fee_pct / 100


def maker_fee(notional_usd: float, maker_fee_pct: float) -> float:
    return notional_usd * maker_fee_pct / 100


def slippage_cost(notional_usd: float, slippage_pct: float) -> float:
    return notional_usd * slippage_pct / 100


def funding_fee(notional_usd: float, funding_rate: float, direction: str) -> float:
    """펀딩 정산 1회분 비용. 표준 컨벤션: funding_rate>0이면 롱이 숏에게 지불한다.
    반환값은 항상 '비용' 관점(양수=지출, 음수=수취)으로 방향을 반영해 부호를 맞춘다."""
    if direction == "long":
        return notional_usd * funding_rate
    if direction == "short":
        return -notional_usd * funding_rate
    raise ValueError(f"direction은 'long' 또는 'short'여야 합니다: {direction!r}")


def entry_fill_price(reference_price: float, direction: str, slippage_pct: float, order_type: str = "market") -> float:
    """시장가 진입 체결가 근사: 롱은 더 비싸게, 숏은 더 싸게 체결된다(불리한 쪽).
    지정가(order_type='limit')는 메이커 우선 체결 가정으로 슬리피지 없이 reference_price 그대로."""
    if order_type == "limit":
        return reference_price
    if direction == "long":
        return reference_price * (1 + slippage_pct / 100)
    if direction == "short":
        return reference_price * (1 - slippage_pct / 100)
    raise ValueError(f"direction은 'long' 또는 'short'여야 합니다: {direction!r}")


def exit_fill_price(reference_price: float, direction: str, slippage_pct: float, order_type: str = "market") -> float:
    """청산 체결가 근사: 청산은 진입의 반대 방향 불리함을 겪는다
    (롱 청산=매도 -> 더 싸게, 숏 청산=매수 -> 더 비싸게)."""
    if order_type == "limit":
        return reference_price
    if direction == "long":
        return reference_price * (1 - slippage_pct / 100)
    if direction == "short":
        return reference_price * (1 + slippage_pct / 100)
    raise ValueError(f"direction은 'long' 또는 'short'여야 합니다: {direction!r}")
