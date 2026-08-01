"""tests/test_live_scheduler_integration.py — src/live/scheduler.py 실제
Binance USDM testnet 통합 테스트.

@pytest.mark.integration로 표시돼 기본 `pytest` 실행에서는 제외된다
(pytest.ini의 addopts). 실행하려면 `pytest -m integration`.

두 케이스로 나눈다:
1. filtered_trend 경로 — 실제 `_tick()`을 그대로 1회 호출(신호 발생 여부는
   실시장 상태라 통제 불가 - 진입이 나면 청산까지 확인 후 정리, 안 나면
   크래시 없이 스킵됐는지만 확인).
2. 2a 리밸런스 경로 — `strategy_2a.run_live_step()`을 스텁해 실제 20자산
   전량이 아니라 1롱+1숏(BTC/SOL, ETH 제외 정책과 무관하게 임의 선택)
   으로 범위를 좁힌다. 20자산 전량 왕복 주문은 "배선이 맞는지" 확인하는
   목적에 비해 과도한 실거래 비용/리스크라 판단(REBALANCE_ORDER_ANALYSIS.md
   워스트케이스 계산 대상은 이미 그 문서에서 숫자로 검증했으므로, 여기서는
   "청산->진입 순서/state 기록/telegram 발신 배선"만 실측 확인하면 충분).
   테스트 종료 시 두 포지션 모두 직접 청산해 계정을 정리한다.

두 케이스 모두 실행 후 계정에 잔여 포지션/주문이 없는지 최종 확인한다."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from src.data.feed import get_exchange
from src.live import executor, scheduler
from src.live import state as live_state
from src.notify import telegram

CFG = {
    "mode": "testnet",
    "account": {
        "initial_equity_usd": 10000,
        "leverage": 2,
        "risk_per_trade_pct": 0.5,
        "max_concurrent_positions": 3,
        "daily_loss_limit_pct": -3.0,
    },
    "costs": {"taker_fee_pct": 0.05, "slippage_pct": 0.03},
    "regime": {
        "adx_period": 14, "adx_trend_min": 25, "adx_range_max": 20,
        "bb_period": 20, "bb_width_percentile_window": 120,
        "bb_width_percentile_trend_min": 60, "bb_width_percentile_range_max": 40,
    },
    "trend": {
        "donchian_period": 20, "ema_period": 50, "atr_period": 14, "atr_median_window": 96,
        "low_vol_filter": True, "stop_atr_mult": 2.0, "partial_tp_r_multiple": 1.5,
        "partial_tp_pct": 25, "breakeven_after_partial": True, "chandelier_period": 22,
        "chandelier_atr_mult": 3.0, "time_exit_bars": 96, "time_exit_min_r": 1.0,
        "require_confirmation_bar": True, "stop_grace_period_bars": 4,
    },
    "data": {"since": "2023-01-01T00:00:00Z", "ohlcv_cache_dir": "data/ohlcv", "funding_cache_dir": "data/funding"},
    "portfolio": {"weights": {"filtered_trend": 0.8383017180188289, "2a": 0.1616982819811711}},
}

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "state.db"
    live_state.init_db(path)
    return path


def _close_all_test_positions(exec_exchange) -> None:
    """포지션 청산 + ensure_stop_placed가 걸어둔 STOP_MARKET 알고주문도
    함께 취소한다 - 실측으로 확인된 필수 정리 항목(포지션만 닫고 스탑을
    남겨두면 계정에 고아 알고주문이 남는다, executor.py 통합 테스트에서도
    동일하게 다뤘던 패턴)."""
    positions = exec_exchange.fetch_positions()
    for p in positions:
        qty = float(p.get("contracts") or 0)
        if qty == 0:
            continue
        side = "sell" if p["side"] == "long" else "buy"
        exec_exchange.create_order(p["symbol"], "market", side, abs(qty), params={"reduceOnly": True})
    for o in exec_exchange.fapiPrivateGetOpenAlgoOrders():
        exec_exchange.fapiPrivateDeleteAlgoOrder({"algoId": o["algoId"]})


@pytest.mark.integration
def test_real_testnet_single_tick_filtered_trend_path(db_path, capsys):
    data_exchange = get_exchange(testnet=False)
    exec_exchange = executor.get_authenticated_exchange(testnet=True)
    exec_exchange.load_markets()

    ts = pd.Timestamp.now(tz="UTC").floor("15min")
    try:
        scheduler._tick(CFG, db_path, data_exchange, exec_exchange, ts)

        snapshots = live_state.load_equity_snapshots_since(db_path, ts.replace(hour=0, minute=0, second=0).isoformat())
        assert len(snapshots) == 1  # 크래시 없이 틱 사이클이 끝까지 돎(state 기록까지 확인)

        positions = live_state.load_open_positions(db_path, strategy="filtered_trend")
        assert len(positions) <= 1  # 신호가 있었으면 1건, 없었으면 0건 - 둘 다 정상
    finally:
        _close_all_test_positions(exec_exchange)

    remaining = [p for p in exec_exchange.fetch_positions() if float(p.get("contracts") or 0) != 0]
    assert remaining == []
    assert exec_exchange.fapiPrivateGetOpenAlgoOrders() == []

    api_secret = __import__("os").environ.get("BINANCE_TESTNET_API_SECRET")
    captured = capsys.readouterr()
    assert api_secret not in captured.out
    assert api_secret not in captured.err

    # ★ 2026-08-01(사용자 발견) 회귀 테스트: 2026-07-31 14:00/14:15 UTC
    # 두 틱 모두 진입조건이 충족돼 있었는데도 실제로는 조용히 스킵됐고
    # 로그가 전혀 없어 사후에 원인을 특정할 수 없었다. 신규진입을 안 했다면
    # (positions == 0) 반드시 진단 로그가 남아야 한다 - "조용한 스킵" 자체를
    # 회귀로 잡는다(실제 testnet/실시장 데이터로).
    if len(positions) == 0:
        assert "filtered_trend 진단" in captured.err


@pytest.mark.integration
def test_real_testnet_2a_rebalance_bounded_cycle(db_path):
    """run_live_step을 스텁해 1롱(BTC)+1숏(SOL)으로 범위를 좁힌 실제
    testnet 리밸런스 사이클 - 청산(이번엔 기존 포지션 없어 0건)->신규진입
    (2건)->rebalance_log 기록->telegram 발신까지 실측, 종료 시 정리."""
    data_exchange = get_exchange(testnet=False)
    exec_exchange = executor.get_authenticated_exchange(testnet=True)
    exec_exchange.load_markets()

    ts = pd.Timestamp("2026-07-19 00:00:00", tz="UTC")  # 리밸런스 경계(일요일 00:00) - 실제 트리거 여부와 무관하게 고정 사용
    stub_target = {"long_group": {"BTC"}, "short_group": {"SOL"}, "weight": 0.05, "as_of": ts}

    telegram_calls = []
    original_send_rebalance = telegram.send_rebalance_notification

    def _spy_send_rebalance(**kwargs):
        telegram_calls.append(kwargs)
        return original_send_rebalance(**kwargs)

    try:
        with patch.object(scheduler.strategy_2a, "run_live_step", return_value=stub_target), \
             patch.object(scheduler.telegram, "send_rebalance_notification", side_effect=_spy_send_rebalance):
            scheduler._process_2a_rebalance(CFG, db_path, data_exchange, exec_exchange, ts,
                                             total_equity=10000.0, entries_blocked=False)

        positions = live_state.load_open_positions(db_path, strategy="2a")
        assert {p["symbol"] for p in positions} == {"BTC/USDT:USDT", "SOL/USDT:USDT"}

        rebalance_rows = []
        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rebalance_rows = [dict(r) for r in conn.execute("SELECT * FROM rebalance_log").fetchall()]
        assert len(rebalance_rows) == 1

        assert len(telegram_calls) == 1
        assert telegram_calls[0]["new_long"] == ["BTC/USDT:USDT"]
        assert telegram_calls[0]["new_short"] == ["SOL/USDT:USDT"]
    finally:
        _close_all_test_positions(exec_exchange)
        for p in ["BTC/USDT:USDT", "SOL/USDT:USDT"]:
            live_state.delete_position(db_path, p, "2a")

    remaining = [p for p in exec_exchange.fetch_positions() if float(p.get("contracts") or 0) != 0]
    assert remaining == []
    assert exec_exchange.fapiPrivateGetOpenAlgoOrders() == []
