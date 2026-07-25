"""scripts/validate_signal.py — 신호 검증 하네스 (Signal Validation Gate 오케스트레이터).

signal_specs/{id}.yaml을 읽어 해당 신호의 GENERATOR(이 파일 안의 gen_* 함수)를
호출해 트레이드/이벤트를 만들고, checks/signal_validation.py의 공용 STEP A~D
엔진을 그대로 적용해 reports/SIGNAL_VALIDATION_{id}.md + reports/
signal_validation_data/{id}/*.csv를 만든다.

★ 설계상의 정직한 한계: signal_specs/*.yaml은 완전 선언적 DSL이 아니다 — 신호마다
진입/청산 로직 자체는 다르므로(1a/2a/4a/4b 전부 다른 생성 로직) 이 파일의
GENERATORS 레지스트리에 신호별 함수를 하나씩 둔다. yaml은 그 함수에 주입할
파라미터(숫자 하드코딩 방지)만 담는다. 재사용되는 건 STEP A~D의 "통계 엔진"
부분이고(checks/signal_validation.py), 그건 완전히 신호 무관 공용 코드다.

그리드서치 금지 원칙: 각 gen_* 함수의 파라미터는 REDESIGN_PROPOSAL.md에 이미
명시된 값이거나, config.yaml에 이미 고정된 기존 값을 재사용한다(신규 숫자를
검증 통과 목적으로 발명하지 않는다) — 각 함수 docstring에 출처를 명시한다.

사용:
    python scripts/validate_signal.py signal_specs/1a.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import checks.signal_validation as cv  # noqa: E402
from scripts.diag.common import (  # noqa: E402
    DIAG_DATA_DIR,
    eth_data,
    get_trend_trades,
    load_base_config,
    load_symbol_data,
    symbol_file,
    trades_to_df,
)
from src.strategy.indicators import adx, bb_width_pct, bollinger_bands, rolling_percentile
from src.strategy.trend import generate_trend_signals  # noqa: E402

SIGNAL_DATA_DIR = PROJECT_ROOT / "reports" / "signal_validation_data"


# ---------------------------------------------------------------------------
# 신호별 생성기 (GENERATORS 레지스트리) — 각자 다른 진입/청산 로직
# ---------------------------------------------------------------------------

def gen_1a(cfg: dict, params: dict) -> dict:
    """1a — 1D 모멘텀 정합 필터. REDESIGN_PROPOSAL.md 1a 명세: 기존 trend 신호
    발화 + 1D ROC(기본 5일, params.momentum_lookback_days) 같은 방향일 때만
    채택. 청산은 trend 그대로 재사용(신규 로직 없음, 명세 그대로)."""
    data = eth_data()["ETH/USDT:USDT"]
    trend_trades = get_trend_trades(use_grace=True)
    df = trades_to_df(trend_trades)

    lookback_days = params.get("momentum_lookback_days", 5)  # REDESIGN_PROPOSAL.md 예시값
    daily_close = data.df_15m["close"].resample("1D").last()
    daily_roc = daily_close.pct_change(lookback_days).reset_index()
    daily_roc.columns = ["ts", "roc"]

    entries = df[["entry_time"]].reset_index().rename(columns={"index": "orig_idx"}).sort_values("entry_time")
    merged = pd.merge_asof(entries, daily_roc.sort_values("ts"), left_on="entry_time", right_on="ts", direction="backward")
    merged = merged.set_index("orig_idx").sort_index()
    df["roc"] = merged["roc"]

    agree = ((df["direction"] == "long") & (df["roc"] > 0)) | ((df["direction"] == "short") & (df["roc"] < 0))
    filtered = df[agree].dropna(subset=["roc"]).copy()
    filtered["return"] = filtered["r_multiple"]

    bar_index = data.df_15m.index
    entry_pos = bar_index.searchsorted(filtered["entry_time"].to_numpy())
    exit_pos = bar_index.searchsorted(filtered["exit_time"].to_numpy())
    n_bars = len(bar_index)
    filtered["entry_idx"] = np.clip(entry_pos, 0, n_bars - 1)
    filtered["exit_idx"] = np.clip(exit_pos, 0, n_bars - 1)

    return {
        "trades_df": filtered.reset_index(drop=True), "bar_index": bar_index, "n_bars": n_bars,
        "reference_windows": list(zip(np.clip(bar_index.searchsorted(df["entry_time"].to_numpy()), 0, n_bars - 1),
                                       np.clip(bar_index.searchsorted(df["exit_time"].to_numpy()), 0, n_bars - 1))),
        "daily_net_exposure": None,
    }


def gen_4a(cfg: dict, params: dict) -> dict:
    """4a — 변동성 압축 후 확장(스퀴즈 브레이크아웃). 진입: BB폭 백분위가
    regime cfg의 RANGE 임계치(<40) 아래였다가 TREND 임계치(>=60) 위로 전환하는
    순간(둘 다 config.yaml의 기존 regime 임계값 재사용, 신규 수치 없음).
    lookback_bars는 백분위 계산창(bb_width_percentile_window=120, 기존값)을
    그대로 재사용. 청산: 폭이 다시 60 아래로 내려가거나 time_exit_bars(48,
    3a/3b와 동일하게 재사용) 경과."""
    data = eth_data()["ETH/USDT:USDT"]
    regime_cfg = cfg["regime"]
    range_max = regime_cfg["bb_width_percentile_range_max"]  # 40
    trend_min = regime_cfg["bb_width_percentile_trend_min"]  # 60
    lookback_bars = regime_cfg["bb_width_percentile_window"]  # 120
    time_exit_bars = params.get("time_exit_bars", 48)

    close = data.df_15m["close"]
    width = bb_width_pct(close, regime_cfg["bb_period"])
    width_pct = rolling_percentile(width, regime_cfg["bb_width_percentile_window"]).to_numpy()
    c = close.to_numpy()

    trades = []
    for i in range(lookback_bars, len(c) - 1):
        if np.isnan(width_pct[i]) or np.isnan(width_pct[i - 1]):
            continue
        crossed = width_pct[i - 1] < trend_min <= width_pct[i]
        if not crossed:
            continue
        was_squeezed = np.nanmin(width_pct[max(0, i - lookback_bars):i]) < range_max
        if not was_squeezed:
            continue
        direction = "long" if c[i] > c[i - 1] else "short"
        entry_price = c[i]
        end_idx = min(i + time_exit_bars, len(c) - 1)
        exit_idx, reason = end_idx, "time_exit"
        for j in range(i + 1, end_idx + 1):
            if not np.isnan(width_pct[j]) and width_pct[j] < trend_min:
                exit_idx, reason = j, "vol_contraction"
                break
        exit_price = c[exit_idx]
        ret = exit_price / entry_price - 1 if direction == "long" else entry_price / exit_price - 1
        trades.append({"entry_idx": i, "exit_idx": exit_idx, "direction": direction, "exit_reason": reason, "return": ret})

    trades_df = pd.DataFrame(trades)
    bar_index = data.df_15m.index
    n_bars = len(bar_index)
    trades_df["entry_time"] = bar_index[trades_df["entry_idx"]] if len(trades_df) else []
    trades_df["exit_time"] = bar_index[trades_df["exit_idx"]] if len(trades_df) else []

    trend_trades = get_trend_trades(use_grace=True)
    trend_df = trades_to_df(trend_trades)
    ref_windows = list(zip(np.clip(bar_index.searchsorted(trend_df["entry_time"].to_numpy()), 0, n_bars - 1),
                            np.clip(bar_index.searchsorted(trend_df["exit_time"].to_numpy()), 0, n_bars - 1)))

    return {"trades_df": trades_df, "bar_index": bar_index, "n_bars": n_bars, "reference_windows": ref_windows, "daily_net_exposure": None}


def gen_4b(cfg: dict, params: dict) -> dict:
    """4b — ADX 저점 반전(추세 조기 진입). 진입: ADX(14, regime cfg 재사용)가
    최근 lookback_bars(20, regime cfg의 bb_period 재사용 — 새 수치 발명 안 함)
    구간 최저치를 찍고 3봉 연속 상승, 값은 아직 adx_trend_min(25, 기존값)
    미만. 방향은 동일 lookback의 가격 모멘텀 부호. 청산: ADX 재하락 또는
    time_exit_bars(48, 재사용) 경과 — REDESIGN_PROPOSAL.md의 "TREND 공식
    진입 시 trend에 이관" 부분은 검증 단계 단순화를 위해 생략(자체 신호
    품질만 측정, 본문에 명시)."""
    data = eth_data()["ETH/USDT:USDT"]
    regime_cfg = cfg["regime"]
    lookback_bars = regime_cfg["bb_period"]  # 20, 재사용
    adx_trend_min = regime_cfg["adx_trend_min"]  # 25
    time_exit_bars = params.get("time_exit_bars", 48)

    high, low, close = data.df_15m["high"], data.df_15m["low"], data.df_15m["close"]
    adx_series = adx(high, low, close, regime_cfg["adx_period"]).to_numpy()
    c = close.to_numpy()

    trades = []
    for i in range(lookback_bars + 3, len(c) - 1):
        trough_idx = i - 3
        window = adx_series[trough_idx - lookback_bars + 1:trough_idx + 1]
        if np.isnan(window).any() or np.isnan(adx_series[i]):
            continue
        is_local_min = adx_series[trough_idx] <= window.min()
        rising_3 = (
            adx_series[i - 2] > adx_series[trough_idx]
            and adx_series[i - 1] > adx_series[i - 2]
            and adx_series[i] > adx_series[i - 1]
        )
        below_trend_min = adx_series[i] < adx_trend_min
        if not (is_local_min and rising_3 and below_trend_min):
            continue
        direction = "long" if c[i] > c[i - lookback_bars] else "short"
        entry_price = c[i]
        end_idx = min(i + time_exit_bars, len(c) - 1)
        exit_idx, reason = end_idx, "time_exit"
        for j in range(i + 1, end_idx + 1):
            if not np.isnan(adx_series[j]) and not np.isnan(adx_series[j - 1]) and adx_series[j] < adx_series[j - 1]:
                exit_idx, reason = j, "adx_rollover"
                break
        exit_price = c[exit_idx]
        ret = exit_price / entry_price - 1 if direction == "long" else entry_price / exit_price - 1
        trades.append({"entry_idx": i, "exit_idx": exit_idx, "direction": direction, "exit_reason": reason, "return": ret})

    trades_df = pd.DataFrame(trades)
    bar_index = data.df_15m.index
    n_bars = len(bar_index)
    trades_df["entry_time"] = bar_index[trades_df["entry_idx"]] if len(trades_df) else []
    trades_df["exit_time"] = bar_index[trades_df["exit_idx"]] if len(trades_df) else []

    trend_trades = get_trend_trades(use_grace=True)
    trend_df = trades_to_df(trend_trades)
    ref_windows = list(zip(np.clip(bar_index.searchsorted(trend_df["entry_time"].to_numpy()), 0, n_bars - 1),
                            np.clip(bar_index.searchsorted(trend_df["exit_time"].to_numpy()), 0, n_bars - 1)))

    return {"trades_df": trades_df, "bar_index": bar_index, "n_bars": n_bars, "reference_windows": ref_windows, "daily_net_exposure": None}


def gen_2a(cfg: dict, params: dict) -> dict:
    """2a — 횡단면 상대모멘텀 랭킹(시장중립). config/tickers.txt 20자산 일봉
    기준. lookback_days=7(REDESIGN_PROPOSAL.md 예시값 그대로), 상/하위
    분위(quartile, 20자산 표준 4분위=5개씩 — 탐색 아니라 관행적 분할)
    롱/숏, 주간 리밸런스. STEP A(레짐 배타성)는 해당 없음(spec.regime_filter
    null) — 횡단면 신호는 단일 자산 레짐 개념이 없다."""
    with open(PROJECT_ROOT / "config" / "tickers.txt", encoding="utf-8") as f:
        tickers = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]

    closes = {}
    for pair in tickers:
        d = load_symbol_data(symbol_file(pair))
        closes[pair.split("/")[0]] = d.df_15m["close"].resample("1D").last()
    daily = pd.DataFrame(closes).dropna()

    lookback_days = params.get("lookback_days", 7)
    weekly_closes = daily.resample("W").last()
    lookback_ret = daily.pct_change(lookback_days)

    rows = []
    for i in range(len(weekly_closes.index) - 1):
        reb_date = weekly_closes.index[i]
        exit_date = weekly_closes.index[i + 1]
        asof_ret = lookback_ret.asof(reb_date).dropna()
        if len(asof_ret) < 8:
            continue
        ranked = asof_ret.sort_values()
        k = max(1, len(ranked) // 4)
        bottom, top = ranked.index[:k], ranked.index[-k:]
        next_week_ret = weekly_closes.iloc[i + 1] / weekly_closes.iloc[i] - 1
        port_ret = float(next_week_ret[top].mean() - next_week_ret[bottom].mean())
        rows.append({"entry_time": reb_date, "exit_time": exit_date, "direction": "market_neutral",
                      "return": port_ret, "n_top": len(top), "n_bottom": len(bottom)})

    trades_df = pd.DataFrame(rows)
    daily_net_exposure = pd.Series(0.0, index=daily.index)  # 달러중립 구성상 항상 0

    return {"trades_df": trades_df, "bar_index": None, "n_bars": None, "reference_windows": None,
            "daily_net_exposure": daily_net_exposure, "daily_close_for_btc": daily["BTC"]}


def gen_5a(cfg: dict, params: dict) -> dict:
    """5a — 횡단면 캐리(펀딩비 상대 순위). SIGNAL_CATALOG_V2.md 5a 명세.
    2a와 동일한 20자산·4분위·주간 리밸런스 구조를 재사용(신규 구조 발명 없음),
    랭킹 기준만 모멘텀 대신 "현재" 펀딩비 스냅샷으로 교체. 수익 = 가격
    스프레드(2a와 동일 계산) + 캐리(숏그룹 펀딩합 평균 - 롱그룹 펀딩합 평균,
    숏그룹이 펀딩비 높은 쪽이라 구조적으로 양(+))."""
    with open(PROJECT_ROOT / "config" / "tickers.txt", encoding="utf-8") as f:
        tickers = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]

    closes, funding_raw = {}, {}
    for pair in tickers:
        d = load_symbol_data(symbol_file(pair))
        base = pair.split("/")[0]
        closes[base] = d.df_15m["close"].resample("1D").last()
        fdf = d.funding.copy()
        fdf["ts"] = pd.to_datetime(fdf["timestamp"], unit="ms", utc=True)
        funding_raw[base] = fdf.set_index("ts")["funding_rate"].sort_index()

    daily_close = pd.DataFrame(closes).dropna()
    weekly_closes = daily_close.resample("W").last()
    weekly_dates = weekly_closes.index

    rows = []
    for i in range(len(weekly_dates) - 1):
        reb_date, exit_date = weekly_dates[i], weekly_dates[i + 1]
        snapshot = {b: s.asof(reb_date) for b, s in funding_raw.items()}
        snapshot = pd.Series(snapshot).dropna()
        if len(snapshot) < 8:
            continue
        ranked = snapshot.sort_values()
        k = max(1, len(ranked) // 4)
        long_group, short_group = ranked.index[:k], ranked.index[-k:]

        next_week_price_ret = weekly_closes.iloc[i + 1] / weekly_closes.iloc[i] - 1
        price_component = float(next_week_price_ret[long_group].mean() - next_week_price_ret[short_group].mean())

        funding_sum = {}
        for b in list(long_group) + list(short_group):
            s = funding_raw[b]
            funding_sum[b] = float(s[(s.index > reb_date) & (s.index <= exit_date)].sum())
        funding_sum = pd.Series(funding_sum)
        carry_component = float(funding_sum[short_group].mean() - funding_sum[long_group].mean())

        rows.append({
            "entry_time": reb_date, "exit_time": exit_date, "direction": "market_neutral",
            "return": price_component + carry_component, "price_component": price_component,
            "carry_component": carry_component, "n_top": len(long_group), "n_bottom": len(short_group),
        })

    trades_df = pd.DataFrame(rows)
    daily_net_exposure = pd.Series(0.0, index=daily_close.index)

    return {"trades_df": trades_df, "bar_index": None, "n_bars": None, "reference_windows": None,
            "daily_net_exposure": daily_net_exposure, "daily_close_for_btc": daily_close["BTC"]}


GENERATORS = {"gen_1a": gen_1a, "gen_2a": gen_2a, "gen_4a": gen_4a, "gen_4b": gen_4b, "gen_5a": gen_5a}


# ---------------------------------------------------------------------------
# 오케스트레이션
# ---------------------------------------------------------------------------

def run_validation(spec_path: Path) -> dict:
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    sig_id = spec["id"]
    cfg = load_base_config()
    gen_fn = GENERATORS[spec["generator"]]
    gen_out = gen_fn(cfg, spec.get("params", {}))

    trades_df = gen_out["trades_df"]
    out_dir = SIGNAL_DATA_DIR / sig_id
    out_dir.mkdir(parents=True, exist_ok=True)
    trades_df.to_csv(out_dir / f"{sig_id}_trades.csv", index=False)

    result: dict = {"id": sig_id, "n_trades": len(trades_df)}

    # STEP A
    step_a = None
    if spec.get("regime_filter") and gen_out.get("reference_windows") is not None and len(trades_df):
        cand_windows = list(zip(trades_df["entry_idx"], trades_df["exit_idx"]))
        step_a = cv.check_exclusivity(cand_windows, gen_out["reference_windows"], gen_out["n_bars"])
    result["step_a"] = step_a

    if len(trades_df) == 0:
        result["step_b"] = result["step_c"] = result["step_d"] = None
        result["verdict"] = "FAIL_NOT_SIGNIFICANT"
        _write_report(spec, result, out_dir, gen_out)
        return result

    # STEP B
    cluster_ids = cv.cluster_events(trades_df["entry_time"])
    trades_df["cluster_id"] = cluster_ids
    step_b = cv.clustering_summary(cluster_ids)
    result["step_b"] = step_b

    # STEP C
    returns = trades_df["return"].to_numpy()
    step_c = cv.cluster_bootstrap(returns, cluster_ids)
    step_c["tail_loss"] = cv.tail_loss_analysis(returns)
    result["step_c"] = step_c
    pd.Series({k: v for k, v in step_c.items() if k != "tail_loss"}).to_csv(out_dir / f"{sig_id}_stepC_bootstrap.csv")
    pd.Series(step_c["tail_loss"]).to_csv(out_dir / f"{sig_id}_stepC_tail_loss.csv")

    # STEP D
    cand_weekly = cv.weekly_return_series(trades_df["exit_time"], returns)
    trend_trades = get_trend_trades(use_grace=True)
    trend_df = trades_to_df(trend_trades)
    trend_weekly = cv.weekly_return_series(trend_df["exit_time"], trend_df["r_multiple"].to_numpy())
    step_d = cv.series_correlation(cand_weekly, trend_weekly)
    result["step_d"] = step_d
    cand_weekly.to_csv(out_dir / f"{sig_id}_weekly_returns.csv", header=["weekly_return"])

    # 넷중립 체크(옵션)
    if spec.get("requires_net_neutral_check"):
        btc_daily = gen_out["daily_close_for_btc"].pct_change().dropna()
        btc_weekly = btc_daily.groupby(pd.Grouper(freq="W")).apply(lambda s: (1 + s).prod() - 1)
        nn = cv.net_neutral_check(gen_out["daily_net_exposure"], cand_weekly, btc_weekly)
        result["net_neutral"] = nn
        pd.Series(nn).to_csv(out_dir / f"{sig_id}_net_neutral.csv")

    result["verdict"] = cv.compute_verdict(step_c, step_d)
    _write_report(spec, result, out_dir, gen_out)
    return result


def _write_report(spec: dict, result: dict, out_dir: Path, gen_out: dict) -> None:
    sig_id = result["id"]
    is_r_multiple = spec.get("return_units") == "r_multiple"
    unit_suffix = "R" if is_r_multiple else "%"
    def fmt(pct_value: float) -> str:
        v = pct_value / 100.0 if is_r_multiple else pct_value
        return f"{v:.4f}{unit_suffix}" if is_r_multiple else f"{v:.4f}%"

    lines = [f"# SIGNAL_VALIDATION_{sig_id}.md — {spec.get('description', sig_id)}", ""]
    if is_r_multiple:
        lines.append("(주: 이 신호는 trend 청산을 그대로 재사용해 수익률 단위가 R-멀티플이다 — % 아님)")
        lines.append("")
    lines.append(f"카테고리: {spec.get('category', 'N/A')} | 트레이드/이벤트 수: {result['n_trades']}")
    lines.append("")

    lines.append("## STEP A. 배타성")
    if result.get("step_a") is None:
        lines.append("해당 없음(spec.regime_filter 미지정 또는 트레이드 없음)")
    else:
        a = result["step_a"]
        lines.append(f"- 활성 봉수: {a['cand_active_bars']}, trend와 중첩 봉수: {a['overlap_bars']} ({a['overlap_pct']:.2f}%)")
        lines.append(f"- 판정(<{cv.OVERLAP_THRESHOLD_PCT}%): {'배타성 확인' if a['exclusivity_confirmed'] else '배타성 실패(높은 중첩)'}")
    lines.append("")

    lines.append("## STEP B. 이벤트 독립성(클러스터링)")
    if result.get("step_b") is None:
        lines.append("[자료 부족] — 트레이드 없음")
    else:
        b = result["step_b"]
        lines.append(f"- 원시 이벤트 {b['n_events_raw']}건 -> 독립 클러스터 {b['n_independent_clusters']}개")
        lines.append(f"- 클러스터 크기 평균 {b['cluster_size_mean']:.2f} / 최대 {b['cluster_size_max']} / 클러스터≥2 비중 {b['pct_events_in_clusters_ge2']:.1f}%")
    lines.append("")

    lines.append("## STEP C. 클러스터 블록부트스트랩 유의성 + 꼬리손실")
    if result.get("step_c") is None:
        lines.append("[자료 부족]")
    else:
        c = result["step_c"]
        lines.append(f"- 클러스터 {c['n_clusters']}개, 평균수익률 {fmt(c['point_mean_return_pct'])} (95% CI [{fmt(c['mean_ci_lo_pct'])}, {fmt(c['mean_ci_hi_pct'])}]) — 0 배제: {c['mean_excludes_0']}")
        lines.append(f"- PF {c['point_pf']:.3f} (95% CI [{c['pf_ci_lo']:.3f}, {c['pf_ci_hi']:.3f}]) — 1.0 배제: {c['pf_excludes_1']}")
        t = c["tail_loss"]
        lines.append(f"- 꼬리손실(하위 {t['tail_pct']}%, {t['n_tail_trades']}건): 평균 {fmt(t['tail_mean_return_pct'])} vs 나머지 평균 {fmt(t['rest_mean_return_pct'])}, 전체 음의손익 중 비중 {t['tail_share_of_total_negative_pnl_pct']:.1f}%")
    lines.append("")

    lines.append("## STEP D. 기존 trend 신호와의 상관관계")
    if result.get("step_d") is None:
        lines.append("[자료 부족]")
    else:
        d = result["step_d"]
        lines.append(f"- 비교 주수 {d['n_weeks_compared']}, 주간수익률 상관계수 {d['weekly_return_correlation']:.4f} — 독립(<{cv.CORR_THRESHOLD}): {d['is_independent']}")
    lines.append("")

    if "net_neutral" in result:
        nn = result["net_neutral"]
        lines.append("## 넷중립 체크")
        lines.append(f"- 일별 넷익스포저 표준편차: {nn['daily_net_exposure_std_pct']:.4f}%")
        lines.append(f"- BTC 대비 잔여베타: {nn['residual_beta_vs_btc']:.3f} (R²={nn['r_squared_vs_btc']:.3f}, 비교주수 {nn['n_weeks_compared']})")
        lines.append("")

    lines.append(f"## 최종 판정: **{result['verdict']}**")
    (Path(PROJECT_ROOT) / "reports" / f"SIGNAL_VALIDATION_{sig_id}.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python scripts/validate_signal.py signal_specs/<id>.yaml")
        sys.exit(1)
    res = run_validation(Path(sys.argv[1]))
    print(f"{res['id']}: verdict={res['verdict']}, n_trades={res['n_trades']}")
