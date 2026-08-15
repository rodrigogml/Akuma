from __future__ import annotations

import argparse
from datetime import datetime
import json
import threading

from .executor import Executor
from .models import Job
from .rpc import RpcServer
from .scheduler import Scheduler
from .store import Store


def job_from_payload(payload: dict) -> Job:
    return Job(
        id=payload["id"], command=payload["command"],
        args=tuple(payload.get("args", [])),
        schedule_type=payload.get("schedule_type", "once"),
        run_at=datetime.fromisoformat(payload["run_at"]) if payload.get("run_at") else None,
        interval_seconds=payload.get("interval_seconds"), cron=payload.get("cron"),
        cwd=payload.get("cwd"), environment=payload.get("environment"),
        timeout_seconds=payload.get("timeout_seconds", 3600),
        enabled=payload.get("enabled", True),
    )


def job_payload(job: Job) -> dict:
    return {"id": job.id, "command": job.command, "args": list(job.args),
            "schedule_type": job.schedule_type, "run_at": job.run_at.isoformat() if job.run_at else None,
            "interval_seconds": job.interval_seconds, "cron": job.cron, "cwd": job.cwd,
            "environment": job.environment or {}, "timeout_seconds": job.timeout_seconds,
            "enabled": job.enabled}


def run(args: argparse.Namespace) -> None:
    store = Store(args.data_dir / "daemon.sqlite3")
    executor = Executor(store)
    scheduler = Scheduler(store, executor)
    stop_event = threading.Event()

    def handle(method: str, params: dict):
        if method == "ping": return {"service": "task-scheduler", "state": "running"}
        if method == "task.list": return [job_payload(job) for job in store.list_jobs()]
        if method == "task.add": scheduler.add(job_from_payload(params["job"])); return {"id": params["job"]["id"]}
        if method == "task.pause": store.set_enabled(params["id"], False); return {"id": params["id"], "enabled": False}
        if method == "task.resume": store.set_enabled(params["id"], True); return {"id": params["id"], "enabled": True}
        if method == "task.remove": store.delete_job(params["id"]); return {"id": params["id"]}
        if method == "task.run-now":
            job = store.get_job(params["id"])
            if not job: raise ValueError("job not found")
            result = executor.run(job)
            return result.__dict__
        if method == "task.history":
            return [dict(row) for row in store.executions(params.get("id"))]
        if method == "shutdown": stop_event.set(); return {"state": "stopping"}
        raise ValueError(f"unknown method: {method}")

    rpc = RpcServer(args.host, args.port, args.token, handle)
    scheduler_thread = threading.Thread(target=scheduler.run_forever, args=(stop_event,), daemon=True)
    scheduler_thread.start()
    try:
        rpc.serve_forever(stop_event)
    finally:
        stop_event.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="akuma-task-scheduler")
    parser.add_argument("--data-dir", type=lambda value: __import__("pathlib").Path(value), required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    run(parser.parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
