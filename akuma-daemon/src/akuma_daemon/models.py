from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class Job:
    id: str
    command: str
    args: tuple[str, ...] = ()
    schedule_type: str = "once"
    run_at: datetime | None = None
    interval_seconds: int | None = None
    cron: str | None = None
    cwd: str | None = None
    environment: dict[str, str] | None = None
    timeout_seconds: int = 3600
    enabled: bool = True

    def validate(self) -> None:
        if self.schedule_type not in {"once", "interval", "cron"}:
            raise ValueError("schedule_type must be once, interval, or cron")
        if self.schedule_type == "once" and self.run_at is None:
            raise ValueError("once jobs require run_at")
        if self.schedule_type == "interval" and (self.interval_seconds or 0) <= 0:
            raise ValueError("interval jobs require interval_seconds > 0")
        if self.schedule_type == "cron" and not self.cron:
            raise ValueError("cron jobs require a cron expression")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")

    def as_row(self) -> tuple[object, ...]:
        self.validate()
        return (self.id, self.command, json.dumps(self.args), self.schedule_type,
                iso(self.run_at) if self.run_at else None, self.interval_seconds,
                self.cron, self.cwd, json.dumps(self.environment or {}),
                self.timeout_seconds, int(self.enabled))

    @classmethod
    def from_row(cls, row) -> "Job":
        return cls(id=row[0], command=row[1], args=tuple(json.loads(row[2])),
                   schedule_type=row[3], run_at=datetime.fromisoformat(row[4]) if row[4] else None,
                   interval_seconds=row[5], cron=row[6], cwd=row[7],
                   environment=json.loads(row[8]), timeout_seconds=row[9], enabled=bool(row[10]))
