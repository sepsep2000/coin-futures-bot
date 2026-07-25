"""BATCH_VALIDATION_SUMMARY_V2.md 컴파일 — 1세대(3a/3b/1a/2a/4a/4b) +
2세대(5a/5b) 전체 누적 상관관계 매트릭스 + 판정 결과 표.

batch_validation_summary.py(1세대용)와 로직은 같지만 5a/5b를 추가하고
1a 필터 재현성 검정 결과도 표에 포함한다. 기존 스크립트는 수정하지
않는다(1세대 실행 결과 재현용으로 그대로 보존).
"""

from __future__ import annotations

import pandas as pd
import yaml

import checks.signal_validation as cv
from scripts.diag.batch_validation_summary import LEGACY_RESULTS, _harness_weekly, _legacy_weekly, _trend_weekly
from scripts.diag.common import DIAG_DATA_DIR

PROJECT_ROOT = DIAG_DATA_DIR.parent.parent
SIGNAL_DATA_DIR = PROJECT_ROOT / "reports" / "signal_validation_data"

HARNESS_IDS_V1 = ["1a", "2a", "4a", "4b"]
HARNESS_IDS_V2 = ["5a", "5b"]


def _harness_result_row(sig_id: str) -> dict:
    spec = yaml.safe_load((PROJECT_ROOT / "signal_specs" / f"{sig_id}.yaml").read_text(encoding="utf-8"))
    step_c_path = SIGNAL_DATA_DIR / sig_id / f"{sig_id}_stepC_bootstrap.csv"
    step_c = pd.read_csv(step_c_path, index_col=0).iloc[:, 0]
    n_events = int(step_c["n_events"])
    point_mean = float(step_c["point_mean_return_pct"])
    is_r = spec.get("return_units") == "r_multiple"
    unit = "R" if is_r else "%"
    point_mean_disp = point_mean / 100 if is_r else point_mean
    md_text = (PROJECT_ROOT / "reports" / f"SIGNAL_VALIDATION_{sig_id}.md").read_text(encoding="utf-8")
    verdict = [ln for ln in md_text.splitlines() if ln.startswith("## 최종 판정")][0].split("**")[1]
    return {
        "id": sig_id, "category": spec.get("category"), "n_events": n_events,
        "point_mean": f"{point_mean_disp:.4f}{unit}", "verdict": verdict,
    }


def run() -> None:
    series = {"trend": _trend_weekly(), "3a": _legacy_weekly("3a"), "3b": _legacy_weekly("3b")}
    for sig_id in HARNESS_IDS_V1 + HARNESS_IDS_V2:
        s = _harness_weekly(sig_id)
        if s is not None:
            series[sig_id] = s

    corr_matrix = cv.correlation_matrix(series)
    corr_matrix.to_csv(DIAG_DATA_DIR / "cumulative_signal_correlation_matrix_v2.csv")

    rows = []
    for sig_id, meta in LEGACY_RESULTS.items():
        rows.append({"id": sig_id, "category": meta["category"], "n_events": meta["n_events"],
                      "point_mean": f"{meta['point_mean_pct']:.4f}%", "verdict": meta["verdict"], "batch": "1세대"})
    for sig_id in HARNESS_IDS_V1:
        row = _harness_result_row(sig_id)
        row["batch"] = "1세대"
        rows.append(row)
    for sig_id in HARNESS_IDS_V2:
        row = _harness_result_row(sig_id)
        row["batch"] = "2세대"
        rows.append(row)

    summary_df = pd.DataFrame(rows)
    summary_df.to_csv(DIAG_DATA_DIR / "batch_validation_summary_table_v2.csv", index=False)

    print(corr_matrix.round(3))
    print(summary_df)


if __name__ == "__main__":
    run()
