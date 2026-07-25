"""src/live/scheduler.py — 메인 실행 루프 (골격, 구현 없음).

LIVE_EXECUTION_ARCHITECTURE.md 3절 대응. 단일 프로세스·단일 루프,
매 15m 틱마다 filtered_trend를 항상 체크하고 2a는 주간 경계에서만 체크
(자본 공유·킬스위치 공유 이유로 프로세스 분리 안 함 — 설계 문서 3절
근거 참조).

신호 계산은 src/strategy/*, strategies/*(filtered_trend, 2a)를 백테스트와
동일하게 그대로 호출한다 — 이 파일은 "언제 부를지"만 담당하고 "무엇을
할지"는 기존 순수함수에 위임한다(SPEC 3절 원칙).

★ 골격만 — 실제 루프/타이밍/에러처리는 다음 세션에서 구현.
"""

from __future__ import annotations

from pathlib import Path


def run_live_loop(cfg: dict, db_path: Path) -> None:
    """메인 진입점. 프로세스 시작 시:
    1. src.live.state.recover_state() 호출 — 불일치 시 CRITICAL 알림 후
       사람 확인 대기(자동 진행 안 함).
    2. 이후 매 15m 틱마다 _tick() 반복(무한 루프, /pause 명령 수신 시
       신규진입만 중단하고 루프 자체는 계속 — 기존 포지션 관리는 유지).
    """
    raise NotImplementedError("TODO: recover_state() -> while True: _tick() 루프 + 종료 시그널 처리")


def _tick(cfg: dict, db_path: Path, exchange, ts) -> None:
    """15m 틱 1회 처리. 순서(설계 문서 1절 "최우선 복구 대상" 원칙 반영):
    1. 열린 포지션 전부에 대해 ensure_stop_placed() 먼저 확인
    2. _check_daily_loss_limit() — 초과 시 이번 틱 신규진입 스킵
    3. _process_filtered_trend_tick() — 항상 실행
    4. _is_2a_rebalance_tick(ts)가 True면 _process_2a_rebalance() 실행
    5. 자본 스냅샷 저장(save_equity_snapshot)
    """
    raise NotImplementedError("TODO: 위 5단계 순서대로 호출")


def _is_2a_rebalance_tick(ts) -> bool:
    """2a 리밸런스 경계 판정 — 2a 백테스트의 pandas resample("W") 관례와
    동일 경계(매주 월요일 00:00 UTC)를 써야 백테스트-라이브 정합성이
    유지된다(SPEC 3절 "백테스트와 라이브가 동일 함수 호출" 원칙의 연장 —
    엄밀히는 스케줄 자체도 동일 관례를 따라야 신호 타이밍이 어긋나지
    않는다)."""
    raise NotImplementedError("TODO: ts가 주간 경계(월요일 00:00 UTC)인지 확인")


def _process_filtered_trend_tick(cfg: dict, db_path: Path, exchange, ts) -> None:
    """strategies/filtered_trend.py의 신호 생성 로직을 최신 봉 데이터에
    적용 — 백테스트용 run()과 신호 계산 부분은 동일 함수, 여기서는 그
    결과로 실제 주문을 낼지만 결정(src.live.executor 호출)."""
    raise NotImplementedError("TODO: 최신 봉 fetch -> strategies.filtered_trend 신호 계산 -> executor.place_order")


def _process_2a_rebalance(cfg: dict, db_path: Path, exchange, ts) -> None:
    """strategies/2a.py의 랭킹 로직을 최신 데이터에 적용, 청산 먼저/신규
    진입 나중 순서는 LIVE_EXECUTION_ARCHITECTURE.md 6절 미결 사항 — 다음
    세션에서 결정 후 구현."""
    raise NotImplementedError("TODO: 최신 20자산 데이터 fetch -> strategies.2a 랭킹 -> 청산/진입 순서 결정 -> executor 호출")


def _check_daily_loss_limit(cfg: dict, db_path: Path) -> bool:
    """계좌 레벨 킬스위치 — src.risk.daily_loss_limit_breached()를
    포트폴리오 합산 당일 손익에 그대로 적용(신규 로직 아님, 기존 함수
    재사용)."""
    raise NotImplementedError("TODO: src.risk.daily_loss_limit_breached() 호출")
