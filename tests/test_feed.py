import pandas as pd
import pytest

from src.data.feed import (
    TIMEFRAME_MS,
    cache_path,
    detect_duplicates,
    detect_gaps,
    fetch_funding_rate_history_paginated,
    fetch_ohlcv_paginated,
    funding_rows_to_df,
    load_cache,
    merge_timeseries,
    ohlcv_rows_to_df,
    save_cache,
    update_funding_cache,
    update_ohlcv_cache,
)

INTERVAL_15M = TIMEFRAME_MS["15m"]


def _ohlcv_row(ts, price=100.0):
    return [ts, price, price + 1, price - 1, price, 10.0]


# --- 순수 로직 ---

def test_ohlcv_rows_to_df_basic():
    rows = [_ohlcv_row(0), _ohlcv_row(INTERVAL_15M)]
    df = ohlcv_rows_to_df(rows)
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df["timestamp"].dtype == "int64"


def test_funding_rows_to_df_basic():
    rows = [{"timestamp": 0, "fundingRate": 0.0001}, {"timestamp": 1000, "fundingRate": -0.0002}]
    df = funding_rows_to_df(rows)
    assert list(df.columns) == ["timestamp", "funding_rate"]
    assert df["funding_rate"].tolist() == [0.0001, -0.0002]


def test_merge_timeseries_new_overwrites_overlap():
    existing = ohlcv_rows_to_df([_ohlcv_row(0, price=100.0), _ohlcv_row(INTERVAL_15M, price=101.0)])
    new = ohlcv_rows_to_df([_ohlcv_row(INTERVAL_15M, price=999.0), _ohlcv_row(2 * INTERVAL_15M, price=102.0)])
    merged = merge_timeseries(existing, new)
    assert len(merged) == 3
    assert merged.loc[merged["timestamp"] == INTERVAL_15M, "close"].iloc[0] == 999.0  # 새 값이 이김
    assert merged["timestamp"].is_monotonic_increasing


def test_merge_timeseries_handles_empty_existing():
    new = ohlcv_rows_to_df([_ohlcv_row(0)])
    merged = merge_timeseries(None, new)
    assert len(merged) == 1


def test_merge_timeseries_handles_empty_new():
    existing = ohlcv_rows_to_df([_ohlcv_row(0)])
    merged = merge_timeseries(existing, ohlcv_rows_to_df([]))
    assert len(merged) == 1


def test_detect_duplicates_counts_correctly():
    df = ohlcv_rows_to_df([_ohlcv_row(0), _ohlcv_row(0), _ohlcv_row(INTERVAL_15M)])
    assert detect_duplicates(df) == 1


def test_detect_duplicates_zero_when_none():
    df = ohlcv_rows_to_df([_ohlcv_row(0), _ohlcv_row(INTERVAL_15M)])
    assert detect_duplicates(df) == 0


def test_detect_gaps_finds_missing_candles():
    # 0, 15m, (skip 30m, 45m), 60m -> 60m-15m = 45m = 3*interval -> gap에 missing=2
    df = ohlcv_rows_to_df([_ohlcv_row(0), _ohlcv_row(INTERVAL_15M), _ohlcv_row(4 * INTERVAL_15M)])
    gaps = detect_gaps(df, INTERVAL_15M)
    assert len(gaps) == 1
    assert gaps[0]["missing_candles"] == 2


def test_detect_gaps_empty_for_contiguous_data():
    df = ohlcv_rows_to_df([_ohlcv_row(i * INTERVAL_15M) for i in range(5)])
    assert detect_gaps(df, INTERVAL_15M) == []


def test_detect_gaps_requires_at_least_two_rows():
    assert detect_gaps(ohlcv_rows_to_df([_ohlcv_row(0)]), INTERVAL_15M) == []
    assert detect_gaps(ohlcv_rows_to_df([]), INTERVAL_15M) == []


def test_detect_gaps_ignores_millisecond_jitter_with_zero_missing_candles():
    """실측 회귀 방지: 펀딩비 정산 시각처럼 간격이 정확히 interval_ms가 아니라
    몇 ms 어긋나는 경우(diff=interval_ms+1) missing_candles는 0이므로 갭이 아니다
    (2026-07-24 실증: BTC 펀딩비 3,902건 중 974건이 이 패턴으로 오탐됐었다)."""
    df = ohlcv_rows_to_df([_ohlcv_row(0), _ohlcv_row(INTERVAL_15M + 1)])
    assert detect_gaps(df, INTERVAL_15M) == []


# --- 캐시 저장/로드 (parquet round trip) ---

def test_cache_roundtrip(tmp_path):
    path = cache_path(tmp_path, "BTC/USDT:USDT", "15m")
    assert path.name == "BTCUSDT-USDT_15m.parquet"
    df = ohlcv_rows_to_df([_ohlcv_row(0), _ohlcv_row(INTERVAL_15M)])
    save_cache(df, path)
    loaded = load_cache(path)
    pd.testing.assert_frame_equal(loaded, df)


def test_load_cache_returns_none_when_missing(tmp_path):
    assert load_cache(tmp_path / "does_not_exist.parquet") is None


# --- 페이지네이션 (fake exchange, 네트워크 없음) ---

class _FakeExchange:
    def __init__(self, ohlcv_batches=None, funding_batches=None, now_ms=10**15):
        self._ohlcv_batches = ohlcv_batches or []
        self._funding_batches = funding_batches or []
        self._now_ms = now_ms
        self.ohlcv_calls = 0
        self.funding_calls = 0

    def milliseconds(self):
        return self._now_ms

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        batch = self._ohlcv_batches[self.ohlcv_calls] if self.ohlcv_calls < len(self._ohlcv_batches) else []
        self.ohlcv_calls += 1
        return batch

    def fetch_funding_rate_history(self, symbol, since=None, limit=None):
        batch = self._funding_batches[self.funding_calls] if self.funding_calls < len(self._funding_batches) else []
        self.funding_calls += 1
        return batch


def test_fetch_ohlcv_paginated_keeps_paginating_past_short_batch():
    """실측 회귀 방지: 거래소가 limit(3)보다 적게 반환해도(=Binance klines는 문서상
    max 1500이어도 실제로 1000까지만 준다, 2026-07-24 실증) 그게 '마지막 페이지'라는
    뜻은 아니다 — 빈 배치를 받을 때까지 계속 다음 페이지를 요청해야 한다."""
    batch1 = [_ohlcv_row(i * INTERVAL_15M) for i in range(3)]        # len==limit(3)
    batch2 = [_ohlcv_row(i * INTERVAL_15M) for i in range(3, 5)]     # len(2)<limit(3)이어도 계속
    batch3 = [_ohlcv_row(i * INTERVAL_15M) for i in range(5, 6)]     # 마지막 남은 1개
    ex = _FakeExchange(ohlcv_batches=[batch1, batch2, batch3])
    df = fetch_ohlcv_paginated(ex, "BTC/USDT:USDT", "15m", since_ms=0,
                                until_ms=20 * INTERVAL_15M, limit=3)
    assert len(df) == 6  # 세 배치 전부 반영됨 (짧은 batch2에서 멈추지 않음)
    assert ex.ohlcv_calls == 3


def test_fetch_ohlcv_paginated_stops_at_until_ms():
    batch1 = [_ohlcv_row(0), _ohlcv_row(INTERVAL_15M)]  # 가득 찬 배치라도
    ex = _FakeExchange(ohlcv_batches=[batch1, batch1])
    df = fetch_ohlcv_paginated(ex, "BTC/USDT:USDT", "15m", since_ms=0, until_ms=INTERVAL_15M + 1, limit=2)
    assert ex.ohlcv_calls == 1  # cursor(2*interval)가 until_ms를 넘어서 2번째 호출 없음


def test_fetch_ohlcv_paginated_breaks_on_no_progress():
    stalled_batch = [_ohlcv_row(0)]  # since_ms=0으로 요청했는데 last_ts(0) <= cursor(0)
    ex = _FakeExchange(ohlcv_batches=[stalled_batch, stalled_batch])
    df = fetch_ohlcv_paginated(ex, "BTC/USDT:USDT", "15m", since_ms=0, limit=1)
    assert ex.ohlcv_calls == 1  # 무한루프 없이 1번만 호출하고 중단


def test_fetch_ohlcv_paginated_empty_batch_stops_immediately():
    ex = _FakeExchange(ohlcv_batches=[[]])
    df = fetch_ohlcv_paginated(ex, "BTC/USDT:USDT", "15m", since_ms=0, limit=2)
    assert len(df) == 0
    assert ex.ohlcv_calls == 1


def test_fetch_funding_rate_history_paginated_basic():
    batch1 = [{"timestamp": 0, "fundingRate": 0.0001}, {"timestamp": 1000, "fundingRate": 0.0002}]
    batch2 = [{"timestamp": 2000, "fundingRate": 0.0003}]
    ex = _FakeExchange(funding_batches=[batch1, batch2])
    df = fetch_funding_rate_history_paginated(ex, "BTC/USDT:USDT", since_ms=0, limit=2)
    assert len(df) == 3
    assert df["funding_rate"].tolist() == [0.0001, 0.0002, 0.0003]


# --- 증분 캐시 업데이트 ---

def test_update_ohlcv_cache_creates_new_cache(tmp_path):
    ex = _FakeExchange(ohlcv_batches=[[_ohlcv_row(0), _ohlcv_row(INTERVAL_15M)]])
    df = update_ohlcv_cache(ex, "BTC/USDT:USDT", "15m", since_ms=0, cache_dir=tmp_path)
    assert len(df) == 2
    assert load_cache(cache_path(tmp_path, "BTC/USDT:USDT", "15m")) is not None


def test_update_ohlcv_cache_appends_incrementally(tmp_path):
    ex1 = _FakeExchange(ohlcv_batches=[[_ohlcv_row(0), _ohlcv_row(INTERVAL_15M)]])
    update_ohlcv_cache(ex1, "BTC/USDT:USDT", "15m", since_ms=0, cache_dir=tmp_path)

    # 두 번째 실행: 마지막 캔들(INTERVAL_15M) 하나 겹치게 재조회 + 새 캔들 1개 추가
    ex2 = _FakeExchange(ohlcv_batches=[[_ohlcv_row(INTERVAL_15M), _ohlcv_row(2 * INTERVAL_15M)]])
    df = update_ohlcv_cache(ex2, "BTC/USDT:USDT", "15m", since_ms=0, cache_dir=tmp_path)

    assert len(df) == 3  # 중복 없이 병합됨
    assert df["timestamp"].tolist() == [0, INTERVAL_15M, 2 * INTERVAL_15M]


def test_update_funding_cache_appends_incrementally(tmp_path):
    ex1 = _FakeExchange(funding_batches=[[{"timestamp": 0, "fundingRate": 0.0001}]])
    update_funding_cache(ex1, "BTC/USDT:USDT", since_ms=0, cache_dir=tmp_path)

    ex2 = _FakeExchange(funding_batches=[[{"timestamp": 1000, "fundingRate": 0.0002}]])
    df = update_funding_cache(ex2, "BTC/USDT:USDT", since_ms=0, cache_dir=tmp_path)

    assert len(df) == 2
    assert df["timestamp"].tolist() == [0, 1000]
