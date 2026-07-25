"""STEP 2 — "trend + 1a필터" vs 2a 상관관계 + 활성구간 중첩률.

포트폴리오 구성 전 필수 확인: 1a를 trend의 필터로 채택했는데(FILTER_
VALIDATION_1a.md), 필터링으로 trend의 활성구간이 좁아지면서 2a와
우연히 겹치게 된 건 아닌지 확인한다. 상관계수는 ENGINE_MULTI_CORRELATION_
UPDATE.md(STEP 1, 엔진의 다중상관 체크)에서 이미 계산된 값과 교차검증
겸 재확인한다.

★ 활성구간 중첩 계산이 기존 STEP A(check_exclusivity)와 다른 방식인 이유:
STEP A는 동일 15m bar_index를 공유하는 두 신호에만 쓸 수 있다(bar-index
마스크 비교). trend_filtered_1a(ETH 15m)와 2a(20자산 주간 리밸런스)는
타임프레임 자체가 다르므로, 여기서는 실제 타임스탬프 구간의 합집합/교집합을
직접 계산한다(신규 함수 — checks 엔진에 넣지 않음, 이 비교에만 쓰는
일회성 방법론이라 별도 스크립트에 둔다).
"""

from __future__ import annotations

import pandas as pd

import checks.signal_validation as cv
from scripts.diag.common import DIAG_DATA_DIR

PROJECT_ROOT = DIAG_DATA_DIR.parent.parent
SIGNAL_DATA_DIR = PROJECT_ROOT / "reports" / "signal_validation_data"


def _merge_intervals(intervals: list[tuple[pd.Timestamp, pd.Timestamp]]) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    if not intervals:
        return []
    intervals = sorted(intervals, key=lambda x: x[0])
    merged = [intervals[0]]
    for s, e in intervals[1:]:
        last_s, last_e = merged[-1]
        if s <= last_e:
            merged[-1] = (last_s, max(last_e, e))
        else:
            merged.append((s, e))
    return merged


def _total_duration(intervals: list[tuple[pd.Timestamp, pd.Timestamp]]) -> pd.Timedelta:
    return sum((e - s for s, e in intervals), pd.Timedelta(0))


def _intersection_duration(a: list[tuple[pd.Timestamp, pd.Timestamp]], b: list[tuple[pd.Timestamp, pd.Timestamp]]) -> pd.Timedelta:
    total = pd.Timedelta(0)
    i = j = 0
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if s < e:
            total += (e - s)
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return total


def run() -> dict:
    df_a = pd.read_csv(SIGNAL_DATA_DIR / "1a" / "1a_trades.csv", parse_dates=["entry_time", "exit_time"])
    df_b = pd.read_csv(SIGNAL_DATA_DIR / "2a" / "2a_trades.csv", parse_dates=["entry_time", "exit_time"])

    windows_a = _merge_intervals(list(zip(df_a["entry_time"], df_a["exit_time"])))
    windows_b = _merge_intervals(list(zip(df_b["entry_time"], df_b["exit_time"])))

    total_a = _total_duration(windows_a)
    total_b = _total_duration(windows_b)
    overlap = _intersection_duration(windows_a, windows_b)

    overlap_pct_of_a = overlap / total_a * 100 if total_a.total_seconds() > 0 else float("nan")
    overlap_pct_of_b = overlap / total_b * 100 if total_b.total_seconds() > 0 else float("nan")

    weekly_a = pd.read_csv(SIGNAL_DATA_DIR / "1a" / "1a_weekly_returns.csv", index_col=0, parse_dates=True)["weekly_return"]
    weekly_b = pd.read_csv(SIGNAL_DATA_DIR / "2a" / "2a_weekly_returns.csv", index_col=0, parse_dates=True)["weekly_return"]
    corr_result = cv.series_correlation(weekly_a, weekly_b)

    # 참고: 필터링 전(원본) trend 활성구간 대비 비교 — 필터링이 활성구간을 얼마나 좁혔는지
    from scripts.diag.common import get_trend_trades, trades_to_df
    trend_df = trades_to_df(get_trend_trades(use_grace=True))
    windows_trend_full = _merge_intervals(list(zip(trend_df["entry_time"], trend_df["exit_time"])))
    total_trend_full = _total_duration(windows_trend_full)
    overlap_full_with_b = _intersection_duration(windows_trend_full, windows_b)
    overlap_pct_full_of_a = overlap_full_with_b / total_trend_full * 100 if total_trend_full.total_seconds() > 0 else float("nan")

    summary = {
        "trend_filtered_1a_total_active_days": total_a.total_seconds() / 86400,
        "2a_total_active_days": total_b.total_seconds() / 86400,
        "overlap_days": overlap.total_seconds() / 86400,
        "overlap_pct_of_trend_filtered_1a": overlap_pct_of_a,
        "overlap_pct_of_2a": overlap_pct_of_b,
        "weekly_return_correlation": corr_result["weekly_return_correlation"],
        "n_weeks_compared": corr_result["n_weeks_compared"],
        "is_independent": corr_result["is_independent"],
        "reference_unfiltered_trend_total_active_days": total_trend_full.total_seconds() / 86400,
        "reference_unfiltered_trend_overlap_pct_with_2a": overlap_pct_full_of_a,
    }
    pd.Series(summary).to_csv(DIAG_DATA_DIR / "filtered_trend_2a_correlation_summary.csv")
    return summary


if __name__ == "__main__":
    print(run())
