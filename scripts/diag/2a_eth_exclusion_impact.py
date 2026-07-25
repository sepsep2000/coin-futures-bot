"""scripts/diag/2a_eth_exclusion_impact.py — 2a 라이브 유니버스에서 ETH를
제외한 게(REBALANCE_ORDER_ANALYSIS.md 4절 임시완화책) 시장중립성에 주는
영향을 검증한다(reports/2A_ETH_EXCLUSION_IMPACT.md STEP 3 원자료).

방법론 변경 없음 — strategies/2a.py::run()을 그대로 재사용하되,
_load_universe_daily_and_funding()의 반환값에서 ETH 컬럼만 제거한 걸로
몽키패치해 19자산 기준 전체 이력을 재계산한다(strategies/2a.py 자체는
건드리지 않음, 게이트 통과 수치 영향 없음). 상관계수/잔여베타 계산은
checks/signal_validation.py의 기존 검증 함수(series_correlation,
net_neutral_check)를 그대로 호출한다 — 신규 통계 로직 없음.

filtered_trend 쪽 비교 시계열은 POST_FILTER_BUG_AUDIT.md에서 확정된
정책(사후필터링 진단 데이터 대신 정식 strategies/filtered_trend.py 수치를
기준으로 삼는다)에 따라 strategies/filtered_trend.py::run()에서 새로
계산한다 — reports/signal_validation_data/1a/1a_weekly_returns.csv(오염된
진단 경로)는 쓰지 않는다.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import checks.signal_validation as cv  # noqa: E402
from src.backtest.engine import SymbolData  # noqa: E402
from strategies.filtered_trend import run as run_filtered_trend  # noqa: E402

strategy_2a = importlib.import_module("strategies.2a")


def _load_symbol_data(symbol_file: str) -> SymbolData:
    ohlcv_15m = pd.read_parquet(PROJECT_ROOT / "data" / "ohlcv" / f"{symbol_file}_15m.parquet")
    ohlcv_1h = pd.read_parquet(PROJECT_ROOT / "data" / "ohlcv" / f"{symbol_file}_1h.parquet")
    funding = pd.read_parquet(PROJECT_ROOT / "data" / "funding" / f"{symbol_file}_funding.parquet")

    def _to_dt_index(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df.index = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df.drop(columns=["timestamp"])

    return SymbolData(df_15m=_to_dt_index(ohlcv_15m), df_1h=_to_dt_index(ohlcv_1h), funding=funding)


def _run_2a_with_universe_filter(cfg: dict, exclude: set[str]) -> pd.DataFrame:
    original = strategy_2a._load_universe_daily_and_funding

    def patched(cfg_, project_root_):
        daily_close, funding_raw = original(cfg_, project_root_)
        daily_close = daily_close.drop(columns=[c for c in exclude if c in daily_close.columns])
        funding_raw = {k: v for k, v in funding_raw.items() if k not in exclude}
        return daily_close, funding_raw

    strategy_2a._load_universe_daily_and_funding = patched
    try:
        return strategy_2a.run(cfg, PROJECT_ROOT)
    finally:
        strategy_2a._load_universe_daily_and_funding = original


def main() -> dict:
    cfg = yaml.safe_load((PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))

    df_20 = strategy_2a.run(cfg, PROJECT_ROOT)
    df_19 = _run_2a_with_universe_filter(cfg, exclude={"ETH"})

    weekly_20 = cv.weekly_return_series(pd.DatetimeIndex(df_20["exit_time"]), df_20["gross_return"].to_numpy())
    weekly_19 = cv.weekly_return_series(pd.DatetimeIndex(df_19["exit_time"]), df_19["gross_return"].to_numpy())

    # filtered_trend(정식, POST_FILTER_BUG_AUDIT.md 확정 정책) 주간 R배수 시계열
    ft_result = run_filtered_trend(_load_symbol_data("ETHUSDT-USDT"), cfg)
    ft_df = pd.DataFrame([{"exit_time": t.exit_time, "r_multiple": t.r_multiple} for t in ft_result.trades])
    ft_weekly = cv.weekly_return_series(pd.DatetimeIndex(ft_df["exit_time"]), ft_df["r_multiple"].to_numpy())

    corr_20 = cv.series_correlation(weekly_20, ft_weekly)
    corr_19 = cv.series_correlation(weekly_19, ft_weekly)

    daily_close_20, _ = strategy_2a._load_universe_daily_and_funding(cfg, PROJECT_ROOT)
    btc_daily = daily_close_20["BTC"].pct_change().dropna()
    btc_weekly = btc_daily.groupby(pd.Grouper(freq="W")).apply(lambda s: (1 + s).prod() - 1)
    zero_exposure = pd.Series(0.0, index=daily_close_20.index)  # 2a는 달러중립 구성상 항상 0(gen_2a와 동일 가정)

    nn_20 = cv.net_neutral_check(zero_exposure, weekly_20, btc_weekly)
    nn_19 = cv.net_neutral_check(zero_exposure, weekly_19, btc_weekly)

    summary = {
        "n_weeks_20": len(df_20), "n_weeks_19": len(df_19),
        "corr_vs_filtered_trend_20asset": corr_20["weekly_return_correlation"],
        "corr_vs_filtered_trend_19asset_eth_excluded": corr_19["weekly_return_correlation"],
        "n_weeks_compared_corr_20": corr_20["n_weeks_compared"],
        "n_weeks_compared_corr_19": corr_19["n_weeks_compared"],
        "residual_beta_vs_btc_20asset": nn_20["residual_beta_vs_btc"],
        "residual_beta_vs_btc_19asset_eth_excluded": nn_19["residual_beta_vs_btc"],
        "r_squared_vs_btc_20asset": nn_20["r_squared_vs_btc"],
        "r_squared_vs_btc_19asset_eth_excluded": nn_19["r_squared_vs_btc"],
        "n_weeks_compared_beta_20": nn_20["n_weeks_compared"],
        "n_weeks_compared_beta_19": nn_19["n_weeks_compared"],
        "mean_weekly_gross_return_20asset": float(df_20["gross_return"].mean()),
        "mean_weekly_gross_return_19asset": float(df_19["gross_return"].mean()),
        "std_weekly_gross_return_20asset": float(df_20["gross_return"].std()),
        "std_weekly_gross_return_19asset": float(df_19["gross_return"].std()),
    }

    out_dir = PROJECT_ROOT / "reports" / "diag_data"
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.Series(summary).to_csv(out_dir / "2a_eth_exclusion_impact_summary.csv")
    weekly_20.to_csv(out_dir / "2a_weekly_gross_returns_20asset.csv", header=["weekly_return"])
    weekly_19.to_csv(out_dir / "2a_weekly_gross_returns_19asset_eth_excluded.csv", header=["weekly_return"])
    return summary


if __name__ == "__main__":
    result = main()
    for k, v in result.items():
        print(f"{k}: {v}")
