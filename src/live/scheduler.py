"""src/live/scheduler.py — 메인 실행 루프.

LIVE_EXECUTION_ARCHITECTURE.md 3절 대응. 단일 프로세스·단일 루프,
매 15m 틱마다 filtered_trend를 항상 체크하고 2a는 주간 경계에서만 체크
(자본 공유·킬스위치 공유 이유로 프로세스 분리 안 함 — 설계 문서 3절
근거 참조).

신호 계산은 src/strategy/*, strategies/*(filtered_trend, 2a)를 백테스트와
동일하게 그대로 호출한다 — 이 파일은 "언제 부를지"만 담당하고 "무엇을
할지"는 기존 순수함수에 위임한다(SPEC 3절 원칙).

★ 틱 순서(reports/REBALANCE_ORDER_ANALYSIS.md 실측 계산 확정, 임의 선택
아님): (1) 기존 포지션 전부 ensure_stop_placed (2) 일일 손실한도 체크,
발동 시 미체결 신규진입 주문 취소 (3) 2a 리밸런스 경계면 "2a 청산 전부
-> 2a 신규진입 전부" (4) filtered_trend 처리(자체적으로 이미 청산 체크가
진입 평가보다 먼저 오는 구조) (5) 자본 스냅샷 저장. "모든 청산 -> 모든
신규진입"이 전략 무관 불변식이다 — 신규진입끼리 순간적으로 마진이
이중계상되는 게 유일한 위험이고, 청산은 마진을 풀 뿐이라 순서가 안전에
영향을 안 준다(리포트 3절).

★ 데이터/실행 거래소를 분리한다: 신호 계산용 시세는 항상 프로덕션
엔드포인트(`src.data.feed.get_exchange(testnet=False)`)에서 받는다 —
testnet의 과거 OHLCV는 합성 데이터라 신호가 왜곡된다(feed.py 모듈
docstring에 이미 실측됨). 주문 실행만 `cfg["mode"]`에 따라 testnet/live로
분기한다(`src.live.executor.get_authenticated_exchange`). 이 분리 자체가
"페이퍼/라이브 동일 코드 경로" 원칙(설계 문서 5절)의 정확한 적용이다.

★ [2026-07-25 해소] filtered_trend의 라이브 청산 로직 완성: `state.py`의
`positions` 스키마를 확장(`initial_stop`/`entry_fee_usd`/
`funding_paid_usd`/`partial_taken`)해 매 틱 `Position` 객체를 완전히
재구성할 수 있게 됐고, `_manage_open_filtered_trend_position()`이
engine.py의 `_manage_trend_position()`/`_apply_funding()`을 그대로
호출한다(재해석 없음) — 부분익절/트레일링/시간청산/스탑로스 네 경로
전부 백테스트와 동일 로직. 커버리지 재확인: reports/EXIT_LOGIC_COVERAGE.md
(과거 68.1%→해당 문서의 실측 비율 참조). 실제 체결가는 시뮬레이션
가격을 덮어써 telegram 보고에 쓴다(청산 여부/시점 판단 로직 자체는
100% engine.py 소스, 보고되는 가격 숫자만 실측으로 교체 — bars_held는
컬럼 저장 대신 entry_time에서 매 틱 계산).

★ 2a의 "안전장치"(ensure_stop_placed 통합, 태스크 명시 요구사항): 2a는
설계상 스탑로스 개념이 없는 전략(주간 고정보유, 백테스트에 스탑 없음)
이라 통계적 의미의 스탑을 강제하면 검증된 전략을 변형하게 된다. 대신
**운영상 재해 방지용 광폭 백스톱**(엔트리 대비 30% 역행, `strategies/2a.py`의
정상 주간 변동폭보다 훨씬 넓어 평시엔 절대 안 걸림 — 봇이 다운돼 다음
주 리밸런스를 못 하는 극단 상황에서만 의미있는 안전망)을 건다. 전략
통계에 영향 없이(정상 경로에서 트리거 안 됨) "포지션 있는데 스탑 전혀
없음" 상태만은 피한다 — CLAUDE.md 원칙과 2a의 무스탑 설계를 둘 다
지키는 절충.

★ `_is_2a_rebalance_tick`: 골격 최초 작성 시 "매주 월요일 00:00 UTC"라고
적었는데, 이번에 `pandas.resample("W")`를 실측 재확인한 결과 **일요일
00:00 UTC**가 맞다(기본 앵커는 W-SUN, 라벨이 구간의 오른쪽 끝=일요일).
잘못된 과거 서술이었다 — 실측 없이 문서만 믿고 넘어갔으면 실제 리밸런스
타이밍이 백테스트와 어긋났을 뻔했다.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pandas as pd

from src.backtest.cost_model import entry_fill_price, taker_fee
from src.backtest.engine import Position, SymbolData, _apply_funding, _current_r_multiple, _manage_trend_position
from src.data.feed import cache_path, get_exchange, load_cache, update_funding_cache, update_ohlcv_cache
from src.live import executor
from src.live import state as live_state
from src.notify import telegram
from src.risk import can_open_new_position, consecutive_error_kill_switch_triggered, daily_loss_limit_breached, position_size
from src.strategy.regime import TREND, classify_regime
from src.strategy.trend import generate_trend_signals, trailing_stop

strategy_2a = importlib.import_module("strategies.2a")  # "2a"는 식별자로 import 불가 (portfolio_combined.py와 동일 관례)
from strategies.filtered_trend import MOMENTUM_LOOKBACK_DAYS, SYMBOL as FT_SYMBOL, _momentum_agrees  # noqa: E402

CATASTROPHIC_STOP_PCT = 0.30  # 2a 백스톱 — 전략 파라미터 아님, 운영 안전망(모듈 docstring 참조)
DATA_LOOKBACK_DAYS_15M = 30  # ATR/EMA/BB/chandelier 지표 워밍업에 충분한 여유(가장 긴 lookback: bb_width_percentile_window=120h=5일)
CONSECUTIVE_ERROR_THRESHOLD = 5  # SPEC.md 2.4 "연속 5회 API 오류" 그대로(src/risk.py 기본값과 일치) — 임의 정의 아님
HEARTBEAT_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "heartbeat.json"  # scripts/healthcheck.py가 같은 경로를 읽음


def _log_stderr(message: str) -> None:
    print(f"[SCHEDULER] {message}", file=sys.stderr)


def run_live_loop(cfg: dict, db_path: Path, max_iterations: Optional[int] = None) -> None:
    """메인 진입점.

    ★ max_iterations: 골격에 없던 파라미터(사전 보고, 최소 확장) — 무한
    루프를 자동 테스트/통합 테스트에서 유한 횟수로 끝내려면 필요하다.
    None(기본값)이면 무한 루프(실제 운영), 정수면 그 횟수만큼 틱을 처리한
    뒤 정상 반환(테스트 전용).

    ★ 연속오류 킬스위치(SPEC 2.4 "연속 5회 API 오류 시 전 포지션 청산 후
    봇 정지", src/risk.py::consecutive_error_kill_switch_triggered):
    "오류"로 세는 대상은 **틱 전체가 잡히지 않은 예외로 끝난 경우**만이다
    (개별 주문 1건 실패는 executor.py가 이미 자체 3회 재시도+CRITICAL로
    처리하고 예외를 던지지 않으므로 이 카운터에 안 잡힌다 — 그 경로는
    일일손실한도/ensure_stop_placed 안전망이 이미 담당). 연속 카운터는
    이 함수 스코프의 지역 변수(프로세스 재시작 시 자연히 0으로 초기화,
    DB에 영속화하지 않음) — 발동하면 전 포지션(양쪽 전략) 청산 시도 후
    `run_live_loop`가 즉시 반환해 루프를 멈춘다("봇 정지"는 프로세스
    종료를 뜻함, 일일손실한도처럼 신규진입만 막고 계속 도는 게 아님).
    재개는 사람이 원인을 확인하고 프로세스를 수동으로 재시작해야 한다 —
    자동 재개 없음(카운터도 그때 자연히 리셋됨, 별도 리셋 로직 불필요)."""
    data_exchange = get_exchange(testnet=False)  # 신호용 시세는 항상 프로덕션(테스트넷 과거데이터는 합성값)
    exec_exchange = executor.get_authenticated_exchange(testnet=(cfg["mode"] == "testnet"))
    exec_exchange.load_markets()

    reconciliation = live_state.recover_state(db_path, exec_exchange)
    if not reconciliation.positions_match or not reconciliation.orders_match:
        telegram.send_critical_alert(
            "재시작 시 상태 불일치 발견 - 자동 진행하지 않음, 수동 확인 필요: "
            + "; ".join(reconciliation.mismatches)
        )
        raise RuntimeError(
            f"recover_state 불일치로 자동 시작 중단: {reconciliation.mismatches}"
        )

    iteration = 0
    consecutive_errors = 0
    while max_iterations is None or iteration < max_iterations:
        ts = pd.Timestamp.now(tz="UTC").floor("15min")
        try:
            _tick(cfg, db_path, data_exchange, exec_exchange, ts)
            consecutive_errors = 0
        except Exception as exc:  # noqa: BLE001 - 한 틱의 예외로 루프 전체가 죽으면 안 됨(다음 틱에 재시도)
            consecutive_errors += 1
            _log_stderr(f"_tick 실패({ts}, 연속 {consecutive_errors}회): {type(exc).__name__}: {exc}")
            telegram.send_critical_alert(
                f"틱 처리 중 예외 발생({ts}, 연속 {consecutive_errors}회): {type(exc).__name__}: {exc}"
            )
            if consecutive_error_kill_switch_triggered(consecutive_errors, CONSECUTIVE_ERROR_THRESHOLD):
                _execute_kill_switch_liquidation(db_path, exec_exchange, consecutive_errors)
                return
        _write_heartbeat(HEARTBEAT_PATH, pd.Timestamp.now(tz="UTC"))
        iteration += 1
        if max_iterations is None or iteration < max_iterations:
            _sleep_until_next_tick(ts)


def _write_heartbeat(path: Path, ts: pd.Timestamp) -> None:
    """scripts/healthcheck.py가 읽는 생존 신호. ★ 배치 위치가 중요하다 —
    이 호출은 try/except 블록 "다음"에만 오므로, `_tick()`이 예외 없이
    멈춰버리는 행(hang) 상태에서는 이 줄에 도달하지 못해 하트비트가
    갱신되지 않는다(의도된 동작 — 행 감지가 이 안전망의 존재 이유다).
    `_tick()`이 예외를 던지고 빠르게 끝나는 경우는(루프 자체는 살아있음,
    이미 별도 CRITICAL로 알림됨) 갱신된다 — "루프가 죽었다/멈췄다"와
    "루프는 살아서 매 틱 실패를 보고하고 있다"를 구분하기 위함."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"last_tick_iso": ts.isoformat()}, f)


def _execute_kill_switch_liquidation(db_path: Path, exec_exchange, consecutive_errors: int) -> None:
    """연속오류 킬스위치 발동 처리 — SPEC 2.4 그대로: 전 포지션(filtered_trend
    + 2a 구분 없이 전부) 청산 시도 후 CRITICAL 발신. 청산 주문 자체가
    실패해도(이미 API가 불안정한 상황이니 충분히 가능) executor.place_order가
    예외를 던지지 않고 status="failed"를 반환하므로 이 함수는 안전하게
    끝까지 순회한다 — 청산 성공/실패 목록을 전부 CRITICAL에 담아 사람이
    무엇이 남았는지 즉시 알 수 있게 한다(방치 금지).

    ★ [2026-07-27 사고 대응, reports/G4_KILLSWITCH_INCIDENT_ANALYSIS.md 2.3절]
    청산 대상/수량은 반드시 `exec_exchange.fetch_positions()`(실제 거래소)에서
    구한다 — 로컬 DB(`live_state.load_open_positions`)는 참고(전략 라벨링)
    용도로만 쓴다. 사고 당시 로컬 DB가 이미 오염된 상태(실제로는 청산 안
    됐는데 partial_taken 갱신 실패로 stale)였는데, 킬스위치가 그 DB만 믿고
    청산 방향/수량을 결정해 실제로는 포지션을 배증시켰다 — "안전장치가
    안전장치 자신의 입력을 검증하지 않는" 결함이었다. 또한 방향 실수로
    같은 사고가 재발하는 것 자체를 막기 위해 reduce_only=True로 주문한다
    (거래소 레벨에서 포지션 반전/배증을 원천 차단, 7절 긴급 청산에서 실사용
    검증됨).

    ★ 2026-08-17 사고 대응(실측 재현, Binance testnet -1007 장애 중):
    연속오류 킬스위치가 발동한 원인 자체가 API 불안정인 경우, 바로 이
    `fetch_positions()` 조회도 같은 이유로 실패할 수 있다 - 이 함수의
    나머지 로직(각 place_order 실패 허용)은 이미 그 상황을 가정하고
    설계됐는데, 정작 맨 처음 조회에는 그 원칙이 빠져 있어 여기서 예외가
    나면 잡히지 않고 run_live_loop 전체가 크래시했다(실측 확인:
    23:15 UTC 킬스위치 발동 시 fetch_positions()가 -1007로 실패, 그대로
    전파돼 프로세스 사망). 청산 주문은 한 건도 시도되지 못한 채
    죽었으므로 그 자체로는 안전했지만("아무 것도 안 함"이 최악은
    아니었음), 킬스위치가 있어야 할 상황에서 조용히 사라지는 건 여전히
    방치다 - 조회 실패도 명시적으로 잡아 CRITICAL로 알리고, 프로세스는
    (재시도 루프에 빠지지 않고) 깨끗하게 정지한다."""
    try:
        real_positions = [
            p for p in exec_exchange.fetch_positions()
            if p.get("contracts") and float(p["contracts"]) != 0
        ]
    except Exception as exc:  # noqa: BLE001 - 킬스위치 자신도 조회 실패에 안전해야 함(위 사고 대응)
        telegram.send_critical_alert(
            f"연속오류 킬스위치 발동({consecutive_errors}회 연속 실패) - 봇 정지. "
            f"청산 대상 조회 자체가 실패({type(exc).__name__}: {exc}) - 청산 시도 불가. "
            f"거래소 실제 포지션/스탑 상태를 즉시 수동 확인해야 함."
        )
        return

    db_positions_by_symbol = {p["symbol"]: p for p in live_state.load_open_positions(db_path)}

    liquidated: list[str] = []
    failed: list[str] = []
    for p in real_positions:
        symbol = p["symbol"]
        qty = abs(float(p["contracts"]))
        exchange_direction = "long" if p.get("side") == "long" else "short"
        close_direction = "short" if exchange_direction == "long" else "long"
        db_pos = db_positions_by_symbol.get(symbol)
        strategy_label = db_pos["strategy"] if db_pos else "DB에 기록 없음(거래소 실측으로만 발견)"

        result = executor.place_order(exec_exchange, db_path, symbol, close_direction, qty, reduce_only=True)
        if result.status == "filled":
            if db_pos:
                live_state.delete_position(db_path, symbol, db_pos["strategy"])
            liquidated.append(f"{symbol}({strategy_label})")
        else:
            failed.append(f"{symbol}({strategy_label})")

    # 실거래소엔 없는데 DB에만 남아있는 stale 레코드도 함께 정리(청산할 실물이 없으므로
    # 주문 없이 삭제만) - 그대로 두면 다음 재시작 때 recover_state()가 다시 혼란을 일으킨다.
    stale_db_only = [s for s in db_positions_by_symbol if s not in {p["symbol"] for p in real_positions}]
    for symbol in stale_db_only:
        strategy = db_positions_by_symbol[symbol]["strategy"]
        live_state.delete_position(db_path, symbol, strategy)

    telegram.send_critical_alert(
        f"연속오류 킬스위치 발동({consecutive_errors}회 연속 실패) - 봇 정지. "
        f"청산 완료(거래소 실측 기준): {liquidated or '없음'}. "
        f"청산 실패(수동 확인 필요): {failed or '없음'}. "
        f"DB에만 남아있던 stale 레코드 정리: {stale_db_only or '없음'}."
    )


def _sleep_until_next_tick(ts: pd.Timestamp) -> None:
    import time

    next_tick = ts + pd.Timedelta(minutes=15)
    wait_sec = max(0.0, (next_tick - pd.Timestamp.now(tz="UTC")).total_seconds())
    time.sleep(wait_sec)


def _handle_stop_result(db_path: Path, symbol: str, strategy: str, direction: str,
                         stop_result) -> None:
    """★ 2026-08-11 사고② 대응: ensure_stop_placed()가 스탑 대신 즉시
    시장가 청산으로 대체한 경우(closed_instead=True), 그 결과를 호출부가
    반드시 반영해야 한다 - 안 하면 거래소는 이미 포지션이 없는데 DB엔
    여전히 열려있는 것으로 남아 다음 틱에서 혼란을 일으킨다(이 프로젝트가
    반복적으로 지적해 온 "로컬 상태 vs 거래소 실측 불일치" 패턴 재발
    방지). stop_confirmed=False인데 closed_instead도 False인 경우는
    executor.py가 이미 CRITICAL로 로깅했으므로 여기서 추가로 할 일 없음."""
    if not stop_result.closed_instead:
        return
    live_state.delete_position(db_path, symbol, strategy)
    close = stop_result.close_result
    telegram.send_critical_alert(
        f"{symbol}({strategy}) {direction} 포지션: 스탑 배치가 거래소에서 거부(이미 트리거 조건 충족)돼 "
        f"대신 reduceOnly 시장가로 즉시청산함 - qty={close.filled_qty}, 체결가={close.avg_fill_price}"
    )


def _verify_and_cap_close_qty(exec_exchange, symbol: str, expected_direction: str, requested_qty: float) -> float:
    """★ 2026-08-11 ETH phantom long 사고 대응(reports/G4_ETH_MANAGEMENT_
    PHANTOM_LONG_20260811.md): 청산 주문을 내기 "전에" 거래소 실측으로
    실제 청산 가능한 수량을 확인한다 - DB만 믿고 청산 주문을 내면, 그
    사이 거래소 스탑이 이미 포지션을 정리해버린 경우 청산이 아니라
    신규진입이 돼버린다(로컬 상태 vs 거래소 실측 불일치 패턴, 2026-07-27
    킬스위치 사고와 같은 계열).

    - 거래소에 해당 심볼 포지션이 아예 없거나 방향이 다르면(이미
      청산됐거나 반전됐음) 0.0을 반환 - 호출부는 주문을 내지 않고 DB만
      정리해야 한다는 신호.
    - 있으면 min(요청 수량, 실제 보유 수량)을 반환 - 부분청산 요청이
      실제 보유량보다 많아지는 경우(예: 거래소 자동 스탑이 이미 일부를
      먼저 정리한 경우)에도 초과분이 신규진입으로 새지 않게 한다."""
    real_positions = [
        p for p in exec_exchange.fetch_positions()
        if p.get("symbol") == symbol and p.get("contracts") and float(p["contracts"]) != 0
    ]
    if not real_positions:
        return 0.0
    p = real_positions[0]
    real_direction = "long" if p.get("side") == "long" else "short"
    if real_direction != expected_direction:
        return 0.0
    real_qty = abs(float(p["contracts"]))
    return min(requested_qty, real_qty)


def _tick(cfg: dict, db_path: Path, data_exchange, exec_exchange, ts: pd.Timestamp) -> None:
    """15m 틱 1회 처리 — 순서는 모듈 docstring 및
    reports/REBALANCE_ORDER_ANALYSIS.md 3절 확정 순서 그대로."""
    for pos in live_state.load_open_positions(db_path):
        position = SimpleNamespace(direction=pos["direction"], qty=pos["qty"], current_stop=pos["current_stop"])
        stop_result = executor.ensure_stop_placed(exec_exchange, db_path, pos["symbol"], position)
        _handle_stop_result(db_path, pos["symbol"], pos["strategy"], pos["direction"], stop_result)

    daily_pnl, total_equity = _get_daily_pnl_and_equity(cfg, db_path, exec_exchange, ts)
    entries_blocked = daily_loss_limit_breached(daily_pnl, total_equity, cfg["account"]["daily_loss_limit_pct"])
    if entries_blocked:
        _handle_daily_loss_limit_breach(db_path, exec_exchange, daily_pnl, total_equity, ts,
                                         cfg["account"]["daily_loss_limit_pct"])

    if _is_2a_rebalance_tick(ts):
        _process_2a_rebalance(cfg, db_path, data_exchange, exec_exchange, ts, total_equity, entries_blocked)

    _process_filtered_trend_tick(cfg, db_path, data_exchange, exec_exchange, ts, total_equity, entries_blocked)

    live_state.save_equity_snapshot(db_path, ts.isoformat(), total_equity, total_equity, total_equity)


def _is_2a_rebalance_tick(ts: pd.Timestamp) -> bool:
    """2a 리밸런스 경계 판정 — `strategies/2a.py::run()`이 쓰는
    `pandas.resample("W")` 기본 앵커(W-SUN)와 정확히 같은 경계를 실측
    확인 후 사용한다(일요일 00:00 UTC, 월요일 아님 — 모듈 docstring 참조).
    """
    return ts.weekday() == 6 and ts.hour == 0 and ts.minute == 0


def _fetch_total_equity_usd(exchange) -> float:
    balance = exchange.fetch_balance()
    return float(balance["total"]["USDT"])


def _get_daily_pnl_and_equity(cfg: dict, db_path: Path, exec_exchange, ts: pd.Timestamp) -> tuple[float, float]:
    """오늘(UTC) 첫 스냅샷 대비 현재 자본 변화 = daily_pnl. 스냅샷은 매 틱
    마지막에 저장되므로(이 함수는 그 전에 호출됨) "오늘 첫 스냅샷"은 항상
    이전 틱들이 남긴 기록이다 — 당일 첫 틱에는 기준점이 없어 daily_pnl=0
    (진짜 손실이 있어도 놓칠 수 있는 게 아니라, 그 시점까지 이 프로세스가
    추적한 손익이 0이라는 뜻 — 프로세스가 자정 근처에 재시작됐다면
    그 이전 손익은 recover_state()가 이미 별도로 다뤘어야 할 몫이다,
    한계로 인지하고 넘어간다)."""
    total_equity = _fetch_total_equity_usd(exec_exchange)
    day_start_iso = ts.replace(hour=0, minute=0, second=0, microsecond=0, nanosecond=0).isoformat()
    todays_snapshots = live_state.load_equity_snapshots_since(db_path, day_start_iso)
    if not todays_snapshots:
        return 0.0, total_equity
    day_start_equity = todays_snapshots[0]["total_equity_usd"]
    return total_equity - day_start_equity, total_equity


def _handle_daily_loss_limit_breach(db_path: Path, exec_exchange, daily_pnl: float, total_equity: float,
                                     ts: pd.Timestamp, daily_loss_limit_pct: float) -> None:
    """SPEC 1: 일일손실 -3% -> 당일 신규진입 금지(킬스위치, 기존 포지션
    관리는 계속 — 전량청산 아님, 그건 별도의 연속오류 킬스위치 몫이라
    이번 태스크 범위 밖이다). 발동을 이 틱에서 처음 감지했을 때만
    CRITICAL 1회 발신(과거 스냅샷에도 이미 이 기준을 넘겼던 게 있으면
    "이미 통보했다"고 보고 재통보하지 않는다 - 매 15분마다 스팸 방지,
    telegram.py의 "알림 피로 방지" 원칙과 동일선상). 이미 제출된
    미체결 신규진입 주문은 명시적으로 취소한다(방치 금지, 태스크 명시
    요구사항) - 심볼에 기존 포지션이 없는 pending 주문 = 신규진입으로
    판단(현재 스키마로는 이게 유일하게 안전한 구분법)."""
    day_start_iso = ts.replace(hour=0, minute=0, second=0, microsecond=0, nanosecond=0).isoformat()
    todays_snapshots = live_state.load_equity_snapshots_since(db_path, day_start_iso)
    already_notified_today = False
    if todays_snapshots:
        day_start_equity = todays_snapshots[0]["total_equity_usd"]
        for snap in todays_snapshots:
            prior_pnl = snap["total_equity_usd"] - day_start_equity
            if daily_loss_limit_breached(prior_pnl, day_start_equity, daily_loss_limit_pct):
                already_notified_today = True
                break

    open_symbols = {p["symbol"] for p in live_state.load_open_positions(db_path)}
    pending = live_state.load_pending_orders(db_path)
    cancelled = []
    for order in pending:
        if order["symbol"] not in open_symbols:  # 기존 포지션 없음 = 신규진입 주문으로 판단
            if executor.cancel_order(exec_exchange, db_path, order["order_id"], order["symbol"]):
                cancelled.append(order["order_id"])

    if not already_notified_today:
        telegram.send_critical_alert(
            f"일일 손실한도 발동 - 당일 신규진입 금지. daily_pnl=${daily_pnl:,.2f}, "
            f"total_equity=${total_equity:,.2f}, 취소된 미체결 신규진입 주문: {cancelled}"
        )


def _process_2a_rebalance(cfg: dict, db_path: Path, data_exchange, exec_exchange, ts: pd.Timestamp,
                           total_equity: float, entries_blocked: bool) -> None:
    """2a 주간 리밸런스. 순서(REBALANCE_ORDER_ANALYSIS.md 확정): 청산 전부
    -> 신규진입 전부(entries_blocked=True면 신규진입 단계 스킵, 청산은
    계속 - SPEC "신규진입만 금지" 원칙)."""
    project_root = Path(__file__).resolve().parent.parent.parent
    tickers_path = project_root / "config" / "tickers.txt"
    tickers = [ln.strip() for ln in tickers_path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.strip().startswith("#")]
    since_ms = int(pd.Timestamp(cfg["data"]["since"]).timestamp() * 1000)
    ohlcv_dir = project_root / cfg["data"]["ohlcv_cache_dir"]
    funding_dir = project_root / cfg["data"]["funding_cache_dir"]
    for pair in tickers:
        update_ohlcv_cache(data_exchange, pair, "15m", since_ms, ohlcv_dir)
        update_funding_cache(data_exchange, pair, since_ms, funding_dir)

    target = strategy_2a.run_live_step(cfg, project_root, ts)
    target_long, target_short, weight = target["long_group"], target["short_group"], target["weight"]

    current_positions = live_state.load_open_positions(db_path, strategy="2a")
    current_long = {p["symbol"] for p in current_positions if p["direction"] == "long"}
    current_short = {p["symbol"] for p in current_positions if p["direction"] == "short"}

    def _base_to_pair(base: str) -> str:
        return f"{base}/USDT:USDT"

    target_long_pairs = {_base_to_pair(b) for b in target_long}
    target_short_pairs = {_base_to_pair(b) for b in target_short}

    exited_long = current_long - target_long_pairs
    exited_short = current_short - target_short_pairs
    new_long = target_long_pairs - current_long if not entries_blocked else set()
    new_short = target_short_pairs - current_short if not entries_blocked else set()

    leg_equity = total_equity * cfg["portfolio"]["weights"]["2a"]

    # --- 1단계: 청산 전부 ---
    # ★ 2026-08-17 사용자 발견 대응: realized_pnl_usd가 여태 0.0으로
    # 하드코딩돼 있었다(실제로 청산이 있었던 8/16 사이클도 텔레그램/DB에
    # "$0.00"으로 잘못 표시됨, 실측 재구성 결과 그 사이클만 +$15.99였음).
    # 청산 체결가(result.avg_fill_price, 진입 경로와 동일하게 None 폴백)와
    # 원래 진입가로 가격 기준 실현손익을 직접 계산해 누적한다 - 수수료는
    # place_order()의 OrderResult에 없어 제외(다른 realized PnL 보고
    # 지점, 예: filtered_trend 텔레그램 r_multiple도 동일하게 가격 기준만
    # 씀 - 새 계산방식 아님, 기존 관례 그대로).
    realized_pnl_usd = 0.0
    for symbol in sorted(exited_long | exited_short):
        pos = next(p for p in current_positions if p["symbol"] == symbol)
        capped_qty = _verify_and_cap_close_qty(exec_exchange, symbol, pos["direction"], pos["qty"])
        if capped_qty <= 0:
            _log_stderr(f"2a 리밸런스({ts}): {symbol} 거래소에 이미 포지션 없음 - 청산 주문 스킵, DB만 정리")
            live_state.delete_position(db_path, symbol, "2a")
            executor.cancel_stop_orders(exec_exchange, symbol)
            continue
        close_direction = "short" if pos["direction"] == "long" else "long"
        result = executor.place_order(exec_exchange, db_path, symbol, close_direction, capped_qty, reduce_only=True)
        if result.status == "filled":
            exit_price = result.avg_fill_price
            if exit_price is None:
                exit_price = exec_exchange.fetch_ticker(symbol)["last"]
            sign = 1.0 if pos["direction"] == "long" else -1.0
            realized_pnl_usd += sign * (exit_price - pos["entry_price"]) * result.filled_qty
            live_state.delete_position(db_path, symbol, "2a")
            executor.cancel_stop_orders(exec_exchange, symbol)

    # --- 2단계: 신규진입 전부 ---
    for symbol, direction in [(s, "long") for s in sorted(new_long)] + [(s, "short") for s in sorted(new_short)]:
        step, minq = _qty_step_and_min(exec_exchange, symbol)
        ticker = exec_exchange.fetch_ticker(symbol)
        entry_price = entry_fill_price(ticker["last"], direction, cfg["costs"]["slippage_pct"], "market")
        notional = leg_equity * weight
        qty = notional / entry_price
        qty = max(0.0, (qty // step) * step) if step else qty
        if qty < minq:
            continue
        result = executor.place_order(exec_exchange, db_path, symbol, direction, qty)
        if result.status != "filled":
            continue
        entry_time = ts.isoformat()
        catastrophic_stop = entry_price * (1 - CATASTROPHIC_STOP_PCT if direction == "long" else 1 + CATASTROPHIC_STOP_PCT)
        live_state.save_position(db_path, symbol, "2a", direction, qty, entry_price, entry_time, catastrophic_stop)
        stop_result = executor.ensure_stop_placed(exec_exchange, db_path, symbol,
                                                    SimpleNamespace(direction=direction, qty=qty, current_stop=catastrophic_stop))
        _handle_stop_result(db_path, symbol, "2a", direction, stop_result)

    turnover_pct = 100.0 * (len(exited_long) + len(exited_short) + len(new_long) + len(new_short)) / max(1, len(target_long_pairs) + len(target_short_pairs))
    live_state.save_rebalance_log(
        db_path, ts.isoformat(),
        new_long=sorted(new_long), new_short=sorted(new_short),
        exited_long=sorted(exited_long), exited_short=sorted(exited_short),
        turnover_pct=turnover_pct, realized_pnl_usd=realized_pnl_usd,
    )
    telegram.send_rebalance_notification(
        new_long=sorted(new_long), new_short=sorted(new_short),
        exited_long=sorted(exited_long), exited_short=sorted(exited_short),
        turnover_pct=turnover_pct, realized_pnl_usd=realized_pnl_usd,
    )


def _qty_step_and_min(exchange, symbol: str) -> tuple[float, float]:
    market = exchange.market(symbol)
    return market["precision"]["amount"], market["limits"]["amount"]["min"]


def _compute_bars_held(entry_time: pd.Timestamp, ts: pd.Timestamp) -> int:
    """engine.py의 Position.bars_held와 동일 정의(진입 이후 관리 호출
    횟수) — 컬럼으로 저장하지 않고 entry_time에서 매 틱 계산한다(상태
    이중관리 방지, 재시작에도 안전). 백테스트 루프에서 bars_held는 진입
    봉 자체에서는 0(관리 호출 안 됨)이고, 그다음 봉부터 관리 호출마다
    1씩 늘어난다 — 즉 진입 이후 경과한 완결 15분 봉 수와 정확히 같다."""
    return int((ts - entry_time) / pd.Timedelta(minutes=15))


def _reconstruct_filtered_trend_position(pos: dict, ts: pd.Timestamp) -> Position:
    """state.py에 저장된 행 -> engine.py의 Position 데이터클래스. strategy
    필드는 state.py의 부기 라벨("filtered_trend")이 아니라 filtered_trend.py
    ::run()이 쓰는 값("trend")을 그대로 쓴다 — Position.strategy는 Trade
    레코드 라벨용으로 다른 이름공간(EXIT_LOGIC_COVERAGE.md에 명시)."""
    entry_time = pd.Timestamp(pos["entry_time"])
    initial_stop = pos["initial_stop"]
    return Position(
        symbol=pos["symbol"], strategy="trend", direction=pos["direction"],
        entry_time=entry_time, entry_price=pos["entry_price"], qty=pos["qty"],
        initial_stop=initial_stop, current_stop=pos["current_stop"],
        initial_stop_distance=abs(pos["entry_price"] - initial_stop),
        target_price=None, partial_taken=bool(pos["partial_taken"]),
        bars_held=_compute_bars_held(entry_time, ts),
        entry_fee_usd=pos["entry_fee_usd"], funding_paid_usd=pos["funding_paid_usd"],
    )


def _process_filtered_trend_tick(cfg: dict, db_path: Path, data_exchange, exec_exchange, ts: pd.Timestamp,
                                  total_equity: float, entries_blocked: bool) -> None:
    """filtered_trend 처리. 청산 관리는 engine.py의 _manage_trend_position()을
    그대로 호출한다(부분익절/트레일링/시간청산/스탑로스 전부 포함 — 재해석
    없음, EXIT_LOGIC_COVERAGE.md). 신규진입은 strategies/filtered_trend.py와
    동일한 신호 함수로 판단."""
    account_cfg = cfg["account"]
    trend_cfg, cost_cfg = cfg["trend"], cfg["costs"]
    project_root = Path(__file__).resolve().parent.parent.parent
    ohlcv_dir = project_root / cfg["data"]["ohlcv_cache_dir"]
    funding_dir = project_root / cfg["data"]["funding_cache_dir"]
    since_ms = int((ts - pd.Timedelta(days=DATA_LOOKBACK_DAYS_15M)).timestamp() * 1000)
    update_ohlcv_cache(data_exchange, FT_SYMBOL, "15m", since_ms, ohlcv_dir)
    update_ohlcv_cache(data_exchange, FT_SYMBOL, "1h", since_ms, ohlcv_dir)
    update_funding_cache(data_exchange, FT_SYMBOL, since_ms, funding_dir)

    df_15m = _load_and_index(cache_path(ohlcv_dir, FT_SYMBOL, "15m"))
    df_1h = _load_and_index(cache_path(ohlcv_dir, FT_SYMBOL, "1h"))
    funding_df = load_cache(cache_path(funding_dir, FT_SYMBOL, "funding"))
    if df_15m is None or df_1h is None or df_15m.empty or funding_df is None:
        _log_stderr("filtered_trend: 캐시 데이터 없음 - 이번 틱 스킵")
        return
    # 캐시는 2023년부터의 전체 백테스트 이력을 공유한다(같은 parquet 경로) —
    # 매 15분 틱마다 지표를 전체 이력에 대해 재계산하면 이력이 늘수록 느려진다.
    # 워밍업에 필요한 최근 구간만 잘라 쓴다(DATA_LOOKBACK_DAYS_15M, 지표 계산
    # 함수 자체는 그대로 - 입력 윈도만 좁힘, 수치에 영향 없음).
    window_start = ts - pd.Timedelta(days=DATA_LOOKBACK_DAYS_15M)
    df_15m = df_15m[df_15m.index >= window_start]
    df_1h = df_1h[df_1h.index >= window_start]

    positions = live_state.load_open_positions(db_path, strategy="filtered_trend")
    latest_bar = df_15m.iloc[-1]

    if positions:
        _manage_open_filtered_trend_position(db_path, exec_exchange, positions[0], df_15m, df_1h, funding_df,
                                              trend_cfg, cost_cfg, ts, latest_bar, total_equity, account_cfg)
        return  # 포지션이 있으면(청산했든 아니든) 이번 틱엔 신규진입 평가 안 함

    if entries_blocked:
        _log_stderr(f"filtered_trend 진단({ts}): entries_blocked=True(일일손실한도) - 신규진입 평가 스킵")
        return
    if not can_open_new_position(set(), FT_SYMBOL, account_cfg["max_concurrent_positions"]):
        _log_stderr(f"filtered_trend 진단({ts}): can_open_new_position=False - 신규진입 스킵")
        return

    regime_cfg = cfg["regime"]
    regime_1h = classify_regime(df_1h, regime_cfg)
    regime_15m = regime_1h.reindex(df_15m.index, method="ffill")
    current_regime = regime_15m.iloc[-1]
    if current_regime != TREND:
        _log_stderr(f"filtered_trend 진단({ts}): regime={current_regime}(TREND 아님) - 신규진입 평가 스킵")
        return

    trend_sig = generate_trend_signals(df_15m, trend_cfg)
    daily_close = df_15m["close"].resample("1D").last()
    daily_roc = daily_close.pct_change(MOMENTUM_LOOKBACK_DAYS)
    roc_15m = daily_roc.reindex(df_15m.index, method="ffill")

    trow = trend_sig.iloc[-1]
    direction = "long" if bool(trow["entry_long"]) else ("short" if bool(trow["entry_short"]) else None)
    current_roc = roc_15m.iloc[-1]
    # ★ 2026-08-01(사용자 발견): 2026-07-31 14:00/14:15 UTC 두 틱 모두 사후
    # 재현 시 진입조건이 전부 충족돼 있었는데도(regime=TREND, 숏 브레이크아웃
    # 확정, 모멘텀 합치, qty>0) 실제로는 주문이 안 나갔고 에러/CRITICAL도
    # 전혀 없었다 - "조용한 스킵"이라 원인을 사후에 특정할 수 없었다(로그가
    # 아예 없었으므로). 이후 각 분기(regime/방향-모멘텀/수량/체결)마다 스킵
    # 사유를 반드시 남긴다 - CRITICAL이 아니라 로컬 로그(runner.log)로만,
    # 알림 폭주 방지 원칙(2026-07-29)과는 별개 - 이건 텔레그램이 아니라
    # 사후진단용 로컬 기록이다.
    if direction is None:
        _log_stderr(f"filtered_trend 진단({ts}): regime=TREND, 브레이크아웃 신호 없음(entry_long/short 둘 다 False)")
        return
    if not _momentum_agrees(direction, current_roc):
        _log_stderr(f"filtered_trend 진단({ts}): regime=TREND, direction={direction} 신호 있으나 모멘텀 불일치(roc={current_roc}) - 진입 스킵")
        return

    initial_stop = trow["initial_stop_long"] if direction == "long" else trow["initial_stop_short"]
    entry_price = entry_fill_price(latest_bar["close"], direction, cost_cfg["slippage_pct"], "market")
    leg_equity = total_equity * cfg["portfolio"]["weights"]["filtered_trend"]
    step, minq = _qty_step_and_min(exec_exchange, FT_SYMBOL)
    qty = position_size(leg_equity, entry_price, initial_stop, account_cfg["risk_per_trade_pct"],
                         max_leverage=account_cfg["leverage"], qty_step=step, min_qty=minq)
    if qty <= 0:
        _log_stderr(
            f"filtered_trend 진단({ts}): regime=TREND, direction={direction}, 모멘텀 일치, "
            f"qty<=0({qty}) - 진입 스킵(step={step}, min={minq}, leg_equity={leg_equity:.2f})"
        )
        return

    _log_stderr(f"filtered_trend 진단({ts}): regime=TREND, direction={direction}, qty={qty} - 주문 시도")
    result = executor.place_order(exec_exchange, db_path, FT_SYMBOL, direction, qty)
    if result.status != "filled":
        _log_stderr(f"filtered_trend 진단({ts}): 주문 시도했으나 체결 안 됨(status={result.status}) - 포지션 미생성")
        return
    real_entry_price = result.avg_fill_price or entry_price
    notional = qty * real_entry_price
    entry_fee_usd = taker_fee(notional, cost_cfg["taker_fee_pct"])  # filtered_trend.py::run()의 진입 수수료 계산과 동일
    live_state.save_position(db_path, FT_SYMBOL, "filtered_trend", direction, qty,
                              real_entry_price, ts.isoformat(), initial_stop,
                              initial_stop=initial_stop, entry_fee_usd=entry_fee_usd,
                              funding_paid_usd=0.0, partial_taken=False)
    stop_result = executor.ensure_stop_placed(exec_exchange, db_path, FT_SYMBOL,
                                               SimpleNamespace(direction=direction, qty=qty, current_stop=initial_stop))
    if stop_result.closed_instead:
        # 스탑조건이 진입 직후 이미 충족돼 즉시 청산됨 - 방금 저장한 진입 상태를
        # _handle_stop_result가 삭제하고 별도 CRITICAL로 알린다. 정상 진입인 것처럼
        # entry 알림을 보내면(포지션은 이미 없는데) 오해를 부르므로 여기서 막는다.
        _handle_stop_result(db_path, FT_SYMBOL, "filtered_trend", direction, stop_result)
        return
    telegram.send_entry_exit_notification(
        symbol=FT_SYMBOL, strategy="filtered_trend", direction=direction, regime="trend",
        qty=qty, price=real_entry_price, r_multiple=None,
        cumulative_pnl_usd=total_equity - account_cfg["initial_equity_usd"],
    )


def _manage_open_filtered_trend_position(db_path: Path, exec_exchange, pos: dict, df_15m: pd.DataFrame,
                                          df_1h: pd.DataFrame, funding_df: pd.DataFrame,
                                          trend_cfg: dict, cost_cfg: dict, ts: pd.Timestamp, latest_bar: pd.Series,
                                          total_equity: float, account_cfg: dict) -> None:
    """이미 열린 filtered_trend 포지션 1틱 관리 — engine.py::_manage_trend_position()을
    그대로 호출(부분익절/트레일링/시간청산/스탑로스, 재해석 없음). 실제
    청산 주문은 반환된 Trade별로 개별 실행하고(부분익절 후 같은 틱에서
    시간청산까지 겹치면 Trade가 2건일 수 있음, engine.py 로직 그대로),
    telegram에는 시뮬레이션 가격이 아니라 실제 체결가를 보고한다(_manage_
    trend_position의 결정 로직은 그대로, 보고되는 숫자만 실측으로 교체).

    ★ 상태 갱신은 "전부 체결됐을 때만" 한다 — 청산 주문이 미체결이면
    _manage_trend_position이 "이렇게 됐어야 한다"고 계산한 결과(qty 축소/
    포지션 삭제 등)를 실제로 일어나지 않은 일처럼 state에 반영하지 않는다
    (gate_verify.py의 공허한 PASS 버그와 같은 종류의 실수 반복 금지 —
    이번 태스크 이전 세션에서 이미 한 번 지적됐던 원칙). 미체결 시 포지션
    행은 건드리지 않고(직전 상태 그대로 유지) CRITICAL만 발신, 다음 틱에서
    재시도된다.

    ★ 알려진 한계: 한 틱에서 Trade가 2건(예: 부분익절 성공 직후 같은 틱에서
    시간청산까지 트리거) 나올 때 첫 번째만 체결되고 두 번째가 실패하면,
    실제로는 부분청산이 일어났는데도 state는 갱신하지 않는다(all-or-nothing
    단순화) — 다음 틱에서 같은 부분익절 조건이 다시 트리거돼 중복 청산
    시도가 발생할 수 있다. 두 Trade가 같은 틱에 겹치는 경우 자체가 드물고
    (부분익절 임계치와 시간청산 임계치가 같은 봉에서 동시 충족돼야 함),
    완전한 해결은 트레이드 단위로 state를 즉시 갱신하는 재설계가 필요해
    이번 범위 밖으로 남긴다."""
    position = _reconstruct_filtered_trend_position(pos, ts)
    trend_row = pd.Series({"chandelier_stop": trailing_stop(df_15m, trend_cfg, position.direction).iloc[-1]})

    new_position, trades, _simulated_realized = _manage_trend_position(position, latest_bar, trend_row, trend_cfg, cost_cfg)

    close_direction = "short" if position.direction == "long" else "long"
    all_filled = True
    for trade in trades:
        # ★ 2026-08-11 ETH phantom long 사고 대응(reports/G4_ETH_MANAGEMENT_
        # PHANTOM_LONG_20260811.md): DB만 믿고 청산 주문을 내면, 그 사이
        # 거래소 스탑이 이미 포지션을 정리해버린 경우 이 "청산" 주문이
        # 실제로는 신규진입이 돼버린다(실사고, 13시간 49분 무보호 롱 노출).
        # 주문 전 반드시 거래소 실측으로 재확인·수량 보정한다.
        capped_qty = _verify_and_cap_close_qty(exec_exchange, FT_SYMBOL, position.direction, trade.qty)
        if capped_qty <= 0:
            _log_stderr(
                f"filtered_trend 관리({ts}): 거래소에 이미 포지션 없음({trade.exit_reason}) - "
                f"청산 주문 스킵, DB 정리만"
            )
            telegram.send_critical_alert(
                f"filtered_trend {FT_SYMBOL} 포지션이 DB엔 남아있었지만 거래소엔 이미 없었음"
                f"(사유: {trade.exit_reason}) - 청산 주문 없이 상태만 정리함"
            )
            live_state.delete_position(db_path, FT_SYMBOL, "filtered_trend")
            executor.cancel_stop_orders(exec_exchange, FT_SYMBOL)
            return
        result = executor.place_order(exec_exchange, db_path, FT_SYMBOL, close_direction, capped_qty, reduce_only=True)
        if result.status != "filled":
            all_filled = False
            telegram.send_critical_alert(
                f"filtered_trend 청산 주문({trade.exit_reason}) 미체결 - 수동 확인 필요: {FT_SYMBOL} qty={capped_qty}"
            )
            continue
        # 2026-07-27 사고 대응: market 주문의 create_order 응답이 average(체결평균가)를
        # None으로 반환하는 경우가 실측으로 확인됨(ccxt/바이낸스 타이밍 특성, 실제로는
        # 정상 체결됨) - line 459(진입 경로)와 동일하게 None 가드 필요. 진입 경로는
        # 시뮬레이션 entry_price로 폴백하지만 청산 경로엔 그런 값이 없으므로, 사용자
        # 지시대로 거래소 현재가(mark price) 재조회로 폴백한다. 이 가드가 없으면
        # _current_r_multiple()이 TypeError를 던지고, 그 크래시가 place_order(주문은
        # 이미 실체결됨) 이후·save_position(상태 영속화) 이전에 발생해 다음 틱에
        # 같은 청산이 중복 실행되는 사고로 이어졌다(reports/G4_KILLSWITCH_INCIDENT_ANALYSIS.md).
        real_fill_price = result.avg_fill_price
        if real_fill_price is None:
            real_fill_price = exec_exchange.fetch_ticker(FT_SYMBOL)["last"]
        real_r_multiple = _current_r_multiple(position, real_fill_price)
        telegram.send_entry_exit_notification(
            symbol=FT_SYMBOL, strategy="filtered_trend", direction=position.direction, regime="trend",
            qty=trade.qty, price=real_fill_price, r_multiple=real_r_multiple,
            cumulative_pnl_usd=total_equity - account_cfg["initial_equity_usd"],
        )

    if not all_filled:
        return  # 상태 미갱신 - 다음 틱에서 _manage_trend_position이 같은 조건을 다시 평가해 재시도

    if new_position is None:
        live_state.delete_position(db_path, FT_SYMBOL, "filtered_trend")
        executor.cancel_stop_orders(exec_exchange, FT_SYMBOL)  # 2026-08-11: 완전청산 시 잔여 스탑 정리
        return

    symbol_data = SymbolData(df_15m=df_15m, df_1h=df_1h, funding=funding_df)
    ts_ms = int(ts.value // 1_000_000)
    _apply_funding(new_position, symbol_data, ts_ms, cost_cfg)

    live_state.save_position(
        db_path, FT_SYMBOL, "filtered_trend", new_position.direction, new_position.qty,
        new_position.entry_price, new_position.entry_time.isoformat(), new_position.current_stop,
        initial_stop=new_position.initial_stop, entry_fee_usd=new_position.entry_fee_usd,
        funding_paid_usd=new_position.funding_paid_usd, partial_taken=new_position.partial_taken,
    )
    stop_result = executor.ensure_stop_placed(exec_exchange, db_path, FT_SYMBOL,
                                               SimpleNamespace(direction=new_position.direction, qty=new_position.qty,
                                                                current_stop=new_position.current_stop))
    _handle_stop_result(db_path, FT_SYMBOL, "filtered_trend", new_position.direction, stop_result)


def _load_and_index(path: Path):
    df = load_cache(path)
    if df is None or df.empty:
        return df
    df = df.copy()
    df.index = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.drop(columns=["timestamp"])


def _check_daily_loss_limit(cfg: dict, db_path: Path, exec_exchange, ts: pd.Timestamp) -> bool:
    """공개 헬퍼(테스트/외부 호출용) — 내부적으로는 `_tick()`이
    `_get_daily_pnl_and_equity()` + `daily_loss_limit_breached()`를 직접
    호출한다(이 함수는 그 조합을 감싼 얇은 래퍼, 골격 시그니처 호환용)."""
    daily_pnl, total_equity = _get_daily_pnl_and_equity(cfg, db_path, exec_exchange, ts)
    return daily_loss_limit_breached(daily_pnl, total_equity, cfg["account"]["daily_loss_limit_pct"])
