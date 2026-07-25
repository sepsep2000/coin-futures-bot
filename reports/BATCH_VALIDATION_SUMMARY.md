# BATCH_VALIDATION_SUMMARY.md — 신호 카탈로그 배치 검증 종합

SIGNAL_CATALOG(REDESIGN_PROPOSAL.md STEP 1) 후보 전체의 검증 결과를 한
표로 정리한다. 결과 해석·다음 액션 결정(카탈로그 우선순위 재조정 등)은
이 문서의 범위가 아니다 — 사실관계와 수치만 기록한다.

## STEP 0 선행조건 (2a)

[UNIVERSE_DIVERSIFICATION_CHECK.md](UNIVERSE_DIVERSIFICATION_CHECK.md) 판정:
**PASS** (평균 pairwise 상관계수 0.586 < 기존 3자산 0.757, corr>0.5 기준
독립 클러스터 4개) — 2a 검증 진행함.

## 판정 결과 표

| id | 카테고리 | 이벤트/트레이드 수 | 평균수익률(점추정) | 판정 | 비고 |
|---|---|---|---|---|---|
| 3a | meanrev | 2,459 | -0.0492% | **FAIL_SIGNIFICANT_NEGATIVE** | 이전 세션(SIGNAL_VALIDATION_3a3b.md) |
| 3b | meanrev | 410 | -0.0430% | **FAIL_NOT_SIGNIFICANT** | 이전 세션(SIGNAL_VALIDATION_3a3b.md) |
| 1a | multi_horizon_trend | 633 | 0.7219R | **RECLASSIFY_AS_FILTER** | trend와 상관계수 0.876, 중첩률 100% |
| 2a | cross_sectional_momentum | 185 | 1.4001% | **PASS** | trend와 상관계수 -0.060, 넷중립 확인(잔여베타 0.063) |
| 4a | volatility_regime_transition | 2,993 | 0.0273% | **FAIL_NOT_SIGNIFICANT** | CI가 0/1.0을 포함, 꼬리손실이 음의손익의 40.1% 차지 |
| 4b | volatility_regime_transition | 2,474 | 0.0090% | **FAIL_NOT_SIGNIFICANT** | CI가 0/1.0을 포함, 예상과 달리 trend와 중첩률 12.1%(낮음) |

7개 후보(trend 자체 제외) 중 **1개(2a) PASS**, 1개(1a) 필터로 재분류,
3개(3a/4a/4b) 유의성 없음, 1개(3a) 유의하게 음수.

## 누적 신호 간 상관관계 매트릭스

|  | trend | 3a | 3b | 1a | 2a | 4a | 4b |
|---|---|---|---|---|---|---|---|
| **trend** | 1.000 | -0.028 | -0.102 | 0.877 | -0.059 | 0.258 | 0.227 |
| **3a** | -0.028 | 1.000 | 0.617 | -0.089 | 0.030 | -0.068 | -0.115 |
| **3b** | -0.102 | 0.617 | 1.000 | -0.124 | 0.088 | -0.075 | -0.033 |
| **1a** | 0.877 | -0.089 | -0.124 | 1.000 | -0.097 | 0.199 | 0.207 |
| **2a** | -0.059 | 0.030 | 0.088 | -0.097 | 1.000 | 0.003 | -0.093 |
| **4a** | 0.258 | -0.068 | -0.075 | 0.199 | 0.003 | 1.000 | 0.108 |
| **4b** | 0.227 | -0.115 | -0.033 | 0.207 | -0.093 | 0.108 | 1.000 |

원본: `reports/diag_data/cumulative_signal_correlation_matrix.csv`.
부가 관찰(해석 아님, 수치 기록): 3a-3b 상관계수 0.617로 카탈로그 내에서
가장 높다. 1a-trend 0.877도 카탈로그 내 최고 수준이다. 2a는 다른 모든
신호와 |상관| < 0.1로 가장 낮다.

## 개별 리포트

- [SIGNAL_VALIDATION_3a3b.md](SIGNAL_VALIDATION_3a3b.md)
- [SIGNAL_VALIDATION_1a.md](SIGNAL_VALIDATION_1a.md)
- [SIGNAL_VALIDATION_2a.md](SIGNAL_VALIDATION_2a.md)
- [SIGNAL_VALIDATION_4a.md](SIGNAL_VALIDATION_4a.md)
- [SIGNAL_VALIDATION_4b.md](SIGNAL_VALIDATION_4b.md)
