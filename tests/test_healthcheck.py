"""tests/test_healthcheck.py — scripts/healthcheck.py 단위 테스트.

각 체크 함수를 개별적으로 스텁 거래소/텔레그램으로 검증(네트워크 없음).
heartbeat 신선도는 `now` 파라미터로 시간을 직접 주입해 테스트한다(실제
대기 없음)."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.healthcheck as hc
from src.live.state import init_db, save_position

CFG = {"mode": "testnet"}


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "state.db"
    init_db(path)
    return path


# =====================================================================
# check_telegram_reachable
# =====================================================================

def test_check_telegram_reachable_success(monkeypatch):
    monkeypatch.setattr(hc.telegram, "send_message", lambda text: True)
    passed, detail = hc.check_telegram_reachable()
    assert passed is True


def test_check_telegram_reachable_failure(monkeypatch):
    monkeypatch.setattr(hc.telegram, "send_message", lambda text: False)
    passed, detail = hc.check_telegram_reachable()
    assert passed is False
    assert "발신 실패" in detail


# =====================================================================
# check_heartbeat_fresh
# =====================================================================

def test_check_heartbeat_fresh_recent_passes(tmp_path):
    path = tmp_path / "heartbeat.json"
    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    last_tick = now - timedelta(minutes=10)
    path.write_text(json.dumps({"last_tick_iso": last_tick.isoformat()}), encoding="utf-8")

    passed, detail = hc.check_heartbeat_fresh(heartbeat_path=path, now=now)
    assert passed is True


def test_check_heartbeat_fresh_stale_fails(tmp_path):
    """★ 임계치(45분 = 15m x 3) 초과 시 실패해야 한다."""
    path = tmp_path / "heartbeat.json"
    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    last_tick = now - timedelta(minutes=50)
    path.write_text(json.dumps({"last_tick_iso": last_tick.isoformat()}), encoding="utf-8")

    passed, detail = hc.check_heartbeat_fresh(heartbeat_path=path, now=now)
    assert passed is False
    assert "45" in detail or "0:45:00" in detail


def test_check_heartbeat_fresh_exactly_at_threshold_boundary(tmp_path):
    """경계 케이스: 정확히 임계치(45분)면 아직 초과가 아니므로 통과."""
    path = tmp_path / "heartbeat.json"
    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    last_tick = now - timedelta(minutes=45)
    path.write_text(json.dumps({"last_tick_iso": last_tick.isoformat()}), encoding="utf-8")

    passed, detail = hc.check_heartbeat_fresh(heartbeat_path=path, now=now)
    assert passed is True


def test_check_heartbeat_fresh_missing_file_fails(tmp_path):
    path = tmp_path / "does_not_exist.json"
    passed, detail = hc.check_heartbeat_fresh(heartbeat_path=path)
    assert passed is False
    assert "없음" in detail


def test_check_heartbeat_fresh_corrupt_file_fails(tmp_path):
    path = tmp_path / "heartbeat.json"
    path.write_text("not valid json", encoding="utf-8")
    passed, detail = hc.check_heartbeat_fresh(heartbeat_path=path)
    assert passed is False


# =====================================================================
# check_exchange_reachable
# =====================================================================

class _StubOkExchange:
    def fetch_ticker(self, symbol):
        return {"last": 3000.0}

    def fetch_balance(self):
        return {"total": {"USDT": 10000.0}}


class _StubFailingExchange:
    def fetch_ticker(self, symbol):
        raise ConnectionError("network down")

    def fetch_balance(self):
        raise ConnectionError("network down")


def test_check_exchange_reachable_both_ok(monkeypatch):
    monkeypatch.setattr(hc, "get_exchange", lambda testnet=False: _StubOkExchange())
    monkeypatch.setattr(hc.executor, "get_authenticated_exchange", lambda testnet=True: _StubOkExchange())
    passed, detail = hc.check_exchange_reachable(CFG)
    assert passed is True


def test_check_exchange_reachable_data_exchange_fails(monkeypatch):
    monkeypatch.setattr(hc, "get_exchange", lambda testnet=False: _StubFailingExchange())
    monkeypatch.setattr(hc.executor, "get_authenticated_exchange", lambda testnet=True: _StubOkExchange())
    passed, detail = hc.check_exchange_reachable(CFG)
    assert passed is False
    assert "데이터 거래소" in detail


def test_check_exchange_reachable_exec_exchange_fails(monkeypatch):
    monkeypatch.setattr(hc, "get_exchange", lambda testnet=False: _StubOkExchange())
    monkeypatch.setattr(hc.executor, "get_authenticated_exchange", lambda testnet=True: _StubFailingExchange())
    passed, detail = hc.check_exchange_reachable(CFG)
    assert passed is False
    assert "실행 거래소" in detail


# =====================================================================
# check_state_matches_exchange (recover_state 재사용 확인)
# =====================================================================

class _StubReconcileExchange:
    def __init__(self, positions=None, orders=None):
        self._positions = positions or []
        self._orders = orders or []

    def fetch_positions(self):
        return self._positions

    def fetch_open_orders(self):
        return self._orders


def test_check_state_matches_exchange_agree(db_path, monkeypatch):
    save_position(db_path, "ETH/USDT:USDT", "filtered_trend", "long", 1.0, 3000.0, "2026-07-25T00:00:00Z", 2900.0)
    exchange = _StubReconcileExchange(positions=[{"symbol": "ETH/USDT:USDT", "contracts": 1.0}])
    monkeypatch.setattr(hc.executor, "get_authenticated_exchange", lambda testnet=True: exchange)

    passed, detail = hc.check_state_matches_exchange(CFG, db_path)
    assert passed is True


def test_check_state_matches_exchange_mismatch_fails(db_path, monkeypatch):
    save_position(db_path, "ETH/USDT:USDT", "filtered_trend", "long", 1.0, 3000.0, "2026-07-25T00:00:00Z", 2900.0)
    exchange = _StubReconcileExchange(positions=[])  # DB엔 있는데 거래소엔 없음
    monkeypatch.setattr(hc.executor, "get_authenticated_exchange", lambda testnet=True: exchange)

    passed, detail = hc.check_state_matches_exchange(CFG, db_path)
    assert passed is False
    assert "불일치" in detail


def test_check_state_matches_exchange_missing_db_file_fails(tmp_path, monkeypatch):
    missing_db = tmp_path / "never_created.db"
    monkeypatch.setattr(hc.executor, "get_authenticated_exchange", lambda testnet=True: _StubReconcileExchange())
    passed, detail = hc.check_state_matches_exchange(CFG, missing_db)
    assert passed is False
    assert "없음" in detail


# =====================================================================
# check_disk_and_db_accessible
# =====================================================================

def test_check_disk_and_db_accessible_sufficient_space(db_path, monkeypatch):
    monkeypatch.setattr(hc.shutil, "disk_usage", lambda path: SimpleNamespace(total=0, used=0, free=10 * 1024**3))
    passed, detail = hc.check_disk_and_db_accessible(db_path)
    assert passed is True


def test_check_disk_and_db_accessible_low_space_fails(db_path, monkeypatch):
    monkeypatch.setattr(hc.shutil, "disk_usage", lambda path: SimpleNamespace(total=0, used=0, free=100 * 1024**2))  # 100MB < 500MB 임계치
    passed, detail = hc.check_disk_and_db_accessible(db_path)
    assert passed is False
    assert "여유 디스크" in detail


def test_check_disk_and_db_accessible_db_write_failure(db_path, monkeypatch):
    monkeypatch.setattr(hc.shutil, "disk_usage", lambda path: SimpleNamespace(total=0, used=0, free=10 * 1024**3))
    monkeypatch.setattr(hc.live_state, "init_db", lambda p: (_ for _ in ()).throw(PermissionError("no write access")))
    passed, detail = hc.check_disk_and_db_accessible(db_path)
    assert passed is False
    assert "접근/쓰기 실패" in detail


# =====================================================================
# run_all_checks / main — 집계 로직
# =====================================================================

def test_run_all_checks_aggregates_all_five(db_path, monkeypatch):
    monkeypatch.setattr(hc, "CHECKS", [
        ("a", lambda cfg, db: (True, "ok")),
        ("b", lambda cfg, db: (False, "bad")),
    ])
    results = hc.run_all_checks(CFG, db_path)
    assert results == {"a": {"passed": True, "detail": "ok"}, "b": {"passed": False, "detail": "bad"}}


def test_run_all_checks_check_raising_exception_counts_as_failure(db_path, monkeypatch):
    def _boom(cfg, db):
        raise RuntimeError("check itself is buggy")

    monkeypatch.setattr(hc, "CHECKS", [("buggy_check", _boom)])
    results = hc.run_all_checks(CFG, db_path)
    assert results["buggy_check"]["passed"] is False
    assert "예외" in results["buggy_check"]["detail"]


def test_main_exit_code_zero_when_all_pass(db_path, monkeypatch):
    monkeypatch.setattr(hc, "_load_config", lambda: CFG)
    monkeypatch.setattr(hc, "_resolve_db_path", lambda cfg: db_path)
    monkeypatch.setattr(hc, "run_all_checks", lambda cfg, db: {"a": {"passed": True, "detail": "ok"}})

    assert hc.main() == 0


def test_main_exit_code_one_and_sends_critical_alert_when_any_fails(db_path, monkeypatch):
    monkeypatch.setattr(hc, "_load_config", lambda: CFG)
    monkeypatch.setattr(hc, "_resolve_db_path", lambda cfg: db_path)
    monkeypatch.setattr(hc, "run_all_checks", lambda cfg, db: {"a": {"passed": False, "detail": "broken"}})

    sent = []
    monkeypatch.setattr(hc.telegram, "send_critical_alert", lambda msg: sent.append(msg) or True)

    assert hc.main() == 1
    assert len(sent) == 1
    assert "broken" in sent[0]


def test_main_does_not_crash_when_critical_alert_itself_fails(db_path, monkeypatch):
    """★ telegram_reachable 자체가 실패 원인이면 실패 보고 시도도 실패할
    수 있다 - 그래도 healthcheck.py 자체는 크래시하지 않고 exit 1로
    끝나야 한다(docstring에 명시된 한계, 조용히 죽으면 안 됨)."""
    monkeypatch.setattr(hc, "_load_config", lambda: CFG)
    monkeypatch.setattr(hc, "_resolve_db_path", lambda cfg: db_path)
    monkeypatch.setattr(hc, "run_all_checks", lambda cfg, db: {"telegram_reachable": {"passed": False, "detail": "발신 실패"}})
    monkeypatch.setattr(hc.telegram, "send_critical_alert",
                         lambda msg: (_ for _ in ()).throw(ConnectionError("also down")))

    assert hc.main() == 1
