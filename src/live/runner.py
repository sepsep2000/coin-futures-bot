"""src/live/runner.py — 프로덕션 엔트리포인트.

config.yaml 로드 -> db_path 해석(src.live.state.resolve_db_path, 유일한
진실 소스) -> DB 초기화 -> G4 시작 시각 기록(최초 1회만) ->
src.live.scheduler.run_live_loop() 호출까지의 실제 실행 흐름.

★ 이 파일이 생기기 전엔 `run_live_loop()`을 실제 운영 조건으로 호출하는
코드가 프로젝트에 전혀 없었다(테스트에서만 호출됨) — `scripts/run_cycle.sh`가
존재하지 않는 `src/live/runner.py`를 참조하고 있었던 것도 이 때문이다
(G4_PREFLIGHT_CONFIG_CHECK.md). 이 파일이 그 빈 자리를 채운다.

★ active_strategies 경고(G4_PREFLIGHT_CONFIG_CHECK.md 확인 1/3): config.yaml의
`active_strategies` 필드는 이 실행 경로(scheduler.py 이하)에서 전혀
읽히지 않는다 — 레거시 `src/backtest/engine.py::run_backtest()` 전용
설정이다. filtered_trend는 이 값과 무관하게 항상 실행된다. 코드 동작을
바꾸는 게 아니라 혼란 방지를 위해 시작 시 로그로 명시적으로 알린다.

사용:
    python src/live/runner.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.live import state as live_state  # noqa: E402
from src.live.scheduler import run_live_loop  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def _log_stderr(message: str) -> None:
    print(f"[RUNNER] {message}", file=sys.stderr)


def _load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def _record_g4_start_if_first_run(db_path: Path) -> None:
    """G4 착수 시각은 최초 1회만 기록한다 — 매 실행(재시작)마다 덮어쓰면
    30건 페이퍼 트레이딩 리뷰의 기준점이 재시작할 때마다 흔들린다. 이미
    기록돼 있으면 스킵하고 로그만 남긴다(태스크 명시 요구사항)."""
    existing = live_state.load_g4_start_timestamp(db_path)
    if existing is not None:
        _log_stderr(f"G4 시작 시각 이미 기록됨({existing}) - 재기록 안 함")
        return
    g4_start = datetime.now(timezone.utc).isoformat()
    live_state.save_g4_start_timestamp(db_path, g4_start)
    _log_stderr(f"G4 시작 시각 최초 기록: {g4_start}")


def main(max_iterations: Optional[int] = None) -> int:
    """★ max_iterations: scheduler.py::run_live_loop()과 동일한 이유로
    골격에 없던 파라미터를 추가(테스트/통합검증 전용, 실제 운영은
    None=무한루프로 호출)."""
    cfg = _load_config()
    db_path = live_state.resolve_db_path(cfg)
    live_state.init_db(db_path)

    _log_stderr(
        f"active_strategies={cfg.get('active_strategies')}는 레거시 run_backtest() 엔진 전용 설정이며 "
        "이 라이브 경로(scheduler.py)에서는 읽히지 않는다 - filtered_trend는 이 값과 무관하게 항상 "
        "실행된다(G4_PREFLIGHT_CONFIG_CHECK.md 확인 1/3 참조)."
    )

    _record_g4_start_if_first_run(db_path)

    _log_stderr(f"시작 - mode={cfg['mode']}, db_path={db_path}")
    run_live_loop(cfg, db_path, max_iterations=max_iterations)
    return 0


if __name__ == "__main__":
    sys.exit(main())
