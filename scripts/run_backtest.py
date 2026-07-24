"""scripts/run_backtest.py — Phase 2: 2023-01~현재 전체 기간 백테스트 1회 실행.

SPEC.md 3절 아키텍처의 최종 산출물 진입점. G1 무결성(룩어헤드 방지·수수료
민감도)은 tests/test_engine_integration.py의 자동 테스트로 별도 검증되고,
이 스크립트는 그 결과를 요약하고 실제 전체 구간 성과를 BACKTEST_BASELINE.md로
남긴다. 결과가 나빠도 파라미터를 조정하지 않고 있는 그대로 보고한다
(CLAUDE.md 금지사항 1항).

사용:
    python scripts/run_backtest.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.backtest.engine import BacktestResult, SymbolData, run_backtest  # noqa: E402
from src.backtest.report import compute_metrics, compute_monthly_returns_pct, render_report_html  # noqa: E402


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


def _run_g1_pytest() -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_engine_integration.py", "-v"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    return result.returncode == 0, (result.stdout + result.stderr)[-4000:]


def main() -> None:
    with open(PROJECT_ROOT / "config" / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    print("[G1] 룩어헤드/수수료민감도 자동 테스트 실행...", flush=True)
    g1_pass, g1_log = _run_g1_pytest()
    print(f"[G1] {'PASS' if g1_pass else 'FAIL'}", flush=True)

    pairs = cfg["exchange"]["pairs"]
    data: dict[str, SymbolData] = {}
    for pair in pairs:
        print(f"[데이터 로드] {pair}...", flush=True)
        data[pair] = _load_symbol_data(_symbol_file(pair))

    print("[백테스트] 실비용 전체 구간 실행...", flush=True)
    t0 = time.time()
    real_result = run_backtest(data, cfg)
    real_elapsed = time.time() - t0
    print(f"  -> {len(real_result.trades)}건 거래, {real_elapsed:.1f}초", flush=True)

    print("[백테스트] 0비용(수수료·슬리피지 0) 비교 실행...", flush=True)
    zero_cost_cfg = {**cfg, "costs": {**cfg["costs"], "taker_fee_pct": 0, "maker_fee_pct": 0, "slippage_pct": 0}}
    t0 = time.time()
    zero_result = run_backtest(data, zero_cost_cfg)
    zero_elapsed = time.time() - t0
    print(f"  -> {len(zero_result.trades)}건 거래, {zero_elapsed:.1f}초", flush=True)

    real_metrics = compute_metrics(real_result)
    zero_metrics = compute_metrics(zero_result)
    monthly = compute_monthly_returns_pct(real_result.equity_curve)

    reports_dir = PROJECT_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    html = render_report_html(real_result, real_metrics, monthly, title="RSIB Backtest — 실비용")
    (reports_dir / "report.html").write_text(html, encoding="utf-8")

    per_symbol_trades: dict[str, list] = {p: [] for p in pairs}
    for t in real_result.trades:
        per_symbol_trades[t.symbol].append(t)

    per_symbol_rows = []
    for pair in pairs:
        trades = per_symbol_trades[pair]
        from src.backtest.report import compute_profit_factor, compute_win_rate_pct
        per_symbol_rows.append(
            f"| {pair} | {len(trades)} | {compute_win_rate_pct(trades) or float('nan'):.1f}% "
            f"| {compute_profit_factor(trades) if compute_profit_factor(trades) is not None else float('nan'):.2f} "
            f"| ${sum(t.pnl_usd for t in trades):,.2f} |"
        )

    lines = [
        "# BACKTEST_BASELINE.md — Phase 2 백테스트 기준선 (G1)",
        "",
        f"생성 시각: {datetime.now(timezone.utc).isoformat()}",
        f"구간: {cfg['data']['since']} ~ 현재 (live 데이터, DATA_REPORT.md 참조)",
        f"페어: {', '.join(pairs)}",
        "",
        "## G1 무결성 검증",
        "",
        f"- 룩어헤드 방지 테스트: {'PASS' if g1_pass else 'FAIL'} (`tests/test_engine_integration.py`)",
        f"- 수수료 0 vs 실비용: 0비용 총손익 ${sum(t.pnl_usd for t in zero_result.trades):,.2f} "
        f"vs 실비용 총손익 ${sum(t.pnl_usd for t in real_result.trades):,.2f} "
        f"(비용 총액 ${sum(t.pnl_usd for t in zero_result.trades) - sum(t.pnl_usd for t in real_result.trades):,.2f})",
        "",
        "**G1 판정: " + ("PASS" if g1_pass else "FAIL — 아래 로그 확인") + "**",
        "" if g1_pass else f"```\n{g1_log}\n```",
        "",
        "## 전체 성과 (실비용, 3페어 합산)",
        "",
        f"| 지표 | 값 |",
        f"|---|---|",
        f"| 거래수 | {real_metrics.n_trades} |",
        f"| 승률 | {real_metrics.win_rate_pct:.1f}% |" if real_metrics.win_rate_pct is not None else "| 승률 | N/A |",
        f"| Profit Factor | {real_metrics.profit_factor:.2f} |" if real_metrics.profit_factor not in (None,) else "| Profit Factor | N/A |",
        f"| Sharpe(연환산) | {real_metrics.sharpe_annualized:.2f} |" if real_metrics.sharpe_annualized is not None else "| Sharpe(연환산) | N/A |",
        f"| MaxDD | {real_metrics.max_drawdown_pct:.2f}% |" if real_metrics.max_drawdown_pct is not None else "| MaxDD | N/A |",
        f"| 평균 R | {real_metrics.avg_r_multiple:.2f} |" if real_metrics.avg_r_multiple is not None else "| 평균 R | N/A |",
        f"| 중앙값 R | {real_metrics.median_r_multiple:.2f} |" if real_metrics.median_r_multiple is not None else "| 중앙값 R | N/A |",
        f"| 총손익 | ${real_metrics.total_pnl_usd:,.2f} |",
        f"| 최종 자본 | ${real_result.final_equity:,.2f} (초기 ${cfg['account']['initial_equity_usd']:,.2f}) |",
        "",
        "## 페어별 분해",
        "",
        "| 페어 | 거래수 | 승률 | PF | 손익 |",
        "|---|---|---|---|---|",
        *per_symbol_rows,
        "",
        "## 자본 부족으로 스킵된 진입",
        "",
        f"- {len(real_result.skipped_entries)}건",
        "",
        "이 결과는 있는 그대로 기록한다 — 나쁜 결과라고 파라미터를 조정하지 않는다 (CLAUDE.md).",
        "",
        "상세 리포트: `reports/report.html`",
    ]
    (PROJECT_ROOT / "BACKTEST_BASELINE.md").write_text("\n".join(lines), encoding="utf-8")
    print("BACKTEST_BASELINE.md 작성 완료", flush=True)


if __name__ == "__main__":
    main()
