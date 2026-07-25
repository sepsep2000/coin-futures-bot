"""1a 필터(1D 모멘텀 정합) 학습/홀드아웃 재현성 검정.

1a는 신호로서는 RECLASSIFY_AS_FILTER 판정을 받았다(trend와 상관 0.876,
활성구간 100% 중첩) — 신호 자체의 통계적 실재는 이미 확인됐다
(SIGNAL_VALIDATION_1a.md). 남은 질문은 "trend의 필터로서 일반화되는가"다.

방법론(사전 확정, 결과를 보고 기준을 정하지 않음):
1. trend 전체 트레이드를 entry_time 기준 시간순 정렬 후 반으로 나눠
   학습/홀드아웃 구간으로 삼는다(각 구간 트레이드 수 절반씩, 시간순 —
   미래 데이터로 과거를 보정하지 않음).
2. 각 구간에서 "전체"(필터 없음) vs "필터 적용"(1D ROC 방향 일치 트레이드만)
   PF·평균R을 계산.
3. 판정 기준(사전 확정): 홀드아웃 구간에서 필터 적용 PF > 필터 없음 PF
   **그리고** 필터 적용 평균R > 필터 없음 평균R 이면 재현 확인(필터 유효).
   둘 중 하나라도 개선되지 않으면 과최적화로 판정해 기각.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.diag.common import DIAG_DATA_DIR, eth_data, get_trend_trades, profit_factor_from_pnls, trades_to_df

MOMENTUM_LOOKBACK_DAYS = 5  # 1a 명세와 동일 값, 재조정 없음


def _add_roc(df: pd.DataFrame, close_15m: pd.Series) -> pd.DataFrame:
    daily_close = close_15m.resample("1D").last()
    daily_roc = daily_close.pct_change(MOMENTUM_LOOKBACK_DAYS).reset_index()
    daily_roc.columns = ["ts", "roc"]
    entries = df[["entry_time"]].reset_index().rename(columns={"index": "orig_idx"}).sort_values("entry_time")
    merged = pd.merge_asof(entries, daily_roc.sort_values("ts"), left_on="entry_time", right_on="ts", direction="backward")
    merged = merged.set_index("orig_idx").sort_index()
    df = df.copy()
    df["roc"] = merged["roc"]
    df["agree"] = ((df["direction"] == "long") & (df["roc"] > 0)) | ((df["direction"] == "short") & (df["roc"] < 0))
    return df


def _metrics(sub: pd.DataFrame) -> dict:
    if len(sub) == 0:
        return {"n": 0, "pf": float("nan"), "mean_r": float("nan")}
    return {
        "n": len(sub), "pf": profit_factor_from_pnls(sub["pnl_usd"].to_numpy()),
        "mean_r": float(sub["r_multiple"].mean()),
    }


def run() -> dict:
    data = eth_data()["ETH/USDT:USDT"]
    trades = get_trend_trades(use_grace=True)
    df = trades_to_df(trades).sort_values("entry_time").reset_index(drop=True)
    df = _add_roc(df, data.df_15m["close"]).dropna(subset=["roc"])

    mid = len(df) // 2
    train, holdout = df.iloc[:mid], df.iloc[mid:]

    results = {}
    for label, half in [("train", train), ("holdout", holdout)]:
        baseline = _metrics(half)
        filtered = _metrics(half[half["agree"]])
        results[label] = {
            "period_start": str(half["entry_time"].min()), "period_end": str(half["entry_time"].max()),
            "baseline": baseline, "filtered": filtered,
            "pf_improvement": filtered["pf"] - baseline["pf"] if np.isfinite(filtered["pf"]) and np.isfinite(baseline["pf"]) else float("nan"),
            "mean_r_improvement": filtered["mean_r"] - baseline["mean_r"],
        }

    holdout_replicated = bool(
        results["holdout"]["filtered"]["pf"] > results["holdout"]["baseline"]["pf"]
        and results["holdout"]["filtered"]["mean_r"] > results["holdout"]["baseline"]["mean_r"]
    )
    verdict = "REPLICATED_ACCEPT_FILTER" if holdout_replicated else "NOT_REPLICATED_REJECT_FILTER"

    df.to_csv(DIAG_DATA_DIR / "filter_1a_trades_with_roc.csv", index=False)
    summary_rows = []
    for label in ["train", "holdout"]:
        r = results[label]
        summary_rows.append({
            "period": label, "start": r["period_start"], "end": r["period_end"],
            "baseline_n": r["baseline"]["n"], "baseline_pf": r["baseline"]["pf"], "baseline_mean_r": r["baseline"]["mean_r"],
            "filtered_n": r["filtered"]["n"], "filtered_pf": r["filtered"]["pf"], "filtered_mean_r": r["filtered"]["mean_r"],
            "pf_improvement": r["pf_improvement"], "mean_r_improvement": r["mean_r_improvement"],
        })
    pd.DataFrame(summary_rows).to_csv(DIAG_DATA_DIR / "filter_1a_train_holdout_summary.csv", index=False)

    return {"results": results, "verdict": verdict}


if __name__ == "__main__":
    out = run()
    for label, r in out["results"].items():
        print(label, r)
    print("verdict:", out["verdict"])
