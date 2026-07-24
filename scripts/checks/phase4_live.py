"""
Phase 4 / G5 (실주문 로직) 전용 체크. 기존 harness_verify.py의 로직을 그대로 이관.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

MAX_LEVERAGE = 5.0
MIN_LEVERAGE = 0.5
MAX_SINGLE_TRADE_LOSS_PCT = 0.15
MAX_SLIPPAGE_BPS = 50
MAX_ERRORS_LAST_HOUR = 3


def _load_jsonl(path, since: datetime | None = None):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None:
                try:
                    ts = datetime.fromisoformat(r["ts"])
                except Exception:
                    continue
                if ts < since:
                    continue
            rows.append(r)
    return rows


def check_invariants(recent_trades, max_leverage=MAX_LEVERAGE, min_leverage=MIN_LEVERAGE, max_slippage_bps=MAX_SLIPPAGE_BPS):
    failures = []
    for r in recent_trades:
        if r.get("action") in ("enter_long", "enter_short"):
            lev = r.get("leverage")
            if lev is not None and not (min_leverage <= lev <= max_leverage):
                failures.append(f"leverage 범위 이탈: {lev} (symbol={r.get('symbol')}, ts={r.get('ts')})")
            if r.get("stop_price") is None:
                failures.append(f"stop_price 없이 진입: symbol={r.get('symbol')}, ts={r.get('ts')}")
            slip = r.get("slippage_bps")
            if slip is not None and abs(slip) > max_slippage_bps:
                failures.append(f"슬리피지 과다: {slip}bp (symbol={r.get('symbol')}, ts={r.get('ts')})")
    return failures


def check_regressions(test_path="tests/"):
    result = subprocess.run([sys.executable, "-m", "pytest", test_path, "-q"], capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-20:])
        return [f"pytest 실패 (exit={result.returncode}):\n{tail}"]
    return []


def check_anomalies(recent_trades, recent_errors, account_equity_usd, max_loss_pct=MAX_SINGLE_TRADE_LOSS_PCT, max_errors=MAX_ERRORS_LAST_HOUR):
    failures = []
    for r in recent_trades:
        pnl = r.get("pnl_realized")
        if pnl is not None and pnl < 0:
            loss_pct = abs(pnl) / account_equity_usd
            if loss_pct > max_loss_pct:
                failures.append(
                    f"단일 트레이드 손실 과다: {pnl} USD ({loss_pct:.1%}, symbol={r.get('symbol')}, ts={r.get('ts')})"
                )
    if len(recent_errors) > max_errors:
        failures.append(f"최근 1시간 에러 {len(recent_errors)}건 (임계 {max_errors})")
    return failures


def check_drift(recent_trades, backtest_expected_winrate=None):
    closed = [r for r in recent_trades if r.get("action") == "exit" and r.get("pnl_realized") is not None]
    if len(closed) < 20:
        return []
    wins = sum(1 for r in closed if r["pnl_realized"] > 0)
    winrate = wins / len(closed)
    if backtest_expected_winrate is not None and abs(winrate - backtest_expected_winrate) > 0.25:
        return [f"승률 드리프트: 실측 {winrate:.1%} vs 기대 {backtest_expected_winrate:.1%}"]
    return []


def run_all(config: dict):
    """
    config 예:
    {
        "log_path": "logs/trades.jsonl",
        "error_log_path": "logs/errors.jsonl",
        "account_equity_usd": 1000,
        "backtest_expected_winrate": 0.45,
    }
    G5 미완료(로그 없음) 상태에서는 checked_trades=0으로 항상 통과.
    """
    now = datetime.now(timezone.utc)
    log_path = config.get("log_path", "logs/trades.jsonl")
    error_log_path = config.get("error_log_path", "logs/errors.jsonl")
    account_equity_usd = config.get("account_equity_usd", 1000)

    recent_trades = _load_jsonl(log_path, since=now - timedelta(hours=6))
    recent_errors = _load_jsonl(error_log_path, since=now - timedelta(hours=1))

    failures = []
    failures += check_invariants(recent_trades)
    failures += check_regressions()
    failures += check_anomalies(recent_trades, recent_errors, account_equity_usd)
    failures += check_drift(recent_trades, config.get("backtest_expected_winrate"))
    return [f"[phase4] {x}" for x in failures]
