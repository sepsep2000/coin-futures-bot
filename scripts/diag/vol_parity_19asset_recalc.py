"""scripts/diag/vol_parity_19asset_recalc.py — 2a 19자산(ETH 제외, 라이브
정식 채택) 변동성 기준 vol-parity 가중치 재계산 + 결합 포트폴리오 재검증
(reports/VOL_PARITY_19ASSET_RECALC.md 원자료).

가중치 공식은 scripts/diag/portfolio_2a_filteredtrend_backtest_net.py와
완전히 동일(w1 = (1/sigma1)/(1/sigma1+1/sigma2)) — 신규 로직 없음, sigma2만
20자산 net 대신 19자산(ETH 제외) net으로 교체한다. leg1(filtered_trend)은
POST_FILTER_BUG_AUDIT.md 이후 정식 경로인 strategies/portfolio_combined.py
::_leg1_weekly_return()을 그대로 재사용(오염된 진단 데이터 아님). 포트폴리오
결합·게이트 판정은 scripts/diag/official_gate_check.py::_metrics_from_weekly
그대로 재사용(신규 통계 없음).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.diag.common import DIAG_DATA_DIR, eth_data, load_base_config  # noqa: E402
from scripts.diag.official_gate_check import _metrics_from_weekly  # noqa: E402
import strategies.portfolio_combined as portfolio_combined_strategy  # noqa: E402

strategy_2a = importlib.import_module("strategies.2a")


def _run_2a_with_universe_filter(cfg: dict, exclude: set[str]) -> pd.DataFrame:
    """2a_eth_exclusion_impact.py와 동일한 몽키패치 방식 — strategies/2a.py
    자체는 건드리지 않는다."""
    original = strategy_2a._load_universe_daily_and_funding

    def patched(cfg_, project_root_):
        daily_close, funding_raw = original(cfg_, project_root_)
        daily_close = daily_close.drop(columns=[c for c in exclude if c in daily_close.columns])
        funding_raw = {k: v for k, v in funding_raw.items() if k not in exclude}
        return daily_close, funding_raw

    strategy_2a._load_universe_daily_and_funding = patched
    try:
        return strategy_2a.run(cfg, PROJECT_ROOT)
    finally:
        strategy_2a._load_universe_daily_and_funding = original


def run() -> dict:
    cfg = load_base_config()
    gates_cfg = cfg["gates"]
    data = eth_data()["ETH/USDT:USDT"]
    total_capital = float(cfg["portfolio"]["total_capital_usd"])

    leg1_weekly = portfolio_combined_strategy._leg1_weekly_return(data, cfg, total_capital)

    df_19 = _run_2a_with_universe_filter(cfg, exclude={"ETH"})
    leg2_19_net = pd.Series(df_19["net_return"].to_numpy(), index=pd.DatetimeIndex(df_19["exit_time"]))

    # 참고용(20자산, 현재 config 기준) — 비교표에 나란히 싣기 위해 재계산
    df_20 = strategy_2a.run(cfg, PROJECT_ROOT)
    leg2_20_net = pd.Series(df_20["net_return"].to_numpy(), index=pd.DatetimeIndex(df_20["exit_time"]))

    full_idx_19 = leg1_weekly.index.union(leg2_19_net.index)
    sigma1 = leg1_weekly.reindex(full_idx_19, fill_value=0.0).std(ddof=1)
    sigma2_19 = leg2_19_net.reindex(full_idx_19, fill_value=0.0).std(ddof=1)
    w1_vp_19 = float((1 / sigma1) / (1 / sigma1 + 1 / sigma2_19))
    w2_vp_19 = 1 - w1_vp_19

    current_weights = cfg["portfolio"]["weights"]  # 20자산 기준 정정값(83.83:16.17), 이미 config에 반영됨

    scenarios = {
        "current_config_20asset_weights_on_20asset_2a": (current_weights["filtered_trend"], current_weights["2a"], leg2_20_net),
        "current_config_20asset_weights_on_19asset_2a": (current_weights["filtered_trend"], current_weights["2a"], leg2_19_net),
        "new_vol_parity_19asset_weights_on_19asset_2a": (w1_vp_19, w2_vp_19, leg2_19_net),
    }

    rows = []
    for label, (w1, w2, leg2) in scenarios.items():
        port = portfolio_combined_strategy.build(leg1_weekly, leg2, {"filtered_trend": w1, "2a": w2}, total_capital)
        m = _metrics_from_weekly(port["weekly_return"], port["equity_curve"], gates_cfg, total_capital)
        rows.append({"config": label, "w_filtered_trend": w1, "w_2a": w2, **m})

    result_df = pd.DataFrame(rows)
    result_df.to_csv(DIAG_DATA_DIR / "vol_parity_19asset_recalc_comparison.csv", index=False)
    return {"table": rows, "new_vol_parity_19asset_weights": {"filtered_trend": w1_vp_19, "2a": w2_vp_19},
            "sigma1": float(sigma1), "sigma2_19asset": float(sigma2_19)}


if __name__ == "__main__":
    out = run()
    for row in out["table"]:
        print(row)
    print("new 19-asset vol-parity weights:", out["new_vol_parity_19asset_weights"])
    print("sigma1 (filtered_trend):", out["sigma1"], "sigma2 (2a, 19asset net):", out["sigma2_19asset"])
