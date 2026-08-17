"""src/notify/telegram.py — 텔레그램 알림 발신 전용.

LIVE_EXECUTION_ARCHITECTURE.md 4절 최소 이벤트 목록 그대로 구현
(filtered_trend 진입/청산, 2a 주간 리밸런스, CRITICAL, 일일요약) — 문서에
없는 알림 종류는 추가하지 않는다(알림 피로 방지 원칙, 문서 자체가 명시).

명령 수신(/status /pause /resume /close_all, handle_command)은 이번
범위 밖(우선순위 6, 별도 태스크) — 골격(NotImplementedError) 그대로 둔다.

★ 봇 토큰/chat_id는 config/.env(python-dotenv)로만 읽는다 — 코드 하드코딩
금지, 값 자체는 어디에도 출력/로그하지 않는다(CLAUDE.md 금지사항).

★ 설계 문서 대비 확장(사전 보고): 스켈레톤 시그니처는 전부 반환값이
`-> None`이었지만, "실패를 조용히 삼키지 않는다"는 이 프로젝트의 반복
원칙(gate_verify.py의 공허한 PASS 버그, executor.py의 OrderResult)에
맞춰 전부 `-> bool`(발신 성공 여부)로 바꿨다. 단 이 bool은 거래 로직이
분기에 쓰라고 만든 게 아니라 "실패를 로그로 확인할 수 있게" 하려는
용도다 — 텔레그램 발신 실패 자체는 executor.py 쪽 예외로 절대 전파되지
않는다(아래 send_message()의 try/except가 격리 지점).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import Bot

ENV_PATH = Path(__file__).resolve().parent.parent.parent / "config" / ".env"


def _log_stderr(message: str) -> None:
    """CLAUDE.md "except: pass 금지" — 알림 실패는 거래 로직을 막지 않되
    반드시 어딘가에는 남긴다(gate_verify.py/executor.py와 동일 패턴)."""
    print(f"[NOTIFY] {message}", file=sys.stderr)


def _get_credentials() -> tuple[str, str]:
    load_dotenv(ENV_PATH)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID가 config/.env에 설정되지 않음 (값 자체는 로그에 남기지 않음)"
        )
    return token, chat_id


def send_message(text: str, chat_id: Optional[str] = None) -> bool:
    """가장 기본적인 발신 함수 — 아래 특화 함수들이 내부적으로 이걸 호출.

    격리 원칙: 자격증명 누락이나 텔레그램 API/네트워크 오류는 여기서 잡아
    False를 반환한다(예외를 executor.py의 주문 로직까지 전파시키지 않음).
    반면 text가 비어있는 건 호출부 버그이므로(예: executor._side_from_direction과
    동일한 선례) 네트워크 시도 전에 즉시 ValueError를 던진다 — 이건
    "격리 대상"이 아니라 "호출 계약 위반"이라 구분한다.
    """
    if not text or not text.strip():
        raise ValueError("text는 비어있을 수 없습니다")

    try:
        token, default_chat_id = _get_credentials()
    except RuntimeError as exc:
        _log_stderr(f"send_message 실패(자격증명): {exc}")
        return False

    target_chat_id = chat_id or default_chat_id

    try:
        bot = Bot(token=token)
        asyncio.run(bot.send_message(chat_id=target_chat_id, text=text))
        return True
    except Exception as exc:  # noqa: BLE001 - 텔레그램 실패가 거래 로직으로 전파되면 안 됨(격리 지점)
        _log_stderr(f"send_message 발신 실패: {type(exc).__name__}: {exc}")
        return False


def check_reachable() -> bool:
    """★ 2026-07-29 알림 피로 방지: healthcheck.py가 15분마다 텔레그램
    연결을 확인하는데, 예전엔 그때마다 send_message()로 실제 채팅
    메시지("텔레그램 발신 정상 확인")를 보내서 하루 96번씩 무의미한
    알림이 쌓였다(사용자 피드백: 알림이 너무 자주 옴). get_me()는 토큰이
    유효하고 텔레그램 API에 네트워크로 도달 가능한지만 확인하고 채팅
    메시지는 전혀 보내지 않는다 - chat_id 설정 자체는 여전히 확인한다
    (get_me가 안 쓰더라도, "알림을 보낼 준비가 됐는지"의 일부이므로)."""
    try:
        token, _ = _get_credentials()
    except RuntimeError as exc:
        _log_stderr(f"check_reachable 실패(자격증명): {exc}")
        return False

    try:
        bot = Bot(token=token)
        asyncio.run(bot.get_me())
        return True
    except Exception as exc:  # noqa: BLE001 - send_message과 동일한 격리 원칙
        _log_stderr(f"check_reachable 실패: {type(exc).__name__}: {exc}")
        return False


def send_critical_alert(message: str) -> bool:
    """SPEC 5절 CRITICAL: 스탑 배치 실패, API 연속 오류, 킬스위치 발동,
    프로세스 재시작, 상태 불일치(recover_state 결과) 등. 즉시 발신,
    배치/디바운스 없음. 일반 알림과 절대 같은 프리픽스를 쓰지 않는다
    (알림 목록에서 CRITICAL이 섞여 안 보이면 설계 취지가 무너짐)."""
    return send_message(f"[CRITICAL] {message}")


def send_entry_exit_notification(symbol: str, strategy: str, direction: str, regime: str,
                                  qty: float, price: float, r_multiple: Optional[float],
                                  cumulative_pnl_usd: float) -> bool:
    """SPEC 5절: 페어/방향/레짐/수량/가격/R배수/누적PnL. filtered_trend
    전용(진입·청산 시 각각 호출) — r_multiple은 청산 시에만 값 있음(이
    계약 그대로 이용해 진입/청산 라벨을 결정한다, 별도 파라미터 신설 안 함)."""
    event = "청산" if r_multiple is not None else "진입"
    lines = [
        f"[{strategy}] {symbol} {direction} {event}",
        f"레짐: {regime}",
        f"수량: {qty}",
        f"가격: {price}",
    ]
    if r_multiple is not None:
        lines.append(f"R배수: {r_multiple:.2f}")
    lines.append(f"누적 PnL: ${cumulative_pnl_usd:,.2f}")
    return send_message("\n".join(lines))


def send_rebalance_notification(new_long: list[str], new_short: list[str],
                                 exited_long: list[str], exited_short: list[str],
                                 turnover_pct: float, realized_pnl_usd: float) -> bool:
    """2a 주간 리밸런스 실행 결과 — 신규/청산 자산 목록, 회전율, 이번 주
    실현손익(LIVE_EXECUTION_ARCHITECTURE.md 4절, SPEC 5절 확장분).

    ★ 2026-08-17 사용자 발견 대응: realized_pnl_usd가 예전엔 항상
    0.0으로 하드코딩돼 있어서(scheduler.py 쪽 수정 완료), "진짜 청산이
    없어서 0"과 "계산이 아예 안 돼서 0"을 구분할 방법이 없었다(실측:
    청산 5건이 실제로 있었던 사이클도 "$0.00"으로 표시됨). 이제
    scheduler.py가 실측 체결가로 계산해 넘겨주므로, 여기서는 청산 자체가
    없었던 사이클만 문구로 명시해 둘을 헷갈리지 않게 한다."""
    if not exited_long and not exited_short:
        pnl_line = f"실현 PnL: ${realized_pnl_usd:,.2f} (청산 없음, 신규진입만)"
    else:
        pnl_line = f"실현 PnL: ${realized_pnl_usd:,.2f}"
    lines = [
        "[2a] 주간 리밸런스",
        f"신규 롱: {', '.join(new_long) if new_long else '없음'}",
        f"신규 숏: {', '.join(new_short) if new_short else '없음'}",
        f"청산 롱: {', '.join(exited_long) if exited_long else '없음'}",
        f"청산 숏: {', '.join(exited_short) if exited_short else '없음'}",
        f"회전율: {turnover_pct:.2f}%",
        pnl_line,
    ]
    return send_message("\n".join(lines))


def send_daily_summary(total_trades: int, total_pnl_usd: float, win_rate_pct: float,
                        current_positions: list[dict], regime_state: str,
                        filtered_trend_pnl_usd: float, two_a_pnl_usd: float) -> bool:
    """SPEC 5절 일일요약(09:00 KST) + 다리별 분리 표기(설계 문서 4절 —
    합산만 보면 어느 다리가 기여했는지 안 보여서 추가). 호출 스케줄링은
    scheduler.py 책임(이 함수는 트리거 시점과 무관하게 데이터만 받아
    포맷팅)."""
    lines = [
        "[일일요약]",
        f"거래수: {total_trades}",
        f"PnL: ${total_pnl_usd:,.2f} (filtered_trend ${filtered_trend_pnl_usd:,.2f} / 2a ${two_a_pnl_usd:,.2f})",
        f"승률: {win_rate_pct:.1f}%",
        f"레짐: {regime_state}",
        f"현재 포지션: {len(current_positions)}건",
    ]
    for p in current_positions:
        lines.append(f"  - {p.get('symbol')} {p.get('strategy')} {p.get('direction')} qty={p.get('qty')}")
    return send_message("\n".join(lines))


def handle_command(update) -> None:
    """/status /pause /resume /close_all 디스패처. 인증(누가 명령을
    실행할 수 있는지)은 LIVE_EXECUTION_ARCHITECTURE.md 6절 미결 사항 —
    다음 세션 결정 후 구현. /close_all은 확인 프롬프트 필요(설계 문서
    4절 — 되돌릴 수 없는 액션이라 실수 방지).

    ★ 이번 태스크(우선순위4, 발신 전용) 범위 밖 — 명령 수신은 우선순위6
    별도 태스크. 골격 그대로 유지."""
    raise NotImplementedError("TODO: 명령별 분기 + /close_all 확인 프롬프트 (우선순위6 별도 태스크)")
