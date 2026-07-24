"""src/backtest/walkforward.py — SPEC 3절 워크포워드 검증 (Phase 3, 게이트 G2/G3).

학습(train) 구간에서 그리드 탐색으로 파라미터를 고르고, 그 파라미터를 그대로
검증(test/OOS) 구간에 적용한다. **파라미터 탐색은 학습 구간에서만** 일어난다 —
OOS 성과를 보고 파라미터를 고르면 과최적화(SPEC 4절 금지). require_confirmation_bar
같은 이미 결정된 설계 선택은 그리드에 넣지 않고 고정값으로 검증한다.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from itertools import product
from typing import Optional

import numpy as np
import pandas as pd

from src.backtest.engine import SymbolData, Trade, run_backtest
from src.backtest.report import (
    compute_max_drawdown_pct,
    compute_profit_factor,
    compute_sharpe_annualized,
    compute_win_rate_pct,
)


@dataclass(frozen=True)
class WalkforwardWindow:
    window_id: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def _to_utc(ts) -> pd.Timestamp:
    """문자열/naive/aware 어떤 입력이 와도 UTC-aware Timestamp로 통일한다 —
    데이터(feed.py 로더)가 전부 UTC-aware라 경계값 비교 시 tz가 안 맞으면
    pandas가 TypeError를 낸다(2026-07-24 실측)."""
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def generate_windows(
    start: pd.Timestamp, end: pd.Timestamp, train_months: int, test_months: int,
) -> list[WalkforwardWindow]:
    """학습 train_months개월 / 검증 test_months개월 롤링 윈도우. 완전한 윈도우만
    생성한다(끝자락에 test 구간이 다 안 채워지면 그 윈도우는 버림 — 부분 OOS로
    성과를 부풀리지 않기 위함)."""
    start, end = _to_utc(start), _to_utc(end)
    windows = []
    train_start = start
    window_id = 0
    while True:
        train_end = train_start + pd.DateOffset(months=train_months)
        test_end = train_end + pd.DateOffset(months=test_months)
        if test_end > end:
            break
        window_id += 1
        windows.append(WalkforwardWindow(window_id, train_start, train_end, train_end, test_end))
        train_start = train_start + pd.DateOffset(months=test_months)
    return windows


def _slice(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """[start, end) 반개구간 — 경계 봉이 train/test 양쪽에 중복 포함되지 않게 한다."""
    return df[(df.index >= start) & (df.index < end)]


def _slice_symbol_data(sd: SymbolData, start: pd.Timestamp, end: pd.Timestamp) -> SymbolData:
    return SymbolData(df_15m=_slice(sd.df_15m, start, end), df_1h=_slice(sd.df_1h, start, end), funding=sd.funding)


def _apply_grid_params(base_cfg: dict, donchian_period: int, stop_atr_mult: float) -> dict:
    cfg = copy.deepcopy(base_cfg)
    cfg["trend"]["donchian_period"] = donchian_period
    cfg["trend"]["stop_atr_mult"] = stop_atr_mult
    return cfg


def _pf_rank_key(pf: Optional[float]) -> float:
    """None(거래 없음)은 최악, inf(손실 0)는 최상으로 정렬 키를 만든다."""
    if pf is None:
        return float("-inf")
    return pf


def select_best_params(
    data_train: dict[str, SymbolData], base_cfg: dict, grid: dict,
) -> tuple[dict, list[dict]]:
    """학습 구간에서 그리드 전 조합을 실행해 PF(Profit Factor) 기준 최고 조합을 고른다.
    반환: (선택된 파라미터, 조합별 결과 목록[리포트용])."""
    combos = list(product(grid["donchian_period"], grid["trend_stop_atr_mult"]))
    results = []
    for donchian_period, stop_atr_mult in combos:
        cfg = _apply_grid_params(base_cfg, donchian_period, stop_atr_mult)
        result = run_backtest(data_train, cfg)
        pf = compute_profit_factor(result.trades)
        results.append({
            "donchian_period": donchian_period, "stop_atr_mult": stop_atr_mult,
            "pf": pf, "n_trades": len(result.trades),
        })
    best = max(results, key=lambda r: _pf_rank_key(r["pf"]))
    return {"donchian_period": best["donchian_period"], "stop_atr_mult": best["stop_atr_mult"]}, results


@dataclass
class WindowResult:
    window: WalkforwardWindow
    grid_results: list[dict]
    best_params: dict
    is_pf: Optional[float]
    is_sharpe: Optional[float]
    is_trades: int
    oos_pf: Optional[float]
    oos_sharpe: Optional[float]
    oos_trades: list[Trade] = field(default_factory=list)


@dataclass
class WalkforwardResult:
    windows: list[WindowResult]
    all_oos_trades: list[Trade]
    stitched_equity_curve: list[tuple]


def _stitch_equity_curve(oos_trades: list[Trade], initial_equity: float) -> list[tuple]:
    """OOS 트레이드를 시간순으로 이어붙여 단일 equity curve를 만든다.

    ★ 근사: 각 윈도우 내부 포지션 사이징은 그 윈도우 자체의 initial_equity_usd
    기준으로 계산됐다(윈도우 간 복리 반영 안 됨) — 여기서는 그 실현 손익을
    하나의 계좌 잔고에 순서대로 누적하는 방식으로 '합산 성과'를 근사한다.
    PF/승률/거래수는 이 근사와 무관하게 정확하다(달러 합산이라 순서 무관)."""
    sorted_trades = sorted(oos_trades, key=lambda t: t.exit_time)
    equity = initial_equity
    curve = [(sorted_trades[0].entry_time if sorted_trades else pd.Timestamp.now(tz="UTC"), equity)]
    for t in sorted_trades:
        equity += t.pnl_usd
        curve.append((t.exit_time, equity))
    return curve


def run_walkforward(
    data: dict[str, SymbolData], base_cfg: dict, windows: list[WalkforwardWindow],
) -> WalkforwardResult:
    window_results: list[WindowResult] = []
    all_oos_trades: list[Trade] = []

    grid = base_cfg["walkforward"]["grid"]
    initial_equity = base_cfg["account"]["initial_equity_usd"]

    for w in windows:
        data_train = {sym: _slice_symbol_data(sd, w.train_start, w.train_end) for sym, sd in data.items()}
        data_test = {sym: _slice_symbol_data(sd, w.test_start, w.test_end) for sym, sd in data.items()}

        best_params, grid_results = select_best_params(data_train, base_cfg, grid)
        chosen_cfg = _apply_grid_params(base_cfg, best_params["donchian_period"], best_params["stop_atr_mult"])

        train_result = run_backtest(data_train, chosen_cfg)
        test_result = run_backtest(data_test, chosen_cfg)

        all_oos_trades.extend(test_result.trades)
        window_results.append(WindowResult(
            window=w, grid_results=grid_results, best_params=best_params,
            is_pf=compute_profit_factor(train_result.trades),
            is_sharpe=compute_sharpe_annualized(train_result.equity_curve),
            is_trades=len(train_result.trades),
            oos_pf=compute_profit_factor(test_result.trades),
            oos_sharpe=compute_sharpe_annualized(test_result.equity_curve),
            oos_trades=test_result.trades,
        ))

    stitched = _stitch_equity_curve(all_oos_trades, initial_equity)
    return WalkforwardResult(windows=window_results, all_oos_trades=all_oos_trades, stitched_equity_curve=stitched)


# ---------------------------------------------------------------------------
# G2 판정
# ---------------------------------------------------------------------------

@dataclass
class G2Result:
    passed: bool
    n_oos_trades: int
    profit_factor: Optional[float]
    max_drawdown_pct: Optional[float]
    positive_month_pct: Optional[float]
    failures: list[str] = field(default_factory=list)


def compute_monthly_positive_ratio(oos_trades: list[Trade]) -> Optional[float]:
    """OOS 트레이드를 청산월 기준으로 묶어 월손익 양수 비율(%)을 낸다."""
    if not oos_trades:
        return None
    df = pd.DataFrame({"exit_time": [t.exit_time for t in oos_trades], "pnl": [t.pnl_usd for t in oos_trades]})
    monthly = df.groupby(df["exit_time"].dt.tz_localize(None).dt.to_period("M"))["pnl"].sum()
    if monthly.empty:
        return None
    return float((monthly > 0).sum() / len(monthly) * 100)


def evaluate_g2(result: WalkforwardResult, gates_cfg: dict) -> G2Result:
    trades = result.all_oos_trades
    n_trades = len(trades)
    pf = compute_profit_factor(trades)
    max_dd = compute_max_drawdown_pct(result.stitched_equity_curve)
    positive_month_pct = compute_monthly_positive_ratio(trades)

    failures = []
    if n_trades < gates_cfg["g2_min_oos_trades"]:
        failures.append(f"OOS 거래수 {n_trades} < {gates_cfg['g2_min_oos_trades']}")
    if pf is None or pf < gates_cfg["g2_min_profit_factor"]:
        failures.append(f"PF {pf} < {gates_cfg['g2_min_profit_factor']}")
    if max_dd is None or max_dd > gates_cfg["g2_max_drawdown_pct"]:
        failures.append(f"MaxDD {max_dd} > {gates_cfg['g2_max_drawdown_pct']}%")
    if positive_month_pct is None or positive_month_pct < gates_cfg["g2_min_positive_month_pct"]:
        failures.append(f"월간 양수 비율 {positive_month_pct} < {gates_cfg['g2_min_positive_month_pct']}%")

    return G2Result(
        passed=not failures, n_oos_trades=n_trades, profit_factor=pf,
        max_drawdown_pct=max_dd, positive_month_pct=positive_month_pct, failures=failures,
    )


# ---------------------------------------------------------------------------
# G3 판정 (몬테카를로)
# ---------------------------------------------------------------------------

@dataclass
class G3Result:
    passed: bool
    max_drawdown_p95_pct: Optional[float]
    n_runs: int
    failures: list[str] = field(default_factory=list)


def monte_carlo_max_drawdowns(oos_trades: list[Trade], initial_equity: float, n_runs: int, seed: int = 42) -> np.ndarray:
    """OOS 트레이드 손익 순서를 n_runs회 셔플해서(고정 시드, 재현 가능) 매번의
    MaxDD를 계산한다. 반환: MaxDD(%) 배열, 길이 n_runs."""
    if not oos_trades:
        return np.array([])
    pnls = np.array([t.pnl_usd for t in oos_trades])
    rng = np.random.default_rng(seed)
    maxdds = np.empty(n_runs)
    for i in range(n_runs):
        shuffled = rng.permutation(pnls)
        equity = initial_equity + np.cumsum(shuffled)
        equity_with_start = np.concatenate([[initial_equity], equity])
        peak = np.maximum.accumulate(equity_with_start)
        dd = np.where(peak > 0, (peak - equity_with_start) / peak * 100, 0.0)
        maxdds[i] = dd.max()
    return maxdds


def evaluate_g3(result: WalkforwardResult, initial_equity: float, gates_cfg: dict) -> G3Result:
    maxdds = monte_carlo_max_drawdowns(result.all_oos_trades, initial_equity, gates_cfg["g3_monte_carlo_runs"])
    if maxdds.size == 0:
        return G3Result(passed=False, max_drawdown_p95_pct=None, n_runs=0, failures=["OOS 트레이드 없음"])
    p95 = float(np.percentile(maxdds, 95))
    failures = []
    if p95 > gates_cfg["g3_max_drawdown_p95_pct"]:
        failures.append(f"MaxDD 95%ile {p95:.1f}% > {gates_cfg['g3_max_drawdown_p95_pct']}%")
    return G3Result(passed=not failures, max_drawdown_p95_pct=p95, n_runs=len(maxdds), failures=failures)
