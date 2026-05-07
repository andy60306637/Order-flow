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
  ATRComponent                  → atr_{period}            純 kline
  RegimeComponent               → regime                  純 kline
  SessionComponent              → session                 純 kline（時間戳）
  VolatilityComponent           → volatility_{period}     純 kline
  MarketVolatilityRegimeComponent → market_vol_regime     純 kline
  MicroVolatilityComponent      → micro_volatility_{period}_l{N} L2 snapshot-first
  TickDeltaComponent            → tick_delta              tick-first，kline fallback
  TickVWAPComponent             → tick_vwap               tick-first，kline fallback
  VWAPDeviationComponent        → vwap_dev_{window}_{lookback}  tick-first，kline fallback
  VolumeProfileComponent        → volume_profile_*        tick-first，kline fallback
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Optional

import numpy as np

from core.data_types import Kline
from core.micro_volatility import MicroVolatilityEngine
from core.volume_profile import VolumeProfile, build_composite_profile, build_volume_profile

TickBarMap = Mapping[int, np.ndarray]
MicroSnapshotMap = Mapping[int, Any]


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


class RegimeClassifier(SharedComponent, ABC):
    """
    Regime 多維度分類器基底。

    繼承此類別的 Component 代表市場狀態的一個維度（趨勢、時段、波動…），
    可被 RegimeStage 組合，每個維度獨立計算並過濾。

    規範：
      - dimension  : 唯一維度名稱，作為 ctx.regime[dimension] 的鍵
      - compute()  : 回傳 dict 必須包含 "label" 鍵，代表此維度的分類結果（str）
                     其餘鍵為詳細數值，存入 ctx.regime_meta[dimension]
    """

    dimension: str


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

class RegimeComponent(RegimeClassifier):
    """
    趨勢 Regime 分類器（dimension = "trend"）。

    演算法：EMA slope（趨勢方向）+ ATR% 高低（波動強度）
    label：
      "trending_bull"  EMA 向上 + 正常波動
      "trending_bear"  EMA 向下 + 正常波動
      "ranging"        EMA 平坦
      "volatile"       ATR% 超過閾值（不論方向）

    回傳：
      label        : str   （= regime，供 RegimeStage 過濾）
      regime       : str   （同 label，向下相容）
      ema_slope    : float
      ema          : float
      atr_pct      : float
      atr          : float
    """

    component_id = "regime"
    dimension    = "trend"

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
            "label":     regime,   # RegimeClassifier 標準鍵
            "regime":    regime,   # 向下相容
            "ema_slope": float(slope),
            "ema":       float(ema),
            "atr_pct":   float(atr_pct),
            "atr":       float(atr),
        }


# ── SessionComponent ──────────────────────────────────────────────────────────

class SessionComponent(RegimeClassifier):
    """
    交易時段分類器（dimension = "session"）。

    完整支援美國（EST/EDT）與英國（GMT/BST）DST。

    時段定義（以各地交易所本地時間為準）：
      asian   : 09:00–18:00 Asia/Tokyo
      london  : 08:00–17:00 Europe/London  （自動切換 GMT/BST）
      ny      : 08:00–17:00 America/New_York（自動切換 EST/EDT）

    回傳：
      label           : str   （= session，供 RegimeStage 過濾）
      session         : str   主時段名稱（overlap > ny > london > asian > off）
      active_sessions : list  所有當前活躍時段
      utc_hour        : int   UTC 小時
      london_hour     : int   倫敦本地小時
      ny_hour         : int   紐約本地小時
    """

    component_id = "session"
    dimension    = "session"

    _TZ_LONDON = ZoneInfo("Europe/London")
    _TZ_NY     = ZoneInfo("America/New_York")
    _TZ_TOKYO  = ZoneInfo("Asia/Tokyo")

    # 各時段以本地時間定義（start_hour, end_hour），end_hour 不含
    _LONDON_HOURS = (8, 17)   # 倫敦本地：08:00–17:00（BST/GMT 自動切換）
    _NY_HOURS     = (8, 17)   # 紐約本地：08:00–17:00（EDT/EST 自動切換）
    _ASIAN_HOURS  = (9, 18)   # 東京本地：09:00–18:00 JST（= UTC 00:00–09:00，日本無 DST）

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        ts_ms = klines[idx].open_time
        dt_utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

        london_hour = dt_utc.astimezone(self._TZ_LONDON).hour
        ny_hour     = dt_utc.astimezone(self._TZ_NY).hour
        tokyo_hour  = dt_utc.astimezone(self._TZ_TOKYO).hour

        is_london = self._LONDON_HOURS[0] <= london_hour < self._LONDON_HOURS[1]
        is_ny     = self._NY_HOURS[0]     <= ny_hour     < self._NY_HOURS[1]
        is_asian  = self._ASIAN_HOURS[0]  <= tokyo_hour  < self._ASIAN_HOURS[1]

        active: list[str] = []
        if is_asian:
            active.append("asian")
        if is_london:
            active.append("london")
        if is_ny:
            active.append("ny")

        if is_london and is_ny:
            primary = "overlap"
        elif is_ny:
            primary = "ny"
        elif is_london:
            primary = "london"
        elif is_asian:
            primary = "asian"
        else:
            primary = "off"

        return {
            "label":           primary,   # RegimeClassifier 標準鍵
            "session":         primary,   # 向下相容
            "active_sessions": active,
            "utc_hour":        dt_utc.hour,
            "london_hour":     london_hour,
            "ny_hour":         ny_hour,
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


# ── MicroVolatilityComponent ─────────────────────────────────────────────────

class MicroVolatilityComponent(SharedComponent):
    """
    Microstructural fragility component.

    This is a pipeline wrapper around ``MicroVolatilityEngine``. In live mode,
    prefer using the engine directly and feeding it every order-book/trade
    update. In backtest/pipeline mode, pass ``snapshot_map`` keyed by
    ``Kline.open_time``. Each value can be:
      {"orderbook": {...}, "trade": {...}}
      (orderbook_snapshot, trade_snapshot)
      one dict containing both order-book and trade fields

    If no L2 snapshot is available, kline fallback is used only to keep the
    pipeline stable; ``source`` will be ``"kline_fallback"``.
    """

    def __init__(
        self,
        period_label: str = "15m",
        window_size: int = 15,
        normalization_window: int = 100,
        top_n: int = 10,
        weights: tuple[float, float, float] = (0.34, 0.33, 0.33),
        snapshot_map: Optional[MicroSnapshotMap] = None,
        use_kline_fallback: bool = True,
    ) -> None:
        self.period_label = period_label
        self.window_size = window_size
        self.normalization_window = normalization_window
        self.top_n = top_n
        self.weights = weights
        self.snapshot_map = snapshot_map
        self.use_kline_fallback = use_kline_fallback
        safe_period = period_label.lower().replace(" ", "")
        self.component_id = f"micro_volatility_{safe_period}_l{top_n}"

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        engine = MicroVolatilityEngine(
            window_size=self.window_size,
            normalization_window=self.normalization_window,
            top_n=self.top_n,
            weights=self.weights,
        )
        lookback = self.window_size + self.normalization_window
        start = max(0, idx - lookback + 1)
        source = "missing_snapshot"
        updates = 0

        for k in klines[start : idx + 1]:
            orderbook_snapshot, trade_snapshot, item_source = self._snapshots_for_kline(k)
            if orderbook_snapshot is None:
                continue
            engine.update(orderbook_snapshot, trade_snapshot)
            source = item_source
            updates += 1

        result = engine.snapshot()
        result.update({
            "source": source,
            "updates": updates,
            "period": self.period_label,
            "window_size": self.window_size,
            "normalization_window": self.normalization_window,
            "top_n": self.top_n,
        })
        return result

    def _snapshots_for_kline(self, k: Kline) -> tuple[Optional[Any], Optional[Any], str]:
        if self.snapshot_map is not None:
            item = self.snapshot_map.get(k.open_time)
            if item is not None:
                orderbook, trade = self._parse_snapshot_item(item)
                if orderbook is not None:
                    return orderbook, trade, "snapshot"

        if not self.use_kline_fallback:
            return None, None, "missing_snapshot"

        spread = max(k.high - k.low, 0.0)
        half_spread = spread / 2.0
        orderbook_snapshot = {
            "best_bid_price": max(k.close - half_spread, 0.0),
            "best_ask_price": k.close + half_spread,
            "bids_volume_top_N": max(k.volume - k.taker_buy_volume, 0.0),
            "asks_volume_top_N": max(k.taker_buy_volume, 0.0),
        }
        trade_snapshot = {
            "taker_buy_volume": max(k.taker_buy_volume, 0.0),
            "taker_sell_volume": max(k.volume - k.taker_buy_volume, 0.0),
        }
        return orderbook_snapshot, trade_snapshot, "kline_fallback"

    @staticmethod
    def _parse_snapshot_item(item: Any) -> tuple[Optional[Any], Optional[Any]]:
        if isinstance(item, tuple) and len(item) >= 2:
            return item[0], item[1]
        if isinstance(item, list) and len(item) >= 2:
            return item[0], item[1]
        if isinstance(item, Mapping):
            orderbook = (
                item.get("orderbook")
                or item.get("orderbook_snapshot")
                or item.get("book")
            )
            trade = (
                item.get("trade")
                or item.get("trade_snapshot")
                or item.get("trades")
            )
            if orderbook is not None:
                return orderbook, trade
            return item, item
        return item, None


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


# ── VWAPDeviationComponent ────────────────────────────────────────────────────

class VWAPDeviationComponent(SharedComponent):
    """
    滾動 VWAP 乖離帶分類（tick-first，kline fallback）。

    以過去 window 根 K 棒建立滾動 VWAP，計算當前棒的相對乖離 vwap_dev，
    並以 lookback 根歷史棒的相對乖離分布估計 σ，得到真正的 z-score。

    z-score 計算（單位一致）：
      vwap_i   = 歷史第 i 棒的滾動 kline VWAP（window 根）
      dev_i    = (close_i − vwap_i) / vwap_i          ← 相對乖離，無單位
      σ        = std(dev_i  for i in [idx−lookback, idx−1])
      z_score  = vwap_dev_current / σ

    σ 估計固定用 kline（歷史掃描效率考量）；當前棒 VWAP 仍 tick-first。

    Zone 定義（OVEREXTENDED_LOW / HIGH 可覆蓋類別屬性）：
      normal                  |z| < 1.0
      extended_high/low       1.0 ≤ |z| < 2.0
      overextended_high/low   2.0 ≤ |z| ≤ 2.5   ← 極端乖離區
      extreme_high/low        |z| > 2.5

    回傳：
      vwap             : float  當前滾動 VWAP
      vwap_dev         : float  (close − vwap) / vwap
      z_score          : float  vwap_dev / σ
      sigma            : float  歷史相對乖離的標準差（與 vwap_dev 同單位）
      zone             : str    區帶名稱（見上）
      in_overextended  : bool   2.0 ≤ |z| ≤ 2.5
      above_vwap       : bool   close > vwap
      source           : str    "tick" | "kline_fallback" | "insufficient_data"
    """

    OVEREXTENDED_LOW:  float = 2.0
    OVEREXTENDED_HIGH: float = 2.5

    def __init__(
        self,
        window:            int            = 24,
        lookback:          int            = 100,
        overextended_low:  Optional[float] = None,
        overextended_high: Optional[float] = None,
    ) -> None:
        self.window   = window
        self.lookback = lookback
        self.oe_low   = self.OVEREXTENDED_LOW  if overextended_low  is None else overextended_low
        self.oe_high  = self.OVEREXTENDED_HIGH if overextended_high is None else overextended_high
        self.component_id = f"vwap_dev_{window}_{lookback}"

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        current_price = klines[idx].close

        if idx < self.window - 1:
            return self._empty_result(current_price)

        # ── 1. 當前棒 VWAP（tick-first）────────────────────────────────────
        cur_window = klines[max(0, idx - self.window + 1) : idx + 1]
        if tick_map is not None:
            vwap = self._tick_vwap(cur_window, tick_map)
            source = "tick" if vwap is not None else "kline_fallback"
        else:
            vwap, source = None, "kline_fallback"

        if vwap is None:
            vwap = self._kline_vwap(cur_window)

        if vwap is None:
            return self._empty_result(current_price)

        vwap_dev = (current_price - vwap) / vwap if vwap > 0 else 0.0

        # ── 2. 歷史相對乖離 → σ（純 kline，效率考量）─────────────────────
        hist_start = max(self.window - 1, idx - self.lookback)
        hist_devs: list[float] = []
        for j in range(hist_start, idx):          # 不含當前棒
            w = klines[max(0, j - self.window + 1) : j + 1]
            hv = self._kline_vwap(w)
            if hv is not None and hv > 0:
                hist_devs.append((klines[j].close - hv) / hv)

        sigma   = float(np.std(hist_devs)) if len(hist_devs) > 1 else 0.0
        z_score = vwap_dev / (sigma + 1e-10)
        abs_z   = abs(z_score)

        return {
            "vwap":            float(vwap),
            "vwap_dev":        float(vwap_dev),
            "z_score":         float(z_score),
            "sigma":           float(sigma),
            "zone":            self._classify_zone(z_score),
            "in_overextended": self.oe_low <= abs_z <= self.oe_high,
            "above_vwap":      current_price > vwap,
            "source":          source,
        }

    def _kline_vwap(self, klines: list[Kline]) -> Optional[float]:
        """Typical-price VWAP from OHLCV klines."""
        pv = tv = 0.0
        for k in klines:
            if k.volume > 0:
                pv += (k.high + k.low + k.close) / 3.0 * k.volume
                tv += k.volume
        return pv / tv if tv > 0 else None

    def _tick_vwap(self, klines: list[Kline], tick_map: TickBarMap) -> Optional[float]:
        """Bar-level tick VWAP; falls back to typical price per bar when ticks missing.

        Returns None if no real tick data was found (signals caller to use kline path).
        """
        pv = tv = 0.0
        tick_vol = 0.0
        for k in klines:
            ticks = tick_map.get(k.open_time)
            if ticks is not None and len(ticks) > 0:
                vols = ticks[:, 2]
                v    = float(np.sum(vols))
                if v > 0:
                    pv      += float(np.dot(ticks[:, 1], vols))
                    tv      += v
                    tick_vol += v
            elif k.volume > 0:
                pv += (k.high + k.low + k.close) / 3.0 * k.volume
                tv += k.volume
        if tick_vol == 0.0:
            return None   # no real ticks; let caller fall back to kline_vwap
        return pv / tv if tv > 0 else None

    def _classify_zone(self, z_score: float) -> str:
        abs_z     = abs(z_score)
        direction = "high" if z_score >= 0 else "low"
        if abs_z > self.oe_high:
            return f"extreme_{direction}"
        if abs_z >= self.oe_low:
            return f"overextended_{direction}"
        if abs_z >= 1.0:
            return f"extended_{direction}"
        return "normal"

    def _empty_result(self, current_price: float) -> dict:
        return {
            "vwap":            current_price,
            "vwap_dev":        0.0,
            "z_score":         0.0,
            "sigma":           0.0,
            "zone":            "normal",
            "in_overextended": False,
            "above_vwap":      False,
            "source":          "insufficient_data",
        }


# ── VolumeProfileComponent ────────────────────────────────────────────────────

class VolumeProfileComponent(SharedComponent):
    """
    滾動 Volume Profile（tick-first，kline fallback）。

    以過去 window 根 K 棒建立 composite Volume Profile，計算交易密集區（Value Area）、
    POC、VAH、VAL 及 HVN / LVN 節點。

    觸碰帶（touch band）：
      POC / VAH / VAL 以帶寬 (current_price × touch_band_pct) 取代單一價格觸碰，
      回傳 price_in_xxx_band 布林值供策略層直接使用。
      touch_band_pct 預設值由 DEFAULT_TOUCH_BAND_PCT 決定，程式員可修改類別屬性覆蓋。

    回傳：
      poc_price         : float
      vah               : float
      val               : float
      poc_band          : tuple[float, float]  (低界, 高界)
      vah_band          : tuple[float, float]
      val_band          : tuple[float, float]
      price_in_poc_band : bool
      price_in_vah_band : bool
      price_in_val_band : bool
      hvn_prices        : list[float]
      lvn_prices        : list[float]
      in_value_area     : bool
      above_poc         : bool
      total_volume      : float
      source            : str  "tick" | "kline_fallback" | "insufficient_data"
    """

    DEFAULT_TOUCH_BAND_PCT: float = 0.001  # 0.1% — 程式員可覆蓋此類別屬性

    def __init__(
        self,
        interval: str = "1h",
        window: int = 24,
        tick_size: float = 1.0,
        value_area_pct: float = 0.70,
        touch_band_pct: Optional[float] = None,
        hvn_threshold: float = 1.5,
        lvn_threshold: float = 0.5,
    ) -> None:
        self.interval       = interval
        self.window         = window
        self.tick_size      = tick_size
        self.value_area_pct = value_area_pct
        self.touch_band_pct = self.DEFAULT_TOUCH_BAND_PCT if touch_band_pct is None else touch_band_pct
        self.hvn_threshold  = hvn_threshold
        self.lvn_threshold  = lvn_threshold

        safe_interval = interval.lower().replace(" ", "")
        va_pct = int(round(value_area_pct * 100))
        tb_bp  = int(round(self.touch_band_pct * 10000))
        self.component_id = f"volume_profile_{safe_interval}_{window}_va{va_pct}_tb{tb_bp}"

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        start          = max(0, idx - self.window + 1)
        window_klines  = klines[start : idx + 1]
        current_price  = klines[idx].close

        vp: Optional[VolumeProfile] = None
        source = "insufficient_data"

        if tick_map is not None:
            open_times = [k.open_time for k in window_klines]
            vp = build_composite_profile(
                tick_map, open_times,
                self.tick_size, self.value_area_pct,
                self.hvn_threshold, self.lvn_threshold,
            )
            if vp is not None:
                source = "tick"

        if vp is None:
            vp = self._build_from_klines(window_klines)
            if vp is not None:
                source = "kline_fallback"

        if vp is None:
            return self._empty_result(current_price)

        band_size = current_price * self.touch_band_pct

        return {
            "poc_price":         vp.poc_price,
            "vah":               vp.vah,
            "val":               vp.val,
            "poc_band":          (vp.poc_price - band_size, vp.poc_price + band_size),
            "vah_band":          (vp.vah - band_size, vp.vah + band_size),
            "val_band":          (vp.val - band_size, vp.val + band_size),
            "price_in_poc_band": abs(current_price - vp.poc_price) <= band_size,
            "price_in_vah_band": abs(current_price - vp.vah) <= band_size,
            "price_in_val_band": abs(current_price - vp.val) <= band_size,
            "hvn_prices":        vp.hvn_prices,
            "lvn_prices":        vp.lvn_prices,
            "in_value_area":     vp.is_in_value_area(current_price),
            "above_poc":         current_price > vp.poc_price,
            "total_volume":      vp.total_volume,
            "source":            source,
        }

    def _build_from_klines(self, klines: list[Kline]) -> Optional[VolumeProfile]:
        rows: list[list[float]] = []
        for k in klines:
            if k.volume <= 0:
                continue
            typical = (k.high + k.low + k.close) / 3.0
            buy_vol  = k.taker_buy_volume
            sell_vol = k.volume - k.taker_buy_volume
            if buy_vol > 0:
                rows.append([float(k.open_time), typical, buy_vol, 0.0])
            if sell_vol > 0:
                rows.append([float(k.open_time), typical, sell_vol, 1.0])

        if not rows:
            return None

        ticks = np.array(rows, dtype=float)
        return build_volume_profile(
            ticks, self.tick_size, self.value_area_pct,
            self.hvn_threshold, self.lvn_threshold,
        )

    def _empty_result(self, current_price: float) -> dict:
        return {
            "poc_price":         current_price,
            "vah":               current_price,
            "val":               current_price,
            "poc_band":          (current_price, current_price),
            "vah_band":          (current_price, current_price),
            "val_band":          (current_price, current_price),
            "price_in_poc_band": False,
            "price_in_vah_band": False,
            "price_in_val_band": False,
            "hvn_prices":        [],
            "lvn_prices":        [],
            "in_value_area":     False,
            "above_poc":         False,
            "total_volume":      0.0,
            "source":            "insufficient_data",
        }


# ── MarketVolatilityRegimeComponent ──────────────────────────────────────────

class MarketVolatilityRegimeComponent(RegimeClassifier):
    """
    市場波動率環境分類器（dimension = "market_vol_regime"）。

    綜合五個指標判斷市場的波動率環境（方向性擴張 / 均值回歸 / 壓縮 / 混沌）。

    Regime 定義：
      MEAN_REVERSION   rv60_pct < 60  & atr10_atr60 < 1.2 & er30 < 0.30 & adx14 < 25
      BREAKOUT_TREND   rv60_pct >= 60 & atr10_atr60 > 1.3 & er30 > 0.40 & adx14 > 25
      CHAOTIC_HIGH_VOL rv60_pct >= 85 & atr10_atr60 > 1.5 & er30 < 0.30
      COMPRESSION_WAIT rv60_pct < 30  & bb_width_pct < 20
      NEUTRAL          以上條件均不滿足

    回傳：
      label        : str   Regime 名稱（= regime，供 RegimeStage 過濾）
      regime       : str   同 label，向下相容
      rv60_pct     : float 已實現波動率(rv_period) 百分位（0–100）
      atr10_atr60  : float ATR(atr_short) / ATR(atr_long) 擴張比
      er30         : float Kaufman 效率比（0–1；越高越趨勢）
      adx14        : float ADX(adx_period)
      bb_width_pct : float Bollinger 帶寬百分位（0–100）
      atr10        : float ATR(atr_short) 絕對值
      atr60        : float ATR(atr_long)  絕對值
    """

    component_id = "market_vol_regime"
    dimension    = "market_vol_regime"

    def __init__(
        self,
        rv_period:  int = 60,
        atr_short:  int = 10,
        atr_long:   int = 60,
        er_period:  int = 30,
        adx_period: int = 14,
        bb_period:  int = 20,
        lookback:   int = 100,
    ) -> None:
        self.rv_period  = rv_period
        self.atr_short  = atr_short
        self.atr_long   = atr_long
        self.er_period  = er_period
        self.adx_period = adx_period
        self.bb_period  = bb_period
        self.lookback   = lookback

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        min_bars = max(
            self.rv_period + self.lookback,
            self.atr_long * 3,
            self.adx_period * 3,
            self.bb_period + self.lookback,
        )
        start  = max(0, idx - min_bars + 1)
        window = klines[start : idx + 1]

        min_need = max(
            self.rv_period + 2,
            self.atr_long + 2,
            self.er_period + 2,
            self.adx_period * 2 + 2,
        )
        if len(window) < min_need:
            return self._neutral_result()

        closes = np.array([k.close for k in window], dtype=float)
        highs  = np.array([k.high  for k in window], dtype=float)
        lows   = np.array([k.low   for k in window], dtype=float)

        rv60_pct     = self._rv_percentile(closes, self.rv_period, self.lookback)
        atr10        = self._wilder_atr(highs, lows, closes, self.atr_short)
        atr60        = self._wilder_atr(highs, lows, closes, self.atr_long)
        atr10_atr60  = atr10 / (atr60 + 1e-10)
        er30         = self._efficiency_ratio(closes, self.er_period)
        adx14        = self._adx(highs, lows, closes, self.adx_period)
        bb_width_pct = self._bb_width_percentile(closes, self.bb_period, self.lookback)

        if rv60_pct < 60 and atr10_atr60 < 1.2 and er30 < 0.30 and adx14 < 25:
            regime = "MEAN_REVERSION"
        elif rv60_pct >= 60 and atr10_atr60 > 1.3 and er30 > 0.40 and adx14 > 25:
            regime = "BREAKOUT_TREND"
        elif rv60_pct >= 85 and atr10_atr60 > 1.5 and er30 < 0.30:
            regime = "CHAOTIC_HIGH_VOL"
        elif rv60_pct < 30 and bb_width_pct < 20:
            regime = "COMPRESSION_WAIT"
        else:
            regime = "NEUTRAL"

        return {
            "label":        regime,
            "regime":       regime,
            "rv60_pct":     float(rv60_pct),
            "atr10_atr60":  float(atr10_atr60),
            "er30":         float(er30),
            "adx14":        float(adx14),
            "bb_width_pct": float(bb_width_pct),
            "atr10":        float(atr10),
            "atr60":        float(atr60),
        }

    # ── indicator helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _wilder_atr(
        highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int
    ) -> float:
        """Wilder-smoothed ATR scalar (last value)."""
        if len(highs) < period + 1:
            return 0.0
        prev_c = closes[:-1]
        tr = np.maximum.reduce([
            highs[1:] - lows[1:],
            np.abs(highs[1:] - prev_c),
            np.abs(lows[1:]  - prev_c),
        ])
        val = float(np.mean(tr[:period]))
        for v in tr[period:]:
            val = (val * (period - 1) + v) / period
        return val

    @staticmethod
    def _efficiency_ratio(closes: np.ndarray, period: int) -> float:
        """Kaufman Efficiency Ratio: net move / sum of bar-by-bar moves."""
        if len(closes) < period + 1:
            return 0.5
        net  = abs(float(closes[-1] - closes[-(period + 1)]))
        path = float(np.sum(np.abs(np.diff(closes[-(period + 1):]))))
        return net / (path + 1e-10)

    @staticmethod
    def _adx(
        highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int
    ) -> float:
        """ADX scalar (last value) via full Wilder-smooth of DX series."""
        n = len(highs)
        if n < period * 2 + 2:
            return 0.0

        tr       = np.zeros(n)
        plus_dm  = np.zeros(n)
        minus_dm = np.zeros(n)
        for i in range(1, n):
            h, l, pc    = highs[i], lows[i], closes[i - 1]
            tr[i]        = max(h - l, abs(h - pc), abs(l - pc))
            up            = highs[i] - highs[i - 1]
            dn            = lows[i - 1] - lows[i]
            plus_dm[i]  = up if up > dn and up > 0 else 0.0
            minus_dm[i] = dn if dn > up and dn > 0 else 0.0

        # Wilder smoothing: seed at index `period` using bars 1..period
        def _ws(arr: np.ndarray) -> np.ndarray:
            out = np.full(n, np.nan)
            if n < period + 1:
                return out
            out[period] = float(np.mean(arr[1: period + 1]))
            for i in range(period + 1, n):
                out[i] = (out[i - 1] * (period - 1) + arr[i]) / period
            return out

        atr_s   = _ws(tr)
        plus_s  = _ws(plus_dm)
        minus_s = _ws(minus_dm)

        eps      = 1e-10
        plus_di  = np.where(atr_s > 0, 100.0 * plus_s  / (atr_s + eps), 0.0)
        minus_di = np.where(atr_s > 0, 100.0 * minus_s / (atr_s + eps), 0.0)
        di_sum   = plus_di + minus_di
        dx       = np.where(di_sum > 0, 100.0 * np.abs(plus_di - minus_di) / (di_sum + eps), 0.0)

        # ADX = Wilder smooth of DX; seed with mean of first `period` valid DX values
        seed_i  = 2 * period - 1  # first `period` valid DX values span [period, 2*period-1]
        if seed_i >= n:
            return 0.0
        adx_arr = np.full(n, np.nan)
        adx_arr[seed_i] = float(np.mean(dx[period: seed_i + 1]))
        for i in range(seed_i + 1, n):
            adx_arr[i] = (adx_arr[i - 1] * (period - 1) + dx[i]) / period

        valid = adx_arr[~np.isnan(adx_arr)]
        return float(valid[-1]) if len(valid) > 0 else 0.0

    @staticmethod
    def _rv_percentile(closes: np.ndarray, period: int, lookback: int) -> float:
        """Percentile rank (0–100) of current realized-vol vs trailing history."""
        n = len(closes)
        if n < period + 1:
            return 50.0

        log_ret = np.zeros(n)
        valid   = (closes[:-1] > 0) & (closes[1:] > 0)
        log_ret[1:][valid] = np.log(closes[1:][valid] / closes[:-1][valid])

        rv = np.full(n, np.nan)
        for i in range(period - 1, n):
            rv[i] = float(np.std(log_ret[i - period + 1: i + 1]))

        current = rv[-1]
        if np.isnan(current):
            return 50.0

        hist = rv[~np.isnan(rv)][:-1][-lookback:]   # exclude current bar
        if len(hist) == 0:
            return 50.0
        return float(np.sum(hist < current) / len(hist) * 100.0)

    @staticmethod
    def _bb_width_percentile(closes: np.ndarray, bb_period: int, lookback: int) -> float:
        """Percentile rank (0–100) of current Bollinger Band width vs trailing history."""
        n = len(closes)
        if n < bb_period:
            return 50.0

        bw = np.full(n, np.nan)
        for i in range(bb_period - 1, n):
            w  = closes[i - bb_period + 1: i + 1]
            ma = float(np.mean(w))
            if ma <= 0:
                continue
            bw[i] = 4.0 * float(np.std(w)) / ma * 100.0   # expressed as % of price

        current = bw[-1]
        if np.isnan(current):
            return 50.0

        hist = bw[~np.isnan(bw)][:-1][-lookback:]
        if len(hist) == 0:
            return 50.0
        return float(np.sum(hist < current) / len(hist) * 100.0)

    def _neutral_result(self) -> dict:
        return {
            "label":        "NEUTRAL",
            "regime":       "NEUTRAL",
            "rv60_pct":     50.0,
            "atr10_atr60":  1.0,
            "er30":         0.5,
            "adx14":        0.0,
            "bb_width_pct": 50.0,
            "atr10":        0.0,
            "atr60":        0.0,
        }
