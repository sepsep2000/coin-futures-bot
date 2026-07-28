"""src/data/feed.py — Binance USDT-M OHLCV/펀딩비 수집·캐시 (SPEC.md 3절, Phase 0).

exchange I/O(네트워크 호출)와 순수 로직(병합/중복제거/갭 탐지)을 분리한다 —
CLAUDE.md 규칙 2(전략 로직은 순수함수)와 같은 원칙을 데이터 계층에도 적용해
네트워크 없이 테스트 가능하게 한다.

★ SPEC 8 조사 결과 (Phase 0): Binance testnet의 과거 OHLCV는 실제 가격이력과
다른 합성 데이터이고(같은 since로 live와 비교 시 값이 다름), 펀딩비 이력도
`since`를 사실상 무시하고 최근 수일치만 반환한다. 따라서 백테스트용 히스토리는
반드시 live(프로덕션, 공개 엔드포인트라 API 키 불필요) 데이터로 수집한다.
testnet은 Phase 4 주문 실행 검증에만 쓴다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import ccxt
import pandas as pd

OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
FUNDING_COLUMNS = ["timestamp", "funding_rate"]

# ccxt unified timeframe -> 밀리초 (갭 탐지 기준 간격)
TIMEFRAME_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

# 펀딩비 정산 주기(SPEC 1: 8h) — funding 갭 탐지 기준. 상장 초기 등은 실제로
# 더 잦을 수 있어(1h) 완전히 정확하진 않지만, 큰 결측 구간을 잡아내는 데는 충분하다.
FUNDING_INTERVAL_MS = 8 * 60 * 60 * 1000


def get_exchange(testnet: bool = False) -> ccxt.Exchange:
    """SPEC 8 조사 결과 — 과거 데이터 수집(collect_*)은 절대 testnet=True로 호출하지
    않는다. testnet은 Phase 4 라이브 주문 실행 전용."""
    exchange = ccxt.binanceusdm({"enableRateLimit": True})
    if testnet:
        exchange.set_sandbox_mode(True)
    return exchange


# ---------------------------------------------------------------------------
# 순수 로직 (네트워크 없음, 단위 테스트 대상)
# ---------------------------------------------------------------------------

def ohlcv_rows_to_df(rows: list[list]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=OHLCV_COLUMNS)
    return df.astype({"timestamp": "int64"})


def funding_rows_to_df(rows: list[dict]) -> pd.DataFrame:
    data = [{"timestamp": int(r["timestamp"]), "funding_rate": float(r["fundingRate"])} for r in rows]
    df = pd.DataFrame(data, columns=FUNDING_COLUMNS)
    return df.astype({"timestamp": "int64"})


def merge_timeseries(existing: Optional[pd.DataFrame], new: pd.DataFrame) -> pd.DataFrame:
    """timestamp 기준 병합. 겹치는 구간은 새 데이터로 덮어쓴다(재조회분이 더 정확할
    수 있음 — 거래소가 사후에 캔들을 정정하는 경우가 있다), 정렬 후 반환."""
    if existing is None or existing.empty:
        combined = new
    elif new.empty:
        combined = existing
    else:
        combined = pd.concat([existing, new], ignore_index=True)
    if combined.empty:
        return combined
    combined = combined.drop_duplicates(subset="timestamp", keep="last")
    combined = combined.sort_values("timestamp").reset_index(drop=True)
    return combined


def detect_duplicates(df: pd.DataFrame) -> int:
    """merge_timeseries 이전의 raw 데이터에 대해서만 의미 있다 — merge 이후엔
    항상 0이어야 한다(중복 제거됨). 데이터 품질 리포트의 raw 진단용."""
    if df.empty:
        return 0
    return int(df["timestamp"].duplicated().sum())


def detect_gaps(df: pd.DataFrame, interval_ms: int) -> list[dict]:
    """timestamp 간격이 interval_ms보다 크면 결측 구간으로 기록. df는 정렬·중복제거
    되어 있다고 가정(merge_timeseries 결과를 넘길 것).

    ★ `missing_candles`가 0인 항목은 갭이 아니라 서버 타임스탬프의 밀리초 단위
    지터다 — 예: 펀딩비 정산 시각이 정확히 8h(28,800,000ms)가 아니라 몇 ms
    어긋나는 경우가 실제로 매우 흔하다(2026-07-24 실증: BTC 펀딩비 3,902건 중
    974건이 `diff > interval_ms`였지만 전부 missing_candles=0, 즉 진짜 결측은
    0건). 이런 항목까지 갭으로 세면 정상 데이터를 결측으로 오判정하게 되므로
    `missing_candles >= 1`인 것만 실제 갭으로 취급한다.
    """
    if len(df) < 2:
        return []
    ts = df["timestamp"].to_numpy()
    gaps = []
    for i in range(1, len(ts)):
        diff = int(ts[i] - ts[i - 1])
        missing = diff // interval_ms - 1
        if missing >= 1:
            gaps.append({
                "after_ts": int(ts[i - 1]), "before_ts": int(ts[i]),
                "gap_ms": diff, "missing_candles": int(missing),
            })
    return gaps


def cache_path(cache_dir: Path, symbol: str, suffix: str) -> Path:
    safe_symbol = symbol.replace("/", "").replace(":", "-")
    return cache_dir / f"{safe_symbol}_{suffix}.parquet"


def load_cache(path: Path) -> Optional[pd.DataFrame]:
    if path.exists():
        return pd.read_parquet(path)
    return None


def save_cache(df: pd.DataFrame, path: Path) -> None:
    """★ 2026-07-28 사고 대응: 임시파일에 쓴 뒤 원자적으로 교체한다 — 기존에는
    `path`에 직접 `to_parquet`을 호출해서, 쓰기 도중 프로세스가 중단되면(강제종료,
    슬립, 재시작 등) 파일 끝의 parquet 푸터가 잘려 손상된 채로 남았다(실측:
    data/ohlcv/ETHUSDT-USDT_15m.parquet가 이 방식으로 손상되어 라이브 틱이 2026-07-27
    14:30부터 7시간 넘게 매번 실패, 결국 watchdog까지 정지됨). 쓰기가 중간에 실패해도
    `path`는 항상 이전(정상) 상태 아니면 새(정상) 상태 둘 중 하나만 갖는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp_path, index=False)
    tmp_path.replace(path)


# ---------------------------------------------------------------------------
# exchange I/O (네트워크 호출)
# ---------------------------------------------------------------------------

def fetch_ohlcv_paginated(
    exchange: ccxt.Exchange, symbol: str, timeframe: str, since_ms: int,
    until_ms: Optional[int] = None, limit: int = 1500,
) -> pd.DataFrame:
    """★ `len(batch) < limit`을 '마지막 페이지' 신호로 쓰지 않는다 — 실측 결과
    Binance USDM klines는 limit=1500을 요청해도 실제로는 1000개까지만 반환한다
    (문서상 max 1500이지만 응답은 항상 1000 이하). 이 휴리스틱을 쓰면 매 페이지가
    '짧아서' 첫 페이지에서 조기 종료해버린다(2026-07-24 실증, 3.5년치 요청했는데
    1000개=약 10일치만 받고 멈춘 버그). 종료 조건은 오직 빈 배치 또는 커서 진행
    없음뿐이다 — 대신 `until_ms` 도달로 자연 종료된다."""
    interval = TIMEFRAME_MS[timeframe]
    until_ms = until_ms if until_ms is not None else exchange.milliseconds()
    all_rows: list[list] = []
    cursor = since_ms
    while cursor < until_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=limit)
        if not batch:
            break
        all_rows.extend(batch)
        last_ts = batch[-1][0]
        if last_ts <= cursor:  # 진행 안 되면 무한루프 방지
            break
        cursor = last_ts + interval
    return ohlcv_rows_to_df(all_rows)


def fetch_funding_rate_history_paginated(
    exchange: ccxt.Exchange, symbol: str, since_ms: int,
    until_ms: Optional[int] = None, limit: int = 1000,
) -> pd.DataFrame:
    """fetch_ohlcv_paginated와 동일한 이유로 `len(batch) < limit` 조기종료 휴리스틱을
    쓰지 않는다 — 이 엔드포인트는 실측상 limit을 그대로 지키지만(1000 요청 시 1000
    반환), 다른 거래소/버전에서 재발할 수 있는 클래스의 버그라 방어적으로 통일한다."""
    until_ms = until_ms if until_ms is not None else exchange.milliseconds()
    all_rows: list[dict] = []
    cursor = since_ms
    while cursor < until_ms:
        batch = exchange.fetch_funding_rate_history(symbol, since=cursor, limit=limit)
        if not batch:
            break
        all_rows.extend(batch)
        last_ts = batch[-1]["timestamp"]
        if last_ts <= cursor:
            break
        cursor = last_ts + 1
    return funding_rows_to_df(all_rows)


def update_ohlcv_cache(
    exchange: ccxt.Exchange, symbol: str, timeframe: str, since_ms: int, cache_dir: Path,
) -> pd.DataFrame:
    """증분 업데이트: 캐시가 있으면 마지막 캔들 하나 전부터 다시 받아 겹치게 채운다
    (거래소가 최신 미확정 캔들을 사후 정정하는 경우 대비)."""
    path = cache_path(cache_dir, symbol, timeframe)
    existing = load_cache(path)
    fetch_since = since_ms
    if existing is not None and not existing.empty:
        fetch_since = int(existing["timestamp"].max()) - TIMEFRAME_MS[timeframe]
    new_df = fetch_ohlcv_paginated(exchange, symbol, timeframe, fetch_since)
    merged = merge_timeseries(existing, new_df)
    save_cache(merged, path)
    return merged


def update_funding_cache(
    exchange: ccxt.Exchange, symbol: str, since_ms: int, cache_dir: Path,
) -> pd.DataFrame:
    path = cache_path(cache_dir, symbol, "funding")
    existing = load_cache(path)
    fetch_since = since_ms
    if existing is not None and not existing.empty:
        fetch_since = int(existing["timestamp"].max()) + 1
    new_df = fetch_funding_rate_history_paginated(exchange, symbol, fetch_since)
    merged = merge_timeseries(existing, new_df)
    save_cache(merged, path)
    return merged
