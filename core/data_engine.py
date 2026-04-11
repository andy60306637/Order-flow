"""
Framework-agnostic 資料引擎 (DataEngine)。

統一管理 WebSocket 連線、歷史資料載入、Order Book、CVD、Footprint，
以 callback dict 取代 PyQt6 Signal，可嵌入任何 runtime（Qt / FastAPI / CLI）。

事件類型：
  trade, kline, depth, ob_snapshot, history, agg_history,
  more_history, more_agg_history, exchange_info, status,
  backtest_history, cache_ready, footprint
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

import config
from core.data_types import Kline
from core.order_book import OrderBook
from core.cvd_calculator import CvdCalculator
from core.footprint_builder import FootprintBuilder

logger = logging.getLogger(__name__)


class DataEngine:
    """
    Framework-agnostic 資料引擎。

    Usage::

        engine = DataEngine("BTCUSDT", "1m")
        engine.on("trade", my_trade_handler)
        engine.on("kline", my_kline_handler)
        await engine.start()   # blocks until stop() is called
    """

    def __init__(self, symbol: str, interval: str) -> None:
        self.symbol = symbol
        self.interval = interval

        # ── 內建處理器 ───────────────────────────────────────────────
        self.ob = OrderBook()
        self.cvd = CvdCalculator()
        self.footprint = FootprintBuilder()
        self.klines: List[Kline] = []

        # ── 事件系統 ─────────────────────────────────────────────────
        self._listeners: Dict[str, List[Callable]] = defaultdict(list)

        # ── 狀態 ─────────────────────────────────────────────────────
        self._ws_client: Optional["WsClient"] = None
        self._running = False

    # ── 事件系統 ─────────────────────────────────────────────────────

    def on(self, event: str, callback: Callable) -> None:
        """註冊事件回調。callback 會在觸發時被同步呼叫。"""
        self._listeners[event].append(callback)

    def off(self, event: str, callback: Callable) -> None:
        """移除特定回調。"""
        try:
            self._listeners[event].remove(callback)
        except ValueError:
            pass

    def emit(self, event: str, data: Any = None) -> None:
        """觸發事件，呼叫所有已註冊的 callback。"""
        for cb in self._listeners.get(event, []):
            try:
                cb(data)
            except Exception:
                logger.exception("Error in %s callback", event)

    # ── 生命週期 ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """啟動 WebSocket 連線。阻塞直到 stop() 被呼叫。"""
        from core.ws_client import WsClient

        self._running = True
        self._ws_client = WsClient(
            symbol=self.symbol,
            interval=self.interval,
            engine=self,
        )
        await self._ws_client.run()

    async def stop(self) -> None:
        """優雅關閉。"""
        self._running = False
        if self._ws_client:
            self._ws_client.stop()

    # ── 便利方法（供 WsClient 或外部調用）─────────────────────────

    def request_resync(self) -> None:
        if self._ws_client:
            self._ws_client.request_resync()

    def request_more_history(self, end_time_ms: int) -> None:
        if self._ws_client:
            self._ws_client.request_more_history(end_time_ms)

    def request_backtest_history(
        self, total_candles: int, cache_only: bool = False
    ) -> None:
        if self._ws_client:
            self._ws_client.request_backtest_history(
                total_candles, cache_only=cache_only
            )

    @property
    def is_running(self) -> bool:
        return self._running
