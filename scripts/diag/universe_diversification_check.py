"""STEP 0 — 유니버스 분산성 체크 (2a 횡단면 신호의 선행조건).

20자산(config/tickers.txt) 1h 수익률 상관구조를 실측한다. 기존 3자산(BTC/
ETH/SOL)이 0.72~0.82로 사실상 베타 1개였던 것과 비교해, 유니버스 확장이
실제로 분산효과를 주는지 확인한다. scipy/sklearn 미사용(requirements.txt
고정 원칙) — 계층적 군집화(평균연결법)를 numpy만으로 직접 구현한다. 군집
수는 단일 임계값을 임의로 고르지 않고 상관계수 임계값 [0.3, 0.5, 0.7]
셋에서 각각 보고한다(하나만 고르면 그 선택 자체가 자의적이 됨).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.diag.common import DIAG_DATA_DIR, load_symbol_data, symbol_file

PROJECT_ROOT_TICKERS = "config/tickers.txt"
CORR_CUT_THRESHOLDS = [0.3, 0.5, 0.7]


def _load_tickers() -> list[str]:
    from pathlib import Path
    path = Path(__file__).parent.parent.parent / PROJECT_ROOT_TICKERS
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _load_hourly_returns(tickers: list[str]) -> pd.DataFrame:
    closes = {}
    for pair in tickers:
        data = load_symbol_data(symbol_file(pair))
        base = pair.split("/")[0]
        closes[base] = data.df_1h["close"]
    df = pd.DataFrame(closes).dropna()
    return df.pct_change().dropna()


def _average_linkage_clustering(corr: pd.DataFrame) -> list[dict]:
    """평균연결법 응집형 계층적 군집화(numpy 전용 구현). 반환: 병합 이력
    [{step, cluster_a, cluster_b, distance, size}] — distance는 1-상관계수."""
    labels = list(corr.columns)
    n = len(labels)
    dist = 1 - corr.to_numpy()
    np.fill_diagonal(dist, np.inf)
    clusters = {i: [i] for i in range(n)}
    active = list(range(n))
    merge_log = []
    next_id = n
    while len(active) > 1:
        best = (np.inf, None, None)
        for ii in range(len(active)):
            for jj in range(ii + 1, len(active)):
                a, b = active[ii], active[jj]
                members_a, members_b = clusters[a], clusters[b]
                d = np.mean([dist[x, y] for x in members_a for y in members_b])
                if d < best[0]:
                    best = (d, a, b)
        d, a, b = best
        merged = clusters[a] + clusters[b]
        clusters[next_id] = merged
        merge_log.append({
            "step": len(merge_log), "cluster_a_members": ",".join(labels[i] for i in clusters[a]),
            "cluster_b_members": ",".join(labels[i] for i in clusters[b]),
            "distance": float(d), "merged_size": len(merged),
        })
        active.remove(a)
        active.remove(b)
        active.append(next_id)
        next_id += 1
    return merge_log


def _n_clusters_at_cut(merge_log: list[dict], n_assets: int, distance_cut: float) -> int:
    """distance_cut 미만에서 병합된 것만 실제 병합으로 인정 -> 남은 군집 수."""
    n_merges_below_cut = sum(1 for m in merge_log if m["distance"] < distance_cut)
    return n_assets - n_merges_below_cut


def _beta_vs_btc(returns: pd.DataFrame, btc_col: str = "BTC") -> pd.Series:
    btc = returns[btc_col].to_numpy()
    var_btc = np.var(btc, ddof=1)
    betas = {}
    for col in returns.columns:
        if col == btc_col:
            betas[col] = 1.0
            continue
        cov = np.cov(returns[col].to_numpy(), btc, ddof=1)[0, 1]
        betas[col] = float(cov / var_btc)
    return pd.Series(betas)


def run() -> dict:
    tickers = _load_tickers()
    returns = _load_hourly_returns(tickers)
    corr = returns.corr()
    corr.to_csv(DIAG_DATA_DIR / "universe_corr_matrix.csv")

    iu = np.triu_indices_from(corr.to_numpy(), k=1)
    pairwise_vals = corr.to_numpy()[iu]
    pair_labels = [(corr.columns[i], corr.columns[j]) for i, j in zip(*iu)]
    pair_df = pd.DataFrame({"asset_a": [p[0] for p in pair_labels], "asset_b": [p[1] for p in pair_labels], "corr": pairwise_vals})
    pair_df = pair_df.sort_values("corr", ascending=False).reset_index(drop=True)
    pair_df.to_csv(DIAG_DATA_DIR / "universe_pairwise_corr_sorted.csv", index=False)

    mean_pairwise = float(pairwise_vals.mean())
    top5 = pair_df.head(5).to_dict("records")
    bottom5 = pair_df.tail(5).to_dict("records")

    betas = _beta_vs_btc(returns)
    betas.to_csv(DIAG_DATA_DIR / "universe_btc_beta.csv", header=["beta_vs_btc"])

    merge_log = _average_linkage_clustering(corr)
    pd.DataFrame(merge_log).to_csv(DIAG_DATA_DIR / "universe_cluster_merge_log.csv", index=False)
    n_assets = len(corr.columns)
    cluster_counts = {f"corr_gt_{c}": _n_clusters_at_cut(merge_log, n_assets, 1 - c) for c in CORR_CUT_THRESHOLDS}

    old3 = returns[["BTC", "ETH", "SOL"]].corr()
    old3_mean = float(old3.to_numpy()[np.triu_indices_from(old3.to_numpy(), k=1)].mean())

    verdict_pass = mean_pairwise < old3_mean and cluster_counts["corr_gt_0.5"] >= 3

    summary = {
        "n_assets": n_assets,
        "mean_pairwise_corr_20": mean_pairwise,
        "mean_pairwise_corr_btc_eth_sol": old3_mean,
        **cluster_counts,
        "verdict_pass_2a_proceed": verdict_pass,
    }
    pd.Series(summary).to_csv(DIAG_DATA_DIR / "universe_diversification_summary.csv")

    return {
        "summary": summary, "top5_pairs": top5, "bottom5_pairs": bottom5,
        "betas": betas.to_dict(), "n_assets": n_assets,
    }


if __name__ == "__main__":
    result = run()
    print(result["summary"])
    print("top5:", result["top5_pairs"])
    print("bottom5:", result["bottom5_pairs"])
