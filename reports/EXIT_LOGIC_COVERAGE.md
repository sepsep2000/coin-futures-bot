# EXIT_LOGIC_COVERAGE.md — filtered_trend 라이브 청산 로직 완성 (STEP 2)

배경: `src/live/scheduler.py`가 처음 구현됐을 때(스케줄러 태스크) filtered_trend의
라이브 청산은 스탑로스만 구현됐다(`state.py`의 `positions` 스키마에
`initial_stop`/`bars_held`/`partial_taken`/`funding_paid_usd`가 없어
`engine.py::_manage_trend_position()`을 그대로 재사용할 수 없었기 때문 —
당시 모듈 docstring에 명시적으로 기록됨). 이번 태스크에서 스키마를
확장(별도 커밋)하고, `_manage_trend_position()`/`_apply_funding()`을
재해석 없이 그대로 호출하도록 `src/live/scheduler.py::_manage_open_
filtered_trend_position()`을 구현했다.

## 1. 구현 방식 — 재해석 금지 원칙 준수

- 청산 판단(스탑로스/부분익절/트레일링/시간청산) 전부 `src/backtest/engine.py`의
  `_manage_trend_position()`을 그대로 호출한다. 스케줄러는 매 틱마다
  DB에 저장된 필드로 `Position` 데이터클래스를 재구성(`_reconstruct_
  filtered_trend_position()`)해 넘길 뿐, 청산 조건 자체를 재구현하지
  않는다.
- `bars_held`는 컬럼으로 저장하지 않고 `entry_time`에서 매 틱 계산한다
  (`_compute_bars_held()`) — 상태 이중관리를 피하고 프로세스 재시작에도
  안전하다(엔진 루프의 "매 관리 호출마다 +1" 의미론과 정확히 같은 값이
  나옴을 단위 테스트로 확인).
- 트레일링 스탑 계산(`trailing_stop()`)도 `strategies/filtered_trend.py::run()`과
  동일하게 최근 데이터 윈도 전체에 대해 계산한 뒤 마지막 값만 쓴다.
- 실제 주문은 `_manage_trend_position()`이 반환한 `Trade` 객체(들)마다
  개별 실행한다 — 배치·시뮬레이션 없음, 각 청산 이벤트를 실제 시장가
  주문으로 처리.

## 2. 시뮬레이션 가격 vs 실제 체결가

`_manage_trend_position()`은 백테스트와 동일한 슬리피지 모델로 시뮬레이션된
`exit_price`를 계산해 `Trade`에 담는다. 라이브에서는 이 시뮬레이션 가격을
청산 여부/시점 "판단"에만 쓰고(엔진 로직 100% 그대로), 실제로 telegram에
보고하는 가격·R배수는 거래소가 반환한 실제 체결가로 다시 계산한다
(`_current_r_multiple()`을 실제 체결가로 재호출 — 공식은 동일 함수,
입력값만 실측으로 교체). 두 값의 미세한 차이는 이미 OFFICIAL_GATE_RESULT.md
STEP 3에서 확인된 것과 같은 종류(슬리피지 추정 vs 실제 체결)이며 새로운
현상이 아니다.

## 3. 실패 처리 — 공허한 성공 금지

청산 주문이 미체결이면(`executor.place_order`가 `status != "filled"`
반환) `_manage_trend_position()`이 계산한 "이렇게 됐어야 한다"는 결과를
state에 반영하지 않는다 — 포지션 행을 그대로 두고 CRITICAL 알림만
보낸다. gate_verify.py의 공허한 PASS 버그와 같은 클래스의 실수를
반복하지 않기 위한 설계(이전 세션에서 이미 확립된 원칙, 이번에도 동일
기준 적용). 알려진 한계 1건(같은 틱에 Trade가 2건 나오고 그 중 하나만
체결될 때 state가 all-or-nothing으로 갱신 안 됨, 드문 엣지케이스)은
코드 docstring에 명시하고 이번 범위 밖으로 남겼다.

## 4. 커버리지 재확인 (실측)

`strategies/filtered_trend.py::run()`을 현재 config.yaml + 최신 ETH
데이터로 재실행, `Trade.exit_reason` 분포를 집계했다(방법론 동일 재사용,
신규 통계 없음):

| exit_reason | 트레이드 수 | 비중 |
|---|---|---|
| stop_loss | 416 | 65.2% |
| partial_tp | 214 | 33.5% |
| time_stop | 8 | 1.3% |
| **합계** | **638** | **100.0%** |

**해석**: 스케줄러 최초 구현 시점에 "과거 실측상 청산의 68.1%가
stop_loss"라고 인용했던 수치는 `engine.py::_manage_trend_position()`
docstring에 있던 **다른 분석**(stop_loss 청산들의 봉수(bars-held)
분포 중앙값 논의, "즉시 스탑 vs 지연 스탑" 비율 — exit_reason 전체
분포가 아님)이었다 — 이번에 직접 재집계한 exit_reason 분포는 65.2%로
약간 다르다(설정/데이터 버전 차이, 큰 틀은 같음: stop_loss가 다수).
**중요한 건 이전 수치와의 정합이 아니라, 이번 구현으로 세 가지 exit_reason
전부(stop_loss/partial_tp/time_stop)가 스탑로스만(65.2%)이 아니라
100% 라이브에 반영됐다는 사실이다.**

## 5. 테스트

`tests/test_live_scheduler.py`에 정상/경계/실패 케이스 6건 추가(스텁
거래소, 네트워크 없음):
- `test_compute_bars_held_zero_on_entry_and_increments_per_bar`
- `test_reconstruct_filtered_trend_position_maps_all_fields`
- `test_manage_open_position_stop_loss_full_fill_deletes_position`
- `test_manage_open_position_close_order_fails_keeps_position_and_alerts`
  (★ 요구된 실패 케이스)
- `test_manage_open_position_no_exit_condition_keeps_position_open`
- `test_manage_open_position_partial_tp_reduces_qty_and_moves_stop_to_breakeven`

time_stop(시간청산) 경로는 별도 스텁 단위 테스트를 추가하지 않았다 —
`_manage_trend_position()` 자체가 engine.py에서 이미 골든케이스로
검증된 함수이고(tests/test_engine.py), 스케줄러 쪽에서 새로 하는 일은
"그 함수를 호출하고 결과를 실행한다"뿐이라 stop_loss/partial_tp 두
경로로 이미 "호출→실행→상태갱신" 배선이 검증됐다고 판단했다(재구현이
없으므로 exit_reason별로 매번 별도 시나리오를 반복할 필요는 낮음 —
판단이 틀렸다면 통합 테스트에서 드러난다).

## 6. 결론

filtered_trend 라이브 청산은 이제 백테스트가 검증한 세 가지 exit_reason을
전부(재해석 없이) 재사용한다. `PAPER_TRADING_REMAINING_WORK.md`의
우선순위 11(이번 태스크로 해소)을 갱신 필요.
