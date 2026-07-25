"""strategies/portfolio_combined.py — 2a + filtered_trend 2다리 결합, 정식.

배분은 config.yaml의 portfolio.weights(vol-parity, 1회 산출된 고정값)를
그대로 쓴다 — 여기서 재계산/재탐색하지 않는다(절대 금지 사항). 결합 방식은
scripts/diag/portfolio_2a_filteredtrend_backtest.py의 build_portfolio와
동일한 고정가중 주간 리밸런스(constant-mix, 복리) 로직을 그대로 이식.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

import strategies.filtered_trend as filtered_trend_strategy
from scripts.diag.common import trades_to_df
from src.backtest.engine import SymbolData


def _leg1_weekly_return(data: SymbolData, cfg: dict, total_capital: float) -> pd.Series:
    """filtered_trend의 실제 pnl_usd(엔진 비용모델 이미 반영)를 total_capital
    기준 %수익률로 환산 — position_size가 equity의 순수 선형함수라 스케일
    불변(PORTFOLIO_2A_FILTEREDTREND_BACKTEST.md 근거와 동일 논리)."""
    result = filtered_trend_strategy.run(data, cfg)
    df = trades_to_df(result.trades)
    if df.empty:
        return pd.Series(dtype=float)
    weekly_pnl = df.groupby(pd.Grouper(key="exit_time", freq="W"))["pnl_usd"].sum()
    return weekly_pnl / total_capital


def build(leg1_weekly_return: pd.Series, leg2_weekly_return: pd.Series, weights: dict, total_capital: float) -> dict:
    w1 = weights["filtered_trend"]
    w2 = weights["2a"]
    full_idx = leg1_weekly_return.index.union(leg2_weekly_return.index).sort_values()
    leg1_full = leg1_weekly_return.reindex(full_idx, fill_value=0.0)
    leg2_full = leg2_weekly_return.reindex(full_idx, fill_value=0.0)
    port_return = w1 * leg1_full + w2 * leg2_full

    equity_vals = [total_capital]
    for r in port_return:
        equity_vals.append(equity_vals[-1] * (1 + r))
    start_date = full_idx[0] - (full_idx[1] - full_idx[0]) if len(full_idx) > 1 else full_idx[0] - pd.Timedelta(weeks=1)
    dates = [start_date] + list(full_idx)
    equity_curve = list(zip(dates, equity_vals))

    return {"weekly_return": port_return, "equity_curve": equity_curve, "weights": {"filtered_trend": w1, "2a": w2}}


def run(data: SymbolData, cfg: dict, project_root: Path) -> dict:
    import importlib
    strategy_2a = importlib.import_module("strategies.2a")

    total_capital = float(cfg["portfolio"]["total_capital_usd"])
    weights = cfg["portfolio"]["weights"]

    leg1 = _leg1_weekly_return(data, cfg, total_capital)
    leg2_df = strategy_2a.run(cfg, project_root)
    leg2 = pd.Series(leg2_df["net_return"].to_numpy(), index=pd.DatetimeIndex(leg2_df["exit_time"]))

    return build(leg1, leg2, weights, total_capital)
