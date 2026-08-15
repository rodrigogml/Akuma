from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from .models import Job
from .store import Store


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    return_code: int | None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


class Executor:
    def __init__(self, store: Store):
        self.store = store

    def run(self, job: Job) -> ExecutionResult:
        execution_id = self.store.start_execution(job.id)
        try:
            env = os.environ.copy()
            env.update(job.environment or {})
            completed = subprocess.run(
                [job.command, *job.args], cwd=job.cwd, env=env,
                capture_output=True, text=True, timeout=job.timeout_seconds,
                shell=False, check=False,
            )
            result = ExecutionResult(
                "success" if completed.returncode == 0 else "failed",
                completed.returncode, completed.stdout, completed.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            result = ExecutionResult("timeout", None, exc.stdout or "", exc.stderr or "", "process timeout")
        except OSError as exc:
            result = ExecutionResult("error", None, error=str(exc))
        self.store.finish_execution(execution_id, result.status, result.return_code,
                                    result.stdout, result.stderr, result.error)
        return result
