"""2a 비용모델 적용(gross -> net) — src/backtest/cost_model.py의 기존 함수
(taker_fee/slippage_cost/funding_fee)를 2a의 주간 리밸런스 로직에 그대로
연결한다. 신규 백테스트 엔진 아님. 신규 매개변수(리밸런스 주기 등) 튜닝
없음 — gen_2a와 동일한 lookback_days=7, 4분위 구조 그대로, 비용만 추가.

★ 기존 scripts/validate_signal.py의 gen_2a는 수정하지 않는다 — gross
버전(SIGNAL_VALIDATION_2a.md)은 그대로 보존, 이 스크립트가 net 버전을
별도 생성한다(reports/PORTFOLIO_2A_FILTEREDTREND_BACKTEST_NET.md에서 병기).

회전율 정의: 매주 각 다리(롱 5개/숏 5개, 총 10슬롯)에서 전주 대비 바뀐
자산 수 / 10. 바뀐 자산(신규진입·청산)에만 수수료+슬리피지 부과, 그대로
유지된 포지션은 거래비용 없음(회전 없으므로). 펀딩은 보유 여부와 무관하게
그 주에 실제로 들고 있던 모든 포지션에 매주 부과(신규/유지 구분 없음 —
포지션을 들고 있으면 펀딩은 발생).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.diag.common import DIAG_DATA_DIR, load_base_config, load_symbol_data, profit_factor_from_pnls, symbol_file
from src.backtest.cost_model import funding_fee, slippage_cost, taker_fee

PROJECT_ROOT = DIAG_DATA_DIR.parent.parent
LOOKBACK_DAYS = 7  # gen_2a와 동일


def _load_universe():
    tickers_path = PROJECT_ROOT / "config" / "tickers.txt"
    tickers = [ln.strip() for ln in tickers_path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.strip().startswith("#")]
    closes, funding_raw = {}, {}
    for pair in tickers:
        d = load_symbol_data(symbol_file(pair))
        base = pair.split("/")[0]
        closes[base] = d.df_15m["close"].resample("1D").last()
        fdf = d.funding.copy()
        fdf["ts"] = pd.to_datetime(fdf["timestamp"], unit="ms", utc=True)
        funding_raw[base] = fdf.set_index("ts")["funding_rate"].sort_index()
    daily_close = pd.DataFrame(closes).dropna()
    return daily_close, funding_raw


def run() -> dict:
    cfg = load_base_config()
    cost_cfg = cfg["costs"]
    daily_close, funding_raw = _load_universe()
    weekly_closes = daily_close.resample("W").last()
    weekly_dates = weekly_closes.index
    lookback_ret = daily_close.pct_change(LOOKBACK_DAYS)

    prev_long: set = set()
    prev_short: set = set()
    rows = []

    for i in range(len(weekly_dates) - 1):
        reb_date, exit_date = weekly_dates[i], weekly_dates[i + 1]
        asof_ret = lookback_ret.asof(reb_date).dropna()
        if len(asof_ret) < 8:
            continue
        ranked = asof_ret.sort_values()
        k = max(1, len(ranked) // 4)
        # gen_2a와 동일 컨벤션: 상위(최근 수익률 높음=모멘텀 승자) 롱, 하위(패자) 숏
        short_group, long_group = set(ranked.index[:k]), set(ranked.index[-k:])
        weight = 1.0 / k  # 각 포지션이 "1단위 그로스"에서 차지하는 비중

        next_week_price_ret = weekly_closes.iloc[i + 1] / weekly_closes.iloc[i] - 1
        price_component = float(next_week_price_ret[list(long_group)].mean() - next_week_price_ret[list(short_group)].mean())

        new_long, exited_long = long_group - prev_long, prev_long - long_group
        new_short, exited_short = short_group - prev_short, prev_short - short_group
        n_changes = len(new_long) + len(exited_long) + len(new_short) + len(exited_short)
        n_slots = len(long_group) + len(short_group)
        turnover = n_changes / n_slots

        per_event_cost = taker_fee(weight, cost_cfg["taker_fee_pct"]) + slippage_cost(weight, cost_cfg["slippage_pct"])
        txn_cost = per_event_cost * n_changes

        funding_cost = 0.0
        for b in long_group:
            s = funding_raw[b]
            fsum = float(s[(s.index > reb_date) & (s.index <= exit_date)].sum())
            funding_cost += funding_fee(weight, fsum, "long")
        for b in short_group:
            s = funding_raw[b]
            fsum = float(s[(s.index > reb_date) & (s.index <= exit_date)].sum())
            funding_cost += funding_fee(weight, fsum, "short")

        net_return = price_component - txn_cost - funding_cost

        rows.append({
            "entry_time": reb_date, "exit_time": exit_date,
            "gross_return": price_component, "txn_cost": txn_cost, "funding_cost": funding_cost,
            "net_return": net_return, "n_changes": n_changes, "n_slots": n_slots, "turnover": turnover,
        })
        prev_long, prev_short = long_group, short_group

    df = pd.DataFrame(rows)
    df.to_csv(DIAG_DATA_DIR / "portfolio_2a_net_trades.csv", index=False)

    mean_weekly_turnover = float(df["turnover"].mean())
    annualized_turnover = mean_weekly_turnover * 52

    def pf_at_multiplier(m: float) -> float:
        adj = df["gross_return"] - m * (df["txn_cost"] + df["funding_cost"])
        return profit_factor_from_pnls(adj.to_numpy())

    gross_pf = profit_factor_from_pnls(df["gross_return"].to_numpy())
    net_pf = profit_factor_from_pnls(df["net_return"].to_numpy())
    lo, hi = 0.0, 10.0
    breakeven_m = None
    if pf_at_multiplier(lo) >= 1.0 and pf_at_multiplier(hi) <= 1.0:
        for _ in range(60):
            mid = (lo + hi) / 2
            if pf_at_multiplier(mid) >= 1.0:
                lo = mid
            else:
                hi = mid
        breakeven_m = (lo + hi) / 2

    summary = {
        "n_weeks": len(df),
        "mean_weekly_turnover_pct": mean_weekly_turnover * 100,
        "annualized_turnover_x": annualized_turnover,
        "mean_weekly_txn_cost_pct": float(df["txn_cost"].mean() * 100),
        "mean_weekly_funding_cost_pct": float(df["funding_cost"].mean() * 100),
        "gross_pf": gross_pf, "net_pf": net_pf, "pf_erosion": gross_pf - net_pf if np.isfinite(gross_pf) and np.isfinite(net_pf) else None,
        "breakeven_cost_multiplier": breakeven_m,
        "gross_mean_weekly_return_pct": float(df["gross_return"].mean() * 100),
        "net_mean_weekly_return_pct": float(df["net_return"].mean() * 100),
    }
    pd.Series(summary).to_csv(DIAG_DATA_DIR / "portfolio_2a_net_cost_summary.csv")

    net_weekly_series = pd.Series(df["net_return"].to_numpy(), index=pd.DatetimeIndex(df["exit_time"]))
    net_weekly_series.to_csv(DIAG_DATA_DIR / "portfolio_2a_net_weekly_returns.csv", header=["weekly_return"])

    return {"summary": summary, "df": df}


if __name__ == "__main__":
    out = run()
    print(out["summary"])
