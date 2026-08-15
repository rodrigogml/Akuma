from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from .executor import Executor
from .models import Job, iso, utc_now
from .store import Store


def _cron_field(value: str, minimum: int, maximum: int) -> set[int]:
    result: set[int] = set()
    for part in value.split(","):
        base, _, step_text = part.partition("/")
        step = int(step_text or 1)
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start, end = map(int, base.split("-", 1))
        else:
            start = end = int(base)
        result.update(range(start, end + 1, step))
    if not result or min(result) < minimum or max(result) > maximum:
        raise ValueError("cron field out of range")
    return result


def cron_matches(expression: str, value: datetime) -> bool:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cron must have five fields")
    minute, hour, dom, month, dow = (
        _cron_field(fields[0], 0, 59), _cron_field(fields[1], 0, 23),
        _cron_field(fields[2], 1, 31), _cron_field(fields[3], 1, 12),
        _cron_field(fields[4], 0, 6),
    )
    return (value.minute in minute and value.hour in hour and value.day in dom
            and value.month in month and value.weekday() in dow)


def next_cron(expression: str, after: datetime) -> datetime:
    candidate = after.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if cron_matches(expression, candidate):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("cron expression has no occurrence in the next year")


class Scheduler:
    def __init__(self, store: Store, executor: Executor, poll_seconds: float = 1.0):
        self.store, self.executor, self.poll_seconds = store, executor, poll_seconds

    def add(self, job: Job) -> None:
        now = utc_now()
        if job.schedule_type == "once":
            next_run = job.run_at
        elif job.schedule_type == "interval":
            next_run = now + timedelta(seconds=job.interval_seconds or 0)
        else:
            next_run = next_cron(job.cron or "", now)
        self.store.upsert_job(job, iso(next_run) if next_run else None)

    def run_once(self) -> list[tuple[Job, object]]:
        results = []
        now = utc_now()
        for job in self.store.due_jobs(iso(now)):
            if job.schedule_type == "once":
                next_run = None
            elif job.schedule_type == "interval":
                next_run = iso(now + timedelta(seconds=job.interval_seconds or 0))
            else:
                next_run = iso(next_cron(job.cron or "", now))
            self.store.set_next_run(job.id, next_run, iso(now))
            results.append((job, self.executor.run(job)))
        return results

    def run_forever(self, stop_event=None) -> None:
        while stop_event is None or not stop_event.is_set():
            self.run_once()
            if stop_event is not None:
                stop_event.wait(self.poll_seconds)
            else:
                time.sleep(self.poll_seconds)
