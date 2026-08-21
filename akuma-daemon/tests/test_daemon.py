from datetime import datetime, timedelta, timezone
import json
import sys
import threading
import time
import json

from akuma_daemon.executor import Executor
from akuma_daemon.models import Job
from akuma_daemon.rpc import rpc_call
from akuma_daemon.scheduler import Scheduler, cron_matches
from akuma_daemon.store import Store
from akuma_daemon.supervisor import Supervisor, read_endpoint
from akuma_daemon.telegram_manager import MAX_PAIR_TTL_SECONDS, TelegramManager


def setup(tmp_path):
    store = Store(tmp_path / "daemon.sqlite3")
    return store, Scheduler(store, Executor(store), poll_seconds=0)


def test_once_job_runs_and_records_output(tmp_path):
    store, scheduler = setup(tmp_path)
    job = Job("one", sys.executable, ("-c", "print('hello')"),
              run_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    scheduler.add(job)
    result = scheduler.run_once()[0][1]
    assert result.status == "success"
    assert store.executions("one")[0]["stdout"].strip() == "hello"
    assert scheduler.run_once() == []


def test_interval_job_is_not_due_immediately(tmp_path):
    store, scheduler = setup(tmp_path)
    job = Job("interval", sys.executable, ("-c", "pass"), "interval", interval_seconds=60)
    scheduler.add(job)
    assert store.get_job("interval") is not None
    assert scheduler.run_once() == []


def test_cron_matching():
    assert cron_matches("*/5 * * * *", datetime(2026, 1, 1, 12, 10, tzinfo=timezone.utc))
    assert not cron_matches("*/5 * * * *", datetime(2026, 1, 1, 12, 11, tzinfo=timezone.utc))


def test_timeout(tmp_path):
    store, _ = setup(tmp_path)
    job = Job("slow", sys.executable, ("-c", "import time; time.sleep(2)"), timeout_seconds=1)
    assert Executor(store).run(job).status == "timeout"


def test_supervisor_starts_scheduler_and_routes_task_commands(tmp_path):
    supervisor = Supervisor(tmp_path / "daemon")
    thread = threading.Thread(target=supervisor.run_forever, daemon=True)
    thread.start()
    endpoint = tmp_path / "daemon" / "supervisor" / "endpoint.json"
    for _ in range(100):
        if endpoint.exists():
            break
        time.sleep(0.05)
    host, port, token = read_endpoint(tmp_path / "daemon")
    services = rpc_call(host, port, token, "list-services")
    assert services[0]["name"] == "task-scheduler"
    assert services[0]["state"] == "running"
    assert services[1]["name"] == "telegram"
    assert services[1]["state"] == "running"
    assert rpc_call(host, port, token, "telegram.bots") == []
    job = {"id": "remote", "command": sys.executable,
           "args": ["-c", "print('remote-ok')"], "schedule_type": "once",
           "run_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
           "timeout_seconds": 10}
    rpc_call(host, port, token, "task.add", {"job": job})
    result = rpc_call(host, port, token, "task.run-now", {"id": "remote"})
    assert result["status"] == "success"
    assert "remote-ok" in result["stdout"]
    rpc_call(host, port, token, "shutdown")
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_telegram_pairing_and_fixed_reply(tmp_path):
    root = tmp_path / "telegram"
    bots = root / "bots"
    bots.mkdir(parents=True)
    (bots / "main.json").write_text(json.dumps({
        "id": "main", "listener": {"enabled": False, "mode": "polling"},
        "reply_text": "Não incomode o Akuma", "owners": [],
    }), encoding="utf-8")
    manager = TelegramManager(root)
    sent = []
    manager.bots["main"].send = lambda chat_id, text, thread_id=None: sent.append((chat_id, text, thread_id)) or {}
    pairing = manager.pair_request("main", ttl_seconds=60)
    manager.process_update("main", {"update_id": 1, "message": {
        "text": f"/pair {pairing['pin']}", "message_id": 10, "message_thread_id": 42,
        "chat": {"id": -100, "type": "supergroup"},
        "from": {"id": 123, "username": "owner", "first_name": "Owner"},
    }})
    assert manager.bots["main"].agent.contacts.owners()[0]["telegram_user_id"] == 123
    assert "owners" not in manager.bots["main"].config.data
    assert sent == [(-100, "Pairing concluído com sucesso. Sua conta agora é owner deste bot.", 42)]
    assert manager.pair_request("main")["pin"] != pairing["pin"]


def test_pair_request_replaces_previous_and_caps_timeout(tmp_path):
    root = tmp_path / "telegram"
    bots = root / "bots"
    bots.mkdir(parents=True)
    (bots / "main.json").write_text(json.dumps({"id": "main", "owners": []}), encoding="utf-8")
    manager = TelegramManager(root)
    first = manager.pair_request("main", ttl_seconds=99999)
    second = manager.pair_request("main", ttl_seconds=99999)
    stored = json.loads((bots / "main" / "state" / "pairing.json").read_text(encoding="utf-8"))
    assert second["pin"] != first["pin"]
    assert stored["expires_at"] - time.time() <= MAX_PAIR_TTL_SECONDS
    assert stored["hash"] != __import__("hashlib").sha256(f"main:{first['pin']}".encode()).hexdigest()


def test_pair_feedback_and_three_failures(tmp_path):
    root = tmp_path / "telegram"
    bots = root / "bots"
    bots.mkdir(parents=True)
    (bots / "main.json").write_text(json.dumps({"id": "main", "owners": []}), encoding="utf-8")
    manager = TelegramManager(root)
    sent = []
    manager.bots["main"].send = lambda chat_id, text, thread_id=None: sent.append(text) or {}
    message = lambda text: {"message": {"text": text, "chat": {"id": 1}, "from": {"id": 2}}}
    manager.process_update("main", message("hello"))
    assert "ainda não tem proprietário" in sent[-1]
    manager.pair_request("main")
    manager.process_update("main", message("/pair 111111"))
    manager.process_update("main", message("/pair 222222"))
    manager.process_update("main", message("/pair 333333"))
    assert "cancelado por excesso" in sent[-1]
    assert not (bots / "main" / "state" / "pairing.json").exists()


def test_totp_private_owner_flow_is_paginated_and_ephemeral(tmp_path):
    root = tmp_path / "telegram"
    bots = root / "bots"
    bots.mkdir(parents=True)
    (bots / "main.json").write_text(json.dumps({
        "id": "main", "owners": [{"user_id": 123}],
        "totp": {"enabled": True, "profile": "unused", "period_seconds": 30},
    }), encoding="utf-8")
    manager = TelegramManager(root)
    sent, deleted, callbacks, typing = [], [], [], []
    next_id = iter(range(100, 200))
    runtime = manager.bots["main"]
    runtime.send = lambda chat_id, text, thread_id=None, reply_markup=None, protect_content=False: sent.append((text, reply_markup, protect_content)) or {"message_id": next(next_id)}
    runtime.delete = lambda chat_id, message_id: deleted.append(message_id) or True
    runtime.answer_callback = lambda callback_id, text=None: callbacks.append((callback_id, text)) or True
    runtime.typing = lambda chat_id: typing.append(chat_id) or True
    manager._schedule_delete = lambda *args: None
    profile = {"real_password_entry": "real", "fake_password_entry": "fake"}
    manager.tokens.totp_profile = lambda path: profile
    manager.tokens.read = lambda _profile, entry, _field="password": {"real": "real-password", "fake": "fake-password"}[entry]
    manager.tokens.list_totp = lambda _profile: ["Mail/zulu", "Mail/Alpha"]
    manager.tokens.current_totp = lambda _profile, entry: "123456"
    owner_message = lambda text, message_id: {"message": {
        "text": text, "message_id": message_id, "chat": {"id": 123, "type": "private"},
        "from": {"id": 123},
    }}
    manager.process_update("main", owner_message("/totp", 1))
    assert sent[-1][0] == "Envie a senha TOTP."
    manager.process_update("main", owner_message("real-password", 2))
    assert sent[-1][0] == "Selecione um TOTP (1–2 de 2)."
    keyboard = sent[-1][1]["inline_keyboard"]
    assert [row[0]["text"] for row in keyboard[:2]] == ["Alpha", "zulu"]
    callback_data = keyboard[0][0]["callback_data"]
    manager.process_update("main", {"callback_query": {
        "id": "callback", "data": callback_data, "from": {"id": 123},
        "message": {"message_id": 101, "chat": {"id": 123, "type": "private"}},
    }})
    assert sent[-2][0] == "123456" and sent[-2][2] is False
    assert sent[-1][0].startswith("Expira em ") and sent[-1][2] is True
    assert all(item[2] for item in sent[:-2])
    assert 1 in deleted and 2 in deleted
    assert callbacks == [("callback", None)]
    assert typing == [123, 123]


def test_totp_is_ignored_outside_private_owner_chat(tmp_path):
    root = tmp_path / "telegram"
    bots = root / "bots"
    bots.mkdir(parents=True)
    (bots / "main.json").write_text(json.dumps({"id": "main", "owners": [{"user_id": 123}], "totp": {"enabled": True}}), encoding="utf-8")
    manager = TelegramManager(root)
    sent = []
    manager.bots["main"].send = lambda *args, **kwargs: sent.append(args) or {}
    manager.process_update("main", {"message": {
        "text": "/totp", "message_id": 1, "chat": {"id": -100, "type": "supergroup"}, "from": {"id": 123},
    }})
    assert sent == []


def test_totp_command_menu_is_scoped_only_to_owner_private_chat(tmp_path):
    root = tmp_path / "telegram"
    bots = root / "bots"
    bots.mkdir(parents=True)
    (bots / "main.json").write_text(json.dumps({
        "id": "main", "owners": [{"user_id": 123}, {"user_id": "invalid"}],
        "totp": {"enabled": True}, "listener": {"enabled": True, "mode": "polling"},
    }), encoding="utf-8")
    manager = TelegramManager(root)
    runtime = manager.bots["main"]
    configured = []
    runtime.start = lambda: setattr(runtime, "state", "running") or runtime.status()
    runtime.set_owner_commands = lambda owner_id: configured.append(owner_id) or True
    manager.start()
    assert configured == [123]


def test_owner_totp_command_uses_private_chat_scope(tmp_path):
    root = tmp_path / "telegram"
    bots = root / "bots"
    bots.mkdir(parents=True)
    (bots / "main.json").write_text(json.dumps({"id": "main", "owners": [], "totp": {"enabled": True}}), encoding="utf-8")
    runtime = TelegramManager(root).bots["main"]
    calls = []

    class Api:
        def set_my_commands(self, commands, scope):
            calls.append((commands, scope))
            return True

    runtime.api = Api()
    assert runtime.set_owner_totp_command(123)
    assert calls == [([{"command": "totp", "description": "Obter código de autenticação"}], {"type": "chat", "chat_id": 123})]


def test_totp_command_menu_is_reconciled_after_pairing(tmp_path):
    root = tmp_path / "telegram"
    bots = root / "bots"
    bots.mkdir(parents=True)
    (bots / "main.json").write_text(json.dumps({"id": "main", "owners": [], "totp": {"enabled": True}}), encoding="utf-8")
    manager = TelegramManager(root)
    runtime = manager.bots["main"]
    runtime.state = "running"
    configured = []
    runtime.set_owner_commands = lambda owner_id: configured.append(owner_id) or True
    runtime.send = lambda *args, **kwargs: {}
    pairing = manager.pair_request("main")
    manager.process_update("main", {"message": {
        "text": f"/pair {pairing['pin']}", "chat": {"id": -100, "type": "supergroup"},
        "from": {"id": 123},
    }})
    assert configured == [123]


def test_agent_payload_accepts_only_telegram_voice_for_eccovox(tmp_path):
    root = tmp_path / "telegram"
    bots = root / "bots"
    bots.mkdir(parents=True)
    (bots / "main.json").write_text(json.dumps({
        "id": "main", "agent": {"enabled": True},
        "voice_transcription": {"enabled": True},
    }), encoding="utf-8")
    manager = TelegramManager(root)
    runtime = manager.bots["main"]

    def download(_file_id, destination, _maximum):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"opus")
        return destination

    runtime.download = download
    voice_payload = manager._agent_payload(runtime, {
        "message_id": 10, "chat": {"id": 7},
        "voice": {"file_id": "voice-file", "file_size": 4, "duration": 2, "mime_type": "audio/ogg"},
    })
    assert voice_payload["attachments"][0]["kind"] == "voice"
    assert voice_payload["attachments"][0]["path"].endswith("voice-10.ogg")

    media_payload = manager._agent_payload(runtime, {
        "message_id": 11, "chat": {"id": 7},
        "audio": {"file_id": "media-file", "file_size": 4},
    })
    assert media_payload["attachments"] == []
    runtime.agent.close()
