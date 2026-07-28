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

# ★ 2026-07-27 사고 대응 중 재발견(task j에서 이미 한 번 플래그됐던 미해결
# 배포 이슈): PATH상의 bare `python`은 환경(WSL 시스템 python, 이 세션의
# Windows 시스템 python 등)에 따라 프로젝트 의존성(yaml/ccxt/pandas 등)이
# 없는 인터프리터를 가리킬 수 있다(logs/runner.log의 과거
# `ModuleNotFoundError: No module named 'yaml'` 크래시가 실증). 프로젝트
# venv 인터프리터를 명시적으로 우선 사용하도록 고정 — WSL2는 Windows
# .exe를 경로로 직접 실행 가능하므로 .venv/Scripts/python.exe 하나로
# 양쪽 환경 모두 커버된다(별도 .venv/bin/python은 이 프로젝트에 없음,
# 실측 확인됨).
if [ -x ".venv/Scripts/python.exe" ]; then
    PYTHON_BIN=".venv/Scripts/python.exe"
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
