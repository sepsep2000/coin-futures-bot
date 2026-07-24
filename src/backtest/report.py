"""src/backtest/report.py — SPEC 3절 백테스트 성과 리포트 (Phase 2).

계산(순수함수)과 HTML 렌더링을 분리한다. 외부 플로팅 라이브러리 없이(SPEC
Phase 0 requirements.txt 고정) equity curve는 인라인 SVG로 그린다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from src.backtest.engine import BacktestResult, Trade

BARS_PER_YEAR_15M = 365 * 24 * 4  # 35,040 — 15m 봉 기준 연간 봉 수(연환산 Sharpe용)


@dataclass
class PerformanceMetrics:
    n_trades: int
    win_rate_pct: Optional[float]
    profit_factor: Optional[float]
    sharpe_annualized: Optional[float]
    max_drawdown_pct: Optional[float]
    avg_r_multiple: Optional[float]
    median_r_multiple: Optional[float]
    total_pnl_usd: float


def compute_profit_factor(trades: list[Trade]) -> Optional[float]:
    """총이익 / 총손실(절대값). 손실이 0이면 이익도 0인 경우 None(거래 없음과 동일 취급),
    이익만 있으면 inf."""
    gains = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    losses = sum(-t.pnl_usd for t in trades if t.pnl_usd < 0)
    if losses == 0:
        return math.inf if gains > 0 else None
    return gains / losses


def compute_win_rate_pct(trades: list[Trade]) -> Optional[float]:
    if not trades:
        return None
    wins = sum(1 for t in trades if t.pnl_usd > 0)
    return wins / len(trades) * 100


def compute_avg_r_multiple(trades: list[Trade]) -> Optional[float]:
    if not trades:
        return None
    return sum(t.r_multiple for t in trades) / len(trades)


def compute_median_r_multiple(trades: list[Trade]) -> Optional[float]:
    """평균만 보면 소수의 큰 승리가 평균을 왜곡할 수 있어 중앙값을 함께 낸다."""
    if not trades:
        return None
    return float(pd.Series([t.r_multiple for t in trades]).median())


def compute_max_drawdown_pct(equity_curve: list[tuple]) -> Optional[float]:
    if not equity_curve:
        return None
    peak = equity_curve[0][1]
    max_dd = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    return max_dd


def compute_sharpe_annualized(equity_curve: list[tuple], bars_per_year: int = BARS_PER_YEAR_15M) -> Optional[float]:
    """봉 단위 수익률의 평균/표준편차로 Sharpe를 구한 뒤 sqrt(bars_per_year)로 연환산
    (무위험수익률 0 가정)."""
    if len(equity_curve) < 2:
        return None
    equities = pd.Series([e for _, e in equity_curve])
    returns = equities.pct_change().dropna()
    std = returns.std(ddof=0)
    if returns.empty or std == 0:
        return None
    sharpe = returns.mean() / std
    return float(sharpe * math.sqrt(bars_per_year))


def compute_monthly_returns_pct(equity_curve: list[tuple]) -> pd.Series:
    """월말 기준 equity curve의 월별 수익률(%). 첫 달은 구간 시작 equity 대비 계산."""
    if not equity_curve:
        return pd.Series(dtype=float)
    s = pd.Series({ts: eq for ts, eq in equity_curve}).sort_index()
    monthly_last = s.resample("ME").last()
    monthly_ret = monthly_last.pct_change() * 100
    monthly_ret.iloc[0] = (monthly_last.iloc[0] / s.iloc[0] - 1) * 100
    return monthly_ret


def compute_metrics(result: BacktestResult, bars_per_year: int = BARS_PER_YEAR_15M) -> PerformanceMetrics:
    return PerformanceMetrics(
        n_trades=len(result.trades),
        win_rate_pct=compute_win_rate_pct(result.trades),
        profit_factor=compute_profit_factor(result.trades),
        sharpe_annualized=compute_sharpe_annualized(result.equity_curve, bars_per_year),
        max_drawdown_pct=compute_max_drawdown_pct(result.equity_curve),
        avg_r_multiple=compute_avg_r_multiple(result.trades),
        median_r_multiple=compute_median_r_multiple(result.trades),
        total_pnl_usd=sum(t.pnl_usd for t in result.trades),
    )


def _equity_curve_svg(equity_curve: list[tuple], width: int = 760, height: int = 220) -> str:
    if len(equity_curve) < 2:
        return "<p>equity curve 데이터 부족</p>"
    values = [e for _, e in equity_curve]
    lo, hi = min(values), max(values)
    span = hi - lo if hi != lo else 1.0
    n = len(values)
    points = " ".join(
        f"{i / (n - 1) * width:.1f},{height - (v - lo) / span * height:.1f}"
        for i, v in enumerate(values)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'style="background:#111;border:1px solid #333">'
        f'<polyline points="{points}" fill="none" stroke="#4ade80" stroke-width="1.5" />'
        f"</svg>"
    )


def _fmt(v, spec="", suffix="") -> str:
    if v is None:
        return "N/A"
    if v == math.inf:
        return "∞"
    return f"{v:{spec}}{suffix}"


def render_report_html(result: BacktestResult, metrics: PerformanceMetrics, monthly_returns_pct: pd.Series, title: str = "Backtest Report") -> str:
    monthly_rows = "".join(
        f"<tr><td>{idx.strftime('%Y-%m')}</td><td>{v:.2f}%</td></tr>"
        for idx, v in monthly_returns_pct.items()
    ) or "<tr><td colspan='2'>데이터 없음</td></tr>"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: monospace; background:#0b0b0b; color:#eee; padding: 24px; }}
table {{ border-collapse: collapse; margin-bottom: 24px; }}
td, th {{ border: 1px solid #333; padding: 6px 12px; text-align: right; }}
th {{ text-align: left; }}
</style></head>
<body>
<h1>{title}</h1>
<table>
<tr><th>거래수</th><td>{metrics.n_trades}</td></tr>
<tr><th>승률</th><td>{_fmt(metrics.win_rate_pct, '.1f', '%')}</td></tr>
<tr><th>Profit Factor</th><td>{_fmt(metrics.profit_factor, '.2f')}</td></tr>
<tr><th>Sharpe(연환산)</th><td>{_fmt(metrics.sharpe_annualized, '.2f')}</td></tr>
<tr><th>MaxDD</th><td>{_fmt(metrics.max_drawdown_pct, '.2f', '%')}</td></tr>
<tr><th>평균 R</th><td>{_fmt(metrics.avg_r_multiple, '.2f')}</td></tr>
<tr><th>중앙값 R</th><td>{_fmt(metrics.median_r_multiple, '.2f')}</td></tr>
<tr><th>총손익</th><td>${metrics.total_pnl_usd:,.2f}</td></tr>
<tr><th>최종 자본</th><td>${result.final_equity:,.2f}</td></tr>
</table>
<h2>Equity Curve</h2>
{_equity_curve_svg(result.equity_curve)}
<h2>월별 수익률</h2>
<table><tr><th>월</th><th>수익률</th></tr>{monthly_rows}</table>
</body></html>"""
