# POST_FILTER_BUG_AUDIT.md — 사후 필터링 버그 소급 영향 감사

OFFICIAL_GATE_RESULT.md STEP 3에서 발견된 버그: `gen_1a`류 진단 스크립트가
"무필터 trend 백테스트를 먼저 돌린 뒤 결과를 사후 필터링"하는 방식을 썼는데,
ETH가 단일 포지션 슬롯이라 필터에 걸렸어야 할 트레이드가 원본 백테스트에서
여전히 슬롯을 점유해 그 다음에 왔을(필터를 통과했을) 신호를 원천 차단하는
경로의존적 오염이 있었다(entry_time이 인접한 트레이드 7~9건 교차 확인).
이 문서는 이 버그가 프로젝트의 다른 검증 결과에 미친 영향을 전수 조사한다.
원본 데이터: `reports/diag_data/audit_*.csv`, 스크립트:
`scripts/diag/post_filter_bug_audit.py`.

---

## STEP 1. 오염 범위 조사 (코드 기준, 재확인 완료)

### 사후 필터링을 실제로 사용한 것 (오염 확정)

| 대상 | 코드 근거 |
|---|---|
| `scripts/validate_signal.py`의 `gen_1a()` → SIGNAL_VALIDATION_1a.md | `trend_trades = get_trend_trades(...)` → `filtered = df[agree]` (55-89행) — 무필터 백테스트 결과를 사후 필터링 |
| `scripts/diag/filter_validation_1a.py` → FILTER_VALIDATION_1a.md | `trades = get_trend_trades(...)` → `half[half["agree"]]` (52행, 62행) — 동일 패턴 |

### 사후 필터링을 쓰지 않은 것 (코드 검토로 오염 없음 확인)

| 대상 | 코드 근거 |
|---|---|
| `scripts/diag/signal_validation_3a3b.py`(3a/3b 레짐 배타성) | `_simulate_3a`/`_simulate_3b`(42-88행)는 BB/RSI 조건으로 **처음부터 독자적인** 트레이드를 생성 — trend 백테스트 결과를 필터링하는 구조가 아니다. `get_trend_trades()`는 STEP A/D의 비교 대상(레퍼런스)으로만 쓰인다(227행). 애초에 3a/3b는 RANGE 레짐 전용, trend는 TREND 레짐 전용이라 포지션 슬롯을 공유하지 않는다 — 슬롯 점유 캐스케이드 문제 자체가 발생할 수 없는 구조. |
| `gen_4a`/`gen_4b`(scripts/validate_signal.py 92-204행) | 3a/3b와 동일 — BB폭/ADX 조건으로 독자 시뮬레이션, `get_trend_trades()`는 레퍼런스로만 사용 |
| `gen_2a`/`gen_5a`(횡단면 신호) | trend과 아예 다른 자산군(20자산)·다른 메커니즘(주간 리밸런스) — trend의 포지션 슬롯과 무관 |
| D1~D7b(사후진단 시리즈) | `get_trend_trades()`를 trend **자신의** 성과 분석에만 사용(비용귀속·MFE/MAE·부트스트랩 등) — "trend+필터" 같은 파생물을 만들지 않는다 |

재확인 방법: 각 파일을 다시 읽어 `get_trend_trades()`의 반환값이 (a) 필터링돼
최종 산출물이 되는지, (b) 단순 비교 레퍼런스로만 쓰이는지 코드 흐름을
직접 추적했다(기억/추정 없이 이번에 재확인).

### 다운스트림 오염 전파 (1차 오염원의 산출물을 다시 읽어들인 것들)

| 문서 | 오염 경로 |
|---|---|
| ENGINE_MULTI_CORRELATION_UPDATE.md | `validate_signal.py`로 1a 소급 재실행 시 `gen_1a()` 재호출 → 동일 오염 |
| FILTERED_TREND_2A_CORRELATION.md | `1a_trades.csv`(오염된 633건)를 직접 로드 |
| PORTFOLIO_2A_FILTEREDTREND_BACKTEST.md / `_NET.md` | `1a_trades.csv`를 leg1 소스로 직접 로드(`portfolio_2a_filteredtrend_backtest.py` 42행) — **vol-parity 배분 가중치가 여기서 계산됨** |
| BATCH_VALIDATION_SUMMARY.md / `_V2.md` | 누적 상관관계 매트릭스의 "1a" 행/열이 오염된 `1a_weekly_returns.csv` 기반 |
| GATE_STRUCTURE_PROPOSAL_FINAL.md | 인용된 Sharpe/Calmar 수치가 오염된 PORTFOLIO_2A_FILTEREDTREND_BACKTEST(_NET).md 근거 |

이 문서들은 재작성하지 않는다(과거 시점 기록으로 보존) — 대신 아래에서
정정된 수치를 제시하고, 이미 OFFICIAL_GATE_RESULT.md가 정식 strategies/
코드 기준으로 이 오염을 우회해 만들어졌다는 점을 명시한다(정식 파이프라인
자체는 처음부터 깨끗했다 — 이 감사가 필요했던 건 그 이전 진단 단계 산출물
때문).

---

## STEP 2. 재실행 결과

### 2-1. SIGNAL_VALIDATION_1a 재현 (정식 strategies/filtered_trend.py 638건 기준)

| 항목 | 원래(오염, 633건) | 재실행(정정, 638건) |
|---|---|---|
| trend과의 상관계수(STEP D) | 0.8762 | 0.8729 |
| 2a와의 상관계수(다중상관) | -0.0986 | -0.0975 |
| 평균R(STEP C) | 0.7219 | 0.7093 |
| 95% CI | [0.5991, 0.8394] | [0.5837, 0.8287] |
| PF | 3.244 | 3.174 |
| 활성구간 중첩률(trend 대비) | 100.00% | 99.07% |

**판정: RECLASSIFY_AS_FILTER — 변화 없음(회귀 확인, 결론 유지).** trend
상관계수(0.87 근방)가 임계치(0.3)를 압도적으로 넘는 구조이므로, 수치의
소폭 변화가 판정을 바꿀 여지가 애초에 없었다.

### 2-2. FILTER_VALIDATION_1a 재현 (학습/홀드아웃, 정식 데이터 기준)

| 구간 | PF 개선폭(원래→정정) | 평균R 개선폭(원래→정정) |
|---|---|---|
| 학습 | +0.228 → +0.219 | +0.116 → +0.110 |
| 홀드아웃 | **+0.411 → +0.349** | **+0.171 → +0.152** |

**판정: REPLICATED_ACCEPT_FILTER — 변화 없음(회귀 확인, 결론 유지).**
홀드아웃 개선폭이 학습보다 크다는 원래의 핵심 근거(과최적화 아님)는
정정 후에도 유지된다(+0.349 > +0.219, +0.152 > +0.110). 다만 개선폭
자체는 원래 보고보다 다소 작다 — "얼마나 좋은가"의 정도는 소폭
하향 조정되지만 "재현되는가"의 결론(예/아니오)은 그대로다.

### 2-3. 3a/3b — 재실행 불필요(코드 구조상 오염 경로 없음, STEP 1 근거 참조)

---

## STEP 3. 다운스트림 연쇄 영향 — vol-parity 배분 가중치

**이것이 가장 실질적인 영향이다.** 현재 `config.yaml`의
`portfolio.weights`(filtered_trend=0.8532, 2a=0.1468)는 오염된 leg1
데이터(σ1=0.01242)로 계산됐다. 정정된 leg1(σ1=0.01392, 변동성이 실제로는
더 컸다)로 다시 계산하면:

| | filtered_trend 가중치 | 2a 가중치 |
|---|---|---|
| 현재 config.yaml(오염 기준) | 0.8532 | 0.1468 |
| 정정 데이터 기준 재계산 | **0.8383** | **0.1617** |
| 차이 | -0.0149 | +0.0149 |

약 1.5%p 차이 — 크지 않지만 실재한다. **이 값을 지금 config.yaml에
반영하지는 않았다** — 이번 태스크의 출력 범위는 이 감사 문서이고,
배분 가중치를 실제로 바꾸는 것은 별도 결정 사항이라고 판단했다(그리드
서치성 재탐색과 혼동되지 않도록 신중하게 분리). **권고**: 다음 배분
갱신 시점에 이 정정값(83.8:16.2)을 반영할지 사용자가 결정할 것 —
현재 값(85.3:14.7)을 그대로 둔다면 그것이 오염된 계산에서 나온 값임을
인지한 채로 유지하는 것이라는 점만 분명히 해둔다.

---

## 요약

| 항목 | 오염 여부 | 재실행 판정 | 원래 판정 대비 |
|---|---|---|---|
| SIGNAL_VALIDATION_1a | 오염됨 | RECLASSIFY_AS_FILTER | **변화 없음** |
| FILTER_VALIDATION_1a | 오염됨 | REPLICATED_ACCEPT_FILTER | **변화 없음**(개선폭 다소 축소) |
| 3a/3b 레짐 배타성 | 오염 안 됨 | (재실행 불필요) | 해당 없음 |
| vol-parity 배분 가중치 | **오염된 값이 현재 config.yaml에 유효** | 정정 시 85.3:14.7 → 83.8:16.2 | **반영 여부 결정 필요** |

핵심 결론: 이번 감사에서 **어떤 판정도 뒤집히지 않았다** — 1a 필터의
"신호로는 부적합, 필터로는 유효"라는 결론은 정정된 데이터로도 그대로
재현된다. 다만 정확한 수치(특히 포트폴리오 배분 가중치)는 소폭
정정이 필요하며, 이를 실제로 config에 반영할지는 별도 승인 사항으로
남긴다.
