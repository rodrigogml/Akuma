from __future__ import annotations

import argparse
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
from .telegram_vault import VaultTokenResolver


DEFAULT_REPLY = "Não incomode o Akuma"
MAX_PAIR_TTL_SECONDS = 5 * 60
PAIR_RE = re.compile(r"^/(?:pair|painr)(?:@\w+)?\s+(\d{6})\s*$", re.IGNORECASE)
PAIR_COMMAND_RE = re.compile(r"^/(?:pair|painr)(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE)
MAX_PAIR_ATTEMPTS = 3


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
        allowed = self.config.listener.get("allowed_updates")
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
        self.api.set_webhook(url, webhook.get("secret_token"), self.config.listener.get("allowed_updates"))  # type: ignore[union-attr]

    def _webhook_wait(self) -> None:
        self.stop_event.wait()

    def send(self, chat_id: int | str, text: str, thread_id: int | None = None) -> dict[str, Any]:
        if not self.api:
            self._connect()
        return self.api.send_message(chat_id, text, thread_id)  # type: ignore[union-attr]

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
        self.bots_dir.mkdir(parents=True, exist_ok=True); self.state_dir.mkdir(parents=True, exist_ok=True)
        self.tokens = VaultTokenResolver()
        self.bots: dict[str, BotRuntime] = {}
        for path in sorted(self.bots_dir.glob("*.json")):
            data = _read_json(path, {})
            if isinstance(data, dict) and data.get("id"):
                self.bots[str(data["id"])] = BotRuntime(self, BotConfig(data, path))
        self.webhook: WebhookReceiver | None = None
        self.stop_event = threading.Event()

    def start(self) -> None:
        webhook_bots = [runtime for runtime in self.bots.values() if runtime.config.listener.get("mode") == "webhook"]
        if webhook_bots:
            manager_config = _read_json(self.root / "manager.json", {})
            receiver = manager_config.get("webhook_server", {})
            self.webhook = WebhookReceiver(receiver.get("host", "127.0.0.1"), int(receiver.get("port", 0)), self.process_webhook)
            self.webhook.start()
        for runtime in self.bots.values():
            if runtime.config.listener.get("enabled", False): runtime.start()

    def shutdown(self) -> None:
        for runtime in self.bots.values(): runtime.stop()
        if self.webhook: self.webhook.close()
        self.stop_event.set()

    def process_webhook(self, bot_id: str, update: dict[str, Any], secret: str | None) -> None:
        runtime = self.bots.get(bot_id)
        expected = runtime.config.listener.get("webhook", {}).get("secret_token") if runtime else None
        if not runtime or not expected or secret != expected: raise PermissionError("invalid webhook")
        self.process_update(bot_id, update)

    def process_update(self, bot_id: str, update: dict[str, Any]) -> None:
        runtime = self.bots[bot_id]
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict): return
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        text = message.get("text") or ""
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
