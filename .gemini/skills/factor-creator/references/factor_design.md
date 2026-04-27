# Factor Design and Classification

All factors must be implemented in the `research` module and inherit from `FactorBase`.

## Classification

Factors must specify `sides` and `group` attributes.

### Sides

Determine the intended trade direction based on the logic:
- `FACTOR_SIDES`: Bidirectional signals (default).
- `(FACTOR_SIDE_LONG,)`: Signal is specifically for long entries (e.g. Lower Wick Rejection).
- `(FACTOR_SIDE_SHORT,)`: Signal is specifically for short entries.

### Groups

Assign the factor to one of the following constants from `research.base`:
- `GROUP_MICROSTRUCTURE`: Order Flow and Tick-level data factors.
- `GROUP_REGIME`: Regime, Condition, and market structure filters (default).
- `GROUP_VOLUME`: Volume and Liquidity factors.
- `GROUP_MOMENTUM`: Momentum and Trend factors.
- `GROUP_MEAN_REVERSION`: Mean-Reversion and Extreme factors.
- `GROUP_VOLATILITY`: Volatility and Compression factors.
- `GROUP_CRYPTO_DERIVATIVES`: Crypto-specific derivative data (e.g., funding rates).

## Helper Functions

- `klines_to_arrays(klines)`: Converts a list of Kline objects to aligned `dict[str, np.ndarray]`. Keys include `open_time`, `close_time`, `open`, `high`, `low`, `close`, `volume`, `taker_buy_volume`.
- `safe_divide(num, den)`: Use this for array division to gracefully handle division by zero (returns `np.nan`).
- `_tick_metric(klines, tick_map, fn)`: Helper to iterate over Klines and their Ticks efficiently.

## Factor Templates

### Bar-level Factor

```python
from research.base import (
    FactorBase, klines_to_arrays, safe_divide,
    FACTOR_SIDE_LONG, FACTOR_SIDE_SHORT, FACTOR_SIDES,
    GROUP_MOMENTUM
)
from research.registry import register_factor
import numpy as np

@register_factor
class MyCustomFactor(FactorBase):
    name = "my_custom_factor"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        # Compute logic returning np.ndarray of shape arr["close"].shape
        # Example: return arr["close"] / arr["open"] - 1
        return safe_divide(arr["close"] - arr["open"], arr["open"])
```

### Tick-level Factor

```python
from research.base import (
    FactorBase, FACTOR_SIDES, GROUP_MICROSTRUCTURE
)
from research.registry import register_factor
from research.factors import _tick_metric
import numpy as np

@register_factor
class MyTickFactor(FactorBase):
    name = "my_tick_factor"
    requires_ticks = True
    sides = FACTOR_SIDES
    group = GROUP_MICROSTRUCTURE

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        def calc(k: Kline, ticks: np.ndarray) -> float:
            # ticks columns: [timestamp, price, size, side]
            # Custom tick aggregation
            if len(ticks) == 0:
                return np.nan
            return float(np.sum(ticks[:, 2])) # Example: Total Volume
            
        return _tick_metric(klines, tick_map, calc)
```
