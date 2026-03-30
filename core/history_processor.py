"""
歷史 aggTrade 資料處理執行緒。

在獨立 QThread 中執行大量歷史成交資料的 Footprint 建構，
避免阻塞主執行緒（UI）。

最佳化：使用 bisect 二分搜尋取代線性掃描，
將 K 棒歸屬查找由 O(N·M) 降為 O(N·log M)。
"""
from __future__ import annotations

import bisect
import logging
from typing import List

from PyQt6.QtCore import QThread, pyqtSignal

from core.data_types import Trade, Kline
from core.footprint_builder import FootprintBuilder

logger = logging.getLogger(__name__)


class HistoryProcessorThread(QThread):
    """
    背景執行緒：處理歷史 aggTrades，建構 Footprint K 棒後發送結果。

    輸入（constructor）：
      payload        — WsWorkerThread.agg_history_signal 的 payload 字典
      tick_size      — 價格分桶大小
      history_klines — 最近 N 根歷史 Kline 物件（供 OHLCV 更新）

    輸出 signal：
      result_signal(list)  — 完成後發送 List[FootprintCandle] 到主執行緒
      status_signal(str)   — 進度文字
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

    # ──────────────────────────────────────────────────────────────────────────
    def run(self) -> None:
        trades = self._payload.get("trades", [])
        k_ranges = self._payload.get("klines", [])   # [(open_t, close_t), ...]

        if not trades or not k_ranges:
            self.result_signal.emit([])
            return

        fp = FootprintBuilder()
        fp.reset(self._tick_size)

        # 建立 bisect 用的有序陣列
        # open_times 已由 WsWorkerThread 按時間順序組成，無需額外排序
        open_times  = [ot for ot, _  in k_ranges]
        close_times = [ct for _,  ct in k_ranges]

        n_trades = len(trades)
        logger.info(
            "HistoryProcessor: processing %d trades across %d klines",
            n_trades, len(k_ranges),
        )
        self.status_signal.emit(f"Footprint 回填 {n_trades} 筆成交中…")

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
        for k in self._history_klines:
            fp.update_kline(k)

        candles = fp.get_candles()
        logger.info("HistoryProcessor: built %d footprint candles", len(candles))
        self.result_signal.emit(candles)
