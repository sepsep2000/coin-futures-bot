# PROGRESS.md

## 2026-07-29 — 텔레그램 알림 피로 완화 (사용자 피드백: "너무 자주 옴, 쓸데없는거 안오게 해")

**배경**: WSL 유휴-정지 문제(전날 발견)로 봇이 다시 멈춰있는 걸 확인해 수동
재기동하던 중, 사용자가 텔레그램 알림이 너무 잦다고 피드백. 원인 조사 결과
두 가지 확인.

**원인 1 (상시 노이즈, 하루 96건)**: `scripts/healthcheck.py::check_telegram_reachable()`
가 15분마다 `telegram.send_message()`로 **실제 채팅 메시지**("[HEALTHCHECK]
텔레그램 발신 정상 확인")를 보내고 있었다 - 아무 문제가 없어도 하루 96번씩
쌓이는 순수 낭비. `src/notify/telegram.py`에 `check_reachable()` 신설 -
`bot.get_me()`만 호출해 토큰/네트워크는 검증하되 채팅 메시지는 전혀 안 보냄.
`check_telegram_reachable()`이 이걸 쓰도록 교체.

**원인 2 (장애 시 폭주, 실측 58건/7시간)**: `main()`이 실패를 발견할 때마다
매번 새 CRITICAL을 발신 - 같은 장애가 지속되는 동안 15분마다 거의 동일한
메시지가 반복됐다(전날 파케이 손상 사고 때 실측 58건). `_should_send_failure_alert()`
신설: 실패 항목 이름의 **집합**(detail 텍스트가 아님 - heartbeat_fresh의
경과시간처럼 매번 바뀌는 값 때문에 텍스트 기준 dedup은 무의미)을 dedup
키로 써서, 새 종류의 실패는 즉시 알리되 같은 실패가 지속되면
`ALERT_REMINDER_INTERVAL`(2시간)마다만 재알림. 상태는 `logs/healthcheck_alert_state.json`
에 저장, 회복 시 초기화(다음에 재발하면 다시 "새 실패"로 즉시 알림).

**영향 없는 것(그대로 유지)**: 실거래 이벤트(진입/청산/리밸런스), 킬스위치
발동, 일일요약 등 `send_entry_exit_notification`/`send_rebalance_notification`
계열은 손대지 않음 - 사용자가 원한 건 "쓸데없는" 반복/자동확인 알림 억제이지
실제 거래/장애 신호 자체를 줄이는 게 아니므로.

**테스트**: 신규 11개(telegram.check_reachable 3개: 정상/자격증명누락/네트워크
오류, healthcheck dedup 6개: 정상/경계/실패 + 시그니처변경시즉시알림/회복시
초기화/재발생시즉시알림, healthcheck telegram_reachable이 send_message를
안 부르는지 회귀 1개). Windows/WSL 양쪽 venv에서 `tests/ -x -q` 310/310 통과.

**미해결**: scheduler.py의 틱 실패(`_tick 실패`) CRITICAL도 같은 패턴으로
반복 가능성 있음(장애 지속 시 5회 연속실패마다 킬스위치→프로세스 재시작→
카운터 리셋→다시 5회 반복 사이클) - 이번 범위는 healthcheck.py로 한정,
필요시 별도 요청.

## 2026-07-28 — 클로드 코드 세션 유실 후 전면 재점검 + 파케이 캐시 손상 사고 대응

**배경**: 로컬 클로드 코드 앱이 강제종료되어 이전 세션 컨텍스트 유실. 재점검 결과
파일/커밋 자체는 전혀 유실되지 않았음(디스크와 세션 컨텍스트는 별개) — 다만
원격에 push 안 된 커밋 77개 + 미커밋 WIP 5개 파일이 로컬에만 있던 상태였음.

**조사 결과 (코드 변경 없이 순수 조사)**
- 봇 프로세스는 WSL crontab(`run_cycle.sh`, 15분마다, nohup+PID파일)으로 클로드 코드
  앱과 완전히 분리되어 독립 실행되는 구조가 맞음을 확인 — 크래시가 봇에 전파된
  증거 없음.
- crontab에서 `run_cycle.sh` 줄이 `# [2026-07-27T21:40Z EMERGENCY STOP - disabled
  pending parquet corruption root-cause fix]`로 **의도적으로 비활성화**되어 있었음
  (이전 세션이 아래 버그를 진단하고 재발 방지로 watchdog을 직접 끈 것으로 판단).
- 근본원인: `data/ohlcv/ETHUSDT-USDT_15m.parquet`가 실제로 손상되어 있었음(직접
  로드 재현: `OSError: Couldn't deserialize thrift`). 07-27 14:30~21:30 UTC 매 틱
  실패 → healthcheck가 15분마다 CRITICAL 텔레그램 발송(58건 누적, 사용자가 받은
  알림 폭주의 원인) → 21:40 watchdog 비활성화로 정지.
- 거래소 실측 재조회(`fetch_positions`/`fetch_open_orders`) 결과 열린 포지션·미체결
  주문 0건, DB와 일치 — 위험 노출 없음 확인. 잔고 $5,160.11 USDT.
- 이 사고는 `reports/G4_KILLSWITCH_INCIDENT_ANALYSIS.md`(07-27 킬스위치 사고)와는
  별개(그 사고의 수동 청산 이후 13:45 UTC 재진입한 ETH 롱의 스탑 배치가
  `OrderImmediatelyFillable`로 반복 실패한 것이 계기 — 이 포지션은 현재 거래소에
  없어 이후 정상 청산된 것으로 판단, 재구성 근거는 `orders` 테이블 타임스탬프).

**수정**: [src/data/feed.py](src/data/feed.py) `save_cache()`가 캐시 경로에 직접
`to_parquet`을 호출해서, 쓰기 도중 프로세스가 중단되면(강제종료/슬립/재시작 등)
parquet 푸터가 잘린 손상 파일이 남고 이후 영구히 복구 안 되는 구조적 결함이었음
— `.tmp` 파일에 먼저 쓰고 `Path.replace()`로 원자적 교체하도록 수정(쓰기 실패 시
기존 파일은 항상 이전 정상 상태 그대로 유지됨). 신규 테스트 2개(경계: 성공 후
임시파일 잔존 안 함, 실패: 쓰기 중 예외 발생해도 기존 캐시 손상 안 됨 — 사고
재현 회귀 테스트). `tests/ -x -q` 299/299 통과(기존 297 + 신규 2).

**데이터 복구**: 손상된 `ETHUSDT-USDT_15m.parquet` 삭제 후 live 엔드포인트에서
재수집 — 125,233행, 결측 캔들 0건, 2023-01-01~현재 정상 재구성 확인.

**미해결(당시 시점)**:
- crontab watchdog은 여전히 비활성화 상태로 유지함(재발 방지 검증 후 사용자
  승인 하에 재활성화 필요 — 임의로 켜지 않음).
- 어제(07-27) 킬스위치 사고 대응 코드(`scheduler.py` reduceOnly/실측기준 청산/
  None가드, `daily_report.py`)는 이번 커밋에 함께 반영.
- 원격(`origin/main`)에 77개+ 커밋 미푸시 상태 — 백업 목적 push는 사용자 승인
  대기 중.

## 2026-07-28 (이어서) — crontab 재활성화 중 WSL↔Windows 인터롭 실행 불안정 발견 → WSL 내부 완결형 실행 구조로 전환

**원격 push**: 위 5개 커밋 전부 `origin/main`에 반영 완료(`git log origin/main`
으로 확인).

**crontab 재활성화 1차 시도 → 실패, 새 문제 발견**: 비활성화 주석 제거 후
`run_cycle.sh` 수동 실행(PID 386) → **15분 넘게 CPU 시간 0초, heartbeat 갱신
0회, 파일 변화 0건** — 첫 틱(계좌 조회 이전 단계)조차 진행 안 됨. 같은 시작
시퀀스(`get_exchange`→`load_markets`→`recover_state`)를 **Windows에서 직접
실행**하면 5.1초에 정상 완료 — 파케이 버그와 무관한 별개 문제로 확인, 프로세스
kill + crontab 재비활성화(임의 재시도 안 함, 안전 상태로 롤백).

**`wsl --shutdown` 후 재검증 → 증상 악화(즉시 재현), 근본원인 특정**:
재기동 후 동일 절차로 재시도 → 이번엔 프로세스가 24초 만에 사망, `runner.log`에
`.venv/Scripts/python.exe: 1: MZ...` — **WSL이 Windows PE 실행파일(.exe)을
인터롭으로 넘기지 못하고 바이너리 내용을 셸 스크립트로 직접 실행하려다 깨짐**
(binfmt_misc 인터롭 핸들러가 그 시점에 준비 안 된 것으로 추정). 재현성 없이
매번 다르게 실패 — "WSL cron이 nohup으로 Windows 네이티브 python.exe를
백그라운드 실행"하는 구조 자체가 이 환경에서 신뢰할 수 없다고 판정, 재시도
대신 구조 전환 결정.

**구조 전환**: WSL(Ubuntu 22.04) 내부에 독립 Python 3.10 venv(`.venv-wsl`,
Windows용 기존 `.venv`와 별개) 신설, `requirements.txt` 전체 재설치
(ccxt 4.5.69/pandas 2.3.3/numpy 2.2.6/pyarrow 25.0.0 등). `tests/ -x -q`
299/299 통과(WSL 파이썬에서도 재확인). 거래소 접근성(`fetch_balance`/
`fetch_positions`) 3.3초 내 정상 확인. [scripts/run_cycle.sh](scripts/run_cycle.sh)
의 인터프리터 탐색 우선순위를 `.venv-wsl/bin/python` 최우선으로 변경, crontab의
`healthcheck.py`/`daily_report.py` 호출도 같은 경로로 교체 — Windows 실행파일
경계 자체를 없애 이 클래스의 실패가 구조적으로 불가능해짐.

**검증 결과**:
- 수동 실행(PID 383): 시작 13:42:27 → 첫 틱 완료 13:42:41 (14초), CPU 시간 정상 증가, "MZ" 에러 재현 안 됨
- crontab 경유 재기동(PID 521): cron이 13:45:01에 자동 시작 → 13:45:15 첫 틱 완료(14초) — 사람 개입 없이 정상 동작 확인
- healthcheck.py(13:45:16 실행): telegram_reachable/heartbeat_fresh/exchange_reachable/state_matches_exchange/disk_and_db_accessible **5개 항목 전부 PASS** (heartbeat_fresh: "마지막 틱 0:02:31 전 - 정상")
- 매 단계 전후 거래소 실측(`fetch_positions`/`fetch_open_orders`) 재확인 — 전 과정에서 포지션 0건, 미체결주문 0건, USDT 잔고 $5,160.11 유지(우발적 주문 없음)

**텔레그램 "정상 재개" 알림 발송 완료**(위 검증 전부 통과 후).

**미해결**:
- 기존 `.venv`(Windows)는 남겨둠 — 로컬에서 Windows 파이썬으로 직접 스크립트를
  돌릴 때(진단/조사용) 계속 씀. 운영(cron)은 이제 `.venv-wsl`만 사용.
  둘을 동시에 유지보수해야 하는 부담은 생김(향후 Windows 쪽을 완전히 걷어낼지는
  별도 논의 필요).
- WSL binfmt 인터롭이 정확히 "왜" 재부팅 직후 불안정한지(레이스 컨디션의 정확한
  트리거)는 규명하지 않음 — 구조 전환으로 그 원인 자체를 우회했으므로 추가 조사
  불필요 판단.

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

## 2026-07-24 — 옵션 3(자산 구성) 재검토 채택

사용자 승인한 옵션 3(자산 구성 변경)부터 시도. `config.yaml`/코드는 안 건드리고
자산 리스트만 필터링한 임시 스크립트로 동일 18윈도우 워크포워드 재실행:

| 구성 | OOS 거래수 | PF | MaxDD | 월간양수 | G2 | G3(MaxDD 95%ile) |
|---|---|---|---|---|---|---|
| BTC+ETH+SOL | 2,016 | 0.97 | 25.96% | 41.7% | FAIL | FAIL (37.7%) |
| ETH 단독 | 734 | 1.02 | 10.96% | 41.7% | FAIL | **PASS (19.3%)** |
| BTC+ETH | 1,435 | 0.98 | 19.89% | 33.3% | FAIL | FAIL (31.5%) |

**결론**: 자산을 줄일수록 MaxDD/몬테카를로가 확실히 개선(ETH 단독만 G3 통과) —
BTC/SOL이 엣지 없이 드로다운만 키우는 것으로 판단. 반면 PF(0.93~1.09)와 월간
양수 비율(33~42%)은 세 구성 모두 비슷하게 미달 — 이건 자산 선택이 아니라
진입/청산 로직 자체의 문제로 판단.

**결정**: `config.yaml: exchange.pairs`를 ETH 단독으로 확정. BTC/SOL 과거
데이터(parquet)는 보존. 다음으로 옵션 1(스탑 그레이스 피리어드) 구현 진행 —
사용자가 이번을 메커니즘 변경 마지막 시도로 지정: G2+G3 둘 다 PASS해야 계속
진행, 하나라도 FAIL이면 "15m Donchian 브레이크아웃 전략 패밀리 자체가 이
자산/기간에서 유의미한 엣지 없음"으로 최종 결론.

## 2026-07-24 — 옵션 1(스탑 그레이스 피리어드) 구현 + 최종 결론

**분석**: ETH 단독(현재 config, 확인봉 포함) 전체구간 백테스트에서 stop_loss
청산 629건(68.1%) 분포 추출 — 중앙값 18봉(4.5h), 4봉 이내 즉시 스탑 13.5%뿐.
그리드 스윕 없이 **N=4봉(1h)** 확정(근거: 최하위 구간만 겨냥 + 1h는 레짐
판별 주기와 자연스럽게 일치).

**구현**: `src/backtest/engine.py::_manage_trend_position`에 진입 후 N봉 스탑
체크 생략 로직 추가(부분청산/시간청산은 그대로 평가). `config.yaml:
trend.stop_grace_period_bars=4`. 신규 테스트 4개, 189/189 통과. 커밋 `379bbfa`.

**동일 워크포워드(ETH 단독, 18윈도우) 재실행 결과**:

| 지표 | 이전(ETH단독, 그레이스 없음) | 최종(그레이스 4봉) | 기준 |
|---|---|---|---|
| OOS 거래수 | 734 | 737 | ≥300 (통과) |
| PF | 1.02 | **1.07** | ≥1.3 (미달) |
| MaxDD | 10.96% | **10.40%** | ≤15% (통과) |
| 월간양수 | 41.7% | **50.0%** | ≥55% (미달) |
| G3 MaxDD 95%ile | 19.3%(PASS) | **18.25%(PASS)** | ≤20% |

**G2: FAIL / G3: PASS.**

**최종 판정 (사전 합의 기준 적용)**: G2+G3 중 하나(G2)가 여전히 FAIL이므로
그리드/그레이스 봉수를 더 흔들지 않고 여기서 중단. **15m Donchian
브레이크아웃 전략 패밀리는 ETH·2023~현재 구간에서 유의미한 엣지 없음으로
결론.** 4단계 전체 이력·근거는 `FINAL_REPORT.md` 참조.

**다음 단계**: 이 형태의 프로젝트 종료 여부, 또는 다른 전략 패밀리로
처음부터 재설계할지 사용자 논의/승인 필요. 승인 전 아무 것도 구현 안 함.
