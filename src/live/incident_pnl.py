"""src/live/incident_pnl.py — 사고 이벤트 레지스트리 기반 실현PnL 자동 분리.

`reports/incidents.yaml`에 등록된 시간창+심볼을 기준으로 Binance income
원장(REALIZED_PNL/COMMISSION/FUNDING_FEE)을 "사고분"/"정상분"으로
분류한다. 새 사고가 확정되면 incidents.yaml에 항목만 추가하면 이 모듈이
다음 호출부터 자동 반영한다 - 코드 수정 불필요.

★ CLAUDE.md 2절(전략 로직은 순수함수) 원칙을 이 모듈에도 동일하게
적용한다: 거래소/파일 I/O는 전혀 하지 않는다. income 레코드 리스트와
이미 파싱된 incidents 리스트를 인자로 받아 분류 결과만 반환한다.
income 원장 조회는 executor.fetch_income_since(), incidents.yaml 파일
읽기는 load_incidents()가 각각 I/O 경계에서 담당한다.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Optional

import yaml

INCOME_TYPES = ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")


def load_incidents(path: Path) -> dict:
    """incidents.yaml을 읽어 그대로 dict로 반환한다(파싱 전용, 분류 로직
    없음 - I/O와 순수 계산을 분리하는 이 모듈의 원칙 그대로)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        "g4_start": data.get("g4_start"),
        "incidents": data.get("incidents", []),
    }


def _parse_ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _income_ts(record: dict) -> dt.datetime:
    return dt.datetime.fromtimestamp(record["time"] / 1000, tz=dt.timezone.utc)


def _matches_incident(record: dict, incident: dict) -> bool:
    start = _parse_ts(incident["start"])
    end = _parse_ts(incident["end"])
    if not (start <= _income_ts(record) < end):
        return False
    # symbols 키 자체가 없으면(None) 심볼 제한 없이 시간창만으로 매칭한다.
    # symbols: [] 처럼 "명시적으로 빈 리스트"는 정반대 의미 - 특정 심볼이
    # 없는 사고(예: 순수 인프라 장애로 실제 체결이 없는 경우)라 아예
    # 매칭되지 않아야 한다는 뜻이므로 항상 False. 이 둘을 구분하지 않으면
    # 빈 리스트를 "제한 없음"으로 잘못 해석해 넓은 시간창이 무관한 정상
    # 활동 전부를 그 사고로 끌어들이는 사고가 난다(개발 중 실측으로 발견).
    symbols = incident.get("symbols")
    if symbols is not None and record.get("symbol") not in symbols:
        return False
    return True


def classify(income_records: list[dict], incidents: list[dict]) -> dict:
    """income_records를 incidents 정의에 따라 분류한다.

    반환:
        grand_total: 전체 실현PnL(REALIZED_PNL+COMMISSION+FUNDING_FEE 합)
        accident_total: 사고분 합계(pnl_impact=false 항목은 0으로 취급)
        normal_total: grand_total - accident_total ("순수 전략 수익")
        accident_breakdown: {incident_id: 서브합계} (pnl_impact 무관 전체 등록)
        unmatched_impact_incidents: 이 기간에 매칭 레코드가 0건인
            pnl_impact=true 사고 id 리스트(등록은 됐지만 이번 조회 구간
            밖이거나 시간창이 잘못됐을 가능성 - 조용히 넘기지 않고 반환)
    """
    grand_total = 0.0
    accident_total = 0.0
    breakdown: dict[str, float] = {i["id"]: 0.0 for i in incidents}
    matched_count: dict[str, int] = {i["id"]: 0 for i in incidents}

    for record in income_records:
        if record.get("incomeType") not in INCOME_TYPES:
            continue
        amount = float(record["income"])
        grand_total += amount

        for incident in incidents:
            if not _matches_incident(record, incident):
                continue
            breakdown[incident["id"]] += amount
            matched_count[incident["id"]] += 1
            if incident.get("pnl_impact", True):
                accident_total += amount
            break  # 사고 구간은 서로 겹치지 않게 등록한다고 가정(레지스트리 작성 규칙)

    normal_total = grand_total - accident_total
    unmatched_impact_incidents = [
        i["id"] for i in incidents
        if i.get("pnl_impact", True) and matched_count[i["id"]] == 0
    ]

    return {
        "grand_total": round(grand_total, 4),
        "accident_total": round(accident_total, 4),
        "normal_total": round(normal_total, 4),
        "accident_breakdown": {k: round(v, 4) for k, v in breakdown.items()},
        "unmatched_impact_incidents": unmatched_impact_incidents,
    }
