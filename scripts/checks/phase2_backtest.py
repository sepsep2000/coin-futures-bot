"""
Phase 2 (백테스트 엔진) 전용 체크.
Phase 2 코드가 실제로 생기면 아래 TODO 부분만 채우면 됨 — 구조는 이미 완성.
"""
import subprocess
import sys


def check_reproducibility(backtest_cmd: list[str]):
    """같은 입력으로 두 번 돌려서 결과가 동일한지. 랜덤시드 미고정/미래데이터 누수 잡는 용도."""
    failures = []
    runs = []
    for _ in range(2):
        result = subprocess.run(backtest_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            failures.append(f"백테스트 실행 실패 (exit={result.returncode}): {result.stderr[-500:]}")
            return failures
        runs.append(result.stdout)
    if runs[0] != runs[1]:
        failures.append("백테스트 재현성 실패: 동일 입력에 대해 결과가 다름 (랜덤시드 미고정 또는 비결정적 로직 의심)")
    return failures


def check_no_future_leakage(signal_fn_module: str, ohlcv_columns_used: list[str], allowed_columns: list[str]):
    """
    시그널 함수가 참조하는 컬럼이 '현재 시점까지'만 포함하는지 정적 체크.
    TODO: Phase 2에서 signal_fn_module의 AST를 파싱해 실제 참조 컬럼 추출하도록 구현.
    지금은 호출부에서 명시적으로 넘긴 컬럼 목록만 검증하는 최소 버전.
    """
    failures = []
    disallowed = set(ohlcv_columns_used) - set(allowed_columns)
    if disallowed:
        failures.append(f"허용되지 않은(미래 시점 가능성 있는) 컬럼 참조: {disallowed}")
    return failures


def run_all(config: dict):
    """
    config 예:
    {
        "backtest_cmd": ["python", "src/backtest/engine.py", "--symbol", "BTC"],
        "signal_fn_module": "src.strategy.donchian",
        "ohlcv_columns_used": ["open", "high", "low", "close"],
        "allowed_columns": ["open", "high", "low", "close", "volume"],
    }
    """
    failures = []
    if "backtest_cmd" in config:
        failures += check_reproducibility(config["backtest_cmd"])
    if "signal_fn_module" in config:
        failures += check_no_future_leakage(
            config["signal_fn_module"],
            config.get("ohlcv_columns_used", []),
            config.get("allowed_columns", []),
        )
    return [f"[phase2] {x}" for x in failures]
