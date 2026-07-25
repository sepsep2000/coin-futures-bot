# PAPER_TRADING_READINESS.md — 페이퍼 트레이딩 착수 전 최종 체크리스트

STEP 1(OOS_PORTFOLIO_CLUSTER_VERIFICATION.md)이 **PASS**(468개 독립
클러스터 ≥ 300)로 확인됐으므로, 아래 4개 항목의 완료 상태를 명시한다.

## 체크리스트

| # | 항목 | 상태 | 근거 문서 |
|---|---|---|---|
| 1 | 사후 필터링 버그 감사 완료 | **완료** | POST_FILTER_BUG_AUDIT.md — 오염 범위 확정, 재실행 결과 두 판정(SIGNAL_VALIDATION_1a, FILTER_VALIDATION_1a) 모두 변화 없음(회귀 확인) |
| 2 | 이원 게이트 구조 승인 완료 | **완료** | GATE_STRUCTURE_PROPOSAL_FINAL.md(승인됨), SPEC.md G1.5 명문화 |
| 3 | vol-parity 배분 가중치 정정 반영 완료 | **완료** | VOL_PARITY_RECALC.md, config.yaml portfolio.weights(83.8:16.2), 게이트 통과 여부 불변 확인 |
| 4 | OOS≥300 기준 확정 및 통과 확인 | **완료** | SPEC.md 4절 각주1(방법론: G1.5와 동일 클러스터 정의, 포트폴리오 레벨 단순 합산), OOS_PORTFOLIO_CLUSTER_VERIFICATION.md(실측 468개, 두 방식 모두 300 초과) |

**4개 항목 전부 완료.**

## 현재 게이트 상태 요약 (정정된 vol-parity 83.8:16.2 기준)

| 게이트 | 판정 |
|---|---|
| G1 무결성 | PASS |
| G1.5 개별 신호(2a, filtered_trend/1a) | PASS(둘 다 SIGNAL_VALIDATION_*.md에서 클러스터 블록부트스트랩 유의성 확인) |
| G2(Sharpe≥1.0 AND Calmar≥1.0 AND MaxDD≤15% AND OOS≥300) | PASS(Sharpe 2.03, Calmar 5.59, MaxDD 4.41%, 클러스터 468개) |
| G3(몬테카를로 MaxDD 95%ile≤20%) | PASS(9.11%) |

## 범위 밖 참고사항 (이 체크리스트에는 포함되지 않음, 착수 전 별도 확인 필요)

이 문서는 태스크에서 지정한 4개 항목(신호/포트폴리오 검증 계열)만
다룬다. **G4(페이퍼 트레이딩) 자체를 실행하려면 별도의 라이브 실행
인프라가 필요한데, 이번 세션 범위에서 그 존재 여부를 확인한 결과
아직 구현되지 않았다**:

- `src/live/`, `src/notify/` 디렉터리는 존재하나 내부에 `.py` 파일이
  하나도 없다(빈 placeholder).
- `scripts/healthcheck.py`(SPEC.md 6절이 요구하는 config 로드/API
  연결/텔레그램 발신 확인 스크립트)가 존재하지 않는다.
- 주문 실행·상태 sqlite 영속화·텔레그램 알림(SPEC.md 5절) 관련 코드
  없음.

즉 "신호/포트폴리오 검증"은 4/4 완료됐지만, **"페이퍼 트레이딩을 실제로
시작하는 것"은 이 검증과 별개로 상당한 분량의 미착수 구현 작업(Phase 4)이
남아있다.** 이 문서는 그 구현 작업의 착수 여부를 판단하지 않는다 —
사실만 보고한다.
