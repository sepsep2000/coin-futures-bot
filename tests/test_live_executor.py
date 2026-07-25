"""tests/test_live_executor.py — src/live/executor.py 테스트.

정상/경계/실패 케이스는 전부 duck-typed 스텁 거래소로 검증한다(네트워크
호출 없음). 실제 testnet API를 쓰는 케이스는 @pytest.mark.integration로
분리(pytest.ini의 addopts로 기본 실행에서 제외, `pytest -m integration`
으로만 실행)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.live import executor
from src.live.state import init_db


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "state.db"
    init_db(path)
    return path


def _order_row(db_path: Path, order_id: str) -> dict:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        return dict(row) if row else None


# =====================================================================
# place_order
# =====================================================================

class _OrderStubExchange:
    def __init__(self, always_fail=False, order_id="ORD1", status="closed", filled=1.0, average=100.0):
        self.always_fail = always_fail
        self.order_id = order_id
        self.status = status
        self.filled = filled
        self.average = average
        self.calls = 0

    def create_order(self, symbol, order_type, side, qty, price=None):
        self.calls += 1
        if self.always_fail:
            raise RuntimeError("simulated exchange error")
        return {"id": self.order_id, "status": self.status, "filled": self.filled, "average": self.average}


def test_place_order_success_persists_filled_order(db_path):
    exchange = _OrderStubExchange()
    result = executor.place_order(exchange, db_path, "BTC/USDT:USDT", "long", 1.0)

    assert result.status == "filled"
    assert result.order_id == "ORD1"
    assert result.avg_fill_price == 100.0
    assert exchange.calls == 1

    row = _order_row(db_path, "ORD1")
    assert row is not None
    assert row["status"] == "filled"
    assert row["side"] == "buy"


def test_place_order_retries_exhausted_returns_failed_not_success(db_path, monkeypatch):
    """CLAUDE.md 원칙: 실패를 성공처럼 반환하지 않는다 — MAX_RETRIES 소진 시
    status='failed', order_id=None이어야 하고, state에도 실패가 기록돼야 한다."""
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    exchange = _OrderStubExchange(always_fail=True)

    result = executor.place_order(exchange, db_path, "BTC/USDT:USDT", "long", 1.0)

    assert result.status == "failed"
    assert result.order_id is None
    assert result.error is not None
    assert exchange.calls == executor.MAX_RETRIES

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM orders WHERE status = 'failed'").fetchall()]
    assert len(rows) == 1
    assert rows[0]["order_id"].startswith("FAILED_")


def test_place_order_invalid_direction_raises_before_any_order_call(db_path):
    exchange = _OrderStubExchange()
    with pytest.raises(ValueError):
        executor.place_order(exchange, db_path, "BTC/USDT:USDT", "sideways", 1.0)
    assert exchange.calls == 0


# =====================================================================
# poll_order_status
# =====================================================================

class _PollStubExchange:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def fetch_order(self, order_id, symbol):
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        resp = self.responses[idx]
        if isinstance(resp, Exception):
            raise resp
        return resp


def test_poll_order_status_returns_immediately_on_fill(db_path):
    exchange = _PollStubExchange([{"status": "closed", "filled": 1.0, "average": 100.0}])
    result = executor.poll_order_status(exchange, db_path, "ORD1", "BTC/USDT:USDT")

    assert result.status == "filled"
    assert result.avg_fill_price == 100.0
    assert exchange.calls == 1


def test_poll_order_status_timeout_returns_pending_not_error(db_path, monkeypatch):
    """timeout이 발생해도 예외를 던지거나 '실패'로 조작하지 않는다 — 마지막
    관측 상태(pending)를 그대로 반환해 호출부가 판단하게 한다."""
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    exchange = _PollStubExchange([{"status": "open", "filled": 0.0, "average": None}])

    result = executor.poll_order_status(exchange, db_path, "ORD1", "BTC/USDT:USDT", timeout_sec=0)

    assert result.status == "pending"
    assert result.error is None


def test_poll_order_status_survives_transient_fetch_error(db_path, monkeypatch):
    """실측(테스트넷)에서 확인된 케이스: 주문 직후 fetch_order가 일시적으로
    OrderNotFound를 던지다가 곧 정상 응답한다 — 이 일시 오류가 즉시 실패로
    전파되면 안 되고, 다음 폴링에서 회복돼야 한다."""
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    exchange = _PollStubExchange([
        RuntimeError("Order does not exist."),
        {"status": "closed", "filled": 1.0, "average": 100.0},
    ])

    result = executor.poll_order_status(exchange, db_path, "ORD1", "BTC/USDT:USDT")

    assert result.status == "filled"
    assert exchange.calls == 2


# =====================================================================
# cancel_order
# =====================================================================

class _CancelStubExchange:
    def __init__(self, fail=False):
        self.fail = fail

    def cancel_order(self, order_id, symbol):
        if self.fail:
            raise RuntimeError("cancel failed")
        return {"id": order_id, "status": "canceled"}


def test_cancel_order_success(db_path):
    assert executor.cancel_order(_CancelStubExchange(), db_path, "ORD1", "BTC/USDT:USDT") is True


def test_cancel_order_failure_returns_false_not_raise(db_path):
    assert executor.cancel_order(_CancelStubExchange(fail=True), db_path, "ORD1", "BTC/USDT:USDT") is False


# =====================================================================
# ensure_stop_placed
# =====================================================================

class _StopStubExchange:
    """fapiPrivateGetOpenAlgoOrders() 기반(실측으로 확인한 올바른 경로)을
    흉내낸다. create_order 성공 시 다음 조회부터 그 스탑이 보이도록
    상태를 흉내낸다(실거래소의 비동기 반영과 유사)."""

    def __init__(self, initial_stops=None, create_fails=False, market_id="BTCUSDT"):
        self._algo_orders = list(initial_stops or [])
        self._create_fails = create_fails
        self._market_id = market_id
        self.create_order_calls = 0

    def load_markets(self):
        pass

    def market(self, symbol):
        return {"id": self._market_id}

    def fapiPrivateGetOpenAlgoOrders(self):
        return list(self._algo_orders)

    def create_order(self, symbol, order_type, side, qty, params=None):
        self.create_order_calls += 1
        if self._create_fails:
            raise RuntimeError("simulated exchange error")
        self._algo_orders.append({
            "symbol": self._market_id, "orderType": "STOP_MARKET",
            "side": side.upper(), "algoStatus": "NEW", "algoId": f"ALGO{self.create_order_calls}",
        })
        return {"id": f"ALGO{self.create_order_calls}"}


def test_ensure_stop_placed_detects_existing_stop_without_duplicate(db_path):
    """실측으로 발견했던 버그(fetch_open_orders는 STOP_MARKET을 못 봄)의
    회귀 테스트 — 이미 스탑이 있으면 create_order를 호출하지 않아야 한다."""
    existing = [{"symbol": "BTCUSDT", "orderType": "STOP_MARKET", "side": "SELL", "algoStatus": "NEW"}]
    exchange = _StopStubExchange(initial_stops=existing)
    position = SimpleNamespace(direction="long", qty=0.002, current_stop=60000.0)

    result = executor.ensure_stop_placed(exchange, db_path, "BTC/USDT:USDT", position)

    assert result is True
    assert exchange.create_order_calls == 0


def test_ensure_stop_placed_creates_when_missing_then_confirms(db_path, monkeypatch):
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    exchange = _StopStubExchange(initial_stops=[])
    position = SimpleNamespace(direction="long", qty=0.002, current_stop=60000.0)

    result = executor.ensure_stop_placed(exchange, db_path, "BTC/USDT:USDT", position)

    assert result is True
    assert exchange.create_order_calls == 1


def test_ensure_stop_placed_retries_exhausted_returns_false_and_records_failure(db_path, monkeypatch):
    """★ 요구된 실패 케이스: 재시도 소진 시 절대 True(성공)를 반환하지
    않는다 — gate_verify.py의 공허한 PASS 버그와 같은 종류의 실수를
    반복하지 않는지 확인하는 핵심 테스트."""
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    exchange = _StopStubExchange(initial_stops=[], create_fails=True)
    position = SimpleNamespace(direction="long", qty=0.002, current_stop=60000.0)

    result = executor.ensure_stop_placed(exchange, db_path, "BTC/USDT:USDT", position, max_retries=2)

    assert result is False
    assert exchange.create_order_calls == 2

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM orders WHERE status = 'failed'").fetchall()]
    assert len(rows) == 1
    assert rows[0]["order_id"].startswith("STOP_MISSING_")


def test_ensure_stop_placed_short_position_uses_buy_side(db_path, monkeypatch):
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    exchange = _StopStubExchange(initial_stops=[])
    position = SimpleNamespace(direction="short", qty=0.002, current_stop=70000.0)

    result = executor.ensure_stop_placed(exchange, db_path, "BTC/USDT:USDT", position)

    assert result is True
    assert exchange._algo_orders[-1]["side"] == "BUY"


# =====================================================================
# get_authenticated_exchange — 자격증명 누락 시 값 대신 사유만 노출
# =====================================================================

def test_get_authenticated_exchange_missing_keys_raises_without_leaking(monkeypatch, tmp_path):
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setattr(executor, "ENV_PATH", empty_env)
    for key in ("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        executor.get_authenticated_exchange(testnet=True)

    assert "테스트넷" in str(excinfo.value)
