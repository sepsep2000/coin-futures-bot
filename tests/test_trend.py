import numpy as np
import pandas as pd
import pytest

from src.strategy.trend import generate_trend_signals, time_exit_triggered, trailing_stop

CFG = {
    "donchian_period": 20, "ema_period": 50, "atr_period": 14, "atr_median_window": 96,
    "low_vol_filter": True, "stop_atr_mult": 2.0,
    "chandelier_period": 22, "chandelier_atr_mult": 3.0,
    "time_exit_bars": 96, "time_exit_min_r": 1.0,
}


def _breakout_df(direction: str = "long") -> pd.DataFrame:
    n = 150
    rng = np.random.default_rng(2)
    close = 100 + rng.normal(0, 0.1, n)
    if direction == "long":
        close[-1] = 110.0
        high = close + 0.1
        low = close - 0.1
        high[-1], low[-1] = 110.5, 109.0
    else:
        close[-1] = 90.0
        high = close + 0.1
        low = close - 0.1
        high[-1], low[-1] = 91.0, 89.5
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close})


# --- Donchian 브레이크아웃 골든케이스 ---

def test_entry_long_only_on_breakout_bar():
    df = _breakout_df("long")
    sig = generate_trend_signals(df, CFG)
    assert sig["entry_long"].iloc[-1] is np.True_ or sig["entry_long"].iloc[-1] is True
    assert not sig["entry_long"].iloc[:-1].any()  # 그 이전엔 돌파가 없었음
    assert not sig["entry_short"].any()


def test_entry_short_only_on_breakdown_bar():
    df = _breakout_df("short")
    sig = generate_trend_signals(df, CFG)
    assert bool(sig["entry_short"].iloc[-1]) is True
    assert not sig["entry_short"].iloc[:-1].any()
    assert not sig["entry_long"].any()


def test_initial_stop_known_answer():
    df = _breakout_df("long")
    sig = generate_trend_signals(df, CFG)
    last_close = df["close"].iloc[-1]
    last_atr = sig["atr"].iloc[-1]
    expected_stop = last_close - CFG["stop_atr_mult"] * last_atr
    assert sig["initial_stop_long"].iloc[-1] == pytest.approx(expected_stop)


def test_low_vol_filter_blocks_entry_despite_breakout():
    """돌파 조건(종가>Donchian 상단, 종가>EMA)은 성립해도 ATR이 최근 96봉 중위값보다
    낮으면(저변동 휩쏘 가능성) 진입하지 않는다(SPEC 2.2)."""
    n = 150
    rng = np.random.default_rng(3)
    phase1 = 100 + np.cumsum(rng.normal(0, 1.0, 100))  # 고변동 구간 -> ATR 중위값을 높게 형성
    phase2 = phase1[-1] + np.arange(1, 51) * 0.05       # 저변동 완만한 상승
    close = np.concatenate([phase1, phase2])
    spread = np.concatenate([np.full(100, 1.0), np.full(50, 0.05)])
    df = pd.DataFrame({"open": close, "high": close + spread, "low": close - spread, "close": close})

    sig = generate_trend_signals(df, CFG)
    breakout_condition = (df["close"] > sig["donchian_upper"]) & (df["close"] > sig["ema"])
    assert breakout_condition.tail(10).any()  # 돌파 조건 자체는 몇 차례 성립
    assert not sig["entry_long"].tail(10).any()  # 그러나 저변동 필터로 전부 차단됨


def test_low_vol_filter_disabled_allows_entry():
    n = 150
    rng = np.random.default_rng(3)
    phase1 = 100 + np.cumsum(rng.normal(0, 1.0, 100))
    phase2 = phase1[-1] + np.arange(1, 51) * 0.05
    close = np.concatenate([phase1, phase2])
    spread = np.concatenate([np.full(100, 1.0), np.full(50, 0.05)])
    df = pd.DataFrame({"open": close, "high": close + spread, "low": close - spread, "close": close})

    cfg_no_filter = {**CFG, "low_vol_filter": False}
    sig = generate_trend_signals(df, cfg_no_filter)
    breakout_condition = (df["close"] > sig["donchian_upper"]) & (df["close"] > sig["ema"])
    assert (sig["entry_long"] == breakout_condition.fillna(False)).all()


def test_entry_false_during_warmup():
    df = _breakout_df("long").iloc[:30]  # ema_period=50에 못 미침
    sig = generate_trend_signals(df, CFG)
    assert not sig["entry_long"].any()
    assert not sig["entry_short"].any()


# --- 룩어헤드 방지 ---

def test_generate_trend_signals_is_lookahead_free():
    df = _breakout_df("long")
    full = generate_trend_signals(df, CFG)
    truncated = generate_trend_signals(df.iloc[:-1], CFG)
    pd.testing.assert_frame_equal(full.iloc[:-1], truncated, check_dtype=False)


# --- 트레일링 스탑 / 시간 청산 ---

def test_trailing_stop_matches_chandelier_formula():
    df = _breakout_df("long")
    result = trailing_stop(df, CFG, "long")
    from src.strategy.indicators import chandelier_stop
    expected = chandelier_stop(df["high"], df["low"], df["close"], CFG["chandelier_period"], CFG["chandelier_atr_mult"], "long")
    pd.testing.assert_series_equal(result, expected)


@pytest.mark.parametrize("bars_held,r_multiple,expected", [
    (96, 0.5, True),   # 96봉 도달 + 1R 미달 -> 청산
    (96, 1.0, False),  # 1R 도달 -> 청산 안 함
    (95, 0.5, False),  # 아직 96봉 안 됨 -> 청산 안 함
])
def test_time_exit_triggered_boundaries(bars_held, r_multiple, expected):
    assert time_exit_triggered(bars_held, r_multiple, CFG) is expected


# --- 후보 A: 진입 확인봉 (2026-07-24 재설계, SPEC 4절 — 휩쏘 대응) ---

CONFIRM_CFG = {**CFG, "require_confirmation_bar": True}


def _breakout_then_bar(direction: str, next_close: float) -> pd.DataFrame:
    """돌파봉 뒤에 확인봉 1개를 추가한 픽스처. next_close가 돌파봉이 만든
    채널 레벨 위/아래를 유지하는지에 따라 확인 성공/실패를 나눈다."""
    base = _breakout_df(direction)
    extra_close = next_close
    extra_high, extra_low = extra_close + 0.1, extra_close - 0.1
    extra = pd.DataFrame({"open": [extra_close], "high": [extra_high], "low": [extra_low], "close": [extra_close]})
    return pd.concat([base, extra], ignore_index=True)


def test_confirmation_bar_disabled_enters_on_breakout_bar_itself():
    """require_confirmation_bar=False(기본값)면 기존 동작 그대로 — 돌파봉 즉시 진입."""
    df = _breakout_df("long")
    sig = generate_trend_signals(df, CFG)
    assert bool(sig["entry_long"].iloc[-1]) is True


def test_confirmation_bar_enabled_does_not_enter_on_breakout_bar():
    df = _breakout_df("long")
    sig = generate_trend_signals(df, CONFIRM_CFG)
    assert not sig["entry_long"].any()  # 확인봉이 아직 없어 진입 자체가 없음


def test_confirmation_bar_enters_when_next_bar_holds_above_level():
    df = _breakout_then_bar("long", next_close=111.0)  # 돌파봉 채널 상단(~100.3) 위 유지
    sig = generate_trend_signals(df, CONFIRM_CFG)
    assert bool(sig["entry_long"].iloc[-1]) is True   # 확인봉에서 진입
    assert bool(sig["entry_long"].iloc[-2]) is False  # 돌파봉 자체는 진입 안 함


def test_confirmation_bar_rejects_when_next_bar_reverses_below_level():
    df = _breakout_then_bar("long", next_close=100.2)  # 돌파봉 채널 상단(~100.3) 아래로 복귀
    sig = generate_trend_signals(df, CONFIRM_CFG)
    assert not sig["entry_long"].any()  # 확인 실패 -> 휩쏘로 판단, 진입 없음


def test_confirmation_bar_short_symmetric():
    df = _breakout_df("short")
    sig_disabled = generate_trend_signals(df, CFG)
    sig_confirm = generate_trend_signals(df, CONFIRM_CFG)
    assert bool(sig_disabled["entry_short"].iloc[-1]) is True
    assert not sig_confirm["entry_short"].any()  # 확인봉 없음
