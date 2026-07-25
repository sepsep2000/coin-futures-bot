"""OOS>=300 기준 방식 3(클러스터) 채택을 위한 포트폴리오 레벨 실측.

핵심 질문: filtered_trend의 독립 클러스터(283개)와 2a의 독립 클러스터
(185개)를 단순 합산(468)해도 되는가, 아니면 두 신호의 이벤트 타임라인을
합쳐 D5의 24h 병합 규칙을 다시 적용해야 하는가?

판단: D5/D7b의 클러스터링은 "같은 데이터生성 과정(같은 신호 메커니즘)
안에서" 자기상관을 보정하기 위한 것이다 — 연속된 이벤트가 같은 시장
국면을 반복 관측한 것일 수 있어 독립성이 깨진다는 문제를 다룬다. 이미
STEP D(SIGNAL_VALIDATION_2a.md, FILTERED_TREND_2A_CORRELATION.md,
ENGINE_MULTI_CORRELATION_UPDATE.md)에서 두 신호의 주간수익률 상관계수가
반복적으로 -0.06~-0.10 사이(거의 0, 임계치 0.3에 한참 못 미침)로 확인돼
독립임이 이미 실증됐다 — 서로 다른 메커니즘(단일자산 추세추종 vs
20자산 횡단면 상대가치)이 우연히 같은 주에 이벤트가 겹친다고 해서
정보가 겹치는 게 아니다. 따라서 원칙적으로는 **단순 합산이 맞다** —
단, 이 판단이 실제로 유의미한 차이를 만드는지 실측으로 확인한다(합산과
풀링재군집화 둘 다 계산해서 비교).
"""

from __future__ import annotations

import pandas as pd

import checks.signal_validation as cv
from scripts.diag.common import DIAG_DATA_DIR, eth_data, load_base_config, trades_to_df
import strategies.filtered_trend as filtered_trend_strategy

PROJECT_ROOT = DIAG_DATA_DIR.parent.parent


def run() -> dict:
    cfg = load_base_config()

    data = eth_data()["ETH/USDT:USDT"]
    ft_result = filtered_trend_strategy.run(data, cfg)
    ft_df = trades_to_df(ft_result.trades)
    ft_cluster_ids = cv.cluster_events(ft_df["entry_time"])
    ft_summary = cv.clustering_summary(ft_cluster_ids)

    leg2_trades_path = PROJECT_ROOT / "reports" / "diag_data" / "portfolio_2a_net_trades.csv"
    leg2_df = pd.read_csv(leg2_trades_path, parse_dates=["entry_time", "exit_time"])
    leg2_cluster_ids = cv.cluster_events(leg2_df["entry_time"])
    leg2_summary = cv.clustering_summary(leg2_cluster_ids)

    simple_sum = ft_summary["n_independent_clusters"] + leg2_summary["n_independent_clusters"]

    pooled_entry_times = pd.concat([ft_df["entry_time"], leg2_df["entry_time"]]).reset_index(drop=True)
    pooled_cluster_ids = cv.cluster_events(pooled_entry_times)
    pooled_summary = cv.clustering_summary(pooled_cluster_ids)

    # 풀링재군집화에서 두 신호 이벤트가 실제로 같은 클러스터에 섞였는지 확인
    pooled_df = pd.DataFrame({
        "entry_time": pooled_entry_times,
        "source": ["filtered_trend"] * len(ft_df) + ["2a"] * len(leg2_df),
        "cluster_id": pooled_cluster_ids,
    })
    mixed_clusters = pooled_df.groupby("cluster_id")["source"].nunique()
    n_mixed_clusters = int((mixed_clusters > 1).sum())

    result = {
        "filtered_trend_n_trades": len(ft_df), "filtered_trend_n_clusters": ft_summary["n_independent_clusters"],
        "2a_n_events": len(leg2_df), "2a_n_clusters": leg2_summary["n_independent_clusters"],
        "simple_sum_clusters": simple_sum,
        "pooled_reclustered_clusters": pooled_summary["n_independent_clusters"],
        "n_mixed_source_clusters": n_mixed_clusters,
        "threshold": 300,
        "simple_sum_passes_300": simple_sum >= 300,
        "pooled_passes_300": pooled_summary["n_independent_clusters"] >= 300,
    }

    pd.Series(result).to_csv(DIAG_DATA_DIR / "oos_portfolio_cluster_verification.csv")
    pooled_df.to_csv(DIAG_DATA_DIR / "oos_portfolio_pooled_events.csv", index=False)
    return result


if __name__ == "__main__":
    print(run())
