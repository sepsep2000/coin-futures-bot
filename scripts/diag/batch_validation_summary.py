"""BATCH_VALIDATION_SUMMARY.md 컴파일 — 지금까지 검증된 모든 신호 후보의
누적 상관관계 매트릭스 + 판정 결과 표 하나로 정리.

3a/3b(이전 세션, checks/signal_validation.py 하네스 이전에 별도 스크립트로
검증됨)와 1a/2a/4a/4b(이번 배치, 하네스로 검증)를 모두 포함한다. 3a/3b는
당시 검증 결과(reports/SIGNAL_VALIDATION_3a3b.md)를 그대로 인용하고, 이번
매트릭스 계산을 위해 주간 수익률 시계열만 다시 만든다(원본 trades CSV +
ETH bar_index 매핑, 재시뮬레이션 아님).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import checks.signal_validation as cv
from scripts.diag.common import DIAG_DATA_DIR, eth_data, get_trend_trades, trades_to_df

PROJECT_ROOT = DIAG_DATA_DIR.parent.parent
SIGNAL_DATA_DIR = PROJECT_ROOT / "reports" / "signal_validation_data"


def _trend_weekly() -> pd.Series:
    trades = get_trend_trades(use_grace=True)
    df = trades_to_df(trades)
    return cv.weekly_return_series(df["exit_time"], df["r_multiple"].to_numpy())


def _legacy_weekly(sig_id: str) -> pd.Series:
    """3a/3b — entry_idx/exit_idx만 저장돼 있어 ETH bar_index로 타임스탬프 복원."""
    df = pd.read_csv(DIAG_DATA_DIR / f"signalval_{sig_id}_trades.csv")
    bar_index = eth_data()["ETH/USDT:USDT"].df_15m.index
    exit_time = bar_index[df["exit_idx"].to_numpy()]
    return cv.weekly_return_series(exit_time, df["return"].to_numpy())


def _harness_weekly(sig_id: str) -> pd.Series | None:
    path = SIGNAL_DATA_DIR / sig_id / f"{sig_id}_weekly_returns.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=["exit_time"], index_col="exit_time")
    return df["weekly_return"]


LEGACY_RESULTS = {
    "3a": {"category": "meanrev", "n_events": 2459, "point_mean_pct": -0.0492, "verdict": "FAIL_SIGNIFICANT_NEGATIVE"},
    "3b": {"category": "meanrev", "n_events": 410, "point_mean_pct": -0.0430, "verdict": "FAIL_NOT_SIGNIFICANT"},
}


def _harness_result_row(sig_id: str, spec_meta: dict) -> dict:
    import yaml
    spec = yaml.safe_load((PROJECT_ROOT / "signal_specs" / f"{sig_id}.yaml").read_text(encoding="utf-8"))
    step_c_path = SIGNAL_DATA_DIR / sig_id / f"{sig_id}_stepC_bootstrap.csv"
    if not step_c_path.exists():
        return {"id": sig_id, "category": spec.get("category"), "n_events": 0, "point_mean": None, "verdict": "N/A(트레이드 없음)"}
    step_c = pd.read_csv(step_c_path, index_col=0).iloc[:, 0]
    n_events = int(step_c["n_events"])
    point_mean = float(step_c["point_mean_return_pct"])
    is_r = spec.get("return_units") == "r_multiple"
    unit = "R" if is_r else "%"
    point_mean_disp = point_mean / 100 if is_r else point_mean
    return {
        "id": sig_id, "category": spec.get("category"), "n_events": n_events,
        "point_mean": f"{point_mean_disp:.4f}{unit}", "verdict": None,  # filled from md 파일
    }


def run() -> None:
    series = {
        "trend": _trend_weekly(),
        "3a": _legacy_weekly("3a"),
        "3b": _legacy_weekly("3b"),
    }
    for sig_id in ["1a", "2a", "4a", "4b"]:
        s = _harness_weekly(sig_id)
        if s is not None:
            series[sig_id] = s

    corr_matrix = cv.correlation_matrix(series)
    corr_matrix.to_csv(DIAG_DATA_DIR / "cumulative_signal_correlation_matrix.csv")

    # 이번 배치 4개의 실제 판정 문자열은 각 SIGNAL_VALIDATION_{id}.md에서 파싱
    verdicts = {}
    for sig_id in ["1a", "2a", "4a", "4b"]:
        md_path = PROJECT_ROOT / "reports" / f"SIGNAL_VALIDATION_{sig_id}.md"
        text = md_path.read_text(encoding="utf-8")
        line = [ln for ln in text.splitlines() if ln.startswith("## 최종 판정")][0]
        verdicts[sig_id] = line.split("**")[1]

    rows = []
    for sig_id, meta in LEGACY_RESULTS.items():
        rows.append({"id": sig_id, "category": meta["category"], "n_events": meta["n_events"],
                      "point_mean": f"{meta['point_mean_pct']:.4f}%", "verdict": meta["verdict"], "batch": "이전 세션"})
    for sig_id in ["1a", "2a", "4a", "4b"]:
        row = _harness_result_row(sig_id, {})
        row["verdict"] = verdicts.get(sig_id, row["verdict"])
        row["batch"] = "이번 배치"
        rows.append(row)

    summary_df = pd.DataFrame(rows)
    summary_df.to_csv(DIAG_DATA_DIR / "batch_validation_summary_table.csv", index=False)

    print(corr_matrix.round(3))
    print(summary_df)


if __name__ == "__main__":
    run()
