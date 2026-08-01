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


@pytest.fixture(autouse=True)
def _isolate_heartbeat_path(monkeypatch, tmp_path):
    """★ 격리 안전장치 — run_live_loop()을 실행하는 테스트가 실수로 실제
    프로젝트의 logs/heartbeat.json에 쓰지 않도록 전 테스트에 자동 적용.
    개별 테스트가 하트비트 경로를 직접 검증해야 하면 그 안에서 다시
    monkeypatch.setattr로 덮어쓰면 된다(나중 설정이 우선)."""
    monkeypatch.setattr(scheduler, "HEARTBEAT_PATH", tmp_path / "_autouse_heartbeat.json")


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

    def __init__(self, fill_price: float = 100.0, always_fail: bool = False, real_positions: list[dict] | None = None):
        self.fill_price = fill_price
        self.always_fail = always_fail
        self.calls: list[dict] = []
        self._algo_orders: list[dict] = []
        # 2026-07-27 사고 대응(reports/G4_KILLSWITCH_INCIDENT_ANALYSIS.md) - 킬스위치가
        # 이제 fetch_positions()를 청산 대상의 근거로 쓰므로, 테스트가 "거래소 실측"을
        # 직접 지정할 수 있어야 한다(기본값은 빈 리스트 - 명시적으로 지정 안 하면
        # 실측상 포지션 없음으로 취급, DB만 보고 청산하던 예전 동작을 그대로 재현하지 않음).
        self._real_positions = real_positions if real_positions is not None else []

    def fetch_positions(self):
        return list(self._real_positions)

    def create_order(self, symbol, order_type, side, qty, price=None, params=None):
        if order_type == "STOP_MARKET":
            self._algo_orders.append({
                "symbol": symbol.replace("/", "").split(":")[0], "orderType": "STOP_MARKET",
                "side": side.upper(), "algoStatus": "NEW",
            })
            return {"id": f"STOP{len(self._algo_orders)}"}
        self.calls.append({"symbol": symbol, "side": side, "qty": qty, "reduceOnly": (params or {}).get("reduceOnly")})
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
    assert exchange.calls == [{"symbol": scheduler.FT_SYMBOL, "side": "sell", "qty": 1.0, "reduceOnly": None}]
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
    assert exchange.calls == [{"symbol": scheduler.FT_SYMBOL, "side": "sell", "qty": pytest.approx(0.25), "reduceOnly": None}]
    assert len(sent) == 1
    assert sent[0]["price"] == 107.6


# =====================================================================
# 연속오류 킬스위치 (2026-07-25 추가 — SPEC 2.4)
# =====================================================================

def test_run_live_loop_triggers_kill_switch_after_threshold_consecutive_failures(db_path, monkeypatch):
    """★ N=5(SPEC 2.4, 임의 축소 금지) 연속 실패해야 발동 - 그 전엔 계속 재시도."""
    monkeypatch.setattr(scheduler, "get_exchange", lambda testnet=False: object())
    monkeypatch.setattr(scheduler.executor, "get_authenticated_exchange", lambda testnet=True: _StubReconciledExchange())
    monkeypatch.setattr(scheduler.live_state, "recover_state",
                         lambda db, ex: SimpleNamespace(positions_match=True, orders_match=True, mismatches=[]))
    monkeypatch.setattr(scheduler, "_sleep_until_next_tick", lambda ts: None)

    def _boom(cfg, db, dex, eex, ts):
        raise RuntimeError("simulated persistent failure")

    monkeypatch.setattr(scheduler, "_tick", _boom)
    liquidation_calls = []
    monkeypatch.setattr(scheduler, "_execute_kill_switch_liquidation",
                         lambda db, ex, count: liquidation_calls.append(count))
    monkeypatch.setattr(scheduler.telegram, "send_critical_alert", lambda msg: True)

    # max_iterations를 넉넉히(10) 줘도 5회째에서 멈춰야 한다(발동 즉시 반환)
    scheduler.run_live_loop({"mode": "testnet"}, db_path, max_iterations=10)

    assert liquidation_calls == [5]  # 정확히 5번째 실패에서 1회만 발동


def test_run_live_loop_success_resets_consecutive_error_counter(db_path, monkeypatch):
    """실패-실패-실패-실패-성공-실패-실패-실패-실패 처럼 중간에 성공이 끼면
    카운터가 리셋돼 5연속이 안 채워지면 킬스위치가 발동하지 않아야 한다."""
    monkeypatch.setattr(scheduler, "get_exchange", lambda testnet=False: object())
    monkeypatch.setattr(scheduler.executor, "get_authenticated_exchange", lambda testnet=True: _StubReconciledExchange())
    monkeypatch.setattr(scheduler.live_state, "recover_state",
                         lambda db, ex: SimpleNamespace(positions_match=True, orders_match=True, mismatches=[]))
    monkeypatch.setattr(scheduler, "_sleep_until_next_tick", lambda ts: None)
    monkeypatch.setattr(scheduler.telegram, "send_critical_alert", lambda msg: True)

    # 4실패 + 1성공 + 4실패 = 9틱, 연속 5회를 채우는 구간이 없음
    pattern = [False, False, False, False, True, False, False, False, False]
    calls = {"n": 0}

    def _pattern_tick(cfg, db, dex, eex, ts):
        should_succeed = pattern[calls["n"]]
        calls["n"] += 1
        if not should_succeed:
            raise RuntimeError("simulated failure")

    monkeypatch.setattr(scheduler, "_tick", _pattern_tick)
    liquidation_calls = []
    monkeypatch.setattr(scheduler, "_execute_kill_switch_liquidation",
                         lambda db, ex, count: liquidation_calls.append(count))

    scheduler.run_live_loop({"mode": "testnet"}, db_path, max_iterations=len(pattern))

    assert liquidation_calls == []  # 5연속을 채운 구간이 없으므로 발동 안 함
    assert calls["n"] == len(pattern)  # 끝까지 정상적으로 돌았음(중도에 멈추지 않음)


def test_execute_kill_switch_liquidation_closes_all_positions_both_strategies(db_path, monkeypatch):
    save_position(db_path, "ETH/USDT:USDT", "filtered_trend", "long", 1.0, 3000.0, "2026-07-25T00:00:00Z", 2900.0)
    save_position(db_path, "BTC/USDT:USDT", "2a", "short", 0.01, 60000.0, "2026-07-25T00:00:00Z", 78000.0)

    # DB와 거래소가 일치하는 정상 케이스 - 거래소 실측(fetch_positions)이 DB와 같은 내용을 보고한다.
    exchange = _StubMarketOrderExchange(fill_price=100.0, real_positions=[
        {"symbol": "ETH/USDT:USDT", "contracts": 1.0, "side": "long"},
        {"symbol": "BTC/USDT:USDT", "contracts": 0.01, "side": "short"},
    ])
    sent = []
    monkeypatch.setattr(scheduler.telegram, "send_critical_alert", lambda msg: sent.append(msg) or True)

    scheduler._execute_kill_switch_liquidation(db_path, exchange, consecutive_errors=5)

    assert load_open_positions(db_path) == []  # 양쪽 전략 모두 청산됨
    closed_symbols = {c["symbol"] for c in exchange.calls}
    assert closed_symbols == {"ETH/USDT:USDT", "BTC/USDT:USDT"}
    assert all(c["reduceOnly"] is True for c in exchange.calls)  # 배증 방지 안전장치
    assert len(sent) == 1
    assert "5회" in sent[0]


def test_execute_kill_switch_liquidation_reports_failed_closes_without_deleting_state(db_path, monkeypatch):
    """★ 요구된 실패 케이스: 청산 주문마저 실패하면(이미 API가 불안정한
    상황) 그 포지션은 state에서 지우지 않는다 - 공허한 성공 금지."""
    save_position(db_path, "ETH/USDT:USDT", "filtered_trend", "long", 1.0, 3000.0, "2026-07-25T00:00:00Z", 2900.0)

    exchange = _StubMarketOrderExchange(always_fail=True, real_positions=[
        {"symbol": "ETH/USDT:USDT", "contracts": 1.0, "side": "long"},
    ])
    sent = []
    monkeypatch.setattr(scheduler.telegram, "send_critical_alert", lambda msg: sent.append(msg) or True)

    scheduler._execute_kill_switch_liquidation(db_path, exchange, consecutive_errors=5)

    remaining = load_open_positions(db_path)
    assert len(remaining) == 1  # 청산 실패 - state 유지(방치와는 다름, 명시적으로 남겨서 사람이 보게 함)
    assert len(sent) == 1
    assert "ETH/USDT:USDT" in sent[0]  # 실패 목록에 포함돼 CRITICAL로 보고됨


def test_execute_kill_switch_liquidation_uses_exchange_not_db_when_they_disagree(db_path, monkeypatch):
    """★ 회귀 테스트(2026-07-27 사고 재현 방지, reports/G4_KILLSWITCH_INCIDENT_ANALYSIS.md
    2.3절) - DB는 낡은 값(qty=2.077 롱)을 갖고 있지만 실제 거래소는 이미 다른 상태
    (순숏 -2.075)인 상황을 재현한다. 청산은 반드시 거래소 실측(숏 2.075, 이를 닫으려면
    buy)을 따라야 하며, DB의 stale 값(롱 2.077, sell로 착각)을 따라가면 안 된다 —
    이게 바로 사고 당시 포지션을 배증시킨 원인이었다."""
    save_position(db_path, "ETH/USDT:USDT", "filtered_trend", "long", 2.077, 1937.28, "2026-07-26T22:15:00Z", 1900.0)

    exchange = _StubMarketOrderExchange(fill_price=1955.0, real_positions=[
        {"symbol": "ETH/USDT:USDT", "contracts": 2.075, "side": "short"},
    ])
    monkeypatch.setattr(scheduler.telegram, "send_critical_alert", lambda msg: True)

    scheduler._execute_kill_switch_liquidation(db_path, exchange, consecutive_errors=5)

    assert len(exchange.calls) == 1
    call = exchange.calls[0]
    assert call["side"] == "buy"  # 숏을 닫으려면 buy - DB(롱 -> sell)를 따랐다면 틀렸을 것
    assert call["qty"] == 2.075  # 거래소 실측 수량 - DB의 2.077이 아님
    assert call["reduceOnly"] is True


def test_execute_kill_switch_liquidation_cleans_up_stale_db_only_records(db_path, monkeypatch):
    """거래소엔 이미 없는데 DB에만 남아있는 레코드(예: 사고로 상태갱신이 실패한 채
    방치된 행)는 청산 주문 없이(실물이 없으므로) 삭제만 한다 - 다음 재시작 때
    recover_state()가 또 혼란을 일으키지 않도록."""
    save_position(db_path, "BTC/USDT:USDT", "2a", "short", 0.01, 60000.0, "2026-07-25T00:00:00Z", 78000.0)

    exchange = _StubMarketOrderExchange(real_positions=[])  # 거래소엔 아무 포지션도 없음
    monkeypatch.setattr(scheduler.telegram, "send_critical_alert", lambda msg: True)

    scheduler._execute_kill_switch_liquidation(db_path, exchange, consecutive_errors=5)

    assert exchange.calls == []  # 청산할 실물이 없으므로 주문 자체를 내지 않음
    assert load_open_positions(db_path) == []  # stale 레코드는 삭제됨


# =====================================================================
# 하트비트 (2026-07-25 추가 — scripts/healthcheck.py가 읽는 생존 신호)
# =====================================================================

def test_write_heartbeat_creates_file_with_iso_timestamp(tmp_path):
    import json as _json

    path = tmp_path / "sub" / "heartbeat.json"
    ts = pd.Timestamp("2026-07-25 03:15:00", tz="UTC")

    scheduler._write_heartbeat(path, ts)

    data = _json.loads(path.read_text(encoding="utf-8"))
    assert data["last_tick_iso"] == ts.isoformat()


def test_run_live_loop_writes_heartbeat_after_success_and_after_failure(db_path, monkeypatch, tmp_path):
    """★ 성공/실패 둘 다 하트비트를 갱신해야 한다(루프 자체가 살아있다는
    증거) — 갱신이 안 되는 경우는 오직 _tick()이 행(hang)돼 이 지점에
    도달하지 못할 때뿐이어야 한다."""
    heartbeat_path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(scheduler, "HEARTBEAT_PATH", heartbeat_path)
    monkeypatch.setattr(scheduler, "get_exchange", lambda testnet=False: object())
    monkeypatch.setattr(scheduler.executor, "get_authenticated_exchange", lambda testnet=True: _StubReconciledExchange())
    monkeypatch.setattr(scheduler.live_state, "recover_state",
                         lambda db, ex: SimpleNamespace(positions_match=True, orders_match=True, mismatches=[]))
    monkeypatch.setattr(scheduler, "_sleep_until_next_tick", lambda ts: None)
    monkeypatch.setattr(scheduler.telegram, "send_critical_alert", lambda msg: True)

    monkeypatch.setattr(scheduler, "_tick", lambda cfg, db, dex, eex, ts: None)  # 성공 케이스
    scheduler.run_live_loop({"mode": "testnet"}, db_path, max_iterations=1)
    assert heartbeat_path.exists()

    def _boom(cfg, db, dex, eex, ts):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(scheduler, "_tick", _boom)  # 실패 케이스(킬스위치 임계치 미만)
    heartbeat_path.unlink()
    scheduler.run_live_loop({"mode": "testnet"}, db_path, max_iterations=1)
    assert heartbeat_path.exists()  # 실패해도 갱신됨(루프는 살아있으므로)


# =====================================================================
# _process_filtered_trend_tick 진단 로깅 (2026-08-01, 사용자 발견) —
# 2026-07-31 14:00/14:15 UTC 두 틱 모두 사후 재현 시 진입조건이 전부
# 충족돼 있었는데도 실제로는 주문이 안 나갔고 에러/CRITICAL도 전혀 없어
# 원인을 사후에 특정할 수 없었다("조용한 스킵"). 신규진입을 스킵하는
# 모든 분기가 반드시 로컬 로그(stderr, runner.log)를 남기는지 검증한다.
# 데이터 계층(update_ohlcv_cache 등)은 네트워크가 필요해 이 파일의 다른
# 테스트와 같은 이유로 스텁 처리한다(파일 상단 docstring 참조) — 대신
# classify_regime/generate_trend_signals/_momentum_agrees/position_size를
# 직접 스텁해 각 분기를 결정론적으로 재현한다.
# =====================================================================

FT_CFG = {
    "account": {
        "risk_per_trade_pct": 0.5, "leverage": 2, "max_concurrent_positions": 3,
        "initial_equity_usd": 10000.0,
    },
    "trend": {},
    "costs": {"taker_fee_pct": 0.05, "slippage_pct": 0.03},
    "data": {"ohlcv_cache_dir": "data/ohlcv", "funding_cache_dir": "data/funding"},
    "portfolio": {"weights": {"filtered_trend": 0.8}},
    "regime": {},
}


@pytest.fixture
def _stub_data_layer(monkeypatch):
    """네트워크 없이 _process_filtered_trend_tick의 데이터 로딩 앞단을
    통과시키는 최소 더미 OHLCV/펀딩 데이터."""
    idx15 = pd.date_range("2026-07-31 12:00:00", periods=8, freq="15min", tz="UTC")
    dummy_15m = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10.0}, index=idx15)
    idx1h = pd.date_range("2026-07-31 11:00:00", periods=3, freq="1h", tz="UTC")
    dummy_1h = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10.0}, index=idx1h)
    dummy_funding = pd.DataFrame({"timestamp": [0], "funding_rate": [0.0001]})

    monkeypatch.setattr(scheduler, "update_ohlcv_cache", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "update_funding_cache", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "_load_and_index", lambda path: dummy_1h if "1h" in path.name else dummy_15m)
    monkeypatch.setattr(scheduler, "load_cache", lambda path: dummy_funding)
    return idx15[-1]  # ts로 쓸 마지막 15분봉 시각


def test_process_filtered_trend_tick_logs_when_entries_blocked(db_path, _stub_data_layer, capsys):
    """정상: 일일손실한도로 막힌 경우 즉시 스킵 사유를 로그로 남긴다."""
    ts = _stub_data_layer
    scheduler._process_filtered_trend_tick(FT_CFG, db_path, object(), object(), ts, 10000.0, True)

    captured = capsys.readouterr()
    assert "entries_blocked=True" in captured.err


def test_process_filtered_trend_tick_logs_when_regime_not_trend(db_path, _stub_data_layer, monkeypatch, capsys):
    """정상: 국면이 TREND가 아니면 신호 계산 없이 스킵하고 사유를 남긴다."""
    ts = _stub_data_layer
    monkeypatch.setattr(scheduler, "classify_regime", lambda df, cfg: pd.Series("RANGE", index=df.index))

    scheduler._process_filtered_trend_tick(FT_CFG, db_path, object(), object(), ts, 10000.0, False)

    captured = capsys.readouterr()
    assert "regime=RANGE" in captured.err


def test_process_filtered_trend_tick_logs_when_qty_zero(db_path, _stub_data_layer, monkeypatch, capsys):
    """경계: 신호/모멘텀은 충족되지만 사이징 결과 qty<=0이면 스킵하고
    이유(step/min/leg_equity)까지 로그에 남긴다 - 2026-07-31 사고급 상황을
    다음엔 로그만으로 바로 구분할 수 있게 하는 게 이 기능의 핵심 목적."""
    ts = _stub_data_layer
    monkeypatch.setattr(scheduler, "classify_regime", lambda df, cfg: pd.Series("TREND", index=df.index))
    monkeypatch.setattr(scheduler, "generate_trend_signals", lambda df, cfg: pd.DataFrame(
        {"entry_long": True, "entry_short": False, "initial_stop_long": 90.0, "initial_stop_short": 110.0},
        index=df.index,
    ))
    monkeypatch.setattr(scheduler, "_momentum_agrees", lambda direction, roc: True)
    monkeypatch.setattr(scheduler, "_qty_step_and_min", lambda exchange, symbol: (0.001, 0.001))
    monkeypatch.setattr(scheduler, "position_size", lambda *a, **k: 0.0)

    scheduler._process_filtered_trend_tick(FT_CFG, db_path, object(), object(), ts, 10000.0, False)

    captured = capsys.readouterr()
    assert "qty<=0" in captured.err
    assert "direction=long" in captured.err


def test_process_filtered_trend_tick_logs_when_order_not_filled(db_path, _stub_data_layer, monkeypatch, capsys):
    """실패: 조건이 전부 충족돼 주문을 실제로 시도했지만 체결되지 않은
    경우도 조용히 넘어가지 않고 로그를 남긴다."""
    ts = _stub_data_layer
    monkeypatch.setattr(scheduler, "classify_regime", lambda df, cfg: pd.Series("TREND", index=df.index))
    monkeypatch.setattr(scheduler, "generate_trend_signals", lambda df, cfg: pd.DataFrame(
        {"entry_long": True, "entry_short": False, "initial_stop_long": 90.0, "initial_stop_short": 110.0},
        index=df.index,
    ))
    monkeypatch.setattr(scheduler, "_momentum_agrees", lambda direction, roc: True)
    monkeypatch.setattr(scheduler, "_qty_step_and_min", lambda exchange, symbol: (0.001, 0.001))
    monkeypatch.setattr(scheduler, "position_size", lambda *a, **k: 1.0)
    monkeypatch.setattr(scheduler.executor, "place_order",
                         lambda *a, **k: SimpleNamespace(status="failed", avg_fill_price=None))

    scheduler._process_filtered_trend_tick(FT_CFG, db_path, object(), object(), ts, 10000.0, False)

    captured = capsys.readouterr()
    assert "주문 시도" in captured.err  # 시도했다는 로그
    assert "체결 안 됨" in captured.err  # 실패했다는 로그


def test_process_filtered_trend_tick_logs_attempt_before_placing_order(db_path, _stub_data_layer, monkeypatch, capsys):
    """정상(성공 경로): 조건이 전부 충족되면 주문을 넣기 직전에도 로그를
    남긴다 - 이후 place_order 자체가 예외로 죽어도(테스트 범위 밖) 최소한
    "시도했다"는 흔적은 남아야 한다는 걸 보장."""
    ts = _stub_data_layer
    monkeypatch.setattr(scheduler, "classify_regime", lambda df, cfg: pd.Series("TREND", index=df.index))
    monkeypatch.setattr(scheduler, "generate_trend_signals", lambda df, cfg: pd.DataFrame(
        {"entry_long": True, "entry_short": False, "initial_stop_long": 90.0, "initial_stop_short": 110.0},
        index=df.index,
    ))
    monkeypatch.setattr(scheduler, "_momentum_agrees", lambda direction, roc: True)
    monkeypatch.setattr(scheduler, "_qty_step_and_min", lambda exchange, symbol: (0.001, 0.001))
    monkeypatch.setattr(scheduler, "position_size", lambda *a, **k: 1.0)
    monkeypatch.setattr(scheduler.executor, "place_order",
                         lambda *a, **k: SimpleNamespace(status="filled", avg_fill_price=100.0))
    monkeypatch.setattr(scheduler.executor, "ensure_stop_placed", lambda *a, **k: None)
    monkeypatch.setattr(scheduler.telegram, "send_entry_exit_notification", lambda **k: True)

    scheduler._process_filtered_trend_tick(FT_CFG, db_path, object(), object(), ts, 10000.0, False)

    captured = capsys.readouterr()
    assert "qty=1.0 - 주문 시도" in captured.err
    assert load_open_positions(db_path, strategy="filtered_trend") != []  # 실제로 진입까지 됐는지도 같이 확인
