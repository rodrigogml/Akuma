from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable

from .telegram_speech import EccoVoxClient, VoiceTranscriptionError, voice_transcription_settings


TELEGRAM_GATEWAY_PROFILE = "telegram-v1"
TELEGRAM_GATEWAY_INSTRUCTIONS = """You are operating through the Akuma Telegram gateway in a private owner DM.

Channel contract:
- Your ordinary final answer is delivered to the active Telegram conversation. Do not call a tool merely to send the final answer.
- Telegram gateway tools are scoped by the gateway to the current bot, owner, conversation and active turn. Never ask for or invent chat IDs, user IDs, bot tokens or callback data.
- Use telegram_gateway.send_message for a separate or ephemeral message. Set ttl_seconds when the message must disappear; do not promise ephemerality unless the tool succeeds.
- Use telegram_gateway.ask_menu when a short, explicit choice is materially easier than free text. The tool waits for the owner selection and returns the selected option.
- Use telegram_gateway.list_attachments to discover files retained in this conversation. Use telegram_gateway.materialize_attachment before opening a past attachment; only the returned staging path is valid for this turn.
- Text marked `[Transcrição de mensagem de áudio recebida pelo Telegram]` was generated automatically from a voice message. It can contain errors in names, homophones, punctuation, segmentation and uncommon terms. Interpret it in context, ask for clarification before consequential action when ambiguity matters, and explicitly tell the user what you understood from the audio when relying on a conversion or resolving an ambiguity. The original voice attachment remains available through the attachment tools if further processing is necessary.
- Do not call the Telegram HTTP API directly and do not expose gateway-local paths to the user.
- Treat tool failures and timeouts as real failures. Explain only what the user needs to continue, without claiming an action succeeded.
- /new, /config, /totp, pairing and gateway callbacks are native commands handled before your turn.
"""


def _tool_specs() -> list[dict[str, Any]]:
    tools = [
        {
            "type": "function",
            "name": "send_message",
            "description": "Send a separate message to the current Telegram DM, optionally deleting it after a TTL.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "ttl_seconds": {"type": "integer", "minimum": 0, "maximum": 86400, "default": 0},
                    "protect_content": {"type": "boolean", "default": False},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "ask_menu",
            "description": "Ask the current owner to select one option from an inline Telegram menu and wait for the answer.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "options": {
                        "type": "array", "minItems": 2, "maxItems": 20,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "minLength": 1, "maxLength": 64},
                                "label": {"type": "string", "minLength": 1, "maxLength": 64},
                            },
                            "required": ["id", "label"], "additionalProperties": False,
                        },
                    },
                    "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 300, "default": 120},
                    "protect_content": {"type": "boolean", "default": False},
                },
                "required": ["question", "options"], "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "list_attachments",
            "description": "List attachment metadata retained in the current Telegram conversation and generation.",
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}},
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "materialize_attachment",
            "description": "Copy one retained attachment from the current conversation into this bot's staging area and return its local path.",
            "inputSchema": {
                "type": "object",
                "properties": {"attachment_id": {"type": "string", "minLength": 1, "maxLength": 64}},
                "required": ["attachment_id"], "additionalProperties": False,
            },
        },
    ]
    return [{
        "type": "namespace",
        "name": "telegram_gateway",
        "description": "Capabilities safely scoped by the Akuma gateway to the current Telegram owner DM.",
        "tools": tools,
    }]


class AgentConfigurationError(RuntimeError):
    pass


class CodexProtocolError(RuntimeError):
    pass


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def resolve_path(value: str | Path, base: Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def paths_overlap(paths: dict[str, Path]) -> list[str]:
    errors: list[str] = []
    items = list(paths.items())
    for index, (left_name, left) in enumerate(items):
        for right_name, right in items[index + 1:]:
            try:
                left.relative_to(right)
                overlap = True
            except ValueError:
                try:
                    right.relative_to(left)
                    overlap = True
                except ValueError:
                    overlap = False
            if overlap:
                errors.append(f"{left_name} overlaps {right_name}")
    return errors


@dataclass(frozen=True)
class BotPaths:
    bot_id: str
    bot_root: Path
    config_path: Path
    contacts_path: Path
    state_dir: Path
    staging_dir: Path
    codex_home: Path | None
    working_directory: Path
    vault_profile: Path

    @classmethod
    def from_config(cls, config_path: Path, data: dict[str, Any], project_root: Path) -> "BotPaths":
        bot_id = str(data["id"])
        modern = config_path.name.casefold() == "bot.json"
        bot_root = config_path.parent if modern else config_path.parent / bot_id
        contacts_path = bot_root / "contacts.json" if modern else config_path.with_name(f"{config_path.stem}.contacts.json")
        agent = data.get("agent") if isinstance(data.get("agent"), dict) else {}
        context = str(agent.get("context") or "akuma").casefold()
        if context not in {"akuma", "subbot"}:
            raise AgentConfigurationError("agent.context must be akuma or subbot")
        if context == "subbot":
            home = resolve_path(agent.get("codex_home") or "codexhome", bot_root)
            work = resolve_path(agent.get("working_directory") or "codexwork", bot_root)
        else:
            raw_home = agent.get("codex_home")
            home = resolve_path(raw_home, bot_root) if raw_home else None
            raw_work = agent.get("working_directory")
            work = resolve_path(raw_work, bot_root) if raw_work else project_root.resolve()
        vault = data.get("vault") if isinstance(data.get("vault"), dict) else {}
        vault_profile = resolve_path(vault.get("profile") or "vault/keepass.ini", bot_root)
        state_dir = bot_root / "state"
        staging_dir = bot_root / "staging"
        isolated = {"vault": vault_profile.parent, "state": state_dir, "staging": staging_dir}
        if context == "subbot":
            isolated["codexwork"] = work
            if home is not None:
                isolated["codexhome"] = home
        overlaps = paths_overlap(isolated)
        if overlaps:
            raise AgentConfigurationError("; ".join(overlaps))
        return cls(bot_id, bot_root, config_path, contacts_path, state_dir, staging_dir, home, work, vault_profile)


class ContactsStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        if not path.exists():
            write_json(path, {"version": 1, "contacts": []})

    def contacts(self) -> list[dict[str, Any]]:
        with self.lock:
            data = read_json(self.path, {"contacts": []})
            contacts = data.get("contacts") if isinstance(data, dict) else []
            return [dict(item) for item in contacts if isinstance(item, dict)]

    def owners(self) -> list[dict[str, Any]]:
        return [item for item in self.contacts() if "owner" in item.get("roles", [])]

    def is_owner(self, user_id: Any) -> bool:
        return any(item.get("telegram_user_id") == user_id for item in self.owners())

    def add_owner(self, sender: dict[str, Any]) -> bool:
        user_id = sender.get("id")
        with self.lock:
            contacts = self.contacts()
            for item in contacts:
                if item.get("telegram_user_id") == user_id:
                    roles = item.setdefault("roles", [])
                    if "owner" not in roles:
                        roles.append("owner")
                        write_json(self.path, {"version": 1, "contacts": contacts})
                        return True
                    return False
            contacts.append({
                "telegram_user_id": user_id,
                "username": sender.get("username"),
                "display_name": sender.get("first_name"),
                "roles": ["owner"],
                "added_at": time.time(),
                "source": "pair",
            })
            write_json(self.path, {"version": 1, "contacts": contacts})
            return True


class ConversationStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        abandoned_payloads: list[dict[str, Any]] = []
        with self.db:
            self.db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS conversations (
                    context_type TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_thread_id INTEGER NOT NULL DEFAULT 0,
                    codex_thread_id TEXT,
                    contract_hash TEXT,
                    generation INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(context_type, chat_id, message_thread_id)
                );
                CREATE TABLE IF NOT EXISTS conversation_settings (
                    context_type TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_thread_id INTEGER NOT NULL DEFAULT 0,
                    share_thoughts INTEGER NOT NULL DEFAULT 1,
                    delete_thoughts INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(context_type, chat_id, message_thread_id)
                );
                CREATE TABLE IF NOT EXISTS inbox_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    context_type TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_thread_id INTEGER NOT NULL DEFAULT 0,
                    sort_key INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS processed_updates (
                    update_id INTEGER PRIMARY KEY,
                    processed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS managed_home_resources (
                    path TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    target TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_attachments (
                    id TEXT PRIMARY KEY,
                    context_type TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_thread_id INTEGER NOT NULL DEFAULT 0,
                    generation INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    mime_type TEXT,
                    size INTEGER NOT NULL,
                    archive_path TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ephemeral_messages (
                    chat_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    delete_at REAL NOT NULL,
                    PRIMARY KEY(chat_id, message_id)
                );
                """
            )
            columns = {str(row[1]) for row in self.db.execute("PRAGMA table_info(conversations)")}
            if "contract_hash" not in columns:
                self.db.execute("ALTER TABLE conversations ADD COLUMN contract_hash TEXT")
            abandoned = self.db.execute("SELECT payload FROM inbox_items WHERE status IN ('running','abandoned')").fetchall()
            for row in abandoned:
                try:
                    abandoned_payloads.append(json.loads(row["payload"]))
                except (TypeError, ValueError):
                    pass
            self.db.execute("DELETE FROM inbox_items WHERE status IN ('running','abandoned')")
        for payload in abandoned_payloads:
            self._remove_payload_files(payload)

    @staticmethod
    def key(chat_id: Any, thread_id: Any = None) -> tuple[str, str, int]:
        return "dm", str(chat_id), int(thread_id or 0)

    def accept_update(self, update_id: Any) -> bool:
        if not isinstance(update_id, int):
            return True
        with self.lock, self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO processed_updates(update_id, processed_at) VALUES (?, ?)",
                (update_id, time.time()),
            )
            return cursor.rowcount == 1

    def conversation(self, key: tuple[str, str, int]) -> dict[str, Any]:
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM conversations WHERE context_type=? AND chat_id=? AND message_thread_id=?", key
            ).fetchone()
            if row is None:
                self.db.execute(
                    "INSERT INTO conversations(context_type,chat_id,message_thread_id,updated_at) VALUES(?,?,?,?)",
                    (*key, time.time()),
                )
                return {"codex_thread_id": None, "contract_hash": None, "generation": 0}
            return dict(row)

    def set_thread(self, key: tuple[str, str, int], thread_id: str | None, generation: int | None = None,
                   contract_hash: str | None = None) -> None:
        current = self.conversation(key)
        value = int(current["generation"] if generation is None else generation)
        with self.lock, self.db:
            self.db.execute(
                "UPDATE conversations SET codex_thread_id=?,contract_hash=?,generation=?,updated_at=? WHERE context_type=? AND chat_id=? AND message_thread_id=?",
                (thread_id, contract_hash, value, time.time(), *key),
            )

    def reset(self, key: tuple[str, str, int]) -> tuple[str | None, int]:
        current = self.conversation(key)
        generation = int(current["generation"]) + 1
        with self.lock, self.db:
            self.db.execute(
                "UPDATE conversations SET codex_thread_id=NULL,contract_hash=NULL,generation=?,updated_at=? WHERE context_type=? AND chat_id=? AND message_thread_id=?",
                (generation, time.time(), *key),
            )
            rows = self.db.execute(
                "SELECT payload FROM inbox_items WHERE context_type=? AND chat_id=? AND message_thread_id=? AND status='pending'", key
            ).fetchall()
            self.db.execute(
                "DELETE FROM inbox_items WHERE context_type=? AND chat_id=? AND message_thread_id=? AND status='pending'", key
            )
            attachments = self.db.execute(
                "SELECT archive_path FROM conversation_attachments WHERE context_type=? AND chat_id=? AND message_thread_id=?", key
            ).fetchall()
            self.db.execute(
                "DELETE FROM conversation_attachments WHERE context_type=? AND chat_id=? AND message_thread_id=?", key
            )
        for row in rows:
            self._remove_payload_files(json.loads(row["payload"]))
        for row in attachments:
            self._remove_file_and_empty_parent(Path(row["archive_path"]))
        return current.get("codex_thread_id"), generation

    def archive_payload_attachments(self, key: tuple[str, str, int], payload: dict[str, Any],
                                    staging_root: Path, archive_root: Path) -> dict[str, Any]:
        generation = int(self.conversation(key)["generation"])
        staging = staging_root.resolve()
        archive_root.mkdir(parents=True, exist_ok=True)
        archived: list[Path] = []
        archived_ids: list[str] = []
        try:
            for attachment in payload.get("attachments", []):
                if not isinstance(attachment, dict) or not attachment.get("path"):
                    continue
                source = Path(str(attachment["path"]))
                resolved = source.resolve(strict=True)
                if source.is_symlink():
                    raise AgentConfigurationError("attachment staging path cannot be a symbolic link")
                try:
                    resolved.relative_to(staging)
                except ValueError as exc:
                    raise AgentConfigurationError("attachment is outside this bot's staging directory") from exc
                attachment_id = secrets.token_urlsafe(18)
                safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", str(attachment.get("name") or source.name)).strip(" .") or "attachment.bin"
                destination = archive_root / attachment_id / safe_name
                destination.parent.mkdir(parents=True, exist_ok=False)
                shutil.copy2(resolved, destination)
                archived.append(destination)
                archived_ids.append(attachment_id)
                size = destination.stat().st_size
                with self.lock, self.db:
                    self.db.execute(
                        "INSERT INTO conversation_attachments(id,context_type,chat_id,message_thread_id,generation,kind,name,mime_type,size,archive_path,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (attachment_id, *key, generation, str(attachment.get("kind") or "document"), safe_name,
                         attachment.get("mime_type"), size, str(destination), time.time()),
                    )
                attachment["attachment_id"] = attachment_id
            return payload
        except Exception:
            if archived_ids:
                with self.lock, self.db:
                    self.db.executemany("DELETE FROM conversation_attachments WHERE id=?", [(item,) for item in archived_ids])
            for path in archived:
                self._remove_file_and_empty_parent(path)
            raise

    def list_attachments(self, key: tuple[str, str, int], limit: int) -> list[dict[str, Any]]:
        generation = int(self.conversation(key)["generation"])
        with self.lock:
            rows = self.db.execute(
                "SELECT id,kind,name,mime_type,size,created_at FROM conversation_attachments WHERE context_type=? AND chat_id=? AND message_thread_id=? AND generation=? ORDER BY created_at DESC LIMIT ?",
                (*key, generation, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def attachment(self, key: tuple[str, str, int], attachment_id: str) -> dict[str, Any] | None:
        generation = int(self.conversation(key)["generation"])
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM conversation_attachments WHERE id=? AND context_type=? AND chat_id=? AND message_thread_id=? AND generation=?",
                (attachment_id, *key, generation),
            ).fetchone()
        return dict(row) if row else None

    def save_ephemeral(self, chat_id: Any, message_id: int, delete_at: float) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO ephemeral_messages(chat_id,message_id,delete_at) VALUES(?,?,?)",
                            (str(chat_id), message_id, delete_at))

    def remove_ephemeral(self, chat_id: Any, message_id: int) -> None:
        with self.lock:
            if self.db is None:
                return
            with self.db:
                self.db.execute("DELETE FROM ephemeral_messages WHERE chat_id=? AND message_id=?", (str(chat_id), message_id))

    def ephemeral_messages(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT * FROM ephemeral_messages ORDER BY delete_at")]

    def add_inbox(self, key: tuple[str, str, int], sort_key: int, payload: dict[str, Any]) -> int:
        generation = int(self.conversation(key)["generation"])
        with self.lock, self.db:
            cursor = self.db.execute(
                "INSERT INTO inbox_items(context_type,chat_id,message_thread_id,sort_key,generation,payload,status,created_at) VALUES(?,?,?,?,?,?,'pending',?)",
                (*key, sort_key, generation, json.dumps(payload, ensure_ascii=False), time.time()),
            )
            return int(cursor.lastrowid)

    def take_pending(self, key: tuple[str, str, int], maximum: int, maximum_attachment_bytes: int) -> list[dict[str, Any]]:
        generation = int(self.conversation(key)["generation"])
        with self.lock, self.db:
            candidates = self.db.execute(
                "SELECT * FROM inbox_items WHERE context_type=? AND chat_id=? AND message_thread_id=? AND generation=? AND status='pending' ORDER BY sort_key,id LIMIT ?",
                (*key, generation, maximum),
            ).fetchall()
            rows = []
            total = 0
            for row in candidates:
                payload = json.loads(row["payload"])
                size = sum(int(item.get("size") or 0) for item in payload.get("attachments", []) if isinstance(item, dict))
                if rows and total + size > maximum_attachment_bytes:
                    break
                rows.append(row)
                total += size
            ids = [int(row["id"]) for row in rows]
            if ids:
                self.db.executemany("UPDATE inbox_items SET status='running' WHERE id=?", [(item,) for item in ids])
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def finish(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        ids = [(int(row["id"]),) for row in rows]
        with self.lock, self.db:
            self.db.executemany("DELETE FROM inbox_items WHERE id=?", ids)
        for row in rows:
            self._remove_payload_files(row["payload"])

    def has_pending(self, key: tuple[str, str, int]) -> bool:
        generation = int(self.conversation(key)["generation"])
        with self.lock:
            row = self.db.execute(
                "SELECT 1 FROM inbox_items WHERE context_type=? AND chat_id=? AND message_thread_id=? AND generation=? AND status='pending' LIMIT 1",
                (*key, generation),
            ).fetchone()
            return row is not None

    def pending_keys(self) -> list[tuple[str, str, int]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT DISTINCT context_type,chat_id,message_thread_id FROM inbox_items WHERE status='pending' ORDER BY context_type,chat_id,message_thread_id"
            ).fetchall()
            return [(str(row[0]), str(row[1]), int(row[2])) for row in rows]

    def referenced_attachment_paths(self) -> set[Path]:
        referenced: set[Path] = set()
        with self.lock:
            rows = self.db.execute("SELECT payload FROM inbox_items WHERE status='pending'").fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            for attachment in payload.get("attachments", []):
                path = attachment.get("path") if isinstance(attachment, dict) else None
                if path:
                    referenced.add(Path(path).resolve())
        return referenced

    def settings(self, key: tuple[str, str, int]) -> dict[str, bool]:
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT share_thoughts,delete_thoughts FROM conversation_settings WHERE context_type=? AND chat_id=? AND message_thread_id=?", key
            ).fetchone()
            if row is None:
                self.db.execute(
                    "INSERT INTO conversation_settings(context_type,chat_id,message_thread_id) VALUES(?,?,?)", key
                )
                return {"share_thoughts": True, "delete_thoughts": True}
            return {"share_thoughts": bool(row[0]), "delete_thoughts": bool(row[1])}

    def toggle_setting(self, key: tuple[str, str, int], name: str) -> dict[str, bool]:
        if name not in {"share_thoughts", "delete_thoughts"}:
            raise ValueError("invalid setting")
        settings = self.settings(key)
        settings[name] = not settings[name]
        with self.lock, self.db:
            self.db.execute(
                f"UPDATE conversation_settings SET {name}=? WHERE context_type=? AND chat_id=? AND message_thread_id=?",
                (int(settings[name]), *key),
            )
        return settings

    @staticmethod
    def _remove_payload_files(payload: dict[str, Any]) -> None:
        for attachment in payload.get("attachments", []):
            path = attachment.get("path") if isinstance(attachment, dict) else None
            if not path:
                continue
            try:
                Path(path).unlink(missing_ok=True)
                parent = Path(path).parent
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass

    @staticmethod
    def _remove_file_and_empty_parent(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
            parent = path.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass

    def close(self) -> None:
        with self.lock:
            if self.db is not None:
                self.db.close()
                self.db = None


class CodexAppServer:
    def __init__(self, executable: Path, home: Path | None, cwd: Path, environment: dict[str, str], timeout: int = 30):
        self.executable = executable
        self.home = home
        self.cwd = cwd
        self.environment = environment
        self.timeout = timeout
        self.process: subprocess.Popen[str] | None = None
        self.reader: threading.Thread | None = None
        self.stderr_reader: threading.Thread | None = None
        self.write_lock = threading.Lock()
        self.pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self.pending_lock = threading.Lock()
        self.next_id = 1
        self.notification_handler: Callable[[str, dict[str, Any]], None] | None = None
        self.server_request_handler: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None
        self.last_error: str | None = None
        self._restricted_read_capability: bool | None = None
        self.transcriber = EccoVoxClient()

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        env = os.environ.copy()
        env.update(self.environment)
        if self.home is not None:
            env["CODEX_HOME"] = str(self.home)
        self.process = subprocess.Popen(
            [str(self.executable), "app-server", "--stdio"],
            cwd=self.cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.reader = threading.Thread(target=self._read_stdout, name="codex-app-server", daemon=True)
        self.stderr_reader = threading.Thread(target=self._read_stderr, name="codex-app-server-stderr", daemon=True)
        self.reader.start(); self.stderr_reader.start()
        self.request("initialize", {
            "clientInfo": {"name": "akuma_telegram_gateway", "title": "Akuma Telegram Gateway", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        })
        self.notify("initialized", {})

    def stop(self) -> None:
        process = self.process
        self.process = None
        if not process:
            return
        try:
            if process.stdin:
                process.stdin.close()
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try: process.kill()
            except OSError: pass

    def _send(self, payload: dict[str, Any]) -> None:
        self.start_if_needed()
        assert self.process and self.process.stdin
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.write_lock:
            self.process.stdin.write(line + "\n")
            self.process.stdin.flush()

    def start_if_needed(self) -> None:
        if not self.process or self.process.poll() is not None:
            self.start()

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: int | None = None) -> dict[str, Any]:
        with self.pending_lock:
            request_id = self.next_id
            self.next_id += 1
            response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self.pending[request_id] = response_queue
        try:
            self._send({"method": method, "id": request_id, "params": params or {}})
            response = response_queue.get(timeout=timeout or self.timeout)
        except queue.Empty as exc:
            raise CodexProtocolError(f"Codex App Server timed out waiting for {method}") from exc
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)
        if response.get("error"):
            message = response["error"].get("message") if isinstance(response["error"], dict) else str(response["error"])
            raise CodexProtocolError(f"Codex App Server rejected {method}: {message}")
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        for raw in self.process.stdout:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            request_id = message.get("id")
            if isinstance(request_id, (int, str)) and ("result" in message or "error" in message):
                with self.pending_lock:
                    target = self.pending.get(request_id)
                if target:
                    target.put(message)
                continue
            method = message.get("method")
            params = message.get("params")
            if isinstance(request_id, (int, str)) and isinstance(method, str):
                if method == "item/tool/call" and isinstance(params, dict) and self.server_request_handler:
                    threading.Thread(
                        target=self._handle_server_request, args=(request_id, method, params),
                        name=f"codex-tool-{request_id}", daemon=True,
                    ).start()
                else:
                    self._decline_server_request(request_id)
            elif isinstance(method, str) and isinstance(params, dict) and self.notification_handler:
                self.notification_handler(method, params)
        if self.process and self.process.poll() not in {None, 0}:
            self.last_error = f"Codex App Server exited with code {self.process.poll()}"

    def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        for raw in self.process.stderr:
            line = raw.strip()
            if line:
                self.last_error = re.sub(r"(?i)(token|password|secret)=[^\s]+", r"\1=[redacted]", line)[-1000:]

    def _decline_server_request(self, request_id: int | str) -> None:
        try:
            self._send({"id": request_id, "error": {"code": -32000, "message": "Interactive requests are disabled"}})
        except Exception:
            pass

    def _handle_server_request(self, request_id: int | str, method: str, params: dict[str, Any]) -> None:
        try:
            assert self.server_request_handler is not None
            result = self.server_request_handler(method, params)
            self._send({"id": request_id, "result": result})
        except Exception as exc:
            self.last_error = str(exc)[-1000:]
            try:
                self._send({"id": request_id, "error": {"code": -32001, "message": "Telegram gateway tool failed"}})
            except Exception:
                pass


@dataclass
class ActiveTurn:
    key: tuple[str, str, int]
    generation: int
    thread_id: str
    turn_id: str | None
    completed: threading.Event
    status: str = "inProgress"
    final_text: str = ""
    thought_ids: list[int] | None = None
    seen_thoughts: set[str] | None = None
    materialized_paths: list[Path] | None = None

    def __post_init__(self) -> None:
        self.thought_ids = []
        self.seen_thoughts = set()
        self.materialized_paths = []


@dataclass
class MenuSession:
    token: str
    key: tuple[str, str, int]
    generation: int
    owner_id: int
    message_id: int
    options: list[dict[str, str]]
    completed: threading.Event
    selected: dict[str, str] | None = None
    cancelled: bool = False


class TelegramAgentRuntime:
    def __init__(self, bot_runtime: Any, project_root: Path):
        self.bot_runtime = bot_runtime
        self.config = bot_runtime.config.data
        self.paths = BotPaths.from_config(bot_runtime.config.path, self.config, project_root)
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        self.paths.staging_dir.mkdir(parents=True, exist_ok=True)
        self.contacts = ContactsStore(self.paths.contacts_path)
        self.store = ConversationStore(self.paths.state_dir / "gateway.sqlite3")
        self._cleanup_orphan_staging()
        self.client: CodexAppServer | None = None
        self.workers: dict[tuple[str, str, int], threading.Thread] = {}
        self.workers_lock = threading.RLock()
        self.active: dict[str, ActiveTurn] = {}
        self.active_lock = threading.RLock()
        self.menus: dict[str, MenuSession] = {}
        self.menus_lock = threading.RLock()
        self.deletion_timers: dict[tuple[str, int], threading.Timer] = {}
        self.deletion_lock = threading.RLock()
        self._restricted_read_capability: bool | None = None
        self.parallel_turns = threading.BoundedSemaphore(max(1, int(self._agent_config().get("max_parallel_conversations", 2))))
        self.state = "disabled"
        self.last_error: str | None = None
        self._restore_ephemeral_deletions()

    def _cleanup_orphan_staging(self) -> None:
        staging = self.paths.staging_dir.resolve()
        referenced = self.store.referenced_attachment_paths()
        for path in self.paths.staging_dir.rglob("*"):
            if path.is_symlink():
                path.unlink(missing_ok=True)
                continue
            resolved = path.resolve()
            try:
                resolved.relative_to(staging)
            except ValueError:
                continue
            if path.is_file() and resolved not in referenced:
                path.unlink(missing_ok=True)
        directories = []
        for path in self.paths.staging_dir.rglob("*"):
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                path.resolve().relative_to(staging)
                directories.append(path)
            except ValueError:
                continue
        directories.sort(key=lambda path: len(path.parts), reverse=True)
        for directory in directories:
            try:
                directory.rmdir()
            except OSError:
                pass

    def _restore_ephemeral_deletions(self) -> None:
        for item in self.store.ephemeral_messages():
            self._schedule_deletion(item["chat_id"], int(item["message_id"]), float(item["delete_at"]), persisted=True)

    def _schedule_deletion(self, chat_id: Any, message_id: int, delete_at: float, persisted: bool = False) -> None:
        key = (str(chat_id), message_id)
        if not persisted:
            self.store.save_ephemeral(chat_id, message_id, delete_at)
        delay = max(0.0, delete_at - time.time())
        timer = threading.Timer(delay, self._delete_managed_message, args=(str(chat_id), message_id))
        timer.daemon = True
        with self.deletion_lock:
            previous = self.deletion_timers.pop(key, None)
            if previous:
                previous.cancel()
            self.deletion_timers[key] = timer
        timer.start()

    def _delete_managed_message(self, chat_id: Any, message_id: int) -> None:
        key = (str(chat_id), message_id)
        with self.deletion_lock:
            timer = self.deletion_timers.pop(key, None)
            if timer and timer is not threading.current_thread():
                timer.cancel()
        try:
            self.bot_runtime.delete(chat_id, message_id)
        except Exception:
            pass
        finally:
            self.store.remove_ephemeral(chat_id, message_id)

    @property
    def enabled(self) -> bool:
        agent = self.config.get("agent")
        return isinstance(agent, dict) and agent.get("enabled") is True

    def _agent_config(self) -> dict[str, Any]:
        return self.config.get("agent") if isinstance(self.config.get("agent"), dict) else {}

    def _instruction_config(self) -> dict[str, Any]:
        value = self._agent_config().get("instructions")
        return value if isinstance(value, dict) else {}

    def _bot_specific_instructions(self) -> str:
        config = self._instruction_config()
        inline = config.get("developer")
        filename = config.get("developer_file")
        if inline and filename:
            raise AgentConfigurationError("agent.instructions must use developer or developer_file, not both")
        if inline is not None:
            if not isinstance(inline, str):
                raise AgentConfigurationError("agent.instructions.developer must be a string")
            return inline.strip()
        if filename is None:
            return ""
        if not isinstance(filename, str) or not filename.strip():
            raise AgentConfigurationError("agent.instructions.developer_file must be a relative path")
        path = resolve_path(filename, self.paths.bot_root)
        try:
            path.relative_to(self.paths.bot_root.resolve())
        except ValueError as exc:
            raise AgentConfigurationError("agent.instructions.developer_file must stay inside the bot directory") from exc
        if not path.is_file() or path.is_symlink():
            raise AgentConfigurationError("agent.instructions.developer_file was not found or is a link")
        if path.stat().st_size > 65536:
            raise AgentConfigurationError("agent.instructions.developer_file exceeds 64 KiB")
        try:
            return path.read_text(encoding="utf-8").strip()
        except UnicodeError as exc:
            raise AgentConfigurationError("agent.instructions.developer_file must be UTF-8") from exc

    def _developer_instructions(self) -> str:
        profile = str(self._instruction_config().get("gateway_profile") or TELEGRAM_GATEWAY_PROFILE)
        if profile != TELEGRAM_GATEWAY_PROFILE:
            raise AgentConfigurationError(f"unsupported Telegram gateway instruction profile: {profile}")
        bot_specific = self._bot_specific_instructions()
        capabilities = "Available dynamic tools: " + ", ".join(
            f"telegram_gateway.{spec['name']}" for spec in _tool_specs()[0]["tools"]
        ) + "."
        sections = [
            f"[Mandatory Telegram gateway instructions: {profile}]\n{TELEGRAM_GATEWAY_INSTRUCTIONS.strip()}",
            f"[Gateway capability manifest]\n{capabilities}",
        ]
        if bot_specific:
            sections.append(f"[Bot-specific developer instructions]\n{bot_specific}")
        sections.append(f"[Runtime scope]\nBot id: {self.paths.bot_id}. Transport: Telegram private owner DM.")
        return "\n\n".join(sections)

    def _contract_hash(self) -> str:
        payload = json.dumps(
            {"developerInstructions": self._developer_instructions(), "dynamicTools": _tool_specs()},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def validate(self, require_login: bool = False) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        agent = self._agent_config()
        context = str(agent.get("context") or "akuma").casefold()
        executable = self._executable()
        if not executable.exists() and shutil.which(str(executable)) is None:
            errors.append(f"Codex executable not found: {executable}")
        if "instructions" in agent and not isinstance(agent.get("instructions"), dict):
            errors.append("agent.instructions must be an object")
        if agent.get("personality") not in {None, "friendly", "pragmatic"}:
            errors.append("agent.personality must be friendly or pragmatic")
        try:
            self._developer_instructions()
        except AgentConfigurationError as exc:
            errors.append(str(exc))
        try:
            voice_transcription_settings(self.config)
        except ValueError as exc:
            errors.append(str(exc))
        if context == "subbot":
            if self.paths.codex_home is None:
                errors.append("subbot requires a CODEX_HOME")
            config_path = self.paths.codex_home / "config.toml" if self.paths.codex_home else None
            if config_path and not config_path.exists():
                errors.append("subbot config.toml is missing; run agent-init")
            elif config_path and "project_root_markers = []" not in config_path.read_text(encoding="utf-8"):
                errors.append("subbot config.toml must contain project_root_markers = []")
            filesystem = agent.get("filesystem") if isinstance(agent.get("filesystem"), dict) else {}
            if str(filesystem.get("read_access") or "restricted") == "restricted" and not self._supports_restricted_read():
                errors.append("configured Codex App Server does not support restricted filesystem read roots")
            if self.paths.codex_home:
                for skill in agent.get("skills", []):
                    if not isinstance(skill, dict) or not skill.get("name") or not skill.get("source"):
                        errors.append("each agent skill requires name and source")
                        continue
                    source = resolve_path(skill["source"], self.paths.bot_root)
                    destination = self.paths.codex_home / "skills" / str(skill["name"])
                    if not source.is_dir():
                        errors.append(f"skill source not found: {source}")
                    elif not destination.exists() or destination.resolve() != source.resolve():
                        errors.append(f"managed skill link is missing or invalid: {destination}")
        if not self.paths.working_directory.exists():
            errors.append(f"working directory does not exist: {self.paths.working_directory}")
        vault = self.config.get("vault") if isinstance(self.config.get("vault"), dict) else {}
        access = vault.get("access")
        if access not in {"read_only", "read_write"}:
            errors.append("vault.access must be read_only or read_write")
        database_path: str | None = None
        if not self.paths.vault_profile.exists():
            errors.append(f"Vault profile not found: {self.paths.vault_profile}")
        else:
            tool = self._keepass_config_tool()
            if tool:
                result = subprocess.run(
                    [os.sys.executable, str(tool), "validate", "--path", str(self.paths.vault_profile)],
                    capture_output=True, text=True, encoding="utf-8", cwd=tool.parent,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if result.returncode != 0:
                    errors.append("Vault profile failed KeePassVault validation")
            parser = ConfigParser(interpolation=None)
            try:
                parser.read(self.paths.vault_profile, encoding="utf-8")
                database_path = parser.get("keepass", "database_path")
                if not Path(database_path).exists():
                    errors.append(f"Vault database not found: {database_path}")
            except Exception:
                errors.append("Vault profile is invalid")
        auth = vault.get("auth") if isinstance(vault.get("auth"), dict) else {}
        if auth.get("mode") != "windows_credential_manager" or not auth.get("target"):
            errors.append("daemon agents require vault.auth.mode=windows_credential_manager and a target")
        if require_login and not errors:
            status = self.login_status()
            if status["returncode"] != 0:
                errors.append("Codex HOME is not authenticated")
        return {
            "ok": not errors,
            "bot_id": self.paths.bot_id,
            "context": context,
            "codex_home": str(self.paths.codex_home) if self.paths.codex_home else None,
            "working_directory": str(self.paths.working_directory),
            "vault_profile": str(self.paths.vault_profile),
            "vault_database": database_path,
            "vault_access": access,
            "errors": errors,
            "warnings": warnings,
        }

    def init(self) -> dict[str, Any]:
        agent = self._agent_config()
        context = str(agent.get("context") or "akuma").casefold()
        created: list[str] = []
        for directory in (self.paths.bot_root, self.paths.state_dir, self.paths.staging_dir, self.paths.vault_profile.parent):
            if not directory.exists():
                directory.mkdir(parents=True)
                created.append(str(directory))
        if context == "subbot":
            assert self.paths.codex_home is not None
            for directory in (self.paths.codex_home, self.paths.codex_home / "skills", self.paths.working_directory):
                if not directory.exists():
                    directory.mkdir(parents=True)
                    created.append(str(directory))
            config_path = self.paths.codex_home / "config.toml"
            if not config_path.exists():
                config_path.write_text(
                    'project_root_markers = []\napproval_policy = "never"\nsandbox_mode = "workspace-write"\n',
                    encoding="utf-8",
                )
                created.append(str(config_path))
            agents_path = self.paths.working_directory / "AGENTS.md"
            if not agents_path.exists():
                agents_path.write_text(
                    f"# {self.paths.bot_id}\n\nEste é o diretório de trabalho isolado do agente {self.paths.bot_id}. Não acesse arquivos fora das raízes autorizadas.\n",
                    encoding="utf-8",
                )
                created.append(str(agents_path))
        if not self.paths.contacts_path.exists():
            write_json(self.paths.contacts_path, {"version": 1, "contacts": []})
            created.append(str(self.paths.contacts_path))
        if not self.paths.vault_profile.exists():
            tool = self._keepass_config_tool()
            if not tool:
                raise AgentConfigurationError("KeePassVault config_tool.py not found")
            result = subprocess.run(
                [os.sys.executable, str(tool), "init", "--path", str(self.paths.vault_profile)],
                capture_output=True, text=True, encoding="utf-8", cwd=tool.parent,
            )
            if result.returncode != 0:
                raise AgentConfigurationError("KeePassVault profile initialization failed")
            created.append(str(self.paths.vault_profile))
        self.sync()
        return {"bot_id": self.paths.bot_id, "created": created, "validation": self.validate()}

    def sync(self) -> dict[str, Any]:
        agent = self._agent_config()
        if str(agent.get("context") or "akuma").casefold() != "subbot":
            return {"bot_id": self.paths.bot_id, "linked": [], "context": "akuma"}
        assert self.paths.codex_home is not None
        skills_dir = self.paths.codex_home / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        linked: list[str] = []
        removed: list[str] = []
        declared = {
            str((skills_dir / str(skill["name"])).resolve(strict=False))
            for skill in agent.get("skills", [])
            if isinstance(skill, dict) and skill.get("name")
        }
        with self.store.lock:
            managed = self.store.db.execute(
                "SELECT path,target FROM managed_home_resources WHERE kind='skill-link'"
            ).fetchall()
        for resource in managed:
            destination = Path(resource["path"])
            target = Path(resource["target"]) if resource["target"] else None
            if str(destination.resolve(strict=False)) in declared:
                continue
            if destination.exists() and target and destination.resolve() == target.resolve():
                destination.rmdir()
                removed.append(str(destination))
            with self.store.lock, self.store.db:
                self.store.db.execute("DELETE FROM managed_home_resources WHERE path=?", (str(destination),))
        for skill in agent.get("skills", []):
            if not isinstance(skill, dict) or not skill.get("name") or not skill.get("source"):
                raise AgentConfigurationError("each agent skill requires name and source")
            source = resolve_path(skill["source"], self.paths.bot_root)
            destination = skills_dir / str(skill["name"])
            if not source.is_dir():
                raise AgentConfigurationError(f"skill source not found: {source}")
            if destination.exists() or destination.is_symlink():
                if destination.resolve() != source.resolve():
                    raise AgentConfigurationError(f"unmanaged skill path conflict: {destination}")
                continue
            self._create_directory_link(source, destination, str(skill.get("link_type") or "auto"))
            with self.store.lock, self.store.db:
                self.store.db.execute(
                    "INSERT OR REPLACE INTO managed_home_resources(path,kind,target,created_at) VALUES(?,?,?,?)",
                    (str(destination), "skill-link", str(source), time.time()),
                )
            linked.append(str(destination))
        return {"bot_id": self.paths.bot_id, "linked": linked, "removed": removed}

    def _create_directory_link(self, source: Path, destination: Path, link_type: str) -> None:
        if link_type not in {"auto", "symlink", "junction"}:
            raise AgentConfigurationError("skill link_type must be auto, symlink or junction")
        use_junction = os.name == "nt" and link_type in {"auto", "junction"} and source.drive.casefold() == destination.drive.casefold()
        if use_junction:
            destination.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(destination), str(source)],
                capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                raise AgentConfigurationError(f"could not create skill junction: {destination}")
        else:
            destination.symlink_to(source, target_is_directory=True)

    def _keepass_config_tool(self) -> Path | None:
        candidates = [
            self.paths.bot_root.parents[index] / ".agents/skills/integrations/keePassVault/scripts/config_tool.py"
            for index in range(len(self.paths.bot_root.parents))
        ]
        return next((path for path in candidates if path.exists()), None)

    def _executable(self) -> Path:
        raw = self._agent_config().get("codex_executable")
        if raw:
            return Path(str(raw))
        found = shutil.which("codex")
        return Path(found or "codex")

    def _supports_restricted_read(self) -> bool:
        if self._restricted_read_capability is not None:
            return self._restricted_read_capability
        try:
            with tempfile.TemporaryDirectory(prefix="akuma-codex-schema-") as directory:
                result = subprocess.run(
                    [str(self._executable()), "app-server", "generate-json-schema", "--out", directory],
                    capture_output=True, text=True, encoding="utf-8", timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                schema = Path(directory) / "v2" / "TurnStartParams.json"
                self._restricted_read_capability = result.returncode == 0 and schema.exists() and "readOnlyAccess" in schema.read_text(encoding="utf-8")
        except Exception:
            self._restricted_read_capability = False
        return bool(self._restricted_read_capability)

    def environment(self) -> dict[str, str]:
        vault = self.config.get("vault") if isinstance(self.config.get("vault"), dict) else {}
        auth = vault.get("auth") if isinstance(vault.get("auth"), dict) else {}
        return {
            "KEEPASS_VAULT_CONFIG": str(self.paths.vault_profile),
            "KEEPASS_VAULT_ACCESS": str(vault.get("access") or ""),
            "KEEPASS_VAULT_AUTH_MODE": str(auth.get("mode") or ""),
            "KEEPASS_VAULT_AUTH_TARGET": str(auth.get("target") or ""),
        }

    def login_status(self) -> dict[str, Any]:
        env = os.environ.copy(); env.update(self.environment())
        if self.paths.codex_home is not None:
            env["CODEX_HOME"] = str(self.paths.codex_home)
        result = subprocess.run(
            [str(self._executable()), "login", "status"], cwd=self.paths.working_directory,
            env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = (result.stdout or result.stderr or "").strip()
        return {"returncode": result.returncode, "message": output[-500:]}

    def start(self) -> dict[str, Any]:
        if not self.enabled:
            self.state = "disabled"
            return self.status()
        validation = self.validate(require_login=True)
        if not validation["ok"]:
            self.state = "failed"; self.last_error = "; ".join(validation["errors"])
            return self.status()
        agent = self._agent_config()
        self.client = CodexAppServer(
            self._executable(), self.paths.codex_home, self.paths.working_directory,
            self.environment(), int(agent.get("request_timeout_seconds", 30)),
        )
        self.client.notification_handler = self._notification
        self.client.server_request_handler = self._server_request
        try:
            self.client.start()
            self.state = "running"; self.last_error = None
            for key in self.store.pending_keys():
                self._ensure_worker(key)
        except Exception as exc:
            self.state = "failed"; self.last_error = str(exc)
        return self.status()

    def stop(self) -> None:
        self.suspend()
        self.store.close()

    def suspend(self) -> bool:
        was_running = bool(self.client and self.state == "running")
        if self.client:
            self.client.stop()
        self.client = None
        self.state = "stopped"
        return was_running

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "state": self.state,
            "context": str(self._agent_config().get("context") or "akuma"),
            "codex_home": str(self.paths.codex_home) if self.paths.codex_home else None,
            "working_directory": str(self.paths.working_directory),
            "last_error": self.last_error or (self.client.last_error if self.client else None),
        }

    def enqueue(self, update_id: Any, message: dict[str, Any], payload: dict[str, Any], update_already_accepted: bool = False) -> bool:
        if not self.enabled or (not update_already_accepted and not self.store.accept_update(update_id)):
            return False
        chat = message.get("chat") or {}
        key = self.store.key(chat.get("id"), message.get("message_thread_id"))
        sort_key = int(message.get("date") or update_id or message.get("message_id") or time.time() * 1000)
        maximum = int(self._agent_config().get("max_pending_items", 50))
        if self._pending_count(key) >= maximum:
            ConversationStore._remove_payload_files(payload)
            self.bot_runtime.send(chat.get("id"), "A fila deste bot está cheia. Tente novamente mais tarde.")
            return False
        try:
            self.store.archive_payload_attachments(
                key, payload, self.paths.staging_dir, self.paths.state_dir / "attachments"
            )
        except Exception as exc:
            self.last_error = f"attachment archive failed: {exc}"
            ConversationStore._remove_payload_files(payload)
            self.bot_runtime.send(chat.get("id"), "Não foi possível armazenar o anexo com segurança.")
            return False
        self.store.add_inbox(key, sort_key, payload)
        self._ensure_worker(key)
        return True

    def _pending_count(self, key: tuple[str, str, int]) -> int:
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT COUNT(*) FROM inbox_items WHERE context_type=? AND chat_id=? AND message_thread_id=? AND status='pending'", key
            ).fetchone()
            return int(row[0])

    def _ensure_worker(self, key: tuple[str, str, int]) -> None:
        with self.workers_lock:
            current = self.workers.get(key)
            if current and current.is_alive():
                return
            worker = threading.Thread(target=self._worker, args=(key,), name=f"telegram-agent-{self.paths.bot_id}-{key[1]}", daemon=True)
            self.workers[key] = worker
            worker.start()

    def _worker(self, key: tuple[str, str, int]) -> None:
        try:
            while True:
                rows = self.store.take_pending(
                    key,
                    int(self._agent_config().get("max_pending_items", 50)),
                    int(self._agent_config().get("max_batch_attachment_bytes", 50 * 1024 * 1024)),
                )
                if not rows:
                    return
                typing_stop = threading.Event()
                typing_thread = threading.Thread(target=self._typing_loop, args=(key[1], typing_stop), daemon=True)
                typing_thread.start()
                try:
                    self._prepare_voice_transcriptions(rows)
                    with self.parallel_turns:
                        self._run_batch(key, rows)
                finally:
                    typing_stop.set(); typing_thread.join(timeout=1)
                    self.store.finish(rows)
                if not self.store.has_pending(key):
                    return
        finally:
            with self.workers_lock:
                self.workers.pop(key, None)
            if self.store.has_pending(key):
                self._ensure_worker(key)

    def _sandbox_policy(self) -> dict[str, Any]:
        agent = self._agent_config()
        mode = str(agent.get("sandbox") or "workspace-write")
        if mode == "danger-full-access":
            return {"type": "dangerFullAccess"}
        if mode == "read-only":
            policy: dict[str, Any] = {"type": "readOnly", "networkAccess": bool(agent.get("network_access", False))}
            if self._supports_restricted_read() and self._read_access().get("type") == "restricted":
                policy["access"] = self._read_access()
            return policy
        writable = [str(self.paths.working_directory), str(self.paths.staging_dir)]
        filesystem = agent.get("filesystem") if isinstance(agent.get("filesystem"), dict) else {}
        writable.extend(str(resolve_path(path, self.paths.bot_root)) for path in filesystem.get("additional_writable_roots", []))
        vault = self.config.get("vault") if isinstance(self.config.get("vault"), dict) else {}
        if vault.get("access") == "read_write":
            database = self._vault_database()
            if database:
                writable.append(str(database))
        policy: dict[str, Any] = {"type": "workspaceWrite", "writableRoots": list(dict.fromkeys(writable)),
                                  "networkAccess": bool(agent.get("network_access", False))}
        if self._supports_restricted_read() and self._read_access().get("type") == "restricted":
            policy["readOnlyAccess"] = self._read_access()
        return policy

    def _read_access(self) -> dict[str, Any]:
        agent = self._agent_config()
        filesystem = agent.get("filesystem") if isinstance(agent.get("filesystem"), dict) else {}
        context = str(agent.get("context") or "akuma").casefold()
        mode = str(filesystem.get("read_access") or ("restricted" if context == "subbot" else "full"))
        if mode == "full":
            return {"type": "fullAccess"}
        roots = [self.paths.working_directory, self.paths.staging_dir, self.paths.vault_profile]
        database = self._vault_database()
        if database:
            roots.append(database)
        for skill in agent.get("skills", []):
            if isinstance(skill, dict) and skill.get("source"):
                roots.append(resolve_path(skill["source"], self.paths.bot_root))
        roots.extend(resolve_path(path, self.paths.bot_root) for path in filesystem.get("readable_roots", []))
        return {"type": "restricted", "includePlatformDefaults": True,
                "readableRoots": [str(path) for path in dict.fromkeys(roots)]}

    def _vault_database(self) -> Path | None:
        parser = ConfigParser(interpolation=None)
        try:
            parser.read(self.paths.vault_profile, encoding="utf-8")
            return Path(parser.get("keepass", "database_path")).resolve()
        except Exception:
            return None

    def _run_batch(self, key: tuple[str, str, int], rows: list[dict[str, Any]]) -> None:
        if not self.client or self.state != "running":
            self.start()
        if not self.client or self.state != "running":
            self.bot_runtime.send(key[1], "O agente está indisponível no momento.")
            return
        conversation = self.store.conversation(key)
        generation = int(conversation["generation"])
        thread_id = conversation.get("codex_thread_id")
        contract_hash = self._contract_hash()
        if thread_id and conversation.get("contract_hash") != contract_hash:
            try:
                self.client.request("thread/delete", {"threadId": thread_id})
            except Exception:
                pass
            self.store.set_thread(key, None, generation)
            thread_id = None
        if thread_id:
            resume_parameters = self._thread_parameters(for_resume=True)
            resumed = self.client.request("thread/resume", {"threadId": thread_id, **resume_parameters})
            thread = resumed.get("thread") if isinstance(resumed.get("thread"), dict) else resumed
            self._validate_instruction_sources(resumed)
        else:
            started = self.client.request("thread/start", self._thread_parameters(for_resume=False))
            thread = started.get("thread") if isinstance(started.get("thread"), dict) else started
            thread_id = thread.get("id")
            if not isinstance(thread_id, str) or not thread_id:
                raise CodexProtocolError("thread/start did not return a thread id")
            self._validate_instruction_sources(started)
            self.store.set_thread(key, thread_id, generation, contract_hash)
        active = ActiveTurn(key, generation, str(thread_id), None, threading.Event())
        with self.active_lock:
            self.active[str(thread_id)] = active
        inputs = self._inputs(rows)
        try:
            result = self.client.request("turn/start", {"threadId": thread_id, "input": inputs, **self._turn_parameters()},
                                         timeout=int(self._agent_config().get("request_timeout_seconds", 30)))
            turn = result.get("turn") if isinstance(result.get("turn"), dict) else result
            active.turn_id = turn.get("id") if isinstance(turn, dict) else None
            if not active.completed.wait(int(self._agent_config().get("turn_timeout_seconds", 900))):
                if active.turn_id:
                    self.client.request("turn/interrupt", {"threadId": thread_id, "turnId": active.turn_id})
                raise CodexProtocolError("Codex turn timed out")
            current = self.store.conversation(key)
            if int(current["generation"]) != generation:
                return
            if active.final_text:
                self._send_chunks(key[1], active.final_text)
            elif active.status not in {"completed", "interrupted"}:
                self.bot_runtime.send(key[1], "O agente não conseguiu concluir esta solicitação.")
        except Exception as exc:
            self.last_error = str(exc)
            current = self.store.conversation(key)
            if int(current["generation"]) == generation:
                self.bot_runtime.send(key[1], "O agente não conseguiu concluir esta solicitação.")
        finally:
            self._cleanup_thoughts(active)
            for path in active.materialized_paths or []:
                ConversationStore._remove_file_and_empty_parent(path)
            with self.active_lock:
                self.active.pop(str(thread_id), None)

    def _thread_parameters(self, for_resume: bool = False) -> dict[str, Any]:
        agent = self._agent_config()
        params: dict[str, Any] = {
            "cwd": str(self.paths.working_directory),
            "approvalPolicy": str(agent.get("approval_policy") or "never"),
            "sandbox": str(agent.get("sandbox") or "workspace-write"),
            "developerInstructions": self._developer_instructions(),
        }
        if not for_resume:
            params["serviceName"] = f"akuma_telegram_{self.paths.bot_id}"
            params["dynamicTools"] = _tool_specs()
        if agent.get("model"): params["model"] = agent["model"]
        if agent.get("personality") in {"friendly", "pragmatic"}: params["personality"] = agent["personality"]
        return params

    def _turn_parameters(self) -> dict[str, Any]:
        agent = self._agent_config()
        params: dict[str, Any] = {
            "cwd": str(self.paths.working_directory),
            "approvalPolicy": str(agent.get("approval_policy") or "never"),
            "sandboxPolicy": self._sandbox_policy(),
        }
        if agent.get("model"): params["model"] = agent["model"]
        if agent.get("reasoning_effort"): params["effort"] = agent["reasoning_effort"]
        if agent.get("personality") in {"friendly", "pragmatic"}: params["personality"] = agent["personality"]
        return params

    def _validate_instruction_sources(self, result: dict[str, Any]) -> None:
        if str(self._agent_config().get("context") or "akuma").casefold() != "subbot":
            return
        sources = result.get("instructionSources") or []
        for source in sources:
            raw = source.get("path") if isinstance(source, dict) else source
            if not isinstance(raw, str):
                continue
            path = Path(raw.replace("file:///", "")).resolve()
            try:
                path.relative_to(self.paths.working_directory)
            except ValueError as exc:
                raise AgentConfigurationError(f"subbot inherited an external instruction source: {path}") from exc

    def _inputs(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        inputs: list[dict[str, Any]] = []
        for number, row in enumerate(rows, 1):
            payload = row["payload"]
            text = str(payload.get("text") or payload.get("caption") or "").strip()
            if text:
                prefix = f"[Mensagem {number}]\n" if len(rows) > 1 else ""
                inputs.append({"type": "text", "text": prefix + text})
            for attachment in payload.get("attachments", []):
                if attachment.get("kind") == "image":
                    inputs.append({"type": "localImage", "path": attachment["path"]})
                elif attachment.get("kind") == "voice":
                    transcript = attachment.get("transcription")
                    if isinstance(transcript, str) and transcript.strip():
                        prefix = f"[Mensagem {number}]\n" if len(rows) > 1 else ""
                        inputs.append({"type": "text", "text": prefix + "[Transcrição de mensagem de áudio recebida pelo Telegram]\n" + transcript})
                    elif attachment.get("transcription_error"):
                        inputs.append({"type": "text", "text": "[Mensagem de áudio recebida pelo Telegram sem transcrição automática disponível.]"})
                else:
                    inputs.append({"type": "text", "text": f"[Documento recebido: {attachment.get('name')}]\nCaminho local: {attachment['path']}"})
        if not inputs:
            inputs.append({"type": "text", "text": "O usuário enviou uma mensagem sem conteúdo textual suportado."})
        return inputs

    def _prepare_voice_transcriptions(self, rows: list[dict[str, Any]]) -> None:
        try:
            settings = voice_transcription_settings(self.config)
        except ValueError as exc:
            self.last_error = str(exc)
            settings = None
        if settings is None:
            return
        for row in rows:
            for attachment in row["payload"].get("attachments", []):
                if not isinstance(attachment, dict) or attachment.get("kind") != "voice" or attachment.get("transcription"):
                    continue
                try:
                    transcription = self.transcriber.transcribe(Path(str(attachment["path"])), settings)
                    attachment["transcription"] = transcription.text
                    if transcription.language:
                        attachment["transcription_language"] = transcription.language
                except VoiceTranscriptionError as exc:
                    attachment["transcription_error"] = exc.code
                    self.last_error = f"voice transcription failed: {exc.code}"
                except Exception as exc:
                    attachment["transcription_error"] = "internal_error"
                    self.last_error = f"voice transcription failed: {type(exc).__name__}"

    @staticmethod
    def _tool_result(value: Any, success: bool = True) -> dict[str, Any]:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return {"contentItems": [{"type": "inputText", "text": text}], "success": success}

    def _server_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method != "item/tool/call" or params.get("namespace") != "telegram_gateway":
            return self._tool_result("Unsupported gateway request.", False)
        thread_id = params.get("threadId")
        with self.active_lock:
            active = self.active.get(thread_id) if isinstance(thread_id, str) else None
        if not active or int(self.store.conversation(active.key)["generation"]) != active.generation:
            return self._tool_result("The active Telegram conversation is no longer available.", False)
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        tool = params.get("tool")
        if tool == "send_message":
            return self._tool_send_message(active, arguments)
        if tool == "ask_menu":
            return self._tool_ask_menu(active, arguments)
        if tool == "list_attachments":
            limit = max(1, min(50, int(arguments.get("limit", 20))))
            return self._tool_result({"attachments": self.store.list_attachments(active.key, limit)})
        if tool == "materialize_attachment":
            return self._tool_materialize_attachment(active, arguments)
        return self._tool_result("Unknown Telegram gateway tool.", False)

    def _tool_send_message(self, active: ActiveTurn, arguments: dict[str, Any]) -> dict[str, Any]:
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            return self._tool_result("text must contain between 1 and 4000 characters.", False)
        ttl = arguments.get("ttl_seconds", 0)
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not 0 <= ttl <= 86400:
            return self._tool_result("ttl_seconds must be an integer between 0 and 86400.", False)
        sent = self.bot_runtime.send(active.key[1], text, protect_content=bool(arguments.get("protect_content", False)))
        message_id = sent.get("message_id") if isinstance(sent, dict) else None
        if ttl and isinstance(message_id, int):
            try:
                self._schedule_deletion(active.key[1], message_id, time.time() + ttl)
            except Exception:
                self.bot_runtime.delete(active.key[1], message_id)
                return self._tool_result("The message was not retained because its deletion could not be guaranteed.", False)
        return self._tool_result({"sent": True, "ephemeral": bool(ttl), "ttl_seconds": ttl})

    def _tool_ask_menu(self, active: ActiveTurn, arguments: dict[str, Any]) -> dict[str, Any]:
        question = arguments.get("question")
        options = arguments.get("options")
        timeout = arguments.get("timeout_seconds", 120)
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            return self._tool_result("question must contain between 1 and 4000 characters.", False)
        if not isinstance(options, list) or not 2 <= len(options) <= 20:
            return self._tool_result("options must contain between 2 and 20 items.", False)
        normalized: list[dict[str, str]] = []
        identifiers: set[str] = set()
        for option in options:
            if not isinstance(option, dict):
                return self._tool_result("each option must contain id and label.", False)
            option_id, label = option.get("id"), option.get("label")
            if not isinstance(option_id, str) or not 1 <= len(option_id) <= 64 or option_id in identifiers:
                return self._tool_result("option ids must be unique strings of at most 64 characters.", False)
            if not isinstance(label, str) or not 1 <= len(label) <= 64:
                return self._tool_result("option labels must be strings of at most 64 characters.", False)
            identifiers.add(option_id); normalized.append({"id": option_id, "label": label})
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 10 <= timeout <= 300:
            return self._tool_result("timeout_seconds must be an integer between 10 and 300.", False)
        token = secrets.token_urlsafe(12)
        keyboard = {"inline_keyboard": [[{"text": option["label"], "callback_data": f"ag:{token}:{index}"}]
                                        for index, option in enumerate(normalized)]}
        sent = self.bot_runtime.send(active.key[1], question, reply_markup=keyboard,
                                     protect_content=bool(arguments.get("protect_content", False)))
        message_id = sent.get("message_id") if isinstance(sent, dict) else None
        if not isinstance(message_id, int):
            return self._tool_result("Telegram did not return a menu message id.", False)
        try:
            owner_id = int(active.key[1])
        except ValueError:
            self.bot_runtime.delete(active.key[1], message_id)
            return self._tool_result("Menus are only available in owner DMs.", False)
        session = MenuSession(token, active.key, active.generation, owner_id, message_id, normalized, threading.Event())
        with self.menus_lock:
            self.menus[token] = session
        try:
            self._schedule_deletion(active.key[1], message_id, time.time() + timeout)
        except Exception:
            with self.menus_lock:
                self.menus.pop(token, None)
            self.bot_runtime.delete(active.key[1], message_id)
            return self._tool_result("The menu could not be created with a guaranteed timeout.", False)
        session.completed.wait(timeout)
        with self.menus_lock:
            self.menus.pop(token, None)
        self._delete_managed_message(active.key[1], message_id)
        if session.selected:
            return self._tool_result({"selected": session.selected})
        return self._tool_result("The Telegram menu was cancelled or timed out.", False)

    def _tool_materialize_attachment(self, active: ActiveTurn, arguments: dict[str, Any]) -> dict[str, Any]:
        attachment_id = arguments.get("attachment_id")
        if not isinstance(attachment_id, str):
            return self._tool_result("attachment_id is required.", False)
        item = self.store.attachment(active.key, attachment_id)
        if not item:
            return self._tool_result("Attachment not found in this conversation.", False)
        source = Path(item["archive_path"])
        if not source.is_file() or source.is_symlink():
            return self._tool_result("The retained attachment is unavailable.", False)
        directory = self.paths.staging_dir / "materialized" / active.thread_id / secrets.token_urlsafe(8)
        directory.mkdir(parents=True, exist_ok=False)
        destination = directory / str(item["name"])
        shutil.copy2(source, destination)
        assert active.materialized_paths is not None
        active.materialized_paths.append(destination)
        return self._tool_result({"attachment_id": attachment_id, "name": item["name"], "path": str(destination),
                                  "mime_type": item.get("mime_type"), "size": item["size"]})

    def handle_callback(self, callback: dict[str, Any]) -> bool:
        data = callback.get("data")
        if not isinstance(data, str) or not data.startswith("ag:"):
            return False
        parts = data.split(":", 2)
        if len(parts) != 3:
            return True
        token = parts[1]
        with self.menus_lock:
            session = self.menus.get(token)
        query_id = callback.get("id")
        sender = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        valid = bool(session and sender.get("id") == session.owner_id and str(chat.get("id")) == session.key[1]
                     and message.get("message_id") == session.message_id and chat.get("type") == "private"
                     and self.contacts.is_owner(sender.get("id")))
        try:
            index = int(parts[2])
        except ValueError:
            index = -1
        if not valid or not session or not 0 <= index < len(session.options):
            if query_id:
                self.bot_runtime.answer_callback(query_id, "Opção inválida.", show_alert=True)
            return True
        session.selected = session.options[index]
        session.completed.set()
        if query_id:
            self.bot_runtime.answer_callback(query_id, "Selecionado.")
        return True

    def _notification(self, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            return
        with self.active_lock:
            active = self.active.get(thread_id)
        if not active:
            return
        current = self.store.conversation(active.key)
        if int(current["generation"]) != active.generation:
            return
        if method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else params
            active.status = str(turn.get("status") or "completed")
            active.completed.set()
            return
        if method != "item/completed":
            return
        item = params.get("item")
        if not isinstance(item, dict):
            return
        item_type = item.get("type")
        if item_type == "reasoning":
            fragments = item.get("summary") or item.get("content") or []
            text = "\n".join(str(fragment) for fragment in fragments if isinstance(fragment, str))
        else:
            text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        if item_type == "agentMessage" and item.get("phase") in {None, "final_answer"}:
            active.final_text = text
            return
        if item_type == "reasoning" or (item_type == "agentMessage" and item.get("phase") == "commentary"):
            normalized = " ".join(text.split())
            assert active.seen_thoughts is not None
            if normalized in active.seen_thoughts:
                return
            active.seen_thoughts.add(normalized)
            if not self.store.settings(active.key)["share_thoughts"]:
                return
            sent = self.bot_runtime.send(active.key[1], f"💭 {text}", protect_content=True)
            message_id = sent.get("message_id") if isinstance(sent, dict) else None
            if isinstance(message_id, int):
                assert active.thought_ids is not None
                active.thought_ids.append(message_id)

    def _cleanup_thoughts(self, active: ActiveTurn) -> None:
        if not self.store.settings(active.key)["delete_thoughts"]:
            return
        for message_id in active.thought_ids or []:
            try: self.bot_runtime.delete(active.key[1], message_id)
            except Exception: pass

    def _typing_loop(self, chat_id: str, stop: threading.Event) -> None:
        while not stop.is_set():
            try: self.bot_runtime.typing(chat_id)
            except Exception: pass
            stop.wait(4)

    def _send_chunks(self, chat_id: str, text: str) -> None:
        remaining = text
        while remaining:
            if len(remaining) <= 4000:
                chunk, remaining = remaining, ""
            else:
                cut = max(remaining.rfind("\n\n", 0, 4000), remaining.rfind("\n", 0, 4000), remaining.rfind(" ", 0, 4000))
                if cut < 1000: cut = 4000
                chunk, remaining = remaining[:cut], remaining[cut:].lstrip()
            self.bot_runtime.send(chat_id, chunk)

    def reset_conversation(self, chat_id: Any, thread_id: Any = None) -> None:
        key = self.store.key(chat_id, thread_id)
        old_thread, _generation = self.store.reset(key)
        with self.menus_lock:
            sessions = [session for session in self.menus.values() if session.key == key]
        for session in sessions:
            session.cancelled = True
            session.completed.set()
            self._delete_managed_message(session.key[1], session.message_id)
        if not old_thread or not self.client:
            return
        with self.active_lock:
            active = self.active.get(old_thread)
        try:
            if active and active.turn_id:
                self.client.request("turn/interrupt", {"threadId": old_thread, "turnId": active.turn_id})
            self.client.request("thread/delete", {"threadId": old_thread})
        except Exception as exc:
            self.last_error = f"thread deletion failed: {exc}"
        if active:
            active.completed.set()

    def close(self) -> None:
        self.suspend()
        with self.menus_lock:
            sessions = list(self.menus.values())
        for session in sessions:
            session.cancelled = True
            session.completed.set()
            self._delete_managed_message(session.key[1], session.message_id)
        with self.deletion_lock:
            timers = list(self.deletion_timers.values())
            self.deletion_timers.clear()
        for timer in timers:
            timer.cancel()
        self.store.close()
