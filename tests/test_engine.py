import pandas as pd
import pytest

from src.backtest.cost_model import exit_fill_price, taker_fee
from src.backtest.engine import (
    Position,
    _close_position,
    _current_r_multiple,
    _manage_meanrev_position,
    _manage_trend_position,
    _stop_hit,
    _target_hit,
)

COST_CFG = {"taker_fee_pct": 0.05, "maker_fee_pct": 0.02, "slippage_pct": 0.03}
TREND_CFG = {
    "partial_tp_r_multiple": 1.5, "partial_tp_pct": 25, "breakeven_after_partial": True,
    "chandelier_period": 22, "chandelier_atr_mult": 3.0,
    "time_exit_bars": 96, "time_exit_min_r": 1.0,
}
MEANREV_CFG = {"time_exit_bars": 24}


def _bar(ts, o, h, l, c) -> pd.Series:
    return pd.Series({"open": o, "high": h, "low": l, "close": c}, name=pd.Timestamp(ts))


def _long_position(entry=100.0, stop=98.0, qty=10.0) -> Position:
    entry_fee = taker_fee(qty * entry, COST_CFG["taker_fee_pct"])
    return Position(
        symbol="BTC/USDT:USDT", strategy="trend", direction="long",
        entry_time=pd.Timestamp("2024-01-01"), entry_price=entry, qty=qty,
        initial_stop=stop, current_stop=stop, initial_stop_distance=abs(entry - stop),
        entry_fee_usd=entry_fee,
    )


# --- _close_position: 엔진이 cost_model을 올바르게 조합하는지 (수기 계산 대조) ---

def test_close_position_full_exit_matches_manual_calc():
    position = _long_position()
    bar_ts = pd.Timestamp("2024-01-01 00:15")
    trade, pnl = _close_position(position, bar_ts, 98.0, "stop_loss", 1.0, COST_CFG)

    expected_exit_price = exit_fill_price(98.0, "long", COST_CFG["slippage_pct"])
    expected_fee = taker_fee(10.0 * expected_exit_price, COST_CFG["taker_fee_pct"])
    expected_gross = (expected_exit_price - 100.0) * 10.0
    expected_net = expected_gross - expected_fee - position.entry_fee_usd

    assert trade.exit_price == pytest.approx(expected_exit_price)
    assert trade.pnl_usd == pytest.approx(expected_net)
    assert pnl == pytest.approx(expected_net)
    assert trade.exit_reason == "stop_loss"
    assert trade.qty == pytest.approx(10.0)


def test_close_position_partial_exit_uses_qty_fraction():
    position = _long_position()
    trade, pnl = _close_position(position, pd.Timestamp("2024-01-01 00:15"), 103.0, "partial_tp", 0.25, COST_CFG)
    assert trade.qty == pytest.approx(2.5)
    # entry_fee도 청산 비율만큼만 배분
    assert trade.fees_usd == pytest.approx(taker_fee(2.5 * exit_fill_price(103.0, "long", 0.03), 0.05) + position.entry_fee_usd * 0.25)


# --- _stop_hit / _target_hit / _current_r_multiple ---

def test_stop_hit_long_when_low_touches_stop():
    position = _long_position()
    assert _stop_hit(position, _bar("2024-01-01", 99, 100, 97.5, 99))
    assert not _stop_hit(position, _bar("2024-01-01", 99, 100, 98.5, 99))


def test_current_r_multiple_known_answer():
    position = _long_position(entry=100.0, stop=98.0)
    assert _current_r_multiple(position, 103.0) == pytest.approx(1.5)  # (103-100)/2


def test_target_hit_none_when_no_target():
    position = _long_position()
    assert _target_hit(position, _bar("2024-01-01", 100, 200, 50, 100)) is False


# --- 트렌드 포지션 생애주기 (SPEC 2.2 골든케이스) ---

def test_trend_position_stop_loss_closes_fully():
    position = _long_position()
    bar = _bar("2024-01-01 00:15", 99, 99.5, 97.0, 98.5)  # low(97) <= stop(98)
    new_pos, trades, pnl = _manage_trend_position(position, bar, pd.Series({"chandelier_stop": None}), TREND_CFG, COST_CFG)
    assert new_pos is None
    assert len(trades) == 1
    assert trades[0].exit_reason == "stop_loss"


def test_trend_position_partial_tp_at_15r_moves_to_breakeven():
    position = _long_position()  # entry=100, stop=98, stop_distance=2 -> 1.5R = 103
    bar = _bar("2024-01-01 00:15", 100, 103.5, 99.5, 103.0)  # stop 안 닿음, 1.5R 터치
    new_pos, trades, pnl = _manage_trend_position(position, bar, pd.Series({"chandelier_stop": None}), TREND_CFG, COST_CFG)

    assert new_pos is not None
    assert new_pos.partial_taken is True
    assert new_pos.current_stop == pytest.approx(100.0)  # 본전 이동
    assert new_pos.qty == pytest.approx(7.5)  # 75% 잔존
    assert len(trades) == 1
    assert trades[0].exit_reason == "partial_tp"
    assert trades[0].qty == pytest.approx(2.5)


def test_trend_position_chandelier_trail_tightens_stop_after_partial():
    position = _long_position()
    position.partial_taken = True
    position.current_stop = 100.0
    bar = _bar("2024-01-01 01:00", 105, 106, 104.5, 105.5)
    new_pos, trades, pnl = _manage_trend_position(
        position, bar, pd.Series({"chandelier_stop": 102.0}), TREND_CFG, COST_CFG,
    )
    assert new_pos.current_stop == pytest.approx(102.0)  # 트레일이 본전보다 높으면 갱신
    assert trades == []


def test_trend_position_chandelier_never_loosens_stop():
    position = _long_position()
    position.partial_taken = True
    position.current_stop = 102.0
    bar = _bar("2024-01-01 01:00", 103, 104.0, 103.0, 103.5)  # 저가(103)가 스탑(102) 위라 청산 안 됨
    new_pos, trades, pnl = _manage_trend_position(
        position, bar, pd.Series({"chandelier_stop": 99.0}), TREND_CFG, COST_CFG,
    )
    assert new_pos is not None
    assert new_pos.current_stop == pytest.approx(102.0)  # 트레일(99)이 더 낮으면 무시


def test_trend_position_time_exit_when_r_below_threshold():
    position = _long_position()
    position.bars_held = 95
    bar = _bar("2024-01-01 23:45", 100.5, 100.8, 100.2, 100.5)  # r=(100.5-100)/2=0.25 < 1.0
    new_pos, trades, pnl = _manage_trend_position(position, bar, pd.Series({"chandelier_stop": None}), TREND_CFG, COST_CFG)
    assert new_pos is None
    assert trades[0].exit_reason == "time_stop"


def test_trend_position_no_time_exit_before_bar_threshold():
    position = _long_position()
    position.bars_held = 94
    bar = _bar("2024-01-01 23:30", 100.5, 100.8, 100.2, 100.5)
    new_pos, trades, pnl = _manage_trend_position(position, bar, pd.Series({"chandelier_stop": None}), TREND_CFG, COST_CFG)
    assert new_pos is not None
    assert trades == []


# --- 평균회귀 포지션 생애주기 (SPEC 2.3 골든케이스) ---

def _meanrev_long_position(target=105.0) -> Position:
    return Position(
        symbol="ETH/USDT:USDT", strategy="meanrev", direction="long",
        entry_time=pd.Timestamp("2024-01-01"), entry_price=100.0, qty=5.0,
        initial_stop=97.6, current_stop=97.6, initial_stop_distance=2.4,
        target_price=target,
    )


def test_meanrev_position_stop_loss():
    position = _meanrev_long_position()
    bar = _bar("2024-01-01 00:15", 98, 98.5, 97.0, 97.5)
    new_pos, trades, pnl = _manage_meanrev_position(position, bar, "RANGE", MEANREV_CFG, COST_CFG)
    assert new_pos is None
    assert trades[0].exit_reason == "stop_loss"


def test_meanrev_position_profit_target():
    position = _meanrev_long_position(target=105.0)
    bar = _bar("2024-01-01 00:15", 104, 105.5, 103.5, 105.0)
    new_pos, trades, pnl = _manage_meanrev_position(position, bar, "RANGE", MEANREV_CFG, COST_CFG)
    assert new_pos is None
    assert trades[0].exit_reason == "profit_target"


def test_meanrev_position_regime_flip_exit():
    position = _meanrev_long_position()
    bar = _bar("2024-01-01 00:15", 100, 100.5, 99.5, 100.2)  # 스탑/타겟 둘 다 안 닿음
    new_pos, trades, pnl = _manage_meanrev_position(position, bar, "TREND", MEANREV_CFG, COST_CFG)
    assert new_pos is None
    assert trades[0].exit_reason == "regime_flip"


def test_meanrev_position_time_exit():
    position = _meanrev_long_position()
    position.bars_held = 23
    bar = _bar("2024-01-01 06:00", 100, 100.5, 99.5, 100.2)
    new_pos, trades, pnl = _manage_meanrev_position(position, bar, "RANGE", MEANREV_CFG, COST_CFG)
    assert new_pos is None
    assert trades[0].exit_reason == "time_stop"


def test_meanrev_position_holds_when_nothing_triggers():
    position = _meanrev_long_position()
    bar = _bar("2024-01-01 00:15", 100, 100.5, 99.5, 100.2)
    new_pos, trades, pnl = _manage_meanrev_position(position, bar, "RANGE", MEANREV_CFG, COST_CFG)
    assert new_pos is not None
    assert trades == []


# --- 스탑 그레이스 피리어드 (2026-07-24 재설계, 옵션 1) ---

GRACE_CFG = {**TREND_CFG, "stop_grace_period_bars": 4}


def test_grace_period_suppresses_stop_within_window():
    position = _long_position()  # entry=100, stop=98
    bar = _bar("2024-01-01 00:15", 99, 99.5, 97.0, 98.5)  # low(97) <= stop(98) -> 평소면 즉시 스탑
    new_pos, trades, pnl = _manage_trend_position(position, bar, pd.Series({"chandelier_stop": None}), GRACE_CFG, COST_CFG)
    assert new_pos is not None  # bars_held=1 <= grace(4) -> 스탑 체크 자체를 건너뜀
    assert trades == []
    assert new_pos.bars_held == 1


def test_grace_period_stop_still_applies_after_window_expires():
    position = _long_position()
    position.bars_held = 4  # 다음 호출에서 5가 됨 -> grace(4) 초과
    bar = _bar("2024-01-01 01:15", 99, 99.5, 97.0, 98.5)
    new_pos, trades, pnl = _manage_trend_position(position, bar, pd.Series({"chandelier_stop": None}), GRACE_CFG, COST_CFG)
    assert new_pos is None
    assert trades[0].exit_reason == "stop_loss"


def test_grace_period_default_zero_behaves_like_before():
    """stop_grace_period_bars가 config에 없으면(기존 테스트/설정) 그레이스 없이
    즉시 스탑 체크 — 회귀 방지."""
    position = _long_position()
    bar = _bar("2024-01-01 00:15", 99, 99.5, 97.0, 98.5)
    new_pos, trades, pnl = _manage_trend_position(position, bar, pd.Series({"chandelier_stop": None}), TREND_CFG, COST_CFG)
    assert new_pos is None
    assert trades[0].exit_reason == "stop_loss"


def test_grace_period_does_not_suppress_partial_tp():
    """그레이스 피리어드는 스탑에만 적용 — 그레이스 기간 중에도 1.5R 도달 시
    부분청산은 정상 동작해야 한다."""
    position = _long_position()  # entry=100, stop=98, stop_distance=2 -> 1.5R=103
    bar = _bar("2024-01-01 00:15", 100, 103.5, 99.5, 103.0)  # 스탑 안 닿음, 1.5R 터치
    new_pos, trades, pnl = _manage_trend_position(position, bar, pd.Series({"chandelier_stop": None}), GRACE_CFG, COST_CFG)
    assert new_pos is not None
    assert new_pos.partial_taken is True
    assert trades[0].exit_reason == "partial_tp"
