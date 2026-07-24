"""
트레이드/사이클 로그 기록 모듈.
봇 코드에서 매 사이클/매 트레이드마다 이 함수를 호출해서 JSONL로 남긴다.
"""
import json
import os
from datetime import datetime, timezone

LOG_PATH = os.environ.get("BOT_LOG_PATH", "logs/trades.jsonl")


def log_cycle(
    symbol: str,
    timeframe: str,
    signal: dict,          # {"strategy": "donchian_breakout"/"bb_rsi", "trend": "long"/"short"/"none", ...지표별 값}
    action: str,           # "enter_long" / "enter_short" / "exit" / "hold" / "skip"
    price: float,
    leverage: float | None = None,
    position_size_usd: float | None = None,
    stop_price: float | None = None,
    tp_price: float | None = None,
    pnl_realized: float | None = None,
    funding_fee: float | None = None,
    slippage_bps: float | None = None,
    order_id: str | None = None,
    extra: dict | None = None,
):
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "timeframe": timeframe,
        "signal": signal,
        "action": action,
        "price": price,
        "leverage": leverage,
        "position_size_usd": position_size_usd,
        "stop_price": stop_price,
        "tp_price": tp_price,
        "pnl_realized": pnl_realized,
        "funding_fee": funding_fee,
        "slippage_bps": slippage_bps,
        "order_id": order_id,
        "extra": extra or {},
    }
    os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def log_error(context: str, error: Exception, extra: dict | None = None):
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": "error",
        "context": context,
        "error": str(error),
        "error_type": type(error).__name__,
        "extra": extra or {},
    }
    err_path = os.environ.get("BOT_ERROR_LOG_PATH", "logs/errors.jsonl")
    os.makedirs(os.path.dirname(err_path) or ".", exist_ok=True)
    with open(err_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record
