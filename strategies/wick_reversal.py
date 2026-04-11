"""
Wick Reversal 策略 (BTCUSDT 1m v3)

核心邏輯：
  1. 尋找 k0：具有明顯引線的 K 棒（做多 = 看跌長下引線，做空 = 看漲長上引線）
  2. 若出現新 k0，只保留最新一根
  3. zoom = k0 後 1~5 根 K 棒，期間觀察防守線是否被破
  4. zoom 內若突破 + delta 條件同時滿足，立即進場（即時 delta + 即時價格）
  5. 固定停損位移 10 USDT，初始停利 1:1 盈虧比
  6. 達到 1:1 後若 Delta 順向，切換追蹤模式放大利潤
  7. 一次只允許一筆持倉

Tick-by-tick 模式（tick_map 提供時）：
  - 進場條件改為逐 tick 累計 delta，在突破 + delta 同時達標的 tick 入場
  - TP 位的追蹤/了結決策使用價格觸及 TP 瞬間的累計 delta
  - 消除 look-ahead bias
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np

from core.data_types import Kline
from strategies.base import StrategyBase, StrategySignal, TickBarMap
from strategies import register


def _kline_delta(k: Kline) -> float:
    """K 棒 delta = taker_buy_vol - taker_sell_vol。"""
    return 2.0 * k.taker_buy_volume - k.volume


def _kline_delta_eff(k: Kline) -> float:
    """Delta efficiency = delta / volume（介於 -1 ~ +1）。"""
    if k.volume == 0:
        return 0.0
    return _kline_delta(k) / k.volume


@register
class WickReversalStrategy(StrategyBase):
    name = "Wick Reversal 1m"

    # ── 可調參數 ──────────────────────────────────────────────────────────────
    zoom_bars:                 int   = 5     # k0 後觀察窗口（根）
    sl_offset:                 float = 10.0   # 固定停損位移 (USDT)
    rr_ratio:                  float = 1.0    # 盈虧比
    # ── 做多進場檢驗 ────────────────────────────────────────────────────────────
    long_delta_eff_threshold:  float = 0.6   # 做多 Delta Eff 閾值（0~1）
    long_vol_sma_period:       int   = 20    # 做多成交量 SMA 窗期；0=不過濾
    long_vol_sma_mult:         float = 1.2   # 做多成交量門標倍率（volume > SMA * mult）
    # ── 做空進場檢驗 ────────────────────────────────────────────────────────────
    short_delta_eff_threshold: float = 0.6   # 做空 Delta Eff 閾值（0~1）
    short_vol_sma_period:      int   = 20    # 做空成交量 SMA 窗期；0=不過濾
    short_vol_sma_mult:        float = 1.2  # 做空成交量門標倍率（volume > SMA * mult）
    # ── Trailing Delta 出場確認 ────────────────────────────────────────────────
    td_consec_bars:            int   = 2     # 連續幾根反向 delta 才觸發 TD 出場

    # ─────────────────────────────────────────────────────────────────────────
    def on_history(self, klines: List[Kline],
                   tick_map: Optional[TickBarMap] = None) -> List[StrategySignal]:
        signals: List[StrategySignal] = []
        n = len(klines)
        if n < 2:
            return signals

        use_ticks = tick_map is not None and len(tick_map) > 0

        # ── 狀態追蹤 ─────────────────────────────────────────────────────────
        k0: Optional[Kline] = None
        k0_idx: int = -1
        k0_dir: str = ""              # "long" | "short"

        in_position = False
        pos_dir     = ""
        entry_price = 0.0
        stop_price  = 0.0
        target_price = 0.0
        trailing    = False
        td_consec   = 0       # 連續反向 delta 計數

        # tick 模式用 instance 暫存（_tick_exit 需要可變狀態）
        self._trailing  = False
        self._td_consec = 0
        self._stop_price = 0.0

        for i in range(n):
            k = klines[i]
            rng = k.high - k.low

            # ══════════════════════════════════════════════════════════════════
            # Step 0：K0 標記（不受持倉限制，每根 K 棒先標記一次）
            # ══════════════════════════════════════════════════════════════════
            if rng > 0 and not in_position:
                mid  = (k.high + k.low) / 2.0
                body = abs(k.close - k.open)
                # 做多 k0：看跌 + 收在上半部 + 下引線 > 實體
                if (k.close < k.open
                        and k.close >= mid
                        and (k.close - k.low) > body):
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=k.low,
                        signal_type="k0_long", label="k0",
                    ))
                # 做空 k0：看漲 + 收在下半部 + 上引線 > 實體
                elif (k.close > k.open
                        and k.close <= mid
                        and (k.high - k.close) > body):
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=k.high,
                        signal_type="k0_short", label="k0",
                    ))

            # ══════════════════════════════════════════════════════════════════
            # Step 1：有持倉 → 檢查 SL / TP / 追蹤
            # ══════════════════════════════════════════════════════════════════
            if in_position:
                exited = False

                if use_ticks:
                    # 同步局部狀態 → instance 暫存
                    self._trailing = trailing
                    self._td_consec = td_consec
                    self._stop_price = stop_price
                    # Tick 模式：逐 tick 判斷 SL/TP/TS/TD
                    exited = self._tick_exit(
                        k, i, tick_map, signals,
                        pos_dir, entry_price, stop_price, target_price,
                        trailing, td_consec,
                    )
                    if not exited:
                        # 讀回可能被 _tick_exit 更新的狀態
                        trailing = self._trailing
                        td_consec = self._td_consec
                        stop_price = self._stop_price
                else:
                    # 原始 K 棒模式
                    exited, trailing, td_consec, stop_price = self._bar_exit(
                        k, signals, pos_dir, stop_price, target_price,
                        trailing, td_consec,
                    )

                if exited:
                    in_position = False
                    pos_dir = ""
                    trailing = False
                    td_consec = 0
                else:
                    continue

            # ══════════════════════════════════════════════════════════════════
            # Step 2：有 k0 且在 zoom 窗口 → 檢查防守線 / 進場條件
            # ══════════════════════════════════════════════════════════════════
            def _vol_ok_for(period: int, mult: float) -> bool:
                if period <= 0 or i < period:
                    return True
                _s = i - period + 1
                _sma = sum(klines[j].volume for j in range(_s, i + 1)) / period
                return k.volume > _sma * mult

            if k0 is not None and i > k0_idx:
                bars_after = i - k0_idx
                if bars_after <= self.zoom_bars:
                    entered = False

                    if use_ticks and k.open_time in tick_map:
                        # ── Tick 模式進場 ─────────────────────────────────
                        entered, entry_price, stop_price, target_price = \
                            self._tick_entry(
                                k, i, klines, tick_map, signals, k0, k0_dir,
                            )
                    else:
                        # ── 原始 K 棒模式進場 ─────────────────────────────
                        if k0_dir == "long":
                            if k.low < k0.low:
                                k0 = None
                            elif (k.high >= k0.high
                                    and _kline_delta_eff(k) > self.long_delta_eff_threshold
                                    and _vol_ok_for(self.long_vol_sma_period, self.long_vol_sma_mult)):
                                entry_price = k0.high
                                stop_price = k0.low - self.sl_offset
                                risk = entry_price - stop_price
                                target_price = entry_price + risk * self.rr_ratio
                                signals.append(StrategySignal(
                                    open_time=k.open_time, price=entry_price,
                                    signal_type="long_entry", label="L",
                                    stop_price=stop_price,
                                ))
                                entered = True
                        else:  # short
                            if k.high > k0.high:
                                k0 = None
                            elif (k.low <= k0.low
                                    and _kline_delta_eff(k) < -self.short_delta_eff_threshold
                                    and _vol_ok_for(self.short_vol_sma_period, self.short_vol_sma_mult)):
                                entry_price = k0.low
                                stop_price = k0.high + self.sl_offset
                                risk = stop_price - entry_price
                                target_price = entry_price - risk * self.rr_ratio
                                signals.append(StrategySignal(
                                    open_time=k.open_time, price=entry_price,
                                    signal_type="short_entry", label="S",
                                    stop_price=stop_price,
                                ))
                                entered = True

                    if entered:
                        in_position = True
                        pos_dir = k0_dir
                        trailing = False
                        td_consec = 0
                        k0 = None
                        continue
                    elif k0 is not None and k0_dir == "long" and k.low < k0.low:
                        k0 = None
                    elif k0 is not None and k0_dir == "short" and k.high > k0.high:
                        k0 = None
                else:
                    k0 = None  # zoom 過期

            # ══════════════════════════════════════════════════════════════════
            # Step 3：更新 k0 指針（標記已在 Step 0 發出）
            # ══════════════════════════════════════════════════════════════════
            if not in_position and rng > 0:
                mid  = (k.high + k.low) / 2.0
                body = abs(k.close - k.open)
                if (k.close < k.open
                        and k.close >= mid
                        and (k.close - k.low) > body):
                    k0 = k
                    k0_idx = i
                    k0_dir = "long"
                elif (k.close > k.open
                        and k.close <= mid
                        and (k.high - k.close) > body):
                    k0 = k
                    k0_idx = i
                    k0_dir = "short"

        return signals

    # ─────────────────────────────────────────────────────────────────────────
    # Tick-by-tick 進場：逐 tick 累計 delta，在突破 + delta 同時達標時入場
    # ─────────────────────────────────────────────────────────────────────────
    def _tick_entry(
        self, k: Kline, i: int, klines: List[Kline],
        tick_map: TickBarMap, signals: List[StrategySignal],
        k0: Kline, k0_dir: str,
    ) -> tuple[bool, float, float, float]:
        """
        逐 tick 檢查進場條件。回傳 (entered, entry_price, stop_price, target_price)。

        邏輯：遍歷該 K 棒的每筆 aggTrade，逐步累加 delta 和 volume，
        一旦「價格突破 + 累計 delta_eff 達標」同時成立就入場。
        Vol SMA 在迴圈前以前一根已收棒成交量做一次性檢查，避免
        partial volume vs full-bar SMA 的不公平比較。
        """
        ticks = tick_map.get(k.open_time)
        if ticks is None or len(ticks) == 0:
            return False, 0.0, 0.0, 0.0

        # ── Vol SMA 前置檢查：用前一根已收棒成交量，避免 look-ahead ─────
        prev_vol = klines[i - 1].volume if i > 0 else 0.0
        if k0_dir == "long":
            if not self._vol_sma_ok(
                klines, i, prev_vol,
                self.long_vol_sma_period, self.long_vol_sma_mult,
            ):
                return False, 0.0, 0.0, 0.0
        else:
            if not self._vol_sma_ok(
                klines, i, prev_vol,
                self.short_vol_sma_period, self.short_vol_sma_mult,
            ):
                return False, 0.0, 0.0, 0.0

        cum_buy_vol = 0.0
        cum_vol     = 0.0

        for t in ticks:
            # t: [trade_time, price, qty, is_buyer_maker]
            price = t[1]
            qty   = t[2]
            is_bm = t[3] > 0.5  # is_buyer_maker

            cum_vol += qty
            if not is_bm:
                cum_buy_vol += qty  # taker buy

            if cum_vol == 0:
                continue

            cum_delta = 2.0 * cum_buy_vol - cum_vol
            cum_delta_eff = cum_delta / cum_vol

            if k0_dir == "long":
                # 防守線破壞
                if price < k0.low:
                    return False, 0.0, 0.0, 0.0
                # 突破 + delta 達標
                if (price >= k0.high
                        and cum_delta_eff > self.long_delta_eff_threshold):
                    # 成交價 = 觸發 tick 的實際成交價（可能穿越 k0.high）
                    fill_p = price
                    entry_p = k0.high      # 訊號基準價（圖表標記）
                    stop_p = k0.low - self.sl_offset
                    risk = fill_p - stop_p
                    if risk <= 0:
                        continue
                    target_p = fill_p + risk * self.rr_ratio
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=entry_p,
                        signal_type="long_entry", label="L",
                        stop_price=stop_p,
                        fill_price=fill_p,
                    ))
                    return True, fill_p, stop_p, target_p

            else:  # short
                if price > k0.high:
                    return False, 0.0, 0.0, 0.0
                if (price <= k0.low
                        and cum_delta_eff < -self.short_delta_eff_threshold):
                    fill_p = price
                    entry_p = k0.low
                    stop_p = k0.high + self.sl_offset
                    risk = stop_p - fill_p
                    if risk <= 0:
                        continue
                    target_p = fill_p - risk * self.rr_ratio
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=entry_p,
                        signal_type="short_entry", label="S",
                        stop_price=stop_p,
                        fill_price=fill_p,
                    ))
                    return True, fill_p, stop_p, target_p

        return False, 0.0, 0.0, 0.0

    # ─────────────────────────────────────────────────────────────────────────
    # Tick-by-tick 出場：SL/TP/TS/TD 均用 tick 級精度
    # ─────────────────────────────────────────────────────────────────────────
    def _tick_exit(
        self, k: Kline, i: int, tick_map: TickBarMap,
        signals: List[StrategySignal],
        pos_dir: str, entry_price: float,
        stop_price: float, target_price: float,
        trailing: bool, td_consec: int,
    ) -> bool:
        """
        逐 tick 檢查出場。使用 tick 級累計 delta 判斷 TP 位的追蹤決策。
        回傳 True 表示已出場（signal 已 append）。

        注意：此方法會修改 self 的狀態屬性（透過 _state dict）不可行，
        改為直接修改傳入的 mutable wrapper — 但 Python 不支持。
        因此我們改用 instance 級暫存：
          self._trailing, self._td_consec, self._stop_price
        """
        ticks = tick_map.get(k.open_time)

        # 先檢查 K 棒級 SL（即使沒有 tick 也要處理）
        if ticks is None or len(ticks) == 0:
            return self._bar_exit_simple(
                k, signals, pos_dir, stop_price, target_price,
                trailing, td_consec,
            )

        cum_buy_vol = 0.0
        cum_vol     = 0.0

        for t in ticks:
            price = t[1]
            qty   = t[2]
            is_bm = t[3] > 0.5

            cum_vol += qty
            if not is_bm:
                cum_buy_vol += qty

            cum_delta = 2.0 * cum_buy_vol - cum_vol

            if pos_dir == "long":
                # SL / TS — 用實際穿越 tick 價（可能比 stop 更差）
                if price <= self._stop_price:
                    label = "TS" if self._trailing else "SL"
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=self._stop_price,
                        signal_type="long_exit", label=label,
                        fill_price=price,
                    ))
                    return True
                # Trailing: TD 在迴圈後用本根累計 delta 判斷
                if self._trailing:
                    pass
                # TP 觸及：用 tick 級累計 delta 判斷追蹤/了結
                elif price >= target_price:
                    if cum_delta > 0:
                        self._trailing = True
                        self._stop_price = target_price
                        self._td_consec = 0
                    else:
                        signals.append(StrategySignal(
                            open_time=k.open_time, price=target_price,
                            signal_type="long_exit", label="TP",
                        ))
                        return True
            else:  # short
                if price >= self._stop_price:
                    label = "TS" if self._trailing else "SL"
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=self._stop_price,
                        signal_type="short_exit", label=label,
                        fill_price=price,
                    ))
                    return True
                if self._trailing:
                    pass
                elif price <= target_price:
                    if cum_delta < 0:
                        self._trailing = True
                        self._stop_price = target_price
                        self._td_consec = 0
                    else:
                        signals.append(StrategySignal(
                            open_time=k.open_time, price=target_price,
                            signal_type="short_exit", label="TP",
                        ))
                        return True

        # ── 本根結束：追蹤模式用本根 tick 累計 delta 做 TD 判斷 ──────────
        if self._trailing:
            # 用本根 tick 累計 delta（非整根收棒值），消除 look-ahead
            td_delta = cum_delta  # 本根所有 tick 的 cum_delta
            if pos_dir == "long":
                if td_delta <= 0:
                    self._td_consec += 1
                    if self._td_consec >= self.td_consec_bars:
                        signals.append(StrategySignal(
                            open_time=k.open_time, price=k.close,
                            signal_type="long_exit", label="TD",
                        ))
                        return True
                else:
                    self._td_consec = 0
            else:
                if td_delta >= 0:
                    self._td_consec += 1
                    if self._td_consec >= self.td_consec_bars:
                        signals.append(StrategySignal(
                            open_time=k.open_time, price=k.close,
                            signal_type="short_exit", label="TD",
                        ))
                        return True
                else:
                    self._td_consec = 0

        return False

    # ─────────────────────────────────────────────────────────────────────────
    # Vol SMA 檢查（用於 tick 模式，使用前 N-1 根完成 K 棒的 SMA）
    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _vol_sma_ok(
        klines: List[Kline], cur_idx: int, cur_vol: float,
        period: int, mult: float,
    ) -> bool:
        """Tick 模式 vol SMA：僅用已完成棒（不含當根），避免 look-ahead。"""
        if period <= 0 or cur_idx < period:
            return True
        # 只用 cur_idx 之前的 period 根已完成 K 棒
        s = cur_idx - period
        sma = sum(klines[j].volume for j in range(s, cur_idx)) / period
        return cur_vol > sma * mult

    # ─────────────────────────────────────────────────────────────────────────
    # Bar-level 出場（無 tick 模式的原始邏輯）
    # ─────────────────────────────────────────────────────────────────────────
    def _bar_exit(
        self, k: Kline, signals: List[StrategySignal],
        pos_dir: str, stop_price: float, target_price: float,
        trailing: bool, td_consec: int,
    ) -> tuple[bool, bool, int, float]:
        """回傳 (exited, trailing, td_consec, stop_price)。"""
        if pos_dir == "long":
            if k.low <= stop_price:
                label = "TS" if trailing else "SL"
                signals.append(StrategySignal(
                    open_time=k.open_time, price=stop_price,
                    signal_type="long_exit", label=label,
                ))
                return True, trailing, td_consec, stop_price
            if trailing:
                if _kline_delta(k) <= 0:
                    td_consec += 1
                    if td_consec >= self.td_consec_bars:
                        signals.append(StrategySignal(
                            open_time=k.open_time, price=k.close,
                            signal_type="long_exit", label="TD",
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
                        open_time=k.open_time, price=target_price,
                        signal_type="long_exit", label="TP",
                    ))
                    return True, trailing, td_consec, stop_price
        else:  # short
            if k.high >= stop_price:
                label = "TS" if trailing else "SL"
                signals.append(StrategySignal(
                    open_time=k.open_time, price=stop_price,
                    signal_type="short_exit", label=label,
                ))
                return True, trailing, td_consec, stop_price
            if trailing:
                if _kline_delta(k) >= 0:
                    td_consec += 1
                    if td_consec >= self.td_consec_bars:
                        signals.append(StrategySignal(
                            open_time=k.open_time, price=k.close,
                            signal_type="short_exit", label="TD",
                        ))
                        return True, trailing, td_consec, stop_price
                else:
                    td_consec = 0
            elif k.low <= target_price:
                if _kline_delta(k) < 0:
                    trailing = True
                    stop_price = target_price
                    td_consec = 0
                else:
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=target_price,
                        signal_type="short_exit", label="TP",
                    ))
                    return True, trailing, td_consec, stop_price

        return False, trailing, td_consec, stop_price

    def _bar_exit_simple(
        self, k: Kline, signals: List[StrategySignal],
        pos_dir: str, stop_price: float, target_price: float,
        trailing: bool, td_consec: int,
    ) -> bool:
        """Tick 模式但該 K 棒無 tick 資料時的回退出場邏輯。
        使用 self._trailing / self._td_consec / self._stop_price。"""
        exited, self._trailing, self._td_consec, self._stop_price = \
            self._bar_exit(
                k, signals, pos_dir,
                self._stop_price, target_price,
                self._trailing, self._td_consec,
            )
        return exited

