"""
DataEngine ↔ PyQt6 Signal 橋接器。

在獨立 QThread 中運行 DataEngine 的 asyncio event loop，
將所有 DataEngine callback 安全地轉發為 PyQt6 signal（線程安全）。

用法::

    adapter = QtDataEngineAdapter("BTCUSDT", "1m")
    adapter.trade_signal.connect(my_slot)
    adapter.start()   # 啟動背景執行緒
    adapter.stop()    # 優雅關閉
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from PyQt6.QtCore import QThread, pyqtSignal

from core.data_engine import DataEngine

logger = logging.getLogger(__name__)


class QtDataEngineAdapter(QThread):
    """
    QThread 橋接器：在背景執行 DataEngine，
    將事件透過 PyQt6 signal 線程安全地推送到主執行緒。

    Signal 名稱與原 WsWorkerThread 完全一致，
    MainWindow 只需替換數據源即可無縫遷移。
    """

    # ── Qt signals（與 WsWorkerThread 完全相同）──────────────────────
    trade_signal            = pyqtSignal(dict)
    kline_signal            = pyqtSignal(dict)
    depth_signal            = pyqtSignal(dict)
    ob_snapshot_signal      = pyqtSignal(dict)
    history_signal          = pyqtSignal(list)
    agg_history_signal      = pyqtSignal(list)
    more_history_signal     = pyqtSignal(list)
    more_agg_history_signal = pyqtSignal(list)
    exchange_info_signal    = pyqtSignal(dict)
    status_signal           = pyqtSignal(str)
    backtest_history_signal = pyqtSignal(list)
    cache_ready_signal      = pyqtSignal(int)

    # 事件名 → Qt signal 映射
    _SIGNAL_MAP: dict[str, str] = {
        "trade":            "trade_signal",
        "kline":            "kline_signal",
        "depth":            "depth_signal",
        "ob_snapshot":      "ob_snapshot_signal",
        "history":          "history_signal",
        "agg_history":      "agg_history_signal",
        "more_history":     "more_history_signal",
        "more_agg_history": "more_agg_history_signal",
        "exchange_info":    "exchange_info_signal",
        "status":           "status_signal",
        "backtest_history": "backtest_history_signal",
        "cache_ready":      "cache_ready_signal",
    }

    def __init__(
        self,
        symbol: str,
        interval: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._engine = DataEngine(symbol, interval)
        # 逐一註冊每個事件，將 DataEngine callback 橋接到 Qt signal
        for event_name in self._SIGNAL_MAP:
            self._engine.on(event_name, lambda data, e=event_name: self._bridge_emit(e, data))
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def engine(self) -> DataEngine:
        """取得底層 DataEngine 實例（供 MainWindow 直接存取 ob / cvd / footprint 等）。"""
        return self._engine

    def _bridge_emit(self, event: str, data: Any) -> None:
        sig_name = self._SIGNAL_MAP.get(event)
        if sig_name:
            getattr(self, sig_name).emit(data)

    # ── 委派給 DataEngine ─────────────────────────────────────────────

    def request_resync(self) -> None:
        self._engine.request_resync()

    def request_more_history(self, end_time_ms: int) -> None:
        self._engine.request_more_history(end_time_ms)

    def request_backtest_history(
        self, total_candles: int, cache_only: bool = False
    ) -> None:
        self._engine.request_backtest_history(total_candles, cache_only=cache_only)

    def stop(self) -> None:
        """安全停止：停止 DataEngine 並等待 QThread 結束。"""
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._engine.stop(), self._loop)

    # ── QThread.run() ─────────────────────────────────────────────────

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._engine.start())
        except (RuntimeError, asyncio.CancelledError):
            pass
        finally:
            try:
                pending = asyncio.all_tasks(self._loop)
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            self._loop.close()
