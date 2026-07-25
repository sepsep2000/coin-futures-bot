# ENGINE_MULTI_CORRELATION_UPDATE.md — 다중 신호 상관 체크 엔진 확장

## 배경

BATCH_VALIDATION_SUMMARY_V2.md에서 확인된 한계: STEP D가
`reference_signal`(항상 trend)과의 상관만 자동 검사하고, 이미 PASS/
ACCEPT된 다른 신호(예: 2a, trend+1a필터)와의 상관은 수동 사후 확인이
필요했다. 5b-2a 상관 0.523이 STEP C 기각 덕에 우연히 문제되지 않았을
뿐 — 자동화가 필요했다.

## 변경 내용 (인프라)

- `checks/signal_validation.py`: `load_accepted_signals()`,
  `multi_correlation_check()` 신규 추가. `compute_verdict()`에
  `multi_corr` 선택 인자 추가 — **기본값 None이면 기존 동작과 완전히
  동일**(하위호환, 1세대/2세대 검증 결과에 영향 없음).
- `signal_specs/accepted_signals.yaml` 신규: PASS/ACCEPT 신호 레지스트리.
  현재 2개 항목 — `2a`(PASS), `trend_filtered_1a`(ACCEPT_AS_FILTER,
  1a 검증 시 생성된 `1a_weekly_returns.csv`를 그대로 참조 — trend+1a필터
  트레이드와 1a 신호 트레이드는 애초에 동일 데이터라 재계산 안 함).
- `scripts/validate_signal.py`: STEP D 다음에 STEP D 확장 삽입 —
  accepted_signals.yaml의 각 항목과 상관계수 계산, 자기자신은
  `exclude_id`로 제외. 판정 우선순위: ① reference_signal 상관 높음 →
  RECLASSIFY_AS_FILTER(기존과 동일, 최우선) ② accepted 신호와 상관
  높음 → REDUNDANT_WITH_{id}(신규) ③ 유의성 기반 PASS/FAIL(기존과 동일).
  임계치는 CORR_THRESHOLD=0.3 그대로 재사용 — 신호별 조정 없음.

## 소급 재실행 결과

| id | 이전 판정 | 재실행 판정 | 변화 여부 |
|---|---|---|---|
| 2a | PASS | PASS | **변화 없음(회귀 확인 완료)** |
| 1a | RECLASSIFY_AS_FILTER | RECLASSIFY_AS_FILTER | **변화 없음(회귀 확인 완료)** |

두 판정 모두 그대로다 — 엔진 확장이 기존 결론을 뒤집지 않았다.

## 신규로 드러난 상관관계 (이전엔 자동 검사 안 되던 부분)

| 비교 | 상관계수 | 독립(<0.3) |
|---|---|---|
| 2a vs trend_filtered_1a | -0.0986 | True |
| 1a vs 2a | -0.0986 | True |
| 1a vs trend_filtered_1a | **1.0000** | False (중복 플래그 발생) |

**해석**: 1a와 trend_filtered_1a의 상관계수가 정확히 1.0000으로 나온 건
버그가 아니라 예상된 결과다 — 1a 신호의 트레이드 자체가 "trend를
1a 조건으로 거른 부분집합"이라 애초에 동일한 데이터다(신호로서의 1a와
필터로서의 trend+1a는 같은 트레이드 집합을 다른 이름으로 부르는 것).
1a의 최종 판정은 이 중복 플래그가 아니라 그보다 먼저 걸리는
reference_signal(trend) 상관 검사(0.876)에서 이미 RECLASSIFY_AS_FILTER로
확정되므로 결과에 영향 없다 — 다만 다중상관 체크 자체가 정상 작동함을
확인하는 좋은 검증 사례였다.

**가장 중요한 신규 정보**: 2a와 trend_filtered_1a의 상관계수는 -0.099로
낮다 — "2a + 필터링된 trend"를 포트폴리오 두 다리로 구성하는 것이
상관관계 관점에서 타당하다는 뜻이다. 이 결과는 FILTERED_TREND_2A_CORRELATION.md
에서 독립적인 방법론(교차 시간축 구간중첩 계산 포함)으로 다시 확인한다.
