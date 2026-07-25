"""scripts/healthcheck.py — 실행 전 헬스체크 (골격, 구현 없음).

SPEC.md 6절: "python scripts/healthcheck.py — config 로드, API 연결
(testnet), 텔레그램 발신 확인". 골격 단계에서는 뭘 체크할지 목록만
정의한다 — 실제 체크 로직은 다음 세션.

사용(예정): python scripts/healthcheck.py
종료 코드 0 = 전부 통과, 1 = 하나라도 실패(gate_verify.py의 exit code
관례와 동일하게 맞춤).
"""

from __future__ import annotations

import sys


def check_config_loads() -> bool:
    """config.yaml이 파싱되고 필수 키(account/costs/regime/trend/gates/
    portfolio 등)가 전부 존재하는지."""
    raise NotImplementedError("TODO: yaml.safe_load + 필수 키 존재 확인")


def check_exchange_reachable() -> bool:
    """src.data.feed.get_exchange(testnet=True)로 testnet 연결 + 간단한
    공개 엔드포인트(ticker 등) 호출 성공 확인. ★ 절대 live 엔드포인트로
    확인하지 않는다(CLAUDE.md 규칙 5)."""
    raise NotImplementedError("TODO: get_exchange(testnet=True).fetch_ticker() 등")


def check_telegram_reachable() -> bool:
    """봇 토큰이 .env에 있고, 실제로 발신 가능한지(테스트 메시지 1건
    발신). 토큰 자체는 로그에 절대 출력하지 않는다(CLAUDE.md 금지사항)."""
    raise NotImplementedError("TODO: src.notify.telegram.send_message() 테스트 발신")


def check_state_db_writable() -> bool:
    """SQLite 상태 파일이 존재/생성 가능하고 쓰기 권한이 있는지
    (src.live.state.init_db 호출 가능 여부)."""
    raise NotImplementedError("TODO: src.live.state.init_db() 시험 호출 또는 파일 쓰기 테스트")


def check_required_env_vars() -> bool:
    """API 키/텔레그램 토큰 등 필수 환경변수가 .env에 설정돼 있는지
    존재 여부만 확인(값 자체는 출력·검증하지 않음 — 존재 확인만)."""
    raise NotImplementedError("TODO: os.environ에서 필수 키 존재 확인, 값은 출력 안 함")


CHECKS = [
    ("config_loads", check_config_loads),
    ("exchange_reachable", check_exchange_reachable),
    ("telegram_reachable", check_telegram_reachable),
    ("state_db_writable", check_state_db_writable),
    ("required_env_vars", check_required_env_vars),
]


def main() -> int:
    """TODO: CHECKS 순회하며 각 체크 실행, 실패 시 이름과 함께 기록,
    전부 통과하면 exit 0, 하나라도 실패하면 실패 목록 출력 후 exit 1.
    (지금은 골격만 — 아래는 채택 예정 구조 스케치일 뿐 실행되지 않음)"""
    raise NotImplementedError("TODO: CHECKS 순회 + 결과 집계 + exit code")


if __name__ == "__main__":
    sys.exit(main())
