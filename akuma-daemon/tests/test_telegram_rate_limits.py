from io import BytesIO
from urllib.error import HTTPError
from unittest.mock import patch

import pytest

from akuma_daemon.telegram_api import TelegramBotApi, TelegramRateLimitError
from akuma_daemon.telegram_manager import OutboundRateLimiter


def test_api_exposes_retry_after_from_telegram_429():
    payload = b'{"ok":false,"error_code":429,"parameters":{"retry_after":7}}'
    error = HTTPError("https://api.telegram.org/bot/token/sendMessage", 429, "Too Many Requests", {}, BytesIO(payload))
    with patch("akuma_daemon.telegram_api.urlopen", side_effect=error), pytest.raises(TelegramRateLimitError) as raised:
        TelegramBotApi("token").send_message(1, "hello")
    assert raised.value.retry_after == 7
    assert raised.value.method == "sendMessage"


def test_outbound_rate_limiter_spaces_private_group_and_global_messages():
    now = [0.0]
    sleeps = []
    limiter = OutboundRateLimiter(clock=lambda: now[0], sleeper=lambda delay: sleeps.append(delay))

    limiter.reserve_message(1)
    limiter.reserve_message(1)
    limiter.reserve_message(-100)

    assert sleeps == [1.0, 1.04]
    assert limiter.chat_next["1"] == 2.0
    assert limiter.chat_next["-100"] == 4.04


def test_outbound_rate_limiter_honors_rate_limit_block():
    now = [10.0]
    sleeps = []
    limiter = OutboundRateLimiter(clock=lambda: now[0], sleeper=lambda delay: sleeps.append(delay))
    limiter.block(9)
    limiter.reserve_message(1)
    assert sleeps == [9.0]
