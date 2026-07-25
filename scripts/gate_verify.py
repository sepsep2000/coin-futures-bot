"""
통합 게이트 감시 러너. Phase 2/3/4 체크를 한 곳에서 관리.

사용법:
    python scripts/gate_verify.py

harness_config.json에서 각 phase 섹션이 있으면 그 phase 체크를 돌리고,
없으면 스킵한다 (아직 해당 Phase 안 왔다는 뜻이므로 실패 아님).
그래서 Phase 넘어갈 때마다 이 파일을 다시 짤 필요 없이,
harness_config.json에 섹션만 추가하면 됨.

★ 단, 매칭된 phase가 하나도 없으면(harness_config.json이 없거나, 있어도
phase2/3/4 섹션이 하나도 없으면) "0건 검증하고 PASS"로 조용히 넘어가지
않는다 - status="ERROR"로 명시 반환한다(2026-07-25 수정, 원래는 이 경우도
PASS를 반환해 설정 누락/경로 오류를 숨기는 함정이 있었다).

exit code 0 = 전부 PASS(매칭된 phase가 1개 이상이고 전부 통과)
exit code 1 = FAIL 있음, 또는 ERROR(매칭된 phase 0개 - 검증 자체가 안 됨)
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from checks import phase2_backtest, phase3_walkforward, phase4_live  # noqa: E402

CONFIG_PATH = os.environ.get("HARNESS_CONFIG_PATH", "harness_config.json")
STATE_PATH = os.environ.get("HARNESS_STATE_PATH", "logs/harness_state.json")

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_ERROR = "ERROR"


def load_config() -> tuple[dict, bool]:
    """반환: (config, config_file_existed). 파일이 없는 경우와 파일은
    있지만 내용이 비어있는/매칭 안 되는 경우를 evaluate()에서 구분하려면
    "파일이 애초에 존재했는가"를 별도로 알아야 한다."""
    if not os.path.exists(CONFIG_PATH):
        return {}, False
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f), True


def notify(message: str):
    print(f"[HARNESS ALERT] {message}", file=sys.stderr)
    os.makedirs("logs", exist_ok=True)
    with open("logs/harness_alerts.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")


def evaluate(config: dict, config_existed: bool) -> dict:
    """config만으로 게이트 상태를 판정한다(파일 I/O 없음 - 순수함수라
    테스트하기 쉽다). main()이 이 함수의 결과에 타임스탬프를 붙이고
    파일에 쓰고 알림을 보낸다.

    status:
      - "ERROR": 매칭된 phase가 0개. config_existed로 원인을 구분한
        메시지를 failures에 담는다(파일 없음 vs 파일은 있지만 섹션 없음).
        0건 검증을 PASS로 취급하지 않는다 - 이게 이번 수정의 핵심.
      - "FAIL": 매칭된 phase가 1개 이상이고 그 중 실패가 있음.
      - "PASS": 매칭된 phase가 1개 이상이고 전부 통과.
    """
    all_failures: list[str] = []
    ran_phases: list[str] = []

    if "phase2" in config:
        ran_phases.append("phase2")
        all_failures += phase2_backtest.run_all(config["phase2"])
    if "phase3" in config:
        ran_phases.append("phase3")
        all_failures += phase3_walkforward.run_all(config["phase3"])
    if "phase4" in config:
        ran_phases.append("phase4")
        all_failures += phase4_live.run_all(config["phase4"])

    if not ran_phases:
        if not config_existed:
            reason = f"{CONFIG_PATH} 파일이 없음 - 게이트 설정 자체가 로드되지 않았다"
        else:
            reason = f"{CONFIG_PATH}는 존재하지만 phase2/phase3/phase4 섹션이 하나도 없음 - 설정 키 오탈자나 경로 오류 의심"
        return {"status": STATUS_ERROR, "ran_phases": [], "failures": [f"NO_PHASES_RAN: {reason}"]}

    if all_failures:
        return {"status": STATUS_FAIL, "ran_phases": ran_phases, "failures": all_failures}

    return {"status": STATUS_PASS, "ran_phases": ran_phases, "failures": []}


def main():
    os.makedirs("logs", exist_ok=True)
    config, config_existed = load_config()
    result = evaluate(config, config_existed)

    state = {"ts": datetime.now(timezone.utc).isoformat(), **result}
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    if state["status"] != STATUS_PASS:
        notify(f"하네스 {state['status']}:\n" + "\n".join(state["failures"]))
        print(json.dumps(state, ensure_ascii=False, indent=2))
        sys.exit(1)
    else:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        sys.exit(0)


if __name__ == "__main__":
    main()
