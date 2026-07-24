"""scripts/run_walkforward.py — Phase 3: 워크포워드 검증 (G2·G3 게이트).

학습 6개월/검증 2개월 롤링, 2023-01~현재. 그리드 탐색은 학습 구간에서만
(SPEC 4절 과최적화 방지). 결과를 WALKFORWARD_REPORT.md로 저장한다.

사용:
    python scripts/run_walkforward.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.backtest.engine import SymbolData  # noqa: E402
from src.backtest.walkforward import (  # noqa: E402
    evaluate_g2,
    evaluate_g3,
    generate_windows,
    run_walkforward,
)


def _load_symbol_data(symbol_file: str) -> SymbolData:
    ohlcv_15m = pd.read_parquet(PROJECT_ROOT / "data" / "ohlcv" / f"{symbol_file}_15m.parquet")
    ohlcv_1h = pd.read_parquet(PROJECT_ROOT / "data" / "ohlcv" / f"{symbol_file}_1h.parquet")
    funding = pd.read_parquet(PROJECT_ROOT / "data" / "funding" / f"{symbol_file}_funding.parquet")

    def _to_dt_index(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df.index = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df.drop(columns=["timestamp"])

    return SymbolData(df_15m=_to_dt_index(ohlcv_15m), df_1h=_to_dt_index(ohlcv_1h), funding=funding)


def _symbol_file(pair: str) -> str:
    return pair.replace("/", "").replace(":", "-")


def main() -> None:
    with open(PROJECT_ROOT / "config" / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    pairs = cfg["exchange"]["pairs"]
    data: dict[str, SymbolData] = {}
    for pair in pairs:
        print(f"[데이터 로드] {pair}...", flush=True)
        data[pair] = _load_symbol_data(_symbol_file(pair))

    end = max(sd.df_15m.index.max() for sd in data.values())
    start = pd.Timestamp(cfg["data"]["since"])
    wf_cfg = cfg["walkforward"]
    windows = generate_windows(start, end, wf_cfg["train_months"], wf_cfg["test_months"])
    grid = wf_cfg["grid"]
    n_combos = len(list(product(grid["donchian_period"], grid["trend_stop_atr_mult"])))
    print(f"[워크포워드] 윈도우 {len(windows)}개, 윈도우당 그리드 {n_combos}조합", flush=True)

    t0 = time.time()
    result = run_walkforward(data, cfg, windows)
    elapsed = time.time() - t0
    print(f"[워크포워드] 완료: OOS 트레이드 {len(result.all_oos_trades)}건, {elapsed:.1f}초", flush=True)

    g2 = evaluate_g2(result, cfg["gates"])
    g3 = evaluate_g3(result, cfg["account"]["initial_equity_usd"], cfg["gates"])
    print(f"[G2] {'PASS' if g2.passed else 'FAIL'} / [G3] {'PASS' if g3.passed else 'FAIL'}", flush=True)

    _write_report(cfg, windows, result, g2, g3, n_combos)
    print("WALKFORWARD_REPORT.md 작성 완료", flush=True)


def _fmt(v, spec="", suffix="") -> str:
    if v is None:
        return "N/A"
    return f"{v:{spec}}{suffix}"


def _write_report(cfg, windows, result, g2, g3, n_combos) -> None:
    lines = [
        "# WALKFORWARD_REPORT.md — Phase 3 워크포워드 검증 (G2·G3)",
        "",
        f"생성 시각: {datetime.now(timezone.utc).isoformat()}",
        f"윈도우: 학습 {cfg['walkforward']['train_months']}개월 / 검증 {cfg['walkforward']['test_months']}개월 롤링, "
        f"총 {len(windows)}개 윈도우",
        f"그리드: donchian_period={cfg['walkforward']['grid']['donchian_period']} × "
        f"stop_atr_mult={cfg['walkforward']['grid']['trend_stop_atr_mult']} = {n_combos}조합 (윈도우당 학습구간에서만 탐색)",
        f"활성 전략: {cfg['active_strategies']} (require_confirmation_bar={cfg['trend']['require_confirmation_bar']}, 그리드 아님·고정값)",
        "",
        "## G2 판정 (OOS 합산 성과)",
        "",
        f"- OOS 거래수: {g2.n_oos_trades} (기준 ≥ {cfg['gates']['g2_min_oos_trades']})",
        f"- Profit Factor: {_fmt(g2.profit_factor, '.2f')} (기준 ≥ {cfg['gates']['g2_min_profit_factor']})",
        f"- MaxDD: {_fmt(g2.max_drawdown_pct, '.2f', '%')} (기준 ≤ {cfg['gates']['g2_max_drawdown_pct']}%)",
        f"- 월간 양수 비율: {_fmt(g2.positive_month_pct, '.1f', '%')} (기준 ≥ {cfg['gates']['g2_min_positive_month_pct']}%)",
        "",
        f"**G2 판정: {'PASS' if g2.passed else 'FAIL'}**",
    ]
    if not g2.passed:
        lines.append("")
        lines.append("미달 사유:")
        for f in g2.failures:
            lines.append(f"- {f}")

    lines += [
        "",
        "## G3 판정 (몬테카를로 — OOS 트레이드 순서 셔플)",
        "",
        f"- 셔플 횟수: {g3.n_runs}",
        f"- MaxDD 95%ile: {_fmt(g3.max_drawdown_p95_pct, '.2f', '%')} (기준 ≤ {cfg['gates']['g3_max_drawdown_p95_pct']}%)",
        "",
        f"**G3 판정: {'PASS' if g3.passed else 'FAIL'}**",
    ]
    if not g3.passed:
        lines.append("")
        lines.append("미달 사유:")
        for f in g3.failures:
            lines.append(f"- {f}")

    lines += ["", "## 윈도우별 분해", "", "| # | 학습구간 | 검증구간 | 선택 파라미터 | IS PF | IS Sharpe | OOS PF | OOS Sharpe | OOS 거래수 |", "|---|---|---|---|---|---|---|---|---|"]
    for wr in result.windows:
        w = wr.window
        lines.append(
            f"| {w.window_id} | {w.train_start.date()}~{w.train_end.date()} | {w.test_start.date()}~{w.test_end.date()} "
            f"| donchian={wr.best_params['donchian_period']}, stop={wr.best_params['stop_atr_mult']} "
            f"| {_fmt(wr.is_pf, '.2f')} | {_fmt(wr.is_sharpe, '.2f')} | {_fmt(wr.oos_pf, '.2f')} | {_fmt(wr.oos_sharpe, '.2f')} | {len(wr.oos_trades)} |"
        )

    from src.backtest.report import compute_profit_factor, compute_win_rate_pct

    lines += ["", "## 페어별 분해 (OOS 합산)", "", "| 페어 | 거래수 | 승률 | PF | 손익 |", "|---|---|---|---|---|"]
    for pair in cfg["exchange"]["pairs"]:
        st = [t for t in result.all_oos_trades if t.symbol == pair]
        if not st:
            lines.append(f"| {pair} | 0 | N/A | N/A | $0.00 |")
            continue
        pf = compute_profit_factor(st)
        lines.append(f"| {pair} | {len(st)} | {_fmt(compute_win_rate_pct(st), '.1f', '%')} | {_fmt(pf, '.2f')} | ${sum(t.pnl_usd for t in st):,.2f} |")

    lines += ["", "## 레짐별 분해 (OOS 합산)", "", "| 전략(=레짐) | 거래수 | PF | 손익 |", "|---|---|---|---|"]
    for strat in ("trend", "meanrev"):
        st = [t for t in result.all_oos_trades if t.strategy == strat]
        if not st:
            lines.append(f"| {strat} | 0 | N/A | $0.00 |")
            continue
        pf = compute_profit_factor(st)
        lines.append(f"| {strat} | {len(st)} | {_fmt(pf, '.2f')} | ${sum(t.pnl_usd for t in st):,.2f} |")

    lines += [
        "",
        "이 결과는 있는 그대로 기록한다 — G2/G3 미달이어도 그리드를 다시 돌리거나",
        "파라미터를 손으로 조정하지 않는다 (CLAUDE.md, SPEC 4절: 미달 시 전략",
        "기각 후 재설계, 파라미터 재탐색 금지).",
    ]
    (PROJECT_ROOT / "WALKFORWARD_REPORT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
