"""
통합 게이트 감시 러너. Phase 2/3/4 체크를 한 곳에서 관리.

사용법:
    python scripts/gate_verify.py

harness_config.json에서 각 phase 섹션이 있으면 그 phase 체크를 돌리고,
없으면 스킵한다 (아직 해당 Phase 안 왔다는 뜻이므로 실패 아님).
그래서 Phase 넘어갈 때마다 이 파일을 다시 짤 필요 없이,
harness_config.json에 섹션만 추가하면 됨.

exit code 0 = 전부 PASS
exit code 1 = FAIL 있음
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from checks import phase2_backtest, phase3_walkforward, phase4_live  # noqa: E402

CONFIG_PATH = os.environ.get("HARNESS_CONFIG_PATH", "harness_config.json")
STATE_PATH = os.environ.get("HARNESS_STATE_PATH", "logs/harness_state.json")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def notify(message: str):
    print(f"[HARNESS ALERT] {message}", file=sys.stderr)
    os.makedirs("logs", exist_ok=True)
    with open("logs/harness_alerts.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")


def main():
    os.makedirs("logs", exist_ok=True)
    config = load_config()
    all_failures = []
    ran_phases = []

    if "phase2" in config:
        ran_phases.append("phase2")
        all_failures += phase2_backtest.run_all(config["phase2"])
    if "phase3" in config:
        ran_phases.append("phase3")
        all_failures += phase3_walkforward.run_all(config["phase3"])
    if "phase4" in config:
        ran_phases.append("phase4")
        all_failures += phase4_live.run_all(config["phase4"])

    state = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": "FAIL" if all_failures else "PASS",
        "ran_phases": ran_phases,
        "failures": all_failures,
    }
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    if all_failures:
        notify("하네스 FAIL:\n" + "\n".join(all_failures))
        print(json.dumps(state, ensure_ascii=False, indent=2))
        sys.exit(1)
    else:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        sys.exit(0)


if __name__ == "__main__":
    main()
