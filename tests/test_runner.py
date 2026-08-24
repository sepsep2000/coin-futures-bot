"""tests/test_runner.py — src/live/runner.py 테스트.

★ 2026-08-23 사고 회귀 테스트: runner.py가 run_cycle.sh 없이 직접
실행돼 PID파일 가드를 우회한 것이 다중 프로세스 동시실행(→ parquet
캐시 손상 → 킬스위치 오발동)의 근본원인이었다. acquire_singleton_lock()이
그 가드를 runner.py 자기 자신으로 옮겼는지 실제 OS 잠금(fcntl/msvcrt)
동작으로 검증한다 - 목(mock) 없이 진짜 파일 잠금을 건다(잠금은 프로세스별이
아니라 open()된 파일핸들별이라 같은 프로세스 안에서도 두 핸들이 충돌하는지
그대로 확인 가능)."""

from __future__ import annotations

from pathlib import Path

from src.live import runner


def test_acquire_singleton_lock_succeeds_when_unlocked(tmp_path):
    """정상: 아무도 잠그지 않은 상태면 잠금을 획득하고 자기 PID를 파일에 쓴다."""
    lock_path = tmp_path / "runner.lock"

    handle = runner.acquire_singleton_lock(lock_path)

    assert handle is not None
    handle.seek(0)
    assert handle.read().strip().isdigit()
    handle.close()


def test_acquire_singleton_lock_rejects_second_instance_while_first_holds_it(tmp_path):
    """★ 요구된 실패 케이스(핵심 회귀): 첫 인스턴스가 잠금을 쥐고 있는 동안
    두 번째 시도는 반드시 거부(None)돼야 한다 - 이게 실패하면 2026-08-23
    사고(다중 프로세스 동시실행)가 그대로 재현된다."""
    lock_path = tmp_path / "runner.lock"
    first = runner.acquire_singleton_lock(lock_path)
    assert first is not None

    second = runner.acquire_singleton_lock(lock_path)

    assert second is None
    first.close()


def test_acquire_singleton_lock_succeeds_again_after_first_releases(tmp_path):
    """경계: 첫 인스턴스가 정상 종료(핸들 close)하면, 뒤이은 정상 재시작
    시도는 다시 잠금을 획득할 수 있어야 한다 - 잠금이 영구히 고아로 남지
    않는지 확인(watchdog의 정상 재시작 시나리오)."""
    lock_path = tmp_path / "runner.lock"
    first = runner.acquire_singleton_lock(lock_path)
    assert first is not None
    first.close()

    second = runner.acquire_singleton_lock(lock_path)

    assert second is not None
    second.close()


def test_main_returns_early_without_running_loop_when_lock_held(tmp_path, monkeypatch):
    """정상(통합): main()이 잠금 실패 시 run_live_loop를 절대 호출하지 않고
    조용히(예외 없이) 0을 반환하는지 확인 - 이중 실행이 실제로 봇 로직까지
    도달하지 못하게 막는지가 이 사고 대응의 핵심이므로 main() 레벨에서도
    검증한다."""
    lock_path = tmp_path / "runner.lock"
    holder = runner.acquire_singleton_lock(lock_path)
    assert holder is not None

    monkeypatch.setattr(runner, "LOCK_PATH", lock_path)
    called = []
    monkeypatch.setattr(runner, "run_live_loop", lambda *a, **k: called.append(True))

    result = runner.main()

    assert result == 0
    assert called == []
    holder.close()
