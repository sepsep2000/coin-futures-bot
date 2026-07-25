"""src/live/state.py — SQLite 상태 영속화.

LIVE_EXECUTION_ARCHITECTURE.md 2절 스키마를 그대로 구현한다(임의 변경
없음). 포지션/주문/자본스냅샷/리밸런스 이력을 SQLite에 저장해 프로세스
재시작 시 복구한다(SPEC.md 3절).

트레이드 이력(청산된 포지션의 영구 로그)은 이 파일의 범위가 아니다 —
`positions` 테이블은 "현재 열린" 포지션만 다룬다(delete_position()으로
제거된 이후의 보관 방식은 별도 결정 사항, LIVE_EXECUTION_ARCHITECTURE.md
6절 미결 목록 참조).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ReconciliationResult:
    """recover_state()의 반환값 — DB 기록과 거래소 실제 상태의 대조 결과."""
    positions_match: bool
    orders_match: bool
    mismatches: list[str] = field(default_factory=list)


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


_POSITIONS_MIGRATED_COLUMNS = {
    # 2026-07-25 추가: filtered_trend 라이브 청산을 스탑로스만이 아니라
    # 부분익절/트레일링/시간청산까지 engine.py의 _manage_trend_position()을
    # 그대로 재사용해 처리하려면 Position 데이터클래스가 요구하는 필드가
    # 전부 있어야 한다(reports/EXIT_LOGIC_COVERAGE.md). bars_held는 컬럼으로
    # 안 두고 entry_time에서 매 틱 계산한다(상태 이중관리 방지, 재시작에도
    # 안전) — 나머지 넷은 계산으로 복원 불가능해 반드시 저장해야 한다.
    "initial_stop": "REAL",
    "entry_fee_usd": "REAL NOT NULL DEFAULT 0.0",
    "funding_paid_usd": "REAL NOT NULL DEFAULT 0.0",
    "partial_taken": "INTEGER NOT NULL DEFAULT 0",
}


def _migrate_positions_schema(conn: sqlite3.Connection) -> None:
    """이미 존재하는 positions 테이블에 신규 컬럼을 추가한다(값이 있는 기존
    행은 DEFAULT로 채워짐 — filtered_trend 쪽은 이후 첫 재진입부터 정확한
    값이 채워지고, 마이그레이션 시점에 이미 열려있던 포지션의 initial_stop은
    NULL로 남는다 — 이런 포지션은 부분익절/트레일링 판단이 불가능하므로
    ensure_stop_placed의 최소 안전망(current_stop)만 계속 적용되고,
    scheduler.py가 재확인 후 청산하는 걸 권장(운영 절차, 코드가 자동으로
    강제하지 않음 — 이번 태스크 범위 밖)."""
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(positions)").fetchall()}
    for col, decl in _POSITIONS_MIGRATED_COLUMNS.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE positions ADD COLUMN {col} {decl}")


def init_db(db_path: Path) -> None:
    """SQLite 파일 생성 + 4개 테이블(positions/orders/equity_snapshots/
    rebalance_log) 스키마 적용. 이미 존재하면 `CREATE TABLE IF NOT EXISTS`라
    아무 일도 안 함(멱등). 컬럼 추가형 마이그레이션은
    `_migrate_positions_schema()`가 담당(TODO: 컬럼 삭제/타입변경처럼
    ALTER TABLE로 못 하는 마이그레이션은 여전히 다루지 않음, 다음 세션)."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT NOT NULL,
                strategy TEXT NOT NULL,
                direction TEXT NOT NULL,
                qty REAL NOT NULL,
                entry_price REAL NOT NULL,
                entry_time TEXT NOT NULL,
                current_stop REAL,
                PRIMARY KEY (symbol, strategy)
            )
            """
        )
        _migrate_positions_schema(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                price REAL,
                status TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                total_equity_usd REAL NOT NULL,
                filtered_trend_equity_usd REAL NOT NULL,
                two_a_equity_usd REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rebalance_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                strategy TEXT NOT NULL,
                new_long TEXT NOT NULL,
                new_short TEXT NOT NULL,
                exited_long TEXT NOT NULL,
                exited_short TEXT NOT NULL,
                turnover_pct REAL NOT NULL,
                realized_pnl_usd REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_strategy ON positions(strategy)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        conn.commit()


def save_metadata(db_path: Path, key: str, value: str) -> None:
    """범용 key-value 저장소 — 2026-07-25 추가, 첫 용도는 G4 시작 시각
    기록(scripts/healthcheck.py). 매번 새 테이블을 만드는 대신 이후에도
    비슷한 단일값 메타데이터가 생기면 재사용한다."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def load_metadata(db_path: Path, key: str) -> Optional[str]:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


G4_START_TIMESTAMP_KEY = "g4_start_timestamp"


def save_g4_start_timestamp(db_path: Path, ts: str) -> None:
    """G4(페이퍼 트레이딩) 착수 시각 기록. 이 testnet 계좌는 이전 봇("코인봇
    1세대")이 쓰던 계좌라 계좌 레벨 조회에 그 봇의 과거 활동이 섞여있을 수
    있다 — 향후 거래 이력(fetch_my_trades 등 과거 데이터) 조회 기능을
    추가할 때는 반드시 이 시각 이후로 필터링해야 한다.

    ★ 2026-07-25 확인: recover_state()가 쓰는 exchange.fetch_positions()/
    fetch_open_orders()는 "현재" 상태만 반환하는 함수라(청산된 포지션·
    체결/취소된 과거 주문은 애초에 안 나옴) 이 필터링이 필요 없다 — 이전
    봇이 남긴 미청산 포지션/미체결 주문이 있다면 그건 실제로 지금 계좌에
    존재하는 이상 상태라 오히려 플래그돼야 맞다. 이 함수는 이후 과거이력
    조회 기능(예: 30건 페이퍼 트레이딩 판정 리뷰)을 위한 기준점 제공용."""
    save_metadata(db_path, G4_START_TIMESTAMP_KEY, ts)


def load_g4_start_timestamp(db_path: Path) -> Optional[str]:
    return load_metadata(db_path, G4_START_TIMESTAMP_KEY)


def save_position(db_path: Path, symbol: str, strategy: str, direction: str, qty: float,
                   entry_price: float, entry_time: str, current_stop: Optional[float],
                   initial_stop: Optional[float] = None, entry_fee_usd: float = 0.0,
                   funding_paid_usd: float = 0.0, partial_taken: bool = False) -> None:
    """포지션 upsert. strategy='filtered_trend'|'2a', (symbol, strategy) 복합키라
    같은 symbol이 두 전략에 동시에(예: ETH가 filtered_trend 포지션이면서
    동시에 2a 바스켓에도 포함) 존재할 수 있다 — 의도된 동작.

    ★ initial_stop/entry_fee_usd/funding_paid_usd/partial_taken은 2026-07-25
    추가(EXIT_LOGIC_COVERAGE.md) — filtered_trend 전용, engine.py의
    Position 데이터클래스를 라이브에서 재구성하는 데 필요. 2a는 이 필드들을
    안 쓴다(기본값 그대로 저장, 2a 포지션 행에서는 의미 없음)."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO positions (symbol, strategy, direction, qty, entry_price, entry_time, current_stop,
                                    initial_stop, entry_fee_usd, funding_paid_usd, partial_taken)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, strategy) DO UPDATE SET
                direction=excluded.direction, qty=excluded.qty, entry_price=excluded.entry_price,
                entry_time=excluded.entry_time, current_stop=excluded.current_stop,
                initial_stop=excluded.initial_stop, entry_fee_usd=excluded.entry_fee_usd,
                funding_paid_usd=excluded.funding_paid_usd, partial_taken=excluded.partial_taken
            """,
            (symbol, strategy, direction, qty, entry_price, entry_time, current_stop,
             initial_stop, entry_fee_usd, funding_paid_usd, int(partial_taken)),
        )
        conn.commit()


def load_open_positions(db_path: Path, strategy: Optional[str] = None) -> list[dict]:
    """strategy가 None이면 전체, 아니면 해당 전략만."""
    with closing(_connect(db_path)) as conn:
        if strategy is None:
            rows = conn.execute("SELECT * FROM positions").fetchall()
        else:
            rows = conn.execute("SELECT * FROM positions WHERE strategy = ?", (strategy,)).fetchall()
        return [dict(r) for r in rows]


def delete_position(db_path: Path, symbol: str, strategy: str) -> None:
    """포지션 청산 완료 시 제거. 이력 보관 방식은 별도 결정 사항
    (LIVE_EXECUTION_ARCHITECTURE.md 6절) — 이 함수는 "현재 상태"에서만
    제거한다."""
    with closing(_connect(db_path)) as conn:
        conn.execute("DELETE FROM positions WHERE symbol = ? AND strategy = ?", (symbol, strategy))
        conn.commit()


def save_order(db_path: Path, order_id: str, symbol: str, side: str, qty: float,
               price: Optional[float], status: str, created_at: Optional[str] = None,
               updated_at: Optional[str] = None) -> None:
    """골격의 원 시그니처(created_at/updated_at 없음)에 스키마가 요구하는
    두 컬럼(orders 테이블, 설계 문서 2절)을 옵션 파라미터로 추가했다 —
    스키마 자체(테이블 구조)는 바꾸지 않았고, 값을 안 넘기면 현재 UTC
    시각으로 자동 채운다(호출부 부담 최소화)."""
    created_at = created_at or _now_iso()
    updated_at = updated_at or created_at
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO orders (order_id, symbol, side, qty, price, status, retry_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                symbol=excluded.symbol, side=excluded.side, qty=excluded.qty, price=excluded.price,
                status=excluded.status, updated_at=excluded.updated_at
            """,
            (order_id, symbol, side, qty, price, status, created_at, updated_at),
        )
        conn.commit()


def update_order_status(db_path: Path, order_id: str, status: str,
                         retry_count: Optional[int] = None, updated_at: Optional[str] = None) -> None:
    """updated_at 미지정 시 현재 UTC 시각으로 자동 채움(save_order와 동일 이유)."""
    updated_at = updated_at or _now_iso()
    with closing(_connect(db_path)) as conn:
        if retry_count is None:
            conn.execute("UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?", (status, updated_at, order_id))
        else:
            conn.execute(
                "UPDATE orders SET status = ?, updated_at = ?, retry_count = ? WHERE order_id = ?",
                (status, updated_at, retry_count, order_id),
            )
        conn.commit()


def load_pending_orders(db_path: Path) -> list[dict]:
    """재시작 시 미체결 주문 복구용 — status가 pending 또는 partial인 것."""
    with closing(_connect(db_path)) as conn:
        rows = conn.execute("SELECT * FROM orders WHERE status IN ('pending', 'partial')").fetchall()
        return [dict(r) for r in rows]


def load_equity_snapshots_since(db_path: Path, since_iso: str) -> list[dict]:
    """ts >= since_iso인 스냅샷 전부, ts 오름차순 — src/live/scheduler.py의
    일일 손실한도 계산(오늘 첫 스냅샷 대비 현재 자본 변화)에 필요해 추가.
    save_equity_snapshot()의 대응 조회 함수가 원래 없었다(쓰기만 있고
    읽기가 없어 킬스위치 로직이 과거 스냅샷을 조회할 방법이 없었음)."""
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM equity_snapshots WHERE ts >= ? ORDER BY ts ASC", (since_iso,)
        ).fetchall()
        return [dict(r) for r in rows]


def save_equity_snapshot(db_path: Path, ts: str, total_equity_usd: float,
                          filtered_trend_equity_usd: float, two_a_equity_usd: float) -> None:
    """일일 손실한도 계산 + 일일 요약 알림용."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO equity_snapshots (ts, total_equity_usd, filtered_trend_equity_usd, two_a_equity_usd)
            VALUES (?, ?, ?, ?)
            """,
            (ts, total_equity_usd, filtered_trend_equity_usd, two_a_equity_usd),
        )
        conn.commit()


def save_rebalance_log(db_path: Path, ts: str, new_long: list[str], new_short: list[str],
                        exited_long: list[str], exited_short: list[str],
                        turnover_pct: float, realized_pnl_usd: float) -> None:
    """2a 주간 리밸런스 실행 이력. 자산 목록(list[str])은 JSON 문자열로
    직렬화해 저장(SQLite에 배열 타입이 없음). strategy 컬럼은 현재 "2a"
    고정값(설계 문서 표 그대로 — 다른 리밸런스형 전략이 생기면 그때 확장)."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO rebalance_log (ts, strategy, new_long, new_short, exited_long, exited_short, turnover_pct, realized_pnl_usd)
            VALUES (?, '2a', ?, ?, ?, ?, ?, ?)
            """,
            (ts, json.dumps(new_long), json.dumps(new_short), json.dumps(exited_long), json.dumps(exited_short),
             turnover_pct, realized_pnl_usd),
        )
        conn.commit()


def recover_state(db_path: Path, exchange) -> ReconciliationResult:
    """LIVE_EXECUTION_ARCHITECTURE.md 2절 "재시작 시 복구" 3단계:
    (1) DB에서 positions/orders 로드
    (2) 거래소 API로 실제 계좌 포지션·미체결주문 조회(exchange.fetch_positions()/
        fetch_open_orders() — ccxt 통일 형식을 기대하되, 테스트에서는 같은
        인터페이스의 스텁 객체를 주입할 수 있다, 덕타이핑)
    (3) 대조 — 불일치 시 자동 덮어쓰기 금지, ReconciliationResult로 반환해
        호출부가 CRITICAL 알림을 보내도록 함(이 함수는 알림을 직접 보내지
        않는다 — 알림은 src/notify 책임).

    조회 자체가 실패하면(네트워크 등) positions_match/orders_match를 둘 다
    False로 반환하고 mismatches에 에러 사유를 담는다 — "확인 불가"를
    "일치"로 취급하지 않는다."""
    db_positions = load_open_positions(db_path)
    db_orders = load_pending_orders(db_path)
    mismatches: list[str] = []

    try:
        exchange_positions = exchange.fetch_positions()
    except Exception as exc:  # noqa: BLE001 - 조회 실패 자체를 결과에 담아 반환
        return ReconciliationResult(False, False, [f"거래소 포지션 조회 실패: {type(exc).__name__}: {exc}"])

    try:
        exchange_orders = exchange.fetch_open_orders()
    except Exception as exc:  # noqa: BLE001
        return ReconciliationResult(False, False, [f"거래소 미체결주문 조회 실패: {type(exc).__name__}: {exc}"])

    exchange_pos_symbols = {
        p.get("symbol") for p in exchange_positions if float(p.get("contracts") or 0) != 0
    }
    db_pos_symbols = {p["symbol"] for p in db_positions}

    for symbol in sorted(db_pos_symbols - exchange_pos_symbols):
        mismatches.append(f"포지션 불일치: DB엔 있는데 거래소엔 없음 ({symbol})")
    for symbol in sorted(exchange_pos_symbols - db_pos_symbols):
        mismatches.append(f"포지션 불일치: 거래소엔 있는데 DB엔 없음 ({symbol})")

    exchange_order_ids = {o.get("id") for o in exchange_orders}
    db_order_ids = {o["order_id"] for o in db_orders}

    for oid in sorted(db_order_ids - exchange_order_ids):
        mismatches.append(f"주문 불일치: DB엔 있는데 거래소엔 없음 ({oid})")
    for oid in sorted(exchange_order_ids - db_order_ids):
        mismatches.append(f"주문 불일치: 거래소엔 있는데 DB엔 없음 ({oid})")

    positions_match = not any(m.startswith("포지션 불일치") for m in mismatches)
    orders_match = not any(m.startswith("주문 불일치") for m in mismatches)

    return ReconciliationResult(positions_match, orders_match, mismatches)
