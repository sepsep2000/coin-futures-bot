"""신호 후보 3a/3b 통계적 검증 (Signal Validation Gate, D-시리즈/D7b와 동일 방법론).

REDESIGN_PROPOSAL.md STEP 1의 3a(BB %B 되돌림)/3b(RSI 극단값 되돌림) 명세를
최소한으로만 구현해 통계적 실재 여부를 검증한다. 별도의 SIGNAL_CATALOG.md
파일은 아직 없으므로 REDESIGN_PROPOSAL.md STEP 1 텍스트를 명세 원본으로
취급한다.

★ 이것은 전략 구현이 아니라 검증용 최소 구현이다 — src/strategy/에 편입하지
않는다. 기존 trend의 ATR스탑+R배수 프레임(부분익절/샹들리에 트레일/그레이스
피리어드)을 재사용하지 않는다 — 청산은 명세 그대로 "되돌림목표(SMA20) 또는
시간청산(48봉=12h) 중 먼저" 만 쓴다. 하드 손절은 넣지 않는다 — 이 단계는
신호의 통계적 실재 여부만 보는 것이고(D3가 청산 로직을 완전 배제했던 것과
같은 정신), 리스크 관리는 검증 통과 후 실제 구현 단계의 별도 문제다.
그리드서치 없음 — BB(20, std=2.0)/RSI(14)는 config.yaml의 기존 regime/meanrev
파라미터를 그대로 재사용, time_exit_bars=48은 REDESIGN_PROPOSAL.md에 이미
명시된 값 하나만 고정 사용(스윕 없음).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.diag.common import DIAG_DATA_DIR, eth_data, get_trend_trades, load_base_config, trades_to_df
from src.strategy.indicators import bollinger_bands, rsi
from src.strategy.regime import RANGE, classify_regime

TIME_EXIT_BARS = 48  # REDESIGN_PROPOSAL.md 3a/3b 명세 값 그대로(12h), 스윕 안 함
CLUSTER_GAP_HOURS = 24  # D7b-1과 동일
N_BOOT = 5000
SEED = 3131
OVERLAP_THRESHOLD_PCT = 20.0
CORR_THRESHOLD = 0.3


def _range_mask(data, cfg) -> pd.Series:
    regime_1h = classify_regime(data.df_1h, cfg["regime"])
    regime_15m = regime_1h.reindex(data.df_15m.index, method="ffill")
    return regime_15m == RANGE


def _simulate_3a(close: pd.Series, range_mask: pd.Series, cfg) -> pd.DataFrame:
    sma, upper, lower = bollinger_bands(close, cfg["meanrev"]["bb_period"], cfg["meanrev"]["bb_std"])
    c = close.to_numpy()
    lo = lower.to_numpy()
    hi = upper.to_numpy()
    mid = sma.to_numpy()
    rmask = range_mask.to_numpy()

    touched_low = np.zeros(len(c), dtype=bool)
    touched_high = np.zeros(len(c), dtype=bool)
    trades = []
    was_below, was_above = False, False
    for i in range(1, len(c)):
        if np.isnan(lo[i]) or np.isnan(hi[i]):
            continue
        if c[i - 1] <= lo[i - 1]:
            was_below = True
        if c[i - 1] >= hi[i - 1]:
            was_above = True
        entered_long = was_below and c[i] > lo[i] and rmask[i]
        entered_short = was_above and c[i] < hi[i] and rmask[i]
        if entered_long:
            trades.append(_walk_to_target_or_time(c, mid, i, "long"))
            was_below = False
        if entered_short:
            trades.append(_walk_to_target_or_time(c, mid, i, "short"))
            was_above = False
    return pd.DataFrame(trades)


def _simulate_3b(close: pd.Series, range_mask: pd.Series, cfg) -> pd.DataFrame:
    r = rsi(close, cfg["meanrev"]["rsi_period"]).to_numpy()
    c = close.to_numpy()
    rmask = range_mask.to_numpy()
    oversold, overbought = cfg["meanrev"]["rsi_oversold"], cfg["meanrev"]["rsi_overbought"]

    trades = []
    for i in range(1, len(c)):
        if np.isnan(r[i]) or np.isnan(r[i - 1]):
            continue
        entered_long = r[i - 1] >= oversold and r[i] < oversold and rmask[i]
        entered_short = r[i - 1] <= overbought and r[i] > overbought and rmask[i]
        if entered_long:
            trades.append(_walk_to_rsi50_or_time(c, r, i, "long"))
        if entered_short:
            trades.append(_walk_to_rsi50_or_time(c, r, i, "short"))
    return pd.DataFrame(trades)


def _walk_to_target_or_time(c: np.ndarray, mid: np.ndarray, entry_idx: int, direction: str) -> dict:
    entry_price = c[entry_idx]
    end_idx = min(entry_idx + TIME_EXIT_BARS, len(c) - 1)
    exit_idx, reason = end_idx, "time_exit"
    for j in range(entry_idx + 1, end_idx + 1):
        if np.isnan(mid[j]):
            continue
        if direction == "long" and c[j] >= mid[j]:
            exit_idx, reason = j, "target"
            break
        if direction == "short" and c[j] <= mid[j]:
            exit_idx, reason = j, "target"
            break
    exit_price = c[exit_idx]
    ret = exit_price / entry_price - 1 if direction == "long" else entry_price / exit_price - 1
    return {"entry_idx": entry_idx, "exit_idx": exit_idx, "direction": direction, "exit_reason": reason, "return": ret}


def _walk_to_rsi50_or_time(c: np.ndarray, r: np.ndarray, entry_idx: int, direction: str) -> dict:
    entry_price = c[entry_idx]
    end_idx = min(entry_idx + TIME_EXIT_BARS, len(c) - 1)
    exit_idx, reason = end_idx, "time_exit"
    for j in range(entry_idx + 1, end_idx + 1):
        if np.isnan(r[j]):
            continue
        if direction == "long" and r[j] >= 50:
            exit_idx, reason = j, "target"
            break
        if direction == "short" and r[j] <= 50:
            exit_idx, reason = j, "target"
            break
    exit_price = c[exit_idx]
    ret = exit_price / entry_price - 1 if direction == "long" else entry_price / exit_price - 1
    return {"entry_idx": entry_idx, "exit_idx": exit_idx, "direction": direction, "exit_reason": reason, "return": ret}


def _bars_covered(windows: list[tuple[int, int]], n_bars: int) -> np.ndarray:
    mask = np.zeros(n_bars, dtype=bool)
    for s, e in windows:
        mask[s:e + 1] = True
    return mask


def step_a_exclusivity(trades_df: pd.DataFrame, trend_windows: list[tuple[int, int]], n_bars: int, label: str) -> dict:
    cand_windows = list(zip(trades_df["entry_idx"], trades_df["exit_idx"]))
    cand_mask = _bars_covered(cand_windows, n_bars)
    trend_mask = _bars_covered(trend_windows, n_bars)
    overlap_bars = int((cand_mask & trend_mask).sum())
    cand_bars = int(cand_mask.sum())
    overlap_pct = overlap_bars / cand_bars * 100 if cand_bars else float("nan")
    return {
        "candidate": label, "n_trades": len(trades_df), "cand_active_bars": cand_bars,
        "overlap_bars": overlap_bars, "overlap_pct": overlap_pct,
        "exclusivity_confirmed": overlap_pct < OVERLAP_THRESHOLD_PCT,
    }


def _cluster_events(entry_idx: np.ndarray, bar_index: pd.DatetimeIndex, gap_hours: int) -> np.ndarray:
    ts = bar_index[entry_idx]
    order = np.argsort(ts.to_numpy())
    ts_sorted = ts.to_numpy()[order]
    cid = np.zeros(len(ts_sorted), dtype=int)
    c = 0
    for i in range(1, len(ts_sorted)):
        gap = (ts_sorted[i] - ts_sorted[i - 1]) / np.timedelta64(1, "h")
        if gap > gap_hours:
            c += 1
        cid[i] = c
    out = np.empty(len(ts_sorted), dtype=int)
    out[order] = cid
    return out


def step_b_clustering(trades_df: pd.DataFrame, bar_index: pd.DatetimeIndex, label: str) -> dict:
    trades_df["cluster_id"] = _cluster_events(trades_df["entry_idx"].to_numpy(), bar_index, CLUSTER_GAP_HOURS)
    sizes = trades_df.groupby("cluster_id").size()
    return {
        "candidate": label, "n_events_raw": len(trades_df), "n_independent_clusters": trades_df["cluster_id"].nunique(),
        "cluster_size_mean": float(sizes.mean()), "cluster_size_max": int(sizes.max()),
        "pct_events_in_clusters_ge2": float(sizes[sizes >= 2].sum() / len(trades_df) * 100),
    }


def _profit_factor(returns: np.ndarray) -> float:
    gains = returns[returns > 0].sum()
    losses = -returns[returns < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else float("nan")
    return float(gains / losses)


def step_c_bootstrap(trades_df: pd.DataFrame, label: str, rng: np.random.Generator) -> dict:
    clusters = trades_df.groupby("cluster_id")["return"].apply(lambda s: s.to_numpy()).to_list()
    n_clusters = len(clusters)
    boot_mean = np.empty(N_BOOT)
    boot_pf = np.empty(N_BOOT)
    for b in range(N_BOOT):
        chosen = rng.integers(0, n_clusters, size=n_clusters)
        pooled = np.concatenate([clusters[c] for c in chosen])
        boot_mean[b] = pooled.mean()
        boot_pf[b] = _profit_factor(pooled)
    finite_pf = boot_pf[np.isfinite(boot_pf)]
    return {
        "candidate": label, "n_clusters": n_clusters,
        "point_mean_return_pct": float(trades_df["return"].mean() * 100),
        "mean_ci95_lo_pct": float(np.percentile(boot_mean, 2.5) * 100),
        "mean_ci95_hi_pct": float(np.percentile(boot_mean, 97.5) * 100),
        "point_pf": _profit_factor(trades_df["return"].to_numpy()),
        "pf_ci95_lo": float(np.percentile(finite_pf, 2.5)) if len(finite_pf) else float("nan"),
        "pf_ci95_hi": float(np.percentile(finite_pf, 97.5)) if len(finite_pf) else float("nan"),
        "mean_excludes_0": bool(np.percentile(boot_mean, 2.5) > 0 or np.percentile(boot_mean, 97.5) < 0),
        "pf_excludes_1": bool((np.percentile(finite_pf, 2.5) > 1.0 or np.percentile(finite_pf, 97.5) < 1.0)) if len(finite_pf) else False,
    }


def step_d_correlation(trades_df: pd.DataFrame, bar_index: pd.DatetimeIndex, trend_trades_df: pd.DataFrame, label: str) -> dict:
    exit_ts = bar_index[trades_df["exit_idx"].to_numpy()]
    weekly_cand = pd.Series(trades_df["return"].to_numpy(), index=exit_ts).groupby(pd.Grouper(freq="W")).sum()
    weekly_trend = trend_trades_df.set_index("exit_time")["r_multiple"].groupby(pd.Grouper(freq="W")).sum()
    weekly_trend.index = weekly_trend.index.tz_convert(weekly_cand.index.tz) if weekly_cand.index.tz else weekly_trend.index

    full_idx = weekly_cand.index.union(weekly_trend.index)
    a = weekly_cand.reindex(full_idx, fill_value=0.0)
    b = weekly_trend.reindex(full_idx, fill_value=0.0)
    corr = float(np.corrcoef(a.to_numpy(), b.to_numpy())[0, 1])
    return {"candidate": label, "n_weeks_compared": len(full_idx), "weekly_return_correlation": corr, "is_independent": corr < CORR_THRESHOLD}


def run() -> dict:
    cfg = load_base_config()
    data = eth_data()["ETH/USDT:USDT"]
    close = data.df_15m["close"]
    bar_index = data.df_15m.index
    n_bars = len(close)
    rmask = _range_mask(data, cfg)

    trend_trades = get_trend_trades(use_grace=True)
    trend_df = trades_to_df(trend_trades)
    entry_pos = bar_index.searchsorted(trend_df["entry_time"].to_numpy())
    exit_pos = bar_index.searchsorted(trend_df["exit_time"].to_numpy())
    trend_windows = list(zip(np.clip(entry_pos, 0, n_bars - 1), np.clip(exit_pos, 0, n_bars - 1)))

    df_3a = _simulate_3a(close, rmask, cfg)
    df_3b = _simulate_3b(close, rmask, cfg)
    df_3a.to_csv(DIAG_DATA_DIR / "signalval_3a_trades.csv", index=False)
    df_3b.to_csv(DIAG_DATA_DIR / "signalval_3b_trades.csv", index=False)

    step_a = [
        step_a_exclusivity(df_3a, trend_windows, n_bars, "3a"),
        step_a_exclusivity(df_3b, trend_windows, n_bars, "3b"),
    ]

    step_b = [step_b_clustering(df_3a, bar_index, "3a"), step_b_clustering(df_3b, bar_index, "3b")]

    rng = np.random.default_rng(SEED)
    step_c = [step_c_bootstrap(df_3a, "3a", rng), step_c_bootstrap(df_3b, "3b", rng)]

    step_d = [
        step_d_correlation(df_3a, bar_index, trend_df, "3a"),
        step_d_correlation(df_3b, bar_index, trend_df, "3b"),
    ]

    pd.DataFrame(step_a).to_csv(DIAG_DATA_DIR / "signalval_stepA_exclusivity.csv", index=False)
    pd.DataFrame(step_b).to_csv(DIAG_DATA_DIR / "signalval_stepB_clustering.csv", index=False)
    pd.DataFrame(step_c).to_csv(DIAG_DATA_DIR / "signalval_stepC_bootstrap.csv", index=False)
    pd.DataFrame(step_d).to_csv(DIAG_DATA_DIR / "signalval_stepD_correlation.csv", index=False)

    return {"step_a": step_a, "step_b": step_b, "step_c": step_c, "step_d": step_d}


if __name__ == "__main__":
    result = run()
    for step_name, rows in result.items():
        print(f"-- {step_name} --")
        for row in rows:
            print(row)
