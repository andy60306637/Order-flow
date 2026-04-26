from __future__ import annotations

import numpy as np

from core.data_types import Kline
from research.base import FactorBase, klines_to_arrays, safe_divide
from research.registry import register_factor
from strategies.base import TickBarMap


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=np.float64)
    if len(values) < window or window <= 0:
        return out
    csum = np.cumsum(np.insert(values, 0, 0.0))
    out[window - 1:] = (csum[window:] - csum[:-window]) / window
    return out


def _rolling_std(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=np.float64)
    if len(values) < window or window <= 0:
        return out
    mean = _rolling_mean(values, window)
    mean_sq = _rolling_mean(values * values, window)
    var = np.maximum(mean_sq - mean * mean, 0.0)
    out[window - 1:] = np.sqrt(var[window - 1:])
    return out


def _true_range(arr: dict[str, np.ndarray]) -> np.ndarray:
    high = arr["high"]
    low = arr["low"]
    close = arr["close"]
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    return np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])


def _tick_metric(
    klines: list[Kline],
    tick_map: TickBarMap | None,
    fn,
) -> np.ndarray:
    out = np.full(len(klines), np.nan, dtype=np.float64)
    if tick_map is None:
        return out
    for i, k in enumerate(klines):
        ticks = tick_map.get(k.open_time)
        if ticks is None or len(ticks) == 0:
            continue
        out[i] = fn(k, ticks)
    return out


@register_factor
class Return1Factor(FactorBase):
    name = "return_1"

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        close = klines_to_arrays(klines)["close"]
        out = np.full(close.shape, np.nan, dtype=np.float64)
        if len(close) > 1:
            out[1:] = safe_divide(close[1:] - close[:-1], close[:-1])
        return out


@register_factor
class RangePctFactor(FactorBase):
    name = "range_pct"

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        return safe_divide(arr["high"] - arr["low"], arr["close"])


@register_factor
class BodyPctFactor(FactorBase):
    name = "body_pct"

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        return safe_divide(np.abs(arr["close"] - arr["open"]), arr["high"] - arr["low"])


@register_factor
class UpperWickRatioFactor(FactorBase):
    name = "upper_wick_ratio"

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        body_hi = np.maximum(arr["open"], arr["close"])
        return safe_divide(arr["high"] - body_hi, arr["high"] - arr["low"])


@register_factor
class LowerWickRatioFactor(FactorBase):
    name = "lower_wick_ratio"

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        body_lo = np.minimum(arr["open"], arr["close"])
        return safe_divide(body_lo - arr["low"], arr["high"] - arr["low"])


@register_factor
class VolumeZscoreFactor(FactorBase):
    name = "volume_zscore"
    window = 20

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        volume = klines_to_arrays(klines)["volume"]
        mean = _rolling_mean(volume, self.window)
        std = _rolling_std(volume, self.window)
        return safe_divide(volume - mean, std)


@register_factor
class AtrRatioFactor(FactorBase):
    name = "atr_ratio"
    window = 14

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        atr = _rolling_mean(_true_range(arr), self.window)
        return safe_divide(atr, arr["close"])


@register_factor
class DeltaEffFactor(FactorBase):
    name = "delta_eff"

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        return safe_divide(2.0 * arr["taker_buy_volume"] - arr["volume"], arr["volume"])


@register_factor
class TakerBuyRatioFactor(FactorBase):
    name = "taker_buy_ratio"

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        return safe_divide(arr["taker_buy_volume"], arr["volume"])


@register_factor
class TickVolumeRatioFactor(FactorBase):
    name = "tick_volume_ratio"
    requires_ticks = True

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _tick_metric(
            klines,
            tick_map,
            lambda k, ticks: float(np.sum(ticks[:, 2])) / k.volume if k.volume > 0 else np.nan,
        )


@register_factor
class WickVolumeRatioFactor(FactorBase):
    name = "wick_volume_ratio"
    requires_ticks = True

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        def calc(k: Kline, ticks: np.ndarray) -> float:
            total = float(np.sum(ticks[:, 2]))
            if total <= 0:
                return np.nan
            body_hi = max(k.open, k.close)
            body_lo = min(k.open, k.close)
            wick_ticks = ticks[(ticks[:, 1] >= body_hi) | (ticks[:, 1] <= body_lo)]
            return float(np.sum(wick_ticks[:, 2])) / total if len(wick_ticks) else 0.0

        return _tick_metric(klines, tick_map, calc)


@register_factor
class WickDeltaEffFactor(FactorBase):
    name = "wick_delta_eff"
    requires_ticks = True

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        def calc(k: Kline, ticks: np.ndarray) -> float:
            body_hi = max(k.open, k.close)
            body_lo = min(k.open, k.close)
            wick_ticks = ticks[(ticks[:, 1] >= body_hi) | (ticks[:, 1] <= body_lo)]
            if len(wick_ticks) == 0:
                return np.nan
            wvol = float(np.sum(wick_ticks[:, 2]))
            if wvol <= 0:
                return np.nan
            wbuy = float(np.sum(wick_ticks[wick_ticks[:, 3] < 0.5, 2]))
            return (2.0 * wbuy - wvol) / wvol

        return _tick_metric(klines, tick_map, calc)
