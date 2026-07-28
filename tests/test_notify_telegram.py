"""tests/test_notify_telegram.py — src/notify/telegram.py 단위 테스트.

전부 telegram.Bot을 스텁으로 교체해 네트워크 호출 없이 검증한다. 실제
텔레그램 API를 쓰는 케이스는 tests/test_notify_telegram_integration.py로
분리(@pytest.mark.integration, 기본 pytest 실행에서 제외)."""

from __future__ import annotations

import pytest

from src.notify import telegram


class _FakeBotInstance:
    def __init__(self, calls: list, raise_exc: Exception | None = None, get_me_calls: list | None = None):
        self._calls = calls
        self._raise_exc = raise_exc
        self._get_me_calls = get_me_calls

    async def send_message(self, chat_id, text):
        if self._raise_exc is not None:
            raise self._raise_exc
        self._calls.append({"chat_id": chat_id, "text": text})

    async def get_me(self):
        if self._raise_exc is not None:
            raise self._raise_exc
        if self._get_me_calls is not None:
            self._get_me_calls.append(True)


class _FakeBotFactory:
    def __init__(self, calls: list, raise_exc: Exception | None = None, get_me_calls: list | None = None):
        self.calls = calls
        self.raise_exc = raise_exc
        self.get_me_calls = get_me_calls
        self.constructed_with_token: list[str] = []

    def __call__(self, token):
        self.constructed_with_token.append(token)
        return _FakeBotInstance(self.calls, self.raise_exc, self.get_me_calls)


@pytest.fixture
def env_credentials(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    # ENV_PATH가 실존 config/.env를 가리키더라도 monkeypatch.setenv가 이미 세팅한
    # 값을 load_dotenv(override=False 기본값)가 덮어쓰지 않는다.


# =====================================================================
# send_message
# =====================================================================

def test_send_message_success_calls_bot_with_correct_args(env_credentials, monkeypatch):
    calls: list = []
    monkeypatch.setattr(telegram, "Bot", _FakeBotFactory(calls))

    result = telegram.send_message("테스트 메시지")

    assert result is True
    assert calls == [{"chat_id": "555", "text": "테스트 메시지"}]


def test_send_message_explicit_chat_id_overrides_default(env_credentials, monkeypatch):
    calls: list = []
    monkeypatch.setattr(telegram, "Bot", _FakeBotFactory(calls))

    telegram.send_message("텍스트", chat_id="999")

    assert calls == [{"chat_id": "999", "text": "텍스트"}]


def test_send_message_empty_text_raises_before_any_network_call(env_credentials, monkeypatch):
    """빈 문자열은 호출부 버그(경계 케이스) — 네트워크 시도 전에 즉시 실패해야 한다."""
    factory = _FakeBotFactory([])
    monkeypatch.setattr(telegram, "Bot", factory)

    with pytest.raises(ValueError):
        telegram.send_message("")
    with pytest.raises(ValueError):
        telegram.send_message("   ")

    assert factory.constructed_with_token == []


def test_send_message_missing_credentials_returns_false_not_raise(monkeypatch, tmp_path):
    """CLAUDE.md 격리 원칙: 텔레그램 설정 누락이 거래 로직까지 크래시로
    전파되면 안 된다 — False + 로그로만 남는다."""
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setattr(telegram, "ENV_PATH", empty_env)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    result = telegram.send_message("테스트")

    assert result is False


def test_send_message_network_error_is_isolated_returns_false(env_credentials, monkeypatch, capsys):
    """실패를 예외로 전파하지 않는다(격리) — 대신 stderr에 사유가 남는다."""
    monkeypatch.setattr(telegram, "Bot", _FakeBotFactory([], raise_exc=ConnectionError("network down")))

    result = telegram.send_message("테스트")

    assert result is False
    captured = capsys.readouterr()
    assert "network down" in captured.err


def test_send_message_failure_does_not_leak_token_in_logs(env_credentials, monkeypatch, capsys):
    monkeypatch.setattr(telegram, "Bot", _FakeBotFactory([], raise_exc=RuntimeError("boom")))

    telegram.send_message("테스트")

    captured = capsys.readouterr()
    assert "fake-token-123" not in captured.out
    assert "fake-token-123" not in captured.err


# =====================================================================
# check_reachable — 2026-07-29 추가(알림 피로 방지: healthcheck가 15분마다
# 부르는데 send_message는 실제 채팅 메시지를 보내 하루 96번씩 쌓였다)
# =====================================================================

def test_check_reachable_success_calls_get_me_not_send_message(env_credentials, monkeypatch):
    """정상: get_me만 호출하고 send_message용 채팅 메시지는 전혀 안 보낸다."""
    calls: list = []
    get_me_calls: list = []
    monkeypatch.setattr(telegram, "Bot", _FakeBotFactory(calls, get_me_calls=get_me_calls))

    result = telegram.check_reachable()

    assert result is True
    assert get_me_calls == [True]
    assert calls == []  # send_message 경로 미사용


def test_check_reachable_missing_credentials_returns_false_not_raise(monkeypatch, tmp_path):
    """경계: 자격증명 누락 시 예외 대신 False(다른 CLAUDE.md 격리 원칙과 동일)."""
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setattr(telegram, "ENV_PATH", empty_env)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    assert telegram.check_reachable() is False


def test_check_reachable_network_error_is_isolated_returns_false(env_credentials, monkeypatch, capsys):
    """실패: get_me가 던지는 예외가 호출부로 전파되면 안 된다(격리)."""
    monkeypatch.setattr(telegram, "Bot", _FakeBotFactory([], raise_exc=ConnectionError("network down")))

    result = telegram.check_reachable()

    assert result is False
    captured = capsys.readouterr()
    assert "network down" in captured.err


# =====================================================================
# 특화 함수 — 포맷팅 + CRITICAL/일반 프리픽스 미혼동 확인
# =====================================================================

def test_send_critical_alert_prefixed_and_distinct_from_normal(env_credentials, monkeypatch):
    """★ 요구된 검증: CRITICAL과 일반 알림이 실수로 섞이지 않는지."""
    sent_texts: list = []
    monkeypatch.setattr(telegram, "send_message", lambda text, chat_id=None: sent_texts.append(text) or True)

    telegram.send_critical_alert("스탑 배치 실패")
    telegram.send_entry_exit_notification(
        symbol="ETH/USDT:USDT", strategy="filtered_trend", direction="long", regime="trend",
        qty=1.0, price=3000.0, r_multiple=None, cumulative_pnl_usd=100.0,
    )

    critical_text, entry_text = sent_texts
    assert critical_text.startswith("[CRITICAL]")
    assert not entry_text.startswith("[CRITICAL]")
    assert "스탑 배치 실패" in critical_text


def test_send_entry_exit_notification_entry_vs_exit_label(env_credentials, monkeypatch):
    sent_texts: list = []
    monkeypatch.setattr(telegram, "send_message", lambda text, chat_id=None: sent_texts.append(text) or True)

    telegram.send_entry_exit_notification(
        symbol="ETH/USDT:USDT", strategy="filtered_trend", direction="long", regime="trend",
        qty=1.0, price=3000.0, r_multiple=None, cumulative_pnl_usd=100.0,
    )
    telegram.send_entry_exit_notification(
        symbol="ETH/USDT:USDT", strategy="filtered_trend", direction="long", regime="trend",
        qty=1.0, price=3100.0, r_multiple=1.5, cumulative_pnl_usd=200.0,
    )

    entry_text, exit_text = sent_texts
    assert "진입" in entry_text and "청산" not in entry_text
    assert "청산" in exit_text
    assert "R배수" in exit_text and "R배수" not in entry_text


def test_send_rebalance_notification_formats_empty_lists_as_none(env_credentials, monkeypatch):
    sent_texts: list = []
    monkeypatch.setattr(telegram, "send_message", lambda text, chat_id=None: sent_texts.append(text) or True)

    telegram.send_rebalance_notification(
        new_long=["BTC"], new_short=[], exited_long=[], exited_short=["XRP"],
        turnover_pct=12.5, realized_pnl_usd=25.0,
    )

    text = sent_texts[0]
    assert "BTC" in text
    assert "없음" in text  # new_short/exited_long 빈 리스트
    assert "XRP" in text


def test_send_daily_summary_includes_per_leg_breakdown(env_credentials, monkeypatch):
    sent_texts: list = []
    monkeypatch.setattr(telegram, "send_message", lambda text, chat_id=None: sent_texts.append(text) or True)

    telegram.send_daily_summary(
        total_trades=5, total_pnl_usd=150.0, win_rate_pct=60.0,
        current_positions=[{"symbol": "ETH/USDT:USDT", "strategy": "filtered_trend", "direction": "long", "qty": 1.0}],
        regime_state="trend", filtered_trend_pnl_usd=100.0, two_a_pnl_usd=50.0,
    )

    text = sent_texts[0]
    assert "filtered_trend" in text and "2a" in text
    assert "ETH/USDT:USDT" in text


# =====================================================================
# handle_command — 이번 태스크 범위 밖, 골격 유지 확인
# =====================================================================

def test_handle_command_still_not_implemented():
    with pytest.raises(NotImplementedError):
        telegram.handle_command(update=None)
