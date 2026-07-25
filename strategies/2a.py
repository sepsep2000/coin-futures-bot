"""strategies/2a.py — 횡단면 상대모멘텀(시장중립), 정식 전략 코드.

SIGNAL_VALIDATION_2a.md(PASS)의 gross 신호 로직 + PORTFOLIO_2A_FILTEREDTREND_
BACKTEST_NET.md에서 채택된 net(비용반영) 계산을 그대로 이식한다. 신규 로직
없음, lookback_days/분위 등 어떤 파라미터도 재조정하지 않는다. 데이터 로딩은
scripts/diag/의 진단용 헬퍼 대신 정식 파이프라인의 src/data/feed.py(cache_path/
load_cache)를 사용 — 이 모듈이 "정식" 경로이기 때문.

비용모델은 src/backtest/cost_model.py의 taker_fee/slippage_cost/funding_fee를
그대로 호출한다(scripts/diag/portfolio_2a_net_cost.py와 동일 방식 이식).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.backtest.cost_model import funding_fee, slippage_cost, taker_fee
from src.data.feed import cache_path, load_cache

LOOKBACK_DAYS = 7  # signal_specs/2a.yaml과 동일, 재조정 없음


def _load_universe_daily_and_funding(cfg: dict, project_root: Path) -> tuple[pd.DataFrame, dict[str, pd.Series]]:
    tickers_path = project_root / "config" / "tickers.txt"
    tickers = [ln.strip() for ln in tickers_path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.strip().startswith("#")]
    ohlcv_dir = project_root / cfg["data"]["ohlcv_cache_dir"]
    funding_dir = project_root / cfg["data"]["funding_cache_dir"]

    closes, funding_raw = {}, {}
    for pair in tickers:
        base = pair.split("/")[0]
        o15 = load_cache(cache_path(ohlcv_dir, pair, "15m"))
        o15.index = pd.to_datetime(o15["timestamp"], unit="ms", utc=True)
        closes[base] = o15["close"].resample("1D").last()

        fd = load_cache(cache_path(funding_dir, pair, "funding"))
        fd = fd.copy()
        fd["ts"] = pd.to_datetime(fd["timestamp"], unit="ms", utc=True)
        funding_raw[base] = fd.set_index("ts")["funding_rate"].sort_index()

    daily_close = pd.DataFrame(closes).dropna()
    return daily_close, funding_raw


def run(cfg: dict, project_root: Path) -> pd.DataFrame:
    """반환: 주간 리밸런스 레코드 DataFrame(entry_time/exit_time/gross_return/
    txn_cost/funding_cost/net_return/turnover 등) — signal_specs/2a.yaml +
    scripts/diag/portfolio_2a_net_cost.py와 동일 스키마."""
    cost_cfg = cfg["costs"]
    daily_close, funding_raw = _load_universe_daily_and_funding(cfg, project_root)
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
        # 상위(최근 수익률 높음=모멘텀 승자) 롱, 하위(패자) 숏
        short_group, long_group = set(ranked.index[:k]), set(ranked.index[-k:])
        weight = 1.0 / k

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

    return pd.DataFrame(rows)
