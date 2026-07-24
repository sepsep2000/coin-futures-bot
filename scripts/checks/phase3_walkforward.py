"""
Phase 3 (워크포워드 검증) 전용 체크.
결과 파일 스키마만 맞으면 바로 동작 (results_path에 JSON: [{"window": 1, "is_sharpe": .., "oos_sharpe": ..}, ...]).
"""
import json
import os

MAX_OOS_DEGRADATION_PCT = 0.40  # OOS가 IS 대비 40% 넘게 나빠지면 과최적화 의심


def check_is_oos_degradation(results_path: str, max_degradation_pct: float = MAX_OOS_DEGRADATION_PCT):
    failures = []
    if not os.path.exists(results_path):
        return [f"결과 파일 없음: {results_path} (Phase 3 아직 미완료면 정상)"]

    with open(results_path, "r", encoding="utf-8") as f:
        windows = json.load(f)

    for w in windows:
        is_val = w.get("is_sharpe")
        oos_val = w.get("oos_sharpe")
        if is_val is None or oos_val is None:
            continue
        if is_val <= 0:
            continue  # IS 자체가 마이너스면 저하율 계산 의미 없음, 별도 체크 필요
        degradation = (is_val - oos_val) / is_val
        if degradation > max_degradation_pct:
            failures.append(
                f"window {w.get('window')}: IS sharpe {is_val:.2f} -> OOS {oos_val:.2f} "
                f"({degradation:.0%} 저하, 임계 {max_degradation_pct:.0%})"
            )
    return failures


def check_window_consistency(results_path: str, min_windows: int = 5):
    """워크포워드 윈도우 수가 너무 적으면 통계적으로 신뢰 불가."""
    if not os.path.exists(results_path):
        return []
    with open(results_path, "r", encoding="utf-8") as f:
        windows = json.load(f)
    if len(windows) < min_windows:
        return [f"워크포워드 윈도우 {len(windows)}개 (최소 {min_windows}개 권장) — 표본 부족"]
    return []


def run_all(config: dict):
    """
    config 예:
    {
        "results_path": "results/walkforward.json",
        "max_degradation_pct": 0.40,
        "min_windows": 5,
    }
    """
    failures = []
    results_path = config.get("results_path", "results/walkforward.json")
    failures += check_is_oos_degradation(results_path, config.get("max_degradation_pct", MAX_OOS_DEGRADATION_PCT))
    failures += check_window_consistency(results_path, config.get("min_windows", 5))
    return [f"[phase3] {x}" for x in failures]
