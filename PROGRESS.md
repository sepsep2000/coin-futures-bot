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
- ~~git 커밋 여부~~ → 커밋 완료 (`53e2f10`)
- SOL 유동성/스프레드 실측 검증(SPEC 8 두 번째 미해결 항목)은 아직 안 함 — Phase 1/2에서 필요 시 진행

## 2026-07-24 — Phase 1: 전략 순수함수 + 단위 테스트

**변경**
- `src/strategy/indicators.py`: ATR/ADX/RSI(Wilder 방식 직접 구현, 시드=첫 N개 단순평균 후 재귀식)·EMA·Donchian(직전 N봉, 자기참조 배제)·Bollinger·BB폭 퍼센타일·Chandelier stop
- `src/strategy/regime.py`: SPEC 2.1 레짐 판별(TREND/RANGE/NEUTRAL), 경계값 로직을 `classify_from_indicators()`로 분리해 지표 계산과 독립적으로 테스트
- `src/strategy/trend.py`: SPEC 2.2 Donchian 브레이크아웃 진입 시그널 + 트레일링(Chandelier)/시간청산 헬퍼
- `src/strategy/meanrev.py`: SPEC 2.3 BB+RSI 평균회귀 진입 시그널 + 시간청산/레짐전환청산 헬퍼
- `tests/test_indicators.py`(26)/`test_regime.py`(13)/`test_trend.py`(11)/`test_meanrev.py`(12): 골든케이스(단조추세→ADX/RSI 극값 등 손으로 검증 가능한 구성) + 레짐 경계값(ADX 25/20, BB폭 60/40) + Donchian/BB+RSI 진입조건 + 룩어헤드 방지(마지막 봉 제거해도 이전 값 불변) 전부 포함

**실행 중 발견/수정한 버그 1건**
- `adx()`가 가격이 전혀 안 움직이는 구간(TR=0)에서 0/0=NaN이 전파되던 문제 — 무추세(DX=0)로 명시 처리하되 웜업 구간의 NaN과는 구분되게 수정(마스크를 정확히 tr_smooth==0인 경우로 한정, NaN에는 영향 없음).

**테스트 결과**: 83/83 통과 (프로젝트 전체)

**SPEC 해석이 필요했던 지점 (임의 결정, 확인 요청)**
1. Donchian 채널은 "직전 N봉"(오늘 봉 제외) 기준으로 구현 — 오늘 고가를 포함하면 브레이크아웃이 자기참조가 되어 버림. SPEC 원문엔 이 배제가 명시돼 있지 않음.
2. 레짐(SPEC 2.1)의 "BB폭"은 std 배수를 명시하지 않아 관행값 2.0을 사용(평균회귀 SPEC 2.3의 BB(20,2.0)과 같은 배수). 레짐용으로 다른 배수를 의도하셨다면 config.yaml만 바꾸면 됨.
3. ADX/RSI는 Wilder 원조 방식(시드=단순평균, 이후 재귀)으로 구현 — 일부 TA 라이브러리(예: `ta` 패키지)는 EWM(alpha=1/N) 근사를 쓰는데, 이는 시드 방식이 달라 수치가 미세하게 갈린다(실측 확인). 수기 계산·Wilder 원문 정의와는 내가 구현한 쪽이 일치.
4. 포지션 생애주기(1.5R 부분청산 25%, 본전 이동, 트레일링 개시 시점 등)는 Phase 1 순수 시그널 함수 범위 밖 — SPEC 3절 구조상 src/backtest/engine.py(Phase 2)가 상태를 들고 있어야 하는 로직이라 이번 단계에선 재사용 가능한 순수 헬퍼(trailing_stop, time_exit_triggered)만 만들고 상태 머신 자체는 안 만들었음.

**미해결**
- 위 4개 확인 필요 (기본값으로 진행함, 문제 있으면 config.yaml 값만 바꾸면 되는 구조)
