# PROGRESS.md

## 2026-07-24 — Phase 0: 스캐폴딩 + 데이터 파이프라인

**변경**
- 프로젝트 스캐폴딩 생성 (SPEC.md 3절 구조): `config/`, `src/data/`, `tests/`, `scripts/`, venv + requirements.txt
- `config/config.yaml`: SPEC 1/2 전체 파라미터 반영 (레짐/트렌드/평균회귀/게이트/워크포워드), `mode: testnet` 기본값
- `src/data/feed.py`: Binance USDT-M(`binanceusdm`) OHLCV(15m/1h) + 펀딩비 이력 수집, parquet 캐시, 증분 업데이트. exchange I/O와 순수 로직(병합/중복제거/갭탐지) 분리
- `scripts/collect_data.py`: 수집 실행 + `DATA_REPORT.md` 생성 진입점
- `tests/test_feed.py`: 21개 (순수 로직 + 페이지네이션 + 캐시 round-trip, 네트워크 미사용)

**실행 중 발견/수정한 버그 2건** (실측 없이 코드만 봤으면 못 잡았을 것들)
1. Binance klines가 `limit=1500` 요청해도 실제로는 1000개까지만 반환 — "batch가 limit보다 짧으면 마지막 페이지"라는 휴리스틱이 첫 페이지(약 10일치)에서 조기 종료시킴. 휴리스틱 제거, `until_ms` 도달로만 종료하도록 수정.
2. 펀딩비 정산 시각이 정확히 8h 간격이 아니라 ms 단위로 흔들림 — `detect_gaps`가 `missing_candles==0`인 경우까지 갭으로 오탐(3,902건 중 974건 오탐). `missing_candles>=1`만 갭으로 세도록 수정.

**테스트 결과**: 21/21 통과

**실제 수집 결과** (`DATA_REPORT.md` 참조)
- BTC/ETH/SOL, 15m(124,838개)·1h(31,210개), 2023-01-01~2026-07-24, 갭 0건
- 펀딩비 3,902건씩, 갭 0건

**SPEC 8 [자료 부족] 해소**: Binance testnet의 과거 OHLCV는 live와 다른 값(합성 데이터로 판단), 펀딩비 이력도 `since`를 사실상 무시하고 최근 데이터만 반환함을 실측 확인. → 백테스트 히스토리는 항상 live 엔드포인트에서 수집(`config.yaml: data.source: "live"`), testnet은 Phase 4 주문 실행 검증 전용으로 확정.

**미해결**
- git 커밋 여부 사용자 확인 대기 중 (아직 커밋 안 함)
- SOL 유동성/스프레드 실측 검증(SPEC 8 두 번째 미해결 항목)은 아직 안 함 — Phase 1/2에서 필요 시 진행
