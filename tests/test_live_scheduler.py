"""tests/test_live_scheduler.py — src/live/scheduler.py 단위 테스트.

`_tick()`이 하는 "언제 무엇을 부를지" 결정(순서/분기)만 검증한다 —
`_process_2a_rebalance`/`_process_filtered_trend_tick`의 내부 신호계산·
주문실행은 이 파일의 범위가 아니다(네트워크 필요, 실거래소 형식 의존이
커서 tests/test_live_scheduler_integration.py에서 실제 testnet으로
검증한다). 여기서는 오케스트레이션 함수들을 monkeypatch로 스텁해
호출 순서/인자만 확인한다(네트워크 없음)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from src.live import scheduler
from src.live.state import init_db, load_open_positions, save_equity_snapshot, save_order, save_position

CFG = {"account": {"daily_loss_limit_pct": -3.0}}

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REAL_CFG = yaml.safe_load((PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
TREND_CFG = _REAL_CFG["trend"]
COST_CFG = _REAL_CFG["costs"]


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "state.db"
    init_db(path)
    return path


class _StubBalanceExchange:
    def __init__(self, balance_usdt: float = 10000.0):
        self.balance_usdt = balance_usdt

    def fetch_balance(self):
        return {"total": {"USDT": self.balance_usdt}}


class _StubCancelExchange:
    def __init__(self):
        self.cancelled: list[str] = []

    def cancel_order(self, order_id, symbol):
        self.cancelled.append(order_id)
        return {"id": order_id, "status": "canceled"}


class _StubReconciledExchange:
    def __init__(self):
        self.markets_loaded = False

    def load_markets(self):
        self.markets_loaded = True

    def fetch_positions(self):
        return []

    def fetch_open_orders(self):
        return []


# =====================================================================
# _is_2a_rebalance_tick — pandas resample("W") 경계(일요일 00:00 UTC)
# =====================================================================

def test_is_2a_rebalance_tick_sunday_midnight_true():
    assert scheduler._is_2a_rebalance_tick(pd.Timestamp("2026-07-19 00:00:00", tz="UTC")) is True


def test_is_2a_rebalance_tick_monday_midnight_false():
    """골격 최초 작성 시의 오기(월요일)가 재발하지 않는지 확인하는 회귀 테스트."""
    assert scheduler._is_2a_rebalance_tick(pd.Timestamp("2026-07-20 00:00:00", tz="UTC")) is False


def test_is_2a_rebalance_tick_sunday_not_midnight_false():
    assert scheduler._is_2a_rebalance_tick(pd.Timestamp("2026-07-19 00:15:00", tz="UTC")) is False


# =====================================================================
# _get_daily_pnl_and_equity
# =====================================================================

def test_get_daily_pnl_no_snapshot_today_returns_zero_baseline(db_path):
    """당일 첫 스냅샷이 아직 없으면 daily_pnl=0(기준점 없음, 모듈 docstring
    한계 인지) — 자본 조회 자체는 정상 수행."""
    exchange = _StubBalanceExchange(balance_usdt=10500.0)
    daily_pnl, total_equity = scheduler._get_daily_pnl_and_equity(
        {}, db_path, exchange, pd.Timestamp("2026-07-20 03:00:00", tz="UTC")
    )
    assert daily_pnl == 0.0
    assert total_equity == 10500.0


def test_get_daily_pnl_compares_against_first_snapshot_today(db_path):
    save_equity_snapshot(db_path, pd.Timestamp("2026-07-20 00:00:00", tz="UTC").isoformat(), 10000.0, 10000.0, 10000.0)
    exchange = _StubBalanceExchange(balance_usdt=9600.0)
    daily_pnl, total_equity = scheduler._get_daily_pnl_and_equity(
        {}, db_path, exchange, pd.Timestamp("2026-07-20 06:00:00", tz="UTC")
    )
    assert daily_pnl == pytest.approx(-400.0)
    assert total_equity == 9600.0


# =====================================================================
# _handle_daily_loss_limit_breach
# =====================================================================

def test_handle_breach_cancels_only_new_entry_orders(db_path, monkeypatch):
    """기존 포지션이 있는 심볼의 미체결 주문(스탑/청산류로 간주)은 건드리지
    않고, 포지션이 없는 심볼의 미체결 주문(신규진입으로 간주)만 취소한다."""
    save_position(db_path, "ETH/USDT:USDT", "filtered_trend", "long", 1.0, 3000.0, "t", 2900.0)
    save_order(db_path, "ORD_EXISTING_SYMBOL", "ETH/USDT:USDT", "sell", 1.0, None, "pending")
    save_order(db_path, "ORD_NEW_ENTRY", "BTC/USDT:USDT", "buy", 0.01, None, "pending")

    sent = []
    monkeypatch.setattr(scheduler.telegram, "send_critical_alert", lambda msg: sent.append(msg) or True)
    exchange = _StubCancelExchange()

    scheduler._handle_daily_loss_limit_breach(
        db_path, exchange, daily_pnl=-350.0, total_equity=10000.0,
        ts=pd.Timestamp("2026-07-20 03:00:00", tz="UTC"), daily_loss_limit_pct=-3.0,
    )

    assert exchange.cancelled == ["ORD_NEW_ENTRY"]
    assert len(sent) == 1


def test_handle_breach_notifies_only_once_per_day(db_path, monkeypatch):
    """알림 피로 방지 — 오늘 이미 한도를 넘긴 스냅샷이 있으면 재통보하지 않는다."""
    save_equity_snapshot(db_path, pd.Timestamp("2026-07-20 00:00:00", tz="UTC").isoformat(), 10000.0, 10000.0, 10000.0)
    save_equity_snapshot(db_path, pd.Timestamp("2026-07-20 00:15:00", tz="UTC").isoformat(), 9600.0, 9600.0, 9600.0)

    sent = []
    monkeypatch.setattr(scheduler.telegram, "send_critical_alert", lambda msg: sent.append(msg) or True)
    exchange = _StubCancelExchange()

    scheduler._handle_daily_loss_limit_breach(
        db_path, exchange, daily_pnl=-410.0, total_equity=9590.0,
        ts=pd.Timestamp("2026-07-20 00:30:00", tz="UTC"), daily_loss_limit_pct=-3.0,
    )

    assert sent == []


# =====================================================================
# _tick — 오케스트레이션 순서/분기 (핵심 요구사항)
# =====================================================================

def test_tick_ensure_stop_sweep_runs_before_2a_and_filtered_trend(db_path, monkeypatch):
    """★ 두 신호(2a 리밸런스 + filtered_trend) 동시 발생 케이스: 이 틱이
    2a 리밸런스 경계라고 강제하고, 실제 호출 순서가 REBALANCE_ORDER_
    ANALYSIS.md 확정 순서(스탑 스윕 -> 2a -> filtered_trend)와 일치하는지
    확인한다."""
    save_position(db_path, "ETH/USDT:USDT", "filtered_trend", "long", 1.0, 3000.0, "t", 2900.0)
    calls: list[str] = []

    monkeypatch.setattr(scheduler.executor, "ensure_stop_placed",
                         lambda exchange, db, symbol, position, max_retries=3: calls.append(f"ensure_stop:{symbol}") or True)
    monkeypatch.setattr(scheduler, "_get_daily_pnl_and_equity", lambda cfg, db, ex, ts: (0.0, 10000.0))
    monkeypatch.setattr(scheduler, "_is_2a_rebalance_tick", lambda ts: True)
    monkeypatch.setattr(scheduler, "_process_2a_rebalance",
                         lambda cfg, db, dex, eex, ts, eq, blocked: calls.append("2a"))
    monkeypatch.setattr(scheduler, "_process_filtered_trend_tick",
                         lambda cfg, db, dex, eex, ts, eq, blocked: calls.append("filtered_trend"))

    scheduler._tick(CFG, db_path, object(), object(), pd.Timestamp("2026-07-19 00:00:00", tz="UTC"))

    assert calls == ["ensure_stop:ETH/USDT:USDT", "2a", "filtered_trend"]


def test_tick_non_rebalance_boundary_skips_2a(db_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(scheduler.executor, "ensure_stop_placed", lambda *a, **k: True)
    monkeypatch.setattr(scheduler, "_get_daily_pnl_and_equity", lambda cfg, db, ex, ts: (0.0, 10000.0))
    monkeypatch.setattr(scheduler, "_is_2a_rebalance_tick", lambda ts: False)
    monkeypatch.setattr(scheduler, "_process_2a_rebalance",
                         lambda cfg, db, dex, eex, ts, eq, blocked: calls.append("2a"))
    monkeypatch.setattr(scheduler, "_process_filtered_trend_tick",
                         lambda cfg, db, dex, eex, ts, eq, blocked: calls.append("filtered_trend"))

    scheduler._tick(CFG, db_path, object(), object(), pd.Timestamp("2026-07-20 03:00:00", tz="UTC"))

    assert calls == ["filtered_trend"]


def test_tick_daily_loss_limit_breach_blocks_entries_but_still_processes(db_path, monkeypatch):
    """SPEC: 당일 신규진입 금지 — 기존 포지션 관리(청산 체크 등)는 계속돼야
    하므로 _process_filtered_trend_tick 자체는 호출되되 blocked=True로
    전달돼야 한다(그 함수 내부가 신규진입만 건너뜀)."""
    calls: list[str] = []
    monkeypatch.setattr(scheduler.executor, "ensure_stop_placed", lambda *a, **k: True)
    monkeypatch.setattr(scheduler, "_get_daily_pnl_and_equity", lambda cfg, db, ex, ts: (-500.0, 10000.0))
    monkeypatch.setattr(scheduler, "_handle_daily_loss_limit_breach",
                         lambda db, ex, pnl, eq, ts, pct: calls.append("breach_handled"))
    monkeypatch.setattr(scheduler, "_is_2a_rebalance_tick", lambda ts: False)
    monkeypatch.setattr(scheduler, "_process_filtered_trend_tick",
                         lambda cfg, db, dex, eex, ts, eq, blocked: calls.append(f"ft_blocked={blocked}"))

    scheduler._tick(CFG, db_path, object(), object(), pd.Timestamp("2026-07-20 03:00:00", tz="UTC"))

    assert "breach_handled" in calls
    assert "ft_blocked=True" in calls


def test_tick_continues_when_ensure_stop_placed_fails(db_path, monkeypatch):
    """★ 요구된 실패 케이스: ensure_stop_placed가 False(실패)를 반환해도
    틱 전체가 죽지 않고 나머지 처리를 계속해야 한다 — 실패 자체는
    executor.ensure_stop_placed 내부에서 이미 CRITICAL 로그/state 기록을
    하므로(tests/test_live_executor.py에서 검증됨), 스케줄러가 또 예외를
    던지면 같은 실패를 이중으로 다루게 된다."""
    save_position(db_path, "ETH/USDT:USDT", "filtered_trend", "long", 1.0, 3000.0, "t", 2900.0)
    calls: list[str] = []

    monkeypatch.setattr(scheduler.executor, "ensure_stop_placed",
                         lambda *a, **k: calls.append("ensure_stop_failed") or False)
    monkeypatch.setattr(scheduler, "_get_daily_pnl_and_equity", lambda cfg, db, ex, ts: (0.0, 10000.0))
    monkeypatch.setattr(scheduler, "_is_2a_rebalance_tick", lambda ts: False)
    monkeypatch.setattr(scheduler, "_process_filtered_trend_tick",
                         lambda cfg, db, dex, eex, ts, eq, blocked: calls.append("ft_processed"))

    scheduler._tick(CFG, db_path, object(), object(), pd.Timestamp("2026-07-20 03:00:00", tz="UTC"))

    assert calls == ["ensure_stop_failed", "ft_processed"]


def test_tick_saves_equity_snapshot(db_path, monkeypatch):
    monkeypatch.setattr(scheduler.executor, "ensure_stop_placed", lambda *a, **k: True)
    monkeypatch.setattr(scheduler, "_get_daily_pnl_and_equity", lambda cfg, db, ex, ts: (0.0, 12345.0))
    monkeypatch.setattr(scheduler, "_is_2a_rebalance_tick", lambda ts: False)
    monkeypatch.setattr(scheduler, "_process_filtered_trend_tick", lambda *a, **k: None)

    ts = pd.Timestamp("2026-07-20 03:00:00", tz="UTC")
    scheduler._tick(CFG, db_path, object(), object(), ts)

    snapshots = scheduler.live_state.load_equity_snapshots_since(db_path, "2026-07-20T00:00:00")
    assert len(snapshots) == 1
    assert snapshots[0]["total_equity_usd"] == 12345.0


# =====================================================================
# run_live_loop
# =====================================================================

def test_run_live_loop_raises_and_alerts_on_state_mismatch(db_path, monkeypatch):
    """재시작 시 불일치 발견 -> 자동 진행 금지(예외로 중단) + CRITICAL 발신."""
    monkeypatch.setattr(scheduler, "get_exchange", lambda testnet=False: object())
    monkeypatch.setattr(scheduler.executor, "get_authenticated_exchange", lambda testnet=True: _StubReconciledExchange())
    mismatch = SimpleNamespace(positions_match=False, orders_match=True, mismatches=["포지션 불일치: BTC/USDT:USDT"])
    monkeypatch.setattr(scheduler.live_state, "recover_state", lambda db, ex: mismatch)

    sent = []
    monkeypatch.setattr(scheduler.telegram, "send_critical_alert", lambda msg: sent.append(msg) or True)

    with pytest.raises(RuntimeError):
        scheduler.run_live_loop({"mode": "testnet"}, db_path)

    assert len(sent) == 1


def test_run_live_loop_runs_bounded_iterations(db_path, monkeypatch):
    """max_iterations(테스트 전용 확장)로 무한루프 없이 유한 횟수만 실행."""
    monkeypatch.setattr(scheduler, "get_exchange", lambda testnet=False: object())
    monkeypatch.setattr(scheduler.executor, "get_authenticated_exchange", lambda testnet=True: _StubReconciledExchange())
    monkeypatch.setattr(scheduler.live_state, "recover_state",
                         lambda db, ex: SimpleNamespace(positions_match=True, orders_match=True, mismatches=[]))
    monkeypatch.setattr(scheduler, "_sleep_until_next_tick", lambda ts: None)

    calls = []
    monkeypatch.setattr(scheduler, "_tick", lambda cfg, db, dex, eex, ts: calls.append(ts))

    scheduler.run_live_loop({"mode": "testnet"}, db_path, max_iterations=3)

    assert len(calls) == 3


def test_run_live_loop_tick_exception_notifies_and_continues(db_path, monkeypatch):
    """한 틱에서 예외가 나도 CRITICAL 알림만 보내고 루프는 계속 돈다(다음
    틱에서 재시도) — 실패를 조용히 삼키지도, 루프 전체를 죽이지도 않는다."""
    monkeypatch.setattr(scheduler, "get_exchange", lambda testnet=False: object())
    monkeypatch.setattr(scheduler.executor, "get_authenticated_exchange", lambda testnet=True: _StubReconciledExchange())
    monkeypatch.setattr(scheduler.live_state, "recover_state",
                         lambda db, ex: SimpleNamespace(positions_match=True, orders_match=True, mismatches=[]))
    monkeypatch.setattr(scheduler, "_sleep_until_next_tick", lambda ts: None)

    def _boom(cfg, db, dex, eex, ts):
        raise RuntimeError("simulated tick failure")

    monkeypatch.setattr(scheduler, "_tick", _boom)
    sent = []
    monkeypatch.setattr(scheduler.telegram, "send_critical_alert", lambda msg: sent.append(msg) or True)

    scheduler.run_live_loop({"mode": "testnet"}, db_path, max_iterations=2)

    assert len(sent) == 2


# =====================================================================
# filtered_trend 청산 로직 (2026-07-25 추가 — engine.py 재사용 검증)
# =====================================================================

def _make_df_15m(n=40, base_price=100.0, low_last=None, high_last=None, close_last=None, freq="15min"):
    idx = pd.date_range("2026-07-01", periods=n, freq=freq, tz="UTC")
    df = pd.DataFrame({
        "open": base_price, "high": base_price + 1.0, "low": base_price - 1.0,
        "close": base_price, "volume": 100.0,
    }, index=idx)
    if low_last is not None:
        df.iloc[-1, df.columns.get_loc("low")] = low_last
    if high_last is not None:
        df.iloc[-1, df.columns.get_loc("high")] = high_last
    if close_last is not None:
        df.iloc[-1, df.columns.get_loc("close")] = close_last
    return df


def _empty_funding_df():
    return pd.DataFrame(columns=["timestamp", "funding_rate"])


class _StubMarketOrderExchange:
    """place_order()용 create_order + ensure_stop_placed()가 요구하는
    최소 인터페이스(load_markets/market/fapiPrivateGetOpenAlgoOrders)도
    갖춰 스탑 스윕이 조용히(에러 로그 없이) 통과하게 한다 - 이 테스트의
    관심사는 청산 로직이지 스탑 배치 자체가 아니므로."""

    def __init__(self, fill_price: float = 100.0, always_fail: bool = False):
        self.fill_price = fill_price
        self.always_fail = always_fail
        self.calls: list[dict] = []
        self._algo_orders: list[dict] = []

    def create_order(self, symbol, order_type, side, qty, price=None, params=None):
        if order_type == "STOP_MARKET":
            self._algo_orders.append({
                "symbol": symbol.replace("/", "").split(":")[0], "orderType": "STOP_MARKET",
                "side": side.upper(), "algoStatus": "NEW",
            })
            return {"id": f"STOP{len(self._algo_orders)}"}
        self.calls.append({"symbol": symbol, "side": side, "qty": qty})
        if self.always_fail:
            raise RuntimeError("simulated exchange error")
        return {"id": f"ORD{len(self.calls)}", "status": "closed", "filled": qty, "average": self.fill_price}

    def load_markets(self):
        pass

    def market(self, symbol):
        return {"id": symbol.replace("/", "").split(":")[0]}

    def fapiPrivateGetOpenAlgoOrders(self):
        return list(self._algo_orders)


def test_compute_bars_held_zero_on_entry_and_increments_per_bar():
    entry_time = pd.Timestamp("2026-07-20 00:00:00", tz="UTC")
    assert scheduler._compute_bars_held(entry_time, entry_time) == 0
    assert scheduler._compute_bars_held(entry_time, entry_time + pd.Timedelta(minutes=15)) == 1
    assert scheduler._compute_bars_held(entry_time, entry_time + pd.Timedelta(hours=1)) == 4


def test_reconstruct_filtered_trend_position_maps_all_fields():
    pos = {
        "symbol": "ETH/USDT:USDT", "direction": "long", "qty": 1.5, "entry_price": 3000.0,
        "entry_time": "2026-07-20T00:00:00+00:00", "current_stop": 2950.0, "initial_stop": 2900.0,
        "entry_fee_usd": 2.25, "funding_paid_usd": 0.3, "partial_taken": 1,
    }
    ts = pd.Timestamp("2026-07-20 01:00:00", tz="UTC")

    position = scheduler._reconstruct_filtered_trend_position(pos, ts)

    assert position.strategy == "trend"  # engine.py 이름공간, state.py의 "filtered_trend"가 아님
    assert position.direction == "long"
    assert position.qty == 1.5
    assert position.initial_stop == 2900.0
    assert position.current_stop == 2950.0
    assert position.initial_stop_distance == pytest.approx(100.0)
    assert position.partial_taken is True
    assert position.bars_held == 4  # 1시간 경과 = 4봉
    assert position.entry_fee_usd == 2.25
    assert position.funding_paid_usd == 0.3


def test_manage_open_position_stop_loss_full_fill_deletes_position(db_path, monkeypatch):
    save_position(db_path, scheduler.FT_SYMBOL, "filtered_trend", "long", 1.0, 100.0,
                   "2026-07-01T00:00:00+00:00", 95.0, initial_stop=95.0, entry_fee_usd=0.05,
                   funding_paid_usd=0.0, partial_taken=False)
    pos = load_open_positions(db_path, strategy="filtered_trend")[0]

    trend_cfg = {**TREND_CFG, "stop_grace_period_bars": 0}  # 유예 없이 즉시 스탑 체크
    df_15m = _make_df_15m(low_last=90.0)  # 스탑(95) 아래로 저가 이탈 -> stop_loss 트리거
    latest_bar = df_15m.iloc[-1]
    ts = df_15m.index[-1]

    exchange = _StubMarketOrderExchange(fill_price=94.5)
    sent = []
    monkeypatch.setattr(scheduler.telegram, "send_entry_exit_notification", lambda **kw: sent.append(kw) or True)

    scheduler._manage_open_filtered_trend_position(
        db_path, exchange, pos, df_15m, df_15m, _empty_funding_df(),
        trend_cfg, COST_CFG, ts, latest_bar, total_equity=10000.0, account_cfg={"initial_equity_usd": 10000},
    )

    assert load_open_positions(db_path, strategy="filtered_trend") == []  # 실제 체결 -> 포지션 삭제
    assert exchange.calls == [{"symbol": scheduler.FT_SYMBOL, "side": "sell", "qty": 1.0}]
    assert len(sent) == 1
    assert sent[0]["price"] == 94.5  # 시뮬레이션 가격이 아니라 실제 체결가 보고


def test_manage_open_position_close_order_fails_keeps_position_and_alerts(db_path, monkeypatch):
    """★ 요구된 실패 케이스: 청산 주문이 미체결이면 _manage_trend_position이
    계산한 "청산됐어야 할" 상태를 state에 반영하지 않는다(공허한 성공 금지)."""
    save_position(db_path, scheduler.FT_SYMBOL, "filtered_trend", "long", 1.0, 100.0,
                   "2026-07-01T00:00:00+00:00", 95.0, initial_stop=95.0, entry_fee_usd=0.05,
                   funding_paid_usd=0.0, partial_taken=False)
    pos = load_open_positions(db_path, strategy="filtered_trend")[0]

    trend_cfg = {**TREND_CFG, "stop_grace_period_bars": 0}
    df_15m = _make_df_15m(low_last=90.0)
    latest_bar = df_15m.iloc[-1]
    ts = df_15m.index[-1]

    exchange = _StubMarketOrderExchange(always_fail=True)
    critical_sent = []
    monkeypatch.setattr(scheduler.telegram, "send_critical_alert", lambda msg: critical_sent.append(msg) or True)
    entry_exit_sent = []
    monkeypatch.setattr(scheduler.telegram, "send_entry_exit_notification", lambda **kw: entry_exit_sent.append(kw) or True)

    scheduler._manage_open_filtered_trend_position(
        db_path, exchange, pos, df_15m, df_15m, _empty_funding_df(),
        trend_cfg, COST_CFG, ts, latest_bar, total_equity=10000.0, account_cfg={"initial_equity_usd": 10000},
    )

    remaining = load_open_positions(db_path, strategy="filtered_trend")
    assert len(remaining) == 1  # 미체결이므로 삭제되지 않음
    assert remaining[0]["qty"] == 1.0  # 상태 불변
    assert len(critical_sent) == 1
    assert entry_exit_sent == []  # 성공한 것처럼 진입/청산 알림을 보내지 않음


def test_manage_open_position_no_exit_condition_keeps_position_open(db_path, monkeypatch):
    """스탑/부분익절/시간청산 어느 것도 트리거 안 되면 포지션이 그대로
    유지돼야 한다(주문 없음, 삭제 없음)."""
    save_position(db_path, scheduler.FT_SYMBOL, "filtered_trend", "long", 1.0, 100.0,
                   "2026-07-01T00:00:00+00:00", 95.0, initial_stop=95.0, entry_fee_usd=0.05,
                   funding_paid_usd=0.0, partial_taken=False)
    pos = load_open_positions(db_path, strategy="filtered_trend")[0]

    trend_cfg = {**TREND_CFG, "stop_grace_period_bars": 0}
    df_15m = _make_df_15m(low_last=99.0, high_last=101.0, close_last=100.5)  # 스탑(95) 근처도 아님, 트리거 없음
    latest_bar = df_15m.iloc[-1]
    ts = df_15m.index[-1]

    exchange = _StubMarketOrderExchange()

    scheduler._manage_open_filtered_trend_position(
        db_path, exchange, pos, df_15m, df_15m, _empty_funding_df(),
        trend_cfg, COST_CFG, ts, latest_bar, total_equity=10000.0, account_cfg={"initial_equity_usd": 10000},
    )

    assert exchange.calls == []  # 청산 주문 없음
    remaining = load_open_positions(db_path, strategy="filtered_trend")
    assert len(remaining) == 1
    assert remaining[0]["qty"] == 1.0


def test_manage_open_position_partial_tp_reduces_qty_and_moves_stop_to_breakeven(db_path, monkeypatch):
    """1.5R 도달 -> 25% 부분청산 + breakeven_after_partial(config.yaml True)로
    스탑이 본전 이동 - engine.py 로직 그대로(재구현 아님)."""
    save_position(db_path, scheduler.FT_SYMBOL, "filtered_trend", "long", 1.0, 100.0,
                   "2026-07-01T00:00:00+00:00", 95.0, initial_stop=95.0, entry_fee_usd=0.05,
                   funding_paid_usd=0.0, partial_taken=False)
    pos = load_open_positions(db_path, strategy="filtered_trend")[0]

    trend_cfg = {**TREND_CFG, "stop_grace_period_bars": 0}
    # entry=100, initial_stop=95, distance=5 -> 1.5R = 107.5 필요(partial_tp_r_multiple=1.5)
    df_15m = _make_df_15m(low_last=99.0, high_last=110.0, close_last=108.0)
    latest_bar = df_15m.iloc[-1]
    ts = df_15m.index[-1]

    exchange = _StubMarketOrderExchange(fill_price=107.6)
    sent = []
    monkeypatch.setattr(scheduler.telegram, "send_entry_exit_notification", lambda **kw: sent.append(kw) or True)

    scheduler._manage_open_filtered_trend_position(
        db_path, exchange, pos, df_15m, df_15m, _empty_funding_df(),
        trend_cfg, COST_CFG, ts, latest_bar, total_equity=10000.0, account_cfg={"initial_equity_usd": 10000},
    )

    remaining = load_open_positions(db_path, strategy="filtered_trend")
    assert len(remaining) == 1  # 전량청산 아님 - 포지션 유지
    assert remaining[0]["qty"] == pytest.approx(0.75)  # 25% 부분청산
    assert remaining[0]["partial_taken"] == 1
    # breakeven_after_partial=True로 최소 본전(100)까지 이동, 이후 트레일링
    # 래칫(max(stop, chandelier))이 같은 호출 안에서 추가로 더 끌어올릴 수
    # 있음(engine.py 로직 그대로) - 정확한 트레일 값을 손으로 재계산하는
    # 대신 "본전 이상으로만 움직인다"는 불변식을 확인한다.
    assert remaining[0]["current_stop"] >= 100.0
    assert exchange.calls == [{"symbol": scheduler.FT_SYMBOL, "side": "sell", "qty": pytest.approx(0.25)}]
    assert len(sent) == 1
    assert sent[0]["price"] == 107.6
