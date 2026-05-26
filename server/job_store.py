"""In-memory async job store for long-running tasks (backtest / research)."""
from __future__ import annotations

import asyncio
import uuid
from enum import Enum
from typing import Any


def _sanitize(obj: Any) -> Any:
    """Recursively convert numpy scalar/array types to JSON-serializable Python types."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    # numpy scalars expose .item() which returns the native Python equivalent
    if hasattr(obj, "item") and callable(obj.item) and not isinstance(obj, type):
        try:
            return obj.item()
        except Exception:
            pass
    # numpy arrays
    if hasattr(obj, "tolist") and callable(obj.tolist) and not isinstance(obj, type):
        try:
            return obj.tolist()
        except Exception:
            pass
    return obj


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
        self.progress_pct = 0.0
        self.result: Any  = None
        self.artifacts: dict[str, Any] = {}
        self.error:  str  = ""
        self._task: asyncio.Task | None = None

    def to_dict(self) -> dict:
        return {
            "job_id":   self.job_id,
            "status":   self.status,
            "progress": self.progress,
            "progress_pct": self.progress_pct,
            "error":    self.error,
            "result":   _sanitize(self.result),
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
