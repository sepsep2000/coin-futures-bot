"""scripts/collect_universe.py — Phase 0-R: 자산 유니버스 확장(3->20) 데이터 수집.

config/tickers.txt(데이터 수집 전용 유니버스, config.yaml의 exchange.pairs와
무관)에 나열된 자산에 대해 기존 src/data/feed.py 파이프라인을 그대로
재사용해 15m·1h OHLCV + 펀딩비를 수집한다. scripts/collect_data.py(Phase 0,
전략 활성 자산 전용)는 수정하지 않는다 — 이 스크립트는 별도 진입점이다.

사용:
    python scripts/collect_universe.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data.feed import (  # noqa: E402
    FUNDING_INTERVAL_MS,
    TIMEFRAME_MS,
    detect_duplicates,
    detect_gaps,
    get_exchange,
    update_funding_cache,
    update_ohlcv_cache,
)


def _load_tickers(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _ts_to_iso(ts_ms) -> str:
    if ts_ms is None:
        return "N/A"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def main() -> None:
    with open(PROJECT_ROOT / "config" / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    tickers = _load_tickers(PROJECT_ROOT / "config" / "tickers.txt")
    timeframes = [cfg["exchange"]["timeframe_trend"], cfg["exchange"]["timeframe_regime"]]
    since_ms = int(datetime.fromisoformat(cfg["data"]["since"].replace("Z", "+00:00")).timestamp() * 1000)
    ohlcv_cache_dir = PROJECT_ROOT / cfg["data"]["ohlcv_cache_dir"]
    funding_cache_dir = PROJECT_ROOT / cfg["data"]["funding_cache_dir"]

    exchange = get_exchange(testnet=False)  # DATA_REPORT.md SPEC 8 결론과 동일 — 과거 데이터는 항상 live

    ohlcv_report: dict[str, dict] = {}
    funding_report: dict[str, dict] = {}

    for pair in tickers:
        for tf in timeframes:
            print(f"[OHLCV] {pair} {tf} 수집 중...", flush=True)
            t0 = time.time()
            df = update_ohlcv_cache(exchange, pair, tf, since_ms, ohlcv_cache_dir)
            dup = detect_duplicates(df)
            gaps = detect_gaps(df, TIMEFRAME_MS[tf])
            ohlcv_report[f"{pair}:{tf}"] = {
                "rows": len(df),
                "start": df["timestamp"].min() if not df.empty else None,
                "end": df["timestamp"].max() if not df.empty else None,
                "duplicate_count": dup,
                "gap_count": len(gaps),
                "missing_candles_total": sum(g["missing_candles"] for g in gaps),
                "elapsed_sec": round(time.time() - t0, 1),
            }
            r = ohlcv_report[f"{pair}:{tf}"]
            print(f"  -> {r['rows']} rows, {r['gap_count']} gaps, {r['duplicate_count']} dup, {r['elapsed_sec']}s", flush=True)

        print(f"[Funding] {pair} 수집 중...", flush=True)
        t0 = time.time()
        fdf = update_funding_cache(exchange, pair, since_ms, funding_cache_dir)
        fdup = detect_duplicates(fdf)
        fgaps = detect_gaps(fdf, FUNDING_INTERVAL_MS)
        funding_report[pair] = {
            "rows": len(fdf),
            "start": fdf["timestamp"].min() if not fdf.empty else None,
            "end": fdf["timestamp"].max() if not fdf.empty else None,
            "duplicate_count": fdup,
            "gap_count": len(fgaps),
            "elapsed_sec": round(time.time() - t0, 1),
        }
        r = funding_report[pair]
        print(f"  -> {r['rows']} rows, {r['gap_count']} gaps, {r['duplicate_count']} dup, {r['elapsed_sec']}s", flush=True)

    _write_report(cfg, tickers, ohlcv_report, funding_report)
    print("UNIVERSE_EXPANSION_LOG.md 작성 완료", flush=True)


def _write_report(cfg, tickers, ohlcv_report, funding_report) -> None:
    lines = [
        "# UNIVERSE_EXPANSION_LOG.md — Phase 0-R 자산 유니버스 확장(3->20) 수집 로그",
        "",
        f"생성 시각: {datetime.now(timezone.utc).isoformat()}",
        f"수집 범위: {cfg['data']['since']} ~ 현재 (live/프로덕션 엔드포인트, config/tickers.txt 기준 {len(tickers)}자산)",
        "",
        "## OHLCV",
        "",
        "| Pair:Timeframe | 캔들 수 | 시작 | 끝 | 중복 | 결측 구간 수 | 결측 캔들 합계 | 소요(초) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    total_gap_candles = 0
    for key, r in ohlcv_report.items():
        total_gap_candles += r["missing_candles_total"]
        lines.append(
            f"| {key} | {r['rows']} | {_ts_to_iso(r['start'])} | {_ts_to_iso(r['end'])} "
            f"| {r['duplicate_count']} | {r['gap_count']} | {r['missing_candles_total']} | {r['elapsed_sec']} |"
        )

    lines += ["", "## 펀딩비 이력", "", "| Pair | 레코드 수 | 시작 | 끝 | 중복 | 결측 구간 수 | 소요(초) |", "|---|---|---|---|---|---|---|"]
    for pair, r in funding_report.items():
        lines.append(
            f"| {pair} | {r['rows']} | {_ts_to_iso(r['start'])} | {_ts_to_iso(r['end'])} "
            f"| {r['duplicate_count']} | {r['gap_count']} | {r['elapsed_sec']} |"
        )

    total_gaps = sum(r["gap_count"] for r in ohlcv_report.values()) + sum(r["gap_count"] for r in funding_report.values())
    total_dup = sum(r["duplicate_count"] for r in ohlcv_report.values()) + sum(r["duplicate_count"] for r in funding_report.values())
    lines += [
        "",
        "## 무결성 요약",
        "",
        f"- 총 결측 구간: {total_gaps}건 (결측 캔들 합계 {total_gap_candles}개)",
        f"- 총 중복 타임스탬프: {total_dup}건",
        f"- 판정: {'PASS — 결측/중복 없음' if total_gaps == 0 and total_dup == 0 else '확인 필요 — 위 표에서 결측/중복 발생 자산 확인'}",
    ]

    (PROJECT_ROOT / "reports" / "UNIVERSE_EXPANSION_LOG.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
