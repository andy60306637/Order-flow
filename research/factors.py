from __future__ import annotations

import numpy as np

from core import market_data_cache
from core.data_types import Kline
from research.base import (
    FACTOR_SIDE_LONG,
    FACTOR_SIDE_SHORT,
    FACTOR_SIDES,
    GROUP_CRYPTO_DERIVATIVES,
    GROUP_MEAN_REVERSION,
    GROUP_MICROSTRUCTURE,
    GROUP_MOMENTUM,
    GROUP_PRICE_ACTION,
    GROUP_REGIME,
    GROUP_VOLATILITY,
    GROUP_VOLUME,
    FactorBase,
    klines_to_arrays,
    safe_divide,
)
from research.registry import register_factor
from strategies.base import TickBarMap


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=np.float64)
    if len(values) < window or window <= 0:
        return out
    from numpy.lib.stride_tricks import sliding_window_view
    v = sliding_window_view(values, window)
    all_valid = np.all(np.isfinite(v), axis=1)
    if all_valid.any():
        out[window - 1:][all_valid] = np.mean(v[all_valid], axis=1)
    return out


def _rolling_std(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=np.float64)
    if len(values) < window or window <= 0:
        return out
    from numpy.lib.stride_tricks import sliding_window_view
    v = sliding_window_view(values, window)
    all_valid = np.all(np.isfinite(v), axis=1)
    if all_valid.any():
        out[window - 1:][all_valid] = np.std(v[all_valid], axis=1, ddof=0)
    return out


def _rolling_min(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=np.float64)
    if len(values) < window or window <= 0:
        return out
    from numpy.lib.stride_tricks import sliding_window_view
    v = sliding_window_view(values, window)
    out[window - 1:] = np.min(v, axis=1)
    return out


def _rolling_max(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=np.float64)
    if len(values) < window or window <= 0:
        return out
    from numpy.lib.stride_tricks import sliding_window_view
    v = sliding_window_view(values, window)
    out[window - 1:] = np.max(v, axis=1)
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


def _ema(values: np.ndarray, span: int) -> np.ndarray:
    """EMA with alpha = 2/(span+1). Skips NaN inputs, does not carry forward."""
    alpha = 2.0 / (span + 1)
    out = np.full(len(values), np.nan, dtype=np.float64)
    prev = np.nan
    for i, v in enumerate(values):
        if np.isnan(v):
            continue
        prev = v if np.isnan(prev) else alpha * v + (1.0 - alpha) * prev
        out[i] = prev
    return out


def _wilder_smooth(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing: seed = SMA(period), then out[i] = (out[i-1]*(n-1) + v[i]) / n."""
    out = np.full(len(values), np.nan, dtype=np.float64)
    if len(values) < period:
        return out
    valid_mask = ~np.isnan(values)
    valid_idx = np.where(valid_mask)[0]
    if len(valid_idx) < period:
        return out
    i0 = valid_idx[period - 1]
    out[i0] = np.mean(values[valid_idx[:period]])
    for i in range(i0 + 1, len(values)):
        if valid_mask[i]:
            out[i] = (out[i - 1] * (period - 1) + values[i]) / period
        else:
            out[i] = out[i - 1]
    return out


def _rolling_zscore(values: np.ndarray, window: int) -> np.ndarray:
    mean = _rolling_mean(values, window)
    std = _rolling_std(values, window)
    return safe_divide(values - mean, std)


def _rolling_percentile(values: np.ndarray, window: int) -> np.ndarray:
    """Rank of current value within its trailing window [0, 1]."""
    out = np.full(len(values), np.nan, dtype=np.float64)
    for i in range(window - 1, len(values)):
        w = values[i - window + 1: i + 1]
        valid = w[~np.isnan(w)]
        if len(valid) < 2:
            continue
        out[i] = np.sum(valid[:-1] < valid[-1]) / (len(valid) - 1)
    return out


def _rolling_vwap(arr: dict[str, np.ndarray], window: int) -> np.ndarray:
    """Rolling VWAP using typical price = (H+L+C)/3."""
    tp = (arr["high"] + arr["low"] + arr["close"]) / 3.0
    vol = arr["volume"]
    tp_vol = tp * vol
    num = _rolling_mean(tp_vol, window) * window
    den = _rolling_mean(vol, window) * window
    return safe_divide(num, den)


def _atr(arr: dict[str, np.ndarray], period: int = 14) -> np.ndarray:
    tr = _true_range(arr)
    return _wilder_smooth(tr, period)


def _bars_for_minutes(open_times: np.ndarray, minutes: int, default_interval_ms: int = 60_000) -> int:
    if len(open_times) >= 2:
        diffs = np.diff(open_times)
        diffs = diffs[diffs > 0]
        if len(diffs):
            interval_ms = int(np.median(diffs))
        else:
            interval_ms = default_interval_ms
    else:
        interval_ms = default_interval_ms
    return max(1, int(round(minutes * 60_000 / interval_ms)))


def _bars_for_days(open_times: np.ndarray, days: int) -> int:
    return _bars_for_minutes(open_times, days * 24 * 60)


def _aligned_market_column(
    klines: list[Kline],
    kind: str,
    value_column: str,
    *,
    mode: str = "ffill",
    default: float = np.nan,
) -> np.ndarray:
    if not klines:
        return np.empty(0, dtype=np.float64)
    arr = klines_to_arrays(klines)
    symbol = klines[0].symbol
    return market_data_cache.align_cache_column(
        kind,
        symbol,
        arr["open_time"],
        value_column,
        mode=mode,
        default=default,
    )


def _liquidation_column(klines: list[Kline], value_column: str) -> np.ndarray:
    if not klines:
        return np.empty(0, dtype=np.float64)
    arr = klines_to_arrays(klines)
    _, manifest = market_data_cache.load_cache("liquidationSnapshot", klines[0].symbol)
    if manifest and int(manifest.get("row_count", 0)) == 0 and manifest.get("availability_note"):
        return np.full(len(klines), np.nan, dtype=np.float64)
    return market_data_cache.align_cache_column(
        "liquidationSnapshot",
        klines[0].symbol,
        arr["open_time"],
        value_column,
        mode="exact",
        default=0.0,
    )


def _delta_n(values: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(values), np.nan, dtype=np.float64)
    if n <= 0 or len(values) <= n:
        return out
    out[n:] = values[n:] - values[:-n]
    return out


# ---------------------------------------------------------------------------
# Group 1 · Micro-structure & Order Flow
# Wick and Delta Efficiency Factors
# ---------------------------------------------------------------------------

@register_factor
class LowerWickToBodyRatioFactor(FactorBase):
    name = "lower_wick_to_body_ratio"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        body_lo = np.minimum(arr["open"], arr["close"])
        body = np.abs(arr["close"] - arr["open"])
        return safe_divide(body_lo - arr["low"], body)


@register_factor
class UpperWickToBodyRatioFactor(FactorBase):
    name = "upper_wick_to_body_ratio"
    sides = (FACTOR_SIDE_SHORT,)
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        body_hi = np.maximum(arr["open"], arr["close"])
        body = np.abs(arr["close"] - arr["open"])
        return safe_divide(arr["high"] - body_hi, body)


@register_factor
class LowerWickDeltaEfficiencyFactor(FactorBase):
    name = "lower_wick_delta_eff"
    requires_ticks = True
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_MICROSTRUCTURE

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        out = np.full(len(klines), np.nan, dtype=np.float64)
        if tick_map is None:
            return out
        for idx, kline in enumerate(klines):
            ticks = tick_map.get(kline.open_time)
            if ticks is None or len(ticks) == 0:
                continue
            body_lo = min(kline.open, kline.close)
            zone = ticks[ticks[:, 1] <= body_lo]
            if len(zone) == 0:
                continue
            qty = zone[:, 2]
            buy_qty = float(np.sum(qty[zone[:, 3] == 0.0]))
            total_qty = float(np.sum(qty))
            if total_qty > 0:
                out[idx] = (2.0 * buy_qty - total_qty) / total_qty
        return out


@register_factor
class DeltaEfficiencyLongFactor(FactorBase):
    name = "delta_eff_long"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_MICROSTRUCTURE

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        return safe_divide(2.0 * arr["taker_buy_volume"] - arr["volume"], arr["volume"])


@register_factor
class DeltaEfficiencyShortFactor(FactorBase):
    name = "delta_eff_short"
    sides = (FACTOR_SIDE_SHORT,)
    group = GROUP_MICROSTRUCTURE

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        return -safe_divide(2.0 * arr["taker_buy_volume"] - arr["volume"], arr["volume"])


# ---------------------------------------------------------------------------
# Crypto Derivatives & Alternative Factors
# ---------------------------------------------------------------------------

@register_factor
class FundingRateFactor(FactorBase):
    name = "funding_rate"
    sides = FACTOR_SIDES
    group = GROUP_CRYPTO_DERIVATIVES

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _aligned_market_column(klines, "fundingRate", "last_funding_rate")


@register_factor
class FundingRateChangeFactor(FactorBase):
    name = "funding_rate_change"
    sides = FACTOR_SIDES
    group = GROUP_CRYPTO_DERIVATIVES

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        funding = _aligned_market_column(klines, "fundingRate", "last_funding_rate")
        return _delta_n(funding, 1)


@register_factor
class FundingRateZscore30dFactor(FactorBase):
    name = "funding_rate_zscore_30d"
    sides = FACTOR_SIDES
    group = GROUP_CRYPTO_DERIVATIVES

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        funding = _aligned_market_column(klines, "fundingRate", "last_funding_rate")
        return _rolling_zscore(funding, _bars_for_days(arr["open_time"], 30))


@register_factor
class OpenInterestFactor(FactorBase):
    name = "open_interest"
    sides = FACTOR_SIDES
    group = GROUP_CRYPTO_DERIVATIVES

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _aligned_market_column(klines, "metrics", "sum_open_interest")


@register_factor
class OpenInterestDelta5mFactor(FactorBase):
    name = "open_interest_delta_5m"
    sides = FACTOR_SIDES
    group = GROUP_CRYPTO_DERIVATIVES

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        oi = _aligned_market_column(klines, "metrics", "sum_open_interest")
        return _delta_n(oi, _bars_for_minutes(arr["open_time"], 5))


@register_factor
class OpenInterestDelta15mFactor(FactorBase):
    name = "open_interest_delta_15m"
    sides = FACTOR_SIDES
    group = GROUP_CRYPTO_DERIVATIVES

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        oi = _aligned_market_column(klines, "metrics", "sum_open_interest")
        return _delta_n(oi, _bars_for_minutes(arr["open_time"], 15))


@register_factor
class OpenInterestDeltaRatio15mFactor(FactorBase):
    name = "open_interest_delta_ratio_15m"
    sides = FACTOR_SIDES
    group = GROUP_CRYPTO_DERIVATIVES

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        oi = _aligned_market_column(klines, "metrics", "sum_open_interest")
        n = _bars_for_minutes(arr["open_time"], 15)
        prev = np.roll(oi, n)
        prev[:n] = np.nan
        return safe_divide(oi - prev, prev)


@register_factor
class OpenInterestZscore30dFactor(FactorBase):
    name = "open_interest_zscore_30d"
    sides = FACTOR_SIDES
    group = GROUP_CRYPTO_DERIVATIVES

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        oi = _aligned_market_column(klines, "metrics", "sum_open_interest")
        return _rolling_zscore(oi, _bars_for_days(arr["open_time"], 30))


@register_factor
class LongLiquidationVolume1mFactor(FactorBase):
    name = "long_liquidation_volume_1m"
    sides = (FACTOR_SIDE_SHORT,)
    group = GROUP_CRYPTO_DERIVATIVES

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _liquidation_column(klines, "long_liq_notional")


@register_factor
class ShortLiquidationVolume1mFactor(FactorBase):
    name = "short_liquidation_volume_1m"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_CRYPTO_DERIVATIVES

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _liquidation_column(klines, "short_liq_notional")


@register_factor
class LiquidationImbalance1mFactor(FactorBase):
    name = "liq_imbalance_1m"
    sides = FACTOR_SIDES
    group = GROUP_CRYPTO_DERIVATIVES

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        short_liq = _liquidation_column(klines, "short_liq_notional")
        long_liq = _liquidation_column(klines, "long_liq_notional")
        return short_liq - long_liq


# ---------------------------------------------------------------------------
# Group 1 Micro-structure & Order Flow
# (Requires only Kline taker_buy_volume + volume)
# ---------------------------------------------------------------------------

@register_factor
class BuyTradeVolumeFactor(FactorBase):
    name = "buy_trade_volume_1m"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_MICROSTRUCTURE

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return klines_to_arrays(klines)["taker_buy_volume"]


@register_factor
class SellTradeVolumeFactor(FactorBase):
    name = "sell_trade_volume_1m"
    sides = (FACTOR_SIDE_SHORT,)
    group = GROUP_MICROSTRUCTURE

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        return arr["volume"] - arr["taker_buy_volume"]


@register_factor
class TradeVolumeDeltaFactor(FactorBase):
    name = "trade_volume_delta_1m"
    sides = FACTOR_SIDES
    group = GROUP_MICROSTRUCTURE

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        return 2.0 * arr["taker_buy_volume"] - arr["volume"]


@register_factor
class TradeVolumeDeltaRatioFactor(FactorBase):
    name = "trade_volume_delta_ratio_1m"
    sides = FACTOR_SIDES
    group = GROUP_MICROSTRUCTURE

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        delta = 2.0 * arr["taker_buy_volume"] - arr["volume"]
        return safe_divide(delta, arr["volume"])


@register_factor
class TakerBuyRatioFactor(FactorBase):
    name = "taker_buy_ratio_1m"
    sides = FACTOR_SIDES
    group = GROUP_MICROSTRUCTURE

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        return safe_divide(arr["taker_buy_volume"], arr["volume"])


# ---------------------------------------------------------------------------
# Group 2 · Regime & Condition Filters
# ---------------------------------------------------------------------------

@register_factor
class AdxFactor(FactorBase):
    name = "adx_15m"
    sides = FACTOR_SIDES
    group = GROUP_REGIME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        high, low, close = arr["high"], arr["low"], arr["close"]
        n = len(high)
        period = 14

        prev_close = np.roll(close, 1); prev_close[0] = close[0]
        prev_high  = np.roll(high, 1);  prev_high[0]  = high[0]
        prev_low   = np.roll(low, 1);   prev_low[0]   = low[0]

        tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
        up_move   = high - prev_high
        down_move = prev_low - low
        plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        plus_dm[0] = minus_dm[0] = tr[0] = 0.0

        atr14      = _wilder_smooth(tr, period)
        plus_dm14  = _wilder_smooth(plus_dm, period)
        minus_dm14 = _wilder_smooth(minus_dm, period)

        plus_di  = safe_divide(100.0 * plus_dm14, atr14)
        minus_di = safe_divide(100.0 * minus_dm14, atr14)
        di_sum   = plus_di + minus_di
        dx = safe_divide(100.0 * np.abs(plus_di - minus_di), di_sum)
        return _wilder_smooth(dx, period)


@register_factor
class ChoppinessFactor(FactorBase):
    name = "chop_index_15m"
    sides = FACTOR_SIDES
    group = GROUP_REGIME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        n = 14
        tr = _true_range(arr)

        # sum of 1-bar TR over window
        tr_sum = _rolling_mean(tr, n) * n
        h_n = _rolling_max(arr["high"], n)
        l_n = _rolling_min(arr["low"], n)
        rng = h_n - l_n
        ratio = safe_divide(tr_sum, rng)
        out = np.full(len(tr), np.nan, dtype=np.float64)
        valid = ratio > 0
        out[valid] = 100.0 * np.log10(ratio[valid]) / np.log10(n)
        return out


@register_factor
class RangePositionFactor(FactorBase):
    name = "range_position_15m"
    sides = FACTOR_SIDES
    group = GROUP_REGIME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        n = 20
        h = _rolling_max(arr["high"], n)
        l = _rolling_min(arr["low"], n)
        return safe_divide(arr["close"] - l, h - l)


@register_factor
class HhHlStructureFactor(FactorBase):
    name = "hh_hl_structure_15m"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_REGIME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        high, low = arr["high"], arr["low"]
        hh = (high[1:] > high[:-1]).astype(np.float64)
        hl = (low[1:] > low[:-1]).astype(np.float64)
        out = np.full(len(high), np.nan, dtype=np.float64)
        out[1:] = np.where((hh == 1) & (hl == 1), 1.0, np.nan)
        return out


@register_factor
class LlLhStructureFactor(FactorBase):
    name = "ll_lh_structure_15m"
    sides = (FACTOR_SIDE_SHORT,)
    group = GROUP_REGIME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        high, low = arr["high"], arr["low"]
        ll = (low[1:] < low[:-1]).astype(np.float64)
        lh = (high[1:] < high[:-1]).astype(np.float64)
        out = np.full(len(high), np.nan, dtype=np.float64)
        out[1:] = np.where((ll == 1) & (lh == 1), 1.0, np.nan)
        return out


@register_factor
class VolumeZscoreRegimeFactor(FactorBase):
    name = "volume_zscore_15m_20"
    sides = FACTOR_SIDES
    group = GROUP_REGIME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        return _rolling_zscore(arr["volume"], 20)


@register_factor
class VolatilityZscoreRegimeFactor(FactorBase):
    name = "volatility_zscore_15m_20"
    sides = FACTOR_SIDES
    group = GROUP_REGIME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        close = arr["close"]
        prev_close = np.roll(close, 1); prev_close[0] = close[0]
        log_ret = np.where(prev_close > 0, np.log(close / prev_close), 0.0)
        vol5 = _rolling_std(log_ret, 5)
        return _rolling_zscore(vol5, 20)


@register_factor
class SessionAsiaFlagFactor(FactorBase):
    name = "session_asia_flag"
    sides = FACTOR_SIDES
    group = GROUP_REGIME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        hours = (arr["open_time"] // 3_600_000) % 24
        return np.where((hours >= 0) & (hours < 8), 1.0, 0.0)


@register_factor
class SessionLondonFlagFactor(FactorBase):
    name = "session_london_flag"
    sides = FACTOR_SIDES
    group = GROUP_REGIME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        hours = (arr["open_time"] // 3_600_000) % 24
        return np.where((hours >= 7) & (hours < 16), 1.0, 0.0)


@register_factor
class SessionUsFlagFactor(FactorBase):
    name = "session_us_flag"
    sides = FACTOR_SIDES
    group = GROUP_REGIME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        hours = (arr["open_time"] // 3_600_000) % 24
        return np.where((hours >= 13) & (hours < 22), 1.0, 0.0)


@register_factor
class WeekendFlagFactor(FactorBase):
    name = "weekend_flag"
    sides = FACTOR_SIDES
    group = GROUP_REGIME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        import datetime
        arr = klines_to_arrays(klines)
        out = np.zeros(len(klines), dtype=np.float64)
        for i, ts in enumerate(arr["open_time"]):
            dow = datetime.datetime.utcfromtimestamp(ts / 1000).weekday()
            out[i] = 1.0 if dow >= 5 else 0.0
        return out


# ---------------------------------------------------------------------------
# Group 3 · Volume & Liquidity
# ---------------------------------------------------------------------------

@register_factor
class VolumeFactor(FactorBase):
    name = "volume_1m"
    sides = FACTOR_SIDES
    group = GROUP_VOLUME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return klines_to_arrays(klines)["volume"]


@register_factor
class VolumeMa20Factor(FactorBase):
    name = "volume_ma_20"
    sides = FACTOR_SIDES
    group = GROUP_VOLUME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _rolling_mean(klines_to_arrays(klines)["volume"], 20)


@register_factor
class VolumeZscore20Factor(FactorBase):
    name = "volume_zscore_20"
    sides = FACTOR_SIDES
    group = GROUP_VOLUME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _rolling_zscore(klines_to_arrays(klines)["volume"], 20)


@register_factor
class VolumeRatio20Factor(FactorBase):
    name = "volume_ratio_20"
    sides = FACTOR_SIDES
    group = GROUP_VOLUME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        vol = arr["volume"]
        ma = _rolling_mean(vol, 20)
        return safe_divide(vol, ma)


@register_factor
class VolumeChangeFactor(FactorBase):
    name = "volume_change_1m"
    sides = FACTOR_SIDES
    group = GROUP_VOLUME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        vol = klines_to_arrays(klines)["volume"]
        prev = np.roll(vol, 1); prev[0] = np.nan
        return safe_divide(vol - prev, prev)


@register_factor
class BuyVolumeZscore20Factor(FactorBase):
    name = "buy_volume_zscore_20"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_VOLUME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _rolling_zscore(klines_to_arrays(klines)["taker_buy_volume"], 20)


@register_factor
class SellVolumeZscore20Factor(FactorBase):
    name = "sell_volume_zscore_20"
    sides = (FACTOR_SIDE_SHORT,)
    group = GROUP_VOLUME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        sell_vol = arr["volume"] - arr["taker_buy_volume"]
        return _rolling_zscore(sell_vol, 20)


@register_factor
class AmihudIlliquidityFactor(FactorBase):
    name = "amihud_illiquidity_1m"
    sides = FACTOR_SIDES
    group = GROUP_VOLUME

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        close = arr["close"]
        prev_close = np.roll(close, 1); prev_close[0] = close[0]
        ret = safe_divide(close - prev_close, prev_close)
        return safe_divide(np.abs(ret), arr["volume"])


# ---------------------------------------------------------------------------
# Group 4 · Momentum & Trend
# ---------------------------------------------------------------------------

def _return_n(close: np.ndarray, n: int) -> np.ndarray:
    prev = np.roll(close, n)
    prev[:n] = np.nan
    return safe_divide(close - prev, prev)


@register_factor
class Return1mFactor(FactorBase):
    name = "return_1m"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        return _return_n(arr["close"], 1)


@register_factor
class Return3mFactor(FactorBase):
    name = "return_3m"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _return_n(klines_to_arrays(klines)["close"], 3)


@register_factor
class Return5mFactor(FactorBase):
    name = "return_5m"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _return_n(klines_to_arrays(klines)["close"], 5)


@register_factor
class Return10mFactor(FactorBase):
    name = "return_10m"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _return_n(klines_to_arrays(klines)["close"], 10)


@register_factor
class Return15mFactor(FactorBase):
    name = "return_15m"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _return_n(klines_to_arrays(klines)["close"], 15)


@register_factor
class LogReturn1mFactor(FactorBase):
    name = "log_return_1m"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        close = klines_to_arrays(klines)["close"]
        prev = np.roll(close, 1)
        out = np.full(len(close), np.nan, dtype=np.float64)
        valid = (prev > 0) & (close > 0)
        valid[0] = False
        out[valid] = np.log(close[valid] / prev[valid])
        return out


@register_factor
class NormalizedReturn5mFactor(FactorBase):
    name = "normalized_return_5m"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        close = klines_to_arrays(klines)["close"]
        ret5 = _return_n(close, 5)
        vol5 = _realized_vol(close, 5)
        return safe_divide(ret5, vol5)


@register_factor
class Ma5Factor(FactorBase):
    name = "ma_5_1m"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _rolling_mean(klines_to_arrays(klines)["close"], 5)


@register_factor
class Ma20Factor(FactorBase):
    name = "ma_20_1m"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _rolling_mean(klines_to_arrays(klines)["close"], 20)


@register_factor
class Ma60Factor(FactorBase):
    name = "ma_60_1m"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _rolling_mean(klines_to_arrays(klines)["close"], 60)


@register_factor
class PriceMaGap20Factor(FactorBase):
    name = "price_ma_gap_20"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        close = klines_to_arrays(klines)["close"]
        ma20 = _rolling_mean(close, 20)
        return safe_divide(close - ma20, ma20)


@register_factor
class MaSlope20Factor(FactorBase):
    name = "ma_slope_20"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        close = klines_to_arrays(klines)["close"]
        ma = _rolling_mean(close, 20)
        prev = np.roll(ma, 1); prev[0] = np.nan
        return safe_divide(ma - prev, prev)


@register_factor
class MaSlope60Factor(FactorBase):
    name = "ma_slope_60"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        close = klines_to_arrays(klines)["close"]
        ma = _rolling_mean(close, 60)
        prev = np.roll(ma, 1); prev[0] = np.nan
        return safe_divide(ma - prev, prev)


@register_factor
class EmaCross5_20Factor(FactorBase):
    name = "ema_cross_5_20"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        close = klines_to_arrays(klines)["close"]
        ema5 = _ema(close, 5)
        ema20 = _ema(close, 20)
        diff = ema5 - ema20
        # Normalize by price level
        return safe_divide(diff, close)


@register_factor
class TrendStrengthMaFactor(FactorBase):
    name = "trend_strength_ma"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        close = klines_to_arrays(klines)["close"]
        ma20 = _rolling_mean(close, 20)
        prev_ma = np.roll(ma20, 1); prev_ma[0] = np.nan
        slope = safe_divide(ma20 - prev_ma, prev_ma)
        vol20 = _realized_vol(close, 20)
        return safe_divide(np.abs(slope), vol20)


@register_factor
class BreakoutHigh20Factor(FactorBase):
    name = "breakout_high_20"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        # compare close to max of previous 20 bars (not including current)
        prev_high = np.roll(arr["high"], 1)
        prev_high[0] = arr["high"][0]
        max20 = _rolling_max(prev_high, 20)
        out = np.where(arr["close"] > max20, 1.0, np.nan)
        out[:20] = np.nan
        return out


@register_factor
class BreakoutLow20Factor(FactorBase):
    name = "breakout_low_20"
    sides = (FACTOR_SIDE_SHORT,)
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        prev_low = np.roll(arr["low"], 1)
        prev_low[0] = arr["low"][0]
        min20 = _rolling_min(prev_low, 20)
        out = np.where(arr["close"] < min20, 1.0, np.nan)
        out[:20] = np.nan
        return out


@register_factor
class DistanceToHigh20Factor(FactorBase):
    name = "distance_to_high_20"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        h20 = _rolling_max(arr["high"], 20)
        return safe_divide(arr["close"] - h20, h20)


@register_factor
class DistanceToLow20Factor(FactorBase):
    name = "distance_to_low_20"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        l20 = _rolling_min(arr["low"], 20)
        return safe_divide(arr["close"] - l20, l20)


@register_factor
class DonchianPosition20Factor(FactorBase):
    name = "donchian_position_20"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        h20 = _rolling_max(arr["high"], 20)
        l20 = _rolling_min(arr["low"], 20)
        return safe_divide(arr["close"] - l20, h20 - l20)


@register_factor
class BreakoutVolumeConfirmFactor(FactorBase):
    name = "breakout_volume_confirm"
    sides = FACTOR_SIDES
    group = GROUP_MOMENTUM

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        close = arr["close"]
        prev_high = np.roll(arr["high"], 1); prev_high[0] = arr["high"][0]
        prev_low  = np.roll(arr["low"], 1);  prev_low[0]  = arr["low"][0]
        max20 = _rolling_max(prev_high, 20)
        min20 = _rolling_min(prev_low, 20)
        vol_z = _rolling_zscore(arr["volume"], 20)
        breakout = np.where(close > max20, 1.0, np.where(close < min20, -1.0, 0.0))
        out = breakout * vol_z
        out[:20] = np.nan
        return out


# ---------------------------------------------------------------------------
# Group 5 · Mean-Reversion & Extreme
# ---------------------------------------------------------------------------

@register_factor
class ZscorePrice20Factor(FactorBase):
    name = "zscore_price_20"
    sides = FACTOR_SIDES
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        close = klines_to_arrays(klines)["close"]
        return _rolling_zscore(close, 20)


@register_factor
class ZscoreReturn20Factor(FactorBase):
    name = "zscore_return_20"
    sides = FACTOR_SIDES
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        close = klines_to_arrays(klines)["close"]
        ret1 = _return_n(close, 1)
        return _rolling_zscore(ret1, 20)


@register_factor
class BollingerPosition20Factor(FactorBase):
    name = "bollinger_position_20"
    sides = FACTOR_SIDES
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        close = klines_to_arrays(klines)["close"]
        ma = _rolling_mean(close, 20)
        std = _rolling_std(close, 20)
        upper = ma + 2.0 * std
        lower = ma - 2.0 * std
        return safe_divide(close - lower, upper - lower)


@register_factor
class BollingerWidth20Factor(FactorBase):
    name = "bollinger_width_20"
    sides = FACTOR_SIDES
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        close = klines_to_arrays(klines)["close"]
        ma = _rolling_mean(close, 20)
        std = _rolling_std(close, 20)
        width = 4.0 * std
        return safe_divide(width, ma)


@register_factor
class Rsi14Factor(FactorBase):
    name = "rsi_14"
    sides = FACTOR_SIDES
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        close = klines_to_arrays(klines)["close"]
        delta = np.diff(close, prepend=close[0])
        delta[0] = 0.0
        gains  = np.where(delta > 0, delta, 0.0)
        losses = np.where(delta < 0, -delta, 0.0)
        avg_gain = _wilder_smooth(gains, 14)
        avg_loss = _wilder_smooth(losses, 14)
        rs = safe_divide(avg_gain, avg_loss)
        rsi = 100.0 - safe_divide(np.full_like(rs, 100.0), 1.0 + rs)
        return rsi


@register_factor
class StochKFactor(FactorBase):
    name = "stoch_k"
    sides = FACTOR_SIDES
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        n = 14
        l14 = _rolling_min(arr["low"], n)
        h14 = _rolling_max(arr["high"], n)
        return safe_divide(arr["close"] - l14, h14 - l14) * 100.0


@register_factor
class DistanceToVwapFactor(FactorBase):
    name = "distance_to_vwap"
    sides = FACTOR_SIDES
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        vwap = _rolling_vwap(arr, 20)
        return safe_divide(arr["close"] - vwap, vwap)


@register_factor
class UpperWickRatioFactor(FactorBase):
    name = "upper_wick_ratio"
    sides = (FACTOR_SIDE_SHORT,)
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        body_hi = np.maximum(arr["open"], arr["close"])
        upper_wick = arr["high"] - body_hi
        rng = arr["high"] - arr["low"]
        return safe_divide(upper_wick, rng)


@register_factor
class LowerWickRatioFactor(FactorBase):
    name = "lower_wick_ratio"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        body_lo = np.minimum(arr["open"], arr["close"])
        lower_wick = body_lo - arr["low"]
        rng = arr["high"] - arr["low"]
        return safe_divide(lower_wick, rng)


@register_factor
class BodyRatioFactor(FactorBase):
    name = "body_ratio"
    sides = FACTOR_SIDES
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        body = np.abs(arr["close"] - arr["open"])
        rng = arr["high"] - arr["low"]
        return safe_divide(body, rng)


@register_factor
class RangeZscore20Factor(FactorBase):
    name = "range_zscore_20"
    sides = FACTOR_SIDES
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        rng = arr["high"] - arr["low"]
        return _rolling_zscore(rng, 20)


@register_factor
class ClosePositionInBarFactor(FactorBase):
    name = "close_position_in_bar"
    sides = FACTOR_SIDES
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        return safe_divide(arr["close"] - arr["low"], arr["high"] - arr["low"])


@register_factor
class ReversalBarUpFactor(FactorBase):
    name = "reversal_bar_up"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        rng = arr["high"] - arr["low"]
        avg_rng = _rolling_mean(rng, 20)
        body_lo = np.minimum(arr["open"], arr["close"])
        lower_wick = safe_divide(body_lo - arr["low"], rng)
        close_pos  = safe_divide(arr["close"] - arr["low"], rng)
        mask = (rng > avg_rng) & (lower_wick >= 0.5) & (close_pos >= 0.6)
        out = np.full(len(rng), np.nan, dtype=np.float64)
        out[mask] = lower_wick[mask]
        return out


@register_factor
class ReversalBarDownFactor(FactorBase):
    name = "reversal_bar_down"
    sides = (FACTOR_SIDE_SHORT,)
    group = GROUP_MEAN_REVERSION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        rng = arr["high"] - arr["low"]
        avg_rng = _rolling_mean(rng, 20)
        body_hi = np.maximum(arr["open"], arr["close"])
        upper_wick = safe_divide(arr["high"] - body_hi, rng)
        close_pos  = safe_divide(arr["close"] - arr["low"], rng)
        mask = (rng > avg_rng) & (upper_wick >= 0.5) & (close_pos <= 0.4)
        out = np.full(len(rng), np.nan, dtype=np.float64)
        out[mask] = upper_wick[mask]
        return out


# ---------------------------------------------------------------------------
# Group 6 · Volatility & Compression
# ---------------------------------------------------------------------------

def _realized_vol(close: np.ndarray, window: int) -> np.ndarray:
    # log_ret[0] = 0.0 (treated as no-change) so rolling_std warm-up starts at bar 0
    log_ret = np.zeros(len(close), dtype=np.float64)
    prev = np.roll(close, 1)
    valid = (prev > 0) & (close > 0)
    valid[0] = False
    log_ret[valid] = np.log(close[valid] / prev[valid])
    return _rolling_std(log_ret, window)


@register_factor
class RealizedVol5mFactor(FactorBase):
    name = "realized_vol_5m"
    sides = FACTOR_SIDES
    group = GROUP_VOLATILITY

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _realized_vol(klines_to_arrays(klines)["close"], 5)


@register_factor
class RealizedVol15mFactor(FactorBase):
    name = "realized_vol_15m"
    sides = FACTOR_SIDES
    group = GROUP_VOLATILITY

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _realized_vol(klines_to_arrays(klines)["close"], 15)


@register_factor
class RealizedVol1hFactor(FactorBase):
    name = "realized_vol_1h"
    sides = FACTOR_SIDES
    group = GROUP_VOLATILITY

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _realized_vol(klines_to_arrays(klines)["close"], 60)


@register_factor
class Atr14Factor(FactorBase):
    name = "atr_14_1m"
    sides = FACTOR_SIDES
    group = GROUP_VOLATILITY

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        return _atr(klines_to_arrays(klines), 14)


@register_factor
class RangeMean20Factor(FactorBase):
    name = "range_mean_20"
    sides = FACTOR_SIDES
    group = GROUP_VOLATILITY

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        return _rolling_mean(arr["high"] - arr["low"], 20)


@register_factor
class BbWidth20Factor(FactorBase):
    name = "bb_width_20"
    sides = FACTOR_SIDES
    group = GROUP_VOLATILITY

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        close = klines_to_arrays(klines)["close"]
        std = _rolling_std(close, 20)
        ma  = _rolling_mean(close, 20)
        return safe_divide(4.0 * std, ma)


@register_factor
class BbWidthPercentile100Factor(FactorBase):
    name = "bb_width_percentile_100"
    sides = FACTOR_SIDES
    group = GROUP_VOLATILITY

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        close = klines_to_arrays(klines)["close"]
        std = _rolling_std(close, 20)
        ma  = _rolling_mean(close, 20)
        bb_width = safe_divide(4.0 * std, ma)
        return _rolling_percentile(bb_width, 100)


@register_factor
class AtrPercentile100Factor(FactorBase):
    name = "atr_percentile_100"
    sides = FACTOR_SIDES
    group = GROUP_VOLATILITY

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        atr = _atr(klines_to_arrays(klines), 14)
        return _rolling_percentile(atr, 100)


@register_factor
class VolCompressionRatioFactor(FactorBase):
    name = "vol_compression_ratio"
    sides = FACTOR_SIDES
    group = GROUP_VOLATILITY

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        close = klines_to_arrays(klines)["close"]
        vol_short = _realized_vol(close, 5)
        vol_long  = _realized_vol(close, 20)
        return safe_divide(vol_short, vol_long)


@register_factor
class RangeCompressionCountFactor(FactorBase):
    name = "range_compression_count"
    sides = FACTOR_SIDES
    group = GROUP_VOLATILITY

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        rng = arr["high"] - arr["low"]
        avg_rng = _rolling_mean(rng, 20)
        small = (rng < avg_rng).astype(np.float64)
        out = np.zeros(len(rng), dtype=np.float64)
        count = 0
        for i in range(len(rng)):
            if np.isnan(avg_rng[i]):
                out[i] = np.nan
                count = 0
            elif small[i] == 1.0:
                count += 1
                out[i] = float(count)
            else:
                count = 0
                out[i] = 0.0
        return out


@register_factor
class VolExpansionFlagFactor(FactorBase):
    name = "vol_expansion_flag"
    sides = FACTOR_SIDES
    group = GROUP_VOLATILITY

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        atr = _atr(klines_to_arrays(klines), 14)
        atr_z = _rolling_zscore(atr, 20)
        return np.where(atr_z > 2.0, 1.0, 0.0)


@register_factor
class TrueRangeSpikeFactor(FactorBase):
    name = "true_range_spike"
    sides = FACTOR_SIDES
    group = GROUP_VOLATILITY

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        tr = _true_range(klines_to_arrays(klines))
        return _rolling_zscore(tr, 20)


# ---------------------------------------------------------------------------
# Group 4 extra · High-Low Range (in priority pool)
# ---------------------------------------------------------------------------

@register_factor
class HighLowRange1mFactor(FactorBase):
    name = "high_low_range_1m"
    sides = FACTOR_SIDES
    group = GROUP_VOLATILITY

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        return arr["high"] - arr["low"]


# ---------------------------------------------------------------------------
# Legacy Price Action factors (kept from original)
# ---------------------------------------------------------------------------

@register_factor
class SweepPinBarLongFactor(FactorBase):
    name = "sweep_pin_bar_long"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_PRICE_ACTION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        n = 20
        tr = arr["high"] - arr["low"]
        avg_tr = _rolling_mean(tr, n)
        body_lo = np.minimum(arr["open"], arr["close"])
        lower_wick = body_lo - arr["low"]
        wick_ratio = safe_divide(lower_wick, tr)
        prev_lows = np.roll(arr["low"], 1); prev_lows[0] = arr["low"][0]
        min_prev_lows = _rolling_min(prev_lows, n)
        mask = (tr > avg_tr) & (wick_ratio >= 0.7) & (arr["low"] < min_prev_lows)
        out = np.full(arr["close"].shape, np.nan, dtype=np.float64)
        out[mask] = wick_ratio[mask]
        return out


@register_factor
class SweepPinBarShortFactor(FactorBase):
    name = "sweep_pin_bar_short"
    sides = (FACTOR_SIDE_SHORT,)
    group = GROUP_PRICE_ACTION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        n = 20
        tr = arr["high"] - arr["low"]
        avg_tr = _rolling_mean(tr, n)
        body_hi = np.maximum(arr["open"], arr["close"])
        upper_wick = arr["high"] - body_hi
        wick_ratio = safe_divide(upper_wick, tr)
        prev_highs = np.roll(arr["high"], 1); prev_highs[0] = arr["high"][0]
        max_prev_highs = _rolling_max(prev_highs, n)
        mask = (tr > avg_tr) & (wick_ratio >= 0.7) & (arr["high"] > max_prev_highs)
        out = np.full(arr["close"].shape, np.nan, dtype=np.float64)
        out[mask] = wick_ratio[mask]
        return out


@register_factor
class MaTrendAlignmentCrossoverFactor(FactorBase):
    name = "ma_trend_alignment_crossover"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_PRICE_ACTION

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        arr = klines_to_arrays(klines)
        close = arr["close"]
        ma20  = _rolling_mean(close, 20)
        ma50  = _rolling_mean(close, 50)
        ma120 = _rolling_mean(close, 120)
        ma20_prev = np.roll(ma20, 1); ma20_prev[0] = ma20[0]
        ma50_prev = np.roll(ma50, 1); ma50_prev[0] = ma50[0]
        cross_up  = (ma20 > ma50) & (ma20_prev <= ma50_prev)
        alignment = (ma20 > ma50) & (ma50 > ma120)
        mask = cross_up & alignment
        out = np.full(close.shape, np.nan, dtype=np.float64)
        out[mask] = safe_divide(ma20[mask] - ma50[mask], ma50[mask])
        return out
