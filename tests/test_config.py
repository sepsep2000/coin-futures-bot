"""tests/test_config.py — config/config.yaml 값 자체에 대한 회귀 테스트.

★ 2026-08-17 추가: account.initial_equity_usd가 실제 G4 시작 자본과
다른 값(오염된 플레이스홀더)으로 되돌아가는 걸 막기 위한 회귀 테스트.
2026-07-27 사고 조사에서 이미 한 번 발견됐던 문제(당시엔 "사이징엔 안
쓰이니 우선순위 낮음"이라며 comment로만 남기고 방치 - G4_KILLSWITCH_
INCIDENT_ANALYSIS.md 4절)가 3주 뒤 텔레그램 진입 알림의 "누적 PnL"에
다시 노출되어("-$5,544.36", 실제로는 +$93.77) 재발했다. 이 값은 코드
로직으로 자동 검증할 방법이 없는 순수 데이터 값이라, 앞으로 다시
플레이스홀더로 덮어써지는 걸 테스트로 고정해둔다."""

from __future__ import annotations

from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.yaml"

# G4(현재 실거래 세션) 시작 직후 첫 equity_snapshots 실측값
# (data/state.db, ts='2026-07-25T15:00:00+00:00') - reports/
# G4_KILLSWITCH_INCIDENT_ANALYSIS.md 4.1절에서 최초 확인, 2026-08-17
# 재확인. data/state.db는 gitignore 대상이라 테스트에서 직접 재조회할
# 수 없어 문서화된 실측값을 상수로 고정한다.
G4_START_EQUITY_USD = 5165.94304781

# 2026-07-27 사고 조사에서 발견된 오염값 - 이 정확한 숫자로 돌아가면
# 안 된다(다른 이유로 우연히 이 값이 될 일은 없으므로 "이 값이면 곧
# 회귀"라고 직접 판정 가능).
KNOWN_BAD_PLACEHOLDER = 10804.0669


def _load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def test_initial_equity_usd_matches_real_g4_start_equity():
    cfg = _load_config()

    assert cfg["account"]["initial_equity_usd"] == G4_START_EQUITY_USD


def test_initial_equity_usd_is_not_the_known_contaminated_placeholder():
    """경계/회귀: 2026-07-27에 발견됐던 그 오염값(10804.0669)으로 다시
    바뀌면 텔레그램 진입/청산 알림의 누적 PnL 부호까지 뒤집히는 사고가
    재발한다(2026-08-17 실측: -$5,544.36으로 표시됐으나 실제는 +$93.77)."""
    cfg = _load_config()

    assert cfg["account"]["initial_equity_usd"] != KNOWN_BAD_PLACEHOLDER
