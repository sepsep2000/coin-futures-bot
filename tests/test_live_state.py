import sqlite3
from pathlib import Path

import pytest

from src.live.state import (
    ReconciliationResult,
    delete_position,
    init_db,
    load_equity_snapshots_since,
    load_g4_start_timestamp,
    load_metadata,
    load_open_positions,
    load_pending_orders,
    recover_state,
    save_equity_snapshot,
    save_g4_start_timestamp,
    save_metadata,
    save_order,
    save_position,
    save_rebalance_log,
    update_order_status,
)


class _StubExchange:
    def __init__(self, positions=None, orders=None, raise_on="none"):
        self._positions = positions or []
        self._orders = orders or []
        self._raise_on = raise_on

    def fetch_positions(self):
        if self._raise_on == "positions":
            raise ConnectionError("network down")
        return self._positions

    def fetch_open_orders(self):
        if self._raise_on == "orders":
            raise ConnectionError("network down")
        return self._orders


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "state.db"
    init_db(path)
    return path


# --- 정상 케이스 ---

def test_save_and_load_position_roundtrip(db_path):
    save_position(db_path, "ETH/USDT:USDT", "filtered_trend", "long", 1.5, 3000.0, "2026-07-25T00:00:00Z", 2900.0)
    positions = load_open_positions(db_path, strategy="filtered_trend")
    assert len(positions) == 1
    assert positions[0]["symbol"] == "ETH/USDT:USDT"
    assert positions[0]["qty"] == 1.5
    assert positions[0]["current_stop"] == 2900.0


def test_save_position_with_exit_logic_fields_roundtrip(db_path):
    """2026-07-25 추가 필드(initial_stop/entry_fee_usd/funding_paid_usd/
    partial_taken) - filtered_trend의 부분익절/트레일링 재구성에 필요."""
    save_position(db_path, "ETH/USDT:USDT", "filtered_trend", "long", 1.5, 3000.0, "2026-07-25T00:00:00Z", 2850.0,
                   initial_stop=2850.0, entry_fee_usd=2.25, funding_paid_usd=0.0, partial_taken=False)
    positions = load_open_positions(db_path, strategy="filtered_trend")
    assert positions[0]["initial_stop"] == 2850.0
    assert positions[0]["entry_fee_usd"] == 2.25
    assert positions[0]["funding_paid_usd"] == 0.0
    assert positions[0]["partial_taken"] == 0

    # 부분익절 후 갱신(같은 (symbol, strategy) 재저장 -> upsert)
    save_position(db_path, "ETH/USDT:USDT", "filtered_trend", "long", 1.125, 3000.0, "2026-07-25T00:00:00Z", 3000.0,
                   initial_stop=2850.0, entry_fee_usd=2.25, funding_paid_usd=0.15, partial_taken=True)
    positions = load_open_positions(db_path, strategy="filtered_trend")
    assert len(positions) == 1
    assert positions[0]["qty"] == 1.125
    assert positions[0]["partial_taken"] == 1
    assert positions[0]["funding_paid_usd"] == 0.15


def test_save_position_without_exit_logic_fields_uses_defaults(db_path):
    """기존 호출부(파라미터 생략)와 하위호환 - 2a처럼 이 필드들을 안 쓰는
    호출도 깨지지 않아야 한다."""
    save_position(db_path, "BTC/USDT:USDT", "2a", "long", 0.01, 60000.0, "2026-07-25T00:00:00Z", 42000.0)
    positions = load_open_positions(db_path, strategy="2a")
    assert positions[0]["initial_stop"] is None
    assert positions[0]["entry_fee_usd"] == 0.0
    assert positions[0]["funding_paid_usd"] == 0.0
    assert positions[0]["partial_taken"] == 0


def test_init_db_migrates_existing_positions_table_missing_new_columns(tmp_path):
    """마이그레이션 경계 케이스: 신규 컬럼 없이 만들어진 구버전 positions
    테이블에 init_db()를 다시 돌리면 컬럼이 추가되고, 기존 행은 깨지지
    않아야 한다(값은 DEFAULT로 채워짐)."""
    db_path = tmp_path / "legacy_state.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE positions (
                symbol TEXT NOT NULL, strategy TEXT NOT NULL, direction TEXT NOT NULL,
                qty REAL NOT NULL, entry_price REAL NOT NULL, entry_time TEXT NOT NULL,
                current_stop REAL, PRIMARY KEY (symbol, strategy)
            )
            """
        )
        conn.execute(
            "INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("ETH/USDT:USDT", "filtered_trend", "long", 1.0, 3000.0, "2026-07-24T00:00:00Z", 2900.0),
        )
        conn.commit()

    init_db(db_path)  # 마이그레이션 실행

    positions = load_open_positions(db_path, strategy="filtered_trend")
    assert len(positions) == 1
    assert positions[0]["qty"] == 1.0  # 기존 행 보존
    assert positions[0]["initial_stop"] is None  # 신규 컬럼은 DEFAULT(NULL)
    assert positions[0]["entry_fee_usd"] == 0.0
    assert positions[0]["partial_taken"] == 0


def test_recover_state_matches_when_db_and_exchange_agree(db_path):
    save_position(db_path, "ETH/USDT:USDT", "filtered_trend", "long", 1.0, 3000.0, "2026-07-25T00:00:00Z", 2900.0)
    save_order(db_path, "ORD1", "ETH/USDT:USDT", "buy", 1.0, 3000.0, "pending")

    exchange = _StubExchange(
        positions=[{"symbol": "ETH/USDT:USDT", "contracts": 1.0}],
        orders=[{"id": "ORD1"}],
    )
    result = recover_state(db_path, exchange)
    assert result == ReconciliationResult(positions_match=True, orders_match=True, mismatches=[])


# --- 경계 케이스: 완전히 빈 상태 ---

def test_init_db_then_recover_state_on_empty_state_matches(db_path):
    """init_db 직후(아무 포지션/주문도 저장 전) recover_state가 거래소도
    비어있으면 정확히 '일치'로 반환하는지 — 빈 상태를 불일치로 오판하면
    안 된다."""
    assert load_open_positions(db_path) == []
    assert load_pending_orders(db_path) == []

    result = recover_state(db_path, _StubExchange(positions=[], orders=[]))
    assert result.positions_match is True
    assert result.orders_match is True
    assert result.mismatches == []


# --- 실패/불일치 케이스 ---

def test_recover_state_detects_position_mismatch(db_path):
    """DB에 없는 포지션이 거래소에 있으면(또는 그 반대) 절대 '일치'로
    조용히 넘어가면 안 된다 — CRITICAL 알림의 전제가 되는 신호."""
    save_position(db_path, "ETH/USDT:USDT", "filtered_trend", "long", 1.0, 3000.0, "2026-07-25T00:00:00Z", 2900.0)

    exchange = _StubExchange(
        positions=[
            {"symbol": "ETH/USDT:USDT", "contracts": 1.0},
            {"symbol": "BTC/USDT:USDT", "contracts": 2.0},  # DB엔 없는 포지션
        ],
        orders=[],
    )
    result = recover_state(db_path, exchange)
    assert result.positions_match is False
    assert any("BTC/USDT:USDT" in m for m in result.mismatches)


def test_recover_state_exchange_unreachable_is_not_treated_as_match(db_path):
    """거래소 조회 자체가 실패하면 '확인 불가'를 '일치'로 취급하지 않는다
    — positions_match/orders_match 둘 다 False, 에러 사유가 mismatches에
    담긴다."""
    result = recover_state(db_path, _StubExchange(raise_on="positions"))
    assert result.positions_match is False
    assert result.orders_match is False
    assert len(result.mismatches) == 1
    assert "포지션 조회 실패" in result.mismatches[0]


# --- 나머지 CRUD 동작 확인 ---

def test_delete_position_removes_only_matching_row(db_path):
    save_position(db_path, "ETH/USDT:USDT", "filtered_trend", "long", 1.0, 3000.0, "t", None)
    save_position(db_path, "BTC/USDT:USDT", "2a", "short", 0.1, 60000.0, "t", None)

    delete_position(db_path, "ETH/USDT:USDT", "filtered_trend")

    remaining = load_open_positions(db_path)
    assert len(remaining) == 1
    assert remaining[0]["symbol"] == "BTC/USDT:USDT"


def test_order_status_transition_removes_from_pending(db_path):
    save_order(db_path, "ORD1", "ETH/USDT:USDT", "buy", 1.0, 3000.0, "pending")
    assert len(load_pending_orders(db_path)) == 1

    update_order_status(db_path, "ORD1", "filled")
    assert load_pending_orders(db_path) == []


def test_save_equity_snapshot_and_rebalance_log_do_not_raise(db_path):
    save_equity_snapshot(db_path, "2026-07-25T00:00:00Z", 1000.0, 838.3, 161.7)
    save_rebalance_log(db_path, "2026-07-25T00:00:00Z", ["BTC", "ETH"], ["SOL"], [], ["XRP"], 25.0, 12.5)


def test_load_equity_snapshots_since_filters_and_orders_ascending(db_path):
    save_equity_snapshot(db_path, "2026-07-19T00:00:00Z", 900.0, 800.0, 100.0)  # 필터 범위 밖(이전 날짜)
    save_equity_snapshot(db_path, "2026-07-20T06:00:00Z", 1000.0, 838.3, 161.7)
    save_equity_snapshot(db_path, "2026-07-20T00:15:00Z", 950.0, 800.0, 150.0)

    rows = load_equity_snapshots_since(db_path, "2026-07-20T00:00:00Z")

    assert [r["ts"] for r in rows] == ["2026-07-20T00:15:00Z", "2026-07-20T06:00:00Z"]


def test_load_equity_snapshots_since_empty_when_none_match(db_path):
    save_equity_snapshot(db_path, "2026-07-19T00:00:00Z", 900.0, 800.0, 100.0)
    assert load_equity_snapshots_since(db_path, "2026-07-20T00:00:00Z") == []


# --- metadata / G4 시작 시각 (2026-07-25 추가) ---

def test_save_and_load_metadata_roundtrip(db_path):
    save_metadata(db_path, "some_key", "some_value")
    assert load_metadata(db_path, "some_key") == "some_value"


def test_load_metadata_missing_key_returns_none(db_path):
    assert load_metadata(db_path, "never_set") is None


def test_save_metadata_upserts_existing_key(db_path):
    save_metadata(db_path, "k", "v1")
    save_metadata(db_path, "k", "v2")
    assert load_metadata(db_path, "k") == "v2"


def test_save_and_load_g4_start_timestamp_roundtrip(db_path):
    assert load_g4_start_timestamp(db_path) is None
    save_g4_start_timestamp(db_path, "2026-07-25T00:00:00+00:00")
    assert load_g4_start_timestamp(db_path) == "2026-07-25T00:00:00+00:00"
