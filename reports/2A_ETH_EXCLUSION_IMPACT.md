# 2A_ETH_EXCLUSION_IMPACT.md — 2a 라이브 ETH 제외 영향 검증 (STEP 3)

배경: `reports/REBALANCE_ORDER_ANALYSIS.md` 4절에서 2a의 20자산 유니버스에
ETH가 포함돼 filtered_trend과 격리마진 넷팅 충돌 위험이 있음을 발견,
임시완화책으로 **라이브 경로에서만** ETH를 제외했다(`strategies/2a.py::
run_live_step()`, 백테스트 `run()`은 20자산 그대로). 이 문서는 그 제외가
2a의 시장중립성(상관관계, BTC 대비 잔여베타)에 실제로 얼마나 영향을
주는지 실측 재계산한다.

## 방법론 (변경 없음, 기존 코드 재사용)

- `strategies/2a.py::run()`을 그대로 재사용하되(`scripts/diag/2a_eth_
  exclusion_impact.py`), `_load_universe_daily_and_funding()`의 반환값에서
  ETH 컬럼만 제거한 걸로 몽키패치해 19자산 기준 전체 이력(185주)을
  재계산했다 — `strategies/2a.py` 자체는 건드리지 않아 게이트 통과
  수치에 영향 없음.
- 상관계수/잔여베타 계산은 `checks/signal_validation.py`의 기존 검증
  함수(`series_correlation`, `net_neutral_check`)를 그대로 호출했다 —
  신규 통계 로직 없음, `SIGNAL_VALIDATION_2a.md`가 썼던 것과 동일 공식.
- 비교 대상 filtered_trend 시계열은 `POST_FILTER_BUG_AUDIT.md`에서 확정된
  정책에 따라 **정식 `strategies/filtered_trend.py::run()`**(진짜
  사전게이팅, 638트레이드)에서 새로 계산했다 — 사후필터링으로 오염된
  `reports/signal_validation_data/1a/1a_weekly_returns.csv`는 쓰지 않았다.
  이 때문에 이 문서의 "20자산" 기준선 수치(-0.0974)는 과거 `FILTERED_
  TREND_2A_CORRELATION.md`가 보고한 -0.0986과 약간 다르다(둘 다 유효한
  값 — 데이터 소스 정책 차이 때문, 재현성 문제 아님).

## 실측 결과

| 지표 | 20자산(원본) | 19자산(ETH 제외) | 변화 |
|---|---|---|---|
| filtered_trend와 주간수익률 상관계수 | -0.0974 | -0.0859 | 절댓값 **감소**(더 독립적) |
| BTC 대비 잔여베타 | 0.0626 | 0.0978 | +0.0353 (상대 +56%) |
| BTC 대비 R² | -0.0315 | -0.0157 | 둘 다 음수(베타 자체가 설명력 거의 없음) |
| 주간수익률 평균(gross) | 1.399% | 1.303% | 소폭 감소 |
| 주간수익률 표준편차(gross) | 7.228% | 8.299% | **+14.8%** |
| 비교 주수 | 185 | 185 | 동일 |

(비고: ETH 제외로 유니버스가 20→19자산이 되며 분위 슬롯 수 k=len//4가
5→4로 줄어든다 — `strategies/2a.py::run_live_step()` 구현 시 이미 실측
확인된 부수효과. 표준편차 증가는 이 슬롯 감소[분산 축소 효과 약화]가
주된 원인으로 판단된다.)

## 판정

### 1) 상관관계 — 문제 없음

-0.0974 → -0.0859로 오히려 **절댓값이 줄었다**(독립성 강화 방향). 이
프로젝트 전체에서 "독립"의 기준으로 써온 0.3 임계치(`checks/
signal_validation.py::CORR_THRESHOLD`)에 비하면 둘 다 미미한 수준이고,
방향 변화도 시장중립성을 위협하지 않는다.

### 2) 잔여베타 — 상대변화는 크지만 절대 수준은 여전히 미미

0.0626 → 0.0978은 상대적으로 56% 증가했지만, 절대 수준은 이 프로젝트가
"유의미"로 취급해온 그 어떤 임계치(0.3 상관계수 등)에도 한참 못 미친다.
게다가 두 경우 모두 **R²가 음수**다 — BTC 단일 베타로 2a 수익률을
예측하는 게 그냥 평균으로 예측하는 것보다 못하다는 뜻으로, 베타 추정치
자체가 노이즈 지배적이며 통계적으로 안정된 관계가 아님을 시사한다.
**19자산 잔여베타(0.098)도 "시장중립"이라는 결론을 바꿀 수준이 아니다**
— 두 케이스 모두 실질적으로 0에 가깝고, 그 차이보다 추정 자체의
불확실성(음의 R²)이 더 크다.

### 3) 종합 판정: 차이는 미미함 → ETH 제외를 정식 채택

STEP 3에서 확인해야 했던 두 지표(상관관계, 잔여베타) 모두 시장중립성을
흔들 만한 변화가 아니다. **2a 라이브 유니버스에서 ETH 제외를 임시완화책이
아닌 정식 채택으로 전환한다.**

## 부가 발견: 표준편차 증가와 vol-parity 가중치 (별도 후속 과제)

STEP 3이 요구한 두 지표(상관관계·잔여베타)와는 별개로, **주간수익률
표준편차가 14.8% 증가**한 것은 실질적 함의가 있다 — `config.yaml`의
`portfolio.weights`(filtered_trend 0.8383 : 2a 0.1617)는 **20자산
2a의 변동성**을 기준으로 산출된 vol-parity 값이다(`reports/
VOL_PARITY_RECALC.md`). 라이브에서 실제로 도는 2a(19자산, ETH 제외)는
그보다 변동성이 더 크므로, 엄밀한 vol-parity 원칙을 적용하면 2a의
실제 배분 비중은 현재 설정값(16.17%)보다 **약간 더 낮아야** 한다.

이 발견 자체가 이번 STEP 3의 판정(ETH 제외 정식 채택)을 바꾸지는
않는다 — 배분 비중 재조정은 별도 결정 사항이다. `config.yaml`의 기존
원칙("배분 로직 재탐색 금지, 값을 바꾸려면 반드시 새 진단 리포트로
재검증 후 갱신할 것")에 따라, 이번 문서는 그 필요성만 기록하고 가중치
자체를 여기서 재계산/변경하지 않는다. `PAPER_TRADING_REMAINING_WORK.md`에
후속 과제로 추가 필요.

## 원자료

- `scripts/diag/2a_eth_exclusion_impact.py` — 재현 스크립트
- `reports/diag_data/2a_eth_exclusion_impact_summary.csv` — 요약 수치
- `reports/diag_data/2a_weekly_gross_returns_20asset.csv` /
  `2a_weekly_gross_returns_19asset_eth_excluded.csv` — 주간수익률 원시계열
