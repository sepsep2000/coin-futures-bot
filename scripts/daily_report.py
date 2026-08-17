"""scripts/daily_report.py — 텔레그램 일일 잔고/포지션 리포트.

★★★ scheduler.py의 메인 루프와 별도 프로세스로 cron 실행한다(healthcheck.py와
동일 원칙) — 봇 프로세스에 새 로직을 얹지 않고, 순수 조회 전용 스크립트로
분리한다(이 스크립트가 죽거나 늦어도 매매 로직에 영향 없음).

★ 포지션의 실제 존재/수량/방향/notional/미실현PnL은 전부 거래소 실측
(`exchange.fetch_positions()`)을 근거로 한다(reports/G4_KILLSWITCH_INCIDENT_
ANALYSIS.md 2.3절 원칙 재적용 — 로컬 DB를 신뢰의 근거로 쓰지 않는다).
"전략별 구분 표시"만 로컬 DB(`state.db`의 positions.strategy)를 참고용으로
대조한다 — 거래소는 "이게 2a인지 filtered_trend인지" 자체를 모르는 정보이므로
DB 조회가 유일한 방법이지만, 만약 DB에 대응 레코드가 없으면(사고 때처럼
DB가 오염/누락된 경우) "전략 미상(DB 미기록)"으로 명시하지 절대 추측하지
않는다.

★ 2026-08-17 추가(사용자 요청): "사고 제외 순수 realized PnL" 3줄
(전체/사고분/순수분)을 매일 자동 표시 - reports/incidents.yaml 사고
레지스트리 기반(src/live/incident_pnl.py, 순수함수). 새 사고가 확정되면
레지스트리에 항목만 추가하면 다음 실행부터 자동 반영되고, 이 스크립트
코드는 수정할 필요가 없다.

사용:
    python scripts/daily_report.py
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.live import executor  # noqa: E402
from src.live import incident_pnl  # noqa: E402
from src.live import state as live_state  # noqa: E402
from src.live.state import resolve_db_path  # noqa: E402
from src.notify import telegram  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
INCIDENTS_PATH = PROJECT_ROOT / "reports" / "incidents.yaml"
KST_OFFSET_HOURS = 9  # UTC 23:00 실행 = KST 08:00(다음날) — cron 등록 시각과 일치


def _load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def _strategy_label(db_positions_by_symbol: dict, symbol: str) -> str:
    pos = db_positions_by_symbol.get(symbol)
    return pos["strategy"] if pos else "전략 미상(DB 미기록)"


def fetch_pnl_classification(exchange) -> dict:
    """incidents.yaml 등록 사고 기준으로 G4 시작~현재 실현PnL을 사고/정상
    분리한다 - I/O(income 원장 조회, 레지스트리 파일 읽기)는 여기서만
    하고, 분류 계산 자체는 incident_pnl.classify()(순수함수)에 위임."""
    registry = incident_pnl.load_incidents(INCIDENTS_PATH)
    g4_start_dt = datetime.datetime.fromisoformat(registry["g4_start"].replace("Z", "+00:00"))
    start_ms = int(g4_start_dt.timestamp() * 1000)
    income_records = executor.fetch_income_since(exchange, start_ms)
    return incident_pnl.classify(income_records, registry["incidents"])


def build_pnl_reconciliation_lines(classification: dict) -> list[str]:
    """fetch_pnl_classification() 결과를 리포트 3줄로 포맷 - 숫자 계산과
    텍스트 포맷을 분리해 네트워크 없이 단위테스트 가능하게 한다. 매칭
    레코드가 0건인 등록 사고가 있으면(시간창 오기입 등 조기 발견 목적)
    경고를 stderr에 남긴다(CLAUDE.md "예외를 조용히 삼키지 않는다" 원칙 -
    텔레그램 알림 자체를 막지는 않되 반드시 어딘가엔 남긴다)."""
    unmatched = classification.get("unmatched_impact_incidents") or []
    if unmatched:
        print(
            f"[daily_report] 경고: incidents.yaml에 등록된 사고 중 이번 조회 구간에서 "
            f"매칭된 income 레코드가 0건인 항목이 있음(시간창/심볼 오기입 의심): {unmatched}",
            file=sys.stderr,
        )
    return [
        "",
        "[실현 PnL 정산 (G4 누적)]",
        f"전체: ${classification['grand_total']:,.2f}",
        f"사고분: ${classification['accident_total']:,.2f}",
        f"순수 전략: ${classification['normal_total']:,.2f}",
    ]


def build_report_text(cfg: dict, exchange, db_path: Path, now_utc, pnl_classification: dict) -> str:
    """실제 발신 전, 포맷/숫자를 독립적으로 테스트할 수 있게 텍스트 생성과
    발신을 분리한다(send_message는 네트워크 호출이라 단위테스트 어려움)."""
    balance = exchange.fetch_balance()
    usdt_balance = float(balance["total"]["USDT"])

    real_positions = [
        p for p in exchange.fetch_positions()
        if p.get("contracts") and float(p["contracts"]) != 0
    ]
    db_positions_by_symbol = {p["symbol"]: p for p in live_state.load_open_positions(db_path)}

    kst_now = now_utc + pd.Timedelta(hours=KST_OFFSET_HOURS)
    date_str = kst_now.strftime("%Y-%m-%d")

    lines = [
        f"📊 일일 리포트 (KST {date_str} 08:00)",
        "",
        f"잔고(USDT): ${usdt_balance:,.2f}",
        "",
        "[보유 포지션]",
    ]

    if not real_positions:
        lines.append("보유 포지션 없음")
        total_unrealized = 0.0
    else:
        lines.append("심볼 | 전략 | 방향 | 수량 | notional(USDT) | 미실현PnL")
        total_unrealized = 0.0
        for p in real_positions:
            symbol = p["symbol"]
            qty = float(p["contracts"])
            direction = p.get("side") or "unknown"
            notional = float(p.get("notional") or 0.0)
            unrealized = float(p.get("unrealizedPnl") or 0.0)
            total_unrealized += unrealized
            strategy = _strategy_label(db_positions_by_symbol, symbol)
            lines.append(
                f"{symbol} | {strategy} | {direction} | {qty} | ${notional:,.2f} | ${unrealized:,.2f}"
            )

    lines.append("")
    lines.append(f"미실현 PnL 합계: ${total_unrealized:,.2f}")

    lines.extend(build_pnl_reconciliation_lines(pnl_classification))

    return "\n".join(lines)


def main() -> bool:
    cfg = _load_config()
    db_path = resolve_db_path(cfg, project_root=PROJECT_ROOT)
    exchange = executor.get_authenticated_exchange(testnet=(cfg["mode"] == "testnet"))
    exchange.load_markets()

    now_utc = pd.Timestamp.now(tz="UTC")
    pnl_classification = fetch_pnl_classification(exchange)
    text = build_report_text(cfg, exchange, db_path, now_utc, pnl_classification)
    return telegram.send_message(text)


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
