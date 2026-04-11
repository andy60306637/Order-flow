"""
歷史 aggTrade 資料處理模組。

提供兩個介面：
  process_footprint_history()  — 純函式，零框架依賴。可在任何 runtime 中使用。
  HistoryProcessorThread       — QThread 向後相容包裝器（Desktop UI 用）。

最佳化：使用 bisect 二分搜尋取代線性掃描，
將 K 棒歸屬查找由 O(N·M) 降為 O(N·log M)。
"""
from __future__ import annotations

import bisect
import logging
from typing import List

from core.data_types import Trade, Kline, FootprintCandle
from core.footprint_builder import FootprintBuilder

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# 純函式：零框架依賴
# ═════════════════════════════════════════════════════════════════════════════

def process_footprint_history(
    payload: dict,
    tick_size: float,
    history_klines: List[Kline],
) -> List[FootprintCandle]:
    """
    處理歷史 aggTrades，建構 Footprint K 棒。

    純函式版本，可直接呼叫或透過 asyncio.to_thread() 在背景執行。
    取代原 HistoryProcessorThread.run() 的核心邏輯。

    Args:
        payload: {"trades": [aggTrade dicts], "klines": [(open_t, close_t), ...]}
        tick_size: 價格分桶大小
        history_klines: 歷史 Kline 物件列表（供 OHLCV 更新）

    Returns:
        建構完成的 FootprintCandle 列表
    """
    trades = payload.get("trades", [])
    k_ranges = payload.get("klines", [])   # [(open_t, close_t), ...]

    if not trades or not k_ranges:
        return []

    fp = FootprintBuilder()
    fp.reset(tick_size)

    open_times  = [ot for ot, _  in k_ranges]
    close_times = [ct for _,  ct in k_ranges]

    n_trades = len(trades)
    logger.info(
        "process_footprint_history: %d trades across %d klines",
        n_trades, len(k_ranges),
    )

    for raw in trades:
        t_ms = int(raw["T"])

        # 二分搜尋：找最後一個 open_time <= t_ms
        idx = bisect.bisect_right(open_times, t_ms) - 1
        if idx < 0 or t_ms > close_times[idx]:
            continue

        bucket_open = open_times[idx]
        trade = Trade(
            symbol="",
            price=float(raw["p"]),
            qty=float(raw["q"]),
            is_buyer_maker=bool(raw["m"]),
            trade_time=t_ms,
        )
        fp.update_trade(trade, open_time=bucket_open)

    # 用歷史 Kline 補齊 OHLCV
    for k in history_klines:
        fp.update_kline(k)

    candles = fp.get_candles()
    logger.info("process_footprint_history: built %d footprint candles", len(candles))
    return candles


# ═════════════════════════════════════════════════════════════════════════════
# QThread 向後相容包裝器
# ═════════════════════════════════════════════════════════════════════════════

try:
    from PyQt6.QtCore import QThread, pyqtSignal

    class HistoryProcessorThread(QThread):
        """
        QThread 包裝器，委派核心邏輯給 process_footprint_history()。
        API 與舊版完全相容，MainWindow 無需修改。
        """

        result_signal = pyqtSignal(list)   # List[FootprintCandle]
        status_signal = pyqtSignal(str)

        def __init__(
            self,
            payload: dict,
            tick_size: float,
            history_klines: List[Kline],
            parent=None,
        ) -> None:
            super().__init__(parent)
            self._payload        = payload
            self._tick_size      = tick_size
            self._history_klines = history_klines

        def run(self) -> None:
            self.status_signal.emit(
                f"Footprint 回填 {len(self._payload.get('trades', []))} 筆成交中…"
            )
            candles = process_footprint_history(
                self._payload,
                self._tick_size,
                self._history_klines,
            )
            self.result_signal.emit(candles)

except ImportError:
    # 無 PyQt6 環境（Server / Worker）：不提供 HistoryProcessorThread
    pass
