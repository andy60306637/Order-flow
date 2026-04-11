"""
Wick Reversal v4

Long-only, tick-first research variant:
  - k0 is color-agnostic, shape-based, requires lower-wick absorption,
    and filters out micro-range noise
  - entry triggers directly inside the zoom window after k0:
    first tick >= k0.high with cumulative delta_eff > threshold (tick mode),
    or bar closes with k.high >= k0.high and delta_eff > threshold (bar mode)
  - stop anchors to k0.low - sl_offset (same as v3, avoids body-low tightness)
  - trailing/TP/TS/TD keep the existing long-side semantics
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np

from core.data_types import Kline
from strategies.base import StrategyBase, StrategySignal, TickBarMap
from strategies import register


def _kline_delta(k: Kline) -> float:
    return 2.0 * k.taker_buy_volume - k.volume


def _kline_delta_eff(k: Kline) -> float:
    if k.volume == 0:
        return 0.0
    return _kline_delta(k) / k.volume


@register
class WickReversalV4Strategy(StrategyBase):
    name = "Wick Reversal 1m v4"

    zoom_bars: int = 3                              # k0 後允許進場的最大觀察根數
    sl_offset: float = 10.0                         # 固定停損位移（k0.low - sl_offset）
    rr_ratio: float = 1.0                           # 盈虧比
    long_delta_eff_threshold: float = 0.6           # 進場 delta_eff 門檻
    long_vol_sma_period: int = 20                   # 成交量 SMA 窗期；0=不過濾
    long_vol_sma_mult: float = 1.2                  # 成交量門標倍率
    td_consec_bars: int = 2                         # 連續反向 delta 才觸發 TD
    k0_range_sma_period: int = 20                   # k0 range 濾網 SMA 窗期；0=不過濾
    k0_min_range_sma_mult: float = 1.0              # k0 range 最低倍率門檻
    lower_wick_absorption_delta_eff_max: float = 0.0
    lower_wick_absorption_min_vol_ratio: float = 0.15
    lower_wick_absorption_bar_delta_max: float = 0.0

    def on_history(
        self,
        klines: List[Kline],
        tick_map: Optional[TickBarMap] = None,
    ) -> List[StrategySignal]:
        signals: List[StrategySignal] = []
        n = len(klines)
        if n < 2:
            return signals

        use_ticks = tick_map is not None and len(tick_map) > 0

        k0: Optional[Kline] = None
        k0_idx = -1

        in_position = False
        entry_price = 0.0
        stop_price = 0.0
        target_price = 0.0
        trailing = False
        td_consec = 0

        self._trailing = False
        self._td_consec = 0
        self._stop_price = 0.0

        for i, k in enumerate(klines):
            # ── Step 1：持倉管理 ────────────────────────────────────────────
            if in_position:
                exited = False

                if use_ticks:
                    self._trailing = trailing
                    self._td_consec = td_consec
                    self._stop_price = stop_price
                    exited = self._tick_exit_long(k, tick_map, signals, target_price)
                    if not exited:
                        trailing = self._trailing
                        td_consec = self._td_consec
                        stop_price = self._stop_price
                else:
                    exited, trailing, td_consec, stop_price = self._bar_exit_long(
                        k, signals, stop_price, target_price, trailing, td_consec,
                    )

                if exited:
                    in_position = False
                    trailing = False
                    td_consec = 0
                else:
                    continue

            # ── Step 2：k0 zoom 進場判定 ────────────────────────────────────
            if k0 is not None and i > k0_idx:
                bars_after = i - k0_idx
                if bars_after > self.zoom_bars:
                    k0 = None   # zoom 過期
                elif k.low < k0.low:
                    k0 = None   # 守護線被破，k0 失效
                else:
                    entered = False
                    if use_ticks and k.open_time in tick_map:
                        entered, entry_price, stop_price, target_price = self._tick_entry(
                            k, i, klines, tick_map, signals, k0,
                        )
                    else:
                        entered, entry_price, stop_price, target_price = self._bar_entry(
                            k, i, klines, signals, k0,
                        )

                    if entered:
                        in_position = True
                        trailing = False
                        td_consec = 0
                        k0 = None
                        continue

            # ── Step 3：k0 偵測 ─────────────────────────────────────────────
            if not in_position:
                cur_ticks = tick_map.get(k.open_time) if tick_map is not None else None
                if self._is_k0_long(klines, i, k, cur_ticks):
                    k0 = k
                    k0_idx = i
                    signals.append(StrategySignal(
                        open_time=k.open_time,
                        price=k.low,
                        signal_type="k0_long",
                        label="k0",
                    ))

        return signals

    # ── 進場：Bar 模式 ─────────────────────────────────────────────────────────
    def _bar_entry(
        self,
        k: Kline,
        i: int,
        klines: List[Kline],
        signals: List[StrategySignal],
        k0: Kline,
    ) -> tuple[bool, float, float, float]:
        """
        Bar 模式進場：整根 K 棒 delta_eff > threshold 且突破 k0.high。
        進場價固定為 k0.high（含輕度 look-ahead：使用整棒 delta）。
        """
        if k.high < k0.high:
            return False, 0.0, 0.0, 0.0
        if _kline_delta_eff(k) <= self.long_delta_eff_threshold:
            return False, 0.0, 0.0, 0.0
        if not self._vol_sma_ok(klines, i, k.volume):
            return False, 0.0, 0.0, 0.0

        entry_p = k0.high
        stop_p = k0.low - self.sl_offset
        risk = entry_p - stop_p
        if risk <= 0:
            return False, 0.0, 0.0, 0.0
        target_p = entry_p + risk * self.rr_ratio
        signals.append(StrategySignal(
            open_time=k.open_time,
            price=entry_p,
            signal_type="long_entry",
            label="L4",
            stop_price=stop_p,
        ))
        return True, entry_p, stop_p, target_p

    # ── 進場：Tick 模式 ────────────────────────────────────────────────────────
    def _tick_entry(
        self,
        k: Kline,
        i: int,
        klines: List[Kline],
        tick_map: TickBarMap,
        signals: List[StrategySignal],
        k0: Kline,
    ) -> tuple[bool, float, float, float]:
        """
        Tick 模式進場：遍歷 tick 累計 delta，第一筆價格 >= k0.high 且
        累計 delta_eff > threshold 時入場。
        Vol SMA 使用前一根已收棒 volume，避免 look-ahead。
        """
        ticks = tick_map.get(k.open_time)
        if ticks is None or len(ticks) == 0:
            return self._bar_entry(k, i, klines, signals, k0)

        # Vol SMA 前置檢查：用前一根已收棒避免 look-ahead
        prev_vol = klines[i - 1].volume if i > 0 else 0.0
        if not self._vol_sma_ok(klines, i, prev_vol):
            return False, 0.0, 0.0, 0.0

        cum_buy_vol = 0.0
        cum_vol = 0.0

        for t in ticks:
            price = float(t[1])
            qty = float(t[2])
            is_bm = t[3] > 0.5

            cum_vol += qty
            if not is_bm:
                cum_buy_vol += qty

            # 守護線被破 → 立即失效
            if price < k0.low:
                return False, 0.0, 0.0, 0.0

            if cum_vol == 0:
                continue

            cum_delta_eff = (2.0 * cum_buy_vol - cum_vol) / cum_vol

            if price >= k0.high and cum_delta_eff > self.long_delta_eff_threshold:
                fill_p = price
                stop_p = k0.low - self.sl_offset
                risk = fill_p - stop_p
                if risk <= 0:
                    continue
                target_p = fill_p + risk * self.rr_ratio
                signals.append(StrategySignal(
                    open_time=k.open_time,
                    price=k0.high,       # 圖表標記基準價
                    signal_type="long_entry",
                    label="L4",
                    stop_price=stop_p,
                    fill_price=fill_p,   # 實際 tick 成交價
                ))
                return True, fill_p, stop_p, target_p

        return False, 0.0, 0.0, 0.0

    # ── k0 判定 ────────────────────────────────────────────────────────────────
    def _is_k0_long(
        self,
        klines: List[Kline],
        i: int,
        k: Kline,
        ticks: Optional[np.ndarray] = None,
    ) -> bool:
        rng = k.high - k.low
        if rng <= 0:
            return False
        if not self._k0_range_ok(klines, i, rng):
            return False
        mid = (k.high + k.low) / 2.0
        body_low = min(k.open, k.close)
        body = abs(k.close - k.open)
        lower_wick = body_low - k.low
        if not (body_low >= mid and lower_wick > 0 and lower_wick > body):
            return False
        return self._has_lower_wick_absorption(k, ticks, body_low)

    def _k0_range_ok(self, klines: List[Kline], i: int, rng: float) -> bool:
        period = self.k0_range_sma_period
        if period <= 0 or i < period:
            return True
        s = i - period
        sma = sum(klines[j].high - klines[j].low for j in range(s, i)) / period
        if sma <= 0:
            return True
        return rng >= sma * self.k0_min_range_sma_mult

    def _has_lower_wick_absorption(
        self,
        k: Kline,
        ticks: Optional[np.ndarray],
        body_low: float,
    ) -> bool:
        if ticks is not None and len(ticks) > 0:
            wick_ticks = ticks[ticks[:, 1] <= body_low]
            if len(wick_ticks) == 0:
                return False
            wick_vol = float(np.sum(wick_ticks[:, 2]))
            total_vol = float(np.sum(ticks[:, 2]))
            if wick_vol <= 0 or total_vol <= 0:
                return False
            wick_buy_vol = float(np.sum(wick_ticks[wick_ticks[:, 3] < 0.5, 2]))
            wick_delta = 2.0 * wick_buy_vol - wick_vol
            wick_delta_eff = wick_delta / wick_vol
            return (
                wick_vol / total_vol >= self.lower_wick_absorption_min_vol_ratio
                and wick_delta_eff <= self.lower_wick_absorption_delta_eff_max
            )
        return _kline_delta(k) <= self.lower_wick_absorption_bar_delta_max

    # ── Vol SMA 工具 ───────────────────────────────────────────────────────────
    def _vol_sma_ok(self, klines: List[Kline], cur_idx: int, cur_vol: float) -> bool:
        """
        SMA 窗口為 klines[cur_idx-period .. cur_idx-1]（不含 cur_idx）。
        bar 模式傳 k.volume；tick 模式傳 klines[i-1].volume，避免 look-ahead。
        """
        period = self.long_vol_sma_period
        if period <= 0 or cur_idx < period:
            return True
        s = cur_idx - period
        sma = sum(klines[j].volume for j in range(s, cur_idx)) / period
        return cur_vol > sma * self.long_vol_sma_mult

    # ── 出場：Tick 模式 ────────────────────────────────────────────────────────
    def _tick_exit_long(
        self,
        k: Kline,
        tick_map: TickBarMap,
        signals: List[StrategySignal],
        target_price: float,
    ) -> bool:
        ticks = tick_map.get(k.open_time)
        if ticks is None or len(ticks) == 0:
            return self._bar_exit_simple_long(k, signals, target_price)

        cum_buy_vol = 0.0
        cum_vol = 0.0
        cum_delta = 0.0

        for t in ticks:
            price = t[1]
            qty = t[2]
            is_bm = t[3] > 0.5

            cum_vol += qty
            if not is_bm:
                cum_buy_vol += qty
            cum_delta = 2.0 * cum_buy_vol - cum_vol

            if price <= self._stop_price:
                label = "TS" if self._trailing else "SL"
                signals.append(StrategySignal(
                    open_time=k.open_time,
                    price=self._stop_price,
                    signal_type="long_exit",
                    label=label,
                    fill_price=price,
                ))
                return True

            if self._trailing:
                continue

            if price >= target_price:
                if cum_delta > 0:
                    self._trailing = True
                    self._stop_price = target_price
                    self._td_consec = 0
                else:
                    signals.append(StrategySignal(
                        open_time=k.open_time,
                        price=target_price,
                        signal_type="long_exit",
                        label="TP",
                    ))
                    return True

        if self._trailing:
            if cum_delta <= 0:
                self._td_consec += 1
                if self._td_consec >= self.td_consec_bars:
                    signals.append(StrategySignal(
                        open_time=k.open_time,
                        price=k.close,
                        signal_type="long_exit",
                        label="TD",
                    ))
                    return True
            else:
                self._td_consec = 0

        return False

    # ── 出場：Bar 模式 ─────────────────────────────────────────────────────────
    def _bar_exit_long(
        self,
        k: Kline,
        signals: List[StrategySignal],
        stop_price: float,
        target_price: float,
        trailing: bool,
        td_consec: int,
    ) -> tuple[bool, bool, int, float]:
        if k.low <= stop_price:
            label = "TS" if trailing else "SL"
            signals.append(StrategySignal(
                open_time=k.open_time,
                price=stop_price,
                signal_type="long_exit",
                label=label,
            ))
            return True, trailing, td_consec, stop_price

        if trailing:
            if _kline_delta(k) <= 0:
                td_consec += 1
                if td_consec >= self.td_consec_bars:
                    signals.append(StrategySignal(
                        open_time=k.open_time,
                        price=k.close,
                        signal_type="long_exit",
                        label="TD",
                    ))
                    return True, trailing, td_consec, stop_price
            else:
                td_consec = 0
        elif k.high >= target_price:
            if _kline_delta(k) > 0:
                trailing = True
                stop_price = target_price
                td_consec = 0
            else:
                signals.append(StrategySignal(
                    open_time=k.open_time,
                    price=target_price,
                    signal_type="long_exit",
                    label="TP",
                ))
                return True, trailing, td_consec, stop_price

        return False, trailing, td_consec, stop_price

    def _bar_exit_simple_long(
        self,
        k: Kline,
        signals: List[StrategySignal],
        target_price: float,
    ) -> bool:
        exited, self._trailing, self._td_consec, self._stop_price = self._bar_exit_long(
            k, signals, self._stop_price, target_price, self._trailing, self._td_consec,
        )
        return exited
