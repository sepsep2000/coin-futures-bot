"""scripts/healthcheck.py — 프로세스 생존 확인.

★★★ 반드시 `src/live/scheduler.py`의 메인 루프와 별도 프로세스로 실행한다
(cron 등) — 같은 프로세스 내부 함수로 두면 메인 루프가 멈췄을 때 그
사실 자체를 감지할 수 없어 무의미하다(태스크 명시 금지사항).

체크 5항목(이번 태스크 STEP 2의 명시 지시를 그대로 따름):
1. telegram_reachable — 텔레그램 발신 가능 여부. **가장 먼저 체크**한다
   (다른 CRITICAL 알림 채널 자체가 죽어있으면 이후 실패를 알릴 방법이
   없으므로). 단, 이 체크 자체가 실패하면 그 사실을 텔레그램으로 알릴
   수 없다는 근본적 한계는 해소되지 않는다 — stdout/exit code/로그로만
   확인 가능(운영자가 cron 실패 자체를 별도로 감시해야 함, 이 스크립트가
   대신할 수 없는 부분).
2. heartbeat_fresh — scheduler.py가 매 틱마다 기록하는 마지막 실행 시각이
   너무 오래되지 않았는지(메인 루프 행/정지 감지).
3. exchange_reachable — 거래소 API 연결 상태(데이터/실행 엔드포인트 둘 다).
4. state_matches_exchange — state.py 기록과 거래소 실제 상태 정합성
   (src.live.state.recover_state() 그대로 재사용, 신규 로직 없음).
5. disk_and_db_accessible — 디스크 여유공간 + DB 파일 접근 가능 여부.

★ 2026-07-25 사실관계 확인: 이 파일 최초 골격의 docstring은 "LIVE_
EXECUTION_ARCHITECTURE.md에 healthcheck 5개 체크 항목이 이미 정의돼
있다"고 전제했으나, 실제로 그 문서를 재확인한 결과 그런 목록은 존재하지
않는다(SPEC.md 6절에 "config 로드/API연결/텔레그램발신" 3개만 한 줄로
언급돼 있을 뿐). 위 5항목은 이번 태스크 STEP2가 새로 명시한 목록을
그대로 따른 것이며, 문서에서 그대로 가져온 게 아님 — 추정으로 넘기지
않고 사실 그대로 기록한다.

exit code 0 = 전부 통과, 1 = 하나라도 실패(scripts/gate_verify.py 관례와 동일).

사용:
    python scripts/healthcheck.py
    HEALTHCHECK_DB_PATH=data/live_state.db python scripts/healthcheck.py  # DB 경로 재정의
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data.feed import get_exchange  # noqa: E402
from src.live import executor  # noqa: E402
from src.live import state as live_state  # noqa: E402
from src.live.scheduler import HEARTBEAT_PATH  # noqa: E402 - 단일 소스, 여기서 재정의 안 함
from src.notify import telegram  # noqa: E402
from strategies.filtered_trend import SYMBOL as FT_SYMBOL  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

TICK_INTERVAL_MINUTES = 15
HEARTBEAT_STALE_MULTIPLE = 3  # 45분(15m x 3) - 근거: executor.py 재시도 백오프(최대 2+4+8=14초/주문) +
# 2a 리밸런스처럼 무거운 틱 1회를 넉넉히 흡수하고도, 조용히 멈춘 프로세스를 1시간 안에는 잡아내기 위한 여유값
MIN_FREE_DISK_MB = 500  # 이 프로젝트의 SQLite DB/로그는 수십MB 규모라 500MB면 충분히 보수적인 하한

ALERT_STATE_PATH = PROJECT_ROOT / "logs" / "healthcheck_alert_state.json"
ALERT_REMINDER_INTERVAL = timedelta(hours=2)  # 같은 실패가 계속되는 동안 이 주기로만 재알림

CheckResult = tuple[bool, str]


def _load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def _resolve_db_path(cfg: dict) -> Path:
    """★ 2026-07-25: DB 경로 자체 추정값(DEFAULT_DB_PATH)을 제거하고
    config.yaml의 db_path를 유일한 진실 소스로 삼는다(src.live.state.
    resolve_db_path() 재사용 — G4_PREFLIGHT_CONFIG_CHECK.md 확인 4에서
    발견된 "확정된 경로가 없다" 문제의 해소). HEALTHCHECK_DB_PATH 환경변수는
    테스트/운영 시 명시적 재정의용으로만 남겨둔다(기본값은 항상 config
    기준)."""
    override = os.environ.get("HEALTHCHECK_DB_PATH")
    return Path(override) if override else live_state.resolve_db_path(cfg)


def check_telegram_reachable() -> CheckResult:
    """★ 2026-07-29: send_message 대신 telegram.check_reachable()(get_me만
    호출, 채팅 메시지 미발신)을 쓴다 - 이전엔 이 체크가 15분마다 실제
    확인 메시지를 보내서 하루 96번씩 알림 피로를 유발했다(사용자 피드백)."""
    ok = telegram.check_reachable()
    if ok:
        return True, "텔레그램 API 연결 확인(get_me) 성공 - 채팅 메시지는 보내지 않음"
    return False, "텔레그램 발신 실패 - 토큰/chat_id 또는 네트워크 확인 필요"


def check_heartbeat_fresh(heartbeat_path: Path = HEARTBEAT_PATH, now: Optional[datetime] = None) -> CheckResult:
    now = now or datetime.now(timezone.utc)
    if not heartbeat_path.exists():
        return False, f"하트비트 파일 없음({heartbeat_path}) - 메인 루프가 한 번도 안 돌았거나 파일이 삭제됨"
    try:
        data = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        last_tick = datetime.fromisoformat(data["last_tick_iso"])
    except Exception as exc:  # noqa: BLE001 - 파일이 깨져있는 것도 이상 상태로 취급
        return False, f"하트비트 파일 파싱 실패: {type(exc).__name__}: {exc}"

    age = now - last_tick
    max_age = timedelta(minutes=TICK_INTERVAL_MINUTES * HEARTBEAT_STALE_MULTIPLE)
    if age > max_age:
        return False, f"마지막 틱이 {age} 전({last_tick.isoformat()}) - 임계치 {max_age} 초과, 메인 루프 응답 없음 의심"
    return True, f"마지막 틱 {age} 전 - 정상(임계치 {max_age})"


def check_exchange_reachable(cfg: dict) -> CheckResult:
    try:
        data_exchange = get_exchange(testnet=False)
        data_exchange.fetch_ticker(FT_SYMBOL)
    except Exception as exc:  # noqa: BLE001
        return False, f"데이터 거래소(프로덕션 공개 엔드포인트) 연결 실패: {type(exc).__name__}: {exc}"

    try:
        exec_exchange = executor.get_authenticated_exchange(testnet=(cfg["mode"] == "testnet"))
        exec_exchange.fetch_balance()
    except Exception as exc:  # noqa: BLE001
        return False, f"실행 거래소(인증) 연결 실패: {type(exc).__name__}: {exc}"

    return True, "데이터 거래소(공개)·실행 거래소(인증) 둘 다 연결 확인"


def check_state_matches_exchange(cfg: dict, db_path: Path) -> CheckResult:
    """src.live.state.recover_state()를 그대로 재사용한다 — 새 정합성
    로직을 만들지 않는다(태스크 명시 요구사항). ★ 이 계좌는 이전 봇이
    쓰던 testnet 계좌라 과거 활동이 섞여있을 수 있다는 우려가 있었으나,
    recover_state()가 쓰는 fetch_positions()/fetch_open_orders()는
    "현재" 상태만 반환하는 함수라(청산된 포지션·완료된 과거 주문은 애초에
    안 나옴) 시간 필터링이 필요 없다 — src/live/state.py의 실제 구현을
    재확인해 검증된 사실이다(추정 아님)."""
    if not db_path.exists():
        return False, f"state DB 파일 없음({db_path}) - 아직 최초 실행 전이거나 경로 설정 오류"
    try:
        exec_exchange = executor.get_authenticated_exchange(testnet=(cfg["mode"] == "testnet"))
    except Exception as exc:  # noqa: BLE001
        return False, f"실행 거래소 인증 실패라 정합성 확인 불가: {type(exc).__name__}: {exc}"

    result = live_state.recover_state(db_path, exec_exchange)
    if result.positions_match and result.orders_match:
        return True, "state.py 기록과 거래소 실제 상태 일치"
    return False, f"불일치 발견: {'; '.join(result.mismatches)}"


def check_disk_and_db_accessible(db_path: Path) -> CheckResult:
    check_dir = db_path.parent if db_path.parent.exists() else PROJECT_ROOT
    try:
        usage = shutil.disk_usage(check_dir)
    except Exception as exc:  # noqa: BLE001
        return False, f"디스크 사용량 조회 실패: {type(exc).__name__}: {exc}"

    free_mb = usage.free / (1024 * 1024)
    if free_mb < MIN_FREE_DISK_MB:
        return False, f"여유 디스크 {free_mb:.1f}MB < 최소 {MIN_FREE_DISK_MB}MB"

    try:
        live_state.init_db(db_path)  # 멱등 - 파일 생성/쓰기 권한 확인용으로 재사용
    except Exception as exc:  # noqa: BLE001
        return False, f"state DB 파일 접근/쓰기 실패({db_path}): {type(exc).__name__}: {exc}"

    return True, f"여유 디스크 {free_mb:.1f}MB, DB 접근 가능"


CHECKS = [
    ("telegram_reachable", lambda cfg, db_path: check_telegram_reachable()),
    ("heartbeat_fresh", lambda cfg, db_path: check_heartbeat_fresh()),
    ("exchange_reachable", lambda cfg, db_path: check_exchange_reachable(cfg)),
    ("state_matches_exchange", lambda cfg, db_path: check_state_matches_exchange(cfg, db_path)),
    ("disk_and_db_accessible", lambda cfg, db_path: check_disk_and_db_accessible(db_path)),
]


def run_all_checks(cfg: dict, db_path: Path) -> dict:
    results = {}
    for name, fn in CHECKS:
        try:
            passed, detail = fn(cfg, db_path)
        except Exception as exc:  # noqa: BLE001 - 체크 함수 자체의 버그도 "실패"로 취급, 전체 크래시 방지
            passed, detail = False, f"체크 실행 자체가 예외로 실패: {type(exc).__name__}: {exc}"
        results[name] = {"passed": passed, "detail": detail}
    return results


def _load_alert_state(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 상태파일이 깨져있으면 "새 실패"로 취급(안전 쪽 fallback)
        return None


def _save_alert_state(path: Path, state: Optional[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if state is None:
        path.unlink(missing_ok=True)
        return
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _should_send_failure_alert(prev_state: Optional[dict], signature: str, now: datetime) -> bool:
    """★ 알림 피로 방지(2026-07-29, 사용자 피드백): 같은 실패가 지속되는
    동안 15분마다 거의 동일한 CRITICAL을 반복 발신하던 것을 억제한다
    (실측: 파케이 손상 사고 때 7시간 동안 58건 발신). 새 실패(직전과 다른
    항목 집합)는 즉시 알리고, 같은 실패가 지속되면 ALERT_REMINDER_INTERVAL
    마다만 재알림한다. dedup 키는 detail 텍스트가 아니라 실패 항목 이름의
    집합이다 - heartbeat_fresh의 detail은 경과시간이 매 실행마다 바뀌어서
    텍스트 기준으로는 "같은 실패"가 절대 안 잡히기 때문."""
    if prev_state is None or prev_state.get("signature") != signature:
        return True
    last_alert = datetime.fromisoformat(prev_state["last_alert_ts"])
    return now - last_alert >= ALERT_REMINDER_INTERVAL


def main() -> int:
    cfg = _load_config()
    db_path = _resolve_db_path(cfg)

    results = run_all_checks(cfg, db_path)
    state = {"ts": datetime.now(timezone.utc).isoformat(), "checks": results}
    print(json.dumps(state, ensure_ascii=False, indent=2))

    failures = {k: v["detail"] for k, v in results.items() if not v["passed"]}
    if not failures:
        _save_alert_state(ALERT_STATE_PATH, None)  # 회복 시 초기화 - 다음 실패는 새 실패로 즉시 재알림
        return 0

    summary = "; ".join(f"{k}: {v}" for k, v in failures.items())
    print(f"[HEALTHCHECK] FAIL: {summary}", file=sys.stderr)

    signature = ",".join(sorted(failures.keys()))
    now = datetime.now(timezone.utc)
    prev_state = _load_alert_state(ALERT_STATE_PATH)
    if _should_send_failure_alert(prev_state, signature, now):
        try:
            telegram.send_critical_alert(f"헬스체크 실패: {summary}")
        except Exception:  # noqa: BLE001 - telegram_reachable 자체가 실패 원인이면 이 시도도 실패할 수 있음(예상된 한계, 위 docstring 참조)
            pass
        _save_alert_state(ALERT_STATE_PATH, {"signature": signature, "last_alert_ts": now.isoformat()})
    return 1


if __name__ == "__main__":
    sys.exit(main())
