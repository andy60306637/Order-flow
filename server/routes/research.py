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


class SignalDatasetRequest(ResearchRequest):
    factor_name:      str   = ""
    regime_key:       str   = "(all)"
    signal_quantile:  float = 0.20
    max_rows:         int   = 20_000


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slices_from_months(selected_months: list[str]) -> list:
    from backtest.time_slice import TimeSlice, _month_start_ms, _next_month_start_ms

    slices: list[TimeSlice] = []
    for mk in sorted(selected_months):
        try:
            s_ms = _month_start_ms(mk)
            e_ms = _next_month_start_ms(mk) - 1
            slices.append(TimeSlice(label=mk, segments=[(s_ms, e_ms)]))
        except Exception:
            continue
    if not slices:
        raise ValueError("No valid months provided.")
    return slices


def _build_research_config(req: ResearchRequest):
    from research.runner import ResearchConfig
    from research.regime_filter import RegimeFilterConfig

    slices = _slices_from_months(req.selected_months)
    regime_filter = (
        RegimeFilterConfig.from_dict(req.regime_filter)
        if isinstance(req.regime_filter, dict)
        else req.regime_filter
    )
    return ResearchConfig(
        symbol=req.symbol,
        interval=req.interval,
        slices=slices,
        factor_names=req.factor_names,
        horizons=req.horizons,
        quantiles=req.quantiles,
        entry_lag=req.entry_lag,
        train_ratio=req.train_ratio,
        use_tick_features=req.use_tick_features,
        regime_filter=regime_filter,
    )


def _run_research_sync(req: ResearchRequest, progress_callback=None) -> dict:
    from research.runner import run_research_with_regimes
    from research.registry import ensure_builtin_factors

    ensure_builtin_factors()

    cfg = _build_research_config(req)
    results_by_regime = run_research_with_regimes(cfg, progress_callback=progress_callback)
    return {
        regime_key: res.to_dict()
        for regime_key, res in results_by_regime.items()
    }


def _run_signal_dataset_sync(req: SignalDatasetRequest) -> dict:
    from research.runner import run_signal_dataset
    from research.registry import ensure_builtin_factors

    ensure_builtin_factors()
    factor_names = [req.factor_name] if req.factor_name else req.factor_names[:1]
    cfg_req = req.copy(update={"factor_names": factor_names})
    cfg = _build_research_config(cfg_req)
    return run_signal_dataset(
        cfg,
        factor_name=req.factor_name or (factor_names[0] if factor_names else ""),
        regime_key=req.regime_key,
        signal_quantile=req.signal_quantile,
        max_rows=req.max_rows,
    )


async def _research_worker(job_id: str, req: ResearchRequest) -> None:
    job = get_job(job_id)
    if job is None:
        return
    job.status = JobStatus.RUNNING
    job.progress = "Preparing research job..."
    job.progress_pct = 0.01

    def set_progress(message: str, pct: float | None = None) -> None:
        job.progress = message
        if pct is not None:
            job.progress_pct = max(0.0, min(0.99, float(pct)))

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _run_research_sync, req, set_progress)
        job.result = result
        job.status = JobStatus.DONE
        job.progress = "Done"
        job.progress_pct = 1.0
    except Exception as exc:
        logger.exception("Research job %s failed", job_id)
        job.status = JobStatus.ERROR
        job.error  = str(exc)
        job.progress_pct = 1.0


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/factors")
def list_factors(include_tick: bool = True) -> dict:
    from research.registry import ensure_builtin_factors, list_factor_infos
    ensure_builtin_factors()
    return {"factors": list_factor_infos(include_tick=include_tick)}


@router.get("/regime-options")
def regime_options() -> dict:
    from research.regime_filter import (
        ALL_DIMENSIONS,
        DIMENSION_DISPLAY,
        DIMENSION_LABELS,
    )
    return {
        "modes": ["filter", "matrix", "cross_matrix"],
        "dimensions": [
            {
                "key": dim,
                "label": DIMENSION_DISPLAY.get(dim, dim),
                "labels": DIMENSION_LABELS.get(dim, []),
            }
            for dim in ALL_DIMENSIONS
        ],
        "defaults": {
            "market_vol": {"lookback": 100},
            "vwap_zone": {"window": 24, "lookback": 100},
            "vol_profile": {"window": 24, "tick_size": 1.0, "value_area_pct": 0.70},
        },
    }


@router.post("/run")
async def run_research(req: ResearchRequest, bg: BackgroundTasks) -> dict:
    job = create_job()
    bg.add_task(_research_worker, job.job_id, req)
    return {"job_id": job.job_id, "status": job.status}


@router.post("/signals")
async def signal_dataset(req: SignalDatasetRequest) -> dict:
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _run_signal_dataset_sync, req)
    except Exception as exc:
        logger.exception("Signal dataset request failed")
        raise HTTPException(400, str(exc)) from exc


@router.get("/jobs")
def get_jobs() -> list:
    return list_jobs()


@router.get("/jobs/{job_id}")
def get_job_status(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job.to_dict()
