"""Live market data WebSocket endpoint."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from server.ws_manager import ws_manager
from config.base import SYMBOLS, INTERVALS

logger = logging.getLogger(__name__)
router = APIRouter(tags=["market"])

# symbol:interval → DataEngine instance
_engines: dict[str, Any] = {}
_engine_tasks: dict[str, asyncio.Task] = {}
_engine_locks: dict[str, asyncio.Lock] = {}

# State cache for late-joining clients
_engine_klines: dict[str, list] = {}   # key → raw Binance REST kline rows
_engine_ob: dict[str, dict] = {}       # key → {"bids": [[p,q],...], "asks": [[p,q],...]}
_engine_exchange_info: dict[str, dict] = {}
_engine_agg_history: dict[str, list] = {}


# ── Serialisation ─────────────────────────────────────────────────────────────

def _to_serialisable(obj: Any) -> Any:
    """Recursively convert DataEngine payloads to JSON-serialisable types."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [_to_serialisable(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _to_serialisable(v) for k, v in obj.items()}
    try:
        import numpy as np
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
    except ImportError:
        pass
    return obj


# ── Replay state to a newly connected client ──────────────────────────────────

async def _replay_state(ws: WebSocket, key: str) -> None:
    """Send cached kline history and OB state to a late-joining client."""
    try:
        if key in _engine_klines:
            await ws.send_text(json.dumps(
                {"type": "history", "data": _engine_klines[key]},
                ensure_ascii=False,
            ))
        if key in _engine_ob:
            await ws.send_text(json.dumps(
                {"type": "ob_snapshot", "data": _engine_ob[key]},
                ensure_ascii=False,
            ))
        if key in _engine_exchange_info:
            await ws.send_text(json.dumps(
                {"type": "exchange_info", "data": _engine_exchange_info[key]},
                ensure_ascii=False,
            ))
        if key in _engine_agg_history:
            await ws.send_text(json.dumps(
                {"type": "agg_history", "data": _engine_agg_history[key]},
                ensure_ascii=False,
            ))
    except Exception as exc:
        logger.warning("_replay_state error for %s: %s", key, exc)


# ── Engine management ─────────────────────────────────────────────────────────

async def _get_or_create_engine(symbol: str, interval: str) -> Any:
    from core.data_engine import DataEngine

    key = f"{symbol}:{interval}"
    if key not in _engine_locks:
        _engine_locks[key] = asyncio.Lock()

    async with _engine_locks[key]:
        task = _engine_tasks.get(key)
        if key in _engines and task and not task.done():
            return _engines[key]
        if task and task.done():
            _cleanup_finished_engine(key, task)

        engine = DataEngine(symbol, interval)
        loop = asyncio.get_event_loop()

        # ── Order book handlers (merge server-side, broadcast clean snapshot) ──

        def _ob_snapshot_handler(payload: dict) -> None:
            engine.ob.init_snapshot(payload)
            _broadcast_ob(key, engine, loop)

        def _depth_handler(payload: dict) -> None:
            needs_resync = engine.ob.apply_diff(payload)
            if needs_resync:
                engine.request_resync()
                return
            if engine.ob.is_initialized:
                _broadcast_ob(key, engine, loop)

        # ── Generic broadcast handler for remaining events ─────────────────────

        def _make_broadcast(event_type: str):
            def _handler(payload: Any) -> None:
                safe = _to_serialisable(payload)
                # Cache history for late-joiners
                if event_type == "history":
                    _engine_klines[key] = safe
                elif event_type == "exchange_info":
                    _engine_exchange_info[key] = safe
                elif event_type == "agg_history":
                    _engine_agg_history[key] = safe
                asyncio.run_coroutine_threadsafe(
                    ws_manager.broadcast(key, {"type": event_type, "data": safe}),
                    loop,
                )
            return _handler

        engine.on("ob_snapshot", _ob_snapshot_handler)
        engine.on("depth", _depth_handler)
        for evt in ("trade", "kline", "history", "agg_history",
                    "more_history", "more_agg_history",
                    "status", "exchange_info"):
            engine.on(evt, _make_broadcast(evt))

        task = asyncio.create_task(engine.start(), name=f"market-engine:{key}")
        task.add_done_callback(lambda done_task, engine_key=key: _cleanup_finished_engine(engine_key, done_task))
        _engines[key] = engine
        _engine_tasks[key] = task
        logger.info("DataEngine started for %s", key)

    return _engines[key]


def _cleanup_finished_engine(key: str, task: asyncio.Task) -> None:
    if not task.done():
        return
    try:
        task.result()
    except asyncio.CancelledError:
        logger.info("DataEngine cancelled for %s", key)
    except Exception:
        logger.exception("DataEngine crashed for %s", key)

    if _engine_tasks.get(key) is task:
        _engine_tasks.pop(key, None)
        _engines.pop(key, None)
        _engine_klines.pop(key, None)
        _engine_ob.pop(key, None)
        _engine_exchange_info.pop(key, None)
        _engine_agg_history.pop(key, None)


def _broadcast_ob(key: str, engine: Any, loop: asyncio.AbstractEventLoop) -> None:
    """Broadcast the current merged order book as ob_snapshot to all clients."""
    bids = [[p, q] for p, q in engine.ob.get_bids(50)]
    asks = [[p, q] for p, q in engine.ob.get_asks(50)]
    ob_safe = {"bids": bids, "asks": asks}
    _engine_ob[key] = ob_safe
    asyncio.run_coroutine_threadsafe(
        ws_manager.broadcast(key, {"type": "ob_snapshot", "data": ob_safe}),
        loop,
    )


# ── REST endpoints ────────────────────────────────────────────────────────────

@router.get("/api/market/symbols")
def market_symbols() -> dict:
    return {"symbols": SYMBOLS, "intervals": INTERVALS}


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@router.websocket("/ws/market/{symbol}/{interval}")
async def market_ws(ws: WebSocket, symbol: str, interval: str) -> None:
    symbol   = symbol.upper()
    interval = interval.lower()

    if symbol not in SYMBOLS:
        await ws.close(code=4000, reason=f"Unknown symbol: {symbol}")
        return
    if interval not in INTERVALS:
        await ws.close(code=4001, reason=f"Unknown interval: {interval}")
        return

    channel = f"{symbol}:{interval}"
    await ws_manager.connect(ws, channel)

    is_new_engine = channel not in _engines
    await _get_or_create_engine(symbol, interval)

    # Late-joining client: replay cached state so they get a populated chart/OB
    if not is_new_engine:
        await _replay_state(ws, channel)

    try:
        while True:
            text = await ws.receive_text()
            if text == "ping":
                await ws.send_text("pong")
                continue
            # Handle JSON messages from the client
            try:
                msg = json.loads(text)
                if msg.get("type") == "more_history":
                    end_time_ms = int(msg.get("end_time_ms", 0))
                    engine = _engines.get(channel)
                    if engine and end_time_ms > 0:
                        loop = asyncio.get_event_loop()
                        loop.run_in_executor(
                            None,
                            lambda: engine.request_more_history(end_time_ms),
                        )
            except (json.JSONDecodeError, ValueError):
                pass
    except WebSocketDisconnect:
        ws_manager.disconnect(ws, channel)
