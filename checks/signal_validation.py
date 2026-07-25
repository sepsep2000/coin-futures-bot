"""checks/signal_validation.py — 신호 검증 공용 통계 엔진.

D7b(펀딩 극단치 후속검증)와 3a/3b(평균회귀 후보검증)에서 반복됐던 로직을
재사용 가능한 함수로 승격했다. scripts/validate_signal.py가 이 모듈의
함수만 호출해 STEP A~D + 넷중립 체크 + 최종 판정을 수행한다.

★ 이 모듈의 임계값(CLUSTER_GAP_HOURS, N_BOOT, OVERLAP_THRESHOLD_PCT,
CORR_THRESHOLD, TAIL_PCT, ALPHA)은 신호별로 조정하지 않는다 — 신호가
통과하도록 엔진을 맞추는 것은 그리드서치와 같은 성격의 위반이다. signal_specs
yaml에서 이 값들을 오버라이드할 수 있게 만들지 않은 것도 같은 이유다(엔진
호출부에서 이 모듈의 상수를 그대로 쓴다).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yaml

CLUSTER_GAP_HOURS = 24
N_BOOT = 5000
OVERLAP_THRESHOLD_PCT = 20.0
CORR_THRESHOLD = 0.3
TAIL_PCT = 5.0
ALPHA = 0.05  # 95% CI


# ---------------------------------------------------------------------------
# STEP A — 레짐/독립성 배타성
# ---------------------------------------------------------------------------

def bars_covered(windows: list[tuple[int, int]], n_bars: int) -> np.ndarray:
    mask = np.zeros(n_bars, dtype=bool)
    for s, e in windows:
        mask[s:e + 1] = True
    return mask


def check_exclusivity(
    cand_windows: list[tuple[int, int]], ref_windows: list[tuple[int, int]],
    n_bars: int, threshold_pct: float = OVERLAP_THRESHOLD_PCT,
) -> dict:
    cand_mask = bars_covered(cand_windows, n_bars)
    ref_mask = bars_covered(ref_windows, n_bars)
    overlap_bars = int((cand_mask & ref_mask).sum())
    cand_bars = int(cand_mask.sum())
    overlap_pct = overlap_bars / cand_bars * 100 if cand_bars else float("nan")
    return {
        "cand_active_bars": cand_bars, "overlap_bars": overlap_bars,
        "overlap_pct": overlap_pct, "exclusivity_confirmed": overlap_pct < threshold_pct,
    }


# ---------------------------------------------------------------------------
# STEP B — 이벤트 클러스터링(독립성)
# ---------------------------------------------------------------------------

def cluster_events(event_ts: pd.DatetimeIndex, gap_hours: float = CLUSTER_GAP_HOURS) -> np.ndarray:
    """event_ts: 이벤트 발생 타임스탬프(정렬 안 돼 있어도 됨). 반환: 원본 순서대로의
    cluster_id 배열 — 정렬 후 직전 이벤트와 gap_hours 이내면 같은 클러스터."""
    ts = pd.DatetimeIndex(event_ts)
    order = np.argsort(ts.to_numpy())
    ts_sorted = ts.to_numpy()[order]
    cid = np.zeros(len(ts_sorted), dtype=int)
    c = 0
    for i in range(1, len(ts_sorted)):
        gap = (ts_sorted[i] - ts_sorted[i - 1]) / np.timedelta64(1, "h")
        if gap > gap_hours:
            c += 1
        cid[i] = c
    out = np.empty(len(ts_sorted), dtype=int)
    out[order] = cid
    return out


def clustering_summary(cluster_ids: np.ndarray) -> dict:
    sizes = pd.Series(cluster_ids).value_counts()
    n_events = len(cluster_ids)
    return {
        "n_events_raw": n_events, "n_independent_clusters": int(sizes.shape[0]),
        "cluster_size_mean": float(sizes.mean()), "cluster_size_max": int(sizes.max()),
        "pct_events_in_clusters_ge2": float(sizes[sizes >= 2].sum() / n_events * 100) if n_events else float("nan"),
    }


# ---------------------------------------------------------------------------
# STEP C — 클러스터 단위 블록부트스트랩 + 꼬리손실
# ---------------------------------------------------------------------------

def _profit_factor(returns: np.ndarray) -> float:
    gains = returns[returns > 0].sum()
    losses = -returns[returns < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else float("nan")
    return float(gains / losses)


def cluster_bootstrap(returns: np.ndarray, cluster_ids: np.ndarray, n_boot: int = N_BOOT, seed: int = 42) -> dict:
    df = pd.DataFrame({"return": returns, "cluster_id": cluster_ids})
    clusters = df.groupby("cluster_id")["return"].apply(lambda s: s.to_numpy()).to_list()
    n_clusters = len(clusters)
    rng = np.random.default_rng(seed)
    boot_mean = np.empty(n_boot)
    boot_pf = np.empty(n_boot)
    for b in range(n_boot):
        chosen = rng.integers(0, n_clusters, size=n_clusters)
        pooled = np.concatenate([clusters[c] for c in chosen])
        boot_mean[b] = pooled.mean()
        boot_pf[b] = _profit_factor(pooled)
    finite_pf = boot_pf[np.isfinite(boot_pf)]
    lo_pct, hi_pct = ALPHA / 2 * 100, (1 - ALPHA / 2) * 100
    mean_lo, mean_hi = np.percentile(boot_mean, [lo_pct, hi_pct])
    pf_lo, pf_hi = (np.percentile(finite_pf, [lo_pct, hi_pct]) if len(finite_pf) else (float("nan"), float("nan")))
    return {
        "n_clusters": n_clusters, "n_events": len(returns),
        "point_mean_return_pct": float(returns.mean() * 100),
        "mean_ci_lo_pct": float(mean_lo * 100), "mean_ci_hi_pct": float(mean_hi * 100),
        "mean_excludes_0": bool(mean_lo > 0 or mean_hi < 0),
        "point_pf": _profit_factor(returns),
        "pf_ci_lo": float(pf_lo), "pf_ci_hi": float(pf_hi),
        "pf_excludes_1": bool(len(finite_pf) and (pf_lo > 1.0 or pf_hi < 1.0)),
    }


def tail_loss_analysis(returns: np.ndarray, tail_pct: float = TAIL_PCT) -> dict:
    """수익률 하위 tail_pct%의 트레이드가 전체 손익에 얼마나 기여하는지(3a에서
    드러난 '소수 트레이드가 대형 꼬리손실로 전체를 깎아먹는' 패턴 자동 탐지)."""
    n = len(returns)
    n_tail = max(1, int(np.ceil(n * tail_pct / 100)))
    sorted_returns = np.sort(returns)
    tail = sorted_returns[:n_tail]
    rest = sorted_returns[n_tail:]
    total_negative = -returns[returns < 0].sum()
    tail_negative_share = float(-tail[tail < 0].sum() / total_negative * 100) if total_negative > 0 else float("nan")
    return {
        "tail_pct": tail_pct, "n_tail_trades": n_tail,
        "tail_mean_return_pct": float(tail.mean() * 100),
        "rest_mean_return_pct": float(rest.mean() * 100) if len(rest) else float("nan"),
        "tail_share_of_total_negative_pnl_pct": tail_negative_share,
    }


# ---------------------------------------------------------------------------
# STEP D — 상관관계(주간 수익률 시계열 기반)
# ---------------------------------------------------------------------------

def weekly_return_series(exit_ts: pd.DatetimeIndex, returns: np.ndarray) -> pd.Series:
    s = pd.Series(np.asarray(returns), index=pd.DatetimeIndex(exit_ts))
    return s.groupby(pd.Grouper(freq="W")).sum()


def series_correlation(a: pd.Series, b: pd.Series) -> dict:
    full_idx = a.index.union(b.index)
    a_full = a.reindex(full_idx, fill_value=0.0)
    b_full = b.reindex(full_idx, fill_value=0.0)
    corr = float(np.corrcoef(a_full.to_numpy(), b_full.to_numpy())[0, 1])
    return {"n_weeks_compared": len(full_idx), "weekly_return_correlation": corr, "is_independent": corr < CORR_THRESHOLD}


def load_accepted_signals(yaml_path: Path, project_root: Path, exclude_id: str | None = None) -> dict[str, pd.Series]:
    """signal_specs/accepted_signals.yaml(PASS/ACCEPT 상태 신호 레지스트리)을 읽어
    각 항목의 주간 수익률 시계열을 로드한다. exclude_id는 자기자신 비교를 막기 위한
    자기제외(문자열 완전일치만 — 의미상 동일한 다른 id는 걸러지지 않으니 호출부에서
    주의)."""
    if not yaml_path.exists():
        return {}
    spec = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    out: dict[str, pd.Series] = {}
    for entry in spec.get("accepted", []):
        if entry["id"] == exclude_id:
            continue
        path = project_root / entry["weekly_returns_path"]
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        out[entry["id"]] = df.iloc[:, 0]
    return out


def multi_correlation_check(candidate_weekly: pd.Series, accepted: dict[str, pd.Series], threshold: float = CORR_THRESHOLD) -> dict:
    """accepted_signals.yaml에 등록된 각 신호와 candidate의 주간수익률 상관계수를
    전부 계산한다(reference_signal 하나만 보던 기존 STEP D의 확장). 임계치는
    CORR_THRESHOLD 그대로 재사용 — 신호별로 조정하지 않는다."""
    per_signal = {sig_id: series_correlation(candidate_weekly, s) for sig_id, s in accepted.items()}
    redundant_with = [sig_id for sig_id, r in per_signal.items() if r["weekly_return_correlation"] >= threshold]
    return {"per_signal": per_signal, "redundant_with": redundant_with}


def correlation_matrix(series_dict: dict[str, pd.Series]) -> pd.DataFrame:
    """여러 신호의 주간 수익률 시계열을 받아 전체 쌍의 상관계수 행렬을 만든다
    (누적 매트릭스 — 새 신호가 검증될 때마다 이 함수에 딕셔너리를 늘려서 호출)."""
    ids = list(series_dict.keys())
    full_idx = None
    for s in series_dict.values():
        full_idx = s.index if full_idx is None else full_idx.union(s.index)
    aligned = pd.DataFrame({k: v.reindex(full_idx, fill_value=0.0) for k, v in series_dict.items()})
    return aligned.corr()


# ---------------------------------------------------------------------------
# 넷중립 체크 (spec.requires_net_neutral_check: true인 경우만)
# ---------------------------------------------------------------------------

def net_neutral_check(daily_net_exposure_pct: pd.Series, portfolio_weekly_returns: pd.Series, btc_weekly_returns: pd.Series) -> dict:
    std_net_exposure = float(daily_net_exposure_pct.std(ddof=1))
    full_idx = portfolio_weekly_returns.index.intersection(btc_weekly_returns.index)
    p = portfolio_weekly_returns.reindex(full_idx).to_numpy()
    b = btc_weekly_returns.reindex(full_idx).to_numpy()
    if len(full_idx) > 1 and np.var(b, ddof=1) > 0:
        beta = float(np.cov(p, b, ddof=1)[0, 1] / np.var(b, ddof=1))
        residuals = p - beta * b
        ss_res = float(np.sum(residuals ** 2))
        ss_tot = float(np.sum((p - p.mean()) ** 2))
        r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    else:
        beta, r_squared = float("nan"), float("nan")
    return {
        "daily_net_exposure_std_pct": std_net_exposure,
        "residual_beta_vs_btc": beta, "r_squared_vs_btc": r_squared,
        "n_weeks_compared": len(full_idx),
    }


# ---------------------------------------------------------------------------
# 최종 판정
# ---------------------------------------------------------------------------

def compute_verdict(step_c: dict, step_d: dict, multi_corr: dict | None = None, corr_threshold: float = CORR_THRESHOLD) -> str:
    """PASS / FAIL_NOT_SIGNIFICANT / FAIL_SIGNIFICANT_NEGATIVE / RECLASSIFY_AS_FILTER /
    REDUNDANT_WITH_{id}. 우선순위:
    1) reference_signal(보통 trend)과 상관 높으면 RECLASSIFY_AS_FILTER — 이건
       "trend의 필터냐 아니냐"를 묻는 특수 케이스라 항상 최우선.
    2) 이미 PASS/ACCEPT된 다른 신호(accepted_signals.yaml)와 상관 높으면
       REDUNDANT_WITH_{id} — multi_corr가 주어졌을 때만 검사(기존 호출부 하위호환,
       multi_corr=None이면 이 단계를 건너뛴다).
    3) 그 외엔 유의성으로 PASS/FAIL 판정.
    multi_corr가 None이면 기존(1세대/2세대) 동작과 완전히 동일하다."""
    if step_d.get("weekly_return_correlation", 0.0) >= corr_threshold:
        return "RECLASSIFY_AS_FILTER"
    if multi_corr and multi_corr.get("redundant_with"):
        return f"REDUNDANT_WITH_{multi_corr['redundant_with'][0]}"
    if step_c["mean_excludes_0"]:
        return "PASS" if step_c["point_mean_return_pct"] > 0 else "FAIL_SIGNIFICANT_NEGATIVE"
    return "FAIL_NOT_SIGNIFICANT"
