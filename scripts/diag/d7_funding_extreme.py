"""D7 — 펀딩비 극단치 이벤트 점검.

(1) 펀딩비가 표본 내 극단치(상/하위 5%)일 때, 그 이후 가격의 방향성(forward
    return)이 평시와 다른지 — 펀딩비가 그 자체로 역행 신호가 될 수 있는지 점검.
(2) 실제 trend 트레이드 중 보유기간에 극단 펀딩 이벤트가 걸친 트레이드가
    얼마나 있고, 그 트레이드들의 펀딩비 비용이 평시 대비 큰지 — D1 비용
    귀속과의 연결점.
둘 다 기술 통계이며 새 필터/신호로 바로 쓰자는 게 아니다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.diag.common import DIAG_DATA_DIR, eth_data, get_trend_trades, trades_to_df

HORIZONS_BARS = [32, 96, 192]  # 8h, 24h, 48h @ 15m
EXTREME_PCTILE = 5.0


def _forward_return_stats(close: pd.Series, event_bar_idx: np.ndarray, horizon: int) -> dict:
    valid = event_bar_idx + horizon < len(close)
    idx = event_bar_idx[valid]
    fwd = close.to_numpy()[idx + horizon] / close.to_numpy()[idx] - 1
    return {
        "n": len(fwd),
        "mean_pct": float(np.mean(fwd) * 100) if len(fwd) else float("nan"),
        "median_pct": float(np.median(fwd) * 100) if len(fwd) else float("nan"),
        "std_pct": float(np.std(fwd) * 100) if len(fwd) else float("nan"),
    }


def run() -> dict:
    data = eth_data()["ETH/USDT:USDT"]
    close = data.df_15m["close"]
    funding = data.funding.copy()
    funding["ts"] = pd.to_datetime(funding["timestamp"], unit="ms", utc=True)
    funding = funding.sort_values("ts").reset_index(drop=True)

    hi_thresh = float(np.percentile(funding["funding_rate"], 100 - EXTREME_PCTILE))
    lo_thresh = float(np.percentile(funding["funding_rate"], EXTREME_PCTILE))
    funding["is_extreme_high"] = funding["funding_rate"] >= hi_thresh
    funding["is_extreme_low"] = funding["funding_rate"] <= lo_thresh

    bar_pos = close.index.searchsorted(funding["ts"].to_numpy())
    bar_pos = np.clip(bar_pos, 0, len(close) - 1)
    funding["bar_idx"] = bar_pos

    all_idx = funding["bar_idx"].to_numpy()
    hi_idx = funding.loc[funding["is_extreme_high"], "bar_idx"].to_numpy()
    lo_idx = funding.loc[funding["is_extreme_low"], "bar_idx"].to_numpy()

    event_rows = []
    for h in HORIZONS_BARS:
        baseline = _forward_return_stats(close, all_idx, h)
        hi_stats = _forward_return_stats(close, hi_idx, h)
        lo_stats = _forward_return_stats(close, lo_idx, h)
        event_rows.append({
            "horizon_bars": h, "horizon_hours": h * 0.25,
            "baseline_n": baseline["n"], "baseline_mean_pct": baseline["mean_pct"],
            "extreme_high_n": hi_stats["n"], "extreme_high_mean_pct": hi_stats["mean_pct"],
            "extreme_high_diff_vs_baseline_pct": hi_stats["mean_pct"] - baseline["mean_pct"],
            "extreme_low_n": lo_stats["n"], "extreme_low_mean_pct": lo_stats["mean_pct"],
            "extreme_low_diff_vs_baseline_pct": lo_stats["mean_pct"] - baseline["mean_pct"],
        })
    event_df = pd.DataFrame(event_rows)
    event_df.to_csv(DIAG_DATA_DIR / "d7_funding_forward_returns.csv", index=False)

    trades = get_trend_trades(use_grace=True)
    df = trades_to_df(trades)
    extreme_ts = pd.DatetimeIndex(pd.concat([
        funding.loc[funding["is_extreme_high"], "ts"],
        funding.loc[funding["is_extreme_low"], "ts"],
    ]).sort_values())

    def overlaps_extreme(entry_time, exit_time) -> bool:
        lo = extreme_ts.searchsorted(entry_time, side="left")
        hi = extreme_ts.searchsorted(exit_time, side="right")
        return hi > lo

    df["overlaps_extreme_funding"] = [overlaps_extreme(e, x) for e, x in zip(df["entry_time"], df["exit_time"])]
    df.to_csv(DIAG_DATA_DIR / "d7_trade_funding_overlap.csv", index=False)

    overlap_trades = df[df["overlaps_extreme_funding"]]
    non_overlap_trades = df[~df["overlaps_extreme_funding"]]

    summary = {
        "n_funding_records": len(funding),
        "extreme_pctile_used": EXTREME_PCTILE,
        "hi_threshold_funding_rate": hi_thresh,
        "lo_threshold_funding_rate": lo_thresh,
        "n_trades_total": len(df),
        "n_trades_overlap_extreme_funding": len(overlap_trades),
        "avg_funding_usd_overlap": float(overlap_trades["funding_usd"].mean()) if len(overlap_trades) else float("nan"),
        "avg_funding_usd_non_overlap": float(non_overlap_trades["funding_usd"].mean()) if len(non_overlap_trades) else float("nan"),
        "avg_r_multiple_overlap": float(overlap_trades["r_multiple"].mean()) if len(overlap_trades) else float("nan"),
        "avg_r_multiple_non_overlap": float(non_overlap_trades["r_multiple"].mean()) if len(non_overlap_trades) else float("nan"),
    }
    pd.Series(summary).to_csv(DIAG_DATA_DIR / "d7_summary.csv")
    return {"event_study": event_rows, "summary": summary}


if __name__ == "__main__":
    result = run()
    for row in result["event_study"]:
        print(row)
    print(result["summary"])
