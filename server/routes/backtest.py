"""Backtest REST API."""
from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timezone
from io import BytesIO
import logging
import shutil
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backtest.engine import BacktestConfig, simulate_trades
from backtest.time_slice import TimeSlice
from config.base import SYMBOLS, INTERVALS
from server.job_store import create_job, get_job, list_jobs, JobStatus
from strategies import STRATEGY_REGISTRY

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/backtest", tags=["backtest"])
ProgressCallback = Callable[[str, float | None], None]


# ── Data availability ─────────────────────────────────────────────────────────

def _scan_available_klines() -> list[dict]:
    """Scan the kline cache directory and return info for each available file."""
    from core import kline_cache, data_paths
    import numpy as np

    cache_dir = data_paths.kline_cache_dir()
    results: list[dict] = []
    if not cache_dir.exists():
        return results

    for path in sorted(cache_dir.glob("*.npy")):
        stem = path.stem  # e.g. BTCUSDT_1m
        parts = stem.rsplit("_", 1)
        if len(parts) != 2:
            continue
        symbol, interval = parts[0], parts[1]
        try:
            arr = np.load(str(path), mmap_mode="r")
            if arr.ndim < 2 or arr.shape[0] == 0:
                continue
            results.append({
                "symbol":   symbol.upper(),
                "interval": interval,
                "start_ms": int(arr[0, 0]),
                "end_ms":   int(arr[-1, 0]),
                "count":    int(arr.shape[0]),
            })
        except Exception as exc:
            logger.warning("available-data scan error [%s]: %s", path.name, exc)

    return results


def _scan_tick_coverage() -> list[dict]:
    """Return aggregated tick shard coverage from all active shard aliases."""
    from core import tick_cache
    from backtest.time_slice import TimeSliceManager, discover_tick_sources

    results: list[dict] = []
    for sym in SYMBOLS:
        sources = discover_tick_sources(sym)
        ranges: list[tuple[int, int]] = []
        months: set[str] = set()
        shard_parts = 0
        for source in sources:
            info = tick_cache.info(source)
            if info is None:
                continue
            ranges.append((int(info["start_ms"]), int(info["end_ms"])))
            mgr = TimeSliceManager(source)
            for shard in mgr.available_shards():
                if shard.available:
                    months.add(shard.month_key)
                    shard_parts += 1
        if ranges:
            results.append({
                "symbol":      sym,
                "start_ms":    min(start for start, _ in ranges),
                "end_ms":      max(end for _, end in ranges),
                "source":      "shards",
                "shard_sets":  len([s for s in sources if tick_cache.info(s) is not None]),
                "shard_parts": shard_parts,
                "months":      sorted(months),
            })
    return results


# ── Request / Response models ─────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    symbol:          str   = "BTCUSDT"
    interval:        str   = "1m"
    strategy_name:   str   = "Wick Reversal 1m v4"
    start_ms:        int   = Field(..., description="start timestamp ms (UTC)")
    end_ms:          int   = Field(..., description="end timestamp ms (UTC)")
    use_tick_mode:   bool  = False
    tick_symbol:     str   = ""
    slice_mode:      str   = "range"  # range | multi_select | walk_forward
    selected_months: list[str] = Field(default_factory=list)
    wf_segments:     int   = 4
    wf_oos_fraction: float = 0.3
    wf_anchored:     bool  = False
    # BacktestConfig fields
    initial_capital: float = 10_000.0
    max_loss_pct:    float = 2.0
    leverage:        int   = 20
    fee_mode:        str   = "自訂"
    custom_fee_rate: float = 0.032 / 100
    slippage_bps:    float = 0.2
    compound:        bool  = False


class TickImportRequest(BaseModel):
    symbol:    str        = "BTCUSDT"
    paths:     list[str]  = Field(default_factory=list)
    folder:    str        = ""
    recursive: bool       = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bt_config(req: BacktestRequest) -> BacktestConfig:
    return BacktestConfig(
        initial_capital=req.initial_capital,
        max_loss_pct=req.max_loss_pct / 100 if req.max_loss_pct > 1 else req.max_loss_pct,
        leverage=req.leverage,
        fee_mode=req.fee_mode,
        custom_fee_rate=req.custom_fee_rate,
        slippage_bps=req.slippage_bps,
        compound=req.compound,
    )


def _month_segments(month_keys: list[str]) -> list[tuple[int, int]]:
    from backtest.time_slice import _month_start_ms, _next_month_start_ms
    segments: list[tuple[int, int]] = []
    for mk in sorted(set(month_keys)):
        try:
            segments.append((_month_start_ms(mk), _next_month_start_ms(mk) - 1))
        except Exception:
            continue
    return segments


def _request_slices(req: BacktestRequest) -> list[TimeSlice] | list[tuple[TimeSlice, TimeSlice]]:
    from backtest.time_slice import (
        TimeSliceManager,
        WalkForwardConfig,
        build_tick_source_range_slice,
        build_tick_source_slice,
        build_tick_source_walk_forward,
        tick_source_segments,
    )

    tick_source = req.tick_symbol or req.symbol

    if req.use_tick_mode and req.slice_mode == "multi_select" and req.selected_months:
        sl = build_tick_source_slice(tick_source, req.selected_months, label="Custom")
        if not sl.segments:
            raise ValueError("No tick shards found for the selected months.")
        return [sl]

    if req.slice_mode == "multi_select" and req.selected_months:
        segments = _month_segments(req.selected_months)
        if segments:
            return [TimeSlice(label="Custom", segments=segments)]

    if req.slice_mode == "walk_forward":
        cfg = WalkForwardConfig(
            n_segments=max(2, req.wf_segments),
            oos_fraction=min(0.5, max(0.1, req.wf_oos_fraction)),
            anchored=req.wf_anchored,
        )
        if req.use_tick_mode:
            if req.selected_months:
                source_segments = tick_source_segments(tick_source, month_keys=req.selected_months)
                start_ms = None
                end_ms = None
            else:
                source_segments = tick_source_segments(tick_source, start_ms=req.start_ms, end_ms=req.end_ms)
                start_ms = req.start_ms
                end_ms = req.end_ms
            if not source_segments:
                raise ValueError("No tick shards found for the selected walk-forward range.")
            slices = build_tick_source_walk_forward(
                source_segments,
                cfg,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            if not slices:
                raise ValueError("Unable to build walk-forward slices from the selected tick shards.")
            return slices

        mgr = TimeSliceManager(req.tick_symbol or req.symbol)
        return mgr.build_walk_forward(
            req.start_ms,
            req.end_ms,
            cfg,
        )

    if req.use_tick_mode:
        sl = build_tick_source_range_slice(tick_source, req.start_ms, req.end_ms, label="api_range")
        if not sl.segments:
            raise ValueError("No tick shards found for the requested range.")
        return [sl]

    return [TimeSlice(label="api_range", segments=[(req.start_ms, req.end_ms)])]


def _emit_progress(progress_callback: ProgressCallback | None, message: str, pct: float | None = None) -> None:
    if progress_callback is None:
        return
    if pct is not None:
        pct = max(0.0, min(1.0, float(pct)))
    progress_callback(message, pct)


def _load_segments(
    req: BacktestRequest,
    slices: list[TimeSlice],
    progress_callback: ProgressCallback | None = None,
    progress_start: float = 0.0,
    progress_end: float = 1.0,
) -> tuple[list, dict]:
    from core.kline_cache import load_range_as_klines
    from core.tick_cache import build_bar_map_streaming, load_range_sharded, build_bar_map

    all_klines = []
    tick_segments: list[tuple[str, int, int]] = []
    total_segments = max(1, sum(len(sl.segments) for sl in slices))
    loaded_segments = 0
    span = max(0.0, progress_end - progress_start)
    for sl in slices:
        segment_symbols = getattr(sl, "segment_symbols", []) or []
        for idx, (start_ms, end_ms) in enumerate(sl.segments):
            _emit_progress(
                progress_callback,
                f"Loading kline segment {loaded_segments + 1}/{total_segments}",
                progress_start + span * (0.10 + 0.35 * (loaded_segments / total_segments)),
            )
            all_klines.extend(load_range_as_klines(req.symbol, req.interval, start_ms, end_ms))
            if req.use_tick_mode:
                tick_segments.append((
                    segment_symbols[idx] if idx < len(segment_symbols) else (req.tick_symbol or req.symbol),
                    start_ms,
                    end_ms,
                ))
            loaded_segments += 1

    if not all_klines:
        raise ValueError("No klines found for the requested range.")

    seen: dict[int, Any] = {}
    for k in all_klines:
        seen[k.open_time] = k
    klines = [seen[t] for t in sorted(seen)]

    tick_map: dict = {}
    if req.use_tick_mode and tick_segments:
        kline_times = [(k.open_time, k.close_time) for k in klines]
        total_tick_segments = max(1, len(tick_segments))
        for idx, (tick_sym, start_ms, end_ms) in enumerate(tick_segments, start=1):
            _emit_progress(
                progress_callback,
                f"Loading tick bars {idx}/{total_tick_segments}: {tick_sym}",
                progress_start + span * (0.45 + 0.35 * ((idx - 1) / total_tick_segments)),
            )
            streaming = build_bar_map_streaming(tick_sym, start_ms, end_ms, kline_times)
            if streaming is not None:
                tick_map.update(streaming)
            else:
                ticks = load_range_sharded(tick_sym, start_ms, end_ms)
                if ticks is not None and len(ticks) > 0:
                    tick_map.update(build_bar_map(ticks, kline_times))
    _emit_progress(progress_callback, f"Loaded {len(klines):,} kline rows", progress_start + span * 0.85)
    return klines, tick_map


def _run_single_slices(
    req: BacktestRequest,
    strategy: Any,
    bt_cfg: BacktestConfig,
    slices: list[TimeSlice],
    progress_callback: ProgressCallback | None = None,
    progress_start: float = 0.0,
    progress_end: float = 1.0,
) -> dict:
    klines, tick_map = _load_segments(req, slices, progress_callback, progress_start, progress_end)
    span = max(0.0, progress_end - progress_start)
    _emit_progress(progress_callback, "Generating strategy signals...", progress_start + span * 0.88)
    signals = strategy.on_history(klines, tick_map or None)
    _emit_progress(progress_callback, "Simulating trades...", progress_start + span * 0.94)
    result = simulate_trades(signals, bt_cfg)
    result["mode"] = "single" if req.slice_mode != "multi_select" else "multi_select"
    result["strategy_name"] = getattr(strategy, "name", strategy.__class__.__name__)
    result["backtest_start_ms"] = klines[0].open_time if klines else 0
    result["backtest_end_ms"] = klines[-1].open_time if klines else 0
    result["_snapshot_klines"] = klines
    result["_snapshot_tick_map"] = tick_map
    result["_snapshot_signals"] = signals
    result["_snapshot_bar_features"] = getattr(strategy, "_last_bar_features", {})
    return result


def _attach_analysis(result: dict, initial_capital: float) -> None:
    from backtest.engine import run_monte_carlo

    trades = [t for t in result.get("trade_list", []) if not t.get("skipped")]
    result["monte_carlo"] = {
        "iterations": 1000,
        "final_equity": run_monte_carlo(trades, initial_capital, n_iterations=1000, seed=42),
    }
    buckets: dict[tuple[str, int], list[float]] = {}
    for t in trades:
        side = str(t.get("dir", ""))
        hour = t.get("session_hour")
        if not isinstance(hour, int):
            ts = int(t.get("entry_time") or 0)
            hour = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).hour if ts else -1
        if side and 0 <= hour <= 23:
            buckets.setdefault((side, hour), []).append(float(t.get("net_pnl", 0.0)))
    result["optimization_heatmap"] = [
        {
            "side": side,
            "hour": hour,
            "avg_net_pnl": sum(vals) / len(vals),
            "trades": len(vals),
        }
        for (side, hour), vals in sorted(buckets.items())
    ]


def _run_backtest_sync(req: BacktestRequest, progress_callback: ProgressCallback | None = None) -> dict:
    _emit_progress(progress_callback, "Preparing backtest...", 0.03)
    strategy_cls = STRATEGY_REGISTRY.get(req.strategy_name)
    if strategy_cls is None:
        raise ValueError(f"Unknown strategy: {req.strategy_name!r}")

    strategy = strategy_cls()
    bt_cfg = _bt_config(req)
    _emit_progress(progress_callback, "Building time slices...", 0.08)
    slices = _request_slices(req)

    if slices and isinstance(slices[0], tuple):
        from backtest.engine import _build_stats
        combined_trades = []
        segment_results = []
        snapshot_klines = []
        snapshot_tick_map: dict = {}
        snapshot_bar_features: dict = {}
        equity = bt_cfg.initial_capital
        total = max(1, len(slices))
        for idx, (is_sl, oos_sl) in enumerate(slices, start=1):
            start = 0.12 + 0.76 * ((idx - 1) / total)
            end = 0.12 + 0.76 * (idx / total)
            _emit_progress(progress_callback, f"Walk-forward segment {idx}/{total}: in-sample", start)
            is_result = _run_single_slices(
                req, strategy, bt_cfg, [is_sl],
                progress_callback, start, start + (end - start) * 0.45,
            )
            oos_cfg = BacktestConfig(
                initial_capital=equity,
                max_loss_pct=bt_cfg.max_loss_pct,
                leverage=bt_cfg.leverage,
                fee_mode=bt_cfg.fee_mode,
                custom_fee_rate=bt_cfg.custom_fee_rate,
                slippage_bps=bt_cfg.slippage_bps,
                compound=bt_cfg.compound,
            )
            _emit_progress(progress_callback, f"Walk-forward segment {idx}/{total}: out-of-sample", start + (end - start) * 0.48)
            oos_result = _run_single_slices(
                req, strategy, oos_cfg, [oos_sl],
                progress_callback, start + (end - start) * 0.48, end,
            )
            equity = oos_result.get("final_equity", equity)
            combined_trades.extend(oos_result.get("trade_list", []))
            segment_results.append({
                "is_label": is_sl.label,
                "oos_label": oos_sl.label,
                "is_result": {k: v for k, v in is_result.items() if not k.startswith("_")},
                "oos_result": {k: v for k, v in oos_result.items() if not k.startswith("_")},
            })
            snapshot_klines.extend(oos_result.get("_snapshot_klines") or [])
            snapshot_tick_map.update(oos_result.get("_snapshot_tick_map") or {})
            snapshot_bar_features.update(is_result.get("_snapshot_bar_features") or {})
            snapshot_bar_features.update(oos_result.get("_snapshot_bar_features") or {})
        result = _build_stats(combined_trades, bt_cfg, equity, 0)
        result["mode"] = "walk_forward"
        result["segments"] = segment_results
        result["strategy_name"] = getattr(strategy, "name", strategy.__class__.__name__)
        result["backtest_start_ms"] = min((k.open_time for k in snapshot_klines), default=req.start_ms)
        result["backtest_end_ms"] = max((k.open_time for k in snapshot_klines), default=req.end_ms)
        result["_snapshot_klines"] = sorted({k.open_time: k for k in snapshot_klines}.values(), key=lambda k: k.open_time)
        result["_snapshot_tick_map"] = snapshot_tick_map
        result["_snapshot_signals"] = []
        result["_snapshot_bar_features"] = snapshot_bar_features
    else:
        result = _run_single_slices(req, strategy, bt_cfg, slices, progress_callback, 0.12, 0.88)  # type: ignore[arg-type]

    _emit_progress(progress_callback, "Building analysis artifacts...", 0.92)
    _attach_analysis(result, bt_cfg.initial_capital)

    return result


async def _backtest_worker(job_id: str, req: BacktestRequest) -> None:
    job = get_job(job_id)
    if job is None:
        return
    job.status = JobStatus.RUNNING
    job.progress = "Preparing backtest..."
    job.progress_pct = 0.01

    def set_progress(message: str, pct: float | None = None) -> None:
        job.progress = message
        if pct is not None:
            job.progress_pct = max(0.0, min(0.99, float(pct)))

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _run_backtest_sync, req, set_progress)
        job.artifacts = {
            "snapshot_klines": result.pop("_snapshot_klines", None),
            "snapshot_tick_map": result.pop("_snapshot_tick_map", None),
            "snapshot_signals": result.pop("_snapshot_signals", None),
            "snapshot_bar_features": result.pop("_snapshot_bar_features", None),
        }
        job.result = result
        job.status = JobStatus.DONE
        job.progress = "Done"
        job.progress_pct = 1.0
    except Exception as exc:
        logger.exception("Backtest job %s failed", job_id)
        job.status = JobStatus.ERROR
        job.error  = str(exc)
        job.progress_pct = 1.0


def _expand_tick_import_paths(req: TickImportRequest) -> list[Path]:
    paths = [Path(p).expanduser() for p in req.paths if p and p.strip()]
    if req.folder.strip():
        folder = Path(req.folder).expanduser()
        if not folder.exists() or not folder.is_dir():
            raise ValueError(f"Tick folder does not exist: {folder}")
        pattern = "**/*" if req.recursive else "*"
        paths.extend(
            p for p in folder.glob(pattern)
            if p.is_file() and p.suffix.lower() in {".csv", ".zip"}
        )

    uniq: dict[str, Path] = {}
    for path in paths:
        resolved = path.resolve()
        if resolved.suffix.lower() not in {".csv", ".zip"}:
            continue
        if not resolved.exists() or not resolved.is_file():
            raise ValueError(f"Tick file does not exist: {resolved}")
        uniq[str(resolved)] = resolved
    return sorted(uniq.values())


def _import_ticks_sync(job_id: str, req: TickImportRequest) -> dict:
    from core import tick_cache

    job = get_job(job_id)
    paths = _expand_tick_import_paths(req)
    if not paths:
        raise ValueError("No .csv or .zip tick files were provided.")

    symbol = req.symbol.upper()
    if symbol not in SYMBOLS:
        raise ValueError(f"Unknown symbol: {symbol}")

    total_count = 0
    imported_files: list[str] = []
    for idx, path in enumerate(paths, 1):
        if job is not None:
            job.progress = f"Importing {path.name} ({idx}/{len(paths)})"
        arr = (
            tick_cache.from_zip_file(path)
            if path.suffix.lower() == ".zip"
            else tick_cache.from_csv_file(path)
        )
        if len(arr) == 0:
            continue
        total_count = tick_cache.merge_and_save_array(
            symbol,
            arr,
            int(arr[:, 0].min()),
            int(arr[:, 0].max()),
        )
        imported_files.append(str(path))

    info = tick_cache.info(symbol)
    return {
        "symbol": symbol,
        "files": imported_files,
        "file_count": len(imported_files),
        "total_count": total_count,
        "coverage": info,
    }


async def _tick_import_worker(job_id: str, req: TickImportRequest) -> None:
    job = get_job(job_id)
    if job is None:
        return
    job.status = JobStatus.RUNNING
    job.progress = "Preparing tick import..."
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _import_ticks_sync, job_id, req)
        job.result = result
        job.status = JobStatus.DONE
        job.progress = "Done"
    except Exception as exc:
        logger.exception("Tick import job %s failed", job_id)
        job.status = JobStatus.ERROR
        job.error = str(exc)


def _trade_filename_slug(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in value)[:80]


def _ms_to_utc(ms: int | float | None) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _serialise_kline(k: Any) -> dict:
    return {
        "time_ms": int(k.open_time),
        "close_time": int(k.close_time),
        "open": float(k.open),
        "high": float(k.high),
        "low": float(k.low),
        "close": float(k.close),
        "volume": float(k.volume),
    }


def _to_jsonable(value: Any) -> Any:
    """Convert numpy/dataclass values recursively before FastAPI serialization."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _to_jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if hasattr(value, "item") and callable(value.item) and not isinstance(value, type):
        try:
            return value.item()
        except Exception:
            pass
    if hasattr(value, "tolist") and callable(value.tolist) and not isinstance(value, type):
        try:
            return _to_jsonable(value.tolist())
        except Exception:
            pass
    return value


def _find_bar_index(klines: list, ts_ms: int) -> int | None:
    if not klines or not ts_ms:
        return None
    lo, hi = 0, len(klines) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        k = klines[mid]
        if k.open_time <= ts_ms <= k.close_time:
            return mid
        if ts_ms < k.open_time:
            hi = mid - 1
        else:
            lo = mid + 1
    return max(0, min(lo, len(klines) - 1))


def _bar_ms(klines: list) -> int:
    if len(klines) > 1:
        return max(1, int(klines[1].open_time) - int(klines[0].open_time))
    return 60_000


def _signal_dict(sig: Any | None) -> dict | None:
    if sig is None:
        return None
    return {
        "open_time": int(getattr(sig, "open_time", 0) or 0),
        "price": float(getattr(sig, "price", 0.0) or 0.0),
        "signal_type": str(getattr(sig, "signal_type", "") or ""),
        "label": str(getattr(sig, "label", "") or ""),
        "stop_price": _to_jsonable(getattr(sig, "stop_price", None)),
        "fill_price": _to_jsonable(getattr(sig, "fill_price", None)),
        "fill_time": _to_jsonable(getattr(sig, "fill_time", None)),
        "meta": _to_jsonable(dict(getattr(sig, "meta", {}) or {})),
    }


def _snapshot_context_from_signals(signals: list, trade: dict, klines: list, context_bars: int) -> dict | None:
    """Mirror the GUI snapshot context builder without importing PyQt UI code."""
    if not signals:
        return None

    interval_ms = _bar_ms(klines)
    entry_time = int(trade.get("entry_time", 0) or 0)
    exit_time = int(trade.get("exit_time", 0) or 0)
    direction = trade.get("dir", "long")
    entry_type = "short_entry" if direction == "short" else "long_entry"
    exit_type = "short_exit" if direction == "short" else "long_exit"
    k0_type = "k0_short" if direction == "short" else "k0_long"
    aligned_entry = (entry_time // interval_ms) * interval_ms if entry_time else 0
    aligned_exit = (exit_time // interval_ms) * interval_ms if exit_time else 0

    entry_sig = next(
        (
            s for s in signals
            if int(getattr(s, "open_time", 0) or 0) == aligned_entry
            and getattr(s, "signal_type", "") == entry_type
        ),
        None,
    )
    if entry_sig is None:
        return None

    exit_sig = next(
        (
            s for s in signals
            if aligned_exit
            and int(getattr(s, "open_time", 0) or 0) == aligned_exit
            and getattr(s, "signal_type", "") == exit_type
        ),
        None,
    )
    k0_sig = next(
        (
            s for s in reversed(signals)
            if getattr(s, "signal_type", "") == k0_type
            and int(getattr(s, "open_time", 0) or 0) <= entry_time
        ),
        None,
    )

    entry_idx = _find_bar_index(klines, aligned_entry)
    exit_idx = _find_bar_index(klines, aligned_exit) if aligned_exit else None
    k0_idx = _find_bar_index(klines, int(getattr(k0_sig, "open_time", 0) or 0)) if k0_sig else None
    if entry_idx is None:
        return None

    earliest = min(x for x in (entry_idx, k0_idx) if x is not None)
    latest = exit_idx if exit_idx is not None else entry_idx
    return {
        "entry_idx": entry_idx,
        "exit_idx": exit_idx,
        "k0_idx": k0_idx,
        "win_start": max(0, earliest - context_bars),
        "win_end": min(len(klines) - 1, latest + context_bars),
        "entry_signal": entry_sig,
        "exit_signal": exit_sig,
        "k0_signal": k0_sig,
    }


def _snapshot_context_for_trade(job, trade_idx: int, context_bars: int = 10) -> dict:
    result = job.result or {}
    trades = [t for t in result.get("trade_list", []) if not t.get("skipped")]
    if trade_idx < 0 or trade_idx >= len(trades):
        raise HTTPException(404, "Trade not found")

    klines = job.artifacts.get("snapshot_klines") or []
    if not klines:
        raise HTTPException(404, "Snapshot klines are not available for this job")

    trade = trades[trade_idx]
    signals = job.artifacts.get("snapshot_signals") or []
    ctx = _snapshot_context_from_signals(signals, trade, klines, context_bars)
    if ctx is None:
        entry_idx = _find_bar_index(klines, int(trade.get("entry_time", 0) or 0))
        exit_idx = _find_bar_index(klines, int(trade.get("exit_time", 0) or 0))
        if entry_idx is None:
            raise HTTPException(404, "Entry bar not found")
        latest = exit_idx if exit_idx is not None else entry_idx
        ctx = {
            "entry_idx": entry_idx,
            "exit_idx": exit_idx,
            "k0_idx": None,
            "win_start": max(0, entry_idx - context_bars),
            "win_end": min(len(klines) - 1, latest + context_bars),
            "entry_signal": None,
            "exit_signal": None,
            "k0_signal": None,
        }

    entry_idx = ctx["entry_idx"]
    exit_idx = ctx["exit_idx"]
    k0_idx = ctx["k0_idx"]
    if entry_idx is None:
        raise HTTPException(404, "Entry bar not found")

    start = ctx["win_start"]
    end = ctx["win_end"]
    window = klines[start:end + 1]

    tick_rows: list[dict] = []
    tick_map = job.artifacts.get("snapshot_tick_map")
    if tick_map is not None:
        entry_bar = klines[entry_idx]
        ticks = tick_map.get(entry_bar.open_time)
        if ticks is not None and len(ticks) > 0:
            max_ticks = 1500
            step = max(1, len(ticks) // max_ticks)
            for row in ticks[::step][:max_ticks]:
                tick_rows.append({
                    "time_ms": int(row[0]),
                    "price": float(row[1]),
                    "qty": float(row[2]),
                    "is_sell": bool(row[3] > 0.5),
                })

    stop_price = trade.get("entry_stop", trade.get("stop"))
    tp_price = None
    # Prefer explicit TP from entry signal meta (e.g. POC target set by strategy)
    entry_sig_obj = ctx.get("entry_signal") if ctx else None
    if entry_sig_obj is not None:
        meta_tp = (getattr(entry_sig_obj, "meta", {}) or {}).get("tp")
        if meta_tp is not None:
            try:
                tp_price = float(meta_tp)
            except Exception:
                pass
    if tp_price is None:
        try:
            if stop_price is not None and trade.get("entry") is not None:
                entry_price = float(trade.get("entry"))
                stop = float(stop_price)
                risk = entry_price - stop
                if trade.get("dir") == "short" and risk < 0:
                    tp_price = entry_price + risk
                elif trade.get("dir") == "long" and risk > 0:
                    tp_price = entry_price + risk
        except Exception:
            tp_price = None

    bar_features = job.artifacts.get("snapshot_bar_features") or {}
    window_features = [bar_features.get(k.open_time) for k in window]

    return _to_jsonable({
        "trade": trade,
        "trade_idx": trade_idx,
        "window": [_serialise_kline(k) for k in window],
        "window_features": window_features,
        "entry_index": entry_idx - start,
        "exit_index": (exit_idx - start) if exit_idx is not None else None,
        "k0_index": (k0_idx - start) if k0_idx is not None else None,
        "entry_signal": _signal_dict(ctx.get("entry_signal")),
        "exit_signal": _signal_dict(ctx.get("exit_signal")),
        "k0_signal": _signal_dict(ctx.get("k0_signal")),
        "stop_price": stop_price,
        "tp_price": tp_price,
        "ticks": tick_rows,
    })


def _build_backtest_workbook(result: dict) -> BytesIO:
    """Build a minimal XLSX workbook using only the Python standard library."""
    import html
    import zipfile

    summary_rows = [
        ("strategy", result.get("strategy_name", "")),
        ("mode", result.get("mode", "")),
        ("initial_capital", result.get("initial_capital", 0)),
        ("final_equity", result.get("final_equity", 0)),
        ("total_return_pct", result.get("total_return_pct", 0)),
        ("trades", result.get("trades", 0)),
        ("win_rate_pct", result.get("win_rate", 0)),
        ("profit_factor", result.get("profit_factor", 0)),
        ("total_net_pnl", result.get("total_net_pnl", 0)),
        ("total_fees", result.get("total_fees", 0)),
        ("max_drawdown_pct", result.get("max_drawdown_pct", 0)),
        ("backtest_start_utc", _ms_to_utc(result.get("backtest_start_ms"))),
        ("backtest_end_utc", _ms_to_utc(result.get("backtest_end_ms"))),
    ]

    headers = [
        "#", "dir", "entry_time_utc", "exit_time_utc", "entry", "exit",
        "exit_label", "qty", "gross_pnl", "total_fee", "funding_cost",
        "net_pnl", "equity_after", "r_multiple", "regime",
    ]
    active = [t for t in result.get("trade_list", []) if not t.get("skipped")]
    trade_rows = [headers]
    for idx, t in enumerate(active, 1):
        trade_rows.append([
            idx,
            t.get("dir", ""),
            _ms_to_utc(t.get("entry_time")),
            _ms_to_utc(t.get("exit_time")),
            t.get("entry", 0),
            t.get("exit", 0),
            t.get("exit_label", ""),
            t.get("qty", 0),
            t.get("gross_pnl", 0),
            t.get("total_fee", 0),
            t.get("funding_cost", 0),
            t.get("net_pnl", 0),
            t.get("equity_after", 0),
            t.get("r_multiple", ""),
            t.get("regime", t.get("trend_regime", "")),
        ])

    def col_name(index: int) -> str:
        out = ""
        while index:
            index, rem = divmod(index - 1, 26)
            out = chr(65 + rem) + out
        return out

    def cell_xml(row_idx: int, col_idx: int, value) -> str:
        ref = f"{col_name(col_idx)}{row_idx}"
        if isinstance(value, (int, float)) and value != float("inf"):
            return f'<c r="{ref}"><v>{value}</v></c>'
        text = "∞" if value == float("inf") else str(value)
        return (
            f'<c r="{ref}" t="inlineStr"><is><t>'
            f'{html.escape(text)}'
            f'</t></is></c>'
        )

    def sheet_xml(rows: list[list]) -> str:
        body = []
        for r_idx, row in enumerate(rows, 1):
            cells = "".join(cell_xml(r_idx, c_idx, value) for c_idx, value in enumerate(row, 1))
            body.append(f'<row r="{r_idx}">{cells}</row>')
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData>'
            + "".join(body)
            + '</sheetData></worksheet>'
        )

    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""")
        zf.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""")
        zf.writestr("xl/workbook.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="summary" sheetId="1" r:id="rId1"/>
    <sheet name="trades" sheetId="2" r:id="rId2"/>
  </sheets>
</workbook>""")
        zf.writestr("xl/_rels/workbook.xml.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
</Relationships>""")
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml([[k, v] for k, v in summary_rows]))
        zf.writestr("xl/worksheets/sheet2.xml", sheet_xml(trade_rows))
    out.seek(0)
    return out


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/strategies")
def list_strategies() -> dict:
    return {"strategies": list(STRATEGY_REGISTRY.keys())}


@router.get("/symbols")
def list_symbols() -> dict:
    return {"symbols": SYMBOLS, "intervals": INTERVALS}


@router.get("/available-data")
def available_data() -> dict:
    """Return kline files and tick coverage that actually exist on disk."""
    return {
        "klines": _scan_available_klines(),
        "ticks":  _scan_tick_coverage(),
    }


@router.post("/run")
async def run_backtest(req: BacktestRequest, bg: BackgroundTasks) -> dict:
    job = create_job()
    bg.add_task(_backtest_worker, job.job_id, req)
    return {"job_id": job.job_id, "status": job.status}


@router.post("/tick-cache/import")
async def import_tick_cache(req: TickImportRequest, bg: BackgroundTasks) -> dict:
    job = create_job()
    bg.add_task(_tick_import_worker, job.job_id, req)
    return {"job_id": job.job_id, "status": job.status}


@router.delete("/tick-cache/{symbol}")
def clear_tick_cache(symbol: str) -> dict:
    from core import tick_cache

    sym = symbol.upper()
    if sym not in SYMBOLS:
        raise HTTPException(404, f"Unknown symbol: {sym}")

    deleted: list[str] = []
    legacy = tick_cache.cache_path(sym)
    if legacy.exists():
        legacy.unlink()
        deleted.append(str(legacy))

    manifest = tick_cache.shard_manifest_path(sym)
    if manifest.exists():
        manifest.unlink()
        deleted.append(str(manifest))

    shard_dir = manifest.parent / "shards" / sym
    if shard_dir.exists():
        shutil.rmtree(shard_dir)
        deleted.append(str(shard_dir))

    return {"ok": True, "symbol": sym, "deleted": deleted}


@router.get("/jobs")
def get_jobs() -> list:
    return list_jobs()


@router.get("/jobs/{job_id}")
def get_job_status(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job.to_dict()


@router.get("/jobs/{job_id}/export.xlsx")
def export_job_excel(job_id: str) -> StreamingResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.status != JobStatus.DONE or not isinstance(job.result, dict):
        raise HTTPException(409, "Job result is not ready")

    out = _build_backtest_workbook(job.result)
    strategy = _trade_filename_slug(str(job.result.get("strategy_name", "backtest")))
    start = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"{strategy}_{start}.xlsx"
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/jobs/{job_id}/snapshots/{trade_idx}")
def get_trade_snapshot(job_id: str, trade_idx: int) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.status != JobStatus.DONE or not isinstance(job.result, dict):
        raise HTTPException(409, "Job result is not ready")
    return _snapshot_context_for_trade(job, trade_idx)
