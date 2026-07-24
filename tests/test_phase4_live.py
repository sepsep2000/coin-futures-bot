import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from checks.phase4_live import check_invariants, check_anomalies, check_drift  # noqa: E402


def test_invariant_leverage_out_of_range():
    trades = [{"action": "enter_long", "leverage": 7.0, "stop_price": 100, "symbol": "BTC", "ts": "2026-01-01T00:00:00+00:00"}]
    failures = check_invariants(trades)
    assert any("leverage" in f for f in failures)


def test_invariant_missing_stop():
    trades = [{"action": "enter_short", "leverage": 2.0, "stop_price": None, "symbol": "ETH", "ts": "2026-01-01T00:00:00+00:00"}]
    failures = check_invariants(trades)
    assert any("stop_price" in f for f in failures)


def test_invariant_ok():
    trades = [{"action": "enter_long", "leverage": 2.0, "stop_price": 100, "slippage_bps": 10, "symbol": "SOL", "ts": "2026-01-01T00:00:00+00:00"}]
    assert check_invariants(trades) == []


def test_anomaly_large_loss():
    trades = [{"pnl_realized": -200, "symbol": "BTC", "ts": "2026-01-01T00:00:00+00:00"}]
    failures = check_anomalies(trades, [], account_equity_usd=1000)
    assert any("손실 과다" in f for f in failures)


def test_drift_insufficient_sample():
    trades = [{"action": "exit", "pnl_realized": 10}] * 5
    assert check_drift(trades, backtest_expected_winrate=0.5) == []
