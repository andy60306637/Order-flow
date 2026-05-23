"""In-memory async job store for long-running tasks (backtest / research)."""
from __future__ import annotations

import asyncio
import uuid
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    DONE     = "done"
    ERROR    = "error"
    CANCELED = "canceled"


class Job:
    def __init__(self, job_id: str) -> None:
        self.job_id   = job_id
        self.status   = JobStatus.PENDING
        self.progress = ""
        self.result: Any  = None
        self.error:  str  = ""
        self._task: asyncio.Task | None = None

    def to_dict(self) -> dict:
        return {
            "job_id":   self.job_id,
            "status":   self.status,
            "progress": self.progress,
            "error":    self.error,
            "result":   self.result,
        }


_jobs: dict[str, Job] = {}


def create_job() -> Job:
    job = Job(str(uuid.uuid4()))
    _jobs[job.job_id] = job
    return job


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def list_jobs() -> list[dict]:
    return [
        {k: v for k, v in j.to_dict().items() if k != "result"}
        for j in _jobs.values()
    ]
