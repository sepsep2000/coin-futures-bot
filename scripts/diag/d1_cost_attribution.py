"""D1 — 비용 귀속. 트레이드별 gross/fee/slippage/funding 분해, 비용 배수 스윕,
브레이크이븐 비용 배수. 트레이드 시퀀스는 실제 실행분 그대로 고정하고, 비용만
배수로 재라벨링하는 방식(재시뮬레이션 아님) — cost attribution 목적에 맞다.
슬리피지는 net_pnl에 이미 녹아 있어(체결가 자체가 불리하게 계산됨) fee/funding과
분리해 별도로 스윕하지 않는다 — fee+funding만 배수 스윕 대상으로 명시한다."""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.diag.common import DIAG_DATA_DIR, get_trend_trades, profit_factor_from_pnls, trades_to_df, trend_cfg_current
from src.backtest.report import compute_sharpe_annualized

COST_MULTIPLIERS = [0.0, 0.25, 0.5, 1.0, 1.5, 2.0]


def _sharpe_for_pnls(exit_times: pd.Series, pnls: pd.Series, initial_equity: float):
    order = np.argsort(exit_times.to_numpy())
    equity = initial_equity + pnls.to_numpy()[order].cumsum()
    curve = [(exit_times.iloc[order[0]], initial_equity)] + list(zip(exit_times.iloc[order], equity))
    return compute_sharpe_annualized(curve)


def run() -> dict:
    trades = get_trend_trades(use_grace=True)
    df = trades_to_df(trades)
    cfg = trend_cfg_current()
    initial_equity = cfg["account"]["initial_equity_usd"]

    notional = df["qty"] * df["entry_price"]
    df["notional_usd"] = notional
    df["fee_plus_funding_usd"] = df["fees_usd"] + df["funding_usd"]
    # gross_pnl: 수수료+펀딩 제외(슬리피지는 체결가에 이미 녹아 net_pnl에서 분리 불가 -> gross 정의에서 제외 명시)
    df["gross_pnl_ex_fee_funding"] = df["pnl_usd"] + df["fee_plus_funding_usd"]
    df["cost_bps_of_notional"] = df["fee_plus_funding_usd"] / df["notional_usd"] * 10_000

    # R단위 비용: |r_multiple|>0.01인 트레이드만 대상(0 근방은 스탑거리 역산이 불안정해 제외)
    safe = df["r_multiple"].abs() > 0.01
    stop_distance_est = (df["exit_price"] - df["entry_price"]).abs() / df["r_multiple"].abs().where(safe)
    risk_usd_est = df["qty"] * stop_distance_est
    df["cost_in_R"] = np.where(safe, df["fee_plus_funding_usd"] / risk_usd_est, np.nan)
    n_excluded_for_R = int((~safe).sum())

    df.to_csv(DIAG_DATA_DIR / "d1_trade_cost_breakdown.csv", index=False)

    gross_pf = profit_factor_from_pnls(df["gross_pnl_ex_fee_funding"].to_numpy())
    net_pf = profit_factor_from_pnls(df["pnl_usd"].to_numpy())

    def pf_given_m(m: float) -> float:
        adj_pnl = df["gross_pnl_ex_fee_funding"] - m * df["fee_plus_funding_usd"]
        return profit_factor_from_pnls(adj_pnl.to_numpy())

    sweep_rows = []
    for m in COST_MULTIPLIERS:
        adj_pnl = df["gross_pnl_ex_fee_funding"] - m * df["fee_plus_funding_usd"]
        pf = profit_factor_from_pnls(adj_pnl.to_numpy())
        sharpe = _sharpe_for_pnls(df["exit_time"], adj_pnl, initial_equity)
        sweep_rows.append({"multiplier": m, "pf": pf, "total_pnl_usd": float(adj_pnl.sum()), "sharpe": sharpe})
    sweep_df = pd.DataFrame(sweep_rows)
    sweep_df.to_csv(DIAG_DATA_DIR / "d1_cost_multiplier_sweep.csv", index=False)

    lo, hi = 0.0, 10.0
    breakeven_m = None
    if pf_given_m(lo) >= 1.0 and pf_given_m(hi) <= 1.0:
        for _ in range(60):
            mid = (lo + hi) / 2
            if pf_given_m(mid) >= 1.0:
                lo = mid
            else:
                hi = mid
        breakeven_m = (lo + hi) / 2

    summary = {
        "n_trades": len(df),
        "n_excluded_for_R_cost": n_excluded_for_R,
        "avg_cost_bps_of_notional": float(df["cost_bps_of_notional"].mean()),
        "median_cost_bps_of_notional": float(df["cost_bps_of_notional"].median()),
        "avg_cost_in_R": float(df["cost_in_R"].mean(skipna=True)),
        "median_cost_in_R": float(df["cost_in_R"].median(skipna=True)),
        "gross_pf": gross_pf,
        "net_pf": net_pf,
        "pf_erosion": (gross_pf - net_pf) if np.isfinite(gross_pf) and np.isfinite(net_pf) else None,
        "breakeven_cost_multiplier": breakeven_m,
    }
    pd.Series(summary).to_csv(DIAG_DATA_DIR / "d1_summary.csv")
    return {"summary": summary, "sweep": sweep_rows}


if __name__ == "__main__":
    result = run()
    print(result["summary"])
    for row in result["sweep"]:
        print(row)
