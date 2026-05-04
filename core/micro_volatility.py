"""Microstructural volatility and fragility scoring."""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite, sqrt
from typing import Any, Optional


_EPS = 1e-12


class RollingStats:
    """O(1) rolling mean/std for finite float values."""

    def __init__(self, maxlen: int) -> None:
        if maxlen <= 0:
            raise ValueError("maxlen must be positive")
        self._values: deque[float] = deque(maxlen=maxlen)
        self._sum = 0.0
        self._sum_sq = 0.0

    def append(self, value: float) -> None:
        value = _finite_or_zero(value)
        if len(self._values) == self._values.maxlen:
            old = self._values[0]
            self._sum -= old
            self._sum_sq -= old * old
        self._values.append(value)
        self._sum += value
        self._sum_sq += value * value

    @property
    def count(self) -> int:
        return len(self._values)

    @property
    def mean(self) -> float:
        return self._sum / len(self._values) if self._values else 0.0

    @property
    def std(self) -> float:
        n = len(self._values)
        if n < 2:
            return 0.0
        variance = max(self._sum_sq / n - self.mean * self.mean, 0.0)
        return sqrt(variance)

    def zscore(self, value: float) -> float:
        std = self.std
        if self.count < 2 or std <= _EPS:
            return 0.0
        return _finite_or_zero((value - self.mean) / std)


@dataclass(frozen=True)
class MicroVolatilityReading:
    micro_fragility_index: float
    spread_variance: float
    depth_depletion: float
    ofi_variance: float
    spread_zscore: float
    depth_depletion_zscore: float
    ofi_zscore: float
    spread: float
    total_depth: float
    ofi: float
    samples: int


class MicroVolatilityEngine:
    """
    Rolling microstructure fragility index.

    ``window_size`` is a count of update events. For 1m bars, the default 15
    means a 15 minute window. For tick or sub-second updates, pass the number
    of samples that should represent the target horizon.
    """

    def __init__(
        self,
        window_size: int = 15,
        normalization_window: int = 100,
        top_n: int = 10,
        weights: tuple[float, float, float] = (0.34, 0.33, 0.33),
    ) -> None:
        if window_size <= 0:
            raise ValueError("window_size must be positive")
        if normalization_window <= 1:
            raise ValueError("normalization_window must be greater than 1")
        if top_n <= 0:
            raise ValueError("top_n must be positive")
        if len(weights) != 3:
            raise ValueError("weights must contain three values")

        self.window_size = window_size
        self.normalization_window = normalization_window
        self.top_n = top_n
        self.weights = weights

        self._spread_stats = RollingStats(window_size)
        self._ofi_stats = RollingStats(window_size)

        self._spread_var_stats = RollingStats(normalization_window)
        self._depth_depletion_stats = RollingStats(normalization_window)
        self._ofi_var_stats = RollingStats(normalization_window)

        self._prev_bid_price: Optional[float] = None
        self._prev_ask_price: Optional[float] = None
        self._prev_bid_depth: Optional[float] = None
        self._prev_ask_depth: Optional[float] = None
        self._prev_total_depth: Optional[float] = None
        self._last_reading = MicroVolatilityReading(
            micro_fragility_index=0.0,
            spread_variance=0.0,
            depth_depletion=0.0,
            ofi_variance=0.0,
            spread_zscore=0.0,
            depth_depletion_zscore=0.0,
            ofi_zscore=0.0,
            spread=0.0,
            total_depth=0.0,
            ofi=0.0,
            samples=0,
        )

    def update(self, orderbook_snapshot: Any, trade_snapshot: Any = None) -> float:
        """
        Update the engine and return the current Micro Fragility Index.

        ``orderbook_snapshot`` may be a dict-like object or an object with
        attributes. Required data can be supplied as best bid/ask plus top-N
        volumes, or as ``bids`` / ``asks`` level sequences.
        """

        bid_price = _extract_float(orderbook_snapshot, "best_bid_price", "best_bid", "bid_price")
        ask_price = _extract_float(orderbook_snapshot, "best_ask_price", "best_ask", "ask_price")
        bid_depth = _extract_depth(orderbook_snapshot, "bid", self.top_n)
        ask_depth = _extract_depth(orderbook_snapshot, "ask", self.top_n)

        if bid_price <= 0.0 or ask_price <= 0.0:
            return self._last_reading.micro_fragility_index

        bid_depth = max(bid_depth, 0.0)
        ask_depth = max(ask_depth, 0.0)
        spread = max(ask_price - bid_price, 0.0)
        total_depth = bid_depth + ask_depth

        taker_buy = _extract_float(trade_snapshot, "taker_buy_volume", "taker_buy_vol", "buy_volume")
        taker_sell = _extract_float(trade_snapshot, "taker_sell_volume", "taker_sell_vol", "sell_volume")
        if taker_sell <= 0.0:
            volume = _extract_float(trade_snapshot, "volume", "total_volume")
            if volume > 0.0:
                taker_sell = max(volume - taker_buy, 0.0)

        if self._prev_total_depth is None:
            depth_depletion = 0.0
        else:
            depth_depletion = max(self._prev_total_depth - total_depth, 0.0)

        ofi = self._compute_ofi(
            bid_price=bid_price,
            ask_price=ask_price,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            taker_buy=taker_buy,
            taker_sell=taker_sell,
        )

        self._spread_stats.append(spread)
        self._ofi_stats.append(ofi)
        spread_variance = self._spread_stats.std
        ofi_variance = self._ofi_stats.std

        spread_z = self._spread_var_stats.zscore(spread_variance)
        depth_z = self._depth_depletion_stats.zscore(depth_depletion)
        ofi_z = self._ofi_var_stats.zscore(ofi_variance)

        w1, w2, w3 = self.weights
        mfi = _finite_or_zero(w1 * spread_z + w2 * depth_z + w3 * ofi_z)

        self._spread_var_stats.append(spread_variance)
        self._depth_depletion_stats.append(depth_depletion)
        self._ofi_var_stats.append(ofi_variance)

        self._prev_bid_price = bid_price
        self._prev_ask_price = ask_price
        self._prev_bid_depth = bid_depth
        self._prev_ask_depth = ask_depth
        self._prev_total_depth = total_depth

        self._last_reading = MicroVolatilityReading(
            micro_fragility_index=mfi,
            spread_variance=spread_variance,
            depth_depletion=depth_depletion,
            ofi_variance=ofi_variance,
            spread_zscore=spread_z,
            depth_depletion_zscore=depth_z,
            ofi_zscore=ofi_z,
            spread=spread,
            total_depth=total_depth,
            ofi=ofi,
            samples=self._spread_stats.count,
        )
        return mfi

    @property
    def last_reading(self) -> MicroVolatilityReading:
        return self._last_reading

    def snapshot(self) -> dict[str, float | int]:
        reading = self._last_reading
        return {
            "micro_fragility_index": reading.micro_fragility_index,
            "spread_variance": reading.spread_variance,
            "depth_depletion": reading.depth_depletion,
            "ofi_variance": reading.ofi_variance,
            "spread_zscore": reading.spread_zscore,
            "depth_depletion_zscore": reading.depth_depletion_zscore,
            "ofi_zscore": reading.ofi_zscore,
            "spread": reading.spread,
            "total_depth": reading.total_depth,
            "ofi": reading.ofi,
            "samples": reading.samples,
        }

    def _compute_ofi(
        self,
        *,
        bid_price: float,
        ask_price: float,
        bid_depth: float,
        ask_depth: float,
        taker_buy: float,
        taker_sell: float,
    ) -> float:
        if self._prev_bid_price is None or self._prev_ask_price is None:
            book_ofi = 0.0
        else:
            prev_bid_depth = self._prev_bid_depth or 0.0
            prev_ask_depth = self._prev_ask_depth or 0.0

            if bid_price > self._prev_bid_price:
                bid_flow = bid_depth
            elif bid_price == self._prev_bid_price:
                bid_flow = bid_depth - prev_bid_depth
            else:
                bid_flow = -prev_bid_depth

            if ask_price < self._prev_ask_price:
                ask_flow = -ask_depth
            elif ask_price == self._prev_ask_price:
                ask_flow = prev_ask_depth - ask_depth
            else:
                ask_flow = prev_ask_depth

            book_ofi = bid_flow + ask_flow

        trade_ofi = max(taker_buy, 0.0) - max(taker_sell, 0.0)
        return _finite_or_zero(book_ofi + trade_ofi)


def _finite_or_zero(value: float) -> float:
    value = float(value)
    return value if isfinite(value) else 0.0


def _extract_float(source: Any, *keys: str) -> float:
    if source is None:
        return 0.0
    for key in keys:
        value = _get_value(source, key)
        if value is not None:
            try:
                return _finite_or_zero(float(value))
            except (TypeError, ValueError):
                continue
    return 0.0


def _extract_depth(source: Any, side: str, top_n: int) -> float:
    candidates = (
        f"{side}s_volume_top_N",
        f"{side}s_volume_top_{top_n}",
        f"{side}_volume_top_N",
        f"{side}_volume_top_{top_n}",
        f"{side}_depth_l{top_n}",
        f"{side}s_depth_l{top_n}",
        f"{side}s_volume",
        f"{side}_volume",
    )
    value = _extract_float(source, *candidates)
    if value > 0.0:
        return value

    levels = _get_value(source, f"{side}s")
    if levels is None:
        return 0.0
    return _sum_level_qty(levels, top_n)


def _sum_level_qty(levels: Any, top_n: int) -> float:
    total = 0.0
    if not isinstance(levels, Sequence):
        return 0.0
    for level in levels[:top_n]:
        qty = None
        if isinstance(level, Mapping):
            qty = level.get("qty", level.get("quantity", level.get("volume")))
        elif isinstance(level, Sequence) and not isinstance(level, (str, bytes)) and len(level) >= 2:
            qty = level[1]
        if qty is None:
            continue
        try:
            total += max(float(qty), 0.0)
        except (TypeError, ValueError):
            continue
    return _finite_or_zero(total)


def _get_value(source: Any, key: str) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        if key in source:
            return source[key]
        lower_key = key.lower()
        for existing_key, value in source.items():
            if isinstance(existing_key, str) and existing_key.lower() == lower_key:
                return value
        return None
    return getattr(source, key, None)
