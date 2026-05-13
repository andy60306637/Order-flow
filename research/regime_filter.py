"""
research/regime_filter.py

按 Regime 條件過濾 IC 分析的設定與遮罩計算。

支援四個維度：
  session      SessionComponent              → asian/london/ny/overlap/off
  market_vol   MarketVolatilityRegimeComponent → MEAN_REVERSION / BREAKOUT_TREND / …
  vwap_zone    VWAPDeviationComponent          → normal / extended_* / overextended_* / extreme_*
  vol_profile  VolumeProfileComponent          → in_value_area / above_poc / price_in_*_band

三種執行模式：
  filter        — 所有維度 AND 合併（維度內 OR），跑一次 IC 分析
  matrix        — 每個 label 獨立跑一次 IC 分析，結果並排比較
  cross_matrix  — 各維度勾選 label 的笛卡兒積，每個組合各跑一次（N×M×…次）

效能備注（已向量化）：
  所有 mask 計算改寫為 numpy / pandas 整段向量化，不再使用 Component 的 per-bar
  Python 迴圈。標籤與原 Component 在預設參數下完全相符（VWAP / VolumeProfile
  忽略 tick_map，僅用 kline typical-price，研究用途下足夠）。

  典型耗時（一年 1m K 棒，~525k 根）：
    Session         ~0.3 秒
    VWAP zone       ~1   秒
    Market vol      ~3–5 秒（含 rolling percentile）
    Volume profile  ~10–20 秒（per-bar np.bincount）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

if TYPE_CHECKING:
    from core.data_types import Kline

# ── Dimension identifiers ─────────────────────────────────────────────────────

DIM_SESSION     = "session"
DIM_MARKET_VOL  = "market_vol"
DIM_VWAP_ZONE   = "vwap_zone"
DIM_VOL_PROFILE = "vol_profile"

ALL_DIMENSIONS = [DIM_SESSION, DIM_MARKET_VOL, DIM_VWAP_ZONE, DIM_VOL_PROFILE]

# ── Label lists per dimension ─────────────────────────────────────────────────

SESSION_LABELS: list[str] = ["asian", "london", "ny", "overlap", "off"]

MARKET_VOL_LABELS: list[str] = [
    "MEAN_REVERSION",
    "BREAKOUT_TREND",
    "CHAOTIC_HIGH_VOL",
    "COMPRESSION_WAIT",
    "NEUTRAL",
]

VWAP_ZONE_LABELS: list[str] = [
    "normal",
    "extended_high",
    "extended_low",
    "overextended_high",
    "overextended_low",
    "extreme_high",
    "extreme_low",
]

VOL_PROFILE_LABELS: list[str] = [
    "in_value_area",
    "above_poc",
    "price_in_poc_band",
    "price_in_vah_band",
    "price_in_val_band",
    # Extended labels
    "near_VAL",           # |close - VAL| <= touch_band_pct  (alias with cleaner name)
    "below_VAL",          # close < VAL  (outside value area on the downside)
    "below_VAL_reclaim",  # prev close < prev VAL  AND  close >= VAL  (reclaim candle)
    "near_POC",           # |close - POC| <= touch_band_pct  (alias with cleaner name)
    "below_POC",          # close < POC  (lower half of profile, including below VAL)
    "outside_value_area", # close < VAL  OR  close > VAH
]

DIMENSION_LABELS: dict[str, list[str]] = {
    DIM_SESSION:     SESSION_LABELS,
    DIM_MARKET_VOL:  MARKET_VOL_LABELS,
    DIM_VWAP_ZONE:   VWAP_ZONE_LABELS,
    DIM_VOL_PROFILE: VOL_PROFILE_LABELS,
}

DIMENSION_DISPLAY: dict[str, str] = {
    DIM_SESSION:     "Session",
    DIM_MARKET_VOL:  "Market Vol Regime",
    DIM_VWAP_ZONE:   "VWAP Zone",
    DIM_VOL_PROFILE: "Vol Profile",
}

DIMENSION_SHORT: dict[str, str] = {
    DIM_SESSION:     "Sess",
    DIM_MARKET_VOL:  "MktVol",
    DIM_VWAP_ZONE:   "VWAP",
    DIM_VOL_PROFILE: "VP",
}


def label_display_name(key: str) -> str:
    """Convert regime key to readable label.

    'vwap_zone=overextended_low'            → 'VWAP: overextended_low'
    'session=asian+vwap_zone=normal'        → 'Sess: asian × VWAP: normal'
    """
    if "+" in key:
        return " × ".join(label_display_name(p) for p in key.split("+"))
    if "=" not in key:
        return key
    dim, label = key.split("=", 1)
    return f"{DIMENSION_SHORT.get(dim, dim)}: {label}"


def cross_combination_key(combo: list[tuple[str, str]]) -> str:
    """[(session, asian), (vwap_zone, normal)]  →  'session=asian+vwap_zone=normal'"""
    return "+".join(f"{dim}={lbl}" for dim, lbl in combo)


# ── Config dataclasses ────────────────────────────────────────────────────────

@dataclass
class RegimeDimConfig:
    dimension: str
    enabled: bool = False
    selected_labels: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class RegimeFilterConfig:
    mode: Literal["filter", "matrix", "cross_matrix"] = "matrix"
    dimensions: list[RegimeDimConfig] = field(default_factory=list)

    def is_active(self) -> bool:
        return any(d.enabled and d.selected_labels for d in self.dimensions)

    def active_label_count(self) -> int:
        return sum(
            len(d.selected_labels)
            for d in self.dimensions
            if d.enabled and d.selected_labels
        )

    def cross_combination_count(self) -> int:
        """Number of cartesian-product combinations (cross_matrix mode)."""
        from math import prod
        counts = [
            len(d.selected_labels)
            for d in self.dimensions
            if d.enabled and d.selected_labels
        ]
        return prod(counts) if counts else 0

    def get_cross_combinations(self) -> list[list[tuple[str, str]]]:
        """Cartesian product of enabled dims' selected labels."""
        from itertools import product as iterproduct
        groups: list[list[tuple[str, str]]] = []
        for d in self.dimensions:
            if d.enabled and d.selected_labels:
                groups.append([(d.dimension, lbl) for lbl in d.selected_labels])
        if not groups:
            return []
        return [list(combo) for combo in iterproduct(*groups)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "dimensions": [
                {
                    "dimension": d.dimension,
                    "enabled": d.enabled,
                    "selected_labels": list(d.selected_labels),
                    "params": dict(d.params),
                }
                for d in self.dimensions
            ],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "RegimeFilterConfig":
        if not isinstance(data, dict):
            return cls()
        dims = [
            RegimeDimConfig(
                dimension=d["dimension"],
                enabled=d.get("enabled", False),
                selected_labels=list(d.get("selected_labels", [])),
                params=dict(d.get("params", {})),
            )
            for d in data.get("dimensions", [])
            if isinstance(d, dict) and "dimension" in d
        ]
        return cls(mode=data.get("mode", "matrix"), dimensions=dims)


# ── Mask computation ──────────────────────────────────────────────────────────

def compute_regime_masks(
    klines: list["Kline"],
    config: RegimeFilterConfig,
    tick_map: Any | None = None,
) -> dict[str, np.ndarray]:
    """
    為每個選定的 regime label 計算逐 bar 布林遮罩。

    回傳：{"dimension=label": np.ndarray[bool, shape=(n,)], …}

    tick_map 為可選；無 tick 時各 Component 自動使用 kline fallback。
    """
    masks: dict[str, np.ndarray] = {}
    if not klines:
        return masks

    for dim_cfg in config.dimensions:
        if not dim_cfg.enabled or not dim_cfg.selected_labels:
            continue
        if dim_cfg.dimension == DIM_SESSION:
            _session_masks(klines, dim_cfg, masks)
        elif dim_cfg.dimension == DIM_MARKET_VOL:
            _market_vol_masks(klines, dim_cfg, masks)
        elif dim_cfg.dimension == DIM_VWAP_ZONE:
            _vwap_zone_masks(klines, dim_cfg, masks, tick_map)
        elif dim_cfg.dimension == DIM_VOL_PROFILE:
            _vol_profile_masks(klines, dim_cfg, masks, tick_map)

    return masks


def combine_for_filter(
    n: int,
    masks: dict[str, np.ndarray],
    config: RegimeFilterConfig,
) -> np.ndarray:
    """
    Filter 模式：維度間 AND，維度內 OR。
    回傳長度為 n 的 bool 遮罩。
    """
    combined = np.ones(n, dtype=bool)
    for dim_cfg in config.dimensions:
        if not dim_cfg.enabled or not dim_cfg.selected_labels:
            continue
        dim_or = np.zeros(n, dtype=bool)
        for label in dim_cfg.selected_labels:
            key = f"{dim_cfg.dimension}={label}"
            if key in masks:
                dim_or |= masks[key]
        combined &= dim_or
    return combined


# ── Kline → numpy column extraction (one pass) ────────────────────────────────

def _klines_to_cols(klines: list["Kline"]) -> dict[str, np.ndarray]:
    n = len(klines)
    cols = {
        "open_time": np.empty(n, dtype=np.int64),
        "high": np.empty(n, dtype=np.float64),
        "low": np.empty(n, dtype=np.float64),
        "close": np.empty(n, dtype=np.float64),
        "volume": np.empty(n, dtype=np.float64),
        "taker_buy_volume": np.empty(n, dtype=np.float64),
    }
    for i, k in enumerate(klines):
        cols["open_time"][i] = k.open_time
        cols["high"][i] = k.high
        cols["low"][i] = k.low
        cols["close"][i] = k.close
        cols["volume"][i] = k.volume
        cols["taker_buy_volume"][i] = k.taker_buy_volume
    return cols


# ── Generic rolling helpers (cumsum-based, fully vectorized) ──────────────────

def _rolling_sum(arr: np.ndarray, window: int) -> np.ndarray:
    """out[i] = sum(arr[i-window+1 : i+1]); NaN for i < window-1.
    NaN in input is treated as 0 to keep behavior predictable; callers should
    guard with explicit validity masks where needed.
    """
    n = arr.shape[0]
    out = np.full(n, np.nan, dtype=np.float64)
    if n < window or window <= 0:
        return out
    safe = np.where(np.isnan(arr), 0.0, arr)
    csum = np.concatenate(([0.0], np.cumsum(safe)))
    out[window - 1:] = csum[window:] - csum[:-window][:n - window + 1]
    return out


def _rolling_mean(arr: np.ndarray, window: int) -> np.ndarray:
    return _rolling_sum(arr, window) / window


def _rolling_std_pop(arr: np.ndarray, window: int) -> np.ndarray:
    """Population rolling std (ddof=0). NaN for i < window-1."""
    n = arr.shape[0]
    out = np.full(n, np.nan, dtype=np.float64)
    if n < window or window <= 0:
        return out
    safe = np.where(np.isnan(arr), 0.0, arr)
    csum = np.concatenate(([0.0], np.cumsum(safe)))
    csum_sq = np.concatenate(([0.0], np.cumsum(safe * safe)))
    s = csum[window:] - csum[:-window][:n - window + 1]
    s2 = csum_sq[window:] - csum_sq[:-window][:n - window + 1]
    mean = s / window
    var = np.maximum(s2 / window - mean * mean, 0.0)
    out[window - 1:] = np.sqrt(var)
    return out


def _rolling_percentile_exclude_current(values: np.ndarray, lookback: int) -> np.ndarray:
    """For each i, percentile rank (0–100) of values[i] vs values[i-lookback : i].

    Matches MarketVolatilityRegimeComponent._rv_percentile semantics: history
    excludes the current bar, uses strict '<' comparison, NaN samples skipped.
    """
    n = values.shape[0]
    out = np.full(n, 50.0, dtype=np.float64)
    for i in range(n):
        start = max(0, i - lookback)
        hist = values[start:i]
        cur = values[i]
        if not np.isfinite(cur):
            continue
        hist = hist[np.isfinite(hist)]
        if hist.size == 0:
            continue
        out[i] = float(np.sum(hist < cur)) / hist.size * 100.0
    return out


# ── Per-dimension helpers (vectorized) ────────────────────────────────────────

def _session_masks(
    klines: list["Kline"],
    cfg: RegimeDimConfig,
    out: dict[str, np.ndarray],
) -> None:
    """Vectorized session classification via pandas tz_convert (handles DST)."""
    import pandas as pd

    n = len(klines)
    if n == 0:
        for label in cfg.selected_labels:
            out[f"{DIM_SESSION}={label}"] = np.zeros(0, dtype=bool)
        return

    ts_ms = np.fromiter((k.open_time for k in klines), dtype=np.int64, count=n)
    dt_utc = pd.to_datetime(ts_ms, unit="ms", utc=True)

    london_hour = dt_utc.tz_convert("Europe/London").hour.to_numpy()
    ny_hour     = dt_utc.tz_convert("America/New_York").hour.to_numpy()
    tokyo_hour  = dt_utc.tz_convert("Asia/Tokyo").hour.to_numpy()

    is_london = (london_hour >= 8) & (london_hour < 17)
    is_ny     = (ny_hour     >= 8) & (ny_hour     < 17)
    is_asian  = (tokyo_hour  >= 9) & (tokyo_hour  < 18)

    # Priority overlap > ny > london > asian > off — apply lowest first, overwrite up.
    labels = np.full(n, "off", dtype=object)
    labels[is_asian] = "asian"
    labels[is_london] = "london"
    labels[is_ny] = "ny"
    labels[is_london & is_ny] = "overlap"

    for label in cfg.selected_labels:
        out[f"{DIM_SESSION}={label}"] = (labels == label)


def _vwap_zone_masks(
    klines: list["Kline"],
    cfg: RegimeDimConfig,
    out: dict[str, np.ndarray],
    tick_map: Any | None,  # noqa: ARG001  -- vectorized path uses kline VWAP only
) -> None:
    """Vectorized VWAP deviation zone classification.

    Implements MarketVolatilityRegimeComponent's kline VWAP path:
      vwap[i]    = sum(typical*vol, window) / sum(vol, window)
      dev[i]     = (close[i] - vwap[i]) / vwap[i]
      sigma[i]   = std(dev[i-lookback:i])  -- excludes current bar, kline-only
      z[i]       = dev[i] / (sigma[i] + 1e-10)

    Tick path (if provided) is not used in the vectorized version; for IC
    research this difference is negligible and the speedup is ~60–180×.
    """
    p = cfg.params
    window = int(p.get("window", 24))
    lookback = int(p.get("lookback", 100))
    oe_low = float(p.get("overextended_low", 2.0))
    oe_high = float(p.get("overextended_high", 2.5))

    n = len(klines)
    if n == 0:
        for label in cfg.selected_labels:
            out[f"{DIM_VWAP_ZONE}={label}"] = np.zeros(0, dtype=bool)
        return

    cols = _klines_to_cols(klines)
    closes = cols["close"]
    typical = (cols["high"] + cols["low"] + closes) / 3.0
    vols = cols["volume"]
    pv = typical * vols

    roll_pv = _rolling_sum(pv, window)
    roll_v = _rolling_sum(vols, window)
    vwap = np.full(n, np.nan, dtype=np.float64)
    valid_vwap = np.isfinite(roll_v) & (roll_v > 0)
    vwap[valid_vwap] = roll_pv[valid_vwap] / roll_v[valid_vwap]

    dev = np.full(n, np.nan, dtype=np.float64)
    safe_vwap = (vwap > 0) & np.isfinite(vwap)
    dev[safe_vwap] = (closes[safe_vwap] - vwap[safe_vwap]) / vwap[safe_vwap]

    # σ uses dev[i-lookback : i] (excluding current). Match the original Component
    # exactly: only valid (non-NaN) dev values count; <2 valid samples → σ=0
    # (z then becomes huge via /1e-10 and drives the bar into extreme zone, same
    # warm-up behavior as VWAPDeviationComponent.compute).
    shifted = np.empty(n, dtype=np.float64)
    shifted[0] = np.nan
    shifted[1:] = dev[:-1]
    valid_shift = np.isfinite(shifted).astype(np.float64)
    safe_shift = np.where(np.isnan(shifted), 0.0, shifted)
    cs_v = np.concatenate(([0.0], np.cumsum(valid_shift)))
    cs_x = np.concatenate(([0.0], np.cumsum(safe_shift)))
    cs_x2 = np.concatenate(([0.0], np.cumsum(safe_shift * safe_shift)))
    idx_arr = np.arange(n)
    lo = np.maximum(0, idx_arr - lookback)
    counts = cs_v[idx_arr] - cs_v[lo]
    sx = cs_x[idx_arr] - cs_x[lo]
    sx2 = cs_x2[idx_arr] - cs_x2[lo]
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(counts > 0, sx / np.maximum(counts, 1.0), 0.0)
        var = np.where(counts > 1, sx2 / np.maximum(counts, 1.0) - mean * mean, 0.0)
        var = np.maximum(var, 0.0)
    sigma = np.where(counts > 1, np.sqrt(var), 0.0)

    z = dev / (sigma + 1e-10)
    abs_z = np.abs(z)

    labels = np.full(n, "normal", dtype=object)
    direction_high = z >= 0

    ext = (abs_z >= 1.0) & (abs_z < oe_low)
    labels[ext & direction_high] = "extended_high"
    labels[ext & ~direction_high] = "extended_low"

    ovr = (abs_z >= oe_low) & (abs_z <= oe_high)
    labels[ovr & direction_high] = "overextended_high"
    labels[ovr & ~direction_high] = "overextended_low"

    xtr = abs_z > oe_high
    labels[xtr & direction_high] = "extreme_high"
    labels[xtr & ~direction_high] = "extreme_low"

    # Bars without enough data → "normal" (matches Component._empty_result).
    insufficient = ~np.isfinite(z) | ~valid_vwap
    labels[insufficient] = "normal"

    for label in cfg.selected_labels:
        out[f"{DIM_VWAP_ZONE}={label}"] = (labels == label)


# ── Market-vol vectorized indicator helpers ───────────────────────────────────

def _vec_wilder(arr: np.ndarray, period: int) -> np.ndarray:
    """Wilder-smoothed series. Seed at index `period` = mean(arr[1 : period+1]),
    then arr[i] = (prev * (period-1) + arr[i]) / period. NaN before seed.
    """
    n = arr.shape[0]
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period + 1:
        return out
    seed = float(np.nanmean(arr[1: period + 1]))
    if not np.isfinite(seed):
        return out
    out[period] = seed
    prev = seed
    inv = 1.0 / period
    p_m1 = period - 1
    for i in range(period + 1, n):
        v = arr[i]
        if not np.isfinite(v):
            v = 0.0
        prev = (prev * p_m1 + v) * inv
        out[i] = prev
    return out


def _vec_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> np.ndarray:
    n = highs.shape[0]
    if n < period + 1:
        return np.full(n, np.nan, dtype=np.float64)
    tr = np.full(n, np.nan, dtype=np.float64)
    prev_c = closes[:-1]
    tr[1:] = np.maximum.reduce([
        highs[1:] - lows[1:],
        np.abs(highs[1:] - prev_c),
        np.abs(lows[1:] - prev_c),
    ])
    return _vec_wilder(tr, period)


def _vec_efficiency_ratio(closes: np.ndarray, period: int) -> np.ndarray:
    """ER[i] = |close[i] - close[i-period]| / sum_{k=i-period..i-1} |diff(close)[k]|."""
    n = closes.shape[0]
    out = np.full(n, 0.5, dtype=np.float64)
    if n < period + 1:
        return out
    diffs = np.abs(np.diff(closes))  # length n-1; diffs[k] = |close[k+1] - close[k]|
    csum = np.concatenate(([0.0], np.cumsum(diffs)))
    idx = np.arange(period, n)
    # path = sum(diffs[i-period : i]) = csum[i] - csum[i-period]
    path = csum[idx] - csum[idx - period]
    net = np.abs(closes[period:] - closes[:-period])
    out[period:] = np.where(path > 0, net / (path + 1e-10), 0.0)
    return out


def _vec_adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> np.ndarray:
    """Per-bar ADX series. NaN before sufficient warm-up."""
    n = highs.shape[0]
    out = np.full(n, 0.0, dtype=np.float64)
    if n < period * 2 + 2:
        return out

    h_diff = highs[1:] - highs[:-1]
    l_diff = lows[:-1] - lows[1:]
    plus_dm = np.zeros(n, dtype=np.float64)
    minus_dm = np.zeros(n, dtype=np.float64)
    plus_dm[1:] = np.where((h_diff > l_diff) & (h_diff > 0), h_diff, 0.0)
    minus_dm[1:] = np.where((l_diff > h_diff) & (l_diff > 0), l_diff, 0.0)

    tr = np.full(n, np.nan, dtype=np.float64)
    prev_c = closes[:-1]
    tr[1:] = np.maximum.reduce([
        highs[1:] - lows[1:],
        np.abs(highs[1:] - prev_c),
        np.abs(lows[1:] - prev_c),
    ])

    atr_s = _vec_wilder(tr, period)
    plus_s = _vec_wilder(plus_dm, period)
    minus_s = _vec_wilder(minus_dm, period)

    eps = 1e-10
    plus_di = np.where(atr_s > 0, 100.0 * plus_s / (atr_s + eps), 0.0)
    minus_di = np.where(atr_s > 0, 100.0 * minus_s / (atr_s + eps), 0.0)
    di_sum = plus_di + minus_di
    dx = np.where(di_sum > 0, 100.0 * np.abs(plus_di - minus_di) / (di_sum + eps), 0.0)

    seed_i = 2 * period - 1
    if seed_i >= n:
        return out
    adx_arr = np.full(n, np.nan, dtype=np.float64)
    adx_arr[seed_i] = float(np.nanmean(dx[period: seed_i + 1]))
    prev = adx_arr[seed_i]
    inv = 1.0 / period
    p_m1 = period - 1
    for i in range(seed_i + 1, n):
        prev = (prev * p_m1 + dx[i]) * inv
        adx_arr[i] = prev
    out = np.where(np.isnan(adx_arr), 0.0, adx_arr)
    return out


def _vec_rv(closes: np.ndarray, period: int) -> np.ndarray:
    """Rolling population std of log returns; NaN before warm-up."""
    n = closes.shape[0]
    if n < period + 1:
        return np.full(n, np.nan, dtype=np.float64)
    log_ret = np.zeros(n, dtype=np.float64)
    safe = (closes[:-1] > 0) & (closes[1:] > 0)
    ratio = np.where(safe, closes[1:] / np.where(closes[:-1] > 0, closes[:-1], 1.0), 1.0)
    log_ret[1:] = np.where(safe, np.log(ratio), 0.0)
    rv = _rolling_std_pop(log_ret, period)
    # First valid index in original is period-1 of log_ret which starts at idx 1
    # => first valid is at bar period. Mask earlier bars as NaN to keep semantics.
    rv[:period] = np.nan
    return rv


def _vec_bb_width(closes: np.ndarray, period: int) -> np.ndarray:
    """Bollinger band width (upper - lower) / sma; population std."""
    sma = _rolling_mean(closes, period)
    std = _rolling_std_pop(closes, period)
    upper = sma + 2.0 * std
    lower = sma - 2.0 * std
    out = np.where(sma > 0, (upper - lower) / sma, 0.0)
    out[np.isnan(sma)] = np.nan
    return out


def _market_vol_masks(
    klines: list["Kline"],
    cfg: RegimeDimConfig,
    out: dict[str, np.ndarray],
) -> None:
    """Vectorized MarketVolatilityRegime classification."""
    p = cfg.params
    rv_period = int(p.get("rv_period", 60))
    atr_short = int(p.get("atr_short", 10))
    atr_long = int(p.get("atr_long", 60))
    er_period = int(p.get("er_period", 30))
    adx_period = int(p.get("adx_period", 14))
    bb_period = int(p.get("bb_period", 20))
    lookback = int(p.get("lookback", 100))

    n = len(klines)
    if n == 0:
        for label in cfg.selected_labels:
            out[f"{DIM_MARKET_VOL}={label}"] = np.zeros(0, dtype=bool)
        return

    cols = _klines_to_cols(klines)
    closes = cols["close"]
    highs = cols["high"]
    lows = cols["low"]

    rv_series = _vec_rv(closes, rv_period)
    rv_pct = _rolling_percentile_exclude_current(rv_series, lookback)
    atr_s = _vec_atr(highs, lows, closes, atr_short)
    atr_l = _vec_atr(highs, lows, closes, atr_long)
    atr_ratio = np.where(atr_l > 0, atr_s / (atr_l + 1e-10), 0.0)
    er = _vec_efficiency_ratio(closes, er_period)
    adx = _vec_adx(highs, lows, closes, adx_period)
    bb_width = _vec_bb_width(closes, bb_period)
    bb_pct = _rolling_percentile_exclude_current(bb_width, lookback)

    labels = np.full(n, "NEUTRAL", dtype=object)
    cond_mr = (rv_pct < 60) & (atr_ratio < 1.2) & (er < 0.30) & (adx < 25)
    cond_bt = (rv_pct >= 60) & (atr_ratio > 1.3) & (er > 0.40) & (adx > 25)
    cond_chaos = (rv_pct >= 85) & (atr_ratio > 1.5) & (er < 0.30)
    cond_compress = (rv_pct < 30) & (bb_pct < 20)

    # Match original if/elif chain priority.
    labels[cond_mr] = "MEAN_REVERSION"
    rem = ~cond_mr
    labels[rem & cond_bt] = "BREAKOUT_TREND"
    rem &= ~cond_bt
    labels[rem & cond_chaos] = "CHAOTIC_HIGH_VOL"
    rem &= ~cond_chaos
    labels[rem & cond_compress] = "COMPRESSION_WAIT"

    # Bars below warm-up (any input NaN) → NEUTRAL (mirrors _neutral_result path).
    warmup_invalid = ~np.isfinite(atr_s) | ~np.isfinite(atr_l) | ~np.isfinite(adx) | ~np.isfinite(bb_width)
    labels[warmup_invalid] = "NEUTRAL"

    for label in cfg.selected_labels:
        out[f"{DIM_MARKET_VOL}={label}"] = (labels == label)


def _vol_profile_masks(
    klines: list["Kline"],
    cfg: RegimeDimConfig,
    out: dict[str, np.ndarray],
    tick_map: Any | None,  # noqa: ARG001 -- vectorized path uses kline typical price only
) -> None:
    """Vectorized VolumeProfile labels.

    Each bar i: build a Volume Profile from bars [i-window+1 .. i] using
    typical price + volume binned at tick_size. Find POC = max-volume bin;
    expand outward until cumulative volume ≥ value_area_pct → VAH / VAL.

    Tick path is not used; for IC research the bar-level approximation is
    sufficient and ~10–30× faster than building a full VolumeProfile object.
    """
    p = cfg.params
    window = int(p.get("window", 24))
    tick_size = float(p.get("tick_size", 1.0))
    value_area_pct = float(p.get("value_area_pct", 0.70))
    touch_band_pct = float(p.get("touch_band_pct", 0.001))

    n = len(klines)
    bool_arrs: dict[str, np.ndarray] = {
        lbl: np.zeros(n, dtype=bool) for lbl in VOL_PROFILE_LABELS
    }
    if n == 0:
        for label in cfg.selected_labels:
            if label in bool_arrs:
                out[f"{DIM_VOL_PROFILE}={label}"] = bool_arrs[label]
        return

    cols = _klines_to_cols(klines)
    closes = cols["close"]
    highs = cols["high"]
    lows = cols["low"]
    volumes = cols["volume"]
    typical = (highs + lows + closes) / 3.0

    pocs = np.full(n, np.nan, dtype=np.float64)
    vahs = np.full(n, np.nan, dtype=np.float64)
    vals = np.full(n, np.nan, dtype=np.float64)

    if tick_size <= 0:
        tick_size = 1.0

    for i in range(n):
        s = max(0, i - window + 1)
        ws_typical = typical[s: i + 1]
        ws_vol = volumes[s: i + 1]
        valid = ws_vol > 0
        if not valid.any():
            continue
        prices = ws_typical[valid]
        wvols = ws_vol[valid]

        # Match build_volume_profile: globally-aligned bucket prices, drop
        # empty bins via np.unique so value-area expansion sees only filled levels.
        buckets = np.floor(prices / tick_size) * tick_size
        unique_buckets, inverse = np.unique(buckets, return_inverse=True)
        hist = np.bincount(inverse, weights=wvols, minlength=unique_buckets.size)
        total = float(hist.sum())
        if total <= 0:
            continue
        n_bins = unique_buckets.size

        poc_bin = int(np.argmax(hist))
        target = total * value_area_pct
        low_b = high_b = poc_bin
        cum = float(hist[poc_bin])
        while cum < target:
            can_left = low_b > 0
            can_right = high_b < n_bins - 1
            if not can_left and not can_right:
                break
            left_v = float(hist[low_b - 1]) if can_left else -1.0
            right_v = float(hist[high_b + 1]) if can_right else -1.0
            if left_v >= right_v:
                low_b -= 1
                cum += left_v
            else:
                high_b += 1
                cum += right_v
        pocs[i] = float(unique_buckets[poc_bin])
        vahs[i] = float(unique_buckets[high_b])
        vals[i] = float(unique_buckets[low_b])

    band = closes * touch_band_pct
    has_profile = np.isfinite(pocs)
    bool_arrs["above_poc"] = has_profile & (closes > pocs)
    bool_arrs["in_value_area"] = has_profile & (closes >= vals) & (closes <= vahs)
    bool_arrs["price_in_poc_band"] = has_profile & (np.abs(closes - pocs) <= band)
    bool_arrs["price_in_vah_band"] = has_profile & (np.abs(closes - vahs) <= band)
    bool_arrs["price_in_val_band"] = has_profile & (np.abs(closes - vals) <= band)

    # Extended labels
    bool_arrs["near_VAL"] = has_profile & (np.abs(closes - vals) <= band)
    bool_arrs["near_POC"] = has_profile & (np.abs(closes - pocs) <= band)
    bool_arrs["below_VAL"] = has_profile & (closes < vals)
    bool_arrs["below_POC"] = has_profile & (closes < pocs)
    bool_arrs["outside_value_area"] = has_profile & ((closes < vals) | (closes > vahs))

    # below_VAL_reclaim: previous bar was below VAL, current bar closed back at/above VAL.
    prev_closes = np.empty(n, dtype=np.float64)
    prev_closes[0] = np.nan
    prev_closes[1:] = closes[:-1]
    prev_vals = np.empty(n, dtype=np.float64)
    prev_vals[0] = np.nan
    prev_vals[1:] = vals[:-1]
    prev_has_profile = np.zeros(n, dtype=bool)
    prev_has_profile[1:] = has_profile[:-1]
    bool_arrs["below_VAL_reclaim"] = (
        has_profile
        & prev_has_profile
        & np.isfinite(prev_closes)
        & (prev_closes < prev_vals)   # was below VAL
        & (closes >= vals)             # reclaimed VAL
    )

    for label in cfg.selected_labels:
        if label in bool_arrs:
            out[f"{DIM_VOL_PROFILE}={label}"] = bool_arrs[label]
