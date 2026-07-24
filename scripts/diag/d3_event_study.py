"""D3 — 신호 이벤트 스터디 (청산 로직 완전 배제).

진입 신호(청산 로직·포지션 상태와 무관하게, "이 봉에서 신호 조건이 성립했는가"
자체)가 발생한 모든 시점 t에서 forward return을 여러 horizon으로 측정한다.
같은 종목·기간에 신호가 자주 겹쳐 발생해 표본 간 자기상관이 크므로,
Newey-West HAC 표준오차로 t-stat을 보정한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.diag.common import DIAG_DATA_DIR, eth_data, trend_cfg_current
from src.strategy.trend import generate_trend_signals

HORIZONS = [1, 2, 4, 8, 16, 32, 96, 192]


def newey_west_tstat(x: np.ndarray, max_lag: int) -> tuple[float, float, float]:
    """평균 0 검정용 NW HAC t-stat. 반환: (mean, hac_se, t_stat)."""
    n = len(x)
    mean = float(np.mean(x))
    demeaned = x - mean
    gamma0 = float(np.dot(demeaned, demeaned) / n)
    var = gamma0
    for lag in range(1, min(max_lag, n - 1) + 1):
        w = 1 - lag / (max_lag + 1)
        gamma_l = float(np.dot(demeaned[lag:], demeaned[:-lag]) / n)
        var += 2 * w * gamma_l
    var = max(var, 1e-18)
    se_mean = float(np.sqrt(var / n))
    t_stat = mean / se_mean if se_mean > 0 else float("nan")
    return mean, se_mean, t_stat


def _forward_returns(close: pd.Series, signal_idx: np.ndarray, horizon: int, directions: np.ndarray) -> np.ndarray:
    valid = signal_idx + horizon < len(close)
    idx = signal_idx[valid]
    dirs = directions[valid]
    fwd = (close.to_numpy()[idx + horizon] / close.to_numpy()[idx] - 1)
    return fwd * np.where(dirs == "long", 1.0, -1.0)


def run() -> dict:
    cfg = trend_cfg_current()
    data = eth_data()["ETH/USDT:USDT"]
    close = data.df_15m["close"]
    sig = generate_trend_signals(data.df_15m, cfg["trend"])

    long_idx = np.where(sig["entry_long"].to_numpy())[0]
    short_idx = np.where(sig["entry_short"].to_numpy())[0]
    signal_idx = np.concatenate([long_idx, short_idx])
    directions = np.array(["long"] * len(long_idx) + ["short"] * len(short_idx))
    order = np.argsort(signal_idx)
    signal_idx, directions = signal_idx[order], directions[order]

    rows = []
    for h in HORIZONS:
        cond_fwd = _forward_returns(close, signal_idx, h, directions)
        mean, se, t = newey_west_tstat(cond_fwd, max_lag=h)

        # 무조건부(전체 봉 대상) 비교 베이스라인 — 방향은 반씩 랜덤 부호(대칭 비교 목적)
        all_idx = np.arange(len(close) - h)
        rng = np.random.default_rng(42)
        rand_dirs = rng.choice(["long", "short"], size=len(all_idx))
        uncond_fwd = _forward_returns(close, all_idx, h, rand_dirs)
        uncond_mean = float(np.mean(uncond_fwd))

        rows.append({
            "horizon_bars": h, "horizon_hours": h * 0.25, "n_signals": len(cond_fwd),
            "mean_fwd_return_pct": mean * 100, "median_fwd_return_pct": float(np.median(cond_fwd)) * 100,
            "std_fwd_return_pct": float(np.std(cond_fwd)) * 100, "win_rate_pct": float((cond_fwd > 0).mean()) * 100,
            "nw_se": se, "t_stat": t, "unconditional_mean_pct": uncond_mean * 100,
            "diff_vs_unconditional_pct": (mean - uncond_mean) * 100,
        })

    df = pd.DataFrame(rows)
    df.to_csv(DIAG_DATA_DIR / "d3_event_study.csv", index=False)

    all_below_2 = bool((df["t_stat"].abs() < 2).all())
    summary = {
        "n_horizons": len(HORIZONS),
        "max_abs_t_stat": float(df["t_stat"].abs().max()),
        "horizon_with_max_t": int(df.loc[df["t_stat"].abs().idxmax(), "horizon_bars"]),
        "all_horizons_below_t2": all_below_2,
    }
    pd.Series(summary).to_csv(DIAG_DATA_DIR / "d3_summary.csv")
    return {"table": rows, "summary": summary}


if __name__ == "__main__":
    result = run()
    for row in result["table"]:
        print(row)
    print(result["summary"])
