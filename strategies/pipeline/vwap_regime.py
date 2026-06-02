"""
strategies/pipeline/vwap_regime.py — VWAP Reclaim 均值回歸 Pipeline 策略

VWAP 雙層架構：
  Macro VWAP   — GMT+0（00:00 UTC）起累積的日內 VWAP，每日重設；需累積 ≥ 1h K 棒才生效
  Rolling VWAP — 前 rolling_window 根 K 棒的滾動 VWAP（預設 240 = 4h@1m）

五個 RegimeClassifier（環境確認因子）：
  macro_vwap_zone  Macro VWAP 位置  close < macro_vwap + 日初 1h 累積門檻（做多必要條件）
  vwap_z_score     乖離率因子       Rolling VWAP Z-score 區帶
  vwap_slope       斜率動量因子     Rolling VWAP 線性斜率（趨勢方向性）
  vp_density       流動性密度因子   Rolling VP 當前價位的相對成交密度
  poc_vwap_dist    距離因子         距 Rolling VP POC 與 Macro VWAP 的 ATR 倍數分類

Pipeline（VWAP Reclaim 均值回歸）：
  Gate 1  PositionGateStage  Gate 2  CooldownStage
  Stage 1 RegimeStage [五因子]
  Stage 2 AlphaStage — VWAPReclaimSignal（Rolling VWAP −2σ 穿越 → reclaim 回帶內）
  Stage 3 VWAPReclaimEntryManagementStage（|z| 近期極值確認 + VWAP 斜率平坦）
  Stage 4 RRStage(2:1)  止損 = reclaim bar low
  Stage 5 VWAPDistanceFeeGateStage（以 |price→Macro VWAP| 覆蓋往返手續費）
"""
from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from core.data_types import Kline
from strategies.base import StrategySignal, TickBarMap
from strategies.modules.capital_management import CapitalConfig, CapitalModule
from strategies.modules.exit_management import ExitConfig, ExitModule
from strategies.modules.signal_trigger import SignalModule
from strategies.pipeline.component import (
    ATRComponent,
    RegimeClassifier,
    SessionComponent,
    SharedComponent,
    VWAPDeviationComponent,
)
from strategies.pipeline.context import PipelineContext
from strategies.pipeline.definition import PipelineDef
from strategies.pipeline.pipeline import TradingPipeline
from strategies.pipeline.runner import MultiPipelineRunner
from strategies.pipeline.stages import (
    AlphaStage,
    CooldownStage,
    PipelineStage,
    PositionGateStage,
    RegimeStage,
    RRStage,
)
from strategies.pipeline.strategy import MultiPipelineStrategy
from strategies.pipeline.mean_reversion import _mr_long_entry


# ── MacroVWAPComponent ────────────────────────────────────────────────────────

class MacroVWAPComponent(SharedComponent):
    """
    日內 Macro VWAP（GMT+0 00:00 UTC 每日重設）。

    從本 UTC 日 00:00:00 起，逐根 K 棒累積典型價格 × 成交量。
    Tick-first：tick_map 存在時用 tick 精算；否則使用 (H+L+C)/3 × Volume。

    增量優化：順序存取時 O(1) per bar；換日或非順序時才全量掃描。

    Returns:
      macro_vwap     : float  日內累積 VWAP
      macro_vwap_dev : float  (close − macro_vwap) / macro_vwap
      session_bars   : int    本 UTC 日已計入的 K 棒數
      source         : str    "tick" | "kline_fallback" | "insufficient_data"
    """

    component_id = "macro_vwap"

    def __init__(self) -> None:
        self._day_start_ms: int   = -1
        self._pv:           float = 0.0
        self._tv:           float = 0.0
        self._tick_vol:     float = 0.0
        self._session_bars: int   = 0
        self._last_idx:     int   = -1

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        ts_ms  = klines[idx].open_time
        dt_utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        day_start_ms = int(
            datetime(dt_utc.year, dt_utc.month, dt_utc.day, tzinfo=timezone.utc)
            .timestamp() * 1000
        )

        if day_start_ms != self._day_start_ms or idx != self._last_idx + 1:
            # 換日或非順序存取：重設並從當日起點全量掃描
            self._day_start_ms = day_start_ms
            self._pv = self._tv = self._tick_vol = 0.0
            self._session_bars = 0
            scan = idx
            while scan > 0 and klines[scan - 1].open_time >= day_start_ms:
                scan -= 1
            for i in range(scan, idx + 1):
                self._accumulate(klines[i], tick_map)
                self._session_bars += 1
        else:
            # 順序存取：只加入最新一根
            self._accumulate(klines[idx], tick_map)
            self._session_bars += 1

        self._last_idx = idx

        cp = klines[idx].close
        if self._tv <= 0:
            return {
                "macro_vwap":   cp,  "macro_vwap_dev": 0.0,
                "session_bars": self._session_bars, "source": "insufficient_data",
            }

        macro_vwap     = self._pv / self._tv
        macro_vwap_dev = (cp - macro_vwap) / macro_vwap if macro_vwap > 0 else 0.0
        source         = "tick" if self._tick_vol > 0 else "kline_fallback"

        return {
            "macro_vwap":     float(macro_vwap),
            "macro_vwap_dev": float(macro_vwap_dev),
            "session_bars":   self._session_bars,
            "source":         source,
        }

    def _accumulate(self, k: Kline, tick_map: Optional[TickBarMap]) -> None:
        if tick_map is not None:
            ticks = tick_map.get(k.open_time)
            if ticks is not None and len(ticks) > 0:
                vols = ticks[:, 2]
                v    = float(np.sum(vols))
                if v > 0:
                    self._pv       += float(np.dot(ticks[:, 1], vols))
                    self._tv       += v
                    self._tick_vol += v
                    return
        if k.volume > 0:
            self._pv += (k.high + k.low + k.close) / 3.0 * k.volume
            self._tv += k.volume


# ── MacroVWAPZoneRegimeComponent ──────────────────────────────────────────────

class MacroVWAPZoneRegimeComponent(RegimeClassifier):
    """
    Macro VWAP 相對位置 Regime 分類器（dimension = "macro_vwap_zone"）。

    判斷當前收盤價是否低於日內 Macro VWAP（GMT+0 00:00 UTC 每日重設）。
    日初累積不足 min_session_bars 根 K 棒時輸出 "insufficient_data"，
    避免日初 VWAP 樣本過少造成的錯誤信號。

    Labels:
      below              close < macro_vwap 且 session_bars ≥ min_session_bars
                         → 允許做多（價格低於日內 VWAP 錨點）
      above              close ≥ macro_vwap 且 session_bars ≥ min_session_bars
                         → 封鎖做多
      insufficient_data  session_bars < min_session_bars（預設 60 = 1h@1m）
                         → 封鎖，VWAP 樣本不足，不具參考性
    """

    dimension = "macro_vwap_zone"

    def __init__(
        self,
        min_session_bars:  int                            = 60,
        macro_vwap_comp:   Optional[MacroVWAPComponent]  = None,
    ) -> None:
        self._macro_comp      = macro_vwap_comp or MacroVWAPComponent()
        self.min_session_bars = min_session_bars
        self.component_id     = f"macro_vwap_zone_{min_session_bars}"

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        base         = self._macro_comp.compute(klines, idx, tick_map)
        session_bars = base["session_bars"]
        macro_vwap   = base["macro_vwap"]
        cp           = klines[idx].close

        if session_bars < self.min_session_bars:
            return {**base, "label": "insufficient_data"}

        label = "below" if cp < macro_vwap else "above"
        return {**base, "label": label}


# ── VWAPZScoreRegimeComponent ─────────────────────────────────────────────────

class VWAPZScoreRegimeComponent(RegimeClassifier):
    """
    Rolling VWAP 乖離率 Regime 分類器（dimension = "vwap_z_score"）。

    包裝 VWAPDeviationComponent（前 window 根 K 棒），
    以 z_score 欄位分類乖離區帶。

    Labels:
      deep_below   z < −z_high     強乖離下偏（均值回歸多單首選）
      below        −z_high ≤ z < −z_low
      neutral      |z| ≤ z_low
      above        z_low < z ≤ z_high
      deep_above   z > z_high      強乖離上偏（均值回歸空單首選）

    component_id 與 VWAPDeviationComponent 一致，可共享 SharedContext 快取。
    """

    dimension = "vwap_z_score"

    def __init__(
        self,
        window:   int   = 240,
        lookback: int   = 300,
        z_low:    float = 1.0,
        z_high:   float = 2.0,
    ) -> None:
        self._comp         = VWAPDeviationComponent(window=window, lookback=lookback)
        self.z_low         = z_low
        self.z_high        = z_high
        self.component_id  = f"vwap_z_score_{window}_{lookback}"

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        base   = self._comp.compute(klines, idx, tick_map)
        z      = base["z_score"]

        if z < -self.z_high:
            label = "deep_below"
        elif z < -self.z_low:
            label = "below"
        elif z > self.z_high:
            label = "deep_above"
        elif z > self.z_low:
            label = "above"
        else:
            label = "neutral"

        return {**base, "label": label, "z_low": self.z_low, "z_high": self.z_high}


# ── VWAPSlopeRegimeComponent ──────────────────────────────────────────────────

class VWAPSlopeRegimeComponent(RegimeClassifier):
    """
    Rolling VWAP 斜率動量 Regime 分類器（dimension = "vwap_slope"）。

    以最近 slope_period 個時間點的 rolling VWAP 值（每點以前 window 根 K 棒計算），
    透過線性迴歸估計斜率，並相對 VWAP 均值正規化為 slope_norm（無因次每根 K 棒）。

    Labels:
      rising_strong   slope_norm >  strong_threshold
      rising          slope_norm >  flat_threshold
      flat            |slope_norm| ≤ flat_threshold
      falling         slope_norm < −flat_threshold
      falling_strong  slope_norm < −strong_threshold
    """

    dimension = "vwap_slope"

    def __init__(
        self,
        window:           int   = 240,
        slope_period:     int   = 20,
        flat_threshold:   float = 0.00005,
        strong_threshold: float = 0.00020,
    ) -> None:
        self.window           = window
        self.slope_period     = slope_period
        self.flat_threshold   = flat_threshold
        self.strong_threshold = strong_threshold
        self.component_id     = f"vwap_slope_{window}_{slope_period}"
        # Sliding-window state：O(1) per bar（順序存取）
        self._bar_pv:     deque[float] = deque()  # 每根 K 棒的 pv 貢獻
        self._bar_tv:     deque[float] = deque()  # 每根 K 棒的 tv 貢獻
        self._sum_pv:     float        = 0.0
        self._sum_tv:     float        = 0.0
        self._vwap_deque: deque[float] = deque(maxlen=slope_period)
        self._last_idx:   int          = -1

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        if idx < self.window + self.slope_period - 1:
            return {"label": "flat", "slope_norm": 0.0, "slope_abs": 0.0,
                    "vwap_now": klines[idx].close, "source": "insufficient_data"}

        if idx != self._last_idx + 1:
            # 非順序存取或首次呼叫：重建滑動視窗
            self._bar_pv = deque()
            self._bar_tv = deque()
            self._sum_pv = self._sum_tv = 0.0
            self._vwap_deque = deque(maxlen=self.slope_period)
            start = max(0, idx - self.window - self.slope_period + 2)
            for i in range(start, idx + 1):
                self._push_bar(klines[i])
        else:
            self._push_bar(klines[idx])

        self._last_idx = idx

        if len(self._vwap_deque) < 2:
            return {"label": "flat", "slope_norm": 0.0, "slope_abs": 0.0,
                    "vwap_now": klines[idx].close, "source": "insufficient_data"}

        n      = len(self._vwap_deque)
        x      = np.arange(n, dtype=float)
        y      = np.array(self._vwap_deque, dtype=float)
        x_mean = x.mean()
        y_mean = y.mean()
        num    = float(np.dot(x - x_mean, y - y_mean))
        denom  = float(np.dot(x - x_mean, x - x_mean))
        slope  = num / (denom + 1e-10)
        slope_norm = slope / (y_mean + 1e-10)

        if slope_norm > self.strong_threshold:
            label = "rising_strong"
        elif slope_norm > self.flat_threshold:
            label = "rising"
        elif slope_norm < -self.strong_threshold:
            label = "falling_strong"
        elif slope_norm < -self.flat_threshold:
            label = "falling"
        else:
            label = "flat"

        return {
            "label":      label,
            "slope_norm": float(slope_norm),
            "slope_abs":  float(slope),
            "vwap_now":   float(self._vwap_deque[-1]),
            "vwap_prev":  float(self._vwap_deque[0]),
            "source":     "kline",
        }

    def _push_bar(self, k: Kline) -> None:
        pv = (k.high + k.low + k.close) / 3.0 * k.volume if k.volume > 0 else 0.0
        tv = k.volume if k.volume > 0 else 0.0
        if len(self._bar_pv) == self.window:
            self._sum_pv -= self._bar_pv.popleft()
            self._sum_tv -= self._bar_tv.popleft()
        self._bar_pv.append(pv)
        self._bar_tv.append(tv)
        self._sum_pv += pv
        self._sum_tv += tv
        if self._sum_tv > 0:
            self._vwap_deque.append(self._sum_pv / self._sum_tv)


# ── _RollingVPBuilder ────────────────────────────────────────────────────────

class _RollingVPBuilder:
    """
    滑動視窗 Rolling Value Profile 建構器（內部共享，不對外）。

    VPDensityRatioRegimeComponent 與 POCVWAPDistanceRegimeComponent 共享同一實例，
    避免對相同 window / tick_size 的 price→volume dict 重複建構。

    順序存取（idx == last_idx + 1）時每根 K 棒只計算新增 bar，O(ticks_in_bar)；
    非順序時重建整個視窗，O(window × ticks_per_bar)。
    """

    def __init__(self, window: int, tick_size: float) -> None:
        self.window     = window
        self.tick_size  = tick_size
        self._last_idx: int                        = -1
        self._bar_vps:  deque[dict[float, float]]  = deque()
        self._total_vp: dict[float, float]         = {}

    def get(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict[float, float]:
        if idx == self._last_idx:
            return self._total_vp
        if idx != self._last_idx + 1:
            self._rebuild(klines, idx, tick_map)
        else:
            self._push_bar(klines[idx], tick_map)
        self._last_idx = idx
        return self._total_vp

    def _push_bar(self, k: Kline, tick_map: Optional[TickBarMap]) -> None:
        bar_vp = self._build_bar_vp(k, tick_map)
        if len(self._bar_vps) == self.window:
            old = self._bar_vps.popleft()
            for p, v in old.items():
                rem = self._total_vp.get(p, 0.0) - v
                if rem > 1e-12:
                    self._total_vp[p] = rem
                else:
                    self._total_vp.pop(p, None)
        self._bar_vps.append(bar_vp)
        for p, v in bar_vp.items():
            self._total_vp[p] = self._total_vp.get(p, 0.0) + v

    def _rebuild(self, klines: list[Kline], idx: int, tick_map: Optional[TickBarMap]) -> None:
        self._bar_vps = deque()
        self._total_vp = {}
        start = max(0, idx - self.window + 1)
        for i in range(start, idx + 1):
            self._push_bar(klines[i], tick_map)

    def _build_bar_vp(self, k: Kline, tick_map: Optional[TickBarMap]) -> dict[float, float]:
        if k.volume <= 0:
            return {}
        vp: dict[float, float] = {}
        if tick_map is not None:
            ticks = tick_map.get(k.open_time)
            if ticks is not None and len(ticks) > 0:
                for t in ticks:
                    bp = round(float(t[1]) / self.tick_size) * self.tick_size
                    vp[bp] = vp.get(bp, 0.0) + float(t[2])
                return vp
        typ = round((k.high + k.low + k.close) / 3.0 / self.tick_size) * self.tick_size
        vp[typ] = k.volume
        return vp


# ── VPDensityRatioRegimeComponent ─────────────────────────────────────────────

class VPDensityRatioRegimeComponent(RegimeClassifier):
    """
    Rolling Value Profile 流動性密度 Regime 分類器（dimension = "vp_density"）。

    以前 window 根 K 棒建立 VP（tick-first，kline fallback）。
    計算當前收盤價附近（±band_ticks × tick_size）的平均成交量，
    相對於全體 VP 的平均每 bin 成交量，得到 density_ratio。

    Labels:
      hvn     density_ratio ≥ hvn_threshold   高流動性節點（支撐 / 阻力帶）
      normal  lvn_threshold < ratio < hvn_threshold
      lvn     density_ratio ≤ lvn_threshold   低流動性節點（快速穿越帶）
    """

    dimension = "vp_density"

    def __init__(
        self,
        window:        int                          = 240,
        tick_size:     float                        = 1.0,
        band_ticks:    int                          = 5,
        hvn_threshold: float                        = 1.5,
        lvn_threshold: float                        = 0.5,
        vp_builder:    Optional[_RollingVPBuilder]  = None,
    ) -> None:
        self.window        = window
        self.tick_size     = tick_size
        self.band_ticks    = band_ticks
        self.hvn_threshold = hvn_threshold
        self.lvn_threshold = lvn_threshold
        self.component_id  = f"vp_density_{window}_{int(tick_size * 10)}"
        self._vp_builder   = vp_builder or _RollingVPBuilder(window=window, tick_size=tick_size)

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        cp        = klines[idx].close
        price_vol = self._vp_builder.get(klines, idx, tick_map)

        if not price_vol:
            return {"label": "normal", "density_ratio": 1.0,
                    "band_vol": 0.0, "avg_vol": 0.0, "total_bins": 0, "source": "insufficient_data"}

        total_bins = len(price_vol)
        avg_vol    = sum(price_vol.values()) / total_bins
        band       = self.band_ticks * self.tick_size
        band_vols  = [v for p, v in price_vol.items() if abs(p - cp) <= band]

        if not band_vols or avg_vol <= 0:
            density_ratio = 1.0
        else:
            density_ratio = (sum(band_vols) / len(band_vols)) / avg_vol

        if density_ratio >= self.hvn_threshold:
            label = "hvn"
        elif density_ratio <= self.lvn_threshold:
            label = "lvn"
        else:
            label = "normal"

        source = "tick" if tick_map is not None else "kline_fallback"
        return {
            "label":         label,
            "density_ratio": float(density_ratio),
            "band_vol":      float(sum(band_vols)) if band_vols else 0.0,
            "avg_vol":       float(avg_vol),
            "total_bins":    total_bins,
            "source":        source,
        }


# ── POCVWAPDistanceRegimeComponent ────────────────────────────────────────────

class POCVWAPDistanceRegimeComponent(RegimeClassifier):
    """
    POC 距離 & Macro VWAP 距離 Regime 分類器（dimension = "poc_vwap_dist"）。

    同時計算：
      poc_dist_atr   : (current_price − Rolling VP POC) / ATR(atr_period)
      vwap_dist_atr  : (current_price − Macro VWAP)     / ATR(atr_period)

    Rolling VP POC 取前 vp_window 根 K 棒的成交量最高 bin。
    Macro VWAP     取 GMT+0（00:00 UTC）起累積 VWAP。

    Labels（優先順序由上至下）：
      near_poc    |poc_dist_atr|  ≤ near_atr    價格緊貼 POC
      near_vwap   |vwap_dist_atr| ≤ near_atr    價格緊貼 Macro VWAP
      below_both  poc_dist_atr  < −near_atr AND vwap_dist_atr < −near_atr
      above_both  poc_dist_atr  >  near_atr AND vwap_dist_atr >  near_atr
      between     其餘（在 POC 與 VWAP 之間，或單邊偏離）
    """

    dimension = "poc_vwap_dist"

    def __init__(
        self,
        vp_window:       int                            = 240,
        tick_size:       float                          = 1.0,
        atr_period:      int                            = 14,
        near_atr:        float                          = 0.5,
        vp_builder:      Optional[_RollingVPBuilder]   = None,
        macro_vwap_comp: Optional[MacroVWAPComponent]  = None,
    ) -> None:
        self.vp_window  = vp_window
        self.tick_size  = tick_size
        self.atr_period = atr_period
        self.near_atr   = near_atr
        self.component_id  = f"poc_vwap_dist_{vp_window}_{atr_period}"
        self._atr_comp     = ATRComponent(period=atr_period)
        self._vp_builder   = vp_builder    or _RollingVPBuilder(window=vp_window, tick_size=tick_size)
        self._macro_comp   = macro_vwap_comp or MacroVWAPComponent()

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        cp  = klines[idx].close
        atr = self._atr_comp.compute(klines, idx, tick_map)["atr"] or cp * 0.001

        price_vol  = self._vp_builder.get(klines, idx, tick_map)
        poc        = float(max(price_vol, key=price_vol.__getitem__)) if price_vol else cp
        macro_vwap = self._macro_comp.compute(klines, idx, tick_map)["macro_vwap"]

        poc_dist_atr  = (cp - poc)        / atr
        vwap_dist_atr = (cp - macro_vwap) / atr

        if abs(poc_dist_atr) <= self.near_atr:
            label = "near_poc"
        elif abs(vwap_dist_atr) <= self.near_atr:
            label = "near_vwap"
        elif poc_dist_atr < -self.near_atr and vwap_dist_atr < -self.near_atr:
            label = "below_both"
        elif poc_dist_atr > self.near_atr and vwap_dist_atr > self.near_atr:
            label = "above_both"
        else:
            label = "between"

        return {
            "label":         label,
            "poc_price":     float(poc),
            "macro_vwap":    float(macro_vwap),
            "poc_dist_atr":  float(poc_dist_atr),
            "vwap_dist_atr": float(vwap_dist_atr),
            "atr":           float(atr),
        }


# ── Factory: build_vwap_regime_stage ─────────────────────────────────────────

def build_vwap_regime_stage(
    *,
    # Rolling VWAP / VP 窗口（4h on 1m = 240 bars）
    rolling_window:    int   = 240,
    vwap_lookback:     int   = 300,
    # vwap_z_score 允許區帶
    z_low:             float = 1.0,
    z_high:            float = 2.0,
    allowed_z_zones:   Sequence[str] = ("deep_below", "below"),
    # vwap_slope 允許區帶
    slope_period:      int   = 20,
    flat_threshold:    float = 0.00005,
    strong_threshold:  float = 0.00020,
    allowed_slopes:    Sequence[str] = ("flat", "falling", "rising"),
    # vp_density 允許區帶
    tick_size:         float = 1.0,
    band_ticks:        int   = 5,
    hvn_threshold:     float = 1.5,
    lvn_threshold:     float = 0.5,
    allowed_densities: Sequence[str] = ("hvn", "normal"),
    # poc_vwap_dist 允許區帶
    atr_period:        int   = 14,
    near_atr:          float = 0.5,
    allowed_dist:      Sequence[str] = ("below_both", "near_poc", "near_vwap", "between"),
    # Session 過濾（None = 不過濾）
    allowed_sessions:  Optional[Sequence[str]] = None,
    # Macro VWAP 位置過濾（做多須低於 Macro VWAP，且累積 min_session_bars 根 K 棒）
    use_macro_vwap_zone:  bool                    = False,
    min_session_bars:     int                     = 60,
    allowed_macro_zones:  Optional[Sequence[str]] = ("below",),
) -> RegimeStage:
    """
    建立含四個 VWAP 環境因子的 RegimeStage。

    allowed_xxx 傳入 None 代表不限制該維度。

    典型均值回歸多單設定：
      allowed_z_zones    = ("deep_below", "below")
      allowed_slopes     = ("flat", "falling")    # VWAP 平坦或下彎，價格超跌
      allowed_densities  = ("hvn", "normal")      # 在有成交量支撐的區域
      allowed_dist       = ("below_both",)         # 同時低於 POC 和 Macro VWAP
    """
    # 共享實例：避免同一根 K 棒重複計算 Macro VWAP 和 Rolling VP
    shared_macro_comp = MacroVWAPComponent()
    shared_vp_builder = _RollingVPBuilder(window=rolling_window, tick_size=tick_size)

    z_comp = VWAPZScoreRegimeComponent(
        window=rolling_window, lookback=vwap_lookback, z_low=z_low, z_high=z_high,
    )
    slope_comp = VWAPSlopeRegimeComponent(
        window=rolling_window, slope_period=slope_period,
        flat_threshold=flat_threshold, strong_threshold=strong_threshold,
    )
    density_comp = VPDensityRatioRegimeComponent(
        window=rolling_window, tick_size=tick_size,
        band_ticks=band_ticks, hvn_threshold=hvn_threshold, lvn_threshold=lvn_threshold,
        vp_builder=shared_vp_builder,
    )
    dist_comp = POCVWAPDistanceRegimeComponent(
        vp_window=rolling_window, tick_size=tick_size,
        atr_period=atr_period, near_atr=near_atr,
        vp_builder=shared_vp_builder,
        macro_vwap_comp=shared_macro_comp,
    )

    components = [z_comp, slope_comp, density_comp, dist_comp]
    allowed: dict[str, list[str]] = {}
    if allowed_z_zones is not None:
        allowed["vwap_z_score"] = list(allowed_z_zones)
    if allowed_slopes is not None:
        allowed["vwap_slope"] = list(allowed_slopes)
    if allowed_densities is not None:
        allowed["vp_density"] = list(allowed_densities)
    if allowed_dist is not None:
        allowed["poc_vwap_dist"] = list(allowed_dist)

    if allowed_sessions is not None:
        session_comp = SessionComponent()
        components.append(session_comp)
        allowed["session"] = list(allowed_sessions)

    if use_macro_vwap_zone:
        macro_zone_comp = MacroVWAPZoneRegimeComponent(
            min_session_bars=min_session_bars,
            macro_vwap_comp=shared_macro_comp,
        )
        components.append(macro_zone_comp)
        if allowed_macro_zones is not None:
            allowed["macro_vwap_zone"] = list(allowed_macro_zones)

    return RegimeStage(components=components, allowed=allowed if allowed else None)


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline：VWAP Reclaim 均值回歸
# ═══════════════════════════════════════════════════════════════════════════════

# ── VWAPReclaimSignal ─────────────────────────────────────────────────────────

class VWAPReclaimSignal(SignalModule):
    """
    Rolling VWAP −z_threshold σ 穿越回歸訊號（均值回歸多單）。

    信號 k0 = klines[idx−1]（reclaim bar）：
      klines[idx−2]  z_score < −z_threshold  （sweep bar：跌破 −Nσ 帶）
      klines[idx−1]  z_score > −z_threshold  （reclaim bar：收盤回到帶內）

    z_score 使用短 lookback（sig_lookback，預設 60）估算 sigma，
    效能考量：O(sig_lookback × window) ≈ 14,400 vs full lookback 72,000。
    精確的 |z| > entry_z_min 確認由 VWAPReclaimEntryManagementStage 完成。

    進場（execution bar = klines[idx]）：
      tick trigger  : 首個 tick price ≥ reclaim_bar.high
      kline fallback: exec_bar.high ≥ reclaim_bar.high

    停損：reclaim_bar.low − sl_offset
    """

    name = "VWAPReclaim"

    def __init__(
        self,
        window:        int   = 240,
        sig_lookback:  int   = 60,
        z_threshold:   float = 2.0,
        sl_offset:     float = 0.0,
        min_micro_cvd: float = 0.0,
    ) -> None:
        self._vwap_comp    = VWAPDeviationComponent(window=window, lookback=sig_lookback)
        self.z_threshold   = z_threshold
        self.sl_offset     = sl_offset
        self.min_micro_cvd = min_micro_cvd

    def can_trade(self, klines: list[Kline], idx: int) -> bool:
        min_idx = self._vwap_comp.window + self._vwap_comp.lookback + 2
        return idx >= min_idx

    def detect_k0(
        self,
        klines: list[Kline],
        idx:    int,
    ) -> Optional[dict]:
        if idx < 2:
            return None

        r_sweep   = self._vwap_comp.compute(klines, idx - 2)
        r_reclaim = self._vwap_comp.compute(klines, idx - 1)
        z_sweep   = r_sweep["z_score"]
        z_reclaim = r_reclaim["z_score"]

        # 多單：sweep 跌破 −z_threshold σ，reclaim 收回帶內
        if not (z_sweep < -self.z_threshold and z_reclaim > -self.z_threshold):
            return None

        k0 = klines[idx - 1]
        return {
            "direction":    "long",
            "k0_idx":       idx - 1,
            "k0_low":       k0.low,
            "z_sweep":      float(z_sweep),
            "z_reclaim":    float(z_reclaim),
            "vwap_at_sweep": float(r_sweep["vwap"]),
        }

    def entry_conditions(
        self,
        klines:   list[Kline],
        k0_idx:   int,
        k0_meta:  dict,
        tick_map: Optional[TickBarMap] = None,
    ) -> Optional[StrategySignal]:
        signal_bar = klines[k0_meta["k0_idx"]]
        return _mr_long_entry(
            klines, k0_idx, k0_meta["k0_low"], self.sl_offset,
            label         = "VWAP_RECLAIM",
            meta          = {
                "z_sweep":       k0_meta["z_sweep"],
                "z_reclaim":     k0_meta["z_reclaim"],
                "vwap_at_sweep": k0_meta["vwap_at_sweep"],
                "k0_idx":        k0_meta["k0_idx"],
            },
            tick_map      = tick_map,
            trigger_price = signal_bar.high,
            min_micro_cvd = self.min_micro_cvd,
        )


# ── VWAPReclaimEntryManagementStage ───────────────────────────────────────────

class VWAPReclaimEntryManagementStage(PipelineStage):
    """
    VWAP Reclaim 進場管理 Stage（AlphaStage 之後、RRStage 之前）。

    Gate 1  近期 |z| 極值確認（強乖離才進場）
      從 ctx.alpha_meta["k0_meta"]["z_sweep"] 取 sweep bar z（快速路徑）。
      若不足，掃 idx−2 到 idx−(z_lookback+1)：
        近似 z = (close − rolling_vwap) / sigma_current
        sigma 取 ctx.regime_meta["vwap_z_score"]["sigma"]（RegimeStage 已算好）
        rolling_vwap 用 kline VWAP（O(vwap_window) per bar，快速）
      任一棒 |z_approx| ≥ entry_z_min → 通過

    Gate 2  VWAP 斜率平坦確認
      |slope_norm| < slope_threshold
      slope_norm 取 ctx.regime_meta["vwap_slope"]["slope_norm"]

    Gate 3  停損距離上限（max_sl_pct）
    Gate 4  停損距離下限（min_stop_pct）

    stop_price 保留 AlphaStage 設定的 reclaim_bar.low（不覆蓋 ATR 停損）。
    """

    name = "VWAPReclaimEntryManagementStage"

    def __init__(
        self,
        entry_z_min:     float = 2.5,
        z_lookback:      int   = 10,
        slope_threshold: float = 0.00010,
        max_sl_pct:      float = 0.03,
        min_stop_pct:    float = 0.0015,
        vwap_window:     int   = 240,
    ) -> None:
        self.entry_z_min     = entry_z_min
        self.z_lookback      = z_lookback
        self.slope_threshold = slope_threshold
        self.max_sl_pct      = max_sl_pct
        self.min_stop_pct    = min_stop_pct
        self.vwap_window     = vwap_window

    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        if ctx.entry_price is None or ctx.stop_price is None:
            return None

        entry = ctx.entry_price
        stop  = ctx.stop_price

        # ── Gate 1: |z| >= entry_z_min in recent z_lookback bars ─────────────
        sigma = float(ctx.regime_meta.get("vwap_z_score", {}).get("sigma", 0.0))
        extreme_found = False

        if sigma > 0:
            # 快速路徑：alpha 已算好的 sweep bar z
            k0_meta = ctx.alpha_meta.get("k0_meta", {})
            z_sweep = abs(float(k0_meta.get("z_sweep", 0.0)))
            if z_sweep >= self.entry_z_min:
                extreme_found = True
            else:
                # 向前掃：用 current sigma 近似歷史 z（O(window) per bar）
                for offset in range(2, self.z_lookback + 2):
                    j = ctx.idx - offset
                    if j < 0:
                        break
                    w     = ctx.klines[max(0, j - self.vwap_window + 1): j + 1]
                    vwap  = self._kline_vwap(w)
                    if vwap is not None and vwap > 0:
                        dev   = (ctx.klines[j].close - vwap) / vwap
                        z_abs = abs(dev / sigma)
                        if z_abs >= self.entry_z_min:
                            extreme_found = True
                            break

        if not extreme_found:
            return None

        # ── Gate 2: |vwap_slope| < threshold ─────────────────────────────────
        slope_norm = abs(float(
            ctx.regime_meta.get("vwap_slope", {}).get("slope_norm", 0.0)
        ))
        if slope_norm >= self.slope_threshold:
            return None

        # ── Gate 3/4: 停損距離範圍 ────────────────────────────────────────────
        if entry <= stop:
            return None
        stop_pct = (entry - stop) / entry
        if stop_pct > self.max_sl_pct:
            return None
        if stop_pct < self.min_stop_pct:
            return None

        return ctx

    @staticmethod
    def _kline_vwap(klines: list[Kline]) -> Optional[float]:
        pv = tv = 0.0
        for k in klines:
            if k.volume > 0:
                pv += (k.high + k.low + k.close) / 3.0 * k.volume
                tv += k.volume
        return pv / tv if tv > 0 else None


# ── VWAPDistanceFeeGateStage ──────────────────────────────────────────────────

class VWAPDistanceFeeGateStage(PipelineStage):
    """
    VWAP 距離費用覆蓋率 Gate Stage（RRStage 之後的最終關卡）。

    以「entry_price → Macro VWAP」的絕對距離作為均值回歸的自然獲利目標，
    確認此距離足以覆蓋往返手續費：

      round_trip_cost = 2 × (taker_fee_rate + slippage_rate) × entry_price
      pass if |macro_vwap − entry_price| ≥ round_trip_cost × fee_cover_ratio

    macro_vwap 從 ctx.regime_meta["poc_vwap_dist"]["macro_vwap"] 讀取
    （由 POCVWAPDistanceRegimeComponent 在 RegimeStage 計算填入）。

    通過後填入：
      ctx.expected_fee  雙邊估算費用（(entry + tp) × qty × rate）
      ctx.net_reward    以 VWAP 作為目標的淨獲利（vwap_abs_dist × qty − fees）
      ctx.fee_approved  True
    """

    name = "VWAPDistanceFeeGateStage"

    def __init__(
        self,
        taker_fee_rate:  float = 0.00032,
        slippage_rate:   float = 0.00002,
        fee_cover_ratio: float = 1.5,
    ) -> None:
        self.taker_fee_rate  = taker_fee_rate
        self.slippage_rate   = slippage_rate
        self.fee_cover_ratio = fee_cover_ratio

    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        if ctx.entry_price is None or ctx.qty is None:
            return None

        entry = ctx.entry_price
        qty   = ctx.qty
        tp    = ctx.tp_price or entry

        poc_vwap_meta = ctx.regime_meta.get("poc_vwap_dist", {})
        macro_vwap    = float(poc_vwap_meta.get("macro_vwap", 0.0))
        if macro_vwap <= 0:
            return None

        vwap_abs_dist   = abs(macro_vwap - entry)
        round_trip_cost = 2.0 * (self.taker_fee_rate + self.slippage_rate) * entry

        if vwap_abs_dist < round_trip_cost * self.fee_cover_ratio:
            return None

        rate         = self.taker_fee_rate + self.slippage_rate
        total_fee    = (entry + tp) * qty * rate
        net_reward   = vwap_abs_dist * qty - total_fee

        ctx.expected_fee = total_fee
        ctx.net_reward   = net_reward
        ctx.fee_approved = True
        ctx.alpha_meta.setdefault("vwap_fee_gate", {}).update({
            "macro_vwap":      macro_vwap,
            "vwap_abs_dist":   vwap_abs_dist,
            "round_trip_cost": round_trip_cost,
            "actual_cover":    vwap_abs_dist / (round_trip_cost + 1e-10),
        })
        return ctx


# ── Factory: build_vwap_reclaim_pipeline ─────────────────────────────────────

def build_vwap_reclaim_pipeline(
    *,
    # Gate / Cooldown
    max_positions: int   = 1,
    cooldown_ms:   int   = 300_000,
    # Regime（四因子，Reclaim 策略預設）
    rolling_window:    int            = 240,
    vwap_lookback:     int            = 300,
    z_low:             float          = 1.0,
    z_high:            float          = 2.0,
    # Reclaim 執行棒已反彈，z 值可能回到 neutral；z 過濾交由 EntryManagementStage 做歷史掃描
    allowed_z_zones:   Sequence[str]  = ("deep_below", "below", "neutral"),
    slope_period:      int            = 20,
    flat_threshold:    float          = 0.00005,
    strong_threshold:  float          = 0.00020,
    allowed_slopes:    Sequence[str]  = ("flat", "falling", "rising"),
    tick_size:         float          = 1.0,
    band_ticks:        int            = 5,
    hvn_threshold:     float          = 1.5,
    lvn_threshold:     float          = 0.5,
    # LVN（稀薄區）對 reclaim 同樣有利（快速穿越），預設全放行
    allowed_densities: Sequence[str]  = ("hvn", "normal", "lvn"),
    atr_period:        int            = 14,
    near_atr:          float          = 0.5,
    allowed_dist:      Sequence[str]  = ("below_both", "near_poc", "near_vwap", "between"),
    allowed_sessions:  Optional[Sequence[str]] = None,
    # Macro VWAP 位置過濾
    use_macro_vwap_zone:  bool                    = True,
    min_session_bars:     int                     = 60,
    allowed_macro_zones:  Optional[Sequence[str]] = ("below",),
    # Alpha：VWAPReclaimSignal
    reclaim_z_threshold: float = 2.0,   # alpha 觸發的穿越門檻（−2σ reclaim）
    sig_lookback:        int   = 60,    # alpha 輕量 sigma 估算窗口
    sl_offset:           float = 0.0,
    min_micro_cvd:       float = 0.0,
    # Entry Management
    entry_z_min:         float = 2.5,   # 近期必須觸及 |z| >= 2.5
    z_lookback:          int   = 10,
    slope_threshold:     float = 0.00010,
    max_sl_pct:          float = 0.03,
    min_stop_pct:        float = 0.0015,
    # RR
    rr_ratio:            float                   = 2.0,
    time_decay_bars:     int                     = 40,
    capital_cfg:         Optional[CapitalConfig] = None,
    # Fee Gate（VWAP 距離）
    taker_fee_rate:      float = 0.00032,
    slippage_rate:       float = 0.00002,
    fee_cover_ratio:     float = 1.5,
) -> TradingPipeline:
    """
    VWAP Reclaim 均值回歸 Pipeline 工廠函式。

    完整流程：
      [Gate]  PositionGateStage + CooldownStage
      [1] RegimeStage     四因子 VWAP 環境過濾（vwap_z_score / vwap_slope / vp_density / poc_vwap_dist）
      [2] AlphaStage      VWAPReclaimSignal（−2σ 穿越 → reclaim 回帶內）
      [3] VWAPReclaimEntryManagementStage
            Gate A: 近 z_lookback 棒內 |z| ≥ entry_z_min（2.5σ 強乖離確認）
            Gate B: |vwap_slope_norm| < slope_threshold（VWAP 平坦）
            Gate C/D: 停損距離上下限
      [4] RRStage(2:1)    止損 = reclaim bar low，目標 2RR
      [5] VWAPDistanceFeeGateStage
            pass if |macro_vwap − entry| ≥ 2×(taker+slip)×entry×cover_ratio

    範例（1m K 線 BTCUSDT）：
        from strategies.pipeline.vwap_regime import build_vwap_reclaim_pipeline
        from strategies.modules import CapitalConfig

        pipeline = build_vwap_reclaim_pipeline(
            rolling_window     = 240,          # 4h
            allowed_z_zones    = ("deep_below",),
            allowed_slopes     = ("flat", "falling"),
            reclaim_z_threshold = 2.0,
            entry_z_min        = 2.5,
            capital_cfg        = CapitalConfig(max_risk_pct=1.0, leverage=20),
        )
    """
    regime_stage = build_vwap_regime_stage(
        rolling_window       = rolling_window,
        vwap_lookback        = vwap_lookback,
        z_low                = z_low,
        z_high               = z_high,
        allowed_z_zones      = allowed_z_zones,
        slope_period         = slope_period,
        flat_threshold       = flat_threshold,
        strong_threshold     = strong_threshold,
        allowed_slopes       = allowed_slopes,
        tick_size            = tick_size,
        band_ticks           = band_ticks,
        hvn_threshold        = hvn_threshold,
        lvn_threshold        = lvn_threshold,
        allowed_densities    = allowed_densities,
        atr_period           = atr_period,
        near_atr             = near_atr,
        allowed_dist         = allowed_dist,
        allowed_sessions     = allowed_sessions,
        use_macro_vwap_zone  = use_macro_vwap_zone,
        min_session_bars     = min_session_bars,
        allowed_macro_zones  = allowed_macro_zones,
    )

    return TradingPipeline([
        PositionGateStage(max_positions=max_positions),
        CooldownStage(cooldown_ms=cooldown_ms),
        regime_stage,
        AlphaStage(
            modules = [VWAPReclaimSignal(
                window        = rolling_window,
                sig_lookback  = sig_lookback,
                z_threshold   = reclaim_z_threshold,
                sl_offset     = sl_offset,
                min_micro_cvd = min_micro_cvd,
            )],
            mode = "OR",
        ),
        VWAPReclaimEntryManagementStage(
            entry_z_min     = entry_z_min,
            z_lookback      = z_lookback,
            slope_threshold = slope_threshold,
            max_sl_pct      = max_sl_pct,
            min_stop_pct    = min_stop_pct,
            vwap_window     = rolling_window,
        ),
        RRStage(
            exit_cfg    = ExitConfig(tp_rr_ratio=rr_ratio, time_decay_bars=time_decay_bars),
            capital_cfg = capital_cfg or CapitalConfig(),
            min_rr      = rr_ratio,
        ),
        VWAPDistanceFeeGateStage(
            taker_fee_rate  = taker_fee_rate,
            slippage_rate   = slippage_rate,
            fee_cover_ratio = fee_cover_ratio,
        ),
    ])


def build_vwap_reclaim_pipeline_def(
    name:              str           = "vwap_reclaim",
    allocation_weight: float         = 1.0,
    tags:              Sequence[str] = ("vwap_reclaim", "mean_reversion", "long_only"),
    **pipeline_kwargs,
) -> PipelineDef:
    """便利包裝：直接回傳 PipelineDef，可傳入 MultiPipelineRunner。"""
    return PipelineDef(
        name              = name,
        pipeline          = build_vwap_reclaim_pipeline(**pipeline_kwargs),
        allocation_weight = allocation_weight,
        tags              = list(tags),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Simplified Strategy: Micro VWAP Reclaim (8h Rolling, POC Target)
# ═══════════════════════════════════════════════════════════════════════════════

# ── MicroVWAPReclaimSignal ────────────────────────────────────────────────────

class MicroVWAPReclaimSignal(SignalModule):
    """
    8h Rolling Micro VWAP 超賣回歸多單訊號（tick-level reclaim）。

    Sweep bar  = klines[idx−1]：收盤 z_score < −z_threshold（跌破 −Nσ 帶）
    Reclaim    = 執行棒 klines[idx] 的 tick 流：首個 tick ≥ threshold_price
                 threshold_price = sweep_vwap × (1 − z_threshold × sweep_sigma)

    SL:   sweep bar low（訊號K棒最低點）
    TP:   由 MicroVWAPPOCTargetStage 設定為 Rolling 8h VP POC
    """

    name = "MicroVWAPReclaim"

    def __init__(
        self,
        window:      int   = 480,   # 8h @ 1m
        lookback:    int   = 60,
        z_threshold: float = 2.0,
        sl_offset:   float = 0.0,
    ) -> None:
        self._vwap_comp  = VWAPDeviationComponent(window=window, lookback=lookback)
        self.z_threshold = z_threshold
        self.sl_offset   = sl_offset

    def can_trade(self, klines: list[Kline], idx: int) -> bool:
        return idx >= self._vwap_comp.window + self._vwap_comp.lookback + 1

    def detect_k0(self, klines: list[Kline], idx: int) -> Optional[dict]:
        if idx < 1:
            return None

        r_sweep  = self._vwap_comp.compute(klines, idx - 1)
        z_sweep  = r_sweep["z_score"]
        if z_sweep >= -self.z_threshold:
            return None

        k0              = klines[idx - 1]
        vwap            = r_sweep["vwap"]
        sigma           = r_sweep["sigma"]
        threshold_price = vwap * (1.0 - self.z_threshold * sigma)

        return {
            "direction":       "long",
            "k0_idx":          idx - 1,
            "k0_low":          k0.low,
            "z_sweep":         float(z_sweep),
            "vwap":            float(vwap),
            "sigma":           float(sigma),
            "threshold_price": float(threshold_price),
        }

    def entry_conditions(
        self,
        klines:   list[Kline],
        k0_idx:   int,
        k0_meta:  dict,
        tick_map: Optional[TickBarMap] = None,
    ) -> Optional[StrategySignal]:
        return _mr_long_entry(
            klines, k0_idx, k0_meta["k0_low"], self.sl_offset,
            label    = "MICRO_VWAP_RECLAIM",
            meta     = {
                "z_sweep":         k0_meta["z_sweep"],
                "vwap":            k0_meta["vwap"],
                "sigma":           k0_meta["sigma"],
                "threshold_price": k0_meta["threshold_price"],
                "k0_idx":          k0_meta["k0_idx"],
            },
            tick_map      = tick_map,
            trigger_price = k0_meta["threshold_price"],
        )


# ── MicroVWAPPOCTargetStage ───────────────────────────────────────────────────

class MicroVWAPPOCTargetStage(RRStage):
    """
    Rolling VP POC 目標管理 Stage（AlphaStage 之後）。

    繼承 RRStage 確保平行回測（Phase 2）能透過 isinstance 找到本 Stage 並重算 qty。

    Phase 1（ctx.tp_price is None）：
      1. 從 _RollingVPBuilder 取 Rolling 8h VP → POC（最大成交量 bin）。
      2. long 時 POC 須高於 entry；RR ≥ min_rr；reward ≥ 往返費用。
      3. 計算 qty、fee，填入 ctx 所有欄位。

    Phase 2（ctx.tp_price 已由 Phase 1 設好）：
      ctx.tp_price = poc（已知），直接跳過 VP 查詢，以實際 equity 重算 qty。
    """

    name = "MicroVWAPPOCTargetStage"

    def __init__(
        self,
        vp_builder:     _RollingVPBuilder,
        min_rr:         float                   = 1.5,
        taker_fee_rate: float                   = 0.00032,
        slippage_rate:  float                   = 0.00002,
        capital_cfg:    Optional[CapitalConfig] = None,
    ) -> None:
        super().__init__(capital_cfg=capital_cfg, min_rr=min_rr)
        self._vp_builder    = vp_builder
        self.taker_fee_rate = taker_fee_rate
        self.slippage_rate  = slippage_rate

    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        if None in (ctx.entry_price, ctx.stop_price, ctx.direction):
            return None

        entry = ctx.entry_price
        stop  = ctx.stop_price
        if entry <= stop:
            return None

        risk = entry - stop  # long only

        # Phase 2 fast path：tp already computed in Phase 1
        if ctx.tp_price is not None:
            poc = ctx.tp_price
        else:
            # Phase 1：compute POC from rolling VP
            price_vol = self._vp_builder.get(ctx.klines, ctx.idx, ctx.tick_map)
            if not price_vol:
                return None
            poc = float(max(price_vol, key=price_vol.__getitem__))
            if poc <= entry:
                return None

            round_trip_cost = 2.0 * (self.taker_fee_rate + self.slippage_rate) * entry
            reward0 = poc - entry
            if reward0 < round_trip_cost:
                return None

            ctx.alpha_meta.setdefault("poc_target", {}).update({
                "poc_price":       poc,
                "round_trip_cost": round_trip_cost,
                "total_bins":      len(price_vol),
            })

        reward = poc - entry
        rr     = reward / risk
        if rr < self.min_rr:
            return None

        qty = self._capital.position_size(
            equity      = ctx.equity,
            entry_price = entry,
            stop_price  = stop,
            direction   = ctx.direction,
        )
        if qty is None or qty <= 0:
            return None

        ctx.tp_price    = poc
        ctx.expected_rr = rr
        ctx.qty         = qty
        ctx.risk_amount = risk * qty

        rate        = self.taker_fee_rate + self.slippage_rate
        total_fee   = (entry + poc) * qty * rate
        net_reward  = reward * qty - total_fee

        ctx.expected_fee = total_fee
        ctx.net_reward   = net_reward
        ctx.fee_approved = True

        ctx.alpha_meta.setdefault("poc_target", {}).update({
            "poc_price": poc,
            "rr":        rr,
            "reward":    reward,
            "risk":      risk,
        })
        return ctx


# ── Factory: build_micro_vwap_reclaim_pipeline ───────────────────────────────

def build_micro_vwap_reclaim_pipeline(
    *,
    max_positions:  int                     = 1,
    cooldown_ms:    int                     = 300_000,
    rolling_window: int                     = 480,    # 8h @ 1m
    lookback:       int                     = 60,
    z_threshold:    float                   = 2.0,
    sl_offset:      float                   = 0.0,
    tick_size:      float                   = 1.0,
    min_rr:         float                   = 1.5,
    taker_fee_rate: float                   = 0.00032,
    slippage_rate:  float                   = 0.00002,
    capital_cfg:    Optional[CapitalConfig] = None,
) -> TradingPipeline:
    """
    Micro VWAP Reclaim 均值回歸 Pipeline（極簡版）。

    [Gate]  PositionGateStage + CooldownStage
    [Alpha] MicroVWAPReclaimSignal
              sweep bar z < −z_threshold → 執行棒 tick reclaim 跨越 −Nσ 門檻
    [POC]   MicroVWAPPOCTargetStage
              TP = Rolling 8h VP POC，RR ≥ 1.5，費用驗證，計算倉位
    """
    shared_vp = _RollingVPBuilder(window=rolling_window, tick_size=tick_size)

    return TradingPipeline([
        PositionGateStage(max_positions=max_positions),
        CooldownStage(cooldown_ms=cooldown_ms),
        AlphaStage(
            modules = [MicroVWAPReclaimSignal(
                window      = rolling_window,
                lookback    = lookback,
                z_threshold = z_threshold,
                sl_offset   = sl_offset,
            )],
            mode = "OR",
        ),
        MicroVWAPPOCTargetStage(
            vp_builder     = shared_vp,
            min_rr         = min_rr,
            taker_fee_rate = taker_fee_rate,
            slippage_rate  = slippage_rate,
            capital_cfg    = capital_cfg,
        ),
    ])


def build_micro_vwap_reclaim_pipeline_def(
    name:              str           = "micro_vwap_reclaim",
    allocation_weight: float         = 1.0,
    tags:              Sequence[str] = ("micro_vwap", "mean_reversion", "long_only"),
    **pipeline_kwargs,
) -> PipelineDef:
    return PipelineDef(
        name              = name,
        pipeline          = build_micro_vwap_reclaim_pipeline(**pipeline_kwargs),
        allocation_weight = allocation_weight,
        tags              = list(tags),
    )


# ── UI 可用包裝 ────────────────────────────────────────────────────────────────

class VWAPReclaimPipelineStrategy(MultiPipelineStrategy):
    """
    VWAP Reclaim 均值回歸 Pipeline 的 UI 可用包裝。

    無參數實例化（STRATEGY_REGISTRY 以 cls() 建立）；
    使用 build_vwap_reclaim_pipeline() 的預設參數。
    """

    name = "VWAP Reclaim Pipeline"

    def __init__(self) -> None:
        defn   = build_vwap_reclaim_pipeline_def()
        runner = MultiPipelineRunner(defs=[defn])
        super().__init__(
            runner         = runner,
            exit_mod       = ExitModule(ExitConfig(tp_rr_ratio=2.0)),
            initial_equity = 10_000.0,
        )


class MicroVWAPReclaimStrategy(MultiPipelineStrategy):
    """
    Micro VWAP Reclaim Pipeline 的 UI 可用包裝。

    8h Rolling VWAP，sweep 跌破 −2σ → tick reclaim 入場，
    TP = Rolling VP POC（≥1.5RR），SL = sweep bar low。
    """

    name = "Micro VWAP Reclaim"

    def __init__(self) -> None:
        defn   = build_micro_vwap_reclaim_pipeline_def()
        runner = MultiPipelineRunner(defs=[defn])
        super().__init__(
            runner         = runner,
            exit_mod       = ExitModule(ExitConfig(tp_rr_ratio=1.5)),
            initial_equity = 10_000.0,
        )
