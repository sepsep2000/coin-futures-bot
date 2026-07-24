"""scripts/diag/common.py — 사후 진단(post-mortem) 공용 유틸리티.

읽기 전용 진단 전용 모듈. src/의 기존 전략/엔진 코드는 임포트해서 그대로
재사용만 하고 수정하지 않는다(TASK 절대 금지 사항 준수).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.backtest.engine import SymbolData, Trade, run_backtest  # noqa: E402
from src.backtest.report import compute_profit_factor, compute_win_rate_pct  # noqa: E402

DIAG_DATA_DIR = PROJECT_ROOT / "reports" / "diag_data"
DIAG_DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_base_config() -> dict:
    with open(PROJECT_ROOT / "config" / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_symbol_data(symbol_file: str) -> SymbolData:
    o15 = pd.read_parquet(PROJECT_ROOT / "data" / "ohlcv" / f"{symbol_file}_15m.parquet")
    o1h = pd.read_parquet(PROJECT_ROOT / "data" / "ohlcv" / f"{symbol_file}_1h.parquet")
    fd = pd.read_parquet(PROJECT_ROOT / "data" / "funding" / f"{symbol_file}_funding.parquet")

    def conv(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df.index = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df.drop(columns=["timestamp"])

    return SymbolData(df_15m=conv(o15), df_1h=conv(o1h), funding=fd)


def symbol_file(pair: str) -> str:
    return pair.replace("/", "").replace(":", "-")


def trend_cfg_current() -> dict:
    """현재 확정 설정(config.yaml) 그대로 — ETH 단독, trend만, 확인봉+그레이스피리어드 포함."""
    cfg = load_base_config()
    assert cfg["active_strategies"] == ["trend"]
    assert cfg["exchange"]["pairs"] == ["ETH/USDT:USDT"]
    return cfg


def trend_cfg_no_grace() -> dict:
    """옵션 1(그레이스 피리어드) 적용 이전 상태 — 비교용. config.yaml 파일 자체는 건드리지 않고
    메모리상 dict만 복사해서 grace=0으로 되돌린다."""
    import copy
    cfg = copy.deepcopy(trend_cfg_current())
    cfg["trend"]["stop_grace_period_bars"] = 0
    return cfg


def meanrev_cfg_current() -> dict:
    """meanrev를 ETH 단독에서 재현하기 위한 설정 — config.yaml은 안 건드리고
    active_strategies만 메모리상에서 교체."""
    import copy
    cfg = copy.deepcopy(load_base_config())
    cfg["active_strategies"] = ["meanrev"]
    return cfg


def cfg_3pair_baseline() -> dict:
    """Phase 2 최초 베이스라인 재현(참고용) — BTC+ETH+SOL, trend+meanrev 둘 다,
    확인봉/그레이스 피리어드 없음(당시엔 없었음)."""
    import copy
    cfg = copy.deepcopy(load_base_config())
    cfg["active_strategies"] = ["trend", "meanrev"]
    cfg["exchange"]["pairs"] = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
    cfg["trend"]["require_confirmation_bar"] = False
    cfg["trend"]["stop_grace_period_bars"] = 0
    return cfg


def eth_data() -> dict[str, SymbolData]:
    return {"ETH/USDT:USDT": load_symbol_data("ETHUSDT-USDT")}


def all3_data() -> dict[str, SymbolData]:
    return {p: load_symbol_data(symbol_file(p)) for p in ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT")}


def trades_to_df(trades: list[Trade]) -> pd.DataFrame:
    return pd.DataFrame([{
        "symbol": t.symbol, "strategy": t.strategy, "direction": t.direction,
        "entry_time": t.entry_time, "exit_time": t.exit_time,
        "entry_price": t.entry_price, "exit_price": t.exit_price, "qty": t.qty,
        "pnl_usd": t.pnl_usd, "r_multiple": t.r_multiple, "exit_reason": t.exit_reason,
        "fees_usd": t.fees_usd, "funding_usd": t.funding_usd,
        "bars_held": round((t.exit_time - t.entry_time).total_seconds() / 900),
    } for t in trades])


def stitch_equity(trades: list[Trade], initial_equity: float) -> list[tuple]:
    sorted_trades = sorted(trades, key=lambda t: t.exit_time)
    equity = initial_equity
    curve = [(sorted_trades[0].entry_time, equity)] if sorted_trades else []
    for t in sorted_trades:
        equity += t.pnl_usd
        curve.append((t.exit_time, equity))
    return curve


def profit_factor_from_pnls(pnls: np.ndarray) -> float:
    gains = pnls[pnls > 0].sum()
    losses = -pnls[pnls < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else float("nan")
    return float(gains / losses)


_RUN_CACHE: dict[str, list[Trade]] = {}


def get_trend_trades(use_grace: bool = True) -> list[Trade]:
    key = f"trend_{use_grace}"
    if key not in _RUN_CACHE:
        cfg = trend_cfg_current() if use_grace else trend_cfg_no_grace()
        result = run_backtest(eth_data(), cfg)
        _RUN_CACHE[key] = result.trades
    return _RUN_CACHE[key]


def get_meanrev_trades() -> list[Trade]:
    key = "meanrev"
    if key not in _RUN_CACHE:
        result = run_backtest(eth_data(), meanrev_cfg_current())
        _RUN_CACHE[key] = result.trades
    return _RUN_CACHE[key]


def get_3pair_baseline_trades() -> list[Trade]:
    key = "baseline3pair"
    if key not in _RUN_CACHE:
        result = run_backtest(all3_data(), cfg_3pair_baseline())
        _RUN_CACHE[key] = result.trades
    return _RUN_CACHE[key]
