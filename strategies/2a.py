"""strategies/2a.py — 횡단면 상대모멘텀(시장중립), 정식 전략 코드.

SIGNAL_VALIDATION_2a.md(PASS)의 gross 신호 로직 + PORTFOLIO_2A_FILTEREDTREND_
BACKTEST_NET.md에서 채택된 net(비용반영) 계산을 그대로 이식한다. 신규 로직
없음, lookback_days/분위 등 어떤 파라미터도 재조정하지 않는다. 데이터 로딩은
scripts/diag/의 진단용 헬퍼 대신 정식 파이프라인의 src/data/feed.py(cache_path/
load_cache)를 사용 — 이 모듈이 "정식" 경로이기 때문.

비용모델은 src/backtest/cost_model.py의 taker_fee/slippage_cost/funding_fee를
그대로 호출한다(scripts/diag/portfolio_2a_net_cost.py와 동일 방식 이식).

★ src/live/scheduler.py 편입 시 추출(2026-07-25, 수치 변경 없음): 랭킹 로직
(정렬 -> k=len//4 -> 롱/숏 그룹 -> weight=1/k)은 원래 run()의 백테스트 루프
안에 인라인이었다. 이걸 `_compute_target_groups()`로 그대로 추출해 run()과
`run_live_step()`(라이브 스케줄러 전용, 이번 주 대상 그룹만 계산)이 동일하게
호출하게 했다 — SPEC 3절 "전략은 순수함수, 백테스트와 라이브가 동일 함수
호출" 원칙을 지키기 위해 필요(스케줄러가 이 랭킹 계산을 별도로 재구현하면
원칙 위반). run()의 출력은 리팩터 전후 바이트 단위로 동일함을 재확인함
(reports/REBALANCE_ORDER_ANALYSIS.md 작업 중 실측 diff, 185행 전부 일치).
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


def _compute_target_groups(asof_ret: pd.Series) -> tuple[set, set, float]:
    """이번 랭킹 시점 -> 롱/숏 대상 자산 집합 + 슬롯당 가중치. run()의 백테스트
    루프와 run_live_step()이 동일하게 호출하는 공용 함수 — 원래 run() 안에
    인라인이었던 로직(정렬, k=len//4, weight=1/k)을 그대로 추출한 것뿐, 수치
    변경 없음. len(asof_ret) < 8이면 표본 부족으로 빈 그룹을 반환한다(원래
    run()의 `continue` 조건과 동일)."""
    if len(asof_ret) < 8:
        return set(), set(), 0.0
    ranked = asof_ret.sort_values()
    k = max(1, len(ranked) // 4)
    # 상위(최근 수익률 높음=모멘텀 승자) 롱, 하위(패자) 숏
    short_group, long_group = set(ranked.index[:k]), set(ranked.index[-k:])
    weight = 1.0 / k
    return long_group, short_group, weight


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
        long_group, short_group, weight = _compute_target_groups(asof_ret)
        if not long_group:
            continue

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


# ETH/USDT:USDT는 config/tickers.txt(2a 유니버스)에 포함돼 있지만
# filtered_trend이 거래하는 바로 그 심볼이다 — Binance USDM 원웨이 모드에서는
# 심볼당 격리마진 포지션이 하나뿐이라 두 전략이 동시에 ETH를 들면 거래소
# 레벨에서 넷팅되어 각 전략의 포지션 추적이 깨진다(reports/
# REBALANCE_ORDER_ANALYSIS.md 4절 실측). 근본 해결(헤지모드 전환 등)은 이번
# 범위 밖 — 임시 완화책으로 **라이브 경로에서만** ETH를 유니버스에서 제외한다.
# ★ 백테스트 run()은 건드리지 않는다(20자산 그대로) — 게이트 통과 수치는
# 이 상수와 무관하다, run_live_step()에서만 참조.
LIVE_EXCLUDED_SYMBOLS = {"ETH"}


def run_live_step(cfg: dict, project_root: Path, as_of_ts: pd.Timestamp) -> dict:
    """라이브 스케줄러(src/live/scheduler.py) 전용 — "이번 주" 대상 롱/숏
    그룹만 계산한다(run()처럼 전체 이력을 순회하지 않음). 랭킹 로직은
    `_compute_target_groups()`로 run()과 완전히 동일한 함수를 호출한다(SPEC
    3절 원칙). 데이터는 로컬 parquet 캐시에서 읽는다 — 호출 전에 스케줄러가
    `src.data.feed.update_ohlcv_cache`/`update_funding_cache`로 캐시를 최신화
    했다고 가정한다(이 함수 자체는 네트워크 호출을 하지 않는다, 순수 계산).

    반환: {"long_group": set[str], "short_group": set[str], "weight": float,
    "as_of": pd.Timestamp} — base(심볼) 문자열 집합, ccxt pair 문자열이 아님
    (daily_close 컬럼명이 base라 run()과 동일 형식 유지).

    ★ ETH 제외로 유니버스가 20 -> 19자산이 되어 k(=len//4)가 5 -> 4로
    줄어든다(실측 확인, weight도 0.2 -> 0.25) — run()의 백테스트 수치와는
    무관(run()은 20자산 그대로), 라이브 실행에만 적용되는 부수효과."""
    daily_close, _funding_raw = _load_universe_daily_and_funding(cfg, project_root)
    daily_close = daily_close.drop(columns=[c for c in LIVE_EXCLUDED_SYMBOLS if c in daily_close.columns])

    lookback_ret = daily_close.pct_change(LOOKBACK_DAYS)
    asof_ret = lookback_ret.asof(as_of_ts).dropna()
    long_group, short_group, weight = _compute_target_groups(asof_ret)

    return {"long_group": long_group, "short_group": short_group, "weight": weight, "as_of": as_of_ts}
