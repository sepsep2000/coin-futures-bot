import pytest

from src.risk import (
    can_open_new_position,
    consecutive_error_kill_switch_triggered,
    daily_loss_limit_breached,
    position_size,
    verify_stop_exists,
)


# --- position_size (SPEC 2.4 수기 계산) ---

def test_position_size_known_answer():
    # risk_amount = 3000*0.5/100 = 15, stop_distance = |100-98| = 2 -> qty = 7.5
    qty = position_size(3000, 100, 98, 0.5)
    assert qty == pytest.approx(7.5)


def test_position_size_short_direction_uses_abs_distance():
    qty = position_size(3000, 100, 102, 0.5)  # 숏: 스탑이 진입가보다 위
    assert qty == pytest.approx(7.5)


def test_position_size_rounds_down_to_qty_step():
    qty = position_size(3000, 100, 98, 0.5, qty_step=1.0)
    assert qty == pytest.approx(7.0)  # floor(7.5)


def test_position_size_zero_when_below_min_qty():
    qty = position_size(3000, 100, 98, 0.5, min_qty=8.0)
    assert qty == 0.0


def test_position_size_rejects_non_positive_equity():
    with pytest.raises(ValueError):
        position_size(0, 100, 98, 0.5)
    with pytest.raises(ValueError):
        position_size(-100, 100, 98, 0.5)


def test_position_size_rejects_zero_stop_distance():
    with pytest.raises(ValueError):
        position_size(3000, 100, 100, 0.5)


def test_position_size_capped_by_max_leverage():
    """스탑이 아주 좁으면(저변동기) 리스크 기준 사이징만으로 레버리지 한도를
    넘어설 수 있다 — max_leverage가 주어지면 그쪽으로 캡핑된다."""
    # 리스크 기준: 15*0.5/100=15/0.1=150 -> notional=150*100=15000 (계좌 3000의 5배, 2x 한도 초과)
    qty = position_size(3000, 100, 99.9, 0.5, max_leverage=2.0)
    max_qty_by_leverage = (3000 * 2.0) / 100  # 60
    assert qty == pytest.approx(max_qty_by_leverage)


def test_position_size_not_affected_by_leverage_when_within_limit():
    qty = position_size(3000, 100, 98, 0.5, max_leverage=2.0)  # 기존 골든케이스: qty=7.5, notional=750 < 6000
    assert qty == pytest.approx(7.5)


# --- daily_loss_limit_breached (SPEC 1: -3% 킬스위치) ---

def test_daily_loss_limit_breached_at_exact_threshold():
    assert daily_loss_limit_breached(-90.0, 3000, -3.0) is True


def test_daily_loss_limit_not_breached_just_above_threshold():
    assert daily_loss_limit_breached(-89.99, 3000, -3.0) is False


def test_daily_loss_limit_not_breached_when_profitable():
    assert daily_loss_limit_breached(50.0, 3000, -3.0) is False


# --- can_open_new_position (SPEC 1: 동시 최대 3, 페어당 1) ---

def test_can_open_new_position_blocks_duplicate_symbol():
    assert can_open_new_position({"BTC/USDT:USDT"}, "BTC/USDT:USDT", max_concurrent=3) is False


def test_can_open_new_position_allows_under_limit():
    assert can_open_new_position({"BTC/USDT:USDT", "ETH/USDT:USDT"}, "SOL/USDT:USDT", max_concurrent=3) is True


def test_can_open_new_position_blocks_at_max_concurrent():
    open_symbols = {"BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"}
    assert can_open_new_position(open_symbols, "ADA/USDT:USDT", max_concurrent=3) is False


# --- verify_stop_exists / consecutive_error_kill_switch_triggered ---

def test_verify_stop_exists_ok_when_no_position():
    assert verify_stop_exists(has_open_position=False, has_stop_order=False) is True


def test_verify_stop_exists_ok_when_stop_present():
    assert verify_stop_exists(has_open_position=True, has_stop_order=True) is True


def test_verify_stop_exists_fails_when_position_without_stop():
    assert verify_stop_exists(has_open_position=True, has_stop_order=False) is False


@pytest.mark.parametrize("count,expected", [(4, False), (5, True), (6, True)])
def test_consecutive_error_kill_switch_boundaries(count, expected):
    assert consecutive_error_kill_switch_triggered(count, threshold=5) is expected
