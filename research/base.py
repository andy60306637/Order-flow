from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np

from core.data_types import Kline
from strategies.base import TickBarMap


FACTOR_SIDE_LONG = "long"
FACTOR_SIDE_SHORT = "short"
FACTOR_SIDES = (FACTOR_SIDE_LONG, FACTOR_SIDE_SHORT)
FACTOR_SIDE_LABELS = {
    FACTOR_SIDE_LONG: "Long",
    FACTOR_SIDE_SHORT: "Short",
}

GROUP_MICROSTRUCTURE = "Micro-structure & Order Flow Factors"
GROUP_REGIME = "Regime & Condition Filters"
GROUP_VOLUME = "Volume & Liquidity Factors"
GROUP_MOMENTUM = "Momentum & Trend Factors"
GROUP_MEAN_REVERSION = "Mean-Reversion & Extreme Factors"
GROUP_VOLATILITY = "Volatility & Compression Factors"
GROUP_CRYPTO_DERIVATIVES = "Crypto Derivatives & Alternative Factors"
FACTOR_GROUPS = (
    GROUP_MICROSTRUCTURE,
    GROUP_REGIME,
    GROUP_VOLUME,
    GROUP_MOMENTUM,
    GROUP_MEAN_REVERSION,
    GROUP_VOLATILITY,
    GROUP_CRYPTO_DERIVATIVES,
)


def factor_sides_label(sides: tuple[str, ...]) -> str:
    labels = [FACTOR_SIDE_LABELS.get(side, side) for side in sides]
    return "/".join(labels) if labels else ""


class FactorBase(ABC):
    """Base interface for vectorized research factors."""

    name: str = "Unnamed"
    requires_ticks: bool = False
    sides: tuple[str, ...] = FACTOR_SIDES
    group: str = GROUP_REGIME

    @abstractmethod
    def compute(
        self,
        klines: list[Kline],
        tick_map: TickBarMap | None = None,
    ) -> np.ndarray:
        """Return one float value per kline. Missing/unusable values must be NaN."""


def klines_to_arrays(klines: list[Kline]) -> dict[str, np.ndarray]:
    """Convert Kline objects to aligned numeric arrays."""
    return {
        "open_time": np.array([k.open_time for k in klines], dtype=np.int64),
        "close_time": np.array([k.close_time for k in klines], dtype=np.int64),
        "open": np.array([k.open for k in klines], dtype=np.float64),
        "high": np.array([k.high for k in klines], dtype=np.float64),
        "low": np.array([k.low for k in klines], dtype=np.float64),
        "close": np.array([k.close for k in klines], dtype=np.float64),
        "volume": np.array([k.volume for k in klines], dtype=np.float64),
        "taker_buy_volume": np.array([k.taker_buy_volume for k in klines], dtype=np.float64),
    }


def safe_divide(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    out = np.full_like(num, np.nan, dtype=np.float64)
    np.divide(num, den, out=out, where=den != 0)
    return out
