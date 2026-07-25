"""2다리 포트폴리오 백테스트 — 2a(PASS) + trend+1a필터(ACCEPT_AS_FILTER).

신규 백테스트 엔진을 만들지 않는다 — src/backtest/report.py(Sharpe/MaxDD)와
src/backtest/walkforward.py(몬테카를로)의 기존 함수를 그대로 재사용한다.
PF는 프로젝트 전역에서 이미 써온 profit_factor_from_pnls(gains/losses 비율)
관례를 그대로 따른다.

★ 재사용 정당성(엔진 재실행 없이 기존 트레이드를 스케일링해도 되는 이유):
src/risk.py의 position_size()는 qty_step/min_qty 없이(engine.py 호출부 확인,
둘 다 미전달) equity의 순수 선형함수다 — 레버리지 캡(equity×max_leverage)도
equity에 비례해 스케일되므로 꺾이는 지점이 없다. 즉 초기자본을 k배 하면
전체 equity 경로(모든 시점의 pnl_usd)가 정확히 k배 스케일된다. 따라서
trend+1a필터의 기존 $10,000(=TOTAL_CAPITAL, config.yaml 플레이스홀더 재사용)
기준 pnl_usd를 배분비율만큼 스케일링하는 것으로 "그 비율만큼 배분했을 때"의
성과를 정확히 재현할 수 있다(근사가 아니라 엔진의 선형성에서 나오는 등가
변환) — 재실행 불필요.

포트폴리오 구성: 고정가중 주간 리밸런스(constant-mix) — 매주
portfolio_return = w1*leg1_return + w2*leg2_return, compounding.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from scripts.diag.common import DIAG_DATA_DIR, load_base_config, profit_factor_from_pnls
from src.backtest.report import compute_max_drawdown_pct, compute_sharpe_annualized
from src.backtest.walkforward import monte_carlo_max_drawdowns

PROJECT_ROOT = DIAG_DATA_DIR.parent.parent
SIGNAL_DATA_DIR = PROJECT_ROOT / "reports" / "signal_validation_data"
TOTAL_CAPITAL = 10000.0  # config.yaml의 기존 initial_equity_usd 플레이스홀더 재사용(신규 확정 아님)
WEEKS_PER_YEAR = 52


def load_leg1_weekly_return() -> pd.Series:
    """trend+1a필터 — 기존 $10,000 기준 pnl_usd를 그대로 주간 합산 후
    TOTAL_CAPITAL로 나눠 %수익률로 변환(스케일 불변, 위 docstring 참조)."""
    df = pd.read_csv(SIGNAL_DATA_DIR / "1a" / "1a_trades.csv", parse_dates=["exit_time"])
    weekly_pnl = df.groupby(pd.Grouper(key="exit_time", freq="W"))["pnl_usd"].sum()
    return weekly_pnl / TOTAL_CAPITAL


def load_leg2_weekly_return() -> pd.Series:
    return pd.read_csv(SIGNAL_DATA_DIR / "2a" / "2a_weekly_returns.csv", index_col=0, parse_dates=True)["weekly_return"]


def build_portfolio(leg1: pd.Series, leg2: pd.Series, w1: float, w2: float) -> dict:
    full_idx = leg1.index.union(leg2.index).sort_values()
    leg1_full = leg1.reindex(full_idx, fill_value=0.0)
    leg2_full = leg2.reindex(full_idx, fill_value=0.0)
    port_return = w1 * leg1_full + w2 * leg2_full

    equity_vals = [TOTAL_CAPITAL]
    for r in port_return:
        equity_vals.append(equity_vals[-1] * (1 + r))
    start_date = full_idx[0] - (full_idx[1] - full_idx[0]) if len(full_idx) > 1 else full_idx[0] - pd.Timedelta(weeks=1)
    dates = [start_date] + list(full_idx)
    equity_curve = list(zip(dates, equity_vals))

    return {"weekly_return": port_return, "equity_curve": equity_curve}


def compute_metrics(portfolio: dict, gates_cfg: dict, seed: int = 42) -> dict:
    weekly_return = portfolio["weekly_return"]
    equity_curve = portfolio["equity_curve"]

    pf = profit_factor_from_pnls(weekly_return.to_numpy())
    sharpe = compute_sharpe_annualized(equity_curve, bars_per_year=WEEKS_PER_YEAR)
    maxdd = compute_max_drawdown_pct(equity_curve)

    total_return = equity_curve[-1][1] / equity_curve[0][1] - 1
    n_years = (equity_curve[-1][0] - equity_curve[0][0]).days / 365.25
    ann_return_pct = ((1 + total_return) ** (1 / n_years) - 1) * 100 if n_years > 0 else float("nan")
    calmar = ann_return_pct / maxdd if maxdd else float("nan")

    weekly_dollar_pnl = weekly_return * TOTAL_CAPITAL
    pseudo_trades = [SimpleNamespace(pnl_usd=float(v)) for v in weekly_dollar_pnl.to_numpy()]
    maxdds = monte_carlo_max_drawdowns(pseudo_trades, TOTAL_CAPITAL, gates_cfg["g3_monte_carlo_runs"], seed=seed)
    mc95 = float(np.percentile(maxdds, 95)) if maxdds.size else float("nan")

    return {
        "n_periods": len(weekly_return), "pf": pf, "sharpe_annualized": sharpe,
        "annualized_return_pct": ann_return_pct, "maxdd_pct": maxdd, "calmar": calmar,
        "mc_maxdd_95pct": mc95,
    }


def run() -> dict:
    cfg = load_base_config()
    leg1 = load_leg1_weekly_return()
    leg2 = load_leg2_weekly_return()

    sigma1 = leg1.reindex(leg1.index.union(leg2.index), fill_value=0.0).std(ddof=1)
    sigma2 = leg2.reindex(leg1.index.union(leg2.index), fill_value=0.0).std(ddof=1)
    w1_vp = (1 / sigma1) / (1 / sigma1 + 1 / sigma2)
    w2_vp = 1 - w1_vp

    configs = {
        "leg1_alone": (1.0, 0.0),
        "leg2_alone": (0.0, 1.0),
        "combined_50_50": (0.5, 0.5),
        "combined_vol_parity": (float(w1_vp), float(w2_vp)),
    }

    rows = []
    for label, (w1, w2) in configs.items():
        port = build_portfolio(leg1, leg2, w1, w2)
        metrics = compute_metrics(port, cfg["gates"])
        rows.append({"config": label, "w1_trend_filtered_1a": w1, "w2_2a": w2, **metrics})
        pd.DataFrame({"date": [d for d, _ in port["equity_curve"]], "equity": [e for _, e in port["equity_curve"]]}).to_csv(
            DIAG_DATA_DIR / f"portfolio_equity_{label}.csv", index=False
        )

    result_df = pd.DataFrame(rows)
    result_df.to_csv(DIAG_DATA_DIR / "portfolio_2a_filteredtrend_comparison.csv", index=False)
    return {"table": rows, "vol_parity_weights": {"w1": float(w1_vp), "w2": float(w2_vp)}, "sigma1": float(sigma1), "sigma2": float(sigma2)}


if __name__ == "__main__":
    out = run()
    for row in out["table"]:
        print(row)
    print("vol-parity weights:", out["vol_parity_weights"], "sigma1(leg1):", out["sigma1"], "sigma2(leg2):", out["sigma2"])
