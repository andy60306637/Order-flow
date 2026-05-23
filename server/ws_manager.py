"""WebSocket connection manager — broadcasts market data to all subscribed clients."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WsManager:
    """
    Manages active WebSocket connections grouped by channel key
    (e.g. "BTCUSDT:1m"). Allows broadcasting arbitrary JSON payloads.
    """

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, ws: WebSocket, channel: str) -> None:
        await ws.accept()
        self._connections[channel].add(ws)
        logger.info("WS connected  channel=%s total=%d", channel, len(self._connections[channel]))

    def disconnect(self, ws: WebSocket, channel: str) -> None:
        self._connections[channel].discard(ws)
        logger.info("WS disconnect channel=%s total=%d", channel, len(self._connections[channel]))

    async def broadcast(self, channel: str, payload: dict) -> None:
        dead: list[WebSocket] = []
        text = json.dumps(payload, ensure_ascii=False)
        for ws in list(self._connections.get(channel, [])):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, channel)

    def subscriber_count(self, channel: str) -> int:
        return len(self._connections.get(channel, set()))


ws_manager = WsManager()
