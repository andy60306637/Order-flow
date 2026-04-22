"""
core/volume_profile.py — Volume Profile 引擎

公開 API:
    build_volume_profile(ticks, tick_size, value_area_pct, ...) -> VolumeProfile | None
    build_bar_profiles(tick_map, tick_size, ...)                -> dict[int, VolumeProfile]
    build_composite_profile(tick_map, open_times, tick_size, ...)-> VolumeProfile | None
    build_rolling_profiles(tick_map, klines, window, tick_size, ...)-> dict[int, VolumeProfile]

Tick 格式 ndarray(N, 4):
    col 0: trade_time (ms)
    col 1: price
    col 2: qty
    col 3: is_buyer_maker  (0.0 = 買方主動/bid hit, 1.0 = 賣方主動/ask hit)

設計：無狀態純函數，可被策略或分析工具共用。
      核心計算全程 numpy vectorized，適合大量 tick 資料。
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from core.data_types import Kline

TickBarMap = Mapping[int, np.ndarray]


# ─── Data structures ──────────────────────────────────────────────────────────

@dataclass
class VolumeProfileLevel:
    """Volume Profile 中單一價位分桶的成交資訊。"""
    price: float
    total_vol: float = 0.0
    buy_vol: float = 0.0   # is_buyer_maker=0 (買方主動)
    sell_vol: float = 0.0  # is_buyer_maker=1 (賣方主動)

    @property
    def delta(self) -> float:
        return self.buy_vol - self.sell_vol

    @property
    def delta_pct(self) -> float:
        """Delta 占總量比例 [-1, +1]。"""
        if self.total_vol == 0.0:
            return 0.0
        return self.delta / self.total_vol


@dataclass
class VolumeProfile:
    """
    Volume Profile 計算結果。

    levels 以價格升序排列（dict 在 Python 3.7+ 保持插入順序，
    而 unique_buckets 來自 np.unique 已排序，因此順序有保證）。
    """
    levels: Dict[float, VolumeProfileLevel]
    tick_size: float
    poc_price: float          # Point of Control — 最大成交量的價位
    vah: float                # Value Area High
    val: float                # Value Area Low
    total_volume: float
    value_area_pct: float     # 計算 VA 所用的目標百分比 (e.g. 0.70)
    hvn_prices: List[float]   # High Volume Nodes（升序）
    lvn_prices: List[float]   # Low Volume Nodes（升序）

    # ── convenience accessors ──────────────────────────────────────────────

    @property
    def value_area_volume(self) -> float:
        """Value Area 內的實際累計成交量。"""
        return sum(
            lv.total_vol for p, lv in self.levels.items()
            if self.val <= p <= self.vah
        )

    def level_at(self, price: float) -> Optional[VolumeProfileLevel]:
        """取 price 所在分桶的 level；無資料時回傳 None。"""
        bucketed = math.floor(price / self.tick_size) * self.tick_size
        return self.levels.get(bucketed)

    def nearest_support(self, price: float) -> Optional[float]:
        """
        price 以下最近的強支撐（POC / VAL / HVN）。
        回傳價格，無候選時回傳 None。
        """
        candidates = [
            p for p in [self.poc_price, self.val] + self.hvn_prices
            if p < price
        ]
        return max(candidates) if candidates else None

    def nearest_resistance(self, price: float) -> Optional[float]:
        """
        price 以上最近的強阻力（POC / VAH / HVN）。
        回傳價格，無候選時回傳 None。
        """
        candidates = [
            p for p in [self.poc_price, self.vah] + self.hvn_prices
            if p > price
        ]
        return min(candidates) if candidates else None

    def is_in_value_area(self, price: float) -> bool:
        return self.val <= price <= self.vah


# ─── Public API ───────────────────────────────────────────────────────────────

def build_volume_profile(
    ticks: np.ndarray,
    tick_size: float = 1.0,
    value_area_pct: float = 0.70,
    hvn_threshold: float = 1.5,
    lvn_threshold: float = 0.5,
) -> Optional[VolumeProfile]:
    """
    從 tick ndarray(N, 4) 建立 Volume Profile。

    Args:
        ticks:           shape (N, 4) — [trade_time, price, qty, is_buyer_maker]
        tick_size:       價格分桶大小（e.g. 0.1 for BTCUSDT perp）
        value_area_pct:  Value Area 目標百分比 (0 < x <= 1)
        hvn_threshold:   vol > mean * hvn_threshold → HVN
        lvn_threshold:   vol < mean * lvn_threshold → LVN

    Returns:
        VolumeProfile，或 None（ticks 為空 / 全量為零）。
    """
    if len(ticks) == 0:
        return None

    prices = ticks[:, 1]
    qty    = ticks[:, 2]
    is_bm  = ticks[:, 3]

    buckets        = np.floor(prices / tick_size) * tick_size
    unique_buckets = np.unique(buckets)          # sorted ascending
    n_buckets      = len(unique_buckets)

    idx_array = np.searchsorted(unique_buckets, buckets)

    total_vol = np.bincount(idx_array, weights=qty,                 minlength=n_buckets)
    buy_vol   = np.bincount(idx_array, weights=qty * (is_bm == 0.0), minlength=n_buckets)
    sell_vol  = np.bincount(idx_array, weights=qty * (is_bm == 1.0), minlength=n_buckets)

    total_volume = float(total_vol.sum())
    if total_volume == 0.0:
        return None

    poc_idx   = int(np.argmax(total_vol))
    poc_price = float(unique_buckets[poc_idx])

    val_idx, vah_idx = _calc_value_area(total_vol, poc_idx, value_area_pct)
    val = float(unique_buckets[val_idx])
    vah = float(unique_buckets[vah_idx])

    mean_vol  = total_volume / n_buckets
    hvn_prices = sorted(
        float(unique_buckets[i]) for i in range(n_buckets)
        if total_vol[i] >= mean_vol * hvn_threshold
    )
    lvn_prices = sorted(
        float(unique_buckets[i]) for i in range(n_buckets)
        if total_vol[i] <= mean_vol * lvn_threshold
    )

    levels = {
        float(unique_buckets[i]): VolumeProfileLevel(
            price    = float(unique_buckets[i]),
            total_vol= float(total_vol[i]),
            buy_vol  = float(buy_vol[i]),
            sell_vol = float(sell_vol[i]),
        )
        for i in range(n_buckets)
    }

    return VolumeProfile(
        levels        = levels,
        tick_size     = tick_size,
        poc_price     = poc_price,
        vah           = vah,
        val           = val,
        total_volume  = total_volume,
        value_area_pct= value_area_pct,
        hvn_prices    = hvn_prices,
        lvn_prices    = lvn_prices,
    )


def build_bar_profiles(
    tick_map: TickBarMap,
    tick_size: float = 1.0,
    value_area_pct: float = 0.70,
    hvn_threshold: float = 1.5,
    lvn_threshold: float = 0.5,
) -> Dict[int, VolumeProfile]:
    """
    為 tick_map 中的每根 K 棒建立獨立的單根 Volume Profile。

    Returns:
        dict: open_time_ms → VolumeProfile
              沒有 tick 資料的 bar 不會出現在結果中。
    """
    result: Dict[int, VolumeProfile] = {}
    for open_time, ticks in tick_map.items():
        if len(ticks) == 0:
            continue
        vp = build_volume_profile(ticks, tick_size, value_area_pct, hvn_threshold, lvn_threshold)
        if vp is not None:
            result[int(open_time)] = vp
    return result


def build_composite_profile(
    tick_map: TickBarMap,
    open_times: Sequence[int],
    tick_size: float = 1.0,
    value_area_pct: float = 0.70,
    hvn_threshold: float = 1.5,
    lvn_threshold: float = 0.5,
) -> Optional[VolumeProfile]:
    """
    從指定的多根 K 棒合併建立 composite Volume Profile。

    Args:
        tick_map:    open_time → ticks ndarray
        open_times:  要包含的 K 棒 open_time 列表（順序不重要）

    Returns:
        VolumeProfile，或 None（無任何 tick 資料）。
    """
    parts = [tick_map.get(int(ot)) for ot in open_times]
    parts = [p for p in parts if p is not None and len(p) > 0]
    if not parts:
        return None
    combined = np.concatenate(parts, axis=0) if len(parts) > 1 else parts[0]
    return build_volume_profile(combined, tick_size, value_area_pct, hvn_threshold, lvn_threshold)


def build_rolling_profiles(
    tick_map: TickBarMap,
    klines: List[Kline],
    window: int = 20,
    tick_size: float = 1.0,
    value_area_pct: float = 0.70,
    hvn_threshold: float = 1.5,
    lvn_threshold: float = 0.5,
) -> Dict[int, VolumeProfile]:
    """
    為每根 K 棒建立「過去 window 根（含自身）」的 composite Volume Profile。

    視窗不足 window 根時，使用從最舊 bar 到當前 bar 的所有可用資料。
    沒有任何 tick 的 bar 不出現在結果中。

    Args:
        tick_map:  open_time → ticks ndarray
        klines:    已排序的 K 棒序列（klines[0] 最舊）
        window:    滾動視窗大小（bars）

    Returns:
        dict: open_time_ms → VolumeProfile
    """
    if not klines:
        return {}

    result: Dict[int, VolumeProfile] = {}
    n = len(klines)

    for i in range(n):
        start = max(0, i - window + 1)
        window_times = [klines[j].open_time for j in range(start, i + 1)]
        vp = build_composite_profile(
            tick_map, window_times,
            tick_size, value_area_pct, hvn_threshold, lvn_threshold,
        )
        if vp is not None:
            result[klines[i].open_time] = vp

    return result


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _calc_value_area(
    total_vol: np.ndarray,
    poc_idx: int,
    target_pct: float,
) -> Tuple[int, int]:
    """
    從 POC 向兩側擴展，直到累計量達到 target_pct * total。
    每步選擇量較大的那一側擴展（標準 TPO Value Area 演算法）。

    Returns:
        (val_idx, vah_idx) — Value Area Low / High 在 unique_buckets 的索引。
    """
    n = len(total_vol)
    total_volume = float(total_vol.sum())
    target_vol   = total_volume * target_pct

    cum_vol = float(total_vol[poc_idx])
    lo = poc_idx
    hi = poc_idx

    while cum_vol < target_vol:
        can_expand_lo = lo > 0
        can_expand_hi = hi < n - 1

        if not can_expand_lo and not can_expand_hi:
            break

        vol_lo = float(total_vol[lo - 1]) if can_expand_lo else -1.0
        vol_hi = float(total_vol[hi + 1]) if can_expand_hi else -1.0

        if vol_lo >= vol_hi:
            lo     -= 1
            cum_vol += float(total_vol[lo])
        else:
            hi     += 1
            cum_vol += float(total_vol[hi])

    return lo, hi
