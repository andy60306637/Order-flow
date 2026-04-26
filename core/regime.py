"""
core/regime.py — 市場狀態（Regime）偵測模組

公開 API：
    detect_regime(klines, **params) -> RegimeLabel
    compute_regime_features(klines, **params) -> RegimeFeatures

設計：無狀態純函數，可被策略或分析工具共用。
使用5特徵加權投票系統判斷 trend_up / trend_down / range / neutral。

特徵：
  1. Efficiency Ratio（方向效率比）
  2. EMA Slope（EMA 斜率）
  3. HH/HL Structure Score（Swing Pivot 高低點結構分數）
  4. Breakout Follow-Through Ratio（突破後跟隨率）
  5. Delta Persistence（Delta 方向持續性）
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Tuple

import numpy as np

from core.data_types import Kline

RegimeLabel = Literal["trend_up", "trend_down", "range", "neutral"]


@dataclass
class RegimeFeatures:
    er: Optional[float]                  # Efficiency Ratio [0, 1]；None=資料不足
    ema_slope: Optional[float]           # 歸一化 EMA 斜率 (%/bar)；None=資料不足
    hh_hl_score: Optional[float]         # HH/HL 結構分數 [-1, +1]；None=swing 不足
    breakout_ratio_up: Optional[float]   # 向上突破跟隨率 [0, 1]；None=突破次數不足
    breakout_ratio_down: Optional[float] # 向下突破跟隨率
    breakout_cnt_up: int
    breakout_cnt_down: int
    delta_persistence_up: float          # 多方 delta 持續性 [0, 1]
    delta_persistence_down: float        # 空方 delta 持續性
    delta_consistency: float             # delta 方向一致性 [0, 1]
    score_up: float
    score_down: float
    score_range: float
    label: RegimeLabel
    active_voters: int                   # 有效投票的特徵數（資料不足的特徵不計）


# ─── Public API ───────────────────────────────────────────────────────────────

def detect_regime(
    klines: List[Kline],
    er_window: int = 20,
    er_trend_threshold: float = 0.45,
    er_range_threshold: float = 0.25,
    ema_period: int = 20,
    slope_lookback: int = 5,
    slope_threshold: float = 0.002,
    struct_window: int = 40,
    pivot_bars: int = 3,
    struct_up_threshold: float = 0.5,
    struct_range_threshold: float = 0.3,
    breakout_window: int = 30,
    breakout_lookback: int = 10,
    breakout_min_count: int = 2,
    follow_threshold: float = 0.6,
    follow_fade_threshold: float = 0.4,
    delta_window: int = 10,
    delta_persist_threshold: float = 0.5,
    delta_consistency_threshold: float = 0.4,
    weight_er: float = 1.0,
    weight_ema: float = 1.0,
    weight_hh_hl: float = 1.5,
    weight_breakout: float = 1.5,
    weight_delta: float = 1.0,
    trend_threshold: float = 0.4,
    separation: float = 0.15,
    range_threshold: float = 0.35,
) -> RegimeLabel:
    """輸入 K 棒序列，輸出市場狀態標籤。"""
    return compute_regime_features(
        klines,
        er_window=er_window,
        er_trend_threshold=er_trend_threshold,
        er_range_threshold=er_range_threshold,
        ema_period=ema_period,
        slope_lookback=slope_lookback,
        slope_threshold=slope_threshold,
        struct_window=struct_window,
        pivot_bars=pivot_bars,
        struct_up_threshold=struct_up_threshold,
        struct_range_threshold=struct_range_threshold,
        breakout_window=breakout_window,
        breakout_lookback=breakout_lookback,
        breakout_min_count=breakout_min_count,
        follow_threshold=follow_threshold,
        follow_fade_threshold=follow_fade_threshold,
        delta_window=delta_window,
        delta_persist_threshold=delta_persist_threshold,
        delta_consistency_threshold=delta_consistency_threshold,
        weight_er=weight_er,
        weight_ema=weight_ema,
        weight_hh_hl=weight_hh_hl,
        weight_breakout=weight_breakout,
        weight_delta=weight_delta,
        trend_threshold=trend_threshold,
        separation=separation,
        range_threshold=range_threshold,
    ).label


def compute_regime_features(
    klines: List[Kline],
    er_window: int = 20,
    er_trend_threshold: float = 0.45,
    er_range_threshold: float = 0.25,
    ema_period: int = 20,
    slope_lookback: int = 5,
    slope_threshold: float = 0.002,
    struct_window: int = 40,
    pivot_bars: int = 3,
    struct_up_threshold: float = 0.5,
    struct_range_threshold: float = 0.3,
    breakout_window: int = 30,
    breakout_lookback: int = 10,
    breakout_min_count: int = 2,
    follow_threshold: float = 0.6,
    follow_fade_threshold: float = 0.4,
    delta_window: int = 10,
    delta_persist_threshold: float = 0.5,
    delta_consistency_threshold: float = 0.4,
    weight_er: float = 1.0,
    weight_ema: float = 1.0,
    weight_hh_hl: float = 1.5,
    weight_breakout: float = 1.5,
    weight_delta: float = 1.0,
    trend_threshold: float = 0.4,
    separation: float = 0.15,
    range_threshold: float = 0.35,
) -> RegimeFeatures:
    """計算所有中間特徵並回傳完整 RegimeFeatures（含最終 label）。"""
    _empty = RegimeFeatures(
        er=None, ema_slope=None, hh_hl_score=None,
        breakout_ratio_up=None, breakout_ratio_down=None,
        breakout_cnt_up=0, breakout_cnt_down=0,
        delta_persistence_up=0.0, delta_persistence_down=0.0,
        delta_consistency=0.0,
        score_up=0.0, score_down=0.0, score_range=0.0,
        label="neutral", active_voters=0,
    )

    if len(klines) < delta_window:
        return _empty

    closes = np.array([k.close for k in klines], dtype=float)
    highs  = np.array([k.high  for k in klines], dtype=float)
    lows   = np.array([k.low   for k in klines], dtype=float)

    # ── 計算特徵 ─────────────────────────────────────────────────────────────
    er    = _calc_er(closes, er_window)
    slope = _calc_ema_slope(closes, ema_period, slope_lookback)
    hh_hl = _calc_hh_hl_score(highs, lows, struct_window, pivot_bars)
    bftr_up, bftr_down, bo_cnt_up, bo_cnt_down = _calc_breakout_ratio(
        highs, lows, closes, breakout_window, breakout_lookback,
    )
    dp_up, dp_down, dp_consistency = _calc_delta_persistence(klines, delta_window)

    # ── 投票 ─────────────────────────────────────────────────────────────────
    score_up = score_down = score_range = effective_max = 0.0
    active_voters = 0

    for vu, vd, vr, ew in [
        _vote_er(er, closes, er_window, er_trend_threshold, er_range_threshold, weight_er),
        _vote_ema(slope, slope_threshold, weight_ema),
        _vote_hh_hl(hh_hl, struct_up_threshold, struct_range_threshold, weight_hh_hl),
        _vote_breakout(
            bftr_up, bftr_down, bo_cnt_up, bo_cnt_down,
            breakout_min_count, follow_threshold, follow_fade_threshold, weight_breakout,
        ),
        _vote_delta(dp_up, dp_down, dp_consistency,
                    delta_persist_threshold, delta_consistency_threshold, weight_delta),
    ]:
        score_up    += vu
        score_down  += vd
        score_range += vr
        effective_max += ew
        if ew > 0:
            active_voters += 1

    label = _decide(score_up, score_down, score_range, effective_max,
                    trend_threshold, separation, range_threshold)

    return RegimeFeatures(
        er=er,
        ema_slope=slope,
        hh_hl_score=hh_hl,
        breakout_ratio_up=bftr_up,
        breakout_ratio_down=bftr_down,
        breakout_cnt_up=bo_cnt_up,
        breakout_cnt_down=bo_cnt_down,
        delta_persistence_up=dp_up,
        delta_persistence_down=dp_down,
        delta_consistency=dp_consistency,
        score_up=score_up,
        score_down=score_down,
        score_range=score_range,
        label=label,
        active_voters=active_voters,
    )


# ─── Internal: feature calculators ───────────────────────────────────────────

def _calc_er(closes: np.ndarray, window: int) -> Optional[float]:
    """Kaufman Efficiency Ratio: |net move| / total path."""
    if len(closes) < window + 1:
        return None
    segment = closes[-(window + 1):]
    direction = abs(float(segment[-1] - segment[0]))
    path = float(np.sum(np.abs(np.diff(segment))))
    if path < 1e-30:
        return 0.0
    return direction / path


def _calc_ema_slope(closes: np.ndarray, ema_period: int, slope_lookback: int) -> Optional[float]:
    """Normalized EMA slope (%/bar) computed via numpy least squares (no scipy)."""
    n = len(closes)
    if n < ema_period + slope_lookback:
        return None

    alpha = 2.0 / (ema_period + 1)
    ema = np.empty(n, dtype=float)
    ema[0] = closes[0]
    for i in range(1, n):
        ema[i] = alpha * closes[i] + (1.0 - alpha) * ema[i - 1]

    y = ema[-slope_lookback:]
    x = np.arange(slope_lookback, dtype=float)
    x_mean = x.mean()
    y_mean = y.mean()
    denom = float(np.dot(x - x_mean, x - x_mean))
    if denom < 1e-30:
        return 0.0
    raw_slope = float(np.dot(x - x_mean, y - y_mean)) / denom
    ref = ema[-1]
    if abs(ref) < 1e-30:
        return 0.0
    return raw_slope / ref * 100.0


def _find_swing_highs(values: np.ndarray, pivot_bars: int) -> List[int]:
    """Indices of swing highs: local max with pivot_bars clearance on each side."""
    n = len(values)
    idxs = []
    for i in range(pivot_bars, n - pivot_bars):
        if (all(values[i] >= values[i - j] for j in range(1, pivot_bars + 1)) and
                all(values[i] >= values[i + j] for j in range(1, pivot_bars + 1))):
            idxs.append(i)
    return idxs


def _find_swing_lows(values: np.ndarray, pivot_bars: int) -> List[int]:
    """Indices of swing lows: local min with pivot_bars clearance on each side."""
    n = len(values)
    idxs = []
    for i in range(pivot_bars, n - pivot_bars):
        if (all(values[i] <= values[i - j] for j in range(1, pivot_bars + 1)) and
                all(values[i] <= values[i + j] for j in range(1, pivot_bars + 1))):
            idxs.append(i)
    return idxs


def _calc_hh_hl_score(
    highs: np.ndarray,
    lows: np.ndarray,
    struct_window: int,
    pivot_bars: int,
) -> Optional[float]:
    """HH/HL structure score in [-1, +1]. None when swing points insufficient."""
    seg_h = highs[-struct_window:] if len(highs) >= struct_window else highs
    seg_l = lows[-struct_window:]  if len(lows)  >= struct_window else lows

    sh = _find_swing_highs(seg_h, pivot_bars)
    sl = _find_swing_lows(seg_l, pivot_bars)

    if len(sh) < 2 or len(sl) < 2:
        return None

    hh = sum(1 for a, b in zip(sh, sh[1:]) if seg_h[b] > seg_h[a])
    lh = sum(1 for a, b in zip(sh, sh[1:]) if seg_h[b] < seg_h[a])
    hl = sum(1 for a, b in zip(sl, sl[1:]) if seg_l[b] > seg_l[a])
    ll = sum(1 for a, b in zip(sl, sl[1:]) if seg_l[b] < seg_l[a])

    total = hh + hl + lh + ll
    if total == 0:
        return 0.0
    return float(hh + hl - lh - ll) / total


def _calc_breakout_ratio(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    window: int,
    lookback: int,
) -> Tuple[Optional[float], Optional[float], int, int]:
    """
    Breakout follow-through ratio within the last `window` bars.
    A breakout at bar i is confirmed if bar i+1 maintains the breakout direction.
    Returns (ratio_up, ratio_down, cnt_up, cnt_down).
    """
    n = len(closes)
    if n < lookback + window + 1:
        return None, None, 0, 0

    start = n - window - 1  # +1 for follow-through confirmation on last bar in window
    up_total = up_follow = down_total = down_follow = 0

    for i in range(start, n - 1):
        if i < lookback:
            continue
        recent_high = float(np.max(highs[i - lookback:i]))
        recent_low  = float(np.min(lows[i  - lookback:i]))

        if highs[i] > recent_high and closes[i] > recent_high:
            up_total += 1
            if closes[i + 1] > (closes[i] + recent_high) / 2.0:
                up_follow += 1

        if lows[i] < recent_low and closes[i] < recent_low:
            down_total += 1
            if closes[i + 1] < (closes[i] + recent_low) / 2.0:
                down_follow += 1

    ratio_up   = float(up_follow)   / up_total   if up_total   > 0 else None
    ratio_down = float(down_follow) / down_total if down_total > 0 else None
    return ratio_up, ratio_down, up_total, down_total


def _calc_delta_persistence(
    klines: List[Kline],
    window: int,
) -> Tuple[float, float, float]:
    """
    Delta persistence and consistency over the last `window` bars.
    Returns (persist_up, persist_down, consistency).
    persist_* = directional_ratio × consistency_score
    """
    seg = klines[-window:]
    n = len(seg)
    if n == 0:
        return 0.0, 0.0, 0.0

    deltas = [2.0 * k.taker_buy_volume - k.volume for k in seg]
    signs  = [1 if d > 0 else (-1 if d < 0 else 0) for d in deltas]

    pos_ratio = signs.count(1)  / n
    neg_ratio = signs.count(-1) / n

    non_zero = [s for s in signs if s != 0]
    if len(non_zero) < 2:
        consistency = 1.0
    else:
        changes = sum(1 for a, b in zip(non_zero, non_zero[1:]) if a != b)
        consistency = 1.0 - changes / (len(non_zero) - 1)

    return pos_ratio * consistency, neg_ratio * consistency, consistency


# ─── Internal: voting ─────────────────────────────────────────────────────────
# Each _vote_* returns (up_score, down_score, range_score, effective_weight)
# effective_weight == 0 means this feature abstains (data insufficient).

def _vote_er(
    er: Optional[float],
    closes: np.ndarray,
    er_window: int,
    trend_thr: float,
    range_thr: float,
    weight: float,
) -> Tuple[float, float, float, float]:
    if er is None:
        return 0.0, 0.0, 0.0, 0.0
    if er > trend_thr:
        # Direction: compare close[-1] to close at start of ER window
        ref_idx = -(er_window + 1)
        if len(closes) >= er_window + 1 and closes[-1] > closes[ref_idx]:
            return weight, 0.0, 0.0, weight
        return 0.0, weight, 0.0, weight
    if er < range_thr:
        return 0.0, 0.0, weight, weight
    return 0.0, 0.0, 0.0, weight


def _vote_ema(
    slope: Optional[float],
    threshold: float,
    weight: float,
) -> Tuple[float, float, float, float]:
    if slope is None:
        return 0.0, 0.0, 0.0, 0.0
    if slope > threshold:
        return weight, 0.0, 0.0, weight
    if slope < -threshold:
        return 0.0, weight, 0.0, weight
    return 0.0, 0.0, weight, weight


def _vote_hh_hl(
    score: Optional[float],
    up_thr: float,
    range_thr: float,
    weight: float,
) -> Tuple[float, float, float, float]:
    if score is None:
        return 0.0, 0.0, 0.0, 0.0
    if score > up_thr:
        return weight, 0.0, 0.0, weight
    if score < -up_thr:
        return 0.0, weight, 0.0, weight
    if abs(score) <= range_thr:
        return 0.0, 0.0, weight, weight
    return 0.0, 0.0, 0.0, weight


def _vote_breakout(
    ratio_up: Optional[float],
    ratio_down: Optional[float],
    cnt_up: int,
    cnt_down: int,
    min_count: int,
    follow_thr: float,
    fade_thr: float,
    weight: float,
) -> Tuple[float, float, float, float]:
    has_up   = ratio_up   is not None and cnt_up   >= min_count
    has_down = ratio_down is not None and cnt_down >= min_count
    if not has_up and not has_down:
        return 0.0, 0.0, 0.0, 0.0

    up = down = rng = 0.0
    if has_up   and ratio_up   > follow_thr:
        up   = weight
    if has_down and ratio_down > follow_thr:
        down = weight
    if has_up and has_down and ratio_up < fade_thr and ratio_down < fade_thr:
        rng = weight

    return up, down, rng, weight


def _vote_delta(
    persist_up: float,
    persist_down: float,
    consistency: float,
    persist_thr: float,
    consistency_thr: float,
    weight: float,
) -> Tuple[float, float, float, float]:
    if persist_up > persist_thr:
        return weight, 0.0, 0.0, weight
    if persist_down > persist_thr:
        return 0.0, weight, 0.0, weight
    if consistency < consistency_thr:
        return 0.0, 0.0, weight, weight
    return 0.0, 0.0, 0.0, weight


# ─── Public: trade enrichment ────────────────────────────────────────────────

def enrich_trades_with_regime(
    trade_list: List[Dict[str, Any]],
    klines: List[Kline],
    lookback: int = 50,
    **regime_params,
) -> List[Dict[str, Any]]:
    """
    在 simulate_trades() 回傳的 trade_list 每筆交易中加入 "regime" 欄位。

    使用進場時間（entry_time ms）往前取 lookback 根 K 棒計算 detect_regime()。
    原地修改並回傳同一份 trade_list，方便鏈式呼叫。

    範例：
        result = simulate_trades(signals, cfg)
        enrich_trades_with_regime(result["trade_list"], klines)
        # 之後 result["trade_list"][i]["regime"] 即為該筆交易進場時的市場狀態
    """
    if not klines or not trade_list:
        return trade_list

    # 建立 open_time 排序索引供二分搜尋
    times = [k.open_time for k in klines]

    for trade in trade_list:
        entry_time = trade.get("entry_time", 0)
        if not entry_time:
            trade["regime"] = "neutral"
            if not trade.get("trend_regime"):
                trade["trend_regime"] = "neutral"
            continue

        # 找到 entry_time 對應的 kline 位置（取 ≤ entry_time 的最後一根）
        idx = bisect.bisect_right(times, entry_time)
        if idx == 0:
            trade["regime"] = "neutral"
            if not trade.get("trend_regime"):
                trade["trend_regime"] = "neutral"
            continue

        window = klines[max(0, idx - lookback): idx]
        regime = detect_regime(window, **regime_params)
        trade["regime"] = regime
        if not trade.get("trend_regime"):
            trade["trend_regime"] = regime

    return trade_list


# ─── Internal: decision ───────────────────────────────────────────────────────

def _decide(
    score_up: float,
    score_down: float,
    score_range: float,
    effective_max: float,
    trend_thr: float,
    separation: float,
    range_thr: float,
) -> RegimeLabel:
    if effective_max <= 0:
        return "neutral"

    norm_up    = score_up    / effective_max
    norm_down  = score_down  / effective_max
    norm_range = score_range / effective_max
    best = max(norm_up, norm_down, norm_range)

    if norm_up == best and norm_up >= trend_thr and (norm_up - norm_down) >= separation:
        return "trend_up"
    if norm_down == best and norm_down >= trend_thr and (norm_down - norm_up) >= separation:
        return "trend_down"
    if norm_range == best and norm_range >= range_thr:
        return "range"
    return "neutral"
