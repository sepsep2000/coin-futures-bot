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

★ 스켈레톤 대비 명시적 스코프 축소(사전 보고, 반드시 후속 과제 필요):
filtered_trend의 라이브 포지션 관리는 **스탑로스 청산만** 구현한다.
`_manage_trend_position`(engine.py)의 부분익절(partial_tp)·트레일링
스탑·시간청산(time_stop) 로직은 `state.py`의 `positions` 스키마가
`initial_stop`/`bars_held`/`partial_taken`/`funding_paid_usd` 등을
저장하지 않아(설계 당시 확정된 스키마, 이번 태스크 범위에서 임의로
바꾸지 않음) 그대로 재사용할 수 없다 — 이 세 경로를 라이브에 붙이려면
스키마 확장이 선행돼야 한다. 즉시 스탑로스 청산은 과거 실측상 청산의
대다수(68.1%, engine.py `_manage_trend_position` docstring 인용)를
차지해 가장 중요한 안전장치는 구현돼 있지만, **부분익절/트레일링/시간
청산이 빠진 채로는 백테스트가 검증한 것과 라이브 실제 동작이 정확히
같지 않다** — G4(페이퍼 트레이딩) 착수 전 반드시 해소해야 할 간극으로
남긴다(PAPER_TRADING_REMAINING_WORK.md에 추가 필요).

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
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pandas as pd

from src.backtest.cost_model import entry_fill_price
from src.backtest.engine import _stop_hit
from src.data.feed import cache_path, get_exchange, load_cache, update_funding_cache, update_ohlcv_cache
from src.live import executor
from src.live import state as live_state
from src.notify import telegram
from src.risk import can_open_new_position, daily_loss_limit_breached, position_size
from src.strategy.regime import TREND, classify_regime
from src.strategy.trend import generate_trend_signals

strategy_2a = importlib.import_module("strategies.2a")  # "2a"는 식별자로 import 불가 (portfolio_combined.py와 동일 관례)
from strategies.filtered_trend import MOMENTUM_LOOKBACK_DAYS, SYMBOL as FT_SYMBOL, _momentum_agrees  # noqa: E402

CATASTROPHIC_STOP_PCT = 0.30  # 2a 백스톱 — 전략 파라미터 아님, 운영 안전망(모듈 docstring 참조)
DATA_LOOKBACK_DAYS_15M = 30  # ATR/EMA/BB/chandelier 지표 워밍업에 충분한 여유(가장 긴 lookback: bb_width_percentile_window=120h=5일)


def _log_stderr(message: str) -> None:
    print(f"[SCHEDULER] {message}", file=sys.stderr)


def run_live_loop(cfg: dict, db_path: Path, max_iterations: Optional[int] = None) -> None:
    """메인 진입점.

    ★ max_iterations: 골격에 없던 파라미터(사전 보고, 최소 확장) — 무한
    루프를 자동 테스트/통합 테스트에서 유한 횟수로 끝내려면 필요하다.
    None(기본값)이면 무한 루프(실제 운영), 정수면 그 횟수만큼 틱을 처리한
    뒤 정상 반환(테스트 전용)."""
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
    while max_iterations is None or iteration < max_iterations:
        ts = pd.Timestamp.now(tz="UTC").floor("15min")
        try:
            _tick(cfg, db_path, data_exchange, exec_exchange, ts)
        except Exception as exc:  # noqa: BLE001 - 한 틱의 예외로 루프 전체가 죽으면 안 됨(다음 틱에 재시도)
            _log_stderr(f"_tick 실패({ts}): {type(exc).__name__}: {exc}")
            telegram.send_critical_alert(f"틱 처리 중 예외 발생({ts}): {type(exc).__name__}: {exc}")
        iteration += 1
        if max_iterations is None or iteration < max_iterations:
            _sleep_until_next_tick(ts)


def _sleep_until_next_tick(ts: pd.Timestamp) -> None:
    import time

    next_tick = ts + pd.Timedelta(minutes=15)
    wait_sec = max(0.0, (next_tick - pd.Timestamp.now(tz="UTC")).total_seconds())
    time.sleep(wait_sec)


def _tick(cfg: dict, db_path: Path, data_exchange, exec_exchange, ts: pd.Timestamp) -> None:
    """15m 틱 1회 처리 — 순서는 모듈 docstring 및
    reports/REBALANCE_ORDER_ANALYSIS.md 3절 확정 순서 그대로."""
    for pos in live_state.load_open_positions(db_path):
        position = SimpleNamespace(direction=pos["direction"], qty=pos["qty"], current_stop=pos["current_stop"])
        executor.ensure_stop_placed(exec_exchange, db_path, pos["symbol"], position)

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
    for symbol in sorted(exited_long | exited_short):
        pos = next(p for p in current_positions if p["symbol"] == symbol)
        close_direction = "short" if pos["direction"] == "long" else "long"
        result = executor.place_order(exec_exchange, db_path, symbol, close_direction, pos["qty"])
        if result.status == "filled":
            live_state.delete_position(db_path, symbol, "2a")

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
        executor.ensure_stop_placed(exec_exchange, db_path, symbol, SimpleNamespace(direction=direction, qty=qty, current_stop=catastrophic_stop))

    turnover_pct = 100.0 * (len(exited_long) + len(exited_short) + len(new_long) + len(new_short)) / max(1, len(target_long_pairs) + len(target_short_pairs))
    live_state.save_rebalance_log(
        db_path, ts.isoformat(),
        new_long=sorted(new_long), new_short=sorted(new_short),
        exited_long=sorted(exited_long), exited_short=sorted(exited_short),
        turnover_pct=turnover_pct, realized_pnl_usd=0.0,  # 실현손익 계산은 trades 원장이 없어 이번 범위 밖(모듈 docstring 참조)
    )
    telegram.send_rebalance_notification(
        new_long=sorted(new_long), new_short=sorted(new_short),
        exited_long=sorted(exited_long), exited_short=sorted(exited_short),
        turnover_pct=turnover_pct, realized_pnl_usd=0.0,
    )


def _qty_step_and_min(exchange, symbol: str) -> tuple[float, float]:
    market = exchange.market(symbol)
    return market["precision"]["amount"], market["limits"]["amount"]["min"]


def _process_filtered_trend_tick(cfg: dict, db_path: Path, data_exchange, exec_exchange, ts: pd.Timestamp,
                                  total_equity: float, entries_blocked: bool) -> None:
    """filtered_trend 처리. 스코프(모듈 docstring 참조): 청산은 스탑로스만,
    신규진입은 strategies/filtered_trend.py와 동일한 신호 함수로 판단."""
    account_cfg = cfg["account"]
    project_root = Path(__file__).resolve().parent.parent.parent
    ohlcv_dir = project_root / cfg["data"]["ohlcv_cache_dir"]
    since_ms = int((ts - pd.Timedelta(days=DATA_LOOKBACK_DAYS_15M)).timestamp() * 1000)
    update_ohlcv_cache(data_exchange, FT_SYMBOL, "15m", since_ms, ohlcv_dir)
    update_ohlcv_cache(data_exchange, FT_SYMBOL, "1h", since_ms, ohlcv_dir)

    df_15m = _load_and_index(cache_path(ohlcv_dir, FT_SYMBOL, "15m"))
    df_1h = _load_and_index(cache_path(ohlcv_dir, FT_SYMBOL, "1h"))
    if df_15m is None or df_1h is None or df_15m.empty:
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
        pos = positions[0]
        stub_position = SimpleNamespace(direction=pos["direction"], current_stop=pos["current_stop"])
        if _stop_hit(stub_position, latest_bar):
            close_direction = "short" if pos["direction"] == "long" else "long"
            result = executor.place_order(exec_exchange, db_path, FT_SYMBOL, close_direction, pos["qty"])
            if result.status == "filled":
                live_state.delete_position(db_path, FT_SYMBOL, "filtered_trend")
                telegram.send_entry_exit_notification(
                    symbol=FT_SYMBOL, strategy="filtered_trend", direction=pos["direction"], regime="trend",
                    qty=pos["qty"], price=result.avg_fill_price or latest_bar["close"],
                    r_multiple=None,  # initial_stop 미보존(스코프 축소, 모듈 docstring) - R배수 계산 불가
                    cumulative_pnl_usd=total_equity - account_cfg["initial_equity_usd"],
                )
        return  # 포지션이 있으면(청산했든 안 했든) 이번 틱엔 신규진입 평가 안 함

    if entries_blocked:
        return
    if not can_open_new_position(set(), FT_SYMBOL, account_cfg["max_concurrent_positions"]):
        return

    regime_cfg, trend_cfg = cfg["regime"], cfg["trend"]
    regime_1h = classify_regime(df_1h, regime_cfg)
    regime_15m = regime_1h.reindex(df_15m.index, method="ffill")
    if regime_15m.iloc[-1] != TREND:
        return

    trend_sig = generate_trend_signals(df_15m, trend_cfg)
    daily_close = df_15m["close"].resample("1D").last()
    daily_roc = daily_close.pct_change(MOMENTUM_LOOKBACK_DAYS)
    roc_15m = daily_roc.reindex(df_15m.index, method="ffill")

    trow = trend_sig.iloc[-1]
    direction = "long" if bool(trow["entry_long"]) else ("short" if bool(trow["entry_short"]) else None)
    if direction is None or not _momentum_agrees(direction, roc_15m.iloc[-1]):
        return

    initial_stop = trow["initial_stop_long"] if direction == "long" else trow["initial_stop_short"]
    entry_price = entry_fill_price(latest_bar["close"], direction, cfg["costs"]["slippage_pct"], "market")
    leg_equity = total_equity * cfg["portfolio"]["weights"]["filtered_trend"]
    step, minq = _qty_step_and_min(exec_exchange, FT_SYMBOL)
    qty = position_size(leg_equity, entry_price, initial_stop, account_cfg["risk_per_trade_pct"],
                         max_leverage=account_cfg["leverage"], qty_step=step, min_qty=minq)
    if qty <= 0:
        return

    result = executor.place_order(exec_exchange, db_path, FT_SYMBOL, direction, qty)
    if result.status != "filled":
        return
    live_state.save_position(db_path, FT_SYMBOL, "filtered_trend", direction, qty,
                              result.avg_fill_price or entry_price, ts.isoformat(), initial_stop)
    executor.ensure_stop_placed(exec_exchange, db_path, FT_SYMBOL,
                                 SimpleNamespace(direction=direction, qty=qty, current_stop=initial_stop))
    telegram.send_entry_exit_notification(
        symbol=FT_SYMBOL, strategy="filtered_trend", direction=direction, regime="trend",
        qty=qty, price=result.avg_fill_price or entry_price, r_multiple=None,
        cumulative_pnl_usd=total_equity - account_cfg["initial_equity_usd"],
    )


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
