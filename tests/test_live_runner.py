"""tests/test_live_runner.py — src/live/runner.py 단위 테스트.

네트워크 없음 — run_live_loop() 자체를 스텁해 runner.py의 "조립" 로직만
검증한다(config 로드 -> db_path 해석 -> init_db -> G4 시각 기록 ->
run_live_loop 호출). run_live_loop 내부는 tests/test_live_scheduler.py가
이미 검증."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.live import runner
from src.live.state import init_db, load_g4_start_timestamp


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "state.db"
    init_db(path)
    return path


def test_load_config_reads_real_config_yaml():
    cfg = runner._load_config()
    assert "mode" in cfg
    assert "db_path" in cfg
    assert "portfolio" in cfg


def test_record_g4_start_first_call_records_timestamp(db_path):
    assert load_g4_start_timestamp(db_path) is None
    runner._record_g4_start_if_first_run(db_path)
    assert load_g4_start_timestamp(db_path) is not None


def test_record_g4_start_second_call_does_not_overwrite(db_path):
    runner._record_g4_start_if_first_run(db_path)
    first = load_g4_start_timestamp(db_path)

    runner._record_g4_start_if_first_run(db_path)
    second = load_g4_start_timestamp(db_path)

    assert first == second  # 재기록 안 됨 - 리뷰 기준점 고정


def test_main_wires_config_dbpath_init_and_run_live_loop(tmp_path, monkeypatch):
    """★ 조립 순서 확인: config 로드 -> resolve_db_path -> init_db ->
    G4 시각 기록 -> run_live_loop 호출까지 전부 실행되는지."""
    fake_db_path = tmp_path / "resolved_state.db"
    fake_cfg = {"mode": "testnet", "db_path": "irrelevant", "active_strategies": ["trend"]}

    monkeypatch.setattr(runner, "_load_config", lambda: fake_cfg)
    monkeypatch.setattr(runner.live_state, "resolve_db_path", lambda cfg: fake_db_path)

    calls = []
    monkeypatch.setattr(runner, "run_live_loop",
                         lambda cfg, db_path, max_iterations=None: calls.append((cfg, db_path, max_iterations)))

    result = runner.main(max_iterations=2)

    assert result == 0
    assert fake_db_path.exists()  # init_db가 실제로 파일을 만듦
    assert load_g4_start_timestamp(fake_db_path) is not None  # G4 시각 기록됨
    assert calls == [(fake_cfg, fake_db_path, 2)]


def test_main_defaults_to_infinite_loop_when_max_iterations_omitted(tmp_path, monkeypatch):
    fake_db_path = tmp_path / "state.db"
    monkeypatch.setattr(runner, "_load_config", lambda: {"mode": "testnet", "db_path": "x", "active_strategies": []})
    monkeypatch.setattr(runner.live_state, "resolve_db_path", lambda cfg: fake_db_path)

    calls = []
    monkeypatch.setattr(runner, "run_live_loop",
                         lambda cfg, db_path, max_iterations=None: calls.append(max_iterations))

    runner.main()

    assert calls == [None]
