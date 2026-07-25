"""src/notify/telegram.py — 텔레그램 알림 (골격, 구현 없음).

LIVE_EXECUTION_ARCHITECTURE.md 4절 대응. SPEC.md 5절이 정한 최소 이벤트
목록 그대로 — 과도한 알림 지양(매 틱마다 "신호 없음" 통지 금지, 상태
변화 시에만).

★ 봇 토큰은 .env(기존 python-dotenv 관례, requirements.txt에 이미 있음)
로만 읽는다 — 코드에 하드코딩 금지, 로그에도 출력 금지(CLAUDE.md 금지사항).
"""

from __future__ import annotations

from typing import Optional


def send_message(text: str, chat_id: Optional[str] = None) -> None:
    """가장 기본적인 발신 함수 — 아래 특화 함수들이 내부적으로 이걸 호출.
    TODO: python-telegram-bot Bot.send_message() 래핑, 재시도 정책은
    executor.place_order()와 별개로 결정(알림 실패가 거래 실패를 막으면
    안 됨 — best-effort, 실패해도 예외를 삼키지 않고 로깅만 함, CLAUDE.md
    "except: pass 금지" 원칙 유지)."""
    raise NotImplementedError("TODO: telegram bot 발신")


def send_critical_alert(message: str) -> None:
    """SPEC 5절 CRITICAL: 스탑 배치 실패, API 연속 오류, 킬스위치 발동,
    프로세스 재시작, 상태 불일치(recover_state 결과) 등. 즉시 발신,
    배치/디바운스 없음."""
    raise NotImplementedError("TODO: send_message()에 [CRITICAL] 프리픽스 + 즉시 발신")


def send_entry_exit_notification(symbol: str, strategy: str, direction: str, regime: str,
                                  qty: float, price: float, r_multiple: Optional[float],
                                  cumulative_pnl_usd: float) -> None:
    """SPEC 5절: 페어/방향/레짐/수량/가격/R배수/누적PnL. filtered_trend
    전용(진입·청산 시 각각 호출) — r_multiple은 청산 시에만 값 있음."""
    raise NotImplementedError("TODO: 포맷팅 + send_message()")


def send_rebalance_notification(new_long: list[str], new_short: list[str],
                                 exited_long: list[str], exited_short: list[str],
                                 turnover_pct: float, realized_pnl_usd: float) -> None:
    """2a 주간 리밸런스 실행 결과 — 신규/청산 자산 목록, 회전율, 이번 주
    실현손익(LIVE_EXECUTION_ARCHITECTURE.md 4절, SPEC 5절 확장분)."""
    raise NotImplementedError("TODO: 포맷팅 + send_message()")


def send_daily_summary(total_trades: int, total_pnl_usd: float, win_rate_pct: float,
                        current_positions: list[dict], regime_state: str,
                        filtered_trend_pnl_usd: float, two_a_pnl_usd: float) -> None:
    """SPEC 5절 일일요약(09:00 KST) + 다리별 분리 표기(설계 문서 4절 —
    합산만 보면 어느 다리가 기여했는지 안 보여서 추가). 호출 스케줄링은
    scheduler.py가 아니라 별도 cron/스케줄(TODO: 다음 세션에서 결정 —
    이 함수는 트리거 시점과 무관하게 데이터만 받아 포맷팅)."""
    raise NotImplementedError("TODO: 포맷팅 + send_message()")


def handle_command(update) -> None:
    """/status /pause /resume /close_all 디스패처. 인증(누가 명령을
    실행할 수 있는지)은 LIVE_EXECUTION_ARCHITECTURE.md 6절 미결 사항 —
    다음 세션 결정 후 구현. /close_all은 확인 프롬프트 필요(설계 문서
    4절 — 되돌릴 수 없는 액션이라 실수 방지)."""
    raise NotImplementedError("TODO: 명령별 분기 + /close_all 확인 프롬프트")
