"""Backtest REST API."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from backtest.engine import BacktestConfig, simulate_trades
from backtest.time_slice import TimeSlice
from config.base import SYMBOLS, INTERVALS
from server.job_store import create_job, get_job, list_jobs, JobStatus
from strategies import STRATEGY_REGISTRY

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/backtest", tags=["backtest"])


# ── Request / Response models ─────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    symbol:          str   = "BTCUSDT"
    interval:        str   = "1m"
    strategy_name:   str   = "Wick Reversal 1m v4"
    start_ms:        int   = Field(..., description="start timestamp ms (UTC)")
    end_ms:          int   = Field(..., description="end timestamp ms (UTC)")
    use_tick_mode:   bool  = False
    tick_symbol:     str   = ""
    # BacktestConfig fields
    initial_capital: float = 10_000.0
    max_loss_pct:    float = 2.0
    leverage:        int   = 20
    fee_mode:        str   = "自訂"
    custom_fee_rate: float = 0.032 / 100
    slippage_bps:    float = 0.2
    compound:        bool  = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_backtest_sync(req: BacktestRequest) -> dict:
    from core.kline_cache import load_range_as_klines
    from core.tick_cache import build_bar_map_streaming, load_range_sharded, build_bar_map

    strategy_cls = STRATEGY_REGISTRY.get(req.strategy_name)
    if strategy_cls is None:
        raise ValueError(f"Unknown strategy: {req.strategy_name!r}")

    strategy = strategy_cls()
    sl = TimeSlice(label="api_range", segments=[(req.start_ms, req.end_ms)])

    klines = load_range_as_klines(req.symbol, req.interval, req.start_ms, req.end_ms)
    if not klines:
        raise ValueError("No klines found for the requested range.")

    tick_map: dict = {}
    if req.use_tick_mode:
        tick_sym = req.tick_symbol or req.symbol
        kline_times = [(k.open_time, k.close_time) for k in klines]
        streaming = build_bar_map_streaming(tick_sym, req.start_ms, req.end_ms, kline_times)
        if streaming is not None:
            tick_map = streaming
        else:
            ticks = load_range_sharded(tick_sym, req.start_ms, req.end_ms)
            if ticks is not None and len(ticks) > 0:
                tick_map = build_bar_map(ticks, kline_times)

    signals = strategy.on_history(klines, tick_map or None)

    bt_cfg = BacktestConfig(
        initial_capital=req.initial_capital,
        max_loss_pct=req.max_loss_pct / 100 if req.max_loss_pct > 1 else req.max_loss_pct,
        leverage=req.leverage,
        fee_mode=req.fee_mode,
        custom_fee_rate=req.custom_fee_rate,
        slippage_bps=req.slippage_bps,
        compound=req.compound,
    )
    result = simulate_trades(signals, bt_cfg)
    result["mode"] = "single"
    result["strategy_name"] = getattr(strategy, "name", strategy.__class__.__name__)
    result["backtest_start_ms"] = klines[0].open_time if klines else 0
    result["backtest_end_ms"] = klines[-1].open_time if klines else 0

    # strip non-serialisable objects
    result.pop("_snapshot_klines", None)
    result.pop("_snapshot_tick_map", None)
    result.pop("_snapshot_signals", None)
    return result


async def _backtest_worker(job_id: str, req: BacktestRequest) -> None:
    job = get_job(job_id)
    if job is None:
        return
    job.status = JobStatus.RUNNING
    job.progress = "Running backtest…"
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _run_backtest_sync, req)
        job.result = result
        job.status = JobStatus.DONE
        job.progress = "Done"
    except Exception as exc:
        logger.exception("Backtest job %s failed", job_id)
        job.status = JobStatus.ERROR
        job.error  = str(exc)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/strategies")
def list_strategies() -> dict:
    return {"strategies": list(STRATEGY_REGISTRY.keys())}


@router.get("/symbols")
def list_symbols() -> dict:
    return {"symbols": SYMBOLS, "intervals": INTERVALS}


@router.post("/run")
async def run_backtest(req: BacktestRequest, bg: BackgroundTasks) -> dict:
    job = create_job()
    bg.add_task(_backtest_worker, job.job_id, req)
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
