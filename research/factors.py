from __future__ import annotations

import numpy as np

from core.data_types import Kline
from research.base import (
    FACTOR_SIDE_LONG,
    FACTOR_SIDE_SHORT,
    FACTOR_SIDES,
    GROUP_MEAN_REVERSION,
    GROUP_MICROSTRUCTURE,
    GROUP_MOMENTUM,
    GROUP_REGIME,
    GROUP_VOLATILITY,
    GROUP_VOLUME,
    FactorBase,
    klines_to_arrays,
    safe_divide,
)
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
class LowerWickBodyRatioFactor(FactorBase):
    name = "lower_wick_to_body_ratio"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        body_lo = np.minimum(arr["open"], arr["close"])
        body = np.abs(arr["close"] - arr["open"])
        lower_wick = body_lo - arr["low"]
        # Use body_floor = max(close * 0.00001, 1e-9) to match strategy
        body_floor = np.maximum(arr["close"] * 0.00001, 1e-9)
        denom = np.maximum(body, body_floor)
        return safe_divide(lower_wick, denom)


@register_factor
class UpperWickBodyRatioFactor(FactorBase):
    name = "upper_wick_to_body_ratio"
    sides = (FACTOR_SIDE_SHORT,)
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        body_hi = np.maximum(arr["open"], arr["close"])
        body = np.abs(arr["close"] - arr["open"])
        upper_wick = arr["high"] - body_hi
        # Use body_floor = max(close * 0.00001, 1e-9) to match strategy
        body_floor = np.maximum(arr["close"] * 0.00001, 1e-9)
        denom = np.maximum(body, body_floor)
        return safe_divide(upper_wick, denom)


@register_factor
class BodyPositionRatioFactor(FactorBase):
    """
    Factor 1: Body_Position_Ratio
    Definition: (body_mid - low) / (high - low)
    Interpretation: Closer to 1.0 means body is at the top (bullish resistance/strength).
    """
    name = "body_position_ratio"
    sides = FACTOR_SIDES
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        body_mid = (arr["open"] + arr["close"]) / 2.0
        range_ = arr["high"] - arr["low"]
        return safe_divide(body_mid - arr["low"], range_)


@register_factor
class VolumeZScoreFactor(FactorBase):
    """
    Factor 3: Volume_Z_Score
    Definition: (volume - mean(volume, N)) / std(volume, N)
    """
    name = "volume_z_score"
    sides = FACTOR_SIDES
    group = GROUP_VOLUME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        vol = arr["volume"]
        window = 20  # Default window
        mean = _rolling_mean(vol, window)
        std = _rolling_std(vol, window)
        return safe_divide(vol - mean, std)


@register_factor
class LowerWickDeltaEffFactor(FactorBase):
    name = "lower_wick_delta_eff"
    requires_ticks = True
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_MICROSTRUCTURE

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        def calc(k: Kline, ticks: np.ndarray) -> float:
            body_lo = min(k.open, k.close)
            wick_ticks = ticks[ticks[:, 1] <= body_lo]
            if len(wick_ticks) == 0:
                return np.nan
            wick_vol = float(np.sum(wick_ticks[:, 2]))
            if wick_vol <= 0:
                return np.nan
            wick_buy_vol = float(np.sum(wick_ticks[wick_ticks[:, 3] < 0.5, 2]))
            return (2.0 * wick_buy_vol - wick_vol) / wick_vol

        return _tick_metric(klines, tick_map, calc)


@register_factor
class UpperWickDeltaEffFactor(FactorBase):
    name = "upper_wick_delta_eff"
    requires_ticks = True
    sides = (FACTOR_SIDE_SHORT,)
    group = GROUP_MICROSTRUCTURE

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        def calc(k: Kline, ticks: np.ndarray) -> float:
            body_hi = max(k.open, k.close)
            wick_ticks = ticks[ticks[:, 1] >= body_hi]
            if len(wick_ticks) == 0:
                return np.nan
            wick_vol = float(np.sum(wick_ticks[:, 2]))
            if wick_vol <= 0:
                return np.nan
            wick_buy_vol = float(np.sum(wick_ticks[wick_ticks[:, 3] < 0.5, 2]))
            return (2.0 * wick_buy_vol - wick_vol) / wick_vol

        return _tick_metric(klines, tick_map, calc)


@register_factor
class LowerWickVolumeRatioFactor(FactorBase):
    name = "lower_wick_volume_ratio"
    requires_ticks = True
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_MICROSTRUCTURE

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        def calc(k: Kline, ticks: np.ndarray) -> float:
            body_lo = min(k.open, k.close)
            wick_ticks = ticks[ticks[:, 1] <= body_lo]
            if len(wick_ticks) == 0:
                return np.nan
            wick_vol = float(np.sum(wick_ticks[:, 2]))
            total_vol = float(np.sum(ticks[:, 2]))
            if total_vol <= 0:
                return np.nan
            return wick_vol / total_vol

        return _tick_metric(klines, tick_map, calc)


@register_factor
class UpperWickVolumeRatioFactor(FactorBase):
    name = "upper_wick_volume_ratio"
    requires_ticks = True
    sides = (FACTOR_SIDE_SHORT,)
    group = GROUP_MICROSTRUCTURE

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        def calc(k: Kline, ticks: np.ndarray) -> float:
            body_hi = max(k.open, k.close)
            wick_ticks = ticks[ticks[:, 1] >= body_hi]
            if len(wick_ticks) == 0:
                return np.nan
            wick_vol = float(np.sum(wick_ticks[:, 2]))
            total_vol = float(np.sum(ticks[:, 2]))
            if total_vol <= 0:
                return np.nan
            return wick_vol / total_vol

        return _tick_metric(klines, tick_map, calc)


@register_factor
class BreakoutCumDeltaEffFactor(FactorBase):
    """
    Factor 6: Breakout_Cum_Delta_Eff
    Simplification: Returns kline delta efficiency if it's a potential breakout bar.
    """
    name = "breakout_cum_delta_eff"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        delta = 2.0 * arr["taker_buy_volume"] - arr["volume"]
        return safe_divide(delta, arr["volume"])


@register_factor
class FrictionCoverRatioFactor(FactorBase):
    """
    Factor 7: Friction_Cover_Ratio
    Definition: (Expected Profit) / (Friction Cost)
    Potential Profit = ATR * 2 (as a proxy for RR window)
    Friction Cost = Price * 2 * (fee + slippage)
    """
    name = "friction_cover_ratio"
    sides = FACTOR_SIDES
    group = GROUP_REGIME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        tr = _true_range(arr)
        atr = _rolling_mean(tr, 14)

        # Approximate risk/reward window based on ATR
        potential_profit = atr * 2.0

        # Friction: taker fee (0.032%) + slippage (0.002%)
        fee_rate = 0.00032
        slippage_rate = 0.00002
        friction_cost = arr["close"] * 2.0 * (fee_rate + slippage_rate)

        return safe_divide(potential_profit, friction_cost)
