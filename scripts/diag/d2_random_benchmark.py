"""D2 — 랜덤 진입 벤치마크 (핵심 판정).

실제 trend 전략과 다음을 일치시킨 무작위 진입 시뮬레이션 N회:
  (a) 진입(엔트리) 수      — 실제 전략의 고유 진입 횟수(entry_time 기준)와 동일
  (b) 롱/숏 방향 분포      — 실제 방향 비율 그대로 셔플
  (c) 청산 로직            — src.backtest.engine._manage_trend_position() 그대로 재사용(수정 없음)
  (d) 보유기간 분포        — 강제하지 않음. 동일 청산 로직을 동일 가격 데이터에 적용하면
                             자연스럽게 나오는 결과이지 별도로 맞출 대상이 아님(방법론 각주 참조)

무작위 진입 시점은 실제 전략과 동일한 전제(레짐==TREND, 지표 웜업 완료)를 공유하는
봉 중에서만 뽑는다 — "레짐 필터를 넘어, 진입 신호 자체가 타이밍 정보를 주는가"만
분리해서 검정하기 위함이다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.diag.common import (
    DIAG_DATA_DIR,
    eth_data,
    get_trend_trades,
    profit_factor_from_pnls,
    trades_to_df,
    trend_cfg_current,
)
from src.backtest.cost_model import entry_fill_price, taker_fee
from src.backtest.engine import Position, _manage_trend_position
from src.backtest.report import compute_sharpe_annualized
from src.risk import position_size
from src.strategy.regime import TREND, classify_regime
from src.strategy.trend import generate_trend_signals, trailing_stop

N_SIMULATIONS = 1000
SEED = 12345


def _avg_r(trades) -> float:
    return float(np.mean([t.r_multiple for t in trades])) if trades else float("nan")


def _sharpe(trades, initial_equity) -> float:
    if not trades:
        return float("nan")
    sorted_t = sorted(trades, key=lambda t: t.exit_time)
    equity = initial_equity
    curve = [(sorted_t[0].entry_time, equity)]
    for t in sorted_t:
        equity += t.pnl_usd
        curve.append((t.exit_time, equity))
    sharpe = compute_sharpe_annualized(curve)
    return sharpe if sharpe is not None else float("nan")


def _simulate_one(rng, df_15m, eligible_idx, atr14, chand_long, chand_short, n_entries, n_long, cfg, cost_cfg, initial_equity):
    chosen = rng.choice(eligible_idx, size=n_entries, replace=False)
    directions = np.array(["long"] * n_long + ["short"] * (n_entries - n_long))
    rng.shuffle(directions)

    all_trades = []
    equity = initial_equity
    for idx, direction in zip(chosen, directions):
        atr_val = atr14.iloc[idx]
        if pd.isna(atr_val) or atr_val <= 0:
            continue
        entry_bar = df_15m.iloc[idx]
        entry_ts = df_15m.index[idx]
        entry_price = entry_fill_price(entry_bar["close"], direction, cost_cfg["slippage_pct"], "market")
        stop_mult = cfg["trend"]["stop_atr_mult"]
        initial_stop = entry_price - stop_mult * atr_val if direction == "long" else entry_price + stop_mult * atr_val
        qty = position_size(
            equity, entry_price, initial_stop, cfg["account"]["risk_per_trade_pct"],
            max_leverage=cfg["account"]["leverage"],
        )
        if qty <= 0:
            continue
        fee = taker_fee(qty * entry_price, cost_cfg["taker_fee_pct"])
        position = Position(
            symbol="ETH/USDT:USDT", strategy="trend", direction=direction,
            entry_time=entry_ts, entry_price=entry_price, qty=qty,
            initial_stop=initial_stop, current_stop=initial_stop,
            initial_stop_distance=abs(entry_price - initial_stop), entry_fee_usd=fee,
        )
        j = idx + 1
        while j < len(df_15m):
            bar = df_15m.iloc[j]
            trail = chand_long.iloc[j] if direction == "long" else chand_short.iloc[j]
            trend_row = pd.Series({"chandelier_stop": trail})
            new_pos, bar_trades, pnl = _manage_trend_position(position, bar, trend_row, cfg["trend"], cost_cfg)
            all_trades.extend(bar_trades)
            equity += pnl
            if new_pos is None:
                break
            position = new_pos
            j += 1
    return all_trades


def run(n_simulations: int = N_SIMULATIONS) -> dict:
    cfg = trend_cfg_current()
    cost_cfg = cfg["costs"]
    initial_equity = cfg["account"]["initial_equity_usd"]

    real_trades = get_trend_trades(use_grace=True)
    real_df = trades_to_df(real_trades)
    entries = real_df.groupby("entry_time").first().reset_index()
    n_entries = len(entries)
    n_long = int((entries["direction"] == "long").sum())

    real_pf = profit_factor_from_pnls(real_df["pnl_usd"].to_numpy())
    real_avg_r = float(real_df["r_multiple"].mean())
    real_sharpe = _sharpe(real_trades, initial_equity)

    data = eth_data()["ETH/USDT:USDT"]
    regime_1h = classify_regime(data.df_1h, cfg["regime"])
    regime_15m = regime_1h.reindex(data.df_15m.index, method="ffill")
    trend_sig = generate_trend_signals(data.df_15m, cfg["trend"])
    atr14 = trend_sig["atr"]
    chand_long = trailing_stop(data.df_15m, cfg["trend"], "long")
    chand_short = trailing_stop(data.df_15m, cfg["trend"], "short")

    eligible_mask = (regime_15m == TREND) & atr14.notna()
    eligible_idx = np.where(eligible_mask.to_numpy())[0]
    eligible_idx = eligible_idx[eligible_idx < len(data.df_15m) - 1]

    rng = np.random.default_rng(SEED)
    rows = []
    for i in range(n_simulations):
        sim_trades = _simulate_one(
            rng, data.df_15m, eligible_idx, atr14, chand_long, chand_short,
            n_entries, n_long, cfg, cost_cfg, initial_equity,
        )
        pf = profit_factor_from_pnls(np.array([t.pnl_usd for t in sim_trades])) if sim_trades else float("nan")
        avg_r = _avg_r(sim_trades)
        sharpe = _sharpe(sim_trades, initial_equity)
        rows.append({"sim": i, "n_trade_records": len(sim_trades), "pf": pf, "avg_r": avg_r, "sharpe": sharpe})
        if (i + 1) % 100 == 0:
            print(f"  진행: {i + 1}/{n_simulations}", flush=True)

    sim_df = pd.DataFrame(rows)
    sim_df.to_csv(DIAG_DATA_DIR / "d2_random_benchmark_runs.csv", index=False)

    def percentile_rank(real_value: float, null_values: np.ndarray) -> float:
        valid = null_values[~np.isnan(null_values)]
        if len(valid) == 0 or np.isnan(real_value):
            return float("nan")
        return float((valid < real_value).sum() / len(valid) * 100)

    pf_pctile = percentile_rank(real_pf, sim_df["pf"].to_numpy())
    avgr_pctile = percentile_rank(real_avg_r, sim_df["avg_r"].to_numpy())
    sharpe_pctile = percentile_rank(real_sharpe, sim_df["sharpe"].to_numpy())

    summary = {
        "n_simulations": n_simulations,
        "n_entries_matched": n_entries,
        "n_long_matched": n_long,
        "real_pf": real_pf,
        "real_avg_r": real_avg_r,
        "real_sharpe": real_sharpe,
        "null_pf_median": float(np.nanmedian(sim_df["pf"])),
        "null_avg_r_median": float(np.nanmedian(sim_df["avg_r"])),
        "null_sharpe_median": float(np.nanmedian(sim_df["sharpe"])),
        "real_pf_percentile": pf_pctile,
        "real_avg_r_percentile": avgr_pctile,
        "real_sharpe_percentile": sharpe_pctile,
        "verdict_pass_95pct": bool(pf_pctile is not None and pf_pctile == pf_pctile and pf_pctile >= 95),
    }
    pd.Series(summary).to_csv(DIAG_DATA_DIR / "d2_summary.csv")
    return summary


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else N_SIMULATIONS
    print(run(n))
