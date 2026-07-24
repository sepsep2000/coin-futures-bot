"""D6 — 레짐 세분화 (기술 목적, 필터 적용 아님).

이미 TREND 레짐(ADX>=25 & BB폭 백분위>=60)에서만 진입하므로, 여기서는 그 안을
ADX 강도 구간 / BB폭 백분위 구간으로 더 잘게 나눠 엣지가 특정 하위 구간에
집중되는지 "기술"할 뿐이다. 여기서 나온 구간을 새 필터로 바로 적용하는 것은
그리드서치와 다를 바 없으므로 금지 — 재설계 논의의 참고 자료로만 쓴다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.diag.common import DIAG_DATA_DIR, eth_data, get_trend_trades, profit_factor_from_pnls, trades_to_df, trend_cfg_current
from src.strategy.indicators import adx, bb_width_pct, rolling_percentile

ADX_BINS = [0, 20, 25, 30, 40, 100]
ADX_LABELS = ["<20", "20-25", "25-30", "30-40", ">=40"]
WIDTH_BINS = [0, 40, 60, 80, 100]
WIDTH_LABELS = ["<40", "40-60", "60-80", ">=80"]


def _bucket_stats(df: pd.DataFrame, group_col: str) -> pd.DataFrame:
    rows = []
    for label, g in df.groupby(group_col, observed=True):
        pnls = g["pnl_usd"].to_numpy()
        rows.append({
            group_col: label,
            "n_trades": len(g),
            "win_rate_pct": float((g["pnl_usd"] > 0).mean() * 100) if len(g) else float("nan"),
            "profit_factor": profit_factor_from_pnls(pnls),
            "avg_r_multiple": float(g["r_multiple"].mean()) if len(g) else float("nan"),
            "total_pnl_usd": float(g["pnl_usd"].sum()),
        })
    return pd.DataFrame(rows)


def run() -> dict:
    cfg = trend_cfg_current()
    trades = get_trend_trades(use_grace=True)
    df = trades_to_df(trades)

    data = eth_data()["ETH/USDT:USDT"]
    df_1h = data.df_1h
    regime_cfg = cfg["regime"]
    adx_1h = adx(df_1h["high"], df_1h["low"], df_1h["close"], regime_cfg["adx_period"])
    width_1h = bb_width_pct(df_1h["close"], regime_cfg["bb_period"])
    width_pct_1h = rolling_percentile(width_1h, regime_cfg["bb_width_percentile_window"])

    indicators_1h = pd.DataFrame({"adx_at_entry": adx_1h, "width_pct_at_entry": width_pct_1h}).reset_index(names="ts_1h")
    entries = df[["entry_time"]].sort_values("entry_time").reset_index()
    merged = pd.merge_asof(entries, indicators_1h, left_on="entry_time", right_on="ts_1h", direction="backward")
    merged = merged.set_index("index").sort_index()

    df["adx_at_entry"] = merged["adx_at_entry"].to_numpy()
    df["width_pct_at_entry"] = merged["width_pct_at_entry"].to_numpy()
    df["adx_bucket"] = pd.cut(df["adx_at_entry"], bins=ADX_BINS, labels=ADX_LABELS, right=False)
    df["width_bucket"] = pd.cut(df["width_pct_at_entry"], bins=WIDTH_BINS, labels=WIDTH_LABELS, right=False)

    df.to_csv(DIAG_DATA_DIR / "d6_trade_regime_detail.csv", index=False)

    adx_stats = _bucket_stats(df.dropna(subset=["adx_bucket"]), "adx_bucket")
    width_stats = _bucket_stats(df.dropna(subset=["width_bucket"]), "width_bucket")
    adx_stats.to_csv(DIAG_DATA_DIR / "d6_adx_bucket_stats.csv", index=False)
    width_stats.to_csv(DIAG_DATA_DIR / "d6_width_bucket_stats.csv", index=False)

    n_missing_adx = int(df["adx_at_entry"].isna().sum())
    n_missing_width = int(df["width_pct_at_entry"].isna().sum())

    summary = {
        "n_trades": len(df),
        "n_missing_adx_at_entry": n_missing_adx,
        "n_missing_width_at_entry": n_missing_width,
        "adx_buckets_reported": len(adx_stats),
        "width_buckets_reported": len(width_stats),
    }
    pd.Series(summary).to_csv(DIAG_DATA_DIR / "d6_summary.csv")
    return {"adx_stats": adx_stats.to_dict("records"), "width_stats": width_stats.to_dict("records"), "summary": summary}


if __name__ == "__main__":
    result = run()
    print("-- ADX bucket --")
    for row in result["adx_stats"]:
        print(row)
    print("-- Width percentile bucket --")
    for row in result["width_stats"]:
        print(row)
    print(result["summary"])
