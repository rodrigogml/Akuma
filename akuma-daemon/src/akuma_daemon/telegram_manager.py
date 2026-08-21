from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import random
import re
import secrets
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .rpc import RpcServer
from .telegram_api import TelegramApiError, TelegramBotApi, TelegramRateLimitError
from .telegram_agent import AgentConfigurationError, TelegramAgentRuntime, write_json
from .telegram_speech import voice_transcription_settings
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
NEW_BOT_COMMAND = {"command": "new", "description": "Iniciar uma nova conversa"}
CONFIG_BOT_COMMAND = {"command": "config", "description": "Configurar esta conversa"}
NEW_COMMAND_RE = re.compile(r"^/new(?:@\w+)?\s*$", re.IGNORECASE)
CONFIG_COMMAND_RE = re.compile(r"^/config(?:@\w+)?\s*$", re.IGNORECASE)
CONFIG_SESSION_SECONDS = 3 * 60
OUTBOUND_GLOBAL_MESSAGES_PER_SECOND = 25
OUTBOUND_PRIVATE_MESSAGE_INTERVAL_SECONDS = 1.0
OUTBOUND_GROUP_MESSAGE_INTERVAL_SECONDS = 3.0
POLL_TIMEOUT_MARGIN_SECONDS = 10
POLL_FAILURE_MAX_BACKOFF_SECONDS = 60


class OutboundRateLimiter:
    """Reserve outgoing message slots per bot without exceeding Telegram's published limits."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep):
        self.clock = clock
        self.sleeper = sleeper
        self.lock = threading.Lock()
        self.global_next = 0.0
        self.blocked_until = 0.0
        self.chat_next: dict[str, float] = {}

    @staticmethod
    def _interval(chat_id: int | str) -> float:
        try:
            return OUTBOUND_GROUP_MESSAGE_INTERVAL_SECONDS if int(chat_id) < 0 else OUTBOUND_PRIVATE_MESSAGE_INTERVAL_SECONDS
        except (TypeError, ValueError):
            return OUTBOUND_PRIVATE_MESSAGE_INTERVAL_SECONDS

    def reserve_message(self, chat_id: int | str) -> None:
        key = str(chat_id)
        with self.lock:
            now = self.clock()
            scheduled = max(now, self.blocked_until, self.global_next, self.chat_next.get(key, 0.0))
            self.global_next = scheduled + (1.0 / OUTBOUND_GLOBAL_MESSAGES_PER_SECOND)
            self.chat_next[key] = scheduled + self._interval(chat_id)
        delay = scheduled - self.clock()
        if delay > 0:
            self.sleeper(delay)

    def block(self, retry_after: int) -> None:
        with self.lock:
            self.blocked_until = max(self.blocked_until, self.clock() + max(1, retry_after))


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
        self.agent: TelegramAgentRuntime | None = None
        self.outbound_limiter = OutboundRateLimiter()

    def _connect(self) -> None:
        profile = self.config.data["profile"]
        if isinstance(profile, str) and not Path(profile).is_absolute():
            profile = str((self.config.path.parent / profile).resolve())
        token = self.manager.tokens.resolve(profile)
        poll_timeout = int(self.config.listener.get("poll_timeout", 30))
        api_timeout = max(30, poll_timeout + POLL_TIMEOUT_MARGIN_SECONDS) if self.config.listener.get("mode", "polling") == "polling" else 30
        self.api = TelegramBotApi(token, timeout=api_timeout)
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
            if self.agent and self.agent.enabled:
                self.agent.start()
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
        if self.agent:
            self.agent.suspend()
        self.state = "stopped"
        return self.status()

    def _poll(self) -> None:
        state_path = (self.agent.paths.state_dir if self.agent else self.manager.state_dir / self.config.bot_id) / "listener.json"
        state = _read_json(state_path, {})
        offset = state.get("update_offset")
        timeout = int(self.config.listener.get("poll_timeout", 30))
        allowed = list(self.config.listener.get("allowed_updates") or ["message"])
        if (self.config.data.get("totp", {}).get("enabled") or self.config.data.get("agent", {}).get("enabled")) and "callback_query" not in allowed:
            allowed.append("callback_query")
        failures = 0
        while not self.stop_event.is_set():
            try:
                for update in self.api.get_updates(offset, timeout, allowed):  # type: ignore[union-attr]
                    offset = int(update["update_id"]) + 1
                    _write_json(state_path, {"update_offset": offset})
                    self.manager.process_update(self.config.bot_id, update)
                failures = 0
                self.last_error = None
            except TelegramRateLimitError as exc:
                self.last_error = str(exc)
                self.stop_event.wait(exc.retry_after)
            except Exception as exc:
                failures += 1
                self.last_error = str(exc)
                delay = min(POLL_FAILURE_MAX_BACKOFF_SECONDS, 2 ** min(failures - 1, 6)) + random.uniform(0.0, 0.5)
                self.stop_event.wait(delay)

    def _configure_webhook(self) -> None:
        webhook = self.config.listener.get("webhook", {})
        url = webhook.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("webhook listener requires listener.webhook.url")
        allowed = list(self.config.listener.get("allowed_updates") or ["message"])
        if (self.config.data.get("totp", {}).get("enabled") or self.config.data.get("agent", {}).get("enabled")) and "callback_query" not in allowed:
            allowed.append("callback_query")
        self.api.set_webhook(url, webhook.get("secret_token"), allowed)  # type: ignore[union-attr]

    def _webhook_wait(self) -> None:
        self.stop_event.wait()

    def send(self, chat_id: int | str, text: str, thread_id: int | None = None,
             reply_markup: dict[str, Any] | None = None, protect_content: bool = False) -> dict[str, Any]:
        if not self.api:
            self._connect()
        return self._outbound(lambda: self.api.send_message(chat_id, text, thread_id, reply_markup, protect_content), chat_id, message=True)  # type: ignore[union-attr]

    def delete(self, chat_id: int | str, message_id: int) -> bool:
        if not self.api:
            self._connect()
        return self._outbound(lambda: self.api.delete_message(chat_id, message_id))  # type: ignore[union-attr]

    def answer_callback(self, callback_id: str, text: str | None = None, show_alert: bool = False) -> bool:
        if not self.api:
            self._connect()
        return self._outbound(lambda: self.api.answer_callback_query(callback_id, text, show_alert))  # type: ignore[union-attr]

    def edit(self, chat_id: int | str, message_id: int, text: str,
             reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.api:
            self._connect()
        return self._outbound(lambda: self.api.edit_message_text(chat_id, message_id, text, reply_markup))  # type: ignore[union-attr]

    def download(self, file_id: str, destination: Path, maximum_bytes: int) -> Path:
        if not self.api:
            self._connect()
        return self.api.download_file(file_id, destination, maximum_bytes)  # type: ignore[union-attr]

    def typing(self, chat_id: int | str) -> bool:
        if not self.api:
            self._connect()
        return self._outbound(lambda: self.api.send_chat_action(chat_id, "typing"))  # type: ignore[union-attr]

    def set_owner_commands(self, owner_id: int) -> bool:
        if not self.api:
            self._connect()
        scope = {"type": "chat", "chat_id": owner_id}
        commands: list[dict[str, str]] = []
        if self.config.data.get("agent", {}).get("enabled"):
            commands.extend([NEW_BOT_COMMAND, CONFIG_BOT_COMMAND])
        if self.config.data.get("totp", {}).get("enabled"):
            commands.append(TOTP_BOT_COMMAND)
        return self._outbound(lambda: self.api.set_my_commands(commands, scope))  # type: ignore[union-attr]

    def _outbound(self, operation: Callable[[], Any], chat_id: int | str | None = None, message: bool = False) -> Any:
        for attempt in range(2):
            if message and chat_id is not None:
                self.outbound_limiter.reserve_message(chat_id)
            try:
                return operation()
            except TelegramRateLimitError as exc:
                self.outbound_limiter.block(exc.retry_after)
                if attempt:
                    raise
        raise RuntimeError("unreachable")

    def set_owner_totp_command(self, owner_id: int) -> bool:
        return self.set_owner_commands(owner_id)

    def status(self) -> dict[str, Any]:
        return {"id": self.config.bot_id, "state": self.state,
                "listener_enabled": bool(self.config.listener.get("enabled", False)),
                "listener_mode": self.config.listener.get("mode", "polling"),
                "bot_username": (self.bot_identity or {}).get("username"),
                "last_error": self.last_error,
                "agent": self.agent.status() if self.agent else {"enabled": False, "state": "disabled"}}


class TelegramManager:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.project_root = self._find_project_root(self.root)
        self.bots_dir = self.project_root / "configs" / "telegram" / "bots" if self.project_root else self.root / "bots"
        self.legacy_bots_dir = self.root / "bots"
        self.state_dir = self.root / "state"
        self.bots_dir.mkdir(parents=True, exist_ok=True); self.legacy_bots_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.tokens = VaultTokenResolver()
        self.bots: dict[str, BotRuntime] = {}
        paths = list(sorted(self.bots_dir.glob("*/bot.json")))
        paths.extend(path for path in sorted(self.legacy_bots_dir.glob("*.json"))
                     if path.name != "manager.json" and not any(existing.parent.name == path.stem for existing in paths))
        for path in paths:
            data = _read_json(path, {})
            if isinstance(data, dict) and data.get("id"):
                config = BotConfig(data, path)
                self._migrate_contacts(config)
                runtime = BotRuntime(self, config)
                try:
                    runtime.agent = TelegramAgentRuntime(runtime, self.project_root or self.root)
                except AgentConfigurationError as exc:
                    runtime.last_error = str(exc)
                self.bots[str(data["id"])] = runtime
        self.webhook: WebhookReceiver | None = None
        self.stop_event = threading.Event()
        self.totp_sessions: dict[str, dict[str, Any]] = {}
        self.totp_timers: dict[str, threading.Timer] = {}
        self.totp_cleanup_lock = threading.Lock()
        self.totp_pending_deletions: dict[str, dict[str, Any]] = {}
        for bot_id in self.bots:
            pending = _read_json(self._bot_state_file(bot_id, "totp_cleanup.json"), {})
            if isinstance(pending, dict):
                self.totp_pending_deletions.update(pending)
        self.config_sessions: dict[str, dict[str, Any]] = {}
        self.config_timers: dict[str, threading.Timer] = {}

    @staticmethod
    def _find_project_root(root: Path) -> Path | None:
        resolved = root.resolve()
        for candidate in (resolved, *resolved.parents):
            if (candidate / "AGENTS.md").exists() and (candidate / "akuma-daemon").is_dir():
                return candidate
        return None

    def _migrate_contacts(self, config: BotConfig) -> None:
        modern = config.path.name.casefold() == "bot.json"
        contacts_path = config.path.parent / "contacts.json" if modern else config.path.with_name(f"{config.path.stem}.contacts.json")
        legacy = config.data.get("owners")
        if not isinstance(legacy, list):
            return
        existing = _read_json(contacts_path, {"version": 1, "contacts": []})
        contacts = list(existing.get("contacts", [])) if isinstance(existing, dict) and isinstance(existing.get("contacts"), list) else []
        known = {item.get("telegram_user_id") for item in contacts if isinstance(item, dict)}
        for owner in legacy:
            if not isinstance(owner, dict):
                continue
            if owner.get("user_id") in known:
                continue
            contacts.append({
                "telegram_user_id": owner.get("user_id"), "username": owner.get("username"),
                "display_name": owner.get("display_name"), "roles": ["owner"],
                "added_at": owner.get("added_at", time.time()), "source": owner.get("source", "legacy"),
            })
        _write_json(contacts_path, {"version": 1, "contacts": contacts})
        backup = config.path.with_suffix(config.path.suffix + ".owners.bak")
        if not backup.exists():
            shutil.copy2(config.path, backup)
        config.data.pop("owners", None)
        _write_json(config.path, config.data)

    def _bot_state_file(self, bot_id: str, name: str) -> Path:
        runtime = self.bots.get(bot_id)
        if runtime and runtime.agent:
            base = runtime.agent.paths.state_dir
        elif runtime:
            base = runtime.config.path.parent / "state"
        else:
            base = self.state_dir / bot_id
        base.mkdir(parents=True, exist_ok=True)
        return base / name

    def _save_totp_pending(self, bot_id: str) -> None:
        values = {key: value for key, value in self.totp_pending_deletions.items() if value.get("bot_id") == bot_id}
        _write_json(self._bot_state_file(bot_id, "totp_cleanup.json"), values)

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
                self._configure_owner_commands(runtime)
        self._restore_totp_deletions()

    def _configure_owner_commands(self, runtime: BotRuntime) -> None:
        """Reconcile native commands exclusively in each owner's private chat."""
        if runtime.state != "running":
            return
        for owner in self._owners(runtime):
            owner_id = owner.get("telegram_user_id") if isinstance(owner, dict) else None
            if isinstance(owner_id, int):
                try:
                    runtime.set_owner_commands(owner_id)
                except TelegramApiError as exc:
                    runtime.last_error = f"command configuration failed: {exc}"

    def _configure_totp_commands(self, runtime: BotRuntime) -> None:
        self._configure_owner_commands(runtime)

    def shutdown(self) -> None:
        for timer in self.totp_timers.values(): timer.cancel()
        self.totp_timers.clear()
        for timer in self.config_timers.values(): timer.cancel()
        self.config_timers.clear()
        for runtime in self.bots.values():
            if runtime.agent:
                runtime.agent.close()
        for runtime in self.bots.values(): runtime.stop()
        if self.webhook: self.webhook.close()
        self.stop_event.set()

    def process_webhook(self, bot_id: str, update: dict[str, Any], secret: str | None) -> None:
        runtime = self.bots.get(bot_id)
        expected = runtime.config.listener.get("webhook", {}).get("secret_token") if runtime else None
        if not runtime or not expected or secret != expected: raise PermissionError("invalid webhook")
        self.process_update(bot_id, update)

    @staticmethod
    def _owners(runtime: BotRuntime) -> list[dict[str, Any]]:
        return runtime.agent.contacts.owners() if runtime.agent else []

    @staticmethod
    def _is_owner(runtime: BotRuntime, user_id: Any) -> bool:
        return runtime.agent.contacts.is_owner(user_id) if runtime.agent else False

    @staticmethod
    def _totp_session_key(bot_id: str, user_id: Any, chat_id: Any) -> str:
        return f"{bot_id}:{user_id}:{chat_id}"

    def _delete_message(self, bot_id: str, chat_id: int | str, message_id: Any) -> None:
        if not isinstance(message_id, int): return
        key = f"{bot_id}:{chat_id}:{message_id}"
        with self.totp_cleanup_lock:
            if self.totp_pending_deletions.pop(key, None) is not None:
                self._save_totp_pending(bot_id)
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
            self._save_totp_pending(bot_id)
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
            for bot_id in self.bots:
                self._save_totp_pending(bot_id)

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

    @staticmethod
    def _config_keyboard(token: str, settings: dict[str, bool], submenu: bool = False) -> tuple[str, dict[str, Any]]:
        if not submenu:
            return "Configurações do bot", {"inline_keyboard": [
                [{"text": "Pensamentos", "callback_data": f"cfg:{token}:thoughts"}],
                [{"text": "Fechar", "callback_data": f"cfg:{token}:close"}],
            ]}
        shared = "☑" if settings["share_thoughts"] else "☐"
        deleted = "☑" if settings["delete_thoughts"] else "☐"
        return "Configurações › Pensamentos", {"inline_keyboard": [
            [{"text": f"{shared} Compartilha Pensamentos", "callback_data": f"cfg:{token}:share"}],
            [{"text": f"{deleted} Excluir Pensamentos", "callback_data": f"cfg:{token}:delete"}],
            [{"text": "‹ Voltar", "callback_data": f"cfg:{token}:back"}],
            [{"text": "Fechar", "callback_data": f"cfg:{token}:close"}],
        ]}

    def _open_config(self, bot_id: str, chat_id: int | str, user_id: int) -> None:
        runtime = self.bots[bot_id]
        if not runtime.agent:
            return
        key = runtime.agent.store.key(chat_id)
        token = secrets.token_urlsafe(12)
        text, keyboard = self._config_keyboard(token, runtime.agent.store.settings(key))
        sent = runtime.send(chat_id, text, reply_markup=keyboard, protect_content=True)
        message_id = sent.get("message_id") if isinstance(sent, dict) else None
        if not isinstance(message_id, int):
            return
        self.config_sessions[token] = {
            "bot_id": bot_id, "chat_id": chat_id, "user_id": user_id,
            "message_id": message_id, "expires_at": time.monotonic() + CONFIG_SESSION_SECONDS,
        }
        timer = threading.Timer(CONFIG_SESSION_SECONDS, self._close_config, args=(token,))
        timer.daemon = True; self.config_timers[token] = timer; timer.start()

    def _close_config(self, token: str) -> None:
        session = self.config_sessions.pop(token, None)
        timer = self.config_timers.pop(token, None)
        if timer and threading.current_thread() is not timer:
            timer.cancel()
        if session:
            self._delete_message(session["bot_id"], session["chat_id"], session["message_id"])

    def _handle_config_callback(self, bot_id: str, callback: dict[str, Any]) -> bool:
        data = str(callback.get("data") or "")
        if not data.startswith("cfg:"):
            return False
        pieces = data.split(":")
        if len(pieces) != 3:
            return True
        token, action = pieces[1], pieces[2]
        session = self.config_sessions.get(token)
        runtime = self.bots[bot_id]
        sender = callback.get("from") or {}; message = callback.get("message") or {}; chat = message.get("chat") or {}
        valid = bool(
            session and session["bot_id"] == bot_id and session["chat_id"] == chat.get("id")
            and session["user_id"] == sender.get("id") and session["expires_at"] >= time.monotonic()
            and chat.get("type") == "private" and self._is_owner(runtime, sender.get("id"))
        )
        if not valid:
            runtime.answer_callback(str(callback.get("id") or ""), "Esta configuração expirou.")
            return True
        if action == "close":
            runtime.answer_callback(str(callback.get("id") or "")); self._close_config(token); return True
        assert runtime.agent is not None
        key = runtime.agent.store.key(chat.get("id"))
        if action in {"share", "delete"}:
            settings = runtime.agent.store.toggle_setting(key, "share_thoughts" if action == "share" else "delete_thoughts")
            submenu = True
        else:
            settings = runtime.agent.store.settings(key)
            submenu = action == "thoughts"
        text, keyboard = self._config_keyboard(token, settings, submenu)
        runtime.edit(chat["id"], session["message_id"], text, keyboard)
        runtime.answer_callback(str(callback.get("id") or ""))
        return True

    @staticmethod
    def _safe_filename(name: str, fallback: str) -> str:
        value = re.sub(r"[^\w.() -]+", "_", Path(name).name, flags=re.UNICODE).strip(" .")
        return (value or fallback)[:180]

    def _agent_payload(self, runtime: BotRuntime, message: dict[str, Any]) -> dict[str, Any]:
        assert runtime.agent is not None
        payload: dict[str, Any] = {
            "text": message.get("text"), "caption": message.get("caption"), "attachments": [],
            "telegram_message_id": message.get("message_id"),
        }
        agent_config = runtime.config.data.get("agent") or {}
        maximum = int(agent_config.get("max_attachment_bytes", 20 * 1024 * 1024))
        batch_maximum = int(agent_config.get("max_batch_attachment_bytes", 50 * 1024 * 1024))
        directory = runtime.agent.paths.staging_dir / f"{message.get('chat', {}).get('id')}-{message.get('message_id', int(time.time()))}"
        total = 0
        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            photo = photos[-1]
            file_id = photo.get("file_id") if isinstance(photo, dict) else None
            if file_id:
                size = int(photo.get("file_size") or 0)
                total += size
                if total > batch_maximum: raise TelegramApiError("attachment batch exceeds configured limit")
                path = directory / f"photo-{message.get('message_id', 'image')}.jpg"
                runtime.download(file_id, path, maximum)
                size = path.stat().st_size
                total = total - int(photo.get("file_size") or 0) + size
                if total > batch_maximum:
                    path.unlink(missing_ok=True)
                    raise TelegramApiError("attachment batch exceeds configured limit")
                payload["attachments"].append({"kind": "image", "path": str(path), "name": path.name, "size": size})
        document = message.get("document")
        if isinstance(document, dict) and document.get("file_id"):
            size = int(document.get("file_size") or 0)
            total += size
            if total > batch_maximum: raise TelegramApiError("attachment batch exceeds configured limit")
            name = self._safe_filename(str(document.get("file_name") or "document.bin"), "document.bin")
            path = directory / name
            runtime.download(document["file_id"], path, maximum)
            actual_size = path.stat().st_size
            if total - size + actual_size > batch_maximum:
                path.unlink(missing_ok=True)
                raise TelegramApiError("attachment batch exceeds configured limit")
            payload["attachments"].append({"kind": "document", "path": str(path), "name": name,
                                            "mime_type": document.get("mime_type"), "size": actual_size})
        voice = message.get("voice")
        settings = voice_transcription_settings(runtime.config.data)
        if isinstance(voice, dict) and settings is not None and voice.get("file_id"):
            size = int(voice.get("file_size") or 0)
            limit = min(maximum, settings.max_audio_bytes)
            total += size
            if size > limit or total > batch_maximum:
                raise TelegramApiError("voice message exceeds configured limit")
            path = directory / f"voice-{message.get('message_id', 'audio')}.ogg"
            runtime.download(str(voice["file_id"]), path, limit)
            actual_size = path.stat().st_size
            if total - size + actual_size > batch_maximum:
                path.unlink(missing_ok=True)
                raise TelegramApiError("attachment batch exceeds configured limit")
            payload["attachments"].append({
                "kind": "voice", "path": str(path), "name": path.name, "mime_type": voice.get("mime_type") or "audio/ogg",
                "size": actual_size, "duration_seconds": int(voice.get("duration") or 0),
            })
        return payload

    def process_update(self, bot_id: str, update: dict[str, Any]) -> None:
        runtime = self.bots[bot_id]
        if runtime.agent and not runtime.agent.store.accept_update(update.get("update_id")):
            return
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            if self._handle_config_callback(bot_id, callback):
                return
            if runtime.agent and runtime.agent.handle_callback(callback):
                return
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
        if private_owner and NEW_COMMAND_RE.match(text):
            if session:
                self._clear_totp_session(session_key)
            if runtime.agent and runtime.agent.enabled:
                runtime.agent.reset_conversation(chat.get("id"), message.get("message_thread_id"))
                runtime.send(chat.get("id"), "Nova conversa iniciada.")
            return
        if private_owner and CONFIG_COMMAND_RE.match(text):
            if session:
                self._clear_totp_session(session_key)
            if runtime.agent and runtime.agent.enabled:
                self._open_config(bot_id, chat.get("id"), sender.get("id"))
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
        elif runtime.agent and runtime.agent.enabled:
            if not private_owner:
                return
            voice_enabled = voice_transcription_settings(runtime.config.data) is not None
            if not text and not message.get("caption") and not message.get("photo") and not message.get("document") and not (voice_enabled and message.get("voice")):
                runtime.send(chat.get("id"), "Este tipo de mensagem ainda não é suportado.")
                return
            try:
                with self._typing(bot_id, chat.get("id")):
                    payload = self._agent_payload(runtime, message)
            except TelegramApiError:
                runtime.send(chat.get("id"), "Não foi possível receber o anexo ou ele excede o limite permitido.")
                return
            runtime.agent.enqueue(update.get("update_id"), message, payload, update_already_accepted=True)
            return
        else:
            response = runtime.config.data.get("reply_text", DEFAULT_REPLY)
        runtime.send(chat.get("id"), response, message.get("message_thread_id"))

    def _has_owners(self, runtime: BotRuntime) -> bool:
        return bool(self._owners(runtime))

    def _pair(self, bot_id: str, sender: dict[str, Any], pin: str) -> str:
        pairing_file = self._bot_state_file(bot_id, "pairing.json")
        record = _read_json(pairing_file, None)
        digest = hashlib.sha256(f"{bot_id}:{pin}".encode()).hexdigest()
        if not record:
            return "Não há um pairing ativo para este bot. Solicite um novo PIN de configuração."
        if record.get("expires_at", 0) < time.time():
            pairing_file.unlink(missing_ok=True)
            return "O PIN de pairing expirou. Solicite um novo PIN de configuração."
        if not secrets.compare_digest(record.get("hash", ""), digest):
            attempts = int(record.get("attempts", 0)) + 1
            if attempts >= MAX_PAIR_ATTEMPTS:
                pairing_file.unlink(missing_ok=True)
                return "Pairing cancelado por excesso de tentativas com PIN incorreto. Solicite um novo PIN."
            record["attempts"] = attempts
            _write_json(pairing_file, record)
            remaining = MAX_PAIR_ATTEMPTS - attempts
            suffix = "tentativa" if remaining == 1 else "tentativas"
            return f"PIN incorreto. Você ainda tem {remaining} {suffix}."
        runtime = self.bots[bot_id]
        if not runtime.agent:
            raise AgentConfigurationError("bot contacts are unavailable")
        runtime.agent.contacts.add_owner(sender)
        self._configure_owner_commands(runtime)
        pairing_file.unlink(missing_ok=True)
        return "Pairing concluído com sucesso. Sua conta agora é owner deste bot."

    def pair_request(self, bot_id: str, ttl_seconds: int = 300) -> dict[str, Any]:
        if bot_id not in self.bots: raise ValueError("bot not found")
        ttl_seconds = max(1, min(int(ttl_seconds), MAX_PAIR_TTL_SECONDS))
        pin = f"{secrets.randbelow(1_000_000):06d}"
        pairing_file = self._bot_state_file(bot_id, "pairing.json")
        _write_json(pairing_file, {"hash": hashlib.sha256(f"{bot_id}:{pin}".encode()).hexdigest(), "expires_at": time.time() + ttl_seconds, "attempts": 0})
        return {"bot_id": bot_id, "pin": pin, "expires_in": ttl_seconds}

    def migrate_bots(self, apply: bool = False) -> dict[str, Any]:
        operations: list[dict[str, str]] = []
        for source in sorted(self.legacy_bots_dir.glob("*.json")):
            data = _read_json(source, {})
            if not isinstance(data, dict) or not data.get("id"):
                continue
            bot_id = str(data["id"])
            target_dir = self.bots_dir / bot_id
            target = target_dir / "bot.json"
            if target.exists():
                continue
            operation = {"bot_id": bot_id, "source": str(source), "target": str(target)}
            operations.append(operation)
            if not apply:
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            backup = source.with_suffix(source.suffix + ".migration.bak")
            if not backup.exists():
                shutil.copy2(source, backup)
            shutil.copy2(source, target)
            legacy_contacts = source.with_name(f"{source.stem}.contacts.json")
            if legacy_contacts.exists():
                shutil.copy2(legacy_contacts, target_dir / "contacts.json")
            else:
                write_json(target_dir / "contacts.json", {"version": 1, "contacts": []})
            source.rename(source.with_suffix(source.suffix + ".migrated"))
        return {"applied": apply, "operations": operations}

    def handle(self, method: str, params: dict[str, Any]) -> Any:
        if method == "ping": return {"service": "telegram-manager", "state": "running", "bots": len(self.bots)}
        if method == "telegram.bots": return [runtime.status() for runtime in self.bots.values()]
        bot_id = params.get("bot_id")
        if method == "telegram.bot.pair-request": return self.pair_request(bot_id, int(params.get("ttl_seconds", 300)))
        if method == "telegram.bot.owners": return self._owners(self.bots[bot_id])
        if method == "telegram.bot.start-listener": return self.bots[bot_id].start()
        if method == "telegram.bot.stop-listener": return self.bots[bot_id].stop()
        if method == "telegram.bot.status": return self.bots[bot_id].status()
        if method == "telegram.bot.agent-init": return self.bots[bot_id].agent.init()
        if method == "telegram.bot.agent-sync": return self.bots[bot_id].agent.sync()
        if method == "telegram.bot.agent-status":
            agent = self.bots[bot_id].agent
            return {**agent.status(), "login": agent.login_status() if agent.enabled else None}
        if method == "telegram.bot.agent-validate": return self.bots[bot_id].agent.validate(bool(params.get("require_login", False)))
        if method in {"telegram.bot.agent-spec", "telegram.bot.agent-login-prepare"}:
            agent = self.bots[bot_id].agent
            was_running = agent.suspend() if method.endswith("login-prepare") else False
            return {"executable": str(agent._executable()), "codex_home": str(agent.paths.codex_home) if agent.paths.codex_home else None,
                    "working_directory": str(agent.paths.working_directory), "environment": agent.environment(), "was_running": was_running}
        if method == "telegram.bot.agent-login-finish":
            agent = self.bots[bot_id].agent
            return agent.start() if params.get("restart") and self.bots[bot_id].state == "running" else agent.status()
        if method == "telegram.migrate-bots": return self.migrate_bots(bool(params.get("apply", False)))
        if method == "telegram.send":
            return self.bots[bot_id].send(params["chat_id"], params["text"], params.get("message_thread_id"))
        if method == "shutdown":
            self.stop_event.set()
            return {"state": "stopping"}
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
