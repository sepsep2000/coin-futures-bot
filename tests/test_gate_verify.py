import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import gate_verify  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent


# --- 정상 케이스: 매칭된 phase가 있고 전부 통과 (기존 동작 회귀 확인) ---

def test_evaluate_matched_phase_with_no_failures_still_passes():
    """phase2 config가 비어있으면(backtest_cmd/signal_fn_module 둘 다 없음)
    phase2_backtest.run_all()이 아무 서브체크도 안 돌려 빈 실패 목록을
    반환한다 — 매칭된 phase가 있는 정상 PASS 경로가 이번 수정으로 안
    깨졌는지 확인."""
    result = gate_verify.evaluate({"phase2": {}}, config_existed=True)
    assert result["status"] == gate_verify.STATUS_PASS
    assert result["ran_phases"] == ["phase2"]
    assert result["failures"] == []


# --- 경계 케이스: harness_config.json 파일 자체가 없음 ---

def test_evaluate_no_config_file_returns_error_not_pass():
    """config_existed=False(파일 자체가 없음)면 0건 검증을 PASS로
    취급하지 않고 ERROR를 반환해야 한다 — 이번 수정의 핵심 버그."""
    result = gate_verify.evaluate({}, config_existed=False)
    assert result["status"] == gate_verify.STATUS_ERROR
    assert result["ran_phases"] == []
    assert len(result["failures"]) == 1
    assert "파일이 없음" in result["failures"][0]


# --- 실패 케이스 1: 파일은 있지만 phase 섹션이 하나도 없음 ---

def test_evaluate_config_exists_but_no_phase_sections_returns_error():
    """파일 없음과는 다른 원인(설정 키 오탈자 등)이므로 메시지가 달라야
    한다 — "파일이 없음" 케이스와 구분되는지 확인."""
    result = gate_verify.evaluate({"unrelated_key": 1}, config_existed=True)
    assert result["status"] == gate_verify.STATUS_ERROR
    assert result["ran_phases"] == []
    assert "섹션이 하나도 없음" in result["failures"][0]
    assert "파일이 없음" not in result["failures"][0]


# --- 실패 케이스 2: 매칭된 phase가 있고 그 중 실패가 있음 ---

def test_evaluate_matched_phase_with_failure_returns_fail():
    result = gate_verify.evaluate(
        {"phase3": {"results_path": "no_such_file_ever.json"}}, config_existed=True
    )
    assert result["status"] == gate_verify.STATUS_FAIL
    assert result["ran_phases"] == ["phase3"]
    assert len(result["failures"]) >= 1


# --- 엔드투엔드 회귀: 실제 프로세스 실행 결과의 exit code까지 확인 ---

def test_main_exits_nonzero_when_config_missing(tmp_path):
    """이 프로젝트의 실제 harness_config.json 부재 상황을 그대로 재현 —
    exit code가 0(과거 버그)이 아니라 1이어야 한다."""
    missing_config = tmp_path / "does_not_exist.json"
    state_path = tmp_path / "state.json"
    env = {**os.environ, "HARNESS_CONFIG_PATH": str(missing_config), "HARNESS_STATE_PATH": str(state_path)}
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "gate_verify.py")],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert '"status": "ERROR"' in result.stdout


def test_main_exits_zero_when_matched_phase_passes(tmp_path):
    config_path = tmp_path / "hc.json"
    config_path.write_text('{"phase2": {}}', encoding="utf-8")
    state_path = tmp_path / "state.json"
    env = {**os.environ, "HARNESS_CONFIG_PATH": str(config_path), "HARNESS_STATE_PATH": str(state_path)}
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "gate_verify.py")],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert '"status": "PASS"' in result.stdout
