from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import Job, iso, utc_now


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, command TEXT NOT NULL, args TEXT NOT NULL,
          schedule_type TEXT NOT NULL, run_at TEXT, interval_seconds INTEGER,
          cron TEXT, cwd TEXT, environment TEXT NOT NULL,
          timeout_seconds INTEGER NOT NULL, enabled INTEGER NOT NULL,
          next_run TEXT, last_run TEXT
        );
        CREATE TABLE IF NOT EXISTS executions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
          started_at TEXT NOT NULL, finished_at TEXT, return_code INTEGER,
          stdout TEXT NOT NULL DEFAULT '', stderr TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL, error TEXT
        );
        """)
        self.db.commit()

    def upsert_job(self, job: Job, next_run: str | None = None) -> None:
        values = job.as_row()
        self.db.execute("""INSERT INTO jobs
          (id,command,args,schedule_type,run_at,interval_seconds,cron,cwd,environment,timeout_seconds,enabled,next_run)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET command=excluded.command,args=excluded.args,
          schedule_type=excluded.schedule_type,run_at=excluded.run_at,interval_seconds=excluded.interval_seconds,
          cron=excluded.cron,cwd=excluded.cwd,environment=excluded.environment,timeout_seconds=excluded.timeout_seconds,
          enabled=excluded.enabled,next_run=excluded.next_run""", (*values, next_run))
        self.db.commit()

    def get_job(self, job_id: str) -> Job | None:
        row = self.db.execute("SELECT id,command,args,schedule_type,run_at,interval_seconds,cron,cwd,environment,timeout_seconds,enabled FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def list_jobs(self) -> list[Job]:
        rows = self.db.execute("SELECT id,command,args,schedule_type,run_at,interval_seconds,cron,cwd,environment,timeout_seconds,enabled FROM jobs ORDER BY id")
        return [Job.from_row(row) for row in rows]

    def set_enabled(self, job_id: str, enabled: bool) -> None:
        self.db.execute("UPDATE jobs SET enabled=? WHERE id=?", (int(enabled), job_id)); self.db.commit()

    def delete_job(self, job_id: str) -> None:
        self.db.execute("DELETE FROM jobs WHERE id=?", (job_id,)); self.db.commit()

    def due_jobs(self, now: str | None = None) -> list[Job]:
        now = now or iso(utc_now())
        rows = self.db.execute("SELECT id,command,args,schedule_type,run_at,interval_seconds,cron,cwd,environment,timeout_seconds,enabled FROM jobs WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=?", (now,))
        return [Job.from_row(row) for row in rows]

    def set_next_run(self, job_id: str, next_run: str | None, last_run: str | None = None) -> None:
        self.db.execute("UPDATE jobs SET next_run=?,last_run=COALESCE(?,last_run) WHERE id=?", (next_run, last_run, job_id)); self.db.commit()

    def start_execution(self, job_id: str) -> int:
        cur = self.db.execute("INSERT INTO executions(job_id,started_at,status) VALUES(?,?,?)", (job_id, iso(utc_now()), "running")); self.db.commit(); return int(cur.lastrowid)

    def finish_execution(self, execution_id: int, status: str, return_code: int | None, stdout: str = "", stderr: str = "", error: str | None = None) -> None:
        self.db.execute("UPDATE executions SET finished_at=?,status=?,return_code=?,stdout=?,stderr=?,error=? WHERE id=?", (iso(utc_now()), status, return_code, stdout, stderr, error, execution_id)); self.db.commit()

    def executions(self, job_id: str | None = None) -> list[sqlite3.Row]:
        if job_id:
            return list(self.db.execute("SELECT * FROM executions WHERE job_id=? ORDER BY id DESC", (job_id,)))
        return list(self.db.execute("SELECT * FROM executions ORDER BY id DESC"))
