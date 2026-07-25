"""D7b — 펀딩 극단(저극단, 숏 과열) 신호 후속 검증.

D7에서 저극단 펀딩 이후 24h/48h 수익률이 기준선 대비 8~9배로 관찰됐으나
정식 유의성 검정도, 이벤트 클러스터링(자기상관) 보정도 없었다. 이 스크립트는
"신호 채택 여부"만 판단하며 신호를 구현하거나 파라미터(퍼센타일 임계값 등)를
튜닝하지 않는다.

방법론 요약(각 절 상단에도 명시):
- 극단 임계값: D7과 동일(전체 3,902건 중 하위 5%ile), 재계산만 하고
  d7_funding_extreme.py는 수정하지 않는다.
- 클러스터링: 이벤트 타임스탬프를 정렬해 직전 이벤트와의 간격이 24h 이하면
  같은 클러스터로 묶는다(펀딩은 8h 간격 정규 스케줄이므로 24h는 "최대 2회
  연속 미발생까지 허용"에 해당).
- 블록부트스트랩: 이벤트가 아니라 "클러스터"를 재추출 단위로 삼아 자기상관을
  보정한다(D5와 동일 사상, n_boot=5000).
- 다중비교 보정: D7 원 표는 horizon 3개 × 방향 2개 = 6개 가설을 동시에 봤으므로
  본페로니 보정 임계값(alpha=0.05/6)을 함께 표시한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.diag.common import DIAG_DATA_DIR, eth_data, get_trend_trades, trades_to_df, trend_cfg_current
from src.strategy.trend import generate_trend_signals

EXTREME_PCTILE = 5.0
CLUSTER_GAP_HOURS = 24
HORIZONS_BARS = {"24h": 96, "48h": 192}
N_BOOT = 5000
SEED = 2024
N_HYPOTHESES = 6  # D7 원 표: horizon 3 x 방향 2
ALPHA = 0.05
ALIGN_WINDOW_BARS = 32  # 8h


def _load_low_extreme_events() -> pd.DataFrame:
    data = eth_data()["ETH/USDT:USDT"]
    funding = data.funding.copy()
    funding["ts"] = pd.to_datetime(funding["timestamp"], unit="ms", utc=True)
    funding = funding.sort_values("ts").reset_index(drop=True)
    lo_thresh = float(np.percentile(funding["funding_rate"], EXTREME_PCTILE))
    events = funding[funding["funding_rate"] <= lo_thresh].copy().reset_index(drop=True)
    return events, funding, lo_thresh


def _cluster_events(ts: pd.Series, gap_hours: int) -> np.ndarray:
    ts_sorted_idx = np.argsort(ts.to_numpy())
    ts_sorted = ts.to_numpy()[ts_sorted_idx]
    cluster_id = np.zeros(len(ts_sorted), dtype=int)
    cid = 0
    for i in range(1, len(ts_sorted)):
        gap = (ts_sorted[i] - ts_sorted[i - 1]) / np.timedelta64(1, "h")
        if gap > gap_hours:
            cid += 1
        cluster_id[i] = cid
    out = np.empty(len(ts_sorted), dtype=int)
    out[ts_sorted_idx] = cluster_id
    return out


def _forward_return(close: pd.Series, bar_idx: np.ndarray, horizon: int) -> np.ndarray:
    valid = bar_idx + horizon < len(close)
    idx = bar_idx[valid]
    return close.to_numpy()[idx + horizon] / close.to_numpy()[idx] - 1, valid


def d7b1_clustering(events: pd.DataFrame) -> dict:
    events["cluster_id"] = _cluster_events(events["ts"], CLUSTER_GAP_HOURS)
    cluster_sizes = events.groupby("cluster_id").size()
    events.to_csv(DIAG_DATA_DIR / "d7b_low_extreme_events_clustered.csv", index=False)
    summary = {
        "n_events_raw": len(events),
        "n_independent_clusters": events["cluster_id"].nunique(),
        "cluster_size_mean": float(cluster_sizes.mean()),
        "cluster_size_median": float(cluster_sizes.median()),
        "cluster_size_max": int(cluster_sizes.max()),
        "pct_events_in_clusters_ge2": float((cluster_sizes[cluster_sizes >= 2].sum()) / len(events) * 100),
    }
    return summary


def _cluster_block_bootstrap(events: pd.DataFrame, close: pd.Series, horizon_bars: int, n_boot: int, rng: np.random.Generator) -> dict:
    fwd, valid_mask = _forward_return(close, events["bar_idx"].to_numpy(), horizon_bars)
    ev = events.loc[valid_mask].copy()
    ev["fwd_return"] = fwd
    clusters = ev.groupby("cluster_id")["fwd_return"].apply(lambda s: s.to_numpy())
    cluster_list = clusters.to_list()
    n_clusters = len(cluster_list)

    boot_means = np.empty(n_boot)
    for b in range(n_boot):
        chosen = rng.integers(0, n_clusters, size=n_clusters)
        pooled = np.concatenate([cluster_list[c] for c in chosen])
        boot_means[b] = pooled.mean()

    alpha_adj = ALPHA / N_HYPOTHESES
    return {
        "n_events_valid": len(ev),
        "n_clusters": n_clusters,
        "point_estimate_mean_pct": float(ev["fwd_return"].mean() * 100),
        "ci95_lo_pct": float(np.percentile(boot_means, 2.5) * 100),
        "ci95_hi_pct": float(np.percentile(boot_means, 97.5) * 100),
        "ci_bonferroni_lo_pct": float(np.percentile(boot_means, alpha_adj / 2 * 100) * 100),
        "ci_bonferroni_hi_pct": float(np.percentile(boot_means, (1 - alpha_adj / 2) * 100) * 100),
        "boot_means": boot_means,
    }


def d7b2_bootstrap(events: pd.DataFrame, close: pd.Series, baseline_mean: dict) -> list[dict]:
    rng = np.random.default_rng(SEED)
    rows = []
    for label, h in HORIZONS_BARS.items():
        res = _cluster_block_bootstrap(events, close, h, N_BOOT, rng)
        boot_means = res.pop("boot_means")
        base = baseline_mean[label]
        excludes_95 = not (res["ci95_lo_pct"] <= base <= res["ci95_hi_pct"])
        excludes_bonf = not (res["ci_bonferroni_lo_pct"] <= base <= res["ci_bonferroni_hi_pct"])
        rows.append({
            "horizon": label, **res,
            "baseline_mean_pct": base,
            "excludes_baseline_95ci": excludes_95,
            "excludes_baseline_bonferroni_ci": excludes_bonf,
        })
        pd.Series(boot_means).to_csv(DIAG_DATA_DIR / f"d7b_bootstrap_dist_{label}.csv", index=False, header=["boot_mean_return"])
    return rows


def d7b3_year_breakdown(events: pd.DataFrame, close: pd.Series) -> list[dict]:
    events = events.copy()
    events["year"] = events["ts"].dt.year
    fwd24, valid = _forward_return(close, events["bar_idx"].to_numpy(), HORIZONS_BARS["24h"])
    ev = events.loc[valid].copy()
    ev["fwd_return_24h_pct"] = fwd24 * 100
    rows = []
    for year, g in ev.groupby("year"):
        rows.append({
            "year": int(year), "n_events": len(g),
            "n_clusters": g["cluster_id"].nunique(),
            "mean_fwd_return_24h_pct": float(g["fwd_return_24h_pct"].mean()),
            "median_fwd_return_24h_pct": float(g["fwd_return_24h_pct"].median()),
        })
    return rows


def d7b4_trend_correlation(events: pd.DataFrame, data, cfg: dict) -> dict:
    close = data.df_15m["close"]
    trend_sig = generate_trend_signals(data.df_15m, cfg["trend"])
    entry_long = trend_sig["entry_long"].to_numpy()

    n_bars = len(close)
    aligned_flags = []
    for idx in events["bar_idx"].to_numpy():
        window_end = min(idx + ALIGN_WINDOW_BARS, n_bars)
        aligned_flags.append(bool(entry_long[idx:window_end].any()))
    align_rate = float(np.mean(aligned_flags))

    rng = np.random.default_rng(SEED + 1)
    n_random = 2000
    random_starts = rng.integers(0, n_bars - ALIGN_WINDOW_BARS, size=n_random)
    baseline_rate = float(np.mean([entry_long[s:s + ALIGN_WINDOW_BARS].any() for s in random_starts]))

    trades = get_trend_trades(use_grace=True)
    tdf = trades_to_df(trades)
    tdf["exit_week"] = tdf["exit_time"].dt.to_period("W").dt.start_time
    weekly_trend = tdf.groupby("exit_week")["r_multiple"].sum()

    fwd24, valid = _forward_return(close, events["bar_idx"].to_numpy(), HORIZONS_BARS["24h"])
    ev = events.loc[valid].copy()
    ev["fwd_return_24h"] = fwd24
    ev["event_week"] = ev["ts"].dt.to_period("W").dt.start_time
    weekly_funding = ev.groupby("event_week")["fwd_return_24h"].mean()

    full_index = weekly_trend.index.union(weekly_funding.index)
    weekly_trend_full = weekly_trend.reindex(full_index, fill_value=0.0)
    weekly_funding_full = weekly_funding.reindex(full_index, fill_value=0.0)
    corr = float(np.corrcoef(weekly_trend_full.to_numpy(), weekly_funding_full.to_numpy())[0, 1])

    pd.DataFrame({
        "week": full_index, "trend_weekly_r_sum": weekly_trend_full.to_numpy(),
        "funding_signal_weekly_return": weekly_funding_full.to_numpy(),
    }).to_csv(DIAG_DATA_DIR / "d7b_weekly_return_series.csv", index=False)

    return {
        "align_rate_low_extreme_to_long_signal_8h": align_rate,
        "baseline_rate_random_window_long_signal_8h": baseline_rate,
        "align_rate_minus_baseline": align_rate - baseline_rate,
        "n_weeks_compared": len(full_index),
        "weekly_return_correlation": corr,
    }


def run() -> dict:
    cfg = trend_cfg_current()
    events, funding_all, lo_thresh = _load_low_extreme_events()
    data = eth_data()["ETH/USDT:USDT"]
    close = data.df_15m["close"]
    bar_pos = close.index.searchsorted(events["ts"].to_numpy())
    events["bar_idx"] = np.clip(bar_pos, 0, len(close) - 1)

    baseline_24h, _ = _forward_return(close, np.arange(len(close) - HORIZONS_BARS["24h"]), HORIZONS_BARS["24h"])
    baseline_48h, _ = _forward_return(close, np.arange(len(close) - HORIZONS_BARS["48h"]), HORIZONS_BARS["48h"])
    baseline_mean = {"24h": float(baseline_24h.mean() * 100), "48h": float(baseline_48h.mean() * 100)}

    d1 = d7b1_clustering(events)
    d2 = d7b2_bootstrap(events, close, baseline_mean)
    d3 = d7b3_year_breakdown(events, close)
    d4 = d7b4_trend_correlation(events, data, cfg)

    pd.DataFrame(d2).drop(columns=[]).to_csv(DIAG_DATA_DIR / "d7b_bootstrap_summary.csv", index=False)
    pd.DataFrame(d3).to_csv(DIAG_DATA_DIR / "d7b_year_breakdown.csv", index=False)
    pd.Series({**d1, "lo_threshold_funding_rate": lo_thresh}).to_csv(DIAG_DATA_DIR / "d7b_clustering_summary.csv")
    pd.Series(d4).to_csv(DIAG_DATA_DIR / "d7b_trend_correlation_summary.csv")

    return {"d7b1_clustering": d1, "d7b2_bootstrap": d2, "d7b3_year": d3, "d7b4_correlation": d4}


if __name__ == "__main__":
    result = run()
    print("-- D7b-1 clustering --")
    print(result["d7b1_clustering"])
    print("-- D7b-2 bootstrap --")
    for row in result["d7b2_bootstrap"]:
        print(row)
    print("-- D7b-3 year breakdown --")
    for row in result["d7b3_year"]:
        print(row)
    print("-- D7b-4 trend correlation --")
    print(result["d7b4_correlation"])
