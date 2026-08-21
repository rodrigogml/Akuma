from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess

from .service import create_supervisor, data_directory
from .supervisor import read_endpoint
from .rpc import rpc_call


def _payload(ns: argparse.Namespace) -> dict:
    kind = "cron" if ns.cron else "interval" if ns.interval else "once"
    return {"id": ns.id, "command": ns.command,
            "args": list(ns.args) + list(ns.command_args), "schedule_type": kind,
            "run_at": datetime.fromisoformat(ns.run_at).isoformat() if ns.run_at else None,
            "interval_seconds": ns.interval, "cron": ns.cron, "cwd": ns.cwd,
            "timeout_seconds": ns.timeout, "enabled": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="akuma-daemon")
    parser.add_argument("--data-dir", default=None)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("run")
    for action in ("list-services", "start", "stop", "restart", "enable", "disable", "status"):
        command = sub.add_parser(action)
        if action not in {"list-services"}:
            command.add_argument("service", nargs="?", default="task-scheduler")
    listing = sub.add_parser("list")
    listing.add_argument("--json", action="store_true")
    add = sub.add_parser("add")
    add.add_argument("id"); add.add_argument("command"); add.add_argument("args", nargs="*")
    add.add_argument("--arg", dest="command_args", action="append", default=[])
    add.add_argument("--at", dest="run_at"); add.add_argument("--interval", type=int)
    add.add_argument("--cron"); add.add_argument("--cwd"); add.add_argument("--timeout", type=int, default=3600)
    for action in ("pause", "resume", "run-now", "remove", "history"):
        command = sub.add_parser(action); command.add_argument("id")
    telegram = sub.add_parser("telegram")
    telegram_sub = telegram.add_subparsers(dest="telegram_action", required=True)
    telegram_sub.add_parser("bots")
    for action in ("status", "start-listener", "stop-listener", "owners", "pair-request",
                   "agent-init", "agent-sync", "agent-login", "agent-status", "agent-validate"):
        command = telegram_sub.add_parser(action); command.add_argument("bot_id")
        if action == "agent-validate":
            command.add_argument("--require-login", action="store_true")
    migrate = telegram_sub.add_parser("migrate-bots")
    migrate_mode = migrate.add_mutually_exclusive_group(required=True)
    migrate_mode.add_argument("--dry-run", action="store_true")
    migrate_mode.add_argument("--apply", action="store_true")
    send = telegram_sub.add_parser("send")
    send.add_argument("bot_id"); send.add_argument("chat_id"); send.add_argument("text")
    send.add_argument("--thread-id", type=int)

    ns = parser.parse_args(argv)
    root = data_directory(ns.data_dir)
    if ns.action == "run":
        create_supervisor(root).run_forever(); return 0
    host, port, token = read_endpoint(root)
    service_methods = {"list-services": "list-services", "start": "start", "stop": "stop",
                       "restart": "restart", "enable": "enable", "disable": "disable", "status": "status"}
    if ns.action in service_methods:
        params = {} if ns.action == "list-services" else {"service": ns.service}
        print(json.dumps(rpc_call(host, port, token, service_methods[ns.action], params), default=str))
        return 0
    if ns.action == "telegram":
        if ns.telegram_action == "bots":
            method, params = "telegram.bots", {}
        elif ns.telegram_action == "migrate-bots":
            method, params = "telegram.migrate-bots", {"apply": bool(ns.apply)}
        elif ns.telegram_action == "agent-login":
            spec = rpc_call(host, port, token, "telegram.bot.agent-login-prepare", {"bot_id": ns.bot_id})
            env = os.environ.copy(); env.update(spec.get("environment") or {})
            if spec.get("codex_home"):
                env["CODEX_HOME"] = spec["codex_home"]
            try:
                result = subprocess.run([spec["executable"], "login"], cwd=spec["working_directory"], env=env)
            finally:
                rpc_call(host, port, token, "telegram.bot.agent-login-finish", {
                    "bot_id": ns.bot_id, "restart": bool(spec.get("was_running")),
                })
            return int(result.returncode)
        elif ns.telegram_action == "send":
            method, params = "telegram.send", {"bot_id": ns.bot_id, "chat_id": ns.chat_id,
                                                 "text": ns.text, "message_thread_id": ns.thread_id}
        else:
            method = {
                "status": "telegram.bot.status", "start-listener": "telegram.bot.start-listener",
                "stop-listener": "telegram.bot.stop-listener", "owners": "telegram.bot.owners",
                "pair-request": "telegram.bot.pair-request",
                "agent-init": "telegram.bot.agent-init", "agent-sync": "telegram.bot.agent-sync",
                "agent-status": "telegram.bot.agent-status", "agent-validate": "telegram.bot.agent-validate",
            }[ns.telegram_action]
            params = {"bot_id": ns.bot_id}
            if ns.telegram_action == "agent-validate":
                params["require_login"] = bool(ns.require_login)
        print(json.dumps(rpc_call(host, port, token, method, params), ensure_ascii=False, default=str))
        return 0
    if ns.action == "list":
        result = rpc_call(host, port, token, "task.list")
        print(json.dumps(result, default=str) if ns.json else "\n".join(f"{job['id']}: {job['command']} ({job['schedule_type']})" for job in result))
    elif ns.action == "add":
        print(rpc_call(host, port, token, "task.add", {"job": _payload(ns)}))
    elif ns.action in {"pause", "resume", "remove", "run-now", "history"}:
        method = {"pause": "task.pause", "resume": "task.resume", "remove": "task.remove", "run-now": "task.run-now", "history": "task.history"}[ns.action]
        print(json.dumps(rpc_call(host, port, token, method, {"id": ns.id}), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
