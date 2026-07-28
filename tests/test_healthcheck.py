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
    monkeypatch.setattr(hc.telegram, "check_reachable", lambda: True)
    passed, detail = hc.check_telegram_reachable()
    assert passed is True


def test_check_telegram_reachable_failure(monkeypatch):
    monkeypatch.setattr(hc.telegram, "check_reachable", lambda: False)
    passed, detail = hc.check_telegram_reachable()
    assert passed is False
    assert "발신 실패" in detail


def test_check_telegram_reachable_does_not_send_chat_message(monkeypatch):
    """★ 2026-07-29 회귀 테스트: 이 체크가 실제 채팅 메시지(send_message)를
    호출하면 안 된다 - 15분마다 무의미한 확인 메시지가 쌓이던 문제(사용자
    피드백)의 재발 방지."""
    calls: list = []
    monkeypatch.setattr(hc.telegram, "send_message", lambda text, chat_id=None: calls.append(text) or True)
    monkeypatch.setattr(hc.telegram, "check_reachable", lambda: True)

    hc.check_telegram_reachable()

    assert calls == []


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


def test_main_exit_code_zero_when_all_pass(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "_load_config", lambda: CFG)
    monkeypatch.setattr(hc, "_resolve_db_path", lambda cfg: db_path)
    monkeypatch.setattr(hc, "run_all_checks", lambda cfg, db: {"a": {"passed": True, "detail": "ok"}})
    monkeypatch.setattr(hc, "ALERT_STATE_PATH", tmp_path / "alert_state.json")

    assert hc.main() == 0


def test_main_exit_code_one_and_sends_critical_alert_when_any_fails(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "_load_config", lambda: CFG)
    monkeypatch.setattr(hc, "_resolve_db_path", lambda cfg: db_path)
    monkeypatch.setattr(hc, "run_all_checks", lambda cfg, db: {"a": {"passed": False, "detail": "broken"}})
    monkeypatch.setattr(hc, "ALERT_STATE_PATH", tmp_path / "alert_state.json")

    sent = []
    monkeypatch.setattr(hc.telegram, "send_critical_alert", lambda msg: sent.append(msg) or True)

    assert hc.main() == 1
    assert len(sent) == 1
    assert "broken" in sent[0]


def test_main_does_not_crash_when_critical_alert_itself_fails(db_path, tmp_path, monkeypatch):
    """★ telegram_reachable 자체가 실패 원인이면 실패 보고 시도도 실패할
    수 있다 - 그래도 healthcheck.py 자체는 크래시하지 않고 exit 1로
    끝나야 한다(docstring에 명시된 한계, 조용히 죽으면 안 됨)."""
    monkeypatch.setattr(hc, "_load_config", lambda: CFG)
    monkeypatch.setattr(hc, "_resolve_db_path", lambda cfg: db_path)
    monkeypatch.setattr(hc, "run_all_checks", lambda cfg, db: {"telegram_reachable": {"passed": False, "detail": "발신 실패"}})
    monkeypatch.setattr(hc, "ALERT_STATE_PATH", tmp_path / "alert_state.json")
    monkeypatch.setattr(hc.telegram, "send_critical_alert",
                         lambda msg: (_ for _ in ()).throw(ConnectionError("also down")))

    assert hc.main() == 1


# =====================================================================
# 알림 피로 방지 — 같은 실패 반복 시 dedup (2026-07-29, 사용자 피드백:
# "텔레그램 알림 너무 자주 옴, 쓸데없는거 안오게 해")
# =====================================================================

def test_should_send_failure_alert_true_when_no_previous_state():
    """정상: 이전 상태가 없으면(첫 실패) 무조건 즉시 알린다."""
    assert hc._should_send_failure_alert(None, "a", datetime.now(timezone.utc)) is True


def test_should_send_failure_alert_false_within_reminder_interval():
    """경계: 같은 실패가 REMINDER_INTERVAL 이내에 반복되면 재알림 안 함."""
    now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
    prev = {"signature": "a,b", "last_alert_ts": (now - timedelta(minutes=30)).isoformat()}
    assert hc._should_send_failure_alert(prev, "a,b", now) is False


def test_should_send_failure_alert_true_after_reminder_interval_elapses():
    """실패(지속 상황): REMINDER_INTERVAL을 넘기면 그때는 다시 알린다."""
    now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
    prev = {"signature": "a,b", "last_alert_ts": (now - hc.ALERT_REMINDER_INTERVAL - timedelta(minutes=1)).isoformat()}
    assert hc._should_send_failure_alert(prev, "a,b", now) is True


def test_main_dedupes_repeated_identical_failure_within_reminder_interval(db_path, tmp_path, monkeypatch):
    """★ 회귀 테스트: 파케이 손상 사고 때 7시간 동안 58건 발신됐던 패턴
    재현 방지 - 같은 실패(heartbeat_fresh 등)가 15분마다 반복돼도 두 번째
    호출부터는 재알림하지 않는다."""
    monkeypatch.setattr(hc, "_load_config", lambda: CFG)
    monkeypatch.setattr(hc, "_resolve_db_path", lambda cfg: db_path)
    monkeypatch.setattr(hc, "run_all_checks", lambda cfg, db: {"heartbeat_fresh": {"passed": False, "detail": "마지막 틱 1:02:03 전"}})
    monkeypatch.setattr(hc, "ALERT_STATE_PATH", tmp_path / "alert_state.json")
    sent = []
    monkeypatch.setattr(hc.telegram, "send_critical_alert", lambda msg: sent.append(msg) or True)

    assert hc.main() == 1
    # detail 텍스트(경과시간)가 바뀌어도 dedup 키(실패 항목 집합)는 동일해야 함
    monkeypatch.setattr(hc, "run_all_checks", lambda cfg, db: {"heartbeat_fresh": {"passed": False, "detail": "마지막 틱 1:17:03 전"}})
    assert hc.main() == 1

    assert len(sent) == 1


def test_main_resends_after_reminder_interval_elapses(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "_load_config", lambda: CFG)
    monkeypatch.setattr(hc, "_resolve_db_path", lambda cfg: db_path)
    monkeypatch.setattr(hc, "run_all_checks", lambda cfg, db: {"a": {"passed": False, "detail": "broken"}})
    state_path = tmp_path / "alert_state.json"
    monkeypatch.setattr(hc, "ALERT_STATE_PATH", state_path)
    sent = []
    monkeypatch.setattr(hc.telegram, "send_critical_alert", lambda msg: sent.append(msg) or True)

    assert hc.main() == 1
    assert len(sent) == 1

    # 마지막 알림 시각을 REMINDER_INTERVAL 이전으로 되돌려 장시간 지속된 실패를 재현
    state = json.loads(state_path.read_text(encoding="utf-8"))
    old_ts = datetime.now(timezone.utc) - hc.ALERT_REMINDER_INTERVAL - timedelta(minutes=1)
    state["last_alert_ts"] = old_ts.isoformat()
    state_path.write_text(json.dumps(state), encoding="utf-8")

    assert hc.main() == 1
    assert len(sent) == 2  # 장시간 지속 -> 리마인더로 재발신


def test_main_alerts_immediately_when_failure_signature_changes(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "_load_config", lambda: CFG)
    monkeypatch.setattr(hc, "_resolve_db_path", lambda cfg: db_path)
    monkeypatch.setattr(hc, "ALERT_STATE_PATH", tmp_path / "alert_state.json")
    sent = []
    monkeypatch.setattr(hc.telegram, "send_critical_alert", lambda msg: sent.append(msg) or True)

    monkeypatch.setattr(hc, "run_all_checks", lambda cfg, db: {"heartbeat_fresh": {"passed": False, "detail": "x"}})
    assert hc.main() == 1

    monkeypatch.setattr(hc, "run_all_checks", lambda cfg, db: {"exchange_reachable": {"passed": False, "detail": "y"}})
    assert hc.main() == 1

    assert len(sent) == 2  # 다른 종류의 실패는 즉시 재알림(억제 대상 아님)


def test_main_clears_alert_state_on_recovery_then_realerts_immediately(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "_load_config", lambda: CFG)
    monkeypatch.setattr(hc, "_resolve_db_path", lambda cfg: db_path)
    state_path = tmp_path / "alert_state.json"
    monkeypatch.setattr(hc, "ALERT_STATE_PATH", state_path)
    sent = []
    monkeypatch.setattr(hc.telegram, "send_critical_alert", lambda msg: sent.append(msg) or True)

    monkeypatch.setattr(hc, "run_all_checks", lambda cfg, db: {"a": {"passed": False, "detail": "broken"}})
    hc.main()
    assert state_path.exists()

    monkeypatch.setattr(hc, "run_all_checks", lambda cfg, db: {"a": {"passed": True, "detail": "ok"}})
    hc.main()
    assert not state_path.exists()  # 회복 시 상태 초기화

    monkeypatch.setattr(hc, "run_all_checks", lambda cfg, db: {"a": {"passed": False, "detail": "broken again"}})
    hc.main()

    assert len(sent) == 2  # 회복 이후 재발생한 동일 종류 실패는 새 실패로 취급돼 즉시 알림
