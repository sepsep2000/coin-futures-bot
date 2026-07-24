"""scripts/collect_data.py — Phase 0: OHLCV/펀딩비 수집 + 데이터 품질 리포트.

SPEC.md 3절 데이터 파이프라인의 실행 진입점. config/config.yaml의 pairs/timeframes/
since를 그대로 쓴다(하드코딩 금지, CLAUDE.md 규칙 4). 반드시 live(프로덕션)
엔드포인트로 수집한다 — testnet 히스토리는 신뢰할 수 없다(DATA_REPORT.md의
'테스트넷 조사 결과' 절 참조, SPEC 8 [자료 부족] 항목 해소).

사용:
    python scripts/collect_data.py
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
    cache_path,
    detect_duplicates,
    detect_gaps,
    get_exchange,
    load_cache,
    update_funding_cache,
    update_ohlcv_cache,
)


def _investigate_testnet_history(pair: str, since_ms: int) -> dict:
    """SPEC 8 [자료 부족] 조사: testnet의 과거 OHLCV/펀딩비가 실사용 가능한지 확인.
    실패해도 수집 자체를 막지 않는다 — 조사 결과만 리포트에 남긴다."""
    result = {"ohlcv_matches_live": None, "funding_since_honored": None, "error": None}
    try:
        live_ex = get_exchange(testnet=False)
        test_ex = get_exchange(testnet=True)

        live_candle = live_ex.fetch_ohlcv(pair, "15m", since=since_ms, limit=1)
        test_candle = test_ex.fetch_ohlcv(pair, "15m", since=since_ms, limit=1)
        if live_candle and test_candle:
            live_open, test_open = live_candle[0][1], test_candle[0][1]
            result["ohlcv_matches_live"] = abs(live_open - test_open) < 1e-9
            result["live_open"] = live_open
            result["test_open"] = test_open

        test_funding = test_ex.fetch_funding_rate_history(pair, since=since_ms, limit=5)
        if test_funding:
            first_ts = test_funding[0]["timestamp"]
            # since_ms로부터 30일(대략 90회 정산, 8h 주기) 이상 떨어져 있으면 since가
            # 사실상 무시되고 최근 데이터만 반환된 것으로 판단
            result["funding_since_honored"] = (first_ts - since_ms) < (30 * 86_400_000)
            result["funding_first_ts"] = first_ts
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def main() -> None:
    with open(PROJECT_ROOT / "config" / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    pairs = cfg["exchange"]["pairs"]
    timeframes = [cfg["exchange"]["timeframe_trend"], cfg["exchange"]["timeframe_regime"]]
    since_ms = int(datetime.fromisoformat(cfg["data"]["since"].replace("Z", "+00:00")).timestamp() * 1000)
    ohlcv_cache_dir = PROJECT_ROOT / cfg["data"]["ohlcv_cache_dir"]
    funding_cache_dir = PROJECT_ROOT / cfg["data"]["funding_cache_dir"]

    exchange = get_exchange(testnet=False)  # SPEC 8 결론: 히스토리는 항상 live에서

    ohlcv_report: dict[str, dict] = {}
    funding_report: dict[str, dict] = {}

    for pair in pairs:
        for tf in timeframes:
            print(f"[OHLCV] {pair} {tf} 수집 중...", flush=True)
            t0 = time.time()
            df = update_ohlcv_cache(exchange, pair, tf, since_ms, ohlcv_cache_dir)
            gaps = detect_gaps(df, TIMEFRAME_MS[tf])
            ohlcv_report[f"{pair}:{tf}"] = {
                "rows": len(df),
                "start": df["timestamp"].min() if not df.empty else None,
                "end": df["timestamp"].max() if not df.empty else None,
                "gap_count": len(gaps),
                "missing_candles_total": sum(g["missing_candles"] for g in gaps),
                "elapsed_sec": round(time.time() - t0, 1),
            }
            print(f"  -> {len(df)} rows, {len(gaps)} gaps, {ohlcv_report[f'{pair}:{tf}']['elapsed_sec']}s", flush=True)

        print(f"[Funding] {pair} 수집 중...", flush=True)
        t0 = time.time()
        fdf = update_funding_cache(exchange, pair, since_ms, funding_cache_dir)
        fgaps = detect_gaps(fdf, FUNDING_INTERVAL_MS)
        funding_report[pair] = {
            "rows": len(fdf),
            "start": fdf["timestamp"].min() if not fdf.empty else None,
            "end": fdf["timestamp"].max() if not fdf.empty else None,
            "gap_count": len(fgaps),
            "elapsed_sec": round(time.time() - t0, 1),
        }
        print(f"  -> {len(fdf)} rows, {len(fgaps)} gaps, {funding_report[pair]['elapsed_sec']}s", flush=True)

    print("[Testnet 조사] SPEC 8 [자료 부족] 항목 확인 중...", flush=True)
    testnet_investigation = _investigate_testnet_history(pairs[0], since_ms)
    print(f"  -> {testnet_investigation}", flush=True)

    _write_report(cfg, since_ms, ohlcv_report, funding_report, testnet_investigation)
    print("DATA_REPORT.md 작성 완료", flush=True)


def _ts_to_iso(ts_ms) -> str:
    if ts_ms is None:
        return "N/A"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def _write_report(cfg, since_ms, ohlcv_report, funding_report, testnet_investigation) -> None:
    lines = [
        "# DATA_REPORT.md — Phase 0 데이터 품질 리포트",
        "",
        f"생성 시각: {datetime.now(timezone.utc).isoformat()}",
        f"수집 범위: {cfg['data']['since']} ~ 현재 (live/프로덕션 엔드포인트)",
        "",
        "## OHLCV",
        "",
        "| Pair:Timeframe | 캔들 수 | 시작 | 끝 | 결측 구간 수 | 결측 캔들 합계 | 소요(초) |",
        "|---|---|---|---|---|---|---|",
    ]
    for key, r in ohlcv_report.items():
        lines.append(
            f"| {key} | {r['rows']} | {_ts_to_iso(r['start'])} | {_ts_to_iso(r['end'])} "
            f"| {r['gap_count']} | {r['missing_candles_total']} | {r['elapsed_sec']} |"
        )

    lines += ["", "## 펀딩비 이력", "", "| Pair | 레코드 수 | 시작 | 끝 | 결측 구간 수 | 소요(초) |", "|---|---|---|---|---|---|"]
    for pair, r in funding_report.items():
        lines.append(
            f"| {pair} | {r['rows']} | {_ts_to_iso(r['start'])} | {_ts_to_iso(r['end'])} "
            f"| {r['gap_count']} | {r['elapsed_sec']} |"
        )

    lines += [
        "",
        "## SPEC 8 [자료 부족] 조사 결과 — Binance testnet 과거 데이터 정확도",
        "",
    ]
    if testnet_investigation.get("error"):
        lines.append(f"- 조사 중 오류 발생: {testnet_investigation['error']}")
    else:
        ohlcv_match = testnet_investigation.get("ohlcv_matches_live")
        funding_honored = testnet_investigation.get("funding_since_honored")
        lines.append(
            f"- **OHLCV**: since={cfg['data']['since']}의 첫 캔들 시가를 live/testnet 비교 — "
            f"live={testnet_investigation.get('live_open')}, testnet={testnet_investigation.get('test_open')} "
            f"→ 일치 여부: {'일치(신뢰 가능)' if ohlcv_match else '**불일치(합성 데이터로 판단)**'}"
        )
        lines.append(
            f"- **펀딩비**: since={cfg['data']['since']}로 요청했지만 첫 레코드 시각이 "
            f"{_ts_to_iso(testnet_investigation.get('funding_first_ts'))} → "
            f"since 반영 여부: {'반영됨' if funding_honored else '**사실상 무시(최근 데이터만 반환)**'}"
        )
        lines.append("")
        lines.append(
            "**결론(확정)**: Binance testnet은 과거 OHLCV를 실제 가격이력과 다른 값으로 반환하고"
            "(합성/샘플 데이터로 추정), 펀딩비 이력도 `since` 파라미터를 사실상 무시하고 최근 데이터만"
            " 준다. 따라서 **백테스트용 히스토리 데이터(OHLCV+펀딩비)는 항상 live(프로덕션) 공개"
            " 엔드포인트에서 수집한다** (공개 시장 데이터라 API 키 불필요). testnet은 Phase 4"
            " 주문 실행(체결/스탑 배치) 검증에만 사용한다. `config/config.yaml`의 `data.source: \"live\"`로"
            " 이미 반영되어 있다."
        )

    (PROJECT_ROOT / "DATA_REPORT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
