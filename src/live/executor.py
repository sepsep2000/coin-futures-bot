"""src/live/executor.py — 주문 실행 계층 (골격, 구현 없음).

LIVE_EXECUTION_ARCHITECTURE.md 1절/5절 대응. 이 파일만 testnet/live를
분기한다(그 위 신호계산/사이징은 src/strategy, src/risk를 백테스트와
동일하게 호출) — src/data/feed.py의 get_exchange(testnet) 패턴을 그대로
재사용한다(신규 패턴 발명 없음).

★ 골격만 — 실제 ccxt 호출/재시도/폴링 로직은 다음 세션에서 구현.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

MAX_RETRIES = 3  # CLAUDE.md 오류 처리 표준: 지수 백오프 3회 재시도
POLL_INTERVAL_SEC = 2  # TODO: 실측 후 조정 여지 있음(설계 확정치 아님)


@dataclass
class OrderResult:
    order_id: Optional[str]
    status: str  # "filled" | "partial" | "pending" | "failed"
    filled_qty: float
    avg_fill_price: Optional[float]
    error: Optional[str] = None


def place_order(exchange, symbol: str, direction: str, qty: float, order_type: str = "market",
                 limit_price: Optional[float] = None) -> OrderResult:
    """주문 제출 + 즉시 주문 ID 확보. fire-and-forget 금지(CLAUDE.md) —
    이 함수는 제출만 하고 체결 확인은 poll_order_status()가 별도로 한다.
    실패 시 MAX_RETRIES(3회) 지수 백오프 후 CRITICAL(src.notify 호출은
    이 함수가 아니라 상위 스케줄러 책임 — 여기선 OrderResult.error로만 반환).
    """
    raise NotImplementedError("TODO: exchange.create_order() 호출 + 재시도 루프")


def poll_order_status(exchange, order_id: str, symbol: str, timeout_sec: int = 60) -> OrderResult:
    """주문 ID 폴링으로 체결 확인(CLAUDE.md: fire-and-forget 금지).
    POLL_INTERVAL_SEC 간격으로 timeout_sec까지 조회, 부분체결은
    filled_qty만 반영하고 status="partial"로 반환(handle_partial_fill로
    후속 처리는 호출부 책임)."""
    raise NotImplementedError("TODO: exchange.fetch_order() 폴링 루프")


def cancel_order(exchange, order_id: str, symbol: str) -> bool:
    raise NotImplementedError("TODO: exchange.cancel_order()")


def handle_partial_fill(exchange, order_result: OrderResult, policy: str = "leave_pending") -> OrderResult:
    """미체결 잔량 처리 정책. LIVE_EXECUTION_ARCHITECTURE.md 6절 —
    구체적 재주문 파라미터(대기시간/가격조정)는 다음 세션에서 결정,
    이번엔 정책 인터페이스만 정의(policy: "leave_pending" | "cancel_remainder"
    | "reprice" — 셋 다 미구현)."""
    raise NotImplementedError("TODO: policy별 분기 구현")


def ensure_stop_placed(exchange, symbol: str, position) -> bool:
    """포지션 존재 + 스탑 부재 = 최우선 복구 대상(CLAUDE.md). 스케줄러의
    매 틱 루프에서 가장 먼저 호출돼야 한다 — 이 함수 자체가 우선순위를
    강제하지는 않음(호출 순서는 scheduler.py 책임), 여기선 "스탑이 실제로
    거래소에 걸려있는지" 확인 + 없으면 재배치만 담당."""
    raise NotImplementedError("TODO: exchange.fetch_open_orders()에서 스탑 존재 확인 -> 없으면 재배치")
