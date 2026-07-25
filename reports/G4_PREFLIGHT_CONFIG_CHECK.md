# G4_PREFLIGHT_CONFIG_CHECK.md — G4 착수 전 config.yaml 최종 정합성 확인

방법론: 전부 실제 코드를 `grep`/직접 읽기로 추적했다(추정 없음). 각
항목에 파일:줄번호 인용을 남긴다.

---

## 확인 1. `active_strategies` — scheduler.py가 실제로 읽는가

**결과: 안 읽는다. `active_strategies`는 라이브 경로와 완전히 무관하다.**

전체 코드베이스에서 `active_strategies`를 참조하는 곳은 전부
`src/backtest/engine.py::run_backtest()`(레거시 범용 백테스트 엔진,
Phase 2/3 초기 단계에서 쓰던 것)와 그걸 호출하는 진단/백테스트
스크립트(`scripts/run_backtest.py`, `scripts/run_walkforward.py`,
`scripts/diag/common.py`)뿐이다:

```
src/backtest/engine.py:233:    active_strategies = cfg["active_strategies"]
src/backtest/engine.py:332:        if regime == TREND and "trend" in active_strategies:
src/backtest/engine.py:353:        elif regime == RANGE and "meanrev" in active_strategies:
```

`src/live/scheduler.py`, `strategies/filtered_trend.py`,
`strategies/2a.py` 어디에도 `active_strategies`를 읽는 코드가 없다
(전체 파일 grep 결과 0건).

**의미**: `config.yaml`의 `active_strategies: ["trend"]`는 **라이브
스케줄러의 동작에 아무 영향도 주지 않는다.** 이 값을 `[]`로 비우거나
`["meanrev"]`로 바꿔도 `scheduler.py`는 여전히 정확히 지금과 똑같이
동작한다(아래 확인 3 참조). 이 필드는 순수히 레거시 백테스트 엔진
(`run_backtest()`)을 직접 호출하는 스크립트에만 의미가 있다.

---

## 확인 2. 2a가 실제로 19자산 전부를 대상으로 실행되는가

**결과: 그렇다. `exchange.pairs`(ETH 1개)로 축소되지 않는다.**

`_process_2a_rebalance()`(scheduler.py:281)는 `config.yaml`의
`exchange.pairs`를 전혀 참조하지 않고, **`config/tickers.txt`를 직접
읽는다**:

```
src/live/scheduler.py:287:    tickers_path = project_root / "config" / "tickers.txt"
src/live/scheduler.py:288:    tickers = [ln.strip() for ln in tickers_path.read_text(...) ...]
```

`strategy_2a.run_live_step()`이 내부적으로 호출하는
`_load_universe_daily_and_funding()`도 동일하게 `config/tickers.txt`를
직접 읽는다(strategies/2a.py:35-36). 실제로 `config/tickers.txt`를
읽어보면:

```
$ grep -v '^#' config/tickers.txt | grep -v '^\s*$' | wc -l
20
```

20자산(BTC, ETH, SOL, XRP, ZEC, DOGE, BNB, NEAR, ADA, LINK, UNI, AVAX,
XLM, AAVE, LTC, INJ, TRX, DOT, BCH, XMR)이 실제로 들어있다. 여기서
`strategies/2a.py::LIVE_EXCLUDED_SYMBOLS = {"ETH"}`(2A_ETH_EXCLUSION_
IMPACT.md에서 정식 채택)가 ETH만 라이브 경로에서 제외해 **정확히
19자산**이 남는다. `config.yaml`의 `exchange.pairs: ["ETH/USDT:USDT"]`는
2a 유니버스 결정에 **전혀 관여하지 않는다** — 이 필드가 1개 자산이라고
2a가 1자산으로 축소되는 일은 없다.

`exchange.pairs`를 실제로 읽는 곳은 데이터 수집/백테스트 스크립트뿐이다
(`scripts/collect_data.py`, `scripts/run_backtest.py`,
`scripts/run_walkforward.py`) — 전부 filtered_trend(ETH 단일자산) 백테스트
전용이라 1개 자산인 게 맞다. 2a의 유니버스는 애초에 별도 파일
(`config/tickers.txt`)로 분리 설계돼 있고, 라이브 경로는 그 분리를
정확히 따른다.

---

## 확인 3. `active_strategies: ["trend"]`가 filtered_trend를 가리키는가

**결과: 가리키지 않는다 — 이 필드는 filtered_trend의 활성화 여부와
아무 관계가 없다. filtered_trend는 이 필드와 무관하게 항상 실행된다.**

확인 1에서 이미 증명했듯 `active_strategies`는 `run_backtest()`
(레거시 엔진) 전용이다. 라이브 스케줄러의 `_tick()`은 매 15분 틱마다
`_process_filtered_trend_tick()`을 **조건 없이 무조건** 호출한다:

```
src/live/scheduler.py (tick 순서, 모듈 docstring 및 _tick() 본문):
  1. ensure_stop_placed 스윕
  2. 일일손실한도 체크
  3. (리밸런스 경계면) _process_2a_rebalance()
  4. _process_filtered_trend_tick()   <- cfg["active_strategies"] 체크 없음
  5. 자본 스냅샷 저장
```

`_process_filtered_trend_tick()`(scheduler.py:388) 함수 본문 전체에
`active_strategies`를 조건으로 쓰는 코드가 없다 — 신규진입 게이트는
`entries_blocked`(일일손실한도)와 `can_open_new_position()`뿐이다.

**즉 옛 이름("trend")이 남아있어서 filtered_trend가 꺼져있는 게
아니라, 애초에 이 필드 자체가 라이브 경로에서 읽히지 않아 filtered_trend는
항상(무조건) 켜져 있다.** 우려했던 "죽은 이름 때문에 조용히 꺼져있는"
시나리오는 아니다 — 대신 "이 필드가 라이브에서 아예 무의미하다"는
별개의 문제가 있다(아래 판정 참조, 오해의 소지가 있어 정리 필요).

---

## 확인 4. state.py가 쓰는 실제 DB 파일 경로

**결과: 확정된 경로가 없다 — `config.yaml`에도 없고, `scheduler.py`에도
기본값이 없다. `src/live/state.py`의 모든 함수는 `db_path: Path`를
필수 인자로만 받는다(기본값 없음, `init_db(db_path)`부터 전부).**

`run_live_loop(cfg, db_path, max_iterations=None)`(scheduler.py:91)도
`db_path`를 **호출부가 반드시 넘겨야 하는 필수 매개변수**로 두고
자체 기본값이 없다. 현재 이 함수를 실제로 호출하는 코드는 **테스트
파일뿐**이다:

```
$ grep -rn "run_live_loop(" --include=*.py . | grep -v .venv
tests/test_live_scheduler.py (7건, 전부 tmp_path 기반 테스트 DB)
```

프로덕션에서 실제 경로로 `run_live_loop()`을 호출하는 코드는
**존재하지 않는다** — 이번 세션에서 `scripts/healthcheck.py`를 만들며
`DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "live_state.db"`
(healthcheck.py:60)라는 **healthcheck.py 자체만의 추정값**을 넣었을
뿐, 이 경로가 실제 스케줄러 실행과 일치한다는 보장이 코드 어디에도
없다. 실제로 `data/live_state.db` 파일은 지금 존재하지 않는다(한
번도 실행된 적이 없다는 뜻이기도 하다).

**이건 이번 확인 작업 중 발견한 더 근본적인 문제(아래)의 한 증상이다.**

---

## 추가 발견(요청 범위 밖이지만 실행 전 반드시 알아야 함): 프로덕션 엔트리포인트 자체가 없다

확인 4를 조사하다가 발견 — **`run_live_loop()`을 실제 운영 조건으로
호출하는 스크립트가 프로젝트에 하나도 없다.** `scripts/`를 전부
확인한 결과:

```
scripts/collect_data.py, collect_universe.py, gate_verify.py,
healthcheck.py, run_backtest.py, run_walkforward.py, trade_logger.py,
validate_signal.py
```

`run_live.py` 또는 그에 준하는 이름의 스크립트가 없다. 그런데
`scripts/run_cycle.sh`(cron/systemd timer 등록 대상으로 이미 존재하는
스크립트)는 이렇게 돼있다:

```bash
python src/live/runner.py --once   # G5 완료 후 실제 엔트리포인트로 경로 확정
```

주석에 스스로 "경로 확정 예정"이라고 적어놨고, **`src/live/runner.py`는
실제로 존재하지 않는다**(`src/live/`엔 `executor.py`, `scheduler.py`,
`state.py`, `__init__.py`뿐 — `ls` 직접 확인). 이 스크립트를 지금
그대로 실행하면 즉시 `ModuleNotFoundError`(또는 `No such file or
directory`)로 죽는다.

게다가 **이중으로 막혀있다**: `run_cycle.sh`는 `python src/live/runner.py`를
호출하기 전에 `python scripts/gate_verify.py`가 exit 0이어야
한다는 조건을 거는데, `harness_config.json`이 이 저장소에 없어서
`gate_verify.py`는 (이전 세션에서 고친 버그 수정 결과 그대로)
`STATUS_ERROR` + `exit(1)`을 반환한다(직접 실행해 재확인함). 즉
`run_cycle.sh`는 `runner.py` 부재 여부와 무관하게 **지금 그대로
실행하면 항상 "harness FAIL -> 봇 실행 스킵"으로 끝나 절대 봇을
실행하지 않는다.**

`run_cycle.sh`는 `scheduler.py`가 아직 구현되기 전(설계 단계)에
작성된 뒤 그대로 방치된 것으로 보인다 — `scheduler.py::run_live_loop()`가
실제로 완성된 지금 이 스크립트는 갱신되지 않았다.

---

## 종합 판정

| 항목 | 결과 |
|---|---|
| 1. active_strategies | 라이브 경로와 무관(레거시 백테스트 엔진 전용) — 필드 자체가 오해 소지 |
| 2. 2a 19자산 실행 | **정상** — config/tickers.txt 직접 참조, exchange.pairs로 축소 안 됨 |
| 3. filtered_trend 활성화 | **항상 켜져 있음**(조건부 아님) — active_strategies와 무관 |
| 4. DB 경로 | **확정된 곳이 없음** — healthcheck.py의 추정값만 존재, 실제 실행 경로 없음 |
| 추가 | **프로덕션 엔트리포인트 자체가 없음, run_cycle.sh는 이중으로 깨져 있음** |

**"이 확인 없이 스케줄러를 실행 가능 상태로 보고하지 말 것"이라는
지시에 따라 명시한다: G4는 지금 상태로 착수할 수 없다.** 코드 수준
로직(2a 19자산, filtered_trend 활성화)은 문제없지만, **실제로 봇을
띄울 방법 자체가 없다.**

## G4 착수 전 반드시 고쳐야 할 것

1. **프로덕션 엔트리포인트 스크립트 신설**(`scripts/run_live.py` 등) —
   config.yaml 로드, DB 경로 확정, `run_live_loop(cfg, db_path)` 호출.
   이 스크립트가 DB 경로의 **유일한 진실 소스**가 돼야 하고,
   `scripts/healthcheck.py::DEFAULT_DB_PATH`가 정확히 같은 경로를
   가리키는지 반드시 맞춰야 한다(지금은 healthcheck.py만 추정값을
   갖고 있어 둘이 어긋날 위험이 있음).
2. **`scripts/run_cycle.sh` 수정 또는 폐기** — `src/live/runner.py`
   참조를 새 엔트리포인트로 교체하고, `gate_verify.py` 선행 체크가
   이 프로젝트에 맞는 게이트인지(harness_config.json 기반 범용
   회귀체크 — SPEC.md의 G1~G5 구체적 게이트와는 다른 것, OFFICIAL_
   GATE_RESULT.md 0절에서 이미 확인된 사실) 재검토 필요. 그대로 두면
   봇이 영원히 "스킵"만 된다.
3. **`active_strategies` 필드 정리**(선택, 안전 이슈는 아님) — 라이브
   경로에서 안 쓰이는 필드가 config.yaml에 남아있으면 향후 다른
   사람(또는 다음 세션의 나)이 "이걸 바꾸면 filtered_trend를 끌 수
   있다"고 오해할 위험이 있다. 주석으로 "레거시 run_backtest() 전용,
   라이브 스케줄러는 안 읽음"을 명시하거나, 레거시 스크립트만 쓰는
   별도 섹션으로 분리하는 걸 권고(이번 문서는 조사만, 수정은 별도
   승인 후 진행).

이 세 가지 중 최소 1번(엔트리포인트 신설)은 G4를 물리적으로 시작하기
위한 필수 선행 작업이다.
