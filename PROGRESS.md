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

## 2026-07-24 — Phase 2: 백테스트 엔진 + 게이트 G1

**변경**
- `src/risk.py`: 사이징(리스크%/스탑거리, 레버리지 캡), 일일손실 킬스위치, 포트폴리오 한도(동시3/페어당1), 스탑존재확인/연속에러 킬스위치(라이브용 헬퍼)
- `src/backtest/cost_model.py`: 수수료/슬리피지/펀딩비/체결가 근사 순수함수
- `src/backtest/engine.py`: 이벤트드리븐(15m 기준), 레짐전환(TREND/RANGE/NEUTRAL), 포지션 생애주기(부분청산 25%@1.5R+본전이동+Chandelier트레일/시간청산/레짐전환청산), 멀티심볼 포트폴리오(동시3·페어당1·일일킬스위치), 펀딩비 정산 반영
- `src/backtest/report.py`: PF/Sharpe(연환산)/MaxDD/승률/평균·중앙값R/월별수익률 + report.html(인라인 SVG equity curve)
- `scripts/run_backtest.py`: 전체구간 실행 + G1 자동테스트 + BACKTEST_BASELINE.md 생성
- 신규 테스트 79개(`test_risk`18/`test_cost_model`13/`test_engine`16/`test_engine_integration`3/`test_report`17): 전체 162개 통과

**실행 중 발견/수정한 버그 1건 (중요)**
- 레버리지 상한(SPEC: 최대 2x)을 포지션 사이징에 반영 안 함 — 리스크% 기준 사이징만 쓰면 저변동기(스탑 좁음)에 notional이 계좌의 2.8배까지 치솟는 걸 실측 확인. `risk.position_size()`에 `max_leverage` 캡 추가, 레버리지 초과분은 리스크 기준과 레버리지 기준 중 작은 쪽으로 캡핑. 캡 적용 후 결과는 소폭만 개선(MaxDD 77.2%→73.3%) — 성과 부진이 이 버그 하나 때문은 아니었음.

**G1 판정: PASS**
- 룩어헤드 방지: PASS (데이터 절반 지점에서 자르고 재실행해도 그 이전 트레이드 완전 동일)
- 수수료 0 vs 실비용: 0비용 총손익 +$63,562 vs 실비용 총손익 -$6,881 (비용 총액 $70,443) — 비용이 성과를 갉아먹기만 함(정상)

**전체 성과 (2023-01~현재, BTC+ETH+SOL 합산, 실비용, 초기자본 $10,000 플레이스홀더)**
- 거래수 4,316 / 승률 51.2% / PF 0.92 / Sharpe(연환산) -1.22 / MaxDD 73.3% / 총손익 -$6,881 (최종자본 $3,119)
- 페어별: BTC PF 0.85(-$4,340), ETH PF 1.03(+$844), SOL PF 0.88(-$3,384)

**결과를 있는 그대로 보고한다 (파라미터 조정 안 함)**: G1(무결성)은 통과했지만 전략 자체 성과는 전체 구간(인샘플) 기준으로도 이미 G2 기준(PF≥1.3, MaxDD≤15%)에 크게 못 미친다. SPEC 4절 원칙("G2/G3 미달 시 파라미터 최적화 아니라 전략 기각 후 재설계")을 적용할지, 그래도 워크포워드(Phase 3)로 정식 OOS 판정까지 볼지는 사용자 결정 필요.

**미해결**
- 계좌 규모($10,000)는 SPEC.md에 명시 안 돼 있어 플레이스홀더 — 확인 필요(단, PF/Sharpe/MaxDD%는 이 값에 불변)
- Phase 3(워크포워드+몬테카를로) 진행 여부 확인 필요 — 인샘플 성과가 이미 나쁨

## 2026-07-24 — Phase 2 결과 기반 재설계 (SPEC 4절 전략 기각)

**진단**: 트레이드 단위로 재분해(청산사유/전략별/페어별/비용구조/보유기간/레짐시간분포). 근거 5가지:
1. 68.3%가 stop_loss 청산, 46.4%가 R≈-1 — 스탑이 노이즈에 비해 타이트
2. 트렌드 중앙값 보유 11봉(2.75h) — 저변동 필터만으론 휩쏘를 못 거름
3. meanrev PF 0.85(BTC만 PF 0.67, -$3,408)로 전략 조합 중 최악, 레짐전환청산은 8건(0.2%)뿐 — RANGE 안에서 이미 지고 있음
4. 같은 트레이드 시퀀스 기준 비용반영전 +$8,146 추정 → 비용($15,027, 트레이드당 $3.48)이 흑자를 -$6,881 적자로 뒤집음
5. NEUTRAL이 시간의 ~50% — 레짐 판별 반응이 느림(구조적 배경 요인)

**결정 (SPEC 4절 "파라미터 최적화 아니라 전략 기각 후 재설계")**:
- **meanrev 기각** — `config.yaml: active_strategies: ["trend"]`로 제외. 코드(`src/strategy/meanrev.py`)·테스트는 보존(향후 다른 진입조건으로 재설계 가능성 열어둠).
- **trend에 진입 확인봉(후보 A) 추가** — `trend.require_confirmation_bar: true`. 돌파봉 다음 1봉이 같은 채널 레벨 위/아래에서 마감해야만 진입.
- 후보 B(ATR/RSI 임계값 상향)는 보류 — 전체구간 숫자로 임계값을 고르면 과최적화이므로, 제안만 해두고 미구현.
- ETH/SOL 등 자산 구성은 이번 단계에서 변경 안 함.

**변경 파일**: `src/strategy/trend.py`(확인봉 로직), `src/backtest/engine.py`(active_strategies 게이팅), `config/config.yaml`, `SPEC.md`(변경이력+2.2/2.3 주석), `tests/test_trend.py`(+5)/`tests/test_engine_integration.py`(+1)

**테스트 결과**: 168/168 통과 (신규 6개)

**의도적으로 안 한 것**: 이 두 변경(meanrev 제외 + 확인봉)의 효과를 전체구간 재백테스트로 확인하지 않음 — 사용자 지시대로 Phase 3 워크포워드 학습구간에서만 검증할 예정. 지금 전체구간으로 "잘 나오나" 확인하면 봤던 데이터에 맞춰 고르는 것이라 과최적화가 됨.

**다음 단계**: Phase 3(워크포워드+몬테카를로, G2·G3) 진행 여부 확인 필요.

## 2026-07-24 — Phase 3: 워크포워드 + 몬테카를로 (G2·G3)

**변경**: `src/backtest/walkforward.py`(윈도우 생성, 학습구간 전용 그리드 탐색,
OOS 스티칭, G2/G3 판정), `scripts/run_walkforward.py`(전체 실행+리포트). 신규
테스트 32개(`test_walkforward.py` 15 + `test_walkforward_integration.py` 2, 나머지는
report/engine 재사용) — 185/185 통과.

**실행 중 발견/수정한 버그 1건**: `generate_windows`가 tz-naive Timestamp를 만들어서
tz-aware(UTC) 데이터와 경계 비교 시 `TypeError` — `_to_utc()` 헬퍼로 통일해 수정.

**실행**: 2023-01~현재, BTC/ETH/SOL, 학습 6개월/검증 2개월 롤링 18윈도우,
그리드 donchian_period×stop_atr_mult 9조합(윈도우당 학습구간에서만 탐색,
require_confirmation_bar는 이미 결정된 설계라 그리드 아님·고정 True). 소요 4,703초(≈78분).

**G2 판정: FAIL**
- OOS 거래수 2,016 (기준 ≥300, 통과)
- PF 0.97 (기준 ≥1.3, 미달)
- MaxDD 25.96% (기준 ≤15%, 미달)
- 월간 양수 비율 41.7% (기준 ≥55%, 미달)

**G3 판정: FAIL** — 몬테카를로(OOS 순서 셔플 1,000회) MaxDD 95%ile 37.7% (기준 ≤20%, 미달)

**분해**: 윈도우별 OOS 성과가 매우 들쭉날쭉(예: window7 PF 1.82 vs window8 PF 0.66) —
학습구간 최적 파라미터가 검증구간 성과를 안정적으로 예측 못 함(전형적 비강건성 신호).
그리드가 거의 항상 stop_atr_mult=2.5(그리드 최대값)를 선택 — 스탑을 넓힐수록 학습구간
성과가 좋아지는 경향이 이미 그리드 자체에서 드러남(가설 1과 일치). 페어별: ETH만
흑자(PF 1.12, +$1,527), BTC(PF 0.95, -$734)·SOL(PF 0.87, -$1,923) 적자. meanrev
트레이드 0건 확인(게이팅 정상 동작).

**결정 필요 (SPEC 4절: "G2/G3 미달 시 파라미터 재탐색 금지 — 실패 원인 분해 후
전략 수정 옵션 2~3개 제안, 승인 전 구현 금지")**: 사용자에게 옵션 제시, 승인 대기 중.
그리드/파라미터 재탐색으로 이 결과를 다시 돌리지 않음.

**미해결**: 전략 재설계 방향 승인 대기 (재승인 후에만 다음 구현 진행).
