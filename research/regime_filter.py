"""
research/regime_filter.py

按 Regime 條件過濾 IC 分析的設定與遮罩計算。

支援四個維度：
  session      SessionComponent              → asian/london/ny/overlap/off
  market_vol   MarketVolatilityRegimeComponent → MEAN_REVERSION / BREAKOUT_TREND / …
  vwap_zone    VWAPDeviationComponent          → normal / extended_* / overextended_* / extreme_*
  vol_profile  VolumeProfileComponent          → in_value_area / above_poc / price_in_*_band

兩種執行模式：
  filter  — 所有維度 AND 合併（維度內 OR），跑一次 IC 分析
  matrix  — 每個 label 獨立跑一次 IC 分析，結果並排比較

效能備注：
  SessionComponent       每根 K 棒 O(1)，極快。
  MarketVolatilityRegimeComponent / VWAPDeviationComponent
                         每根 K 棒掃描 window 根，O(n × window)。
  VolumeProfileComponent 每根 K 棒建立完整 Volume Profile，較重；
                         10k 根 1m 棒約 5–15 秒。
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
    """'vwap_zone=overextended_low'  →  'VWAP: overextended_low'"""
    if "=" not in key:
        return key
    dim, label = key.split("=", 1)
    return f"{DIMENSION_SHORT.get(dim, dim)}: {label}"


# ── Config dataclasses ────────────────────────────────────────────────────────

@dataclass
class RegimeDimConfig:
    dimension: str
    enabled: bool = False
    selected_labels: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class RegimeFilterConfig:
    mode: Literal["filter", "matrix"] = "matrix"
    dimensions: list[RegimeDimConfig] = field(default_factory=list)

    def is_active(self) -> bool:
        return any(d.enabled and d.selected_labels for d in self.dimensions)

    def active_label_count(self) -> int:
        return sum(
            len(d.selected_labels)
            for d in self.dimensions
            if d.enabled and d.selected_labels
        )

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


# ── Per-dimension helpers ─────────────────────────────────────────────────────

def _session_masks(
    klines: list["Kline"],
    cfg: RegimeDimConfig,
    out: dict[str, np.ndarray],
) -> None:
    from strategies.pipeline.component import SessionComponent
    comp = SessionComponent()
    n = len(klines)
    labels = np.empty(n, dtype=object)
    for i in range(n):
        labels[i] = comp.compute(klines, i)["session"]
    for label in cfg.selected_labels:
        out[f"{DIM_SESSION}={label}"] = (labels == label)


def _market_vol_masks(
    klines: list["Kline"],
    cfg: RegimeDimConfig,
    out: dict[str, np.ndarray],
) -> None:
    from strategies.pipeline.component import MarketVolatilityRegimeComponent
    p = cfg.params
    comp = MarketVolatilityRegimeComponent(
        rv_period=int(p.get("rv_period", 60)),
        atr_short=int(p.get("atr_short", 10)),
        atr_long=int(p.get("atr_long", 60)),
        er_period=int(p.get("er_period", 30)),
        adx_period=int(p.get("adx_period", 14)),
        bb_period=int(p.get("bb_period", 20)),
        lookback=int(p.get("lookback", 100)),
    )
    n = len(klines)
    labels = np.empty(n, dtype=object)
    for i in range(n):
        labels[i] = comp.compute(klines, i)["regime"]
    for label in cfg.selected_labels:
        out[f"{DIM_MARKET_VOL}={label}"] = (labels == label)


def _vwap_zone_masks(
    klines: list["Kline"],
    cfg: RegimeDimConfig,
    out: dict[str, np.ndarray],
    tick_map: Any | None,
) -> None:
    from strategies.pipeline.component import VWAPDeviationComponent
    p = cfg.params
    comp = VWAPDeviationComponent(
        window=int(p.get("window", 24)),
        lookback=int(p.get("lookback", 100)),
    )
    n = len(klines)
    labels = np.empty(n, dtype=object)
    for i in range(n):
        labels[i] = comp.compute(klines, i, tick_map)["zone"]
    for label in cfg.selected_labels:
        out[f"{DIM_VWAP_ZONE}={label}"] = (labels == label)


def _vol_profile_masks(
    klines: list["Kline"],
    cfg: RegimeDimConfig,
    out: dict[str, np.ndarray],
    tick_map: Any | None,
) -> None:
    from strategies.pipeline.component import VolumeProfileComponent
    p = cfg.params
    comp = VolumeProfileComponent(
        window=int(p.get("window", 24)),
        tick_size=float(p.get("tick_size", 1.0)),
        value_area_pct=float(p.get("value_area_pct", 0.70)),
    )
    n = len(klines)
    bool_arrs: dict[str, np.ndarray] = {
        lbl: np.zeros(n, dtype=bool) for lbl in VOL_PROFILE_LABELS
    }
    for i in range(n):
        r = comp.compute(klines, i, tick_map)
        for lbl in VOL_PROFILE_LABELS:
            bool_arrs[lbl][i] = bool(r.get(lbl, False))
    for label in cfg.selected_labels:
        if label in bool_arrs:
            out[f"{DIM_VOL_PROFILE}={label}"] = bool_arrs[label]
