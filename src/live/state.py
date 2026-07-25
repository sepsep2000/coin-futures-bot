"""src/live/state.py — SQLite 상태 영속화 (골격, 구현 없음).

LIVE_EXECUTION_ARCHITECTURE.md 2절 대응. 포지션/주문/자본스냅샷/리밸런스
이력을 SQLite에 저장해 프로세스 재시작 시 복구한다(SPEC.md 3절).

★ 이 파일은 함수 시그니처와 스키마 설계만 담는다 — 실제 구현은 다음
세션. 모든 함수 본문은 TODO + NotImplementedError.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# 스키마 (LIVE_EXECUTION_ARCHITECTURE.md 2절 표 그대로)
# ---------------------------------------------------------------------------
# positions: symbol, strategy, direction, qty, entry_price, entry_time, current_stop, ...
# orders: order_id(거래소), symbol, side, qty, price, status(pending/filled/partial/
#         cancelled/failed), retry_count, created_at, updated_at
# equity_snapshots: ts, total_equity_usd, filtered_trend_equity_usd, 2a_equity_usd
# rebalance_log: ts, strategy="2a", new_long, new_short, exited_long, exited_short,
#                turnover_pct, realized_pnl_usd


@dataclass
class ReconciliationResult:
    """recover_state()의 반환값 — DB 기록과 거래소 실제 상태의 대조 결과."""
    positions_match: bool
    orders_match: bool
    mismatches: list[str]


def init_db(db_path: Path) -> None:
    """SQLite 파일 생성 + 4개 테이블(positions/orders/equity_snapshots/
    rebalance_log) 스키마 적용. 이미 존재하면 마이그레이션(TODO: 버전 관리
    방식 결정 필요 — 다음 세션).
    """
    raise NotImplementedError("TODO: CREATE TABLE IF NOT EXISTS x4 + 인덱스")


def save_position(db_path: Path, symbol: str, strategy: str, direction: str, qty: float,
                   entry_price: float, entry_time: str, current_stop: Optional[float]) -> None:
    """포지션 upsert. strategy='filtered_trend'|'2a'."""
    raise NotImplementedError("TODO: INSERT OR REPLACE INTO positions")


def load_open_positions(db_path: Path, strategy: Optional[str] = None) -> list[dict]:
    """strategy가 None이면 전체, 아니면 해당 전략만."""
    raise NotImplementedError("TODO: SELECT * FROM positions WHERE ...")


def delete_position(db_path: Path, symbol: str, strategy: str) -> None:
    """포지션 청산 완료 시 제거(이력은 별도 trades 테이블 또는 기존
    reports/signal_validation_data 스타일 CSV 로그로 — TODO: 결정 필요)."""
    raise NotImplementedError("TODO: DELETE FROM positions WHERE ...")


def save_order(db_path: Path, order_id: str, symbol: str, side: str, qty: float,
               price: Optional[float], status: str) -> None:
    raise NotImplementedError("TODO: INSERT INTO orders")


def update_order_status(db_path: Path, order_id: str, status: str, retry_count: Optional[int] = None) -> None:
    raise NotImplementedError("TODO: UPDATE orders SET status=...")


def load_pending_orders(db_path: Path) -> list[dict]:
    """재시작 시 미체결 주문 복구용."""
    raise NotImplementedError("TODO: SELECT * FROM orders WHERE status='pending'")


def save_equity_snapshot(db_path: Path, ts: str, total_equity_usd: float,
                          filtered_trend_equity_usd: float, two_a_equity_usd: float) -> None:
    """일일 손실한도 계산 + 일일 요약 알림용."""
    raise NotImplementedError("TODO: INSERT INTO equity_snapshots")


def save_rebalance_log(db_path: Path, ts: str, new_long: list[str], new_short: list[str],
                        exited_long: list[str], exited_short: list[str],
                        turnover_pct: float, realized_pnl_usd: float) -> None:
    """2a 주간 리밸런스 실행 이력(portfolio_2a_net_trades.csv 스키마 참고)."""
    raise NotImplementedError("TODO: INSERT INTO rebalance_log")


def recover_state(db_path: Path, exchange) -> ReconciliationResult:
    """LIVE_EXECUTION_ARCHITECTURE.md 2절 "재시작 시 복구" 3단계:
    (1) DB에서 positions/orders 로드
    (2) 거래소 API로 실제 계좌 포지션·미체결주문 조회
    (3) 대조 — 불일치 시 자동 덮어쓰기 금지, ReconciliationResult로 반환해
        호출부가 CRITICAL 알림을 보내도록 함(이 함수는 알림을 직접 보내지
        않는다 — 알림은 src/notify 책임).
    """
    raise NotImplementedError("TODO: DB positions/orders vs exchange 실제 상태 대조")
