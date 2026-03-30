"""
CVD (Cumulative Volume Delta) 計算器。

- 買方主動（is_buyer_maker=False）→ +qty
- 賣方主動（is_buyer_maker=True）  → -qty
- 每根 K 棒在 get_series() 時以實時 running_cvd 更新最後一個點
"""
from __future__ import annotations
from collections import deque
from typing import List, Tuple

import config
from core.data_types import Trade


class CvdCalculator:
    def __init__(self) -> None:
        self._running_cvd: float = 0.0
        # 每個元素 = (open_time, cvd_at_candle_start)
        # 最後一個元素的值會在 get_series() 中以 _running_cvd 覆蓋
        self._cvd_series: deque[Tuple[int, float]] = deque(
            maxlen=config.CVD_HISTORY
        )
        self._current_candle_open_time: int = 0

    def reset(self) -> None:
        self._running_cvd = 0.0
        self._cvd_series.clear()
        self._current_candle_open_time = 0

    def seed_history(self, klines) -> None:
        """
        以歷史 K 線資料計算真正的 CVD 曲線（利用 taker_buy_volume）。
        傳入除最後一根以外的所有歷史 Kline 物件（需有 .open_time, .volume, .taker_buy_volume）。
        delta = 2 * taker_buy_volume - volume
        CVD  = cumulative sum of delta
        """
        self._cvd_series.clear()
        cvd = 0.0
        for k in klines:
            delta = 2.0 * k.taker_buy_volume - k.volume
            cvd += delta
            self._cvd_series.append((k.open_time, cvd))
        self._running_cvd = cvd

    def update(self, trade: Trade) -> float:
        """處理一筆成交，回傳更新後的 running CVD。"""
        if trade.is_buyer_maker:
            self._running_cvd -= trade.qty  # 賣方主動
        else:
            self._running_cvd += trade.qty  # 買方主動
        return self._running_cvd

    def on_new_candle(self, open_time: int) -> None:
        """K 棒開始時記錄錨點。"""
        if open_time != self._current_candle_open_time:
            self._cvd_series.append((open_time, self._running_cvd))
            self._current_candle_open_time = open_time

    def get_running_cvd(self) -> float:
        return self._running_cvd

    def get_series(self) -> List[Tuple[int, float]]:
        """
        回傳 [(open_time, cvd), ...] 序列（每根 K 棒一個點）。
        最後一個點永遠使用實時 _running_cvd，反映當前 K 棒的累積值。
        """
        result = list(self._cvd_series)
        if not result:
            # 尚未有任何 K 棒資料
            if self._current_candle_open_time > 0:
                return [(self._current_candle_open_time, self._running_cvd)]
            return []
        # 最後一個點更新為實時值
        result[-1] = (result[-1][0], self._running_cvd)
        return result

