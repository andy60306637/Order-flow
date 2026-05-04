"""
可跨 Pipeline 複用的共享計算元件 (SharedComponent)。

設計原則：
  - 每個 Component 只負責「計算」，不讀寫 PipelineContext，無策略偏好。
  - component_id 是全域唯一快取鍵；同 id 的元件在同一根 K 棒只算一次。
  - Stage 負責「過濾」：從 SharedContext 讀取結果，套用策略特定的邏輯。

Tick 資料支援：
  compute() 接受可選的 tick_map (TickBarMap)。
  支援 tick 的 Component 應在 tick_map=None 時提供 kline 估算 fallback，
  確保回測（有 tick）與實盤快速模式（僅 kline）都能執行。

內建 Components：
  ATRComponent        → atr_{period}       純 kline
  RegimeComponent     → regime             純 kline
  SessionComponent    → session            純 kline（時間戳）
  VolatilityComponent → volatility_{period} 純 kline
  TickDeltaComponent  → tick_delta         tick-first，kline fallback
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from core.data_types import Kline

TickBarMap = Mapping[int, np.ndarray]


class SharedComponent(ABC):
    """
    所有可共享計算元件的抽象基底。

    tick_map 格式：open_time_ms → ndarray(N, 4) [trade_time, price, qty, is_buyer_maker]
      is_buyer_maker=True  → 買方為 maker（被動），賣方主動成交 → sell aggressor
      is_buyer_maker=False → 賣方為 maker（被動），買方主動成交 → buy aggressor
    """

    component_id: str

    @abstractmethod
    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        """
        純計算，不產生副作用，不依賴外部狀態。
        回傳值會被 SharedContext.get_or_compute() 快取。

        tick_map=None 時必須提供合理的 fallback（kline 估算），
        確保所有 Component 在無 tick 環境下仍可執行。
        """
        ...


# ── ATRComponent ──────────────────────────────────────────────────────────────

class ATRComponent(SharedComponent):
    """
    平均真實範圍 (ATR)。

    回傳：
      atr     : float  絕對值（price unit）
      atr_pct : float  相對值（% of close）
    """

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self.component_id = f"atr_{period}"

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        start  = max(0, idx - self.period * 3)
        window = klines[start : idx + 1]

        if len(window) < 2:
            return {"atr": 0.0, "atr_pct": 0.0}

        trs: list[float] = []
        for i in range(1, len(window)):
            h, l, pc = window[i].high, window[i].low, window[i - 1].close
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))

        atr     = float(np.mean(trs[-self.period :])) if trs else 0.0
        mid     = klines[idx].close
        atr_pct = atr / mid * 100 if mid > 0 else 0.0

        return {"atr": atr, "atr_pct": atr_pct}


# ── RegimeComponent ───────────────────────────────────────────────────────────

class RegimeComponent(SharedComponent):
    """
    市場 Regime 分類。

    演算法：EMA slope（趨勢方向）+ ATR% 高低（波動強度）
    輸出 regime：
      "trending_bull"  EMA 向上 + 正常波動
      "trending_bear"  EMA 向下 + 正常波動
      "ranging"        EMA 平坦
      "volatile"       ATR% 超過閾值（不論方向）

    回傳：
      regime       : str
      ema_slope    : float  (相對斜率，5 bar EMA 變化率)
      ema          : float  (當前 EMA 值)
      atr_pct      : float  (ATR%)
    """

    component_id = "regime"

    def __init__(
        self,
        ema_period:       int   = 50,
        atr_period:       int   = 14,
        slope_threshold:  float = 0.0003,  # 趨勢判定斜率門檻（相對）
        vol_threshold_pct: float = 3.0,   # volatile 判定：ATR% 超過此值
    ) -> None:
        self.ema_period        = ema_period
        self.atr_period        = atr_period
        self.slope_threshold   = slope_threshold
        self.vol_threshold_pct = vol_threshold_pct

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        start  = max(0, idx - self.ema_period * 3)
        window = klines[start : idx + 1]

        if len(window) < max(self.ema_period, self.atr_period + 1):
            return {"regime": "ranging", "ema_slope": 0.0, "ema": 0.0, "atr_pct": 0.0}

        closes = np.array([k.close for k in window], dtype=float)

        # EMA (Wilder-style)
        alpha = 2.0 / (self.ema_period + 1)
        ema   = closes[0]
        for c in closes[1:]:
            ema = alpha * c + (1 - alpha) * ema

        # EMA slope：最後 5 bar 重算一次，取首尾差 / 首值
        slope_window = closes[-min(5, len(closes)):]
        ema_s = slope_window[0]
        ema_vals: list[float] = [ema_s]
        for c in slope_window[1:]:
            ema_s = alpha * c + (1 - alpha) * ema_s
            ema_vals.append(ema_s)
        slope = (ema_vals[-1] - ema_vals[0]) / (ema_vals[0] + 1e-10)

        # ATR
        trs: list[float] = []
        for i in range(1, len(window)):
            h, l, pc = window[i].high, window[i].low, window[i - 1].close
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr     = float(np.mean(trs[-self.atr_period :])) if trs else 0.0
        mid     = klines[idx].close
        atr_pct = atr / mid * 100 if mid > 0 else 0.0

        # 分類
        if atr_pct >= self.vol_threshold_pct:
            regime = "volatile"
        elif slope > self.slope_threshold:
            regime = "trending_bull"
        elif slope < -self.slope_threshold:
            regime = "trending_bear"
        else:
            regime = "ranging"

        return {
            "regime":    regime,
            "ema_slope": float(slope),
            "ema":       float(ema),
            "atr_pct":   float(atr_pct),
            "atr":       float(atr),
        }


# ── SessionComponent ──────────────────────────────────────────────────────────

class SessionComponent(SharedComponent):
    """
    UTC 時段識別。

    回傳：
      session         : str   主時段名稱
      active_sessions : list  所有重疊時段
      utc_hour        : int
    """

    component_id = "session"

    SESSION_HOURS: dict[str, tuple[int, int]] = {
        "asian":   (0,  8),
        "london":  (7,  16),
        "ny":      (13, 22),
        "overlap": (13, 16),  # London-NY 重疊
    }

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        ts_ms = klines[idx].open_time
        hour  = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour

        active = [
            name for name, (s, e) in self.SESSION_HOURS.items()
            if s <= hour < e
        ]
        if "overlap" in active:
            primary = "overlap"
        elif active:
            primary = active[0]
        else:
            primary = "off"

        return {
            "session":         primary,
            "active_sessions": active,
            "utc_hour":        hour,
        }


# ── VolatilityComponent ───────────────────────────────────────────────────────

class VolatilityComponent(SharedComponent):
    """
    已實現波動率 + 歷史百分位數。

    回傳：
      realized_vol    : float  滾動標準差（log return × sqrt(period)）
      vol_percentile  : float  0~100，目前波動率在歷史中的百分位
    """

    def __init__(self, period: int = 20, lookback: int = 100) -> None:
        self.period   = period
        self.lookback = lookback
        self.component_id = f"volatility_{period}"

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        start  = max(0, idx - self.lookback)
        window = klines[start : idx + 1]

        if len(window) < self.period + 1:
            return {"realized_vol": 0.0, "vol_percentile": 50.0}

        closes      = np.array([k.close for k in window], dtype=float)
        log_returns = np.diff(np.log(np.maximum(closes, 1e-10)))

        if len(log_returns) < self.period:
            return {"realized_vol": 0.0, "vol_percentile": 50.0}

        recent_vol = float(np.std(log_returns[-self.period :]) * np.sqrt(self.period))

        # 百分位：滾動計算歷史各時點的 realized_vol，排名
        all_vols = [
            float(np.std(log_returns[max(0, i - self.period) : i]))
            for i in range(self.period, len(log_returns) + 1)
        ]
        pct = float(np.mean([v <= recent_vol for v in all_vols]) * 100) if all_vols else 50.0

        return {"realized_vol": recent_vol, "vol_percentile": pct}


# ── TickDeltaComponent ────────────────────────────────────────────────────────

class TickDeltaComponent(SharedComponent):
    """
    單根 K 棒的成交量 Delta 分析。

    tick-first 設計：
      有 tick_map → 逐筆計算精確 delta（買方主動 − 賣方主動）
      無 tick_map → 用 taker_buy_volume 估算（kline fallback）

    回傳：
      delta      : float  買方主動量 − 賣方主動量（正=買壓，負=賣壓）
      buy_vol    : float  買方主動成交量
      sell_vol   : float  賣方主動成交量
      imbalance  : float  delta / total_vol，-1~1
      source     : str    "tick" | "kline_fallback"

    is_buyer_maker 欄位語意（Binance）：
      True  → 買方為被動方（掛單），賣方主動吃單 → sell aggressor
      False → 賣方為被動方（掛單），買方主動吃單 → buy aggressor
    """

    component_id = "tick_delta"

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        k = klines[idx]

        if tick_map is not None:
            ticks = tick_map.get(k.open_time)
            if ticks is not None and len(ticks) > 0:
                # ticks shape: (N, 4) → [trade_time, price, qty, is_buyer_maker]
                is_bm    = ticks[:, 3].astype(bool)
                sell_vol = float(np.sum(ticks[is_bm,  2]))   # buyer=maker → sell aggressor
                buy_vol  = float(np.sum(ticks[~is_bm, 2]))   # buyer=taker → buy aggressor
                total    = buy_vol + sell_vol
                delta    = buy_vol - sell_vol
                return {
                    "delta":     delta,
                    "buy_vol":   buy_vol,
                    "sell_vol":  sell_vol,
                    "imbalance": delta / (total + 1e-10),
                    "source":    "tick",
                }

        # kline fallback：taker_buy_volume 是買方主動成交量
        buy_vol  = k.taker_buy_volume
        sell_vol = k.volume - k.taker_buy_volume
        total    = k.volume
        delta    = buy_vol - sell_vol
        return {
            "delta":     delta,
            "buy_vol":   buy_vol,
            "sell_vol":  sell_vol,
            "imbalance": delta / (total + 1e-10),
            "source":    "kline_fallback",
        }


# ── TickVWAPComponent ─────────────────────────────────────────────────────────

class TickVWAPComponent(SharedComponent):
    """
    單根 K 棒的 tick-level VWAP 與價格分佈。

    tick-first 設計：
      有 tick_map → 精確計算 VWAP（成交量加權均價）
      無 tick_map → 用 (H+L+C)/3 × volume 估算

    回傳：
      vwap       : float  成交量加權均價
      vwap_dev   : float  收盤價偏離 VWAP 的程度（(close-vwap)/vwap）
      tick_count : int    本根 K 棒 tick 數（無 tick 時為 0）
      source     : str    "tick" | "kline_fallback"
    """

    component_id = "tick_vwap"

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        k = klines[idx]

        if tick_map is not None:
            ticks = tick_map.get(k.open_time)
            if ticks is not None and len(ticks) > 0:
                prices  = ticks[:, 1]
                volumes = ticks[:, 2]
                total_vol = float(np.sum(volumes))
                vwap      = float(np.dot(prices, volumes) / total_vol) if total_vol > 0 else k.close
                vwap_dev  = (k.close - vwap) / vwap if vwap > 0 else 0.0
                return {
                    "vwap":       vwap,
                    "vwap_dev":   vwap_dev,
                    "tick_count": len(ticks),
                    "source":     "tick",
                }

        # kline fallback：典型 OHLC VWAP 近似
        vwap     = (k.high + k.low + k.close) / 3.0
        vwap_dev = (k.close - vwap) / vwap if vwap > 0 else 0.0
        return {
            "vwap":       vwap,
            "vwap_dev":   vwap_dev,
            "tick_count": 0,
            "source":     "kline_fallback",
        }
