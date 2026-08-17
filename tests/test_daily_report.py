"""tests/test_daily_report.py — scripts/daily_report.py 단위 테스트.

거래소는 전부 스텁으로 대체(네트워크 없음). 사고/정상 실현PnL 3줄
표시(2026-08-17 추가)를 중심으로 검증 - 계산 자체는
tests/test_incident_pnl.py가 이미 커버하므로, 여기서는 "daily_report가
그 결과를 올바르게 리포트 텍스트에 반영/전달하는지"만 확인한다."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import daily_report  # noqa: E402
from src.live import state as live_state  # noqa: E402


class _StubExchange:
    def __init__(self, balance_usdt: float, positions: list[dict], income_records: list[dict]):
        self._balance_usdt = balance_usdt
        self._positions = positions
        self._income_records = income_records
        self.income_calls: list[dict] = []

    def fetch_balance(self):
        return {"total": {"USDT": self._balance_usdt}}

    def fetch_positions(self):
        return self._positions

    def fapiPrivateGetIncome(self, params):
        self.income_calls.append(params)
        return self._income_records  # 짧은 페이지 - 페이지네이션 루프 1회로 종료


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "state.db"
    live_state.init_db(path)
    return path


_SAMPLE_CLASSIFICATION = {
    "grand_total": 75.9083,
    "accident_total": 158.065,
    "normal_total": -82.1567,
    "accident_breakdown": {},
    "unmatched_impact_incidents": [],
}


# =====================================================================
# build_pnl_reconciliation_lines — 정상/경계
# =====================================================================

def test_build_pnl_reconciliation_lines_shows_three_lines():
    lines = daily_report.build_pnl_reconciliation_lines(_SAMPLE_CLASSIFICATION)
    text = "\n".join(lines)

    assert "전체: $75.91" in text
    assert "사고분: $158.07" in text or "사고분: $158.06" in text  # 반올림
    assert "순수 전략: $-82.16" in text


def test_build_pnl_reconciliation_lines_warns_on_unmatched_incident(capsys):
    """경계: 등록된 사고인데 이번 조회 구간에 매칭 레코드가 0건이면(시간창
    오기입 의심) stderr에 경고를 남긴다 - 텔레그램 발신 자체는 막지 않음."""
    classification = dict(_SAMPLE_CLASSIFICATION, unmatched_impact_incidents=["some_incident"])

    daily_report.build_pnl_reconciliation_lines(classification)

    captured = capsys.readouterr()
    assert "some_incident" in captured.err


# =====================================================================
# build_report_text — pnl 정산 3줄이 실제 리포트에 포함되는지
# =====================================================================

def test_build_report_text_includes_pnl_reconciliation_section(db_path):
    exchange = _StubExchange(balance_usdt=5000.0, positions=[], income_records=[])

    text = daily_report.build_report_text(
        cfg={}, exchange=exchange, db_path=db_path,
        now_utc=pd.Timestamp("2026-08-17T23:00:00Z"),
        pnl_classification=_SAMPLE_CLASSIFICATION,
    )

    assert "[실현 PnL 정산 (G4 누적)]" in text
    assert "순수 전략: $-82.16" in text


# =====================================================================
# fetch_pnl_classification — 레지스트리 로드 + income 조회 + 분류 연결
# =====================================================================

def test_fetch_pnl_classification_uses_registry_g4_start_as_query_start(monkeypatch, tmp_path):
    incidents_yaml = tmp_path / "incidents.yaml"
    incidents_yaml.write_text(
        "g4_start: '2026-07-25T15:14:51Z'\n"
        "incidents:\n"
        "  - id: sample_incident\n"
        "    start: '2026-08-11T13:00:00Z'\n"
        "    end: '2026-08-11T13:20:00Z'\n"
        "    symbols: ['ADAUSDT']\n"
        "    pnl_impact: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(daily_report, "INCIDENTS_PATH", incidents_yaml)

    income_records = [
        {"time": 1786453500000, "incomeType": "REALIZED_PNL", "income": "12.89", "symbol": "ADAUSDT"},  # 2026-08-11T13:05:00Z
    ]
    exchange = _StubExchange(balance_usdt=0.0, positions=[], income_records=income_records)

    result = daily_report.fetch_pnl_classification(exchange)

    assert result["accident_total"] == 12.89
    # G4 시작 시각(2026-07-25T15:14:51Z)이 ms로 정확히 변환돼 조회에 쓰였는지
    import datetime
    expected_start_ms = int(
        datetime.datetime(2026, 7, 25, 15, 14, 51, tzinfo=datetime.timezone.utc).timestamp() * 1000
    )
    assert exchange.income_calls[0]["startTime"] == expected_start_ms


def test_fetch_pnl_classification_no_incidents_registered_returns_all_normal(monkeypatch, tmp_path):
    """실패/경계: 레지스트리에 사고가 하나도 없으면(신규 프로젝트 등) 전부
    정상으로 처리되고 예외가 나지 않아야 한다."""
    incidents_yaml = tmp_path / "incidents.yaml"
    incidents_yaml.write_text("g4_start: '2026-07-25T15:14:51Z'\nincidents: []\n", encoding="utf-8")
    monkeypatch.setattr(daily_report, "INCIDENTS_PATH", incidents_yaml)

    income_records = [
        {"time": 1786547105000, "incomeType": "REALIZED_PNL", "income": "5.0", "symbol": "ETHUSDT"},
    ]
    exchange = _StubExchange(balance_usdt=0.0, positions=[], income_records=income_records)

    result = daily_report.fetch_pnl_classification(exchange)

    assert result["accident_total"] == 0.0
    assert result["normal_total"] == 5.0
