# PROMPTS.md — Claude Code 단계별 실행 프롬프트

사용법: 프로젝트 폴더에 SPEC.md, CLAUDE.md를 넣고 Claude Code를 연 뒤, 아래 블록을 **순서대로 하나씩** 붙여넣는다. 각 Phase의 게이트 결과를 확인하고 다음으로 넘어간다.

---

## ① Phase 0 — 스캐폴딩 + 데이터 파이프라인

```
CLAUDE.md와 SPEC.md를 읽고 시작해.

Phase 0 작업:
1. SPEC.md 3절 구조대로 프로젝트 스캐폴딩 생성 (requirements.txt 포함: ccxt, pandas, numpy, pyyaml, python-telegram-bot, pytest, pyarrow)
2. src/data/feed.py 구현: Binance USDT-M에서 BTCUSDT/ETHUSDT/SOLUSDT의 15m·1h OHLCV를 2023-01-01부터 수집해 data/ 에 parquet 캐시. 증분 업데이트 지원. 펀딩비 히스토리도 동일하게.
3. 수집 후 데이터 품질 리포트 생성: 결측 봉, 중복, 타임스탬프 정합성. SPEC 8절의 [자료 부족] 항목(테스트넷 펀딩비) 조사 결과 포함해서 DATA_REPORT.md로 저장.
4. tests/test_feed.py 작성 후 pytest 통과.

완료 후 CLAUDE.md 보고 형식으로 요약해. SPEC 변경이 필요하면 구현하지 말고 제안만 해.
```

## ② Phase 1 — 전략 순수함수 + 단위 테스트

```
SPEC.md 2절 기준으로 Phase 1 진행:

1. src/strategy/regime.py, trend.py, meanrev.py 구현. 전부 순수함수. 파라미터는 config.yaml에서 주입.
2. tests/에 골든케이스 테스트 작성: 수기로 계산 가능한 소형 봉 배열(20~120봉)을 픽스처로 만들고, 레짐 경계값(ADX 25/20, BB폭 60/40), 돈치안 돌파, BB+RSI 진입 조건을 수기 계산과 대조.
3. 룩어헤드 방지 테스트: 마지막 봉을 잘라낸 데이터로 시그널 계산 시 이전 시그널이 변하지 않음을 검증.
4. pytest 전부 통과 확인.

전략 로직에 SPEC과 다른 해석이 필요한 지점이 있으면 임의로 정하지 말고 질문 목록으로 정리해.
```

## ③ Phase 2 — 백테스트 엔진 + 게이트 G1

```
Phase 2: SPEC.md 3절 backtest/ 구현.

1. engine.py: 이벤트 드리븐, 15m 봉 마감 기준. 수수료(taker 0.05%/maker 0.02%), 슬리피지 0.03%, 펀딩비 히스토리 전부 반영. 부분청산·트레일링·시간청산·레짐 전환 강제청산 지원. 사이징은 src/risk.py 공용 함수 사용.
2. report.py: PF, Sharpe(연환산), MaxDD, 승률, 평균 R, 거래수, 월별 수익 테이블, equity curve를 report.html로.
3. G1 무결성 검증 실행:
   - 수수료 0 vs 실비용 백테스트 비교표 (비용이 수익에 미치는 영향 정량화)
   - 룩어헤드 테스트: 데이터 뒤에 임의 봉을 붙였을 때 과거 거래 내역이 불변임을 자동 검증
4. scripts/run_backtest.py로 2023-01~현재 전체 기간 1회 실행, 결과를 BACKTEST_BASELINE.md로 저장.

G1 통과 여부와 근거를 명시해서 보고해. 결과가 나빠도 파라미터 조정하지 말고 있는 그대로 보고.
```

## ④ Phase 3 — 워크포워드 + 몬테카를로 (G2·G3)

```
Phase 3: 검증 게이트 G2, G3 실행.

1. walkforward.py: 학습 6개월 / 검증 2개월 롤링 (2023-01부터). 파라미터 그리드 탐색은 학습 구간 내에서만 (탐색 범위: 돈치안 15~25, ATR 스탑 1.5~2.5, BB 표준편차 1.8~2.2, RSI 임계 25~35 — 총 조합 수를 보고에 명시).
2. OOS 구간 합산 성과로 G2 판정: 거래 ≥ 300, PF ≥ 1.3, MaxDD ≤ 15%, 월간 양수 비율 ≥ 55%.
3. 몬테카를로: OOS 거래 순서 셔플 1,000회, MaxDD 분포의 95% 분위 ≤ 20%로 G3 판정.
4. 결과를 WALKFORWARD_REPORT.md로 저장. 페어별/레짐별 성과 분해 포함.

G2 또는 G3 미달이면: 파라미터 재탐색 금지. 어느 레짐/페어가 실패 원인인지 분해 분석하고, 전략 수정 옵션을 2~3개 제안만 해. 내 승인 전 구현 금지.
```

## ⑤ Phase 4 — 페이퍼 트레이딩 + 텔레그램 (G4)

```
Phase 4: 라이브 인프라 구현. mode는 반드시 testnet 기본.

1. src/live/executor.py: ccxt로 Binance testnet 주문. 진입 체결 확인(주문 ID 폴링) → 즉시 STOP_MARKET(reduceOnly) 배치. 배치 실패 시 시장가 청산 + CRITICAL 알림. RANGE 진입은 지정가 2봉 미체결 취소 로직 포함.
2. src/live/state.py: sqlite 영속화. 재시작 시 거래소 실포지션과 대조해 복구, 불일치 시 알림.
3. src/live/runner.py: 15m 봉 마감 트리거 메인 루프. 매 루프: 데이터 갱신 → 레짐 → 시그널 → 리스크(킬스위치·스탑 존재 검증) → 실행 → 알림.
4. src/notify/telegram.py: SPEC 5절 전체(진입/청산/CRITICAL/일일요약/명령어 /status /pause /resume /close_all).
5. tests/에 상태 복구·스탑 재배치 테스트 추가. scripts/healthcheck.py 구현.
6. .env.example 갱신, README에 testnet 키 발급·실행 방법 정리.

pytest + healthcheck 통과 확인 후, run_paper.py 실행 절차를 보고해. G4(100건, 체결 괴리 ≤0.05%, PF ≥1.1) 판정용 일일 집계가 텔레그램 요약에 포함되게 해.
```

## ⑥ Phase 5 — 배포 (G4 통과 후에만)

```
G4 통과 확인됨. Phase 5:

1. Dockerfile + docker-compose.yml (restart: always, .env 주입, data/ 볼륨).
2. VPS 배포 가이드 DEPLOY.md: Oracle Free Tier 기준 docker 설치 → 배포 → systemd/compose 재시작 정책 → healthcheck cron(5분) → 실패 시 텔레그램.
3. 로그 로테이션 설정.
4. live 전환 체크리스트 작성 (내 수동 승인 항목 명시). mode 기본값은 여전히 testnet 유지.
```
