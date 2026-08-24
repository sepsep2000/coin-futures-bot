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

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Optional

import yaml

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.live import state as live_state  # noqa: E402
from src.live.scheduler import run_live_loop  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
LOCK_PATH = PROJECT_ROOT / "logs" / "runner.lock"


def _log_stderr(message: str) -> None:
    print(f"[RUNNER] {message}", file=sys.stderr)


def acquire_singleton_lock(lock_path: Path) -> Optional[IO]:
    """★ 2026-08-23 사고 대응(다중 프로세스 동시실행 -> ETH 15분봉 parquet 캐시
    동시쓰기로 손상 -> 손상된 캐시로 매 틱 실패 -> 연속오류 킬스위치(SPEC 2.4)
    오발동 -> 2a 8개 포지션 강제청산, 실측 순영향 +$0.26로 금전피해는 없었으나
    재발 시 위험이 훨씬 클 수 있음).

    근본원인: scripts/run_cycle.sh의 PID파일+`kill -0` 체크는 그 스크립트를
    통해서만 작동한다 - runner.py를 다른 경로(수동 셸 실행, 자동화 도구의
    직접 백그라운드 실행 등)로 직접 띄우면 그 가드를 그냥 지나친다. 실제
    사고 원인이 바로 이 경로(재시작 검증 과정에서 runner.py를 run_cycle.sh
    없이 여러 번 직접 실행)였다.

    이 함수는 그 가드를 runner.py 자기 자신(어떤 경로로 실행되든 항상 거치는
    지점)으로 옮긴다. `fcntl.flock`(POSIX)/`msvcrt.locking`(Windows)은 OS가
    직접 관리하는 잠금이라 프로세스가 어떻게 죽든(정상종료·크래시·SIGKILL)
    자동으로 해제된다 - PID파일+`kill -0` 방식의 TOCTOU 레이스(파일은 있는데
    그 사이 프로세스가 죽었거나, 반대로 아직 파일에 안 쓰였는데 이미 떠 있는
    경우)가 구조적으로 없다.

    이미 다른 인스턴스가 잠금을 쥐고 있으면 None을 반환한다(예외를 던지지
    않음 - 호출부가 "중복 실행 거부"를 정상 흐름으로 명시 처리해야 하므로
    fire-and-forget 금지 원칙에 따라 조용히 넘어가지 않고 값으로 알린다).
    성공하면 파일핸들을 반환 - 호출부가 프로세스 생애주기 내내 참조를 들고
    있어야 한다(가비지컬렉션되며 fd가 닫히면 잠금도 함께 풀린다)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w+")
    try:
        if sys.platform == "win32":
            fh.write("x")
            fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Windows에서는 잠금 충돌 직후 close()도 PermissionError를 던지는 경우가
        # 실측 확인됨(msvcrt 내부 상태 - 잠금 실패 자체와는 무관) - 진짜 결과인
        # "잠금 획득 실패"는 아래 return None으로 이미 명확히 알리므로, close()
        # 실패는 부수적 정리 실패일 뿐 별도로 알릴 실익이 없어 여기서만 삼킨다.
        try:
            fh.close()
        except OSError:
            pass
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


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
    lock_handle = acquire_singleton_lock(LOCK_PATH)
    if lock_handle is None:
        _log_stderr(
            f"이미 다른 runner.py 인스턴스가 실행 중(잠금파일={LOCK_PATH}) - "
            "중복 실행 거부(2026-08-23 다중프로세스 사고 재발 방지). "
            "정상 상황(watchdog이 살아있는 프로세스를 재확인 중)일 수도 있고, "
            "잠금 해제가 안 되는 좀비 프로세스가 있을 수도 있음 - 의심되면 "
            f"'ps -ef | grep runner.py'로 실제 프로세스 상태를 직접 확인할 것."
        )
        return 0

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
