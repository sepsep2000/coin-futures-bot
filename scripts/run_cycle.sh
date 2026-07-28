#!/usr/bin/env bash
# 봇 프로세스 감시자(cron/systemd timer 등록 대상) — src/live/runner.py가
# 이미 돌고 있으면 아무것도 안 하고, 안 돌고 있으면(최초 실행 또는 크래시
# 이후) (재)시작한다.
#
# ★ 2026-07-25 재설계(G4_PREFLIGHT_CONFIG_CHECK.md에서 발견된 문제 해소):
# 이 스크립트는 원래 "매 15분 사이클마다 실행돼 그때그때 1회 처리하고
# 끝난다"는 전제로 작성됐다(구 주석: "매 사이클마다 이 스크립트를 실행").
# 하지만 실제로 구현된 src/live/scheduler.py::run_live_loop()은
# LIVE_EXECUTION_ARCHITECTURE.md 3절이 확정한 "단일 프로세스·단일 루프"
# (15분 대기까지 자체 내부 루프로 처리하는 상주 프로세스)다 — 원래 전제와
# 아키텍처가 어긋나 있었다(그래서 존재하지도 않는 src/live/runner.py를
# --once 옵션으로 부르려 했던 것). 이제 이 스크립트는 "매 15분마다 새로
# 실행"이 아니라 "상주 프로세스가 살아있는지 감시하고, 죽어있으면 다시
# 띄운다"는 역할로 바뀐다 — cron에 5~10분 간격 정도로 등록해두면 된다
# (한 번 시작되면 그 사이 cron이 여러 번 더 돌아도 이미 실행 중이라 스킵).
#
# ★ gate_verify.py 사전체크를 뺀 이유(태스크 STEP4, 판단 근거): 이 파일이
# 참조하던 harness_config.json은 이 프로젝트에 존재한 적이 없고,
# gate_verify.py/scripts/checks/phase2·3·4_*.py는 SPEC.md의 구체적
# G1~G5 수치 게이트(Sharpe/Calmar/MaxDD 등)와는 성격이 다른 범용
# 회귀·이상탐지 스캐폴딩이다(reports/OFFICIAL_GATE_RESULT.md 0절에서
# 이미 실측 확인된 사실 — 재확인 없이 인용하지 않음, 직접 다시 읽고
# 같은 결론 확인함). 이 프로젝트가 실제로 거래를 허가하는 판단
# (G1~G5)은 이미 scripts/diag/official_gate_check.py 계열 스크립트와
# reports/OFFICIAL_GATE_RESULT.md·VOL_PARITY_19ASSET_RECALC.md 등에서
# 별도로, 명시적으로 끝났다 — 봇을 "시작"할 때마다 다시 통과해야 하는
# 게이트가 아니라 이미 config.yaml/strategies/에 반영된 결정이다.
# harness_config.json을 지금 채워서 이 사전체크를 "통과"시키는 건 실제로
# 의미 있는 체크를 추가하는 게 아니라 빈 통과를 만드는 일이라 하지
# 않는다(이전에 gate_verify.py의 "공허한 PASS" 버그를 고친 것과 정반대
# 방향의 실수가 된다). 봇이 실제로 살아있는지/정상인지는 대신
# scripts/healthcheck.py가 독립 프로세스로 별도 주기 감시한다(이미 구현
# 완료 — telegram/heartbeat/거래소연결/state정합성/디스크 5항목).
set -euo pipefail
cd "$(dirname "$0")/.."

PIDFILE="logs/runner.pid"
mkdir -p logs

# ★ 2026-07-28 재설계(2026-07-27 킬스위치 사고 대응 중 파케이 캐시 손상을
# 고친 뒤 재기동하다가 발견): 이 스크립트를 WSL cron이 nohup으로 실행하면서
# `.venv/Scripts/python.exe`(Windows 네이티브 실행파일)를 WSL 인터롭으로
# 넘기는 방식은 실측상 신뢰할 수 없었다 — 어떤 때는 시작은 되지만 계좌 조회
# 이전 단계에서 CPU 0%로 15분 넘게 완전히 멈췄고(진행 증거 없음), WSL을
# 완전 재기동(`wsl --shutdown`)한 뒤 재시도했을 때는 아예 `python.exe`
# 바이너리(PE 헤더 "MZ")를 셸이 그대로 실행하려다 즉시 깨졌다(인터롭
# binfmt 핸들러가 그 시점에 준비 안 됨으로 추정) — 재현성이 없어 운영에
# 못 쓴다. WSL 내부에 별도 venv(`.venv-wsl`)를 만들어 Windows 실행파일
# 경계를 아예 없앴다 — 이제 WSL 안에서 완결되는 네이티브 python3라 인터롭
# 자체가 개입하지 않는다. 이전처럼 PATH의 bare `python`이 프로젝트
# 의존성 없는 인터프리터를 가리킬 위험(과거 `ModuleNotFoundError: No
# module named 'yaml'` 실증)은 여전하므로 venv 경로를 명시적으로 우선한다.
if [ -x ".venv-wsl/bin/python" ]; then
    PYTHON_BIN=".venv-wsl/bin/python"
elif [ -x ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
else
    PYTHON_BIN="python"
fi

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "[$(date -u +%FT%TZ)] runner.py 이미 실행 중(PID $(cat "$PIDFILE")) - 아무것도 안 함"
    exit 0
fi

echo "[$(date -u +%FT%TZ)] runner.py 실행 중이 아님 - 시작($PYTHON_BIN)"
nohup "$PYTHON_BIN" src/live/runner.py >> logs/runner.log 2>&1 &
echo $! > "$PIDFILE"
echo "[$(date -u +%FT%TZ)] runner.py 시작됨(PID $!)"
