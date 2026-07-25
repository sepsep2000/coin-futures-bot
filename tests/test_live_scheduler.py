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

from src.live import scheduler
from src.live.state import init_db, save_equity_snapshot, save_order, save_position

CFG = {"account": {"daily_loss_limit_pct": -3.0}}


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
