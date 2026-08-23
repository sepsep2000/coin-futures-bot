"""tests/test_live_executor.py — src/live/executor.py 테스트.

정상/경계/실패 케이스는 전부 duck-typed 스텁 거래소로 검증한다(네트워크
호출 없음). 실제 testnet API를 쓰는 케이스는 @pytest.mark.integration로
분리(pytest.ini의 addopts로 기본 실행에서 제외, `pytest -m integration`
으로만 실행)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import ccxt
import numpy as np
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

    def create_order(self, symbol, order_type, side, qty, price=None, params=None):
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
        self.last_params = params
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

    assert result.stop_confirmed is True
    assert result.closed_instead is False
    assert exchange.create_order_calls == 0


def test_ensure_stop_placed_creates_when_missing_then_confirms(db_path, monkeypatch):
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    exchange = _StopStubExchange(initial_stops=[])
    position = SimpleNamespace(direction="long", qty=0.002, current_stop=60000.0)

    result = executor.ensure_stop_placed(exchange, db_path, "BTC/USDT:USDT", position)

    assert result.stop_confirmed is True
    assert exchange.create_order_calls == 1


def test_ensure_stop_placed_retries_exhausted_returns_false_and_records_failure(db_path, monkeypatch):
    """★ 요구된 실패 케이스: 재시도 소진 시 절대 True(성공)를 반환하지
    않는다 — gate_verify.py의 공허한 PASS 버그와 같은 종류의 실수를
    반복하지 않는지 확인하는 핵심 테스트."""
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    exchange = _StopStubExchange(initial_stops=[], create_fails=True)
    position = SimpleNamespace(direction="long", qty=0.002, current_stop=60000.0)

    result = executor.ensure_stop_placed(exchange, db_path, "BTC/USDT:USDT", position, max_retries=2)

    assert result.stop_confirmed is False
    assert result.closed_instead is False
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

    assert result.stop_confirmed is True
    assert exchange._algo_orders[-1]["side"] == "BUY"


# =====================================================================
# ensure_stop_placed — 2026-08-11 사고③ 회귀 테스트(근본원인 A: numpy.float64)
# =====================================================================

def test_ensure_stop_placed_casts_numpy_float_to_native(db_path, monkeypatch):
    """정상: pandas 계산값(numpy.float64)을 current_stop으로 넘겨도 ccxt에
    전달되는 stopPrice는 반드시 native float여야 한다 - numpy.float64는
    ccxt.safe_string_2가 None으로 처리해 "requires a triggerPrice"로
    거부당하는 게 실측 확인된 원인(사고③, 2026-08-10 ETH 신규진입 직후)."""
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    exchange = _StopStubExchange(initial_stops=[])
    position = SimpleNamespace(direction="long", qty=0.002, current_stop=np.float64(60000.0))

    result = executor.ensure_stop_placed(exchange, db_path, "BTC/USDT:USDT", position)

    assert result.stop_confirmed is True
    assert type(exchange.last_params["stopPrice"]) is float  # numpy.float64가 아니라 native float
    assert exchange.last_params["stopPrice"] == 60000.0


# =====================================================================
# ensure_stop_placed — 2026-08-11 사고② 회귀 테스트(근본원인 B: 가격 레이스)
# =====================================================================

class _RaceConditionStubExchange:
    """STOP_MARKET 배치는 매번 OrderImmediatelyFillable(-2021)로 거부되고
    (가격이 이미 스탑을 넘었다고 가정), reduceOnly market 청산은 정상
    체결되는 상황을 흉내낸다(2026-08-11 사고②, 2026-07-27 실측 패턴)."""

    def __init__(self, current_price, market_id="ETHUSDT", position_symbol="ETH/USDT:USDT",
                 position_side="long"):
        self.current_price = current_price
        self._market_id = market_id
        self._position_symbol = position_symbol
        self._position_side = position_side
        self.create_order_calls: list[dict] = []

    def load_markets(self):
        pass

    def market(self, symbol):
        return {"id": self._market_id}

    def fapiPrivateGetOpenAlgoOrders(self):
        return []

    def fetch_ticker(self, symbol):
        return {"last": self.current_price}

    def fetch_positions(self):
        """★ 2026-08-23 사고 대응: -2021 폴백이 긴급청산 전 거래소 실측
        재검증을 하도록 바뀌어(_position_still_open) 이 스텁도 기본적으로
        "포지션이 아직 열려있음"을 반환해야 기존 정상 케이스가 안 깨진다 -
        "이미 사라짐" 케이스는 _AlreadyClosedStubExchange가 오버라이드."""
        return [{"symbol": self._position_symbol, "contracts": 1.5, "side": self._position_side}]

    def create_order(self, symbol, order_type, side, qty, price=None, params=None):
        self.create_order_calls.append({"order_type": order_type, "side": side, "qty": qty, "params": params})
        if order_type == "STOP_MARKET":
            raise ccxt.OrderImmediatelyFillable("binanceusdm Order would immediately trigger.")
        return {"id": "CLOSE1", "status": "closed", "filled": qty, "average": self.current_price}


def test_ensure_stop_placed_closes_immediately_when_stop_already_breached(db_path, monkeypatch):
    """경계: 거래소가 -2021로 거부하고 실제로 현재가가 스탑 조건을 이미
    충족했으면(레이스에서 짐), 같은 가격으로 계속 재시도하는 대신 즉시
    reduceOnly 시장가로 청산한다 - 대기하는 스탑은 어차피 계속 거부당할
    뿐이므로(2026-07-27 실측: 5회 그룹 반복해도 전부 동일 거부)."""
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    # 롱 포지션의 sell 스탑(1907) - 현재가(1900)가 이미 그 이하 아님... 손절 조건은
    # "가격이 스탑 이하로 떨어지면"이므로 현재가를 스탑 아래로 설정해 조건 충족을 재현.
    exchange = _RaceConditionStubExchange(current_price=1900.0)
    position = SimpleNamespace(direction="long", qty=1.5, current_stop=1907.0)

    result = executor.ensure_stop_placed(exchange, db_path, "ETH/USDT:USDT", position)

    assert result.stop_confirmed is False
    assert result.closed_instead is True
    assert result.close_result.status == "filled"
    assert [c["order_type"] for c in exchange.create_order_calls] == ["STOP_MARKET", "market"]
    assert exchange.create_order_calls[1]["side"] == "sell"  # 롱 청산이므로 매도
    assert exchange.create_order_calls[1]["params"] == {"reduceOnly": True}


def test_ensure_stop_placed_keeps_retrying_when_price_not_actually_breached(db_path, monkeypatch):
    """실패(대체청산 안 함): -2021을 받았어도 실제 현재가로 재확인했을 때
    조건이 아직 안 충족됐으면(예: 순간적 API 오류 등 다른 원인) 함부로
    청산하지 않고 기존 재시도 흐름을 그대로 유지한다 - 안전 쪽으로만
    행동을 확장했는지 확인."""
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    # 롱 포지션 sell 스탑(1907)인데 현재가(1950)는 스탑보다 훨씬 위 - 조건 미충족
    exchange = _RaceConditionStubExchange(current_price=1950.0)
    position = SimpleNamespace(direction="long", qty=1.5, current_stop=1907.0)

    result = executor.ensure_stop_placed(exchange, db_path, "ETH/USDT:USDT", position, max_retries=2)

    assert result.stop_confirmed is False
    assert result.closed_instead is False
    # market 청산 시도 없이 STOP_MARKET만 max_retries번 재시도했어야 함
    assert [c["order_type"] for c in exchange.create_order_calls] == ["STOP_MARKET", "STOP_MARKET"]


# =====================================================================
# ensure_stop_placed — 2026-08-23 사고 회귀 테스트(AAVE/ADA/BCH/UNI,
# -2021 폴백이 거래소 재검증 없이 이미 사라진 포지션에 reduceOnly 청산을
# 반복 시도하다 -2022로 계속 거부당해 약 24시간 CRITICAL을 반복 발신)
# =====================================================================

class _AlreadyClosedStubExchange(_RaceConditionStubExchange):
    """-2021은 재현하되(가격 재확인까지는 조건 충족), 거래소엔 이미
    포지션이 없는 상태(정상 손절 등으로 우리가 알기 전에 선청산됨)를
    흉내낸다. market(긴급청산) create_order가 실제로 호출되면 실패하도록
    막아, 재검증 없이 청산을 시도하는 회귀를 확실히 잡는다."""

    def fetch_positions(self):
        return []

    def create_order(self, symbol, order_type, side, qty, price=None, params=None):
        if order_type == "market":
            raise AssertionError("거래소 재검증 없이 긴급청산 주문을 시도함 - 회귀")
        return super().create_order(symbol, order_type, side, qty, price, params)


def test_ensure_stop_placed_skips_emergency_close_when_exchange_already_flat(db_path, monkeypatch):
    """정상: -2021 + 가격조건 충족이어도, 거래소 실측(fetch_positions)에
    포지션이 이미 없으면 긴급 reduceOnly 청산을 시도하지 않고 즉시
    already_closed_on_exchange=True로 반환한다 - DB 정리는 호출부
    (_handle_stop_result) 책임."""
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    exchange = _AlreadyClosedStubExchange(current_price=1900.0)
    position = SimpleNamespace(direction="long", qty=1.5, current_stop=1907.0)

    result = executor.ensure_stop_placed(exchange, db_path, "ETH/USDT:USDT", position)

    assert result.stop_confirmed is False
    assert result.closed_instead is False
    assert result.already_closed_on_exchange is True
    assert result.close_result is None
    assert [c["order_type"] for c in exchange.create_order_calls] == ["STOP_MARKET"]


def test_ensure_stop_placed_still_closes_when_exchange_position_reversed(db_path, monkeypatch):
    """경계: fetch_positions()에 심볼이 있어도 방향이 기대와 다르면(이미
    반전됨) 우리 포지션은 없는 것과 같으므로 역시 청산을 시도하지 않는다."""
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    exchange = _AlreadyClosedStubExchange(current_price=1900.0)
    exchange.fetch_positions = lambda: [
        {"symbol": "ETH/USDT:USDT", "contracts": 1.5, "side": "short"}
    ]
    position = SimpleNamespace(direction="long", qty=1.5, current_stop=1907.0)

    result = executor.ensure_stop_placed(exchange, db_path, "ETH/USDT:USDT", position)

    assert result.already_closed_on_exchange is True
    assert [c["order_type"] for c in exchange.create_order_calls] == ["STOP_MARKET"]


def test_ensure_stop_placed_still_closes_when_exchange_position_confirmed_open(db_path, monkeypatch):
    """실패(=계속 정상 동작 확인): 거래소에 실제로 같은 방향 포지션이
    남아있으면 재검증을 통과하고 기존 긴급청산 흐름을 그대로 수행한다 -
    새 재검증 로직이 정상 케이스를 막지 않는지 확인."""
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    exchange = _RaceConditionStubExchange(current_price=1900.0)
    exchange.fetch_positions = lambda: [
        {"symbol": "ETH/USDT:USDT", "contracts": 1.5, "side": "long"}
    ]
    position = SimpleNamespace(direction="long", qty=1.5, current_stop=1907.0)

    result = executor.ensure_stop_placed(exchange, db_path, "ETH/USDT:USDT", position)

    assert result.closed_instead is True
    assert result.already_closed_on_exchange is False
    assert result.close_result.status == "filled"


# =====================================================================
# cancel_stop_orders — 2026-08-11 발견(고아 스탑): 포지션 완전청산 후에도
# STOP_MARKET이 거래소에 남아있던 실측 사례(ETH, trigger=1910.35) 대응
# =====================================================================

class _AlgoOrdersStubExchange:
    def __init__(self, algo_orders, market_id="ETHUSDT"):
        self._algo_orders = list(algo_orders)
        self._market_id = market_id
        self.deleted_algo_ids: list = []

    def market(self, symbol):
        return {"id": self._market_id}

    def fapiPrivateGetOpenAlgoOrders(self):
        return list(self._algo_orders)

    def fapiPrivateDeleteAlgoOrder(self, params):
        self.deleted_algo_ids.append(params["algoId"])


def test_cancel_stop_orders_cancels_all_new_stops_for_symbol():
    """정상: 해당 심볼의 NEW 상태 알고 스탑을 전부 취소하고, 다른 심볼은
    건드리지 않는다."""
    exchange = _AlgoOrdersStubExchange(algo_orders=[
        {"symbol": "ETHUSDT", "algoId": 1, "algoStatus": "NEW"},
        {"symbol": "ETHUSDT", "algoId": 2, "algoStatus": "NEW"},
        {"symbol": "BTCUSDT", "algoId": 3, "algoStatus": "NEW"},
    ])

    cancelled = executor.cancel_stop_orders(exchange, "ETH/USDT:USDT")

    assert cancelled == 2
    assert set(exchange.deleted_algo_ids) == {1, 2}


def test_cancel_stop_orders_skips_non_new_status():
    """경계: 이미 트리거됐거나 취소된 상태는 다시 취소 시도하지 않는다."""
    exchange = _AlgoOrdersStubExchange(algo_orders=[
        {"symbol": "ETHUSDT", "algoId": 1, "algoStatus": "TRIGGERED"},
    ])

    cancelled = executor.cancel_stop_orders(exchange, "ETH/USDT:USDT")

    assert cancelled == 0
    assert exchange.deleted_algo_ids == []


def test_cancel_stop_orders_continues_after_individual_failure(capsys):
    """실패: 개별 취소가 실패해도 나머지는 계속 시도하고, 실패를 조용히
    삼키지 않는다(CLAUDE.md 원칙)."""
    class _FlakyExchange(_AlgoOrdersStubExchange):
        def fapiPrivateDeleteAlgoOrder(self, params):
            if params["algoId"] == 1:
                raise RuntimeError("boom")
            super().fapiPrivateDeleteAlgoOrder(params)

    exchange = _FlakyExchange(algo_orders=[
        {"symbol": "ETHUSDT", "algoId": 1, "algoStatus": "NEW"},
        {"symbol": "ETHUSDT", "algoId": 2, "algoStatus": "NEW"},
    ])

    cancelled = executor.cancel_stop_orders(exchange, "ETH/USDT:USDT")

    assert cancelled == 1
    assert exchange.deleted_algo_ids == [2]
    captured = capsys.readouterr()
    assert "취소 실패" in captured.err


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


def test_get_authenticated_exchange_disables_fetch_open_orders_symbol_warning(monkeypatch, tmp_path):
    """★ 실측으로 발견한 버그의 회귀 테스트(src/live/runner.py 실제 testnet
    통합 테스트 중 발견): recover_state()가 심볼 없이 fetch_open_orders()를
    호출하는데, 이 옵션이 없으면 ccxt가 ExchangeError로 막는다. 두 옵션
    다 꺼야 한다(ccxt/binance.py의 실제 조건문 기준 nested 값 하나로도
    충분하지만, 상위호환 플래그도 이중 안전장치로 같이 끈다)."""
    env_file = tmp_path / ".env"
    env_file.write_text("BINANCE_TESTNET_API_KEY=fake\nBINANCE_TESTNET_API_SECRET=fake\n", encoding="utf-8")
    monkeypatch.setattr(executor, "ENV_PATH", env_file)

    exchange = executor.get_authenticated_exchange(testnet=True)

    assert exchange.options["fetchOpenOrders"]["warnWithoutSymbol"] is False
    assert exchange.options["warnOnFetchOpenOrdersWithoutSymbol"] is False
