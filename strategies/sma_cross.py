"""
SMA 黃金/死亡交叉策略。

參數（修改此檔案中的 class 屬性即可調整）：
  fast = 10   快線週期
  slow = 30   慢線週期

訊號邏輯：
  黃金交叉（fast 由下穿越 slow）→ long_entry
  死亡交叉（fast 由上穿越 slow）→ short_entry
  （本策略只發出 entry，不主動發出 exit；
   對向 entry 出現時，compute_stats 以對向 entry 視為前倉平倉並非本策略直接發出）
"""
from __future__ import annotations

from typing import List

from core.data_types import Kline
from strategies.base import StrategyBase, StrategySignal
from strategies import register


@register
class SmaCrossStrategy(StrategyBase):
    name = "SMA Cross"
    fast = 10
    slow = 30

    def on_history(self, klines: List[Kline]) -> List[StrategySignal]:
        closes = [k.close for k in klines]
        n = len(closes)
        if n < self.slow + 1:
            return []

        def _sma(prices: List[float], period: int, idx: int) -> float:
            return sum(prices[idx - period + 1: idx + 1]) / period

        signals: List[StrategySignal] = []

        for i in range(self.slow, n):
            fast_now  = _sma(closes, self.fast, i)
            fast_prev = _sma(closes, self.fast, i - 1)
            slow_now  = _sma(closes, self.slow, i)
            slow_prev = _sma(closes, self.slow, i - 1)

            # 黃金交叉：前期 fast < slow，本期 fast > slow
            if fast_prev <= slow_prev and fast_now > slow_now:
                signals.append(StrategySignal(
                    open_time=klines[i].open_time,
                    price=klines[i].close,
                    signal_type="long_entry",
                    label=f"SMA↑",
                ))
            # 死亡交叉：前期 fast > slow，本期 fast < slow
            elif fast_prev >= slow_prev and fast_now < slow_now:
                signals.append(StrategySignal(
                    open_time=klines[i].open_time,
                    price=klines[i].close,
                    signal_type="short_entry",
                    label=f"SMA↓",
                ))

        return signals
