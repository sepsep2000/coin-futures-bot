"""D5 — 블록 부트스트랩 신뢰구간 + 통계적 검정력(power) 분석.

트레이드는 같은 추세 구간 내에서 연달아 발생해 독립이 아니다(자기상관).
i.i.d 부트스트랩은 이 상관을 무시해 신뢰구간을 과소평가하므로, 블록
부트스트랩(연속 구간 단위 재추출)을 기본으로 쓰고 i.i.d와 비교해 차이를
같이 보여준다. 블록 길이는 그리드서치 대상이 아니라 n^(1/3) 경험칙(약 10)과
민감도 확인용 30을 함께 보고한다.

검정력 분석은 정규근사(one-sample z-test 근사)를 쓴다 — scipy 의존성을
추가하지 않기 위해 math.erf로 표준정규 CDF를 직접 구현.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from scripts.diag.common import DIAG_DATA_DIR, get_trend_trades, profit_factor_from_pnls, trades_to_df

N_BOOT = 5000
SEED = 777
BLOCK_SIZES = [1, 10, 30]  # 1 = i.i.d 비교용
Z_ALPHA_TWO_SIDED_05 = 1.959963985
Z_BETA_POWER_80 = 0.841621234
Z_BETA_POWER_90 = 1.281551566


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _block_bootstrap_once(rng: np.random.Generator, series: np.ndarray, block_size: int) -> np.ndarray:
    n = len(series)
    if block_size <= 1:
        idx = rng.integers(0, n, size=n)
        return series[idx]
    n_blocks_needed = math.ceil(n / block_size)
    starts = rng.integers(0, n, size=n_blocks_needed)
    out = []
    for s in starts:
        block_idx = (np.arange(block_size) + s) % n
        out.append(series[block_idx])
    return np.concatenate(out)[:n]


def _bootstrap_for_block_size(rng: np.random.Generator, r_multiples: np.ndarray, pnls: np.ndarray, block_size: int, n_boot: int) -> dict:
    n = len(r_multiples)
    boot_mean_r = np.empty(n_boot)
    boot_pf = np.empty(n_boot)
    for b in range(n_boot):
        idx_series = np.arange(n)
        resampled_idx = _block_bootstrap_once(rng, idx_series, block_size)
        boot_mean_r[b] = r_multiples[resampled_idx].mean()
        boot_pf[b] = profit_factor_from_pnls(pnls[resampled_idx])
    finite_pf = boot_pf[np.isfinite(boot_pf)]
    return {
        "block_size": block_size,
        "mean_r_ci_2.5": float(np.percentile(boot_mean_r, 2.5)),
        "mean_r_ci_50": float(np.percentile(boot_mean_r, 50)),
        "mean_r_ci_97.5": float(np.percentile(boot_mean_r, 97.5)),
        "frac_boot_mean_r_le_0": float((boot_mean_r <= 0).mean()),
        "pf_ci_2.5": float(np.percentile(finite_pf, 2.5)) if len(finite_pf) else float("nan"),
        "pf_ci_50": float(np.percentile(finite_pf, 50)) if len(finite_pf) else float("nan"),
        "pf_ci_97.5": float(np.percentile(finite_pf, 97.5)) if len(finite_pf) else float("nan"),
        "frac_boot_pf_le_1": float((boot_pf <= 1.0).mean()),
    }


def _power_analysis(mean_r: float, std_r: float, n: int) -> dict:
    if std_r <= 0 or n <= 1:
        return {"achieved_power": float("nan"), "required_n_power_80": None, "required_n_power_90": None}
    d = mean_r / std_r
    z_effect = math.sqrt(n) * d
    achieved_power = _norm_cdf(z_effect - Z_ALPHA_TWO_SIDED_05) if z_effect >= 0 else _norm_cdf(-z_effect - Z_ALPHA_TWO_SIDED_05)
    if abs(d) < 1e-9:
        req_80 = req_90 = None
    else:
        req_80 = math.ceil(((Z_ALPHA_TWO_SIDED_05 + Z_BETA_POWER_80) / abs(d)) ** 2)
        req_90 = math.ceil(((Z_ALPHA_TWO_SIDED_05 + Z_BETA_POWER_90) / abs(d)) ** 2)
    return {
        "cohens_d": d,
        "achieved_power_at_n": achieved_power,
        "required_n_power_80": req_80,
        "required_n_power_90": req_90,
    }


def run(n_boot: int = N_BOOT) -> dict:
    trades = get_trend_trades(use_grace=True)
    df = trades_to_df(trades).sort_values("exit_time").reset_index(drop=True)
    r_multiples = df["r_multiple"].to_numpy()
    pnls = df["pnl_usd"].to_numpy()
    n = len(df)

    rng = np.random.default_rng(SEED)
    rows = [_bootstrap_for_block_size(rng, r_multiples, pnls, bs, n_boot) for bs in BLOCK_SIZES]
    boot_df = pd.DataFrame(rows)
    boot_df.to_csv(DIAG_DATA_DIR / "d5_block_bootstrap.csv", index=False)

    mean_r = float(r_multiples.mean())
    std_r = float(r_multiples.std(ddof=1))
    power = _power_analysis(mean_r, std_r, n)

    summary = {
        "n_trades": n,
        "mean_r_multiple": mean_r,
        "std_r_multiple": std_r,
        **power,
    }
    pd.Series(summary).to_csv(DIAG_DATA_DIR / "d5_summary.csv")
    return {"bootstrap": rows, "power": summary}


if __name__ == "__main__":
    result = run()
    for row in result["bootstrap"]:
        print(row)
    print(result["power"])
