"""2다리 포트폴리오 재계산(net) — 2a를 비용반영(net) 버전으로 교체해
필터trend와 재결합. portfolio_2a_filteredtrend_backtest.py(gross)의
build_portfolio/compute_metrics를 그대로 재사용(로직 중복 없음, 신규
엔진 아님) — 2a 데이터 소스만 gross 주간수익률에서 net으로 교체.
"""

from __future__ import annotations

import pandas as pd

from scripts.diag.common import DIAG_DATA_DIR, load_base_config
from scripts.diag.portfolio_2a_filteredtrend_backtest import build_portfolio, compute_metrics, load_leg1_weekly_return

PROJECT_ROOT = DIAG_DATA_DIR.parent.parent


def load_leg2_weekly_return_net() -> pd.Series:
    return pd.read_csv(DIAG_DATA_DIR / "portfolio_2a_net_weekly_returns.csv", index_col=0, parse_dates=True)["weekly_return"]


def run() -> dict:
    cfg = load_base_config()
    leg1 = load_leg1_weekly_return()
    leg2_net = load_leg2_weekly_return_net()

    full_idx = leg1.index.union(leg2_net.index)
    sigma1 = leg1.reindex(full_idx, fill_value=0.0).std(ddof=1)
    sigma2 = leg2_net.reindex(full_idx, fill_value=0.0).std(ddof=1)
    w1_vp = (1 / sigma1) / (1 / sigma1 + 1 / sigma2)
    w2_vp = 1 - w1_vp

    configs = {
        "leg2_alone_net": (0.0, 1.0),
        "combined_50_50_net": (0.5, 0.5),
        "combined_vol_parity_net": (float(w1_vp), float(w2_vp)),
    }

    rows = []
    for label, (w1, w2) in configs.items():
        port = build_portfolio(leg1, leg2_net, w1, w2)
        metrics = compute_metrics(port, cfg["gates"])
        rows.append({"config": label, "w1_trend_filtered_1a": w1, "w2_2a_net": w2, **metrics})
        pd.DataFrame({"date": [d for d, _ in port["equity_curve"]], "equity": [e for _, e in port["equity_curve"]]}).to_csv(
            DIAG_DATA_DIR / f"portfolio_equity_{label}.csv", index=False
        )

    result_df = pd.DataFrame(rows)
    result_df.to_csv(DIAG_DATA_DIR / "portfolio_2a_filteredtrend_comparison_net.csv", index=False)
    return {"table": rows, "vol_parity_weights": {"w1": float(w1_vp), "w2": float(w2_vp)}}


if __name__ == "__main__":
    out = run()
    for row in out["table"]:
        print(row)
    print("vol-parity weights(net):", out["vol_parity_weights"])
