import pytest

from src.backtest.cost_model import (
    entry_fill_price,
    exit_fill_price,
    funding_fee,
    maker_fee,
    slippage_cost,
    taker_fee,
)


def test_taker_fee_known_answer():
    assert taker_fee(10_000, 0.05) == pytest.approx(5.0)


def test_maker_fee_known_answer():
    assert maker_fee(10_000, 0.02) == pytest.approx(2.0)


def test_slippage_cost_known_answer():
    assert slippage_cost(10_000, 0.03) == pytest.approx(3.0)


def test_funding_fee_long_pays_positive_rate():
    assert funding_fee(10_000, 0.0001, "long") == pytest.approx(1.0)


def test_funding_fee_short_receives_positive_rate():
    assert funding_fee(10_000, 0.0001, "short") == pytest.approx(-1.0)


def test_funding_fee_rejects_invalid_direction():
    with pytest.raises(ValueError):
        funding_fee(10_000, 0.0001, "sideways")


def test_entry_fill_price_long_market_is_worse_than_reference():
    price = entry_fill_price(100.0, "long", slippage_pct=0.03)
    assert price == pytest.approx(100.03)


def test_entry_fill_price_short_market_is_worse_than_reference():
    price = entry_fill_price(100.0, "short", slippage_pct=0.03)
    assert price == pytest.approx(99.97)


def test_entry_fill_price_limit_ignores_slippage():
    assert entry_fill_price(100.0, "long", slippage_pct=0.03, order_type="limit") == pytest.approx(100.0)


def test_exit_fill_price_long_market_is_worse_than_reference():
    price = exit_fill_price(100.0, "long", slippage_pct=0.03)
    assert price == pytest.approx(99.97)


def test_exit_fill_price_short_market_is_worse_than_reference():
    price = exit_fill_price(100.0, "short", slippage_pct=0.03)
    assert price == pytest.approx(100.03)


def test_exit_fill_price_limit_ignores_slippage():
    assert exit_fill_price(100.0, "short", slippage_pct=0.03, order_type="limit") == pytest.approx(100.0)


def test_fill_price_rejects_invalid_direction():
    with pytest.raises(ValueError):
        entry_fill_price(100.0, "sideways", 0.03)
    with pytest.raises(ValueError):
        exit_fill_price(100.0, "sideways", 0.03)
