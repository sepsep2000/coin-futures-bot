# 2a 8개 포지션 조기청산 사고 (2026-08-11)

## 1. 요약

2026-08-11 스탑 배치 버그(사고②/③) 수정 작업 검증 중, `pytest -m
integration`으로 통합 테스트 전체를 실행하면서 `tests/test_live_
scheduler_integration.py::test_real_testnet_2a_rebalance_bounded_cycle`이
**실계좌(Binance testnet)에 진짜 리밸런스 사이클을 실행**해, 당시 실제로
보유 중이던 2a 8개 포지션이 전부 의도치 않게 청산됐다. 원인은 Claude가
이 테스트의 실계좌 영향을 사전에 확인하지 않고 실행한 것 — 테스트 자체는
설계대로 정상 동작했다(버그 아님).

## 2. 메커니즘

`_process_2a_rebalance()`는 "현재 2a 보유 종목"을 판단할 때 로컬 DB가
아니라 **거래소 실측**(`exec_exchange.fetch_positions()` 계열)을 기준으로
삼는다 — 이는 2026-07-27 킬스위치 사고 이후 확립된 원칙("로컬 상태를
신뢰하지 않는다")을 그대로 따른 정상 설계다. 문제의 테스트는 `db_path`만
격리된 임시 SQLite로 주지만 `exec_exchange`는 **실제 testnet 계정**을
그대로 쓰고, `strategy_2a.run_live_step()`을 "BTC 롱 1개 + SOL 숏 1개"
짜리 축소된 가짜 목표로 스텁한다. 그 결과 리밸런스 로직이 "거래소에 실제
로 있는 8개 종목 중 가짜 목표(BTC/SOL)에 없는 것들"을 전부 "청산 대상"
으로 판단해 실제로 청산했다.

## 3. 청산 내역 (실측, `fetch_my_trades` 재조회 기준)

| 종목 | 방향 | 진입가 | 청산가 | 수량 | 순손익(수수료 반영) |
|---|---|---|---|---|---|
| ADA/USDT:USDT | 롱 | 0.1743 | 0.1865 | 1063.0 | +$12.89 |
| XMR/USDT:USDT | 롱 | 363.91 | 394.57 | 0.509 | +$15.59 |
| NEAR/USDT:USDT | 숏 | 1.674 | 1.585 | 110.0 | +$9.72 |
| SOL/USDT:USDT | 롱 | 76.00 | 75.82 | 2.44 | -$0.51 |
| ZEC/USDT:USDT | 롱 | 509.93 | 487.04 | 0.366 | -$8.45 |
| INJ/USDT:USDT | 숏 | 4.3938 | 4.4930 | 42.2 | -$4.26 |
| XLM/USDT:USDT | 숏 | 0.16441 | 0.16082 | 1132.0 | +$3.99 |
| XRP/USDT:USDT | 숏 | 1.039 | 1.004 | 179.1 | +$6.20 |
| **합계** | | | | | **+$35.16** |

청산 시각: 2026-08-11T13:00:46~48 UTC (전부). 테스트가 임시로 연 BTC/SOL
테스트 포지션은 테스트 자체의 `finally: _close_all_test_positions()`로
정상 정리됨. 사고 직후 계좌 실측: 포지션 0건, 미체결주문 0건, 알고스탑
0건, USDT $5,211.78.

## 4. 30건 판정 대상 제외

`PAPER_TRADING_REMAINING_WORK.md` 3절 기준상, 이번 2a 청산 8건은 **전략
설계(주간 리밸런스, 다음 정기 사이클 08-16)에 의한 것이 아니라 테스트
실행 실수로 인한 조기청산**이므로 정상 30건 판정 풀에서 제외한다. 진입
로직 자체(8/2, 8/9 각 사이클)는 이미 별도로 정상 검증됨 — 이번에 제외되는
건 "청산 사유"뿐이다.

## 5. 재발방지

1. **통합 테스트 재분류**(이번 조치): `pytest -m integration` 대상 5개
   테스트를 "실계좌에 실제 거래를 일으키는가" 기준으로 재검토 — 아래 4개는
   **실계좌 상태에 이미 보유 중인 포지션이 있을 때 절대 무단 실행 금지**
   (사전에 계좌 실측 확인 후 명시적 승인 하에만 실행):
   - `test_live_executor_integration.py::test_real_testnet_place_poll_cancel_and_cleanup`
     (BTC 소량 매매 + 계좌 전체 잔여포지션 0건을 전제하는 assertion 포함
     — 다른 보유 포지션이 있으면 이 assertion 자체가 실패함, 부수적으로
     발견된 별개 결함)
   - `test_live_runner_integration.py::test_real_testnet_runner_end_to_end`
     (`recover_state()`가 막아줄 수도 있지만, 계좌가 비어있으면 실신호로
     실주문까지 진행 가능)
   - `test_live_scheduler_integration.py::test_real_testnet_single_tick_filtered_trend_path`
     (실신호 발생 시 실주문)
   - `test_live_scheduler_integration.py::test_real_testnet_2a_rebalance_bounded_cycle`
     (이번 사고의 직접 원인 — **거래소 실측 기준으로 기존 보유분을
     전부 청산 대상으로 판단할 수 있음**, 계좌에 뭐가 있든 무조건 고위험)

   `test_notify_telegram_integration.py::test_real_telegram_send_message_delivered`
   만 실계좌 영향이 전혀 없음(텔레그램 발신만) — 계속 자유롭게 실행 가능.

2. **향후 원칙**: 실계좌에 보유 포지션이 있는 동안에는 위 4개 통합 테스트를
   실행하지 않는다. 부득이 실행해야 하면 실행 직전 `fetch_positions()`로
   계좌 실측을 먼저 확인하고, 보유 포지션이 있으면 사용자에게 먼저 알리고
   승인받는다.

## 6. 8/16 리밸런스까지 조치

포지션을 지금 임의로 재진입하지 않고, **다음 정기 리밸런스(2026-08-16
일요일 00:00 UTC)까지 의도적으로 무포지션 유지**한다. filtered_trend는
원래도 청산 상태였으므로 영향 없음.
