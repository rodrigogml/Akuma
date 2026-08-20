from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .rpc import RpcServer
from .telegram_api import TelegramApiError, TelegramBotApi
from .telegram_vault import VaultResolutionError, VaultTokenResolver


DEFAULT_REPLY = "Não incomode o Akuma"
MAX_PAIR_TTL_SECONDS = 5 * 60
PAIR_RE = re.compile(r"^/(?:pair|painr)(?:@\w+)?\s+(\d{6})\s*$", re.IGNORECASE)
PAIR_COMMAND_RE = re.compile(r"^/(?:pair|painr)(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE)
MAX_PAIR_ATTEMPTS = 3
TOTP_COMMAND_RE = re.compile(r"^/totp(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE)
TOTP_PAGE_SIZE = 12
TOTP_SESSION_SECONDS = 3 * 60
TOTP_NOTICE_SECONDS = 8
TOTP_CODE_GRACE_SECONDS = 5
TYPING_REFRESH_SECONDS = 4
TOTP_BOT_COMMAND = {"command": "totp", "description": "Obter código de autenticação"}


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


@dataclass
class BotConfig:
    data: dict[str, Any]
    path: Path

    @property
    def bot_id(self) -> str:
        return str(self.data["id"])

    @property
    def listener(self) -> dict[str, Any]:
        return self.data.setdefault("listener", {})


class WebhookReceiver:
    def __init__(self, host: str, port: int, callback: Callable[[str, dict[str, Any], str | None], None]):
        self.callback = callback
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    parts = self.path.strip("/").split("/")
                    bot_id = parts[-1] if len(parts) >= 2 and parts[-2] == "telegram" else ""
                    receiver.callback(bot_id, payload, self.headers.get("X-Telegram-Bot-Api-Secret-Token"))
                    self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
                except Exception:
                    self.send_response(400); self.end_headers()

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.port = self.server.server_address[1]

    def start(self) -> None:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown(); self.server.server_close()


class BotRuntime:
    def __init__(self, manager: "TelegramManager", config: BotConfig):
        self.manager, self.config = manager, config
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.state = "stopped"
        self.last_error: str | None = None
        self.api: TelegramBotApi | None = None
        self.bot_identity: dict[str, Any] | None = None

    def _connect(self) -> None:
        token = self.manager.tokens.resolve(self.config.data["profile"])
        self.api = TelegramBotApi(token)
        self.bot_identity = self.api.get_me()

    def start(self) -> dict[str, Any]:
        if self.thread and self.thread.is_alive():
            return self.status()
        try:
            self._connect()
            mode = self.config.listener.get("mode", "polling")
            self.stop_event.clear()
            if mode == "polling":
                self.thread = threading.Thread(target=self._poll, name=f"telegram-{self.config.bot_id}", daemon=True)
            elif mode == "webhook":
                self._configure_webhook()
                self.thread = threading.Thread(target=self._webhook_wait, daemon=True)
            else:
                raise ValueError("listener.mode must be polling or webhook")
            self.thread.start(); self.state, self.last_error = "running", None
        except Exception as exc:
            self.state, self.last_error = "failed", str(exc)
        return self.status()

    def stop(self) -> dict[str, Any]:
        self.stop_event.set()
        if self.config.listener.get("mode", "polling") == "webhook" and self.api:
            try: self.api.delete_webhook()
            except TelegramApiError: pass
        if self.thread:
            self.thread.join(timeout=5)
        self.state = "stopped"
        return self.status()

    def _poll(self) -> None:
        state_path = self.manager.state_dir / f"{self.config.bot_id}.json"
        state = _read_json(state_path, {})
        offset = state.get("update_offset")
        timeout = int(self.config.listener.get("poll_timeout", 30))
        allowed = list(self.config.listener.get("allowed_updates") or ["message"])
        if self.config.data.get("totp", {}).get("enabled") and "callback_query" not in allowed:
            allowed.append("callback_query")
        while not self.stop_event.is_set():
            try:
                for update in self.api.get_updates(offset, timeout, allowed):  # type: ignore[union-attr]
                    offset = int(update["update_id"]) + 1
                    _write_json(state_path, {"update_offset": offset})
                    self.manager.process_update(self.config.bot_id, update)
            except Exception as exc:
                self.last_error = str(exc)
                self.stop_event.wait(5)

    def _configure_webhook(self) -> None:
        webhook = self.config.listener.get("webhook", {})
        url = webhook.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("webhook listener requires listener.webhook.url")
        allowed = list(self.config.listener.get("allowed_updates") or ["message"])
        if self.config.data.get("totp", {}).get("enabled") and "callback_query" not in allowed:
            allowed.append("callback_query")
        self.api.set_webhook(url, webhook.get("secret_token"), allowed)  # type: ignore[union-attr]

    def _webhook_wait(self) -> None:
        self.stop_event.wait()

    def send(self, chat_id: int | str, text: str, thread_id: int | None = None,
             reply_markup: dict[str, Any] | None = None, protect_content: bool = False) -> dict[str, Any]:
        if not self.api:
            self._connect()
        return self.api.send_message(chat_id, text, thread_id, reply_markup, protect_content)  # type: ignore[union-attr]

    def delete(self, chat_id: int | str, message_id: int) -> bool:
        if not self.api:
            self._connect()
        return self.api.delete_message(chat_id, message_id)  # type: ignore[union-attr]

    def answer_callback(self, callback_id: str, text: str | None = None) -> bool:
        if not self.api:
            self._connect()
        return self.api.answer_callback_query(callback_id, text)  # type: ignore[union-attr]

    def typing(self, chat_id: int | str) -> bool:
        if not self.api:
            self._connect()
        return self.api.send_chat_action(chat_id, "typing")  # type: ignore[union-attr]

    def set_owner_totp_command(self, owner_id: int) -> bool:
        if not self.api:
            self._connect()
        scope = {"type": "chat", "chat_id": owner_id}
        return self.api.set_my_commands([TOTP_BOT_COMMAND], scope)  # type: ignore[union-attr]

    def status(self) -> dict[str, Any]:
        return {"id": self.config.bot_id, "state": self.state,
                "listener_enabled": bool(self.config.listener.get("enabled", False)),
                "listener_mode": self.config.listener.get("mode", "polling"),
                "bot_username": (self.bot_identity or {}).get("username"),
                "last_error": self.last_error}


class TelegramManager:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.bots_dir = self.root / "bots"
        self.state_dir = self.root / "state"
        self.pairing_file = self.state_dir / "pairing.json"
        self.totp_cleanup_file = self.state_dir / "totp_cleanup.json"
        self.bots_dir.mkdir(parents=True, exist_ok=True); self.state_dir.mkdir(parents=True, exist_ok=True)
        self.tokens = VaultTokenResolver()
        self.bots: dict[str, BotRuntime] = {}
        for path in sorted(self.bots_dir.glob("*.json")):
            data = _read_json(path, {})
            if isinstance(data, dict) and data.get("id"):
                self.bots[str(data["id"])] = BotRuntime(self, BotConfig(data, path))
        self.webhook: WebhookReceiver | None = None
        self.stop_event = threading.Event()
        self.totp_sessions: dict[str, dict[str, Any]] = {}
        self.totp_timers: dict[str, threading.Timer] = {}
        self.totp_cleanup_lock = threading.Lock()
        pending = _read_json(self.totp_cleanup_file, {})
        self.totp_pending_deletions: dict[str, dict[str, Any]] = pending if isinstance(pending, dict) else {}

    def start(self) -> None:
        webhook_bots = [runtime for runtime in self.bots.values() if runtime.config.listener.get("mode") == "webhook"]
        if webhook_bots:
            manager_config = _read_json(self.root / "manager.json", {})
            receiver = manager_config.get("webhook_server", {})
            self.webhook = WebhookReceiver(receiver.get("host", "127.0.0.1"), int(receiver.get("port", 0)), self.process_webhook)
            self.webhook.start()
        for runtime in self.bots.values():
            if runtime.config.listener.get("enabled", False):
                runtime.start()
                self._configure_totp_commands(runtime)
        self._restore_totp_deletions()

    def _configure_totp_commands(self, runtime: BotRuntime) -> None:
        """Reconcile the TOTP menu exclusively in each owner's private chat."""
        if runtime.state != "running" or not runtime.config.data.get("totp", {}).get("enabled"):
            return
        for owner in runtime.config.data.get("owners", []):
            owner_id = owner.get("user_id") if isinstance(owner, dict) else None
            if isinstance(owner_id, int):
                try:
                    runtime.set_owner_totp_command(owner_id)
                except TelegramApiError as exc:
                    runtime.last_error = f"TOTP command configuration failed: {exc}"

    def shutdown(self) -> None:
        for timer in self.totp_timers.values(): timer.cancel()
        self.totp_timers.clear()
        for runtime in self.bots.values(): runtime.stop()
        if self.webhook: self.webhook.close()
        self.stop_event.set()

    def process_webhook(self, bot_id: str, update: dict[str, Any], secret: str | None) -> None:
        runtime = self.bots.get(bot_id)
        expected = runtime.config.listener.get("webhook", {}).get("secret_token") if runtime else None
        if not runtime or not expected or secret != expected: raise PermissionError("invalid webhook")
        self.process_update(bot_id, update)

    @staticmethod
    def _is_owner(runtime: BotRuntime, user_id: Any) -> bool:
        return any(owner.get("user_id") == user_id for owner in runtime.config.data.get("owners", []))

    @staticmethod
    def _totp_session_key(bot_id: str, user_id: Any, chat_id: Any) -> str:
        return f"{bot_id}:{user_id}:{chat_id}"

    def _delete_message(self, bot_id: str, chat_id: int | str, message_id: Any) -> None:
        if not isinstance(message_id, int): return
        key = f"{bot_id}:{chat_id}:{message_id}"
        with self.totp_cleanup_lock:
            if self.totp_pending_deletions.pop(key, None) is not None:
                _write_json(self.totp_cleanup_file, self.totp_pending_deletions)
        try: self.bots[bot_id].delete(chat_id, message_id)
        except TelegramApiError: pass

    def _schedule_delete(self, bot_id: str, chat_id: int | str, message_id: Any, seconds: float) -> None:
        if not isinstance(message_id, int): return
        key = f"{bot_id}:{chat_id}:{message_id}"
        previous = self.totp_timers.pop(key, None)
        if previous: previous.cancel()
        expires_at = time.time() + max(0, seconds)
        with self.totp_cleanup_lock:
            self.totp_pending_deletions[key] = {"bot_id": bot_id, "chat_id": chat_id, "message_id": message_id,
                                                "expires_at": expires_at}
            _write_json(self.totp_cleanup_file, self.totp_pending_deletions)
        def delete() -> None:
            self.totp_timers.pop(key, None)
            self._delete_message(bot_id, chat_id, message_id)
        timer = threading.Timer(max(0, seconds), delete)
        timer.daemon = True; self.totp_timers[key] = timer; timer.start()

    def _restore_totp_deletions(self) -> None:
        for key, pending in list(self.totp_pending_deletions.items()):
            if not isinstance(pending, dict):
                self.totp_pending_deletions.pop(key, None); continue
            try:
                bot_id, chat_id, message_id = pending["bot_id"], pending["chat_id"], int(pending["message_id"])
                seconds = float(pending["expires_at"]) - time.time()
            except (KeyError, TypeError, ValueError):
                self.totp_pending_deletions.pop(key, None); continue
            self._schedule_delete(str(bot_id), chat_id, message_id, seconds)
        with self.totp_cleanup_lock:
            _write_json(self.totp_cleanup_file, self.totp_pending_deletions)

    def _send_ephemeral(self, bot_id: str, chat_id: int | str, text: str, seconds: float,
                        reply_markup: dict[str, Any] | None = None, protect_content: bool = False) -> dict[str, Any]:
        sent = self.bots[bot_id].send(chat_id, text, reply_markup=reply_markup, protect_content=protect_content)
        self._schedule_delete(bot_id, chat_id, sent.get("message_id"), seconds)
        return sent

    @contextmanager
    def _typing(self, bot_id: str, chat_id: int | str):
        """Refresh Telegram's transient typing indicator during slow operations."""
        stop = threading.Event()

        def refresh() -> None:
            while not stop.wait(TYPING_REFRESH_SECONDS):
                try:
                    self.bots[bot_id].typing(chat_id)
                except TelegramApiError:
                    return

        try:
            self.bots[bot_id].typing(chat_id)
        except TelegramApiError:
            pass
        thread = threading.Thread(target=refresh, name=f"telegram-typing-{bot_id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=1)

    def _clear_totp_session(self, key: str) -> None:
        session = self.totp_sessions.pop(key, None)
        if not session: return
        for message_id in session.get("message_ids", []):
            self._delete_message(session["bot_id"], session["chat_id"], message_id)

    @staticmethod
    def _filter_totp_entries(entries: list[str], query: str) -> list[str]:
        if not query: return entries
        try:
            pattern = re.compile(".*".join(re.escape(part) for part in query.split("*")), re.IGNORECASE)
        except re.error:
            return []
        return [entry for entry in entries if pattern.search(entry)]

    def _totp_keyboard(self, session: dict[str, Any]) -> dict[str, Any]:
        entries = session["entries"]
        page = session["page"]
        start = page * TOTP_PAGE_SIZE
        rows = [[{"text": path.rsplit("/", 1)[-1], "callback_data": f"totp:{session['token']}:{index}"}]
                for index, path in enumerate(entries[start:start + TOTP_PAGE_SIZE], start)]
        navigation = []
        if page > 0: navigation.append({"text": "‹ Anterior", "callback_data": f"totp:{session['token']}:p"})
        if start + TOTP_PAGE_SIZE < len(entries): navigation.append({"text": "Próxima ›", "callback_data": f"totp:{session['token']}:n"})
        if navigation: rows.append(navigation)
        rows.append([{"text": "Cancelar", "callback_data": f"totp:{session['token']}:c"}])
        return {"inline_keyboard": rows}

    def _show_totp_page(self, session: dict[str, Any]) -> None:
        key = session["key"]
        self._clear_totp_session(key)
        self.totp_sessions[key] = session
        total = len(session["entries"])
        start = session["page"] * TOTP_PAGE_SIZE + 1
        end = min(total, session["page"] * TOTP_PAGE_SIZE + TOTP_PAGE_SIZE)
        sent = self._send_ephemeral(session["bot_id"], session["chat_id"], f"Selecione um TOTP ({start}–{end} de {total}).",
                                    TOTP_SESSION_SECONDS, self._totp_keyboard(session), protect_content=True)
        session["message_ids"] = [sent.get("message_id")]

    def _start_totp(self, bot_id: str, sender: dict[str, Any], chat: dict[str, Any], message: dict[str, Any], query: str) -> None:
        key = self._totp_session_key(bot_id, sender.get("id"), chat.get("id"))
        self._clear_totp_session(key)
        self._delete_message(bot_id, chat["id"], message.get("message_id"))
        session = {"key": key, "bot_id": bot_id, "user_id": sender["id"], "chat_id": chat["id"], "query": query,
                   "phase": "password", "expires_at": time.monotonic() + TOTP_SESSION_SECONDS, "message_ids": []}
        self.totp_sessions[key] = session
        sent = self._send_ephemeral(bot_id, chat["id"], "Envie a senha TOTP.", TOTP_SESSION_SECONDS, protect_content=True)
        session["message_ids"] = [sent.get("message_id")]

    def _receive_totp_password(self, session: dict[str, Any], message: dict[str, Any]) -> None:
        bot_id, chat_id = session["bot_id"], session["chat_id"]
        self._delete_message(bot_id, chat_id, message.get("message_id"))
        self._clear_totp_session(session["key"])
        try:
            with self._typing(bot_id, chat_id):
                config = self.bots[bot_id].config.data["totp"]
                profile = self.tokens.totp_profile(config["profile"])
                value = str(message.get("text") or "")
                fake = self.tokens.read(profile, profile["fake_password_entry"])
                real = self.tokens.read(profile, profile["real_password_entry"])
                if secrets.compare_digest(value, fake):
                    self._send_ephemeral(bot_id, chat_id, "Não existe nenhum TOTP cadastrado.", TOTP_NOTICE_SECONDS, protect_content=True)
                    return
                if not secrets.compare_digest(value, real):
                    self._send_ephemeral(bot_id, chat_id, "Senha inválida.", TOTP_NOTICE_SECONDS, protect_content=True)
                    return
                entries = sorted(self._filter_totp_entries(self.tokens.list_totp(profile), session["query"]), key=str.casefold)
        except (KeyError, VaultResolutionError):
            self._send_ephemeral(bot_id, chat_id, "TOTP indisponível.", TOTP_NOTICE_SECONDS, protect_content=True)
            return
        if not entries:
            self._send_ephemeral(bot_id, chat_id, "Não existe nenhum TOTP cadastrado.", TOTP_NOTICE_SECONDS, protect_content=True)
            return
        session.update({"phase": "selection", "profile": profile, "entries": entries, "page": 0,
                        "token": secrets.token_urlsafe(12), "message_ids": []})
        self._show_totp_page(session)

    def _handle_totp_callback(self, bot_id: str, callback: dict[str, Any]) -> None:
        runtime = self.bots[bot_id]
        sender = callback.get("from") or {}; message = callback.get("message") or {}
        chat = message.get("chat") or {}; data = callback.get("data") or ""
        if chat.get("type") != "private" or not self._is_owner(runtime, sender.get("id")):
            return
        pieces = data.split(":")
        if len(pieces) != 3 or pieces[0] != "totp": return
        key = self._totp_session_key(bot_id, sender.get("id"), chat.get("id"))
        session = self.totp_sessions.get(key)
        if (not session or session.get("phase") != "selection" or session.get("expires_at", 0) < time.monotonic()
                or not secrets.compare_digest(session.get("token", ""), pieces[1])):
            runtime.answer_callback(callback.get("id", ""), "Esta seleção expirou.")
            self._delete_message(bot_id, chat.get("id"), message.get("message_id")); return
        action = pieces[2]
        if action == "c":
            runtime.answer_callback(callback.get("id", "")); self._clear_totp_session(key)
            self._send_ephemeral(bot_id, chat["id"], "TOTP cancelado.", TOTP_NOTICE_SECONDS, protect_content=True); return
        if action in {"p", "n"}:
            session["page"] = max(0, session["page"] + (-1 if action == "p" else 1))
            runtime.answer_callback(callback.get("id", "")); self._show_totp_page(session); return
        try: index = int(action); entry = session["entries"][index]
        except (ValueError, IndexError):
            runtime.answer_callback(callback.get("id", ""), "Seleção inválida."); return
        self._clear_totp_session(key)
        runtime.answer_callback(callback.get("id", ""))
        try:
            with self._typing(bot_id, chat["id"]):
                code = self.tokens.current_totp(session["profile"], entry)
        except VaultResolutionError:
            self._send_ephemeral(bot_id, chat["id"], "TOTP indisponível.", TOTP_NOTICE_SECONDS, protect_content=True); return
        try: period = int(self.bots[bot_id].config.data.get("totp", {}).get("period_seconds", 30))
        except (TypeError, ValueError): period = 30
        period = max(1, period); remaining = period - (int(time.time()) % period)
        lifetime = remaining + TOTP_CODE_GRACE_SECONDS
        self._send_ephemeral(bot_id, chat["id"], code, lifetime, protect_content=False)
        self._send_ephemeral(bot_id, chat["id"], f"Expira em {remaining}s.", lifetime, protect_content=True)

    def process_update(self, bot_id: str, update: dict[str, Any]) -> None:
        runtime = self.bots[bot_id]
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self._handle_totp_callback(bot_id, callback)
            return
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict): return
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        text = message.get("text") or ""
        totp_config = runtime.config.data.get("totp")
        totp_enabled = isinstance(totp_config, dict) and totp_config.get("enabled") is True
        totp_command = TOTP_COMMAND_RE.match(text)
        private_owner = chat.get("type") == "private" and self._is_owner(runtime, sender.get("id"))
        session_key = self._totp_session_key(bot_id, sender.get("id"), chat.get("id"))
        session = self.totp_sessions.get(session_key)
        if totp_command:
            if private_owner and totp_enabled:
                self._start_totp(bot_id, sender, chat, message, (totp_command.group(1) or "").strip())
            return
        if session and private_owner:
            if session.get("expires_at", 0) < time.monotonic():
                self._delete_message(bot_id, chat.get("id"), message.get("message_id"))
                self._clear_totp_session(session_key)
                self._send_ephemeral(bot_id, chat["id"], "TOTP cancelado.", TOTP_NOTICE_SECONDS, protect_content=True)
            elif session.get("phase") == "password":
                self._receive_totp_password(session, message)
            else:
                self._delete_message(bot_id, chat.get("id"), message.get("message_id"))
                self._clear_totp_session(session_key)
                self._send_ephemeral(bot_id, chat["id"], "TOTP cancelado.", TOTP_NOTICE_SECONDS, protect_content=True)
            return
        pair_command = PAIR_COMMAND_RE.match(text)
        if pair_command:
            response = self._pair(bot_id, sender, (pair_command.group(1) or "").strip())
        elif not self._has_owners(runtime):
            response = "Este bot ainda não tem proprietário. Envie /pair [PIN DE CONFIGURAÇÃO] para concluir o pareamento."
        else:
            response = runtime.config.data.get("reply_text", DEFAULT_REPLY)
        runtime.send(chat.get("id"), response, message.get("message_thread_id"))

    @staticmethod
    def _has_owners(runtime: BotRuntime) -> bool:
        return bool(runtime.config.data.get("owners", []))

    def _pair(self, bot_id: str, sender: dict[str, Any], pin: str) -> str:
        pairing = _read_json(self.pairing_file, {})
        record = pairing.get(bot_id)
        digest = hashlib.sha256(f"{bot_id}:{pin}".encode()).hexdigest()
        if not record:
            return "Não há um pairing ativo para este bot. Solicite um novo PIN de configuração."
        if record.get("expires_at", 0) < time.time():
            pairing.pop(bot_id, None)
            _write_json(self.pairing_file, pairing)
            return "O PIN de pairing expirou. Solicite um novo PIN de configuração."
        if not secrets.compare_digest(record.get("hash", ""), digest):
            attempts = int(record.get("attempts", 0)) + 1
            if attempts >= MAX_PAIR_ATTEMPTS:
                pairing.pop(bot_id, None)
                _write_json(self.pairing_file, pairing)
                return "Pairing cancelado por excesso de tentativas com PIN incorreto. Solicite um novo PIN."
            record["attempts"] = attempts
            pairing[bot_id] = record
            _write_json(self.pairing_file, pairing)
            remaining = MAX_PAIR_ATTEMPTS - attempts
            suffix = "tentativa" if remaining == 1 else "tentativas"
            return f"PIN incorreto. Você ainda tem {remaining} {suffix}."
        config = self.bots[bot_id].config
        owners = config.data.setdefault("owners", [])
        if not any(owner.get("user_id") == sender.get("id") for owner in owners):
            owners.append({"user_id": sender.get("id"), "username": sender.get("username"), "display_name": sender.get("first_name"), "added_at": time.time(), "source": "pair"})
            _write_json(config.path, config.data)
        self._configure_totp_commands(self.bots[bot_id])
        pairing.pop(bot_id, None); _write_json(self.pairing_file, pairing)
        return "Pairing concluído com sucesso. Sua conta agora é owner deste bot."

    def pair_request(self, bot_id: str, ttl_seconds: int = 300) -> dict[str, Any]:
        if bot_id not in self.bots: raise ValueError("bot not found")
        ttl_seconds = max(1, min(int(ttl_seconds), MAX_PAIR_TTL_SECONDS))
        pin = f"{secrets.randbelow(1_000_000):06d}"
        pairing = _read_json(self.pairing_file, {})
        pairing[bot_id] = {"hash": hashlib.sha256(f"{bot_id}:{pin}".encode()).hexdigest(), "expires_at": time.time() + ttl_seconds, "attempts": 0}
        _write_json(self.pairing_file, pairing)
        return {"bot_id": bot_id, "pin": pin, "expires_in": ttl_seconds}

    def handle(self, method: str, params: dict[str, Any]) -> Any:
        if method == "ping": return {"service": "telegram-manager", "state": "running", "bots": len(self.bots)}
        if method == "telegram.bots": return [runtime.status() for runtime in self.bots.values()]
        bot_id = params.get("bot_id")
        if method == "telegram.bot.pair-request": return self.pair_request(bot_id, int(params.get("ttl_seconds", 300)))
        if method == "telegram.bot.owners": return self.bots[bot_id].config.data.get("owners", [])
        if method == "telegram.bot.start-listener": return self.bots[bot_id].start()
        if method == "telegram.bot.stop-listener": return self.bots[bot_id].stop()
        if method == "telegram.bot.status": return self.bots[bot_id].status()
        if method == "telegram.send":
            return self.bots[bot_id].send(params["chat_id"], params["text"], params.get("message_thread_id"))
        if method == "shutdown": self.shutdown(); return {"state": "stopping"}
        raise ValueError(f"unknown method: {method}")


def run(args: argparse.Namespace) -> None:
    manager = TelegramManager(args.data_dir)
    manager.start()
    rpc = RpcServer(args.host, args.port, args.token, manager.handle)
    try: rpc.serve_forever(manager.stop_event)
    finally: manager.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="akuma-telegram-manager")
    parser.add_argument("--data-dir", type=Path, required=True); parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True); parser.add_argument("--token", required=True)
    run(parser.parse_args(argv)); return 0


if __name__ == "__main__": raise SystemExit(main())
