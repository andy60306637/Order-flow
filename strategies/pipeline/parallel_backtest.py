"""
Parallel signal pre-computation for MultiPipelineStrategy.

Two-phase approach
──────────────────
Phase 1 (parallel, N processes):
  For each bar, run all pipeline stages using (klines, tick_map) as
  read-only inputs.  PositionGateStage / CooldownStage are skipped
  because they hold external state (open_count / last_signal_ms) that
  is never updated inside on_history; in practice they always pass.
  No equity or position state is required in this phase.

Phase 2 (sequential, main process):
  Walk bars in temporal order.  For each bar with a pre-computed
  candidate, apply the position gate (open_positions dict), compute qty
  with the *current* equity, then build signals and positions exactly as
  the original sequential loop does.

Sequential integrity is preserved: Phase 2 maintains the same trade
ordering and equity accounting as the original single-threaded loop.

OS strategy
───────────
Linux / macOS: fork  — zero-copy CoW; klines + tick_map are shared
               read-only across workers with no extra memory.
Windows:       spawn — safe but copies data once per worker.

Falls back to sequential if ProcessPoolExecutor raises any exception.
"""
from __future__ import annotations

import logging
import platform
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Per-worker globals (set once per process via initializer) ──────────────────
_W_KLINES = None
_W_TICK_MAP = None


def _ensure_importable() -> None:
    """Add project root to sys.path so worker processes can import modules."""
    root = str(Path(__file__).resolve().parent.parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)


def _worker_init(klines, tick_map) -> None:
    """Called once per worker process.  Sets module-level data references."""
    global _W_KLINES, _W_TICK_MAP
    _ensure_importable()
    _W_KLINES = klines
    _W_TICK_MAP = tick_map


def _run_all_no_gate(runner, klines, idx: int, dummy_equity: float, tick_map) -> list[dict]:
    """
    Run every enabled pipeline for bar[idx], skipping state-dependent gates.

    Returns a list of candidate dicts (one per pipeline that produced a signal).
    All values stored are equity-independent; qty is recomputed in Phase 2.
    """
    from strategies.pipeline.context import PipelineContext, SharedContext
    from strategies.pipeline.stages import CooldownStage, PositionGateStage

    runner._shared_ctx.invalidate(klines, idx)
    candidates: list[dict] = []

    for defn in runner._defs:
        if not defn.enabled:
            continue

        ctx = PipelineContext(
            klines          = klines,
            idx             = idx,
            equity          = dummy_equity * defn.allocation_weight,
            tick_map        = tick_map,
            pipeline_name   = defn.name,
            pipeline_weight = defn.allocation_weight,
            shared          = runner._shared_ctx,
        )

        # Run pipeline stages; skip state-dependent gates
        result_ctx: Optional[PipelineContext] = ctx
        for stage in defn.pipeline.stages:
            if isinstance(stage, (PositionGateStage, CooldownStage)):
                continue
            result_ctx = stage.process(result_ctx)  # type: ignore[arg-type]
            if result_ctx is None:
                break

        if result_ctx is None:
            continue

        if defn.direction_filter and result_ctx.direction != defn.direction_filter:
            continue

        candidates.append({
            "pipeline_name":     defn.name,
            "direction":         result_ctx.direction,
            "entry_price":       result_ctx.entry_price,
            "stop_price":        result_ctx.stop_price,
            "tp_price":          result_ctx.tp_price,
            "expected_rr":       result_ctx.expected_rr,
            "alpha_meta":        result_ctx.alpha_meta,   # contains entry_signal StrategySignal
            "regime":            result_ctx.regime,
            "regime_meta":       result_ctx.regime_meta,
            "allocation_weight": defn.allocation_weight,
            "tags":              list(defn.tags),
        })

    return candidates


def _chunk_worker(args: tuple) -> dict[int, list[dict]]:
    """ProcessPoolExecutor entry point: process bars [bar_start, bar_end)."""
    _ensure_importable()
    runner, bar_start, bar_end, dummy_equity = args
    klines   = _W_KLINES
    tick_map = _W_TICK_MAP
    if klines is None:
        return {}

    out: dict[int, list[dict]] = {}
    for i in range(bar_start, bar_end):
        cands = _run_all_no_gate(runner, klines, i, dummy_equity, tick_map)
        if cands:
            out[i] = cands
    return out


def _chunk_sequential(
    runner, klines, tick_map, bar_start: int, bar_end: int, dummy_equity: float
) -> dict[int, list[dict]]:
    """Fallback: same logic without multiprocessing."""
    out: dict[int, list[dict]] = {}
    for i in range(bar_start, bar_end):
        cands = _run_all_no_gate(runner, klines, i, dummy_equity, tick_map)
        if cands:
            out[i] = cands
    return out


def precompute_candidates(
    runner,
    klines: list,
    tick_map,
    dummy_equity: float,
    n_workers: int,
) -> dict[int, list[dict]]:
    """
    Phase 1: collect signal candidates for every bar using N worker processes.

    Args:
        runner:       MultiPipelineRunner instance (picklable).
        klines:       Full kline list (read-only in workers).
        tick_map:     Bar open_time → ndarray of ticks (read-only in workers).
        dummy_equity: Equity used for qty computation inside pipelines.
                      Should equal initial_capital so fee/risk checks are
                      realistic.  Actual qty is recomputed in Phase 2.
        n_workers:    Number of worker processes.

    Returns:
        {bar_idx: [candidate_dict, ...]} for bars where at least one
        pipeline produced a valid signal.
    """
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    n = len(klines)
    if n <= 1:
        return {}

    n_bars         = n - 1          # bar[0] has no history, always skipped
    actual_workers = min(n_workers, n_bars)
    chunk          = max(1, (n_bars + actual_workers - 1) // actual_workers)

    tasks: list[tuple] = []
    for w in range(actual_workers):
        start = 1 + w * chunk
        end   = min(start + chunk, n)
        if start >= end:
            break
        tasks.append((runner, start, end, dummy_equity))

    if not tasks:
        return {}

    mp_ctx_name = "fork" if platform.system() in ("Linux", "Darwin") else "spawn"
    mp_ctx      = multiprocessing.get_context(mp_ctx_name)

    all_candidates: dict[int, list[dict]] = {}
    try:
        with ProcessPoolExecutor(
            max_workers = len(tasks),
            mp_context  = mp_ctx,
            initializer = _worker_init,
            initargs    = (klines, tick_map),
        ) as exe:
            futures = [exe.submit(_chunk_worker, t) for t in tasks]
            for f in futures:
                all_candidates.update(f.result())
    except Exception as exc:
        logger.warning(
            "Parallel signal discovery failed (%s); falling back to sequential.", exc
        )
        all_candidates = {}
        for runner_, start, end, de in tasks:
            all_candidates.update(
                _chunk_sequential(runner_, klines, tick_map, start, end, de)
            )

    return all_candidates
