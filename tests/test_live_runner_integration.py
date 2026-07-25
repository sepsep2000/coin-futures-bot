"""tests/test_live_runner_integration.py — src/live/runner.py 실제
Binance USDM testnet 통합 테스트.

@pytest.mark.integration로 표시돼 기본 `pytest` 실행에서는 제외된다
(pytest.ini의 addopts). 실행하려면 `pytest -m integration`.

★ db_path/heartbeat 경로를 실제 프로덕션 경로(config.yaml의 db_path=
"data/state.db", src.live.scheduler.HEARTBEAT_PATH="logs/heartbeat.json")가
아니라 격리된 tmp_path로 돌린다 — G4 착수 시각(save_g4_start_timestamp)은
최초 1회만 기록되고 이후 절대 덮어쓰지 않도록 설계돼 있어(리뷰 기준점
고정 목적), 이 테스트를 실제 프로덕션 경로로 돌리면 "진짜 G4가
시작"되는 부작용이 생긴다. 그건 사용자가 실제로 페이퍼 트레이딩을
시작하기로 결정한 시점에 본인이 실행해야 할 일이지, 코드 검증용 자동
테스트가 대신 트리거할 일이 아니다 — 그래서 여기서는 db_path와
heartbeat 경로 둘 다 monkeypatch로 격리한 채, 나머지(거래소 연결·주문
가능 여부·config.yaml의 실제 모드/가중치)는 전부 실제로 검증한다."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.live import executor
from src.live import runner, scheduler
from src.live import state as live_state


def _close_all_test_positions(exec_exchange) -> None:
    positions = exec_exchange.fetch_positions()
    for p in positions:
        qty = float(p.get("contracts") or 0)
        if qty == 0:
            continue
        side = "sell" if p["side"] == "long" else "buy"
        exec_exchange.create_order(p["symbol"], "market", side, abs(qty), params={"reduceOnly": True})
    for o in exec_exchange.fapiPrivateGetOpenAlgoOrders():
        exec_exchange.fapiPrivateDeleteAlgoOrder({"algoId": o["algoId"]})


@pytest.mark.integration
def test_real_testnet_runner_end_to_end(tmp_path, monkeypatch):
    scratch_db_path = tmp_path / "state.db"
    scratch_heartbeat_path = tmp_path / "heartbeat.json"

    monkeypatch.setattr(runner.live_state, "resolve_db_path", lambda cfg: scratch_db_path)
    monkeypatch.setattr(scheduler, "HEARTBEAT_PATH", scratch_heartbeat_path)

    real_cfg = runner._load_config()  # 실제 config.yaml(모드=testnet, 가중치 등) 그대로 사용

    result = runner.main(max_iterations=1)
    assert result == 0

    try:
        # 1) DB 생성 확인
        assert scratch_db_path.exists()

        # 2) G4 시작 시각 최초 기록 확인
        g4_ts = live_state.load_g4_start_timestamp(scratch_db_path)
        assert g4_ts is not None

        # 재실행해도 덮어쓰지 않는지 확인(리뷰 기준점 고정)
        runner.main(max_iterations=1)
        assert live_state.load_g4_start_timestamp(scratch_db_path) == g4_ts

        # 3) 하트비트 기록 확인
        assert scratch_heartbeat_path.exists()

        # 4) healthcheck.py가 실제로 정상 통과 판정을 내리는지 확인
        #    (동일 db_path/heartbeat_path로 healthcheck 체크 함수 직접 호출)
        import scripts.healthcheck as hc

        hb_passed, hb_detail = hc.check_heartbeat_fresh(heartbeat_path=scratch_heartbeat_path)
        assert hb_passed is True, hb_detail

        state_passed, state_detail = hc.check_state_matches_exchange(real_cfg, scratch_db_path)
        assert state_passed is True, state_detail

        disk_passed, disk_detail = hc.check_disk_and_db_accessible(scratch_db_path)
        assert disk_passed is True, disk_detail
    finally:
        # 실행 중 실제로 신규진입이 나갔을 수 있으므로(신호 발생 여부는 통제 불가) 계정 정리
        exec_exchange = executor.get_authenticated_exchange(testnet=True)
        _close_all_test_positions(exec_exchange)

    remaining = [p for p in exec_exchange.fetch_positions() if float(p.get("contracts") or 0) != 0]
    assert remaining == []
    assert exec_exchange.fapiPrivateGetOpenAlgoOrders() == []
