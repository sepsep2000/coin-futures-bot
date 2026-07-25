"""tests/test_live_executor_integration.py — src/live/executor.py 실제
Binance USDM testnet 통합 테스트.

전부 @pytest.mark.integration로 표시돼 기본 `pytest` 실행에서는 제외된다
(pytest.ini의 addopts). 실행하려면 `pytest -m integration`.

unit 테스트(tests/test_live_executor.py, 스텁 거래소, 네트워크 없음)와
커밋을 분리하기 위해 별도 파일로 둔다."""

from __future__ import annotations

import os

import pytest

from src.live import executor
from src.live.state import init_db


@pytest.mark.integration
def test_real_testnet_place_poll_cancel_and_cleanup(tmp_path, capsys):
    """실제 Binance USDM testnet에 주문을 낸다. 항상 testnet=True 고정
    (라이브 계좌 절대 사용 안 함). 시장가 주문으로 체결을 확인하고 즉시
    reduceOnly로 청산, 별도로 절대 체결 안 될 지정가 주문을 걸어 취소
    경로를 확인한다. 계정에 잔여 상태를 남기지 않는다."""
    db_path = tmp_path / "state.db"
    init_db(db_path)
    exchange = executor.get_authenticated_exchange(testnet=True)
    symbol = "BTC/USDT:USDT"
    qty = 0.002

    # 1) 시장가 매수 -> 체결 확인
    open_result = executor.place_order(exchange, db_path, symbol, "long", qty)
    assert open_result.status == "filled"
    poll_result = executor.poll_order_status(exchange, db_path, open_result.order_id, symbol)
    assert poll_result.status == "filled"
    assert poll_result.avg_fill_price is not None and poll_result.avg_fill_price > 0

    # 즉시 청산(잔여 포지션 안 남김)
    close_result = executor.place_order(exchange, db_path, symbol, "short", qty)
    assert close_result.status == "filled"

    # 2) 절대 체결 안 될 지정가 매수(현재가의 절반 이하) -> 취소 경로 확인
    ticker = exchange.fetch_ticker(symbol)
    far_price = round(ticker["last"] * 0.5, 1)
    limit_result = executor.place_order(exchange, db_path, symbol, "long", qty, order_type="limit", limit_price=far_price)
    assert limit_result.status in ("pending", "partial")
    cancelled = executor.cancel_order(exchange, db_path, limit_result.order_id, symbol)
    assert cancelled is True

    # 계정에 잔여 포지션/미체결주문이 없는지 최종 확인
    positions = exchange.fetch_positions()
    open_qty = sum(abs(float(p.get("contracts") or 0)) for p in positions)
    assert open_qty == 0
    assert exchange.fetch_open_orders(symbol) == []

    # .env 비밀값이 출력 어디에도 노출되지 않았는지 확인
    api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET")
    captured = capsys.readouterr()
    assert api_secret not in captured.out
    assert api_secret not in captured.err
