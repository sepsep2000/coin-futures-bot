"""tests/test_notify_telegram_integration.py — src/notify/telegram.py 실제
텔레그램 봇 통합 테스트.

@pytest.mark.integration로 표시돼 기본 `pytest` 실행에서는 제외된다
(pytest.ini의 addopts). 실행하려면 `pytest -m integration`.

python-telegram-bot의 Bot.send_message()는 텔레그램 API가 요청을
거부하면(잘못된 토큰/chat_id, rate limit 등) TelegramError를 던진다 —
즉 send_message()가 예외 없이 True를 반환했다는 것 자체가 "텔레그램
API가 발신을 성공으로 응답했다"는 증거다(스크린샷으로 육안 확인하는
대신 이 사실에 의존한다, 태스크 STEP 2 요구사항)."""

from __future__ import annotations

import os

import pytest

from src.notify import telegram


@pytest.mark.integration
def test_real_telegram_send_message_delivered(capsys):
    result = telegram.send_message(
        "[RSIB integration test] src/notify/telegram.py send_message() 실제 발신 확인"
    )

    assert result is True

    # .env 비밀값이 출력 어디에도 노출되지 않았는지 확인
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    captured = capsys.readouterr()
    assert token and token not in captured.out and token not in captured.err
    assert chat_id and chat_id not in captured.out and chat_id not in captured.err
