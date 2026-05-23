"""Research Lab REST API — factor IC analysis."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from server.job_store import create_job, get_job, list_jobs, JobStatus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/research", tags=["research"])


# ── Request models ────────────────────────────────────────────────────────────

class ResearchRequest(BaseModel):
    symbol:            str        = "BTCUSDT"
    interval:          str        = "1m"
    selected_months:   list[str]  = Field(default_factory=list)
    factor_names:      list[str]  = Field(default_factory=list)
    horizons:          list[int]  = Field(default_factory=lambda: [3, 6, 12, 24])
    quantiles:         int        = 4
    entry_lag:         int        = 1
    train_ratio:       float      = 0.4
    use_tick_features: bool       = True
    regime_filter:     Any | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_research_sync(req: ResearchRequest) -> dict:
    from backtest.time_slice import TimeSlice, _month_start_ms, _next_month_start_ms
    from research.runner import ResearchConfig, run_research_with_regimes
    from research.registry import ensure_builtin_factors

    ensure_builtin_factors()

    slices: list[TimeSlice] = []
    for mk in sorted(req.selected_months):
        try:
            s_ms = _month_start_ms(mk)
            e_ms = _next_month_start_ms(mk) - 1
            slices.append(TimeSlice(label=mk, segments=[(s_ms, e_ms)]))
        except Exception:
            continue

    if not slices:
        raise ValueError("No valid months provided.")

    cfg = ResearchConfig(
        symbol=req.symbol,
        interval=req.interval,
        slices=slices,
        factor_names=req.factor_names,
        horizons=req.horizons,
        quantiles=req.quantiles,
        entry_lag=req.entry_lag,
        train_ratio=req.train_ratio,
        use_tick_features=req.use_tick_features,
        regime_filter=req.regime_filter,
    )

    results_by_regime = run_research_with_regimes(cfg)
    return {
        regime_key: res.to_dict()
        for regime_key, res in results_by_regime.items()
    }


async def _research_worker(job_id: str, req: ResearchRequest) -> None:
    job = get_job(job_id)
    if job is None:
        return
    job.status = JobStatus.RUNNING
    job.progress = "Running factor analysis…"
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _run_research_sync, req)
        job.result = result
        job.status = JobStatus.DONE
        job.progress = "Done"
    except Exception as exc:
        logger.exception("Research job %s failed", job_id)
        job.status = JobStatus.ERROR
        job.error  = str(exc)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/factors")
def list_factors(include_tick: bool = True) -> dict:
    from research.registry import ensure_builtin_factors, list_factor_infos
    ensure_builtin_factors()
    return {"factors": list_factor_infos(include_tick=include_tick)}


@router.post("/run")
async def run_research(req: ResearchRequest, bg: BackgroundTasks) -> dict:
    job = create_job()
    bg.add_task(_research_worker, job.job_id, req)
    return {"job_id": job.job_id, "status": job.status}


@router.get("/jobs")
def get_jobs() -> list:
    return list_jobs()


@router.get("/jobs/{job_id}")
def get_job_status(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job.to_dict()
