#!/usr/bin/env bash
# 매 사이클마다 이 스크립트를 실행 (cron/systemd timer 등록 대상)
# 1) 하네스 통과해야만 2) 실제 봇 사이클 실행
# harness_config.json에 채워진 phase 섹션만 체크됨 (Phase 안 왔으면 자동 스킵)
set -euo pipefail
cd "$(dirname "$0")/.."

if python scripts/gate_verify.py; then
    echo "[$(date -u +%FT%TZ)] harness PASS -> 봇 사이클 실행"
    python src/live/runner.py --once   # G5 완료 후 실제 엔트리포인트로 경로 확정
else
    echo "[$(date -u +%FT%TZ)] harness FAIL -> 봇 실행 스킵, logs/harness_alerts.log 확인"
    exit 1
fi
