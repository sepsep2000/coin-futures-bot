import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from checks import phase2_backtest, phase3_walkforward  # noqa: E402


def test_phase2_leakage_detects_disallowed_column():
    failures = phase2_backtest.check_no_future_leakage(
        "dummy.module",
        ohlcv_columns_used=["open", "close", "next_close"],
        allowed_columns=["open", "high", "low", "close", "volume"],
    )
    assert any("next_close" in f for f in failures)


def test_phase2_leakage_ok():
    failures = phase2_backtest.check_no_future_leakage(
        "dummy.module",
        ohlcv_columns_used=["open", "close"],
        allowed_columns=["open", "high", "low", "close", "volume"],
    )
    assert failures == []


def test_phase3_missing_results_file_not_a_failure(tmp_path):
    missing_path = str(tmp_path / "nope.json")
    failures = phase3_walkforward.check_is_oos_degradation(missing_path)
    assert len(failures) == 1
    assert "없음" in failures[0]


def test_phase3_degradation_detected(tmp_path):
    p = tmp_path / "walkforward.json"
    p.write_text(json.dumps([{"window": 1, "is_sharpe": 2.0, "oos_sharpe": 0.5}]), encoding="utf-8")
    failures = phase3_walkforward.check_is_oos_degradation(str(p), max_degradation_pct=0.40)
    assert len(failures) == 1
    assert "window 1" in failures[0]


def test_phase3_window_count_insufficient(tmp_path):
    p = tmp_path / "walkforward.json"
    p.write_text(json.dumps([{"window": i, "is_sharpe": 1.0, "oos_sharpe": 0.9} for i in range(2)]), encoding="utf-8")
    failures = phase3_walkforward.check_window_consistency(str(p), min_windows=5)
    assert len(failures) == 1
