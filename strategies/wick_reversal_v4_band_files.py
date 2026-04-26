"""
Wick Reversal v4 with per-price-band JSON parameter files.

This strategy keeps the original v4 trading logic, but swaps the parameter
set by price band so each band can be tuned manually from its own JSON file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import numpy as np

from core.data_types import Kline
from strategies import register
from strategies.base import StrategySignal, TickBarMap
from strategies.wick_reversal_v4 import WickReversalV4Strategy


@register
class WickReversalV4BandFilesStrategy(WickReversalV4Strategy):
    name = "Wick Reversal 1m v4 band files"

    band_params_root: str = "config/wick_reversal_v4_band_files"
    band_params_symbol: str = "BTCUSDT"
    band_size: float = 1000.0
    band_floor: float = 0.0

    PARAM_FIELDS = [
        "enable_long",
        "long_zoom_bars",
        "long_sl_offset",
        "long_rr_ratio",
        "long_td_consec_bars",
        "long_k0_vol_gate",
        "long_delta_eff_threshold",
        "long_vol_sma_period",
        "long_vol_sma_mult",
        "lower_wick_absorption_delta_eff_max",
        "lower_wick_absorption_min_vol_ratio",
        "lower_wick_absorption_bar_delta_max",
        "long_min_fee_cover_ratio",
        "long_body_floor_pct",
        "long_wick_type_a_threshold",
        "long_wick_type_b_threshold",
        "long_rr_wick_a",
        "long_rr_wick_b",
        "long_rr_wick_c",
        "enable_short",
        "short_zoom_bars",
        "short_sl_offset",
        "short_rr_ratio",
        "short_td_consec_bars",
        "short_k0_vol_gate",
        "short_delta_eff_threshold",
        "short_vol_sma_period",
        "short_vol_sma_mult",
        "upper_wick_absorption_delta_eff_min",
        "upper_wick_absorption_min_vol_ratio",
        "upper_wick_absorption_bar_delta_min",
        "short_min_fee_cover_ratio",
        "short_body_floor_pct",
        "short_wick_type_a_threshold",
        "short_wick_type_b_threshold",
        "enable_short_wick_a",
        "enable_short_wick_b",
        "enable_short_wick_c",
        "short_a_min_upper_wick_pct",
        "short_rr_wick_a",
        "short_rr_wick_b",
        "short_rr_wick_c",
        "short_b_min_upper_wick_pct",
        "short_b_min_k0_vol",
        "short_b_min_runup_pct",
        "short_b_runup_lookback",
        "taker_fee_rate",
        "slippage_rate",
    ]

    def __init__(self) -> None:
        self._base_params = {name: getattr(self, name) for name in self.PARAM_FIELDS}
        self._band_cache: dict[tuple[int, int], dict[str, Any]] = {}
        self._active_band_key: Optional[tuple[int, int]] = None
        self._trade_band_key: Optional[tuple[int, int]] = None

    def _param_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / self.band_params_root / self.band_params_symbol.upper()

    def _band_key_for_price(self, price: float) -> tuple[int, int]:
        band_size = max(float(self.band_size), 1.0)
        band_floor = float(self.band_floor)
        shifted = max(float(price) - band_floor, 0.0)
        idx = int(shifted // band_size)
        low = int(band_floor + idx * band_size)
        high = int(low + band_size)
        return low, high

    def _band_path(self, key: tuple[int, int]) -> Path:
        low, high = key
        return self._param_dir() / f"{low:05d}_{high:05d}.json"

    def _load_band_params(self, key: tuple[int, int]) -> dict[str, Any]:
        cached = self._band_cache.get(key)
        if cached is not None:
            return cached

        merged = dict(self._base_params)
        path = self._band_path(key)
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            payload = raw.get("params", raw)
            for name in self.PARAM_FIELDS:
                if name in payload:
                    merged[name] = payload[name]
        self._band_cache[key] = merged
        return merged

    def _apply_band_by_key(self, key: tuple[int, int]) -> tuple[int, int]:
        if self._active_band_key == key:
            return key
        params = self._load_band_params(key)
        for name, value in params.items():
            setattr(self, name, value)
        self._active_band_key = key
        return key

    def _apply_band_for_price(self, price: float) -> tuple[int, int]:
        key = self._band_key_for_price(price)
        return self._apply_band_by_key(key)

    def _apply_trade_band(self) -> None:
        if self._trade_band_key is not None:
            self._apply_band_by_key(self._trade_band_key)

    def _is_k0_long(
        self,
        k: Kline,
        ticks: Optional[np.ndarray] = None,
    ) -> bool:
        self._apply_band_for_price(k.close)
        return super()._is_k0_long(k, ticks)

    def _is_k0_short(
        self,
        k: Kline,
        ticks: Optional[np.ndarray] = None,
    ) -> bool:
        self._apply_band_for_price(k.close)
        return super()._is_k0_short(k, ticks)

    def _bar_entry(
        self,
        k: Kline,
        i: int,
        klines: list[Kline],
        signals: list[StrategySignal],
        k0: Kline,
    ) -> tuple[bool, float, float, float]:
        band_key = self._apply_band_for_price(k0.close)
        entered, entry_p, stop_p, target_p = super()._bar_entry(k, i, klines, signals, k0)
        if entered:
            self._trade_band_key = band_key
        return entered, entry_p, stop_p, target_p

    def _tick_entry(
        self,
        k: Kline,
        i: int,
        klines: list[Kline],
        tick_map: TickBarMap,
        signals: list[StrategySignal],
        k0: Kline,
    ) -> tuple[bool, float, float, float]:
        band_key = self._apply_band_for_price(k0.close)
        entered, entry_p, stop_p, target_p = super()._tick_entry(k, i, klines, tick_map, signals, k0)
        if entered:
            self._trade_band_key = band_key
        return entered, entry_p, stop_p, target_p

    def _bar_entry_short(
        self,
        k: Kline,
        i: int,
        klines: list[Kline],
        signals: list[StrategySignal],
        k0: Kline,
    ) -> tuple[bool, float, float, float]:
        band_key = self._apply_band_for_price(k0.close)
        entered, entry_p, stop_p, target_p = super()._bar_entry_short(k, i, klines, signals, k0)
        if entered:
            self._trade_band_key = band_key
        return entered, entry_p, stop_p, target_p

    def _tick_entry_short(
        self,
        k: Kline,
        i: int,
        klines: list[Kline],
        tick_map: TickBarMap,
        signals: list[StrategySignal],
        k0: Kline,
    ) -> tuple[bool, float, float, float]:
        band_key = self._apply_band_for_price(k0.close)
        entered, entry_p, stop_p, target_p = super()._tick_entry_short(k, i, klines, tick_map, signals, k0)
        if entered:
            self._trade_band_key = band_key
        return entered, entry_p, stop_p, target_p

    def _tick_exit_long(
        self,
        k: Kline,
        tick_map: TickBarMap,
        signals: list[StrategySignal],
        target_price: float,
    ) -> bool:
        self._apply_trade_band()
        exited = super()._tick_exit_long(k, tick_map, signals, target_price)
        if exited:
            self._trade_band_key = None
        return exited

    def _bar_exit_long(
        self,
        k: Kline,
        signals: list[StrategySignal],
        stop_price: float,
        target_price: float,
        trailing: bool,
        td_consec: int,
    ) -> tuple[bool, bool, int, float]:
        self._apply_trade_band()
        exited, trailing, td_consec, stop_price = super()._bar_exit_long(
            k, signals, stop_price, target_price, trailing, td_consec
        )
        if exited:
            self._trade_band_key = None
        return exited, trailing, td_consec, stop_price

    def _bar_exit_simple_long(
        self,
        k: Kline,
        signals: list[StrategySignal],
        target_price: float,
    ) -> bool:
        self._apply_trade_band()
        exited = super()._bar_exit_simple_long(k, signals, target_price)
        if exited:
            self._trade_band_key = None
        return exited

    def _tick_exit_short(
        self,
        k: Kline,
        tick_map: TickBarMap,
        signals: list[StrategySignal],
        target_price: float,
    ) -> bool:
        self._apply_trade_band()
        exited = super()._tick_exit_short(k, tick_map, signals, target_price)
        if exited:
            self._trade_band_key = None
        return exited

    def _bar_exit_short(
        self,
        k: Kline,
        signals: list[StrategySignal],
        stop_price: float,
        target_price: float,
        trailing: bool,
        td_consec: int,
    ) -> tuple[bool, bool, int, float]:
        self._apply_trade_band()
        exited, trailing, td_consec, stop_price = super()._bar_exit_short(
            k, signals, stop_price, target_price, trailing, td_consec
        )
        if exited:
            self._trade_band_key = None
        return exited, trailing, td_consec, stop_price

    def _bar_exit_simple_short(
        self,
        k: Kline,
        signals: list[StrategySignal],
        target_price: float,
    ) -> bool:
        self._apply_trade_band()
        exited = super()._bar_exit_simple_short(k, signals, target_price)
        if exited:
            self._trade_band_key = None
        return exited
