"""tests/test_incident_pnl.py — src/live/incident_pnl.py 단위 테스트.

전부 순수 데이터(레코드 리스트 + incidents 정의 dict)만으로 검증한다 -
거래소/파일 I/O 없음(모듈 자체가 순수함수라는 설계를 그대로 반영)."""

from __future__ import annotations

from pathlib import Path

from src.live import incident_pnl


def _income(time_iso: str, income_type: str, amount: float, symbol: str = "ETHUSDT") -> dict:
    import datetime as dt
    ts_ms = int(dt.datetime.fromisoformat(time_iso.replace("Z", "+00:00")).timestamp() * 1000)
    return {"time": ts_ms, "incomeType": income_type, "income": str(amount), "symbol": symbol}


# =====================================================================
# classify — 정상
# =====================================================================

def test_classify_separates_accident_and_normal_pnl():
    incidents = [{
        "id": "test_incident",
        "start": "2026-08-11T13:00:00Z",
        "end": "2026-08-11T13:20:00Z",
        "symbols": ["ADAUSDT"],
        "pnl_impact": True,
    }]
    records = [
        _income("2026-08-11T13:05:00Z", "REALIZED_PNL", 12.89, "ADAUSDT"),  # 사고분
        _income("2026-08-16T00:00:25Z", "REALIZED_PNL", 7.70, "DOTUSDT"),   # 정상분
    ]

    result = incident_pnl.classify(records, incidents)

    assert result["grand_total"] == 20.59
    assert result["accident_total"] == 12.89
    assert result["normal_total"] == 7.70
    assert result["accident_breakdown"]["test_incident"] == 12.89


# =====================================================================
# classify — 경계
# =====================================================================

def test_classify_half_open_window_excludes_end_boundary():
    """[start, end) 반열림 - end 시각과 정확히 같은 레코드는 사고에 포함 안 됨."""
    incidents = [{
        "id": "window_test",
        "start": "2026-08-11T13:00:00Z",
        "end": "2026-08-11T13:20:00Z",
        "symbols": ["ADAUSDT"],
        "pnl_impact": True,
    }]
    records = [
        _income("2026-08-11T13:00:00Z", "REALIZED_PNL", 1.0, "ADAUSDT"),  # start 포함
        _income("2026-08-11T13:20:00Z", "REALIZED_PNL", 2.0, "ADAUSDT"),  # end 제외
    ]

    result = incident_pnl.classify(records, incidents)

    assert result["accident_total"] == 1.0
    assert result["normal_total"] == 2.0


def test_classify_symbol_mismatch_within_window_stays_normal():
    """시간창은 겹쳐도 등록된 symbols에 없으면 사고로 분류하지 않는다
    (넓은 시간창이 무관한 심볼의 정상 활동까지 삼키는 것 방지)."""
    incidents = [{
        "id": "eth_only",
        "start": "2026-07-26T23:15:00Z",
        "end": "2026-07-27T12:53:00Z",
        "symbols": ["ETHUSDT"],
        "pnl_impact": True,
    }]
    records = [
        _income("2026-07-27T08:00:00Z", "FUNDING_FEE", -0.5, "AAVEUSDT"),  # 같은 시간대, 다른 심볼
    ]

    result = incident_pnl.classify(records, incidents)

    assert result["accident_total"] == 0.0
    assert result["normal_total"] == -0.5


def test_classify_pnl_impact_false_always_zero_even_if_matched():
    """pnl_impact: false인 사고는 매칭돼도 accident_total에 더해지지 않는다
    (문서화 전용 등록, 예: parquet 손상처럼 실거래 영향 없는 사고). symbols를
    생략(None)하면 시간창만으로 매칭 - 실제 관련 심볼이 있는 인프라 장애를
    가정한 케이스."""
    incidents = [{
        "id": "infra_only",
        "start": "2026-07-27T14:30:00Z",
        "end": "2026-07-27T21:40:00Z",
        "pnl_impact": False,
    }]
    records = [
        _income("2026-07-27T15:00:00Z", "FUNDING_FEE", -1.23, "ETHUSDT"),
    ]

    result = incident_pnl.classify(records, incidents)

    assert result["accident_total"] == 0.0
    assert result["normal_total"] == -1.23
    assert result["accident_breakdown"]["infra_only"] == -1.23  # 서브합계는 참고용으로 그대로 기록


def test_classify_empty_symbols_list_never_matches():
    """symbols: [] (명시적으로 빈 리스트)는 None과 다르다 - 특정 심볼이
    없는 사고(예: 실제 체결 자체가 없는 순수 인프라 장애)를 등록할 때
    쓰며, 시간창이 겹쳐도 절대 매칭되지 않아야 한다. symbols 생략(None)을
    "제한 없음"으로 오인하면 넓은 시간창이 무관한 정상 활동을 전부
    삼키는 사고로 이어진다(실측으로 발견한 버그의 회귀 테스트)."""
    incidents = [{
        "id": "no_symbol_incident",
        "start": "2026-07-27T14:30:00Z",
        "end": "2026-07-27T21:40:00Z",
        "symbols": [],
        "pnl_impact": False,
    }]
    records = [
        _income("2026-07-27T15:00:00Z", "REALIZED_PNL", -70.99, "ETHUSDT"),
    ]

    result = incident_pnl.classify(records, incidents)

    assert result["accident_breakdown"]["no_symbol_incident"] == 0.0
    assert result["normal_total"] == -70.99


# =====================================================================
# classify — 실패/경고 신호
# =====================================================================

def test_classify_flags_unmatched_pnl_impact_incident():
    """조회 구간에 매칭 레코드가 0건인 pnl_impact 사고는 조용히 무시하지
    않고 unmatched_impact_incidents로 반환 - 시간창 오기입이나 조회 범위
    누락을 조기에 발견하기 위함."""
    incidents = [{
        "id": "never_matched",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-01T00:01:00Z",
        "symbols": ["BTCUSDT"],
        "pnl_impact": True,
    }]
    records = [
        _income("2026-08-11T13:05:00Z", "REALIZED_PNL", 1.0, "ADAUSDT"),
    ]

    result = incident_pnl.classify(records, incidents)

    assert result["unmatched_impact_incidents"] == ["never_matched"]


def test_classify_empty_incidents_list_treats_everything_as_normal():
    result = incident_pnl.classify(
        [_income("2026-08-11T13:05:00Z", "REALIZED_PNL", 5.0, "ADAUSDT")], []
    )

    assert result["accident_total"] == 0.0
    assert result["normal_total"] == 5.0
    assert result["unmatched_impact_incidents"] == []


# =====================================================================
# load_incidents
# =====================================================================

def test_load_incidents_reads_yaml_registry(tmp_path):
    yaml_path = tmp_path / "incidents.yaml"
    yaml_path.write_text(
        "g4_start: '2026-07-25T15:14:51Z'\n"
        "incidents:\n"
        "  - id: sample\n"
        "    start: '2026-01-01T00:00:00Z'\n"
        "    end: '2026-01-01T01:00:00Z'\n"
        "    symbols: []\n"
        "    pnl_impact: true\n",
        encoding="utf-8",
    )

    data = incident_pnl.load_incidents(yaml_path)

    assert data["g4_start"] == "2026-07-25T15:14:51Z"
    assert data["incidents"][0]["id"] == "sample"


def test_load_incidents_real_registry_file_parses_without_error():
    """실제 reports/incidents.yaml이 형식 오류 없이 파싱되는지 - 등록된
    5개 사고 전부 필수 필드(id/start/end/pnl_impact)를 갖는지 확인."""
    project_root = Path(__file__).resolve().parent.parent
    data = incident_pnl.load_incidents(project_root / "reports" / "incidents.yaml")

    assert data["g4_start"] is not None
    assert len(data["incidents"]) == 5
    for incident in data["incidents"]:
        assert "id" in incident and "start" in incident and "end" in incident
        assert "pnl_impact" in incident
