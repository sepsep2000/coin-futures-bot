"""vol-parity 가중치 정정(POST_FILTER_BUG_AUDIT.md) 반영 후 결합 포트폴리오
성과지표 재계산. config.yaml의 portfolio.weights를 이미 정정값(83.8:16.2)으로
갱신한 상태에서 실행 — 여기서 가중치를 다시 계산하지 않는다(재탐색 금지).
scripts/diag/official_gate_check.py의 _metrics_from_weekly를 그대로 재사용.
"""

from __future__ import annotations

import pandas as pd

from scripts.diag.common import DIAG_DATA_DIR, eth_data, load_base_config
from scripts.diag.official_gate_check import _metrics_from_weekly
import strategies.portfolio_combined as portfolio_combined_strategy

PROJECT_ROOT = DIAG_DATA_DIR.parent.parent


def run() -> dict:
    cfg = load_base_config()
    gates_cfg = cfg["gates"]
    data = eth_data()["ETH/USDT:USDT"]
    total_capital = float(cfg["portfolio"]["total_capital_usd"])

    leg1_weekly = portfolio_combined_strategy._leg1_weekly_return(data, cfg, total_capital)
    import importlib
    strategy_2a = importlib.import_module("strategies.2a")
    leg2_df = strategy_2a.run(cfg, PROJECT_ROOT)
    leg2_weekly = pd.Series(leg2_df["net_return"].to_numpy(), index=pd.DatetimeIndex(leg2_df["exit_time"]))

    scenarios = {
        "corrected_vol_parity_83.8_16.2": cfg["portfolio"]["weights"],  # config.yaml에 이미 반영된 정정값
        "old_contaminated_vol_parity_85.3_14.7": {"filtered_trend": 0.8531653464429574, "2a": 0.14683465355704262},
        "50_50": {"filtered_trend": 0.5, "2a": 0.5},
    }

    rows = []
    for label, weights in scenarios.items():
        port = portfolio_combined_strategy.build(leg1_weekly, leg2_weekly, weights, total_capital)
        m = _metrics_from_weekly(port["weekly_return"], port["equity_curve"], gates_cfg, total_capital)
        rows.append({"config": label, "w_filtered_trend": weights["filtered_trend"], "w_2a": weights["2a"], **m})

    df = pd.DataFrame(rows)
    df.to_csv(DIAG_DATA_DIR / "vol_parity_recalc_comparison.csv", index=False)
    return {"table": rows}


if __name__ == "__main__":
    out = run()
    for row in out["table"]:
        print(row)
