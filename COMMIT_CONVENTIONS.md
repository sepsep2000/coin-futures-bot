# COMMIT_CONVENTIONS.md — 커밋 규칙

## 커밋 단위 분리

성격이 다르면 반드시 커밋을 나눈다. 하나의 커밋에 서로 다른 성격을 섞지 않는다.

1. **전략/로직 코드 + 관련 테스트** — `src/strategy/*`, `src/backtest/engine.py`,
   `src/risk.py` 등 전략·엔진 로직과 그 로직을 검증하는 테스트. 같은 변경으로
   묶인 테스트는 코드와 한 커밋에 포함한다.
2. **문서/리포트 산출물** — `SPEC.md`, `PROGRESS.md`, `*_BASELINE.md`,
   `*_REPORT.md` 등 사람이 읽는 기록/결정 문서.
3. **인프라(하네스, 스크립트, config)** — `scripts/`, `config/*.yaml`,
   `harness_config.example.json`, `.gitignore` 등 실행/운영 도구.

## 생성물은 커밋하지 않는다

`logs/`, `reports/`(런타임 산출 HTML/상태 파일), `*.db` 등 실행할 때마다
재생성되는 파일은 `.gitignore`에 넣고 커밋 대상에서 제외한다. 예외: 사람이
직접 작성/의도적으로 남기는 요약 리포트(`BACKTEST_BASELINE.md`,
`DATA_REPORT.md` 등)는 문서로 취급해 커밋한다.

## 커밋 메시지 형식

- 제목: `feat(scope): 요약` / `docs: 요약` / `chore(scope): 요약`
  - `feat` — 전략/엔진 로직 변경
  - `docs` — 문서·리포트만
  - `chore` — 인프라/스크립트/설정/gitignore 등 로직 아닌 변경
- 본문: 핵심 수치·근거를 bullet로 (예: 게이트 판정 수치, 발견한 버그, 테스트
  통과 수). "왜"가 드러나야 한다 — "무엇을 바꿨다"보다 "왜 바꿨다"가 우선.

## 커밋 시점 판단

앞으로 커밋 단위를 어떻게 나눌지 매번 사용자에게 묻지 않는다. 위 규칙대로
스스로 판단해서 `git add` + `git commit`까지 실행한다. 애매하면 `git log`의
가장 최근 커밋 히스토리 패턴을 따라간다.
