"""
Footprint K 棒建構器。

依照 kline 的 open_time 切割 K 棒，
每根棒以 tick_size 分桶（price bucket），
記錄每個分桶的 bid_vol / ask_vol。
"""
from __future__ import annotations
import math
import logging
from collections import OrderedDict
from typing import List, Optional

import config
from core.data_types import Trade, Kline, FootprintCandle, FootprintLevel

logger = logging.getLogger(__name__)


class FootprintBuilder:
    def __init__(self) -> None:
        self._tick_size: float = 1.0
        self._candles: OrderedDict[int, FootprintCandle] = OrderedDict()
        self._current_open_time: int = 0

    def reset(self, tick_size: float = 1.0) -> None:
        self._tick_size = tick_size
        self._candles.clear()
        self._current_open_time = 0

    # ──────────────────────────────────────────────────────────────────────────
    def _bucket(self, price: float) -> float:
        """將價格向下對齊到最近的 tick_size 邊界。"""
        return math.floor(price / self._tick_size) * self._tick_size

    def _get_or_create_candle(self, open_time: int) -> FootprintCandle:
        if open_time not in self._candles:
            self._candles[open_time] = FootprintCandle(open_time=open_time)
            # 限制最大保留根數
            while len(self._candles) > config.FOOTPRINT_MAX_CANDLES:
                self._candles.popitem(last=False)
        return self._candles[open_time]

    # ──────────────────────────────────────────────────────────────────────────
    def update_trade(self, trade: Trade, open_time: int = 0) -> None:
        """
        以一筆 aggTrade 更新對應 K 棒的 Footprint。
        open_time: 目前 K 棒的 open_time（由 WS kline 事件提供），
                   呼叫時也可直接用 positional 參數。
        """
        kline_open_time = open_time
        if kline_open_time == 0:
            return
        candle = self._get_or_create_candle(kline_open_time)
        if candle.closed:
            return
        bucket = self._bucket(trade.price)
        if bucket not in candle.levels:
            candle.levels[bucket] = FootprintLevel(price=bucket)
        lv = candle.levels[bucket]
        if trade.is_buyer_maker:
            lv.ask_vol += trade.qty  # 賣方主動
        else:
            lv.bid_vol += trade.qty  # 買方主動

    def update_kline(self, kline: Kline) -> None:
        """從 kline 事件更新 K 棒的 OHLCV 資訊。"""
        candle = self._get_or_create_candle(kline.open_time)
        candle.open   = kline.open
        candle.high   = kline.high
        candle.low    = kline.low
        candle.close  = kline.close
        candle.volume = kline.volume
        if kline.is_closed:
            candle.closed = True

    # ──────────────────────────────────────────────────────────────────────────
    def get_candles(self) -> List[FootprintCandle]:
        """回傳依 open_time 排序的所有 Footprint K 棒。"""
        return list(self._candles.values())

    def get_current_candle(self) -> Optional[FootprintCandle]:
        if self._current_open_time in self._candles:
            return self._candles[self._current_open_time]
        if self._candles:
            return list(self._candles.values())[-1]
        return None

    def set_current_open_time(self, t: int) -> None:
        self._current_open_time = t
