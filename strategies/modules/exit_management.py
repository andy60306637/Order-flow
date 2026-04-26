"""出場管理模組：止損/止盈/追蹤止損/時間衰減。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from core.data_types import Kline
from strategies.base import StrategySignal
from strategies.modules.base_module import BaseModule, ModuleConfig


@dataclass
class ExitConfig(ModuleConfig):
    use_trailing_stop: bool  = True
    trailing_mode:     str   = "lock_tp"      # "lock_tp" | "breakeven_cost"
    trailing_trigger_r: float = 1.0           # 達到幾倍 R 後啟動追蹤
    time_decay_bars:   int   = 0              # 0=關閉；超過此 K 棒數強制離場
    tp_rr_ratio:       float = 2.0            # 固定止盈 RR 倍數


class ExitModule(BaseModule):
    """
    根據持倉狀態與當前 K 棒判斷是否該出場。
    position dict 格式：
      {
        "direction":   "long" | "short",
        "entry_price": float,
        "stop_price":  float,
        "tp_price":    float,
        "trail_price": float | None,   # 追蹤止損觸發價
        "open_time":   int,            # entry K 棒 open_time (ms)
      }
    """

    def __init__(self, cfg: ExitConfig | None = None) -> None:
        self.cfg = cfg or ExitConfig()

    def init_position(
        self,
        direction:   str,
        entry_price: float,
        stop_price:  float,
        open_time:   int,
    ) -> dict:
        """建立新持倉 dict，含 TP 價格。"""
        risk = abs(entry_price - stop_price)
        if direction == "long":
            tp_price = entry_price + risk * self.cfg.tp_rr_ratio
        else:
            tp_price = entry_price - risk * self.cfg.tp_rr_ratio

        return {
            "direction":   direction,
            "entry_price": entry_price,
            "stop_price":  stop_price,
            "tp_price":    tp_price,
            "trail_price": None,
            "open_time":   open_time,
        }

    def check_exit(
        self,
        k:          Kline,
        position:   dict,
        tick_map:   Optional[object] = None,
        bars_held:  int = 0,
    ) -> Optional[StrategySignal]:
        """
        檢查本 K 棒是否應出場。
        回傳 StrategySignal（long_exit / short_exit）或 None。
        """
        direction  = position["direction"]
        entry      = position["entry_price"]
        stop       = position["stop_price"]
        tp         = position["tp_price"]
        trail      = position.get("trail_price")
        cfg        = self.cfg
        sig_type   = "long_exit" if direction == "long" else "short_exit"

        # ── 時間衰減 ─────────────────────────────────────────────────────────
        if cfg.time_decay_bars > 0 and bars_held >= cfg.time_decay_bars:
            return StrategySignal(
                open_time=k.open_time, price=k.close,
                signal_type=sig_type, label="TD",
                fill_price=k.open,
            )

        if direction == "long":
            # ── 止損 ────────────────────────────────────────────────────────
            if k.low <= stop:
                return StrategySignal(
                    open_time=k.open_time, price=stop,
                    signal_type=sig_type, label="SL",
                    fill_price=min(k.open, stop),
                )

            # ── 追蹤止損更新 ────────────────────────────────────────────────
            risk = abs(entry - stop)
            if cfg.use_trailing_stop and risk > 0:
                profit_r = (k.high - entry) / risk
                if profit_r >= cfg.trailing_trigger_r:
                    if cfg.trailing_mode == "lock_tp":
                        new_trail = k.high - risk
                    else:
                        new_trail = entry + (k.high - entry) * 0.0  # breakeven
                    if trail is None or new_trail > trail:
                        position["trail_price"] = new_trail
                        trail = new_trail

            if trail is not None and k.low <= trail:
                return StrategySignal(
                    open_time=k.open_time, price=trail,
                    signal_type=sig_type, label="TS",
                    fill_price=min(k.open, trail),
                )

            # ── 止盈 ────────────────────────────────────────────────────────
            if k.high >= tp:
                return StrategySignal(
                    open_time=k.open_time, price=tp,
                    signal_type=sig_type, label="TP",
                    fill_price=max(k.open, tp),
                )

        else:  # short
            if k.high >= stop:
                return StrategySignal(
                    open_time=k.open_time, price=stop,
                    signal_type=sig_type, label="SL",
                    fill_price=max(k.open, stop),
                )

            risk = abs(entry - stop)
            if cfg.use_trailing_stop and risk > 0:
                profit_r = (entry - k.low) / risk
                if profit_r >= cfg.trailing_trigger_r:
                    if cfg.trailing_mode == "lock_tp":
                        new_trail = k.low + risk
                    else:
                        new_trail = entry
                    if trail is None or new_trail < trail:
                        position["trail_price"] = new_trail
                        trail = new_trail

            if trail is not None and k.high >= trail:
                return StrategySignal(
                    open_time=k.open_time, price=trail,
                    signal_type=sig_type, label="TS",
                    fill_price=max(k.open, trail),
                )

            if k.low <= tp:
                return StrategySignal(
                    open_time=k.open_time, price=tp,
                    signal_type=sig_type, label="TP",
                    fill_price=min(k.open, tp),
                )

        return None
