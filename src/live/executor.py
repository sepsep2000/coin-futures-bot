"""src/live/executor.py — 주문 실행 계층.

LIVE_EXECUTION_ARCHITECTURE.md 1절/5절 구현. 이 파일만 testnet/live를
분기한다(그 위 신호계산/사이징은 src/strategy, src/risk를 백테스트와
동일하게 호출) — src/data/feed.py의 get_exchange(testnet) 패턴(불리언
플래그로 sandbox 토글)을 그대로 재사용한다.

★ 설계 문서 대비 확장 1건(사전 보고): feed.py의 get_exchange()는 인증이
필요 없는 공개 데이터 수집 전용이라 API 키를 안 받는다. 주문 실행은
인증이 필수라 이 파일에 get_authenticated_exchange()를 새로 둔다 — 같은
testnet 불리언 → sandbox 토글 패턴은 그대로, credential 주입만 추가.
실제 testnet 인증 호출로 확인한 결과, ccxt 최신 버전(4.5.68)은 binanceusdm
futures sandbox에 대해 소프트 디프리케이션 경고를 NotSupported 예외로
던진다(공개 엔드포인트는 영향 없음, 인증 필요한 사설 엔드포인트만) —
ccxt 자체가 제공하는 옵션(`disableFuturesSandboxWarning: True`)으로
우회한다(엔드포인트 자체는 살아있고 정상 응답함, 실측 확인 완료).

★ 설계 문서 대비 확장 2건(사전 보고): 골격 시그니처엔 없던 db_path
파라미터를 place_order/poll_order_status/cancel_order/ensure_stop_placed에
추가했다 — "모든 주문 결과는 state.py로 기록"이 설계 문서 자체의 요구사항인데
골격 시그니처엔 상태 저장 경로가 없어서 넣지 않으면 요구사항을 못 지킨다.

★ 설계 문서 대비 확장 3건(사전 보고, 2026-08-17): 사고/정상 실현PnL 자동
분리(src/live/incident_pnl.py) 기능을 위해 fetch_income_since() 신설 —
Binance 실측 income 원장(`fapiPrivateGetIncome`)을 페이지네이션으로 전량
조회한다. 이 함수는 조회 전용(주문 실행 없음)이라 순수 I/O 래퍼로만
두고, 사고 구간 분류 로직(순수함수)은 incident_pnl.py로 분리한다.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import ccxt
from dotenv import load_dotenv

from src.live import state as live_state

MAX_RETRIES = 3  # CLAUDE.md 오류 처리 표준: 지수 백오프 3회 재시도
POLL_INTERVAL_SEC = 2  # TODO: 실측 후 조정 여지 있음(설계 확정치 아님)
DEFAULT_POLL_TIMEOUT_SEC = 60
STOP_CHECK_MAX_RETRIES = 3  # ensure_stop_placed도 동일 재시도 표준 재사용

ENV_PATH = Path(__file__).resolve().parent.parent.parent / "config" / ".env"


@dataclass
class OrderResult:
    order_id: Optional[str]
    status: str  # "filled" | "partial" | "pending" | "failed" | "cancelled"
    filled_qty: float
    avg_fill_price: Optional[float]
    error: Optional[str] = None


@dataclass
class StopPlacementResult:
    """2026-08-11 사고②③ 대응으로 ensure_stop_placed()의 반환 타입을
    bool에서 확장 - "스탑을 못 걸었다"와 "대신 즉시 시장가로 청산했다"는
    호출부(scheduler.py)가 서로 다르게(전자는 CRITICAL만, 후자는 포지션
    상태 정리+알림) 처리해야 해서 bool 하나로는 구분이 안 됐다."""
    stop_confirmed: bool
    closed_instead: bool = False
    close_result: Optional[OrderResult] = None


def get_authenticated_exchange(testnet: bool = True) -> ccxt.Exchange:
    """★ 기본값 testnet=True — CLAUDE.md 규칙5(코드 기본값 절대 live 아님).
    API 키는 config/.env에서만 읽는다(python-dotenv, 기존 관례) — 값은
    어디에도 출력하지 않는다."""
    load_dotenv(ENV_PATH)
    if testnet:
        api_key = os.environ.get("BINANCE_TESTNET_API_KEY")
        api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET")
    else:
        api_key = os.environ.get("BINANCE_API_KEY")
        api_secret = os.environ.get("BINANCE_API_SECRET")

    if not api_key or not api_secret:
        kind = "테스트넷" if testnet else "실계좌"
        raise RuntimeError(f"{kind} API 키가 config/.env에 설정되지 않음 (값 자체는 로그에 남기지 않음)")

    exchange = ccxt.binanceusdm({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {
            "disableFuturesSandboxWarning": True,
            # 2026-07-25 실측 확인(src/live/runner.py 실제 testnet 통합 테스트 중 발견):
            # recover_state()가 심볼 지정 없이 fetch_open_orders()를 호출하는데(계좌
            # 전체의 미체결 주문을 봐야 하므로 — filtered_trend/2a가 서로 다른 심볼을
            # 쓴다), ccxt 기본값은 이걸 ExchangeError로 막는다("계정 전체 조회는 레이트
            # 리밋이 10~40배"라는 경고를 에러로 승격시킴). 이 옵션으로 그 경고를
            # 끈다 — 실제 호출 자체는 정상 동작함(엔드포인트가 막힌 게 아니라 ccxt의
            # 방어적 기본값일 뿐, disableFuturesSandboxWarning과 동일한 종류의 우회).
            #
            # 2026-07-26 보강: ccxt/binance.py 실제 조건문을 직접 읽어 확인한 결과
            # (.venv/Lib/site-packages/ccxt/binance.py:7128-7131) —
            #   warnWithoutSymbol = self.options['fetchOpenOrders']['warnWithoutSymbol']
            #   optValue = self.options['warnOnFetchOpenOrdersWithoutSymbol']  # 하위호환용
            #   if optValue or (optValue is None and warnWithoutSymbol): raise ExchangeError
            # 위 nested 옵션 하나로 이미 충분(optValue가 None이면 nested 값을 본다).
            # 그래도 상위호환용 플래그도 같이 꺼서 이중 안전장치(ccxt 버전이 바뀌어도
            # 안전하도록) — 둘 중 하나가 True로 남아있어도 절대 경고를 되살리지 않는다.
            "warnOnFetchOpenOrdersWithoutSymbol": False,
            "fetchOpenOrders": {"warnWithoutSymbol": False},
        },
    })
    if testnet:
        exchange.set_sandbox_mode(True)
    return exchange


def _log_stderr(message: str) -> None:
    """CLAUDE.md "except: pass 금지" 원칙 — 실패는 항상 어딘가에 남긴다.
    scripts/gate_verify.py의 notify() 패턴과 동일하게 stderr 사용(별도
    로깅 프레임워크 새로 도입하지 않음)."""
    print(f"[EXECUTOR] {message}", file=sys.stderr)


def _side_from_direction(direction: str) -> str:
    if direction == "long":
        return "buy"
    if direction == "short":
        return "sell"
    raise ValueError(f"direction은 'long' 또는 'short'여야 합니다: {direction!r}")


def _normalize_status(ccxt_status: Optional[str], filled: float = 0.0) -> str:
    if ccxt_status == "closed":
        return "filled"
    if ccxt_status in ("canceled", "cancelled", "expired"):
        return "cancelled"
    if ccxt_status == "open":
        return "partial" if filled > 0 else "pending"
    return "pending"


def place_order(exchange, db_path: Path, symbol: str, direction: str, qty: float,
                 order_type: str = "market", limit_price: Optional[float] = None,
                 reduce_only: bool = False) -> OrderResult:
    """주문 제출 + 즉시 주문 ID 확보. fire-and-forget 금지(CLAUDE.md) —
    이 함수는 제출만 하고 체결 확인은 poll_order_status()가 별도로 한다.
    실패 시 MAX_RETRIES(3회) 지수 백오프(2**attempt초) 후 실패 반환.
    모든 시도 결과(성공/최종실패)를 state.save_order로 기록한다.

    ★ reduce_only(2026-07-27 사고 대응 추가): 기본값 False로 기존 호출부
    전부 동작 불변. 청산/킬스위치처럼 "포지션을 줄이기만 해야 하는" 주문에
    True로 넘기면 거래소가 방향 실수로 포지션을 반대로 뒤집거나 배증시키는
    것 자체를 원천 차단한다(reports/G4_KILLSWITCH_INCIDENT_ANALYSIS.md 7절
    긴급 청산 시 실사용 검증됨) - 로컬 상태가 실수로 틀리더라도 거래소가
    한 번 더 막아주는 안전망."""
    side = _side_from_direction(direction)
    last_error: Optional[str] = None
    params = {"reduceOnly": True} if reduce_only else {}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if order_type == "market":
                order = exchange.create_order(symbol, "market", side, qty, None, params)
            elif order_type == "limit":
                if limit_price is None:
                    raise ValueError("order_type='limit'이면 limit_price가 필요합니다")
                order = exchange.create_order(symbol, "limit", side, qty, limit_price, params)
            else:
                raise ValueError(f"지원하지 않는 order_type: {order_type!r}")

            order_id = str(order["id"])
            filled_qty = float(order.get("filled") or 0.0)
            status = _normalize_status(order.get("status"), filled_qty)
            avg_price = order.get("average") or order.get("price")

            live_state.save_order(db_path, order_id, symbol, side, qty, limit_price, status)
            return OrderResult(order_id=order_id, status=status, filled_qty=filled_qty,
                                avg_fill_price=float(avg_price) if avg_price else None)

        except Exception as exc:  # noqa: BLE001 - 네트워크/거래소 예외 전부 재시도 대상
            last_error = f"{type(exc).__name__}: {exc}"
            _log_stderr(f"place_order 시도 {attempt}/{MAX_RETRIES} 실패({symbol}, {direction}, {qty}): {last_error}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

    # 재시도 소진 - 실패를 성공처럼 반환하지 않는다. 실패 자체도 state에 기록(감사 흔적).
    failed_id = f"FAILED_{symbol}_{side}_{int(time.time())}"
    live_state.save_order(db_path, failed_id, symbol, side, qty, limit_price, "failed")
    return OrderResult(order_id=None, status="failed", filled_qty=0.0, avg_fill_price=None, error=last_error)


def poll_order_status(exchange, db_path: Path, order_id: str, symbol: str,
                       timeout_sec: int = DEFAULT_POLL_TIMEOUT_SEC) -> OrderResult:
    """주문 ID 폴링으로 체결 확인(CLAUDE.md: fire-and-forget 금지).
    POLL_INTERVAL_SEC 간격으로 timeout_sec까지 조회, 매 폴링 결과를
    state.update_order_status로 기록. filled/cancelled면 즉시 반환,
    아니면 timeout까지 반복 후 마지막 관측치를 반환(status="pending"
    유지 — timeout을 "실패"로 조작하지 않는다, 호출부가 판단)."""
    elapsed = 0
    last_result: Optional[OrderResult] = None

    while elapsed <= timeout_sec:
        try:
            order = exchange.fetch_order(order_id, symbol)
            filled_qty = float(order.get("filled") or 0.0)
            status = _normalize_status(order.get("status"), filled_qty)
            avg_price = order.get("average") or order.get("price")

            live_state.update_order_status(db_path, order_id, status)
            last_result = OrderResult(order_id=order_id, status=status, filled_qty=filled_qty,
                                       avg_fill_price=float(avg_price) if avg_price else None)
            if status in ("filled", "cancelled"):
                return last_result
        except Exception as exc:  # noqa: BLE001
            _log_stderr(f"poll_order_status 조회 실패({order_id}): {type(exc).__name__}: {exc}")
            last_result = OrderResult(order_id=order_id, status="pending", filled_qty=0.0,
                                       avg_fill_price=None, error=f"{type(exc).__name__}: {exc}")

        time.sleep(POLL_INTERVAL_SEC)
        elapsed += POLL_INTERVAL_SEC

    return last_result or OrderResult(order_id=order_id, status="pending", filled_qty=0.0,
                                       avg_fill_price=None, error="timeout")


def cancel_order(exchange, db_path: Path, order_id: str, symbol: str) -> bool:
    try:
        exchange.cancel_order(order_id, symbol)
        live_state.update_order_status(db_path, order_id, "cancelled")
        return True
    except Exception as exc:  # noqa: BLE001
        _log_stderr(f"cancel_order 실패({order_id}): {type(exc).__name__}: {exc}")
        return False


def handle_partial_fill(exchange, order_result: OrderResult, policy: str = "leave_pending") -> OrderResult:
    """미체결 잔량 처리 정책. LIVE_EXECUTION_ARCHITECTURE.md 6절 —
    구체적 재주문 파라미터(대기시간/가격조정)는 다음 세션에서 결정,
    이번엔 정책 인터페이스만 정의(policy: "leave_pending" | "cancel_remainder"
    | "reprice" — 셋 다 미구현). PAPER_TRADING_REMAINING_WORK.md 우선순위9,
    이번 태스크(우선순위2/3) 범위 밖이라 골격 유지."""
    raise NotImplementedError("TODO: policy별 분기 구현 (PAPER_TRADING_REMAINING_WORK.md 우선순위9)")


def _fetch_open_stop_orders(exchange, raw_symbol: str) -> list:
    """Binance USDM의 STOP_MARKET은 "Algo Orders"라는 별도 서브시스템에
    들어가고 fetch_open_orders()(일반 주문 엔드포인트)에는 절대 나타나지
    않는다 — 실측으로 확인(테스트넷에 STOP_MARKET을 직접 걸고
    fetch_open_orders() 호출 시 0건 반환됨). ccxt에 unified 메서드가 없어
    raw 엔드포인트 fapiPrivateGetOpenAlgoOrders()를 직접 호출한다. 이
    엔드포인트는 symbol 필터를 지원하지 않아 전체를 받아 직접 필터링한다."""
    algo_orders = exchange.fapiPrivateGetOpenAlgoOrders()
    return [o for o in algo_orders if o.get("symbol") == raw_symbol]


def cancel_stop_orders(exchange, symbol: str) -> int:
    """★ 2026-08-11 사고③ 조사 중 발견: 포지션을 완전청산해도 걸어둔
    STOP_MARKET(Algo Order)을 취소하는 코드가 어디에도 없어서 고아
    주문으로 계속 남는다(실측: ETH 완전청산 후에도 trigger=1910.35 스탑이
    거래소에 그대로 남아있었음). reduceOnly라 실제로 위험하진 않지만
    (포지션이 없으면 거래소가 트리거돼도 거부) 계속 쌓이면 계정 상태를
    읽기 어렵게 만든다. 포지션이 완전히 닫힌 직후 호출해 남은 스탑을
    전부 정리한다. 개별 취소 실패는 로깅만 하고 나머지는 계속 시도한다
    (fire-and-forget 금지 원칙 - 실패해도 조용히 넘어가지 않음)."""
    raw_symbol = exchange.market(symbol)["id"]
    open_stops = _fetch_open_stop_orders(exchange, raw_symbol)
    cancelled = 0
    for stop in open_stops:
        if str(stop.get("algoStatus", "")).upper() != "NEW":
            continue
        try:
            exchange.fapiPrivateDeleteAlgoOrder({"algoId": stop["algoId"]})
            cancelled += 1
        except Exception as exc:  # noqa: BLE001
            _log_stderr(f"cancel_stop_orders: {symbol} algoId={stop.get('algoId')} 취소 실패: {type(exc).__name__}: {exc}")
    return cancelled


def _stop_condition_already_true(stop_side: str, stop_price: float, current_price: float) -> bool:
    """sell 스탑(롱 보호)은 가격이 stop_price 이하로 떨어지면, buy 스탑(숏
    보호)은 stop_price 이상으로 오르면 트리거된다 - Binance가 -2021로
    거부했다는 건 이미 이 조건이 참이라는 뜻이므로, 우리도 같은 부등호로
    직접 확인한다(거래소 판단을 재현하는 것뿐, 새 로직 아님)."""
    if stop_side == "sell":
        return current_price <= stop_price
    return current_price >= stop_price


def _safe_fetch_last_price(exchange, symbol: str) -> Optional[float]:
    try:
        return float(exchange.fetch_ticker(symbol)["last"])
    except Exception as exc:  # noqa: BLE001 - 2차 조회 실패가 원래 재시도 흐름을 막으면 안 됨
        _log_stderr(f"ensure_stop_placed: 현재가 재조회 실패({symbol}): {type(exc).__name__}: {exc}")
        return None


def ensure_stop_placed(exchange, db_path: Path, symbol: str, position,
                        max_retries: int = STOP_CHECK_MAX_RETRIES) -> StopPlacementResult:
    """포지션 존재 + 스탑 부재 = 최우선 복구 대상(CLAUDE.md). 매 시도마다
    (1) 거래소에 실제로 스탑 주문이 걸려있는지 확인 (2) 없으면 배치 시도.
    max_retries 소진 후에도 확인 안 되면 **stop_confirmed=False를 반환하고
    실패 사실을 stderr 로그 + state.py에 명시적으로 기록**한다 — 성공한
    것처럼 조용히 True를 반환하지 않는다(gate_verify.py의 공허한 PASS
    버그와 같은 종류의 실수를 반복하지 않기 위한 설계).

    ★ 실측으로 발견한 버그 수정(2026-07-25): 최초 구현은 존재 확인에
    fetch_open_orders()를 썼는데, 이 엔드포인트는 STOP_MARKET(Algo Order)을
    아예 반환하지 않아 "이미 걸려있는 스탑"을 매번 "없음"으로 오판, 매
    재시도마다 새 스탑을 중복 생성했다(테스트넷에서 중복 4건 실측 확인 후
    전부 취소 정리 완료). fapiPrivateGetOpenAlgoOrders() 기반으로 교체.

    ★ 2026-08-11 사고③ 대응(근본원인 A): position.current_stop이 pandas
    계산값(numpy.float64)으로 넘어오면 ccxt의 파라미터 파서가 이를 못
    알아보고 "requires a triggerPrice"로 거부한다(직접 재현 확인 -
    numpy.float64는 ccxt.safe_string_2가 None으로 처리, native float는
    정상 처리). stopPrice를 보내기 직전 float()로 명시 캐스팅해 원천 차단.

    ★ 2026-08-11 사고② 대응(근본원인 B): 거래소가 OrderImmediatelyFillable
    (-2021, "이미 트리거 조건 충족")로 거부하면, 같은 가격으로 재시도해봐야
    시장이 그 사이 되돌아오지 않는 한 결과가 똑같다 - 실측(2026-07-27)으로
    확인된 패턴. 이 예외를 잡으면 현재가를 재조회해 정말 조건이 충족됐는지
    확인하고, 충족됐으면 대기하는 스탑 대신 reduceOnly 시장가로 즉시
    청산한다(스탑의 목적 자체가 "이 가격을 넘으면 즉시 나간다"이므로 이미
    넘었으면 지금 나가는 게 취지와 정확히 같다 - 새 리스크 정책이 아님)."""
    stop_side = "sell" if position.direction == "long" else "buy"
    stop_price = float(position.current_stop)  # 근본원인 A: native float 명시 캐스팅
    last_error: Optional[str] = None

    for attempt in range(1, max_retries + 1):
        try:
            exchange.load_markets()
            raw_symbol = exchange.market(symbol)["id"]
            open_stops = _fetch_open_stop_orders(exchange, raw_symbol)
            stop_exists = any(
                o.get("orderType") == "STOP_MARKET"
                and str(o.get("side", "")).upper() == stop_side.upper()
                and str(o.get("algoStatus", "")).upper() == "NEW"
                for o in open_stops
            )
            if stop_exists:
                return StopPlacementResult(stop_confirmed=True)

            exchange.create_order(
                symbol, "STOP_MARKET", stop_side, position.qty,
                params={"stopPrice": stop_price, "reduceOnly": True},
            )
            # 배치 직후 이번 루프에서 바로 반환하지 않는다 - 다음 반복에서
            # 실제로 걸렸는지 재확인한 뒤에만 stop_confirmed=True.
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            _log_stderr(f"ensure_stop_placed 시도 {attempt}/{max_retries} 실패({symbol}): {last_error}")

            if isinstance(exc, ccxt.OrderImmediatelyFillable):
                current_price = _safe_fetch_last_price(exchange, symbol)
                if current_price is not None and _stop_condition_already_true(stop_side, stop_price, current_price):
                    close_direction = "short" if position.direction == "long" else "long"
                    close_result = place_order(exchange, db_path, symbol, close_direction, position.qty,
                                                reduce_only=True)
                    if close_result.status == "filled":
                        _log_stderr(
                            f"ensure_stop_placed: {symbol} 스탑조건 이미 충족(현재가={current_price}, "
                            f"스탑가={stop_price}) - 대기용 스탑 대신 reduceOnly 시장가로 즉시청산"
                        )
                        return StopPlacementResult(stop_confirmed=False, closed_instead=True,
                                                    close_result=close_result)
                    _log_stderr(
                        f"ensure_stop_placed: {symbol} 스탑조건 충족 확인했으나 대체청산 주문도 실패"
                        f"(status={close_result.status}) - 기존 재시도 계속"
                    )

        if attempt < max_retries:
            time.sleep(2 ** attempt)

    # 재시도 소진 - 스탑 미확인 상태를 명확히 기록(조용히 넘어가지 않음).
    failure_id = f"STOP_MISSING_{symbol}_{position.direction}_{int(time.time())}"
    live_state.save_order(db_path, failure_id, symbol, stop_side, position.qty, stop_price, "failed")
    _log_stderr(
        f"CRITICAL: {symbol} {position.direction} 포지션의 스탑을 {max_retries}회 시도 후에도 "
        f"확인/배치하지 못함 (마지막 오류: {last_error}) - 즉시 수동 확인 필요"
    )
    return StopPlacementResult(stop_confirmed=False)


def fetch_income_since(exchange, start_ms: int, page_limit: int = 1000) -> list[dict]:
    """`start_ms`(epoch ms, UTC) 이후 전체 income 원장을 페이지네이션으로
    조회한다(REALIZED_PNL/COMMISSION/FUNDING_FEE 등 전 타입 포함, 필터링은
    호출부 책임). 재시도는 CLAUDE.md 표준(3회 지수백오프)을 따른다 - 이
    함수가 실패하면 사고/정상 PnL 분리 자체가 불가능하므로 조용히 빈
    리스트를 반환하지 않고 예외를 그대로 전파한다(호출부가 CRITICAL 처리)."""
    records: list[dict] = []
    cursor_ms = start_ms
    while True:
        last_error: Optional[Exception] = None
        page: Optional[list] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                page = exchange.fapiPrivateGetIncome({"startTime": cursor_ms, "limit": page_limit})
                break
            except Exception as exc:  # noqa: BLE001 - CLAUDE.md 표준: 재시도 소진 시에만 전파
                last_error = exc
                _log_stderr(f"fetch_income_since 시도 {attempt}/{MAX_RETRIES} 실패: {type(exc).__name__}: {exc}")
                if attempt < MAX_RETRIES:
                    time.sleep(2 ** attempt)
        if page is None:
            raise RuntimeError(f"fetch_income_since: {MAX_RETRIES}회 재시도 후에도 실패") from last_error

        if not page:
            break
        records.extend(page)
        if len(page) < page_limit:
            break
        cursor_ms = page[-1]["time"] + 1

    return records
