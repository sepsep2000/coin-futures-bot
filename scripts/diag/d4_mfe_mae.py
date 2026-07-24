"""D4 — MFE/MAE(최대 유리/불리 편차) 분석.

각 실현 트레이드의 보유 기간 동안 봉별 high/low를 이용해 진입가 대비
방향 조정된 최대 유리 편차(MFE)와 최대 불리 편차(MAE)를 R 단위로 계산한다.
1R 가격 거리는 실제 엔진 로직과 동일하게 진입 시점 ATR × stop_atr_mult로
역산한다(트레이드 자체의 r_multiple로 역산하지 않음 — 더 정확한 방법).

목적: "진입은 맞았는데 청산이 이익을 반납시켰는가"(시나리오 C, 청산 재설계)
vs "애초에 유리한 방향으로 움직인 적이 없었는가"(시나리오 A, 신호 자체 무정보)
를 구분하기 위한 진단.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.diag.common import DIAG_DATA_DIR, eth_data, get_trend_trades, trades_to_df, trend_cfg_current
from src.strategy.trend import generate_trend_signals


def run() -> dict:
    cfg = trend_cfg_current()
    trades = get_trend_trades(use_grace=True)
    df = trades_to_df(trades)

    data = eth_data()["ETH/USDT:USDT"]
    bars = data.df_15m
    trend_sig = generate_trend_signals(bars, cfg["trend"])
    atr = trend_sig["atr"]
    stop_mult = cfg["trend"]["stop_atr_mult"]

    mfe_r_list, mae_r_list, stop_dist_list, n_bars_list = [], [], [], []
    for row in df.itertuples():
        atr_at_entry = atr.get(row.entry_time, np.nan)
        stop_dist = stop_mult * atr_at_entry if pd.notna(atr_at_entry) else np.nan
        window = bars.loc[row.entry_time:row.exit_time]
        if len(window) == 0 or pd.isna(stop_dist) or stop_dist <= 0:
            mfe_r_list.append(np.nan)
            mae_r_list.append(np.nan)
            stop_dist_list.append(stop_dist)
            n_bars_list.append(len(window))
            continue
        if row.direction == "long":
            favorable = (window["high"] - row.entry_price).clip(lower=0)
            adverse = (row.entry_price - window["low"]).clip(lower=0)
        else:
            favorable = (row.entry_price - window["low"]).clip(lower=0)
            adverse = (window["high"] - row.entry_price).clip(lower=0)
        mfe_r_list.append(float(favorable.max() / stop_dist))
        mae_r_list.append(float(adverse.max() / stop_dist))
        stop_dist_list.append(stop_dist)
        n_bars_list.append(len(window))

    df["stop_dist_price"] = stop_dist_list
    df["n_bars_in_window"] = n_bars_list
    df["mfe_r"] = mfe_r_list
    df["mae_r"] = mae_r_list
    df["capture_ratio"] = np.where(df["mfe_r"] > 0.01, df["r_multiple"] / df["mfe_r"], np.nan)
    df["giveback_r"] = df["mfe_r"] - df["r_multiple"]

    df.to_csv(DIAG_DATA_DIR / "d4_trade_mfe_mae.csv", index=False)

    valid = df.dropna(subset=["mfe_r", "mae_r"])
    winners = valid[valid["r_multiple"] > 0]
    losers = valid[valid["r_multiple"] <= 0]

    frac_losers_mfe_ge_1 = float((losers["mfe_r"] >= 1.0).mean()) if len(losers) else float("nan")
    frac_losers_mfe_ge_05 = float((losers["mfe_r"] >= 0.5).mean()) if len(losers) else float("nan")
    corr_r_mfe = float(valid["r_multiple"].corr(valid["mfe_r"])) if len(valid) > 1 else float("nan")

    summary = {
        "n_trades_valid": len(valid),
        "n_winners": len(winners),
        "n_losers": len(losers),
        "winners_mean_mfe_r": float(winners["mfe_r"].mean()) if len(winners) else float("nan"),
        "winners_median_capture_ratio": float(winners["capture_ratio"].median(skipna=True)) if len(winners) else float("nan"),
        "winners_mean_giveback_r": float(winners["giveback_r"].mean()) if len(winners) else float("nan"),
        "losers_mean_mfe_r": float(losers["mfe_r"].mean()) if len(losers) else float("nan"),
        "losers_median_mfe_r": float(losers["mfe_r"].median()) if len(losers) else float("nan"),
        "losers_mean_mae_r": float(losers["mae_r"].mean()) if len(losers) else float("nan"),
        "frac_losers_reached_mfe_ge_1R": frac_losers_mfe_ge_1,
        "frac_losers_reached_mfe_ge_0.5R": frac_losers_mfe_ge_05,
        "corr_realized_r_vs_mfe_r": corr_r_mfe,
        "mean_stop_dist_price": float(valid["stop_dist_price"].mean()),
    }
    pd.Series(summary).to_csv(DIAG_DATA_DIR / "d4_summary.csv")
    return summary


if __name__ == "__main__":
    print(run())
