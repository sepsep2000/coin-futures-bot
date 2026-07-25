# VOL_PARITY_RECALC.md — vol-parity 가중치 정정 반영 및 재계산

POST_FILTER_BUG_AUDIT.md에서 확인된 오염(사후필터링 leg1 데이터로 계산된
vol-parity 가중치)을 정정한다. `config.yaml`의 `portfolio.weights`를
83.8:16.2(filtered_trend:2a)로 갱신하고, 결합 포트폴리오 성과지표를 전부
재계산했다. 가중치 자체는 재탐색하지 않았다 — POST_FILTER_BUG_AUDIT.md가
이미 산출한 정정값(`corrected_w1_filtered_trend=0.8383017180188289`,
`corrected_w2_2a=0.1616982819811711`)을 그대로 config에 반영했을 뿐이다.
스크립트: `scripts/diag/vol_parity_recalc.py`(기존 `official_gate_check.py`의
`_metrics_from_weekly` 재사용, 신규 계산 로직 없음).

## 비교표

| 구성 | 가중치(trend:2a) | PF | Sharpe | 연환산수익률 | MaxDD | Calmar | MC MaxDD 95%ile | G2 | G3 |
|---|---|---|---|---|---|---|---|---|---|
| **정정(83.8:16.2, 현재 config)** | 83.83 : 16.17 | 2.219 | **2.029** | 24.69% | 4.41% | **5.593** | 9.11% | PASS | PASS |
| 오염값(85.3:14.7, 이전 config) | 85.32 : 14.68 | 2.236 | 2.039 | 23.89% | 4.28% | 5.584 | 8.74% | PASS | PASS |
| 50:50(참고) | 50.00 : 50.00 | 1.764 | 1.481 | 41.99% | 12.48% | 3.365 | 23.26% | PASS | **FAIL** |

## 게이트 통과 여부가 바뀌는가

**바뀌지 않는다.** 정정 전(오염값)과 정정 후 모두 G2(Sharpe≥1.0 AND
Calmar≥1.0 AND MaxDD≤15%)·G3(MC MaxDD 95%ile≤20%) PASS다. 수치 차이는
작다(Sharpe -0.010, Calmar +0.009, MaxDD +0.13%p, MC MaxDD 95%ile
+0.37%p) — 2a의 비중이 14.7%→16.2%로 소폭 늘면서 2a 고유의 변동성이
아주 조금 더 반영된 정도다.

## 50:50 대비 우위는 유지되는가

**유지된다.** 정정된 vol-parity(83.8:16.2)는 50:50 대비 Sharpe(2.03 vs
1.48), Calmar(5.59 vs 3.36), MaxDD(4.41% vs 12.48%) 전부 우월하다.
오히려 이번 재계산에서 **50:50 구성 자체가 G3에서 FAIL**로 확인됐다(MC
MaxDD 95%ile 23.26% > 20% 임계치) — vol-parity 채택의 근거가 정정
후에도 그대로, 아니 더 명확하게 유지된다.

## 결론

가중치 정정(85.3:14.7 → 83.8:16.2)은 결합 포트폴리오의 게이트 통과
여부나 배분 우위 순위를 바꾸지 않는다. `config.yaml`은 정정값으로
갱신 완료(`strategies/portfolio_combined.py`가 이 값을 그대로 읽어
쓴다 — 코드 변경 없음, 설정값만 교체).
