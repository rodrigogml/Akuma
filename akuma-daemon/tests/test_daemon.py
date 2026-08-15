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
    assert manager.bots["main"].config.data["owners"][0]["user_id"] == 123
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
    stored = json.loads((root / "state" / "pairing.json").read_text(encoding="utf-8"))["main"]
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
    assert json.loads((root / "state" / "pairing.json").read_text(encoding="utf-8")) == {}
