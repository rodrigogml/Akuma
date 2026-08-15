from __future__ import annotations

from pathlib import Path

from .supervisor import Supervisor


def data_directory(root: str | Path | None = None) -> Path:
    return Path(root or Path(__file__).resolve().parents[3] / "configs" / "daemon")


def create_supervisor(root: str | Path | None = None) -> Supervisor:
    return Supervisor(data_directory(root))


def run(root: str | Path | None = None, stop_event=None) -> None:
    supervisor = create_supervisor(root)
    if stop_event is not None:
        supervisor.stop_event = stop_event
    supervisor.run_forever()
