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
"""
from __future__ import annotations

from typing import List, Optional

from core.data_types import Kline
from strategies.base import StrategyBase, StrategySignal
from strategies import register


def _kline_delta(k: Kline) -> float:
    """K 棒 delta = taker_buy_vol - taker_sell_vol。"""
    return 2.0 * k.taker_buy_volume - k.volume


@register
class WickReversalStrategy(StrategyBase):
    name = "Wick Reversal 1m"

    # ── 可調參數 ──────────────────────────────────────────────────────────────
    zoom_bars:  int   = 5            # k0 後觀察窗口（根）
    sl_offset:  float = 10.0         # 固定停損位移 (USDT)
    rr_ratio:   float = 1.0          # 盈虧比

    # ─────────────────────────────────────────────────────────────────────────
    def on_history(self, klines: List[Kline]) -> List[StrategySignal]:
        signals: List[StrategySignal] = []
        n = len(klines)
        if n < 2:
            return signals

        # ── 狀態追蹤 ─────────────────────────────────────────────────────────
        k0: Optional[Kline] = None
        k0_idx: int = -1
        k0_dir: str = ""              # "long" | "short"

        in_position = False
        pos_dir     = ""
        stop_price  = 0.0
        target_price = 0.0
        trailing    = False

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
                if pos_dir == "long":
                    if k.low <= stop_price:
                        label = "TS" if trailing else "SL"
                        signals.append(StrategySignal(
                            open_time=k.open_time, price=stop_price,
                            signal_type="long_exit", label=label,
                        ))
                        exited = True
                    elif trailing:
                        # 追蹤模式：delta 轉負 → 停利出場
                        if _kline_delta(k) <= 0:
                            signals.append(StrategySignal(
                                open_time=k.open_time, price=k.close,
                                signal_type="long_exit", label="TD",
                            ))
                            exited = True
                    elif k.high >= target_price:
                        # 觸及 1:1 停利位 → 檢查 delta 決定是否追蹤
                        if _kline_delta(k) > 0:
                            trailing = True
                            stop_price = target_price   # 停損上移至 1:1
                        else:
                            signals.append(StrategySignal(
                                open_time=k.open_time, price=target_price,
                                signal_type="long_exit", label="TP",
                            ))
                            exited = True
                else:  # short
                    if k.high >= stop_price:
                        label = "TS" if trailing else "SL"
                        signals.append(StrategySignal(
                            open_time=k.open_time, price=stop_price,
                            signal_type="short_exit", label=label,
                        ))
                        exited = True
                    elif trailing:
                        # 追蹤模式：delta 轉正 → 停利出場
                        if _kline_delta(k) >= 0:
                            signals.append(StrategySignal(
                                open_time=k.open_time, price=k.close,
                                signal_type="short_exit", label="TD",
                            ))
                            exited = True
                    elif k.low <= target_price:
                        # 觸及 1:1 停利位 → 檢查 delta 決定是否追蹤
                        if _kline_delta(k) < 0:
                            trailing = True
                            stop_price = target_price   # 停損下移至 1:1
                        else:
                            signals.append(StrategySignal(
                                open_time=k.open_time, price=target_price,
                                signal_type="short_exit", label="TP",
                            ))
                            exited = True

                if exited:
                    in_position = False
                    pos_dir = ""
                    trailing = False
                    # 出場後繼續往下檢查是否有新 k0（同根）
                else:
                    continue   # 仍在持倉，跳過其餘邏輯

            # ══════════════════════════════════════════════════════════════════
            # Step 2：有 k0 且在 zoom 窗口 → 檢查防守線 / 進場條件
            # ══════════════════════════════════════════════════════════════════
            if k0 is not None and i > k0_idx:
                bars_after = i - k0_idx
                if bars_after <= self.zoom_bars:
                    if k0_dir == "long":
                        if k.low < k0.low:
                            k0 = None          # 防守線被破，失效
                        elif k.high >= k0.high and _kline_delta(k) > 0:
                            entry = k0.high
                            stop_price = k0.low - self.sl_offset
                            risk = entry - stop_price
                            target_price = entry + risk * self.rr_ratio
                            signals.append(StrategySignal(
                                open_time=k.open_time, price=entry,
                                signal_type="long_entry", label="L",
                                stop_price=stop_price,
                            ))
                            in_position = True
                            pos_dir = "long"
                            k0 = None
                            continue
                    else:  # short
                        if k.high > k0.high:
                            k0 = None          # 防守線被破，失效
                        elif k.low <= k0.low and _kline_delta(k) < 0:
                            entry = k0.low
                            stop_price = k0.high + self.sl_offset
                            risk = stop_price - entry
                            target_price = entry - risk * self.rr_ratio
                            signals.append(StrategySignal(
                                open_time=k.open_time, price=entry,
                                signal_type="short_entry", label="S",
                                stop_price=stop_price,
                            ))
                            in_position = True
                            pos_dir = "short"
                            k0 = None
                            continue
                else:
                    k0 = None                  # zoom 過期

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

