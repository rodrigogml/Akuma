from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class TelegramApiError(RuntimeError):
    pass


class TelegramRateLimitError(TelegramApiError):
    def __init__(self, retry_after: int, method: str):
        self.retry_after = retry_after
        self.method = method
        super().__init__(f"Telegram rate limit for {method}; retry after {retry_after} seconds")


class TelegramBotApi:
    def __init__(self, token: str, timeout: float = 30):
        self._base = f"https://api.telegram.org/bot{token}/"
        self._file_base = f"https://api.telegram.org/file/bot{token}/"
        self.timeout = timeout

    def call(self, method: str, **params: Any) -> Any:
        encoded = {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                   for key, value in params.items() if value is not None}
        request = Request(self._base + method, data=urlencode(encoded).encode("utf-8"), method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
                retry_after = self._retry_after(payload)
                if exc.code == 429 and retry_after is not None:
                    raise TelegramRateLimitError(retry_after, method) from exc
                description = payload.get("description", f"HTTP {exc.code}")
            except TelegramRateLimitError:
                raise
            except Exception:
                description = f"HTTP {exc.code}"
            raise TelegramApiError(f"Telegram API rejected {method}: {description}") from exc
        except Exception as exc:
            raise TelegramApiError(f"Telegram API request failed: {type(exc).__name__}") from exc
        if not payload.get("ok"):
            retry_after = self._retry_after(payload)
            if retry_after is not None:
                raise TelegramRateLimitError(retry_after, method)
            raise TelegramApiError(f"Telegram API rejected {method}: {payload.get('description', 'unknown error')}")
        return payload.get("result")

    @staticmethod
    def _retry_after(payload: Any) -> int | None:
        if not isinstance(payload, dict):
            return None
        parameters = payload.get("parameters")
        value = parameters.get("retry_after") if isinstance(parameters, dict) else None
        if isinstance(value, int) and value > 0:
            return value
        return None

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

    def get_updates(self, offset: int | None, timeout: int, allowed_updates: list[str] | None = None) -> list[dict[str, Any]]:
        return self.call("getUpdates", offset=offset, timeout=timeout, allowed_updates=allowed_updates) or []

    def set_webhook(self, url: str, secret_token: str | None = None, allowed_updates: list[str] | None = None) -> bool:
        return bool(self.call("setWebhook", url=url, secret_token=secret_token, allowed_updates=allowed_updates))

    def delete_webhook(self, drop_pending_updates: bool = False) -> bool:
        return bool(self.call("deleteWebhook", drop_pending_updates=drop_pending_updates))

    def set_my_commands(self, commands: list[dict[str, str]], scope: dict[str, Any]) -> bool:
        return bool(self.call("setMyCommands", commands=commands, scope=scope))

    def get_my_commands(self, scope: dict[str, Any]) -> list[dict[str, Any]]:
        return self.call("getMyCommands", scope=scope) or []

    def send_message(self, chat_id: int | str, text: str, message_thread_id: int | None = None,
                     reply_markup: dict[str, Any] | None = None, protect_content: bool = False) -> dict[str, Any]:
        return self.call("sendMessage", chat_id=chat_id, text=text, message_thread_id=message_thread_id,
                         reply_markup=reply_markup, protect_content=protect_content)

    def delete_message(self, chat_id: int | str, message_id: int) -> bool:
        return bool(self.call("deleteMessage", chat_id=chat_id, message_id=message_id))

    def edit_message_text(self, chat_id: int | str, message_id: int, text: str,
                          reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.call("editMessageText", chat_id=chat_id, message_id=message_id,
                         text=text, reply_markup=reply_markup)

    def get_file(self, file_id: str) -> dict[str, Any]:
        return self.call("getFile", file_id=file_id)

    def download_file(self, file_id: str, destination: Path, maximum_bytes: int) -> Path:
        metadata = self.get_file(file_id)
        size = metadata.get("file_size")
        if isinstance(size, int) and size > maximum_bytes:
            raise TelegramApiError("Telegram file exceeds configured download limit")
        file_path = metadata.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            raise TelegramApiError("Telegram did not provide a downloadable file path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = Request(self._file_base + file_path, method="GET")
        written = 0
        try:
            with urlopen(request, timeout=self.timeout) as response, destination.open("wb") as output:
                while chunk := response.read(64 * 1024):
                    written += len(chunk)
                    if written > maximum_bytes:
                        raise TelegramApiError("Telegram file exceeds configured download limit")
                    output.write(chunk)
        except Exception as exc:
            destination.unlink(missing_ok=True)
            if isinstance(exc, TelegramApiError):
                raise
            raise TelegramApiError(f"Telegram file download failed: {type(exc).__name__}") from exc
        return destination

    def answer_callback_query(self, callback_query_id: str, text: str | None = None,
                              show_alert: bool = False) -> bool:
        return bool(self.call("answerCallbackQuery", callback_query_id=callback_query_id,
                              text=text, show_alert=show_alert))

    def send_chat_action(self, chat_id: int | str, action: str = "typing") -> bool:
        return bool(self.call("sendChatAction", chat_id=chat_id, action=action))
