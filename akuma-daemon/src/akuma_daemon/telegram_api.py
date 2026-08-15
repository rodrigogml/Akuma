from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class TelegramApiError(RuntimeError):
    pass


class TelegramBotApi:
    def __init__(self, token: str, timeout: float = 30):
        self._base = f"https://api.telegram.org/bot{token}/"
        self.timeout = timeout

    def call(self, method: str, **params: Any) -> Any:
        encoded = {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                   for key, value in params.items() if value is not None}
        request = Request(self._base + method, data=urlencode(encoded).encode("utf-8"), method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise TelegramApiError(f"Telegram API request failed: {type(exc).__name__}") from exc
        if not payload.get("ok"):
            raise TelegramApiError(f"Telegram API rejected {method}: {payload.get('description', 'unknown error')}")
        return payload.get("result")

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

    def get_updates(self, offset: int | None, timeout: int, allowed_updates: list[str] | None = None) -> list[dict[str, Any]]:
        return self.call("getUpdates", offset=offset, timeout=timeout, allowed_updates=allowed_updates) or []

    def set_webhook(self, url: str, secret_token: str | None = None, allowed_updates: list[str] | None = None) -> bool:
        return bool(self.call("setWebhook", url=url, secret_token=secret_token, allowed_updates=allowed_updates))

    def delete_webhook(self, drop_pending_updates: bool = False) -> bool:
        return bool(self.call("deleteWebhook", drop_pending_updates=drop_pending_updates))

    def send_message(self, chat_id: int | str, text: str, message_thread_id: int | None = None) -> dict[str, Any]:
        return self.call("sendMessage", chat_id=chat_id, text=text, message_thread_id=message_thread_id)
