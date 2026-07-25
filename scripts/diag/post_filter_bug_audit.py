"""사후 필터링 버그(gen_1a 등이 무필터 trend 백테스트 결과를 사후 필터링해
단일 포지션 슬롯 점유 캐스케이드로 트레이드가 오염된 문제, OFFICIAL_GATE_
RESULT.md STEP 3에서 발견)의 소급 영향 조사.

재실행 대상(사후 필터링으로 확인된 것만 — 3a/3b는 코드 검토 결과 해당
없음, 아래 run()의 docstring 참조):
1. SIGNAL_VALIDATION_1a.md 재현 — 정식 strategies/filtered_trend.py(진짜
   사전 게이팅) 트레이드로 STEP A~D 다시 계산, 판정 변화 확인.
2. FILTER_VALIDATION_1a.md 재현 — 같은 방식으로 학습/홀드아웃 재현성
   재검정.
3. 다운스트림 영향 정량화 — vol-parity 배분 가중치(현재 config.yaml에
   고정된 값)가 오염된 leg1 데이터로 계산됐던 것이므로, 정정된 데이터로
   다시 계산하면 얼마나 달라지는지 산출(가중치를 실제로 바꾸지는 않음 —
   그리드서치/재탐색이 아니라 "영향 정량화"만).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import checks.signal_validation as cv
from scripts.diag.common import DIAG_DATA_DIR, eth_data, get_trend_trades, load_base_config, profit_factor_from_pnls, trades_to_df
import strategies.filtered_trend as filtered_trend_strategy

PROJECT_ROOT = DIAG_DATA_DIR.parent.parent


def official_filtered_trend_df(cfg: dict) -> pd.DataFrame:
    data = eth_data()["ETH/USDT:USDT"]
    result = filtered_trend_strategy.run(data, cfg)
    df = trades_to_df(result.trades)
    df["return"] = df["r_multiple"]
    bar_index = data.df_15m.index
    n_bars = len(bar_index)
    df["entry_idx"] = np.clip(bar_index.searchsorted(df["entry_time"].to_numpy()), 0, n_bars - 1)
    df["exit_idx"] = np.clip(bar_index.searchsorted(df["exit_time"].to_numpy()), 0, n_bars - 1)
    return df, bar_index, n_bars


# ---------------------------------------------------------------------------
# 1. SIGNAL_VALIDATION_1a 재현
# ---------------------------------------------------------------------------

def rerun_signal_validation_1a(cfg: dict) -> dict:
    df, bar_index, n_bars = official_filtered_trend_df(cfg)

    trend_trades = get_trend_trades(use_grace=True)
    trend_df = trades_to_df(trend_trades)
    ref_windows = list(zip(np.clip(bar_index.searchsorted(trend_df["entry_time"].to_numpy()), 0, n_bars - 1),
                            np.clip(bar_index.searchsorted(trend_df["exit_time"].to_numpy()), 0, n_bars - 1)))
    cand_windows = list(zip(df["entry_idx"], df["exit_idx"]))
    step_a = cv.check_exclusivity(cand_windows, ref_windows, n_bars)

    cluster_ids = cv.cluster_events(df["entry_time"])
    step_b = cv.clustering_summary(cluster_ids)

    step_c = cv.cluster_bootstrap(df["return"].to_numpy(), cluster_ids)

    cand_weekly = cv.weekly_return_series(df["exit_time"], df["return"].to_numpy())
    trend_weekly = cv.weekly_return_series(trend_df["exit_time"], trend_df["r_multiple"].to_numpy())
    step_d = cv.series_correlation(cand_weekly, trend_weekly)

    accepted_path = PROJECT_ROOT / "signal_specs" / "accepted_signals.yaml"
    accepted = cv.load_accepted_signals(accepted_path, PROJECT_ROOT, exclude_id="1a")
    multi_corr = cv.multi_correlation_check(cand_weekly, accepted) if accepted else None

    verdict = cv.compute_verdict(step_c, step_d, multi_corr=multi_corr)

    return {
        "n_trades": len(df), "step_a_overlap_pct": step_a["overlap_pct"],
        "step_b_n_clusters": step_b["n_independent_clusters"],
        "step_c_mean_r": step_c["point_mean_return_pct"] / 100, "step_c_mean_ci_lo": step_c["mean_ci_lo_pct"] / 100,
        "step_c_mean_ci_hi": step_c["mean_ci_hi_pct"] / 100, "step_c_pf": step_c["point_pf"],
        "step_d_corr_trend": step_d["weekly_return_correlation"],
        "multi_corr_vs_2a": multi_corr["per_signal"].get("2a", {}).get("weekly_return_correlation") if multi_corr else None,
        "verdict": verdict, "original_verdict": "RECLASSIFY_AS_FILTER", "original_n_trades": 633,
    }


# ---------------------------------------------------------------------------
# 2. FILTER_VALIDATION_1a 재현
# ---------------------------------------------------------------------------

def _metrics(sub: pd.DataFrame, pnl_col: str, r_col: str) -> dict:
    if len(sub) == 0:
        return {"n": 0, "pf": float("nan"), "mean_r": float("nan")}
    return {"n": len(sub), "pf": profit_factor_from_pnls(sub[pnl_col].to_numpy()), "mean_r": float(sub[r_col].mean())}


def rerun_filter_validation_1a(cfg: dict) -> dict:
    filtered_df, _, _ = official_filtered_trend_df(cfg)
    trend_trades = get_trend_trades(use_grace=True)
    baseline_df = trades_to_df(trend_trades).sort_values("entry_time").reset_index(drop=True)

    midpoint = baseline_df["entry_time"].iloc[len(baseline_df) // 2]

    results = {}
    for label, cond in [("train", lambda s: s < midpoint), ("holdout", lambda s: s >= midpoint)]:
        baseline_half = baseline_df[cond(baseline_df["entry_time"])]
        filtered_half = filtered_df[cond(filtered_df["entry_time"])]
        baseline_m = _metrics(baseline_half, "pnl_usd", "r_multiple")
        filtered_m = _metrics(filtered_half, "pnl_usd", "r_multiple")
        results[label] = {
            "baseline_n": baseline_m["n"], "baseline_pf": baseline_m["pf"], "baseline_mean_r": baseline_m["mean_r"],
            "filtered_n": filtered_m["n"], "filtered_pf": filtered_m["pf"], "filtered_mean_r": filtered_m["mean_r"],
            "pf_improvement": (filtered_m["pf"] - baseline_m["pf"]) if np.isfinite(filtered_m["pf"]) and np.isfinite(baseline_m["pf"]) else float("nan"),
            "mean_r_improvement": filtered_m["mean_r"] - baseline_m["mean_r"],
        }

    holdout_replicated = bool(
        results["holdout"]["filtered_pf"] > results["holdout"]["baseline_pf"]
        and results["holdout"]["filtered_mean_r"] > results["holdout"]["baseline_mean_r"]
    )
    verdict = "REPLICATED_ACCEPT_FILTER" if holdout_replicated else "NOT_REPLICATED_REJECT_FILTER"
    return {"results": results, "verdict": verdict, "original_verdict": "REPLICATED_ACCEPT_FILTER"}


# ---------------------------------------------------------------------------
# 3. vol-parity 가중치 민감도(다운스트림 영향 정량화, 재탐색 아님)
# ---------------------------------------------------------------------------

def vol_parity_sensitivity(cfg: dict) -> dict:
    total_capital = float(cfg["portfolio"]["total_capital_usd"])

    # 오염된(사후필터) leg1
    contaminated_path = PROJECT_ROOT / "reports" / "signal_validation_data" / "1a" / "1a_trades.csv"
    contam_df = pd.read_csv(contaminated_path, parse_dates=["exit_time"])
    contam_weekly = contam_df.groupby(pd.Grouper(key="exit_time", freq="W"))["pnl_usd"].sum() / total_capital

    # 정정된(사전 게이팅) leg1
    official_df, _, _ = official_filtered_trend_df(cfg)
    official_weekly = official_df.groupby(pd.Grouper(key="exit_time", freq="W"))["pnl_usd"].sum() / total_capital

    leg2_df = pd.read_csv(PROJECT_ROOT / "reports" / "diag_data" / "portfolio_2a_net_weekly_returns.csv", index_col=0, parse_dates=True)["weekly_return"]

    def vp_weight(leg1_weekly):
        full_idx = leg1_weekly.index.union(leg2_df.index)
        sigma1 = leg1_weekly.reindex(full_idx, fill_value=0.0).std(ddof=1)
        sigma2 = leg2_df.reindex(full_idx, fill_value=0.0).std(ddof=1)
        w1 = (1 / sigma1) / (1 / sigma1 + 1 / sigma2)
        return float(w1), float(1 - w1), float(sigma1)

    w1_contam, w2_contam, sigma1_contam = vp_weight(contam_weekly)
    w1_official, w2_official, sigma1_official = vp_weight(official_weekly)

    return {
        "contaminated_w1_filtered_trend": w1_contam, "contaminated_w2_2a": w2_contam, "contaminated_sigma1": sigma1_contam,
        "corrected_w1_filtered_trend": w1_official, "corrected_w2_2a": w2_official, "corrected_sigma1": sigma1_official,
        "current_config_w1": cfg["portfolio"]["weights"]["filtered_trend"],
        "weight_diff_w1": w1_official - w1_contam,
    }


def run() -> dict:
    """3a/3b는 이 스크립트에서 재실행하지 않는다 — scripts/diag/signal_validation_3a3b.py
    코드 재검토 결과(_simulate_3a/_simulate_3b) trend의 백테스트 결과를 필터링하는
    구조가 아니라 BB/RSI 조건으로 처음부터 독자적인 트레이드를 생성하는 구조임을
    확인했다(trend과 슬롯을 공유하지 않음 — RANGE 레짐 전용, trend는 TREND 레짐
    전용으로 애초에 겹치지 않는 별도 시뮬레이션). get_trend_trades()는 STEP A/D의
    비교 대상(레퍼런스)으로만 쓰이지 필터링 소스가 아니다 — 재실행할 코드 경로
    자체가 없으므로 코드 검토로 감사를 마친다(POST_FILTER_BUG_AUDIT.md에 근거 기록)."""
    cfg = load_base_config()

    sv1a = rerun_signal_validation_1a(cfg)
    fv1a = rerun_filter_validation_1a(cfg)
    vps = vol_parity_sensitivity(cfg)

    pd.Series(sv1a).to_csv(DIAG_DATA_DIR / "audit_signal_validation_1a_rerun.csv")
    rows = []
    for label, r in fv1a["results"].items():
        rows.append({"period": label, **r})
    pd.DataFrame(rows).to_csv(DIAG_DATA_DIR / "audit_filter_validation_1a_rerun.csv", index=False)
    pd.Series(vps).to_csv(DIAG_DATA_DIR / "audit_vol_parity_sensitivity.csv")

    return {"signal_validation_1a": sv1a, "filter_validation_1a": fv1a, "vol_parity_sensitivity": vps}


if __name__ == "__main__":
    out = run()
    print("-- SIGNAL_VALIDATION_1a rerun --")
    print(out["signal_validation_1a"])
    print("-- FILTER_VALIDATION_1a rerun --")
    for label, r in out["filter_validation_1a"]["results"].items():
        print(label, r)
    print("verdict:", out["filter_validation_1a"]["verdict"])
    print("-- vol-parity sensitivity --")
    print(out["vol_parity_sensitivity"])
