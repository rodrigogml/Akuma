from __future__ import annotations

from dataclasses import dataclass
import json
import secrets
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from typing import Any

from .rpc import RpcServer, rpc_call


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class ManagedService:
    name: str
    command: list[str]
    port: int = 0
    token: str = ""
    process: subprocess.Popen | None = None
    enabled: bool = True
    state: str = "stopped"
    last_error: str | None = None


class Supervisor:
    """The only long-lived Akuma process registered with the operating system."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.supervisor_dir = self.root / "supervisor"
        self.supervisor_dir.mkdir(parents=True, exist_ok=True)
        self.endpoint_file = self.supervisor_dir / "endpoint.json"
        self.state_file = self.supervisor_dir / "services.json"
        self.host = "127.0.0.1"
        self.port = _free_port()
        self.token = secrets.token_urlsafe(32)
        scheduler_data = self.root / "services" / "task-scheduler"
        scheduler_data.mkdir(parents=True, exist_ok=True)
        telegram_data = self.root / "services" / "telegram"
        telegram_data.mkdir(parents=True, exist_ok=True)
        self.services = {
            "task-scheduler": ManagedService(
                "task-scheduler",
                [sys.executable, "-m", "akuma_daemon.task_scheduler_manager",
                 "--data-dir", str(scheduler_data)],
                 token=secrets.token_urlsafe(32),
            ),
            "telegram": ManagedService(
                "telegram",
                [sys.executable, "-m", "akuma_daemon.telegram_manager",
                 "--data-dir", str(telegram_data)],
                token=secrets.token_urlsafe(32),
            ),
        }
        self.stop_event = threading.Event()
        self.rpc = RpcServer(self.host, self.port, self.token, self.handle)
        self.port = self.rpc.port
        self._load_state()

    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            saved = json.loads(self.state_file.read_text(encoding="utf-8"))
            for name, values in saved.items():
                if name in self.services:
                    self.services[name].enabled = bool(values.get("enabled", True))
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        payload = {name: {"enabled": service.enabled} for name, service in self.services.items()}
        self.state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _write_endpoint(self) -> None:
        self.endpoint_file.write_text(json.dumps({
            "host": self.host, "port": self.port, "token": self.token,
        }), encoding="utf-8")

    def start_service(self, name: str) -> dict[str, Any]:
        service = self.services[name]
        if service.process and service.process.poll() is None:
            service.state = "running"
            return self.service_status(name)
        service.port = _free_port()
        command = service.command + ["--host", self.host, "--port", str(service.port), "--token", service.token]
        try:
            service.process = subprocess.Popen(command, cwd=str(self.root.parent.parent))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if service.process.poll() is not None:
                    raise RuntimeError(f"process exited with code {service.process.returncode}")
                try:
                    rpc_call(self.host, service.port, service.token, "ping", timeout=0.5)
                    service.state = "running"
                    service.last_error = None
                    return self.service_status(name)
                except OSError:
                    time.sleep(0.05)
            raise TimeoutError("service did not become ready")
        except Exception as exc:
            service.state, service.last_error = "failed", str(exc)
            return self.service_status(name)

    def stop_service(self, name: str) -> dict[str, Any]:
        service = self.services[name]
        if service.process and service.process.poll() is None:
            try:
                rpc_call(self.host, service.port, service.token, "shutdown")
                service.process.wait(timeout=5)
            except Exception:
                service.process.terminate()
                service.process.wait(timeout=5)
        service.state = "stopped"
        return self.service_status(name)

    def service_status(self, name: str) -> dict[str, Any]:
        service = self.services[name]
        if service.process and service.process.poll() is not None:
            service.state = "stopped" if service.state != "failed" else service.state
        return {"name": name, "state": service.state, "enabled": service.enabled,
                "pid": service.process.pid if service.process and service.process.poll() is None else None,
                "last_error": service.last_error}

    def handle(self, method: str, params: dict[str, Any]) -> Any:
        if method == "ping":
            return {"service": "akuma-daemon", "state": "running"}
        if method == "list-services":
            return [self.service_status(name) for name in self.services]
        name = params.get("service", "task-scheduler")
        if method == "start": return self.start_service(name)
        if method == "stop": return self.stop_service(name)
        if method == "restart": self.stop_service(name); return self.start_service(name)
        if method == "enable":
            self.services[name].enabled = True; self._save_state(); return self.service_status(name)
        if method == "disable":
            self.services[name].enabled = False; self.stop_service(name); self._save_state(); return self.service_status(name)
        if method == "status": return self.service_status(name)
        if method.startswith("task.") or method.startswith("telegram."):
            service_name = "task-scheduler" if method.startswith("task.") else "telegram"
            service = self.services[service_name]
            if not service.process or service.process.poll() is not None:
                raise RuntimeError(f"{service_name} is not running")
            return rpc_call(self.host, service.port, service.token, method, params)
        if method == "shutdown":
            self.stop_event.set(); return {"state": "stopping"}
        raise ValueError(f"unknown method: {method}")

    def start_enabled_services(self) -> None:
        for name, service in self.services.items():
            if service.enabled:
                self.start_service(name)

    def run_forever(self) -> None:
        self._write_endpoint()
        self._save_state()
        self.start_enabled_services()
        try:
            self.rpc.serve_forever(self.stop_event)
        finally:
            for name in self.services:
                self.stop_service(name)
            self.endpoint_file.unlink(missing_ok=True)


def read_endpoint(root: str | Path) -> tuple[str, int, str]:
    endpoint = Path(root) / "supervisor" / "endpoint.json"
    values = json.loads(endpoint.read_text(encoding="utf-8"))
    return values["host"], int(values["port"]), values["token"]
