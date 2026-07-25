# VOL_PARITY_19ASSET_RECALC.md — 2a 19자산(ETH 제외) 기준 vol-parity 재검증

배경: `2A_ETH_EXCLUSION_IMPACT.md`에서 2a의 라이브 유니버스를 ETH 제외
19자산으로 정식 채택하면서, 2a의 주간수익률 표준편차가 20자산 기준보다
14.8% 높다는 걸 확인했다. 그런데 현재 `config.yaml`의 vol-parity 가중치
(83.83:16.17)와 그걸로 산출된 게이트 통과 수치(Sharpe 2.03/Calmar
5.59/MaxDD 4.41%, `OFFICIAL_GATE_RESULT.md`)는 전부 **20자산** 2a
변동성으로 계산됐다 — 실제 라이브에서 도는 유니버스와 다른 기준이었다.
이 문서는 19자산 기준으로 가중치를 다시 계산하고, 그 가중치로 게이트가
여전히 통과하는지 실측 확인한다.

## 방법론 (재탐색 아님, 기존 공식·스크립트 재사용)

- 가중치 공식: `scripts/diag/portfolio_2a_filteredtrend_backtest_net.py`와
  완전히 동일 — `w1 = (1/σ1) / (1/σ1 + 1/σ2)`, σ는 주간수익률 표준편차
  (ddof=1, 두 다리의 인덱스 합집합에 대해 결측주는 0으로 채움).
- leg1(filtered_trend) 주간수익률: `strategies/portfolio_combined.py::
  _leg1_weekly_return()` 그대로 재사용(POST_FILTER_BUG_AUDIT.md 이후
  정식 경로, 진짜 사전게이팅 638트레이드 기준 — 오염된 진단 데이터 아님).
- leg2(2a) 주간수익률: `strategies/2a.py::run()`을 몽키패치로 19자산에
  재실행(2A_ETH_EXCLUSION_IMPACT.md와 동일 방식)해 `net_return`(비용반영)
  컬럼 사용.
- 게이트 판정: `scripts/diag/official_gate_check.py::_metrics_from_weekly`
  그대로 재사용 — Sharpe/Calmar/MaxDD/MC MaxDD 95%ile 계산 로직 신규
  없음, `OFFICIAL_GATE_RESULT.md`와 동일 기준.
- 스크립트: `scripts/diag/vol_parity_19asset_recalc.py`

## 실측 결과

| 구성 | 가중치(trend:2a) | PF | Sharpe | 연환산수익률 | MaxDD | Calmar | MC MaxDD 95%ile | G2 | G3 |
|---|---|---|---|---|---|---|---|---|---|
| 현재 config, 20자산 2a(기준선, OFFICIAL_GATE_RESULT.md 재확인) | 83.83 : 16.17 | 2.218 | 2.028 | 24.67% | 4.41% | 5.589 | 9.11% | PASS | PASS |
| 현재 config(20자산 가중치)를 19자산 2a에 그대로 적용 | 83.83 : 16.17 | 1.993 | 1.808 | 23.50% | 7.16% | 3.281 | 11.23% | **PASS** | **PASS** |
| **새 vol-parity(19자산 기준 재계산)** | **85.63 : 14.37** | 2.024 | 1.843 | 22.69% | 6.58% | 3.446 | 10.55% | **PASS** | **PASS** |

σ1(filtered_trend) = 0.01392, σ2(2a, 19자산 net) = 0.08293 → w_2a =
14.37%(기존 16.17%보다 낮음 — 2a의 변동성이 더 커진 만큼 vol-parity
공식이 자동으로 비중을 낮춘 결과, 예상과 일치).

## 게이트 통과 여부가 바뀌는가

**바뀌지 않는다 — 세 시나리오 전부 G2·G3 PASS.** 다만 수치 여유폭은
확실히 줄었다:
- Sharpe: 2.028 → 1.843 (하락하지만 기준 1.0의 거의 2배, 여유 충분)
- Calmar: 5.589 → 3.446 (하락하지만 기준 1.0의 3배 이상)
- **MaxDD: 4.41% → 6.58%** (증가하지만 15% 임계치의 절반 이하 — 절대
  근접하지 않음)
- MC MaxDD 95%ile: 9.11% → 10.55% (증가하지만 20% 임계치의 절반 수준)

**MaxDD가 15%에 근접하거나 넘는지가 이번 검증의 핵심 질문이었는데,
6.58%는 임계치의 44% 수준에 불과해 전혀 근접하지 않는다.** 20자산
가중치를 19자산 데이터에 그대로 쓰는 것(중간 시나리오)보다 새로 계산한
19자산 vol-parity 가중치를 쓰는 쪽이 MaxDD/Calmar 모두 소폭 더 낫다
(가중치를 낮춘 효과가 실제로 작동함).

## 판정: PASS 유지 → config.yaml 갱신

전부 PASS이므로 STEP 2의 "PASS 유지" 분기를 따른다. `config.yaml`의
`portfolio.weights`를 19자산 기준 vol-parity 값(filtered_trend
0.8562663620801656, 2a 0.1437336379198344)으로 갱신했다 — 정정 사유는
"2a 라이브 유니버스가 20자산이 아니라 19자산(ETH 제외, 정식 채택)이므로
그 실제 유니버스의 변동성으로 vol-parity를 다시 맞춘다"이며, 가중치
공식 자체나 게이트 기준은 전혀 바꾸지 않았다(재탐색 금지 원칙 준수).

## 결론

2a의 ETH 제외가 vol-parity 가중치와 게이트 여유폭에 실측으로 확인
가능한 영향을 줬지만(Sharpe/Calmar 하락, MaxDD 상승), **어떤 게이트도
FAIL로 전환되지 않았고 MaxDD는 임계치에 근접하지도 않았다.** G4(페이퍼
트레이딩) 착수를 보류할 근거는 이번 검증에서 나오지 않았다.

## 원자료

- `scripts/diag/vol_parity_19asset_recalc.py` — 재현 스크립트
- `reports/diag_data/vol_parity_19asset_recalc_comparison.csv` — 비교표 원자료
