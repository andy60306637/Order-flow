"""
strategies/pipeline/mean_reversion.py — 均值回歸 Pipeline 策略

Pipeline 流程：
  1. RegimeStage           → MarketVolatilityRegimeComponent（僅 MEAN_REVERSION）
                           + VWAPDeviationRegimeComponent（低乖離區帶過濾）
                           + SessionComponent（亞洲/倫敦/紐約/Overlap）
  2. AlphaStage            → LowerWickDeltaEffSignal ‖ CVDDivergenceSignal ‖ ReversalBarUpSignal
                             trigger = signal_bar.HIGH，Micro-CVD 雙重驗證
  3. EntryManagementStage  → ATR(14) 停損 + max_sl_pct cap
                             出場骨架：TP/SL/Time(TODO)/Info(TODO)/Regime(TODO)
  3b. VolumeAreaStage      → TBD（設計中，暫保留）
  4. RRStage               → 2RR 停利
  4b. FeeCoverRatioStage   → 費用覆蓋率（對標 WickReversalV4）

費用覆蓋率公式（對標 WickReversalV4._risk_covers_cost）：
  round_trip_cost = 2 × (taker_fee_rate + slippage_rate) × entry_price
  gross_reward    = risk × rr
  pass if gross_reward >= round_trip_cost × fee_cover_ratio
  ↔ risk >= round_trip_cost × fee_cover_ratio / rr

預設費用參數（WickReversalV4 基準，多單）：
  taker_fee_rate  = 0.00032  （0.032%）
  slippage_rate   = 0.00002  （0.2 bps）
  fee_cover_ratio = 1.2
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

import numpy as np

from core.data_types import Kline
from strategies.base import StrategySignal, TickBarMap
from strategies.modules.capital_management import CapitalConfig
from strategies.modules.exit_management import ExitConfig, ExitModule
from strategies.modules.signal_trigger import SignalModule
from strategies.pipeline.component import (
    ATRComponent,
    MarketVolatilityRegimeComponent,
    RegimeClassifier,
    SessionComponent,
    VolumeProfileComponent,
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


# ── VWAPDeviationRegimeComponent ─────────────────────────────────────────────

class VWAPDeviationRegimeComponent(RegimeClassifier):
    """
    VWAP 乖離 Regime 分類器（dimension = "vwap_dev"）。

    包裝 VWAPDeviationComponent，將 zone 欄位作為 label 供 RegimeStage 過濾。

    Zone → label 對照：
      normal            |z| < 1.0
      extended_low      1.0 ≤ |z| < 2.0，收盤低於 VWAP
      extended_high     1.0 ≤ |z| < 2.0，收盤高於 VWAP
      overextended_low  2.0 ≤ |z| ≤ 2.5，收盤低於 VWAP（均值回歸多單首選區帶）
      overextended_high 2.0 ≤ |z| ≤ 2.5，收盤高於 VWAP
      extreme_low       |z| > 2.5，收盤低於 VWAP
      extreme_high      |z| > 2.5，收盤高於 VWAP

    均值回歸（僅做多）建議 allowed_vwap_zones=("extended_low", "overextended_low")。
    """

    dimension = "vwap_dev"

    def __init__(
        self,
        window:            int   = 24,
        lookback:          int   = 100,
        overextended_low:  float = 2.0,
        overextended_high: float = 2.5,
    ) -> None:
        self._comp = VWAPDeviationComponent(
            window            = window,
            lookback          = lookback,
            overextended_low  = overextended_low,
            overextended_high = overextended_high,
        )
        self.component_id = self._comp.component_id

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        result = self._comp.compute(klines, idx, tick_map)
        return {**result, "label": result["zone"]}


# ── VolumeAreaStage ───────────────────────────────────────────────────────────

class VolumeAreaStage(PipelineStage):
    """
    Value Area 過濾器。

    從 SharedContext 取得 VolumeProfileComponent 計算結果，
    當且僅當收盤價落在 VAL~VAH 之間（in_value_area=True）才允許後續 Stage 執行。

    適合均值回歸策略：在成交密集帶內尋找回歸訊號，
    避免價格已突破 Value Area 後追高殺低。

    計算結果寫入 ctx.regime_meta["volume_area"]，包含 poc/vah/val/source。
    """

    name = "VolumeAreaStage"

    def __init__(self, component: VolumeProfileComponent) -> None:
        self.component = component

    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        vp = ctx.shared.get_or_compute(
            self.component.component_id,
            lambda: self.component.compute(ctx.klines, ctx.idx, ctx.tick_map),
        )

        if not vp.get("in_value_area", False):
            return None

        ctx.regime_meta["volume_area"] = {
            "poc_price":    vp["poc_price"],
            "vah":          vp["vah"],
            "val":          vp["val"],
            "total_volume": vp["total_volume"],
            "source":       vp["source"],
        }
        return ctx


# ── ReversalBarUpSignal ───────────────────────────────────────────────────────

class ReversalBarUpSignal(SignalModule):
    """
    均值回歸 reversal_bar_up 訊號模組（僅做多）。

    detect_k0 評估「前一根 K 棒」（klines[idx-1]）是否符合 reversal_bar_up：
      1. K 棒振幅 > 最近 sma_period 根的平均振幅
      2. 下影線比例 = (body_low - low) / range >= min_lower_wick_ratio（預設 0.5）
      3. 收盤位置   = (close - low) / range    >= min_close_pos（預設 0.6）

    entry_conditions 在「當前 K 棒」（klines[idx]，即信號K棒的下一根）執行：
      - 有 tick_map → 取該 K 棒第一個 tick 價格作為實際成交點
      - 無 tick_map → 取開盤價

    停損：信號K棒（klines[idx-1]）的最低點 - sl_offset
    """

    name = "ReversalBarUp"

    def __init__(
        self,
        sma_period:           int   = 20,
        min_lower_wick_ratio: float = 0.5,
        min_close_pos:        float = 0.6,
        sl_offset:            float = 0.0,
        min_micro_cvd:        float = 0.0,
    ) -> None:
        self.sma_period           = sma_period
        self.min_lower_wick_ratio = min_lower_wick_ratio
        self.min_close_pos        = min_close_pos
        self.sl_offset            = sl_offset
        self.min_micro_cvd        = min_micro_cvd

    def can_trade(self, klines: list[Kline], idx: int) -> bool:
        # 需要 idx-1 作為信號K棒，加上 sma_period 根歷史
        return idx >= self.sma_period + 1

    def detect_k0(self, klines: list[Kline], idx: int) -> Optional[dict]:
        k0 = klines[idx - 1]  # 信號K棒 = 當前K棒的前一根
        rng = k0.high - k0.low
        if rng <= 0:
            return None

        # 計算平均振幅（使用信號K棒之前的歷史，不含 k0 本身，避免前視偏差）
        hist_end = idx - 1
        hist_start = max(0, hist_end - self.sma_period)
        hist = klines[hist_start:hist_end]
        if len(hist) < self.sma_period:
            return None
        avg_rng = float(np.mean([k.high - k.low for k in hist[-self.sma_period:]]))

        if rng <= avg_rng:
            return None

        body_lo           = min(k0.open, k0.close)
        lower_wick_ratio  = (body_lo - k0.low) / rng
        close_pos         = (k0.close - k0.low) / rng

        if lower_wick_ratio < self.min_lower_wick_ratio:
            return None
        if close_pos < self.min_close_pos:
            return None

        return {
            "direction":         "long",
            "k0_idx":            idx - 1,
            "k0_low":            k0.low,
            "lower_wick_ratio":  lower_wick_ratio,
            "close_pos":         close_pos,
            "rng":               rng,
            "avg_rng":           avg_rng,
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
            label         = "MR_RBU",
            meta          = {
                "lower_wick_ratio": k0_meta["lower_wick_ratio"],
                "close_pos":        k0_meta["close_pos"],
                "rng":              k0_meta["rng"],
                "avg_rng":          k0_meta["avg_rng"],
                "k0_idx":           k0_meta["k0_idx"],
            },
            tick_map      = tick_map,
            trigger_price = signal_bar.high,
            min_micro_cvd = self.min_micro_cvd,
        )


# ── 共用進場工具函式 ──────────────────────────────────────────────────────────

def _mr_long_entry(
    klines:        list[Kline],
    k0_idx:        int,          # execution bar index (ctx.idx)
    k0_low:        float,        # signal bar low（初步停損基準，EntryManagementStage 用 ATR 覆蓋）
    sl_offset:     float,
    label:         str,
    meta:          dict,
    tick_map:      Optional[TickBarMap],
    trigger_price: float,        # signal bar HIGH → 進場觸發線
    min_micro_cvd: float = 0.0,  # execution bar 累積 Micro-CVD 最低門檻
) -> Optional[StrategySignal]:
    """
    均值回歸多單進場：Tick-first trigger + Micro-CVD 雙重驗證。

    掃描 execution bar tick 流（按時間順序），累計 Micro-CVD：
      micro_cvd += buy_delta − sell_delta  (is_buyer_maker=True → sell aggressor)
    首個滿足 tick_price >= trigger_price AND micro_cvd > min_micro_cvd 的 tick 即進場。

    kline fallback（無 tick_map 時）：
      exec_bar.high >= trigger_price AND kline_delta > min_micro_cvd
      fill_price = max(exec_bar.open, trigger_price)

    stop_price = k0_low − sl_offset（初步值，由 EntryManagementStage 以 ATR 覆蓋）
    """
    entry_bar  = klines[k0_idx]
    stop_price = k0_low - sl_offset

    fill_price: Optional[float] = None
    fill_time:  Optional[int]   = None
    hit_micro_cvd: float        = 0.0

    # ── Tick-first ────────────────────────────────────────────────────────────
    if tick_map is not None:
        ticks = tick_map.get(entry_bar.open_time)
        if ticks is not None and len(ticks) > 0:
            micro_cvd = 0.0
            for tick in ticks:
                t_price = float(tick[1])
                t_qty   = float(tick[2])
                t_is_bm = bool(tick[3])
                # is_buyer_maker=True → sell aggressor → negative delta
                micro_cvd += -t_qty if t_is_bm else t_qty
                if t_price >= trigger_price and micro_cvd > min_micro_cvd:
                    fill_price    = t_price
                    fill_time     = int(tick[0])
                    hit_micro_cvd = micro_cvd
                    break

    # ── kline fallback ────────────────────────────────────────────────────────
    if fill_price is None:
        kline_delta = entry_bar.taker_buy_volume - (entry_bar.volume - entry_bar.taker_buy_volume)
        if entry_bar.high >= trigger_price and kline_delta > min_micro_cvd:
            fill_price    = max(entry_bar.open, trigger_price)
            fill_time     = entry_bar.open_time
            hit_micro_cvd = kline_delta

    if fill_price is None or fill_price <= stop_price:
        return None

    return StrategySignal(
        open_time   = entry_bar.open_time,
        price       = entry_bar.open,
        signal_type = "long_entry",
        label       = label,
        stop_price  = stop_price,
        fill_price  = fill_price,
        fill_time   = fill_time,
        meta        = {
            **meta,
            "trigger_price": trigger_price,
            "micro_cvd":     hit_micro_cvd,
            "fill_time":     fill_time,
        },
    )


# ── LowerWickRatioSignal ──────────────────────────────────────────────────────

class LowerWickRatioSignal(SignalModule):
    """
    下影線比例因子（lower_wick_ratio）。

    評估信號K棒（klines[idx-1]）的下影線占比：
      lower_wick = min(open, close) − low
      wick_ratio = lower_wick / range
    """
    name = "LowerWickRatio"

    def __init__(
        self,
        min_wick_ratio: float = 0.50,
        sl_offset:      float = 0.0,
        min_micro_cvd:  float = 0.0,
    ) -> None:
        self.min_wick_ratio = min_wick_ratio
        self.sl_offset      = sl_offset
        self.min_micro_cvd  = min_micro_cvd

    def can_trade(self, klines: list[Kline], idx: int) -> bool:
        return idx >= 1

    def detect_k0(self, klines: list[Kline], idx: int) -> Optional[dict]:
        k0  = klines[idx - 1]
        rng = k0.high - k0.low
        if rng <= 0:
            return None

        body_lo    = min(k0.open, k0.close)
        lower_wick = body_lo - k0.low
        wick_ratio = lower_wick / rng
        if wick_ratio < self.min_wick_ratio:
            return None

        return {
            "direction":  "long",
            "k0_idx":     idx - 1,
            "k0_low":     k0.low,
            "wick_ratio": wick_ratio,
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
            label         = "MR_LWR",
            meta          = {
                "wick_ratio": k0_meta["wick_ratio"],
                "k0_idx":     k0_meta["k0_idx"],
            },
            tick_map      = tick_map,
            trigger_price = signal_bar.high,
            min_micro_cvd = self.min_micro_cvd,
        )


# ── CVDDivergenceSignal ───────────────────────────────────────────────────────

class CVDDivergenceSignal(SignalModule):
    """
    CVD 背離因子（Cumulative Volume Delta Divergence）。

    以 window 根 K 棒計算滾動累積買賣盤差（CVD）：
      delta_j = taker_buy_j − taker_sell_j
      cvd_j   = Σ delta_k  (k ∈ [window_start, j])

    牛背離觸發條件（k0 = klines[idx-1] 為信號棒）：
      1. k0.low ≤ prev_trough.low × (1 + price_tolerance)
         （k0 價格在視窗最低點附近或更低，確認空頭未放棄）
      2. cvd_k0 > cvd_prev_trough (if not flipped)
         （CVD 正背離：價格創低，但累積買盤比前低點時更強）
         cvd_k0 < cvd_prev_trough (if flipped)
         （CVD 負向：價格創低，買盤更弱，測試負 Alpha 翻轉）

    直覺：空方持續壓低價格，但每次創低時買盤吸收力逐漸增強 → 空頭動能衰竭。
    """

    name = "CVDDivergence"

    def __init__(
        self,
        window:             int   = 20,
        price_tolerance:    float = 0.002,
        min_cvd_divergence: float = 0.0,
        sl_offset:          float = 0.0,
        min_micro_cvd:      float = 0.0,
        flipped:            bool  = False,  # 是否反轉背離邏輯
    ) -> None:
        self.window             = window
        self.price_tolerance    = price_tolerance
        self.min_cvd_divergence = min_cvd_divergence
        self.sl_offset          = sl_offset
        self.min_micro_cvd      = min_micro_cvd
        self.flipped            = flipped

    def can_trade(self, klines: list[Kline], idx: int) -> bool:
        return idx >= self.window + 1

    def detect_k0(self, klines: list[Kline], idx: int) -> Optional[dict]:
        k0 = klines[idx - 1]

        # 歷史視窗：k0 之前的 window 根棒（不含 k0）
        hist_start = max(0, idx - 1 - self.window)
        hist       = klines[hist_start : idx - 1]
        if len(hist) < 2:
            return None

        # 找歷史視窗中的最低點棒
        prev_trough = min(hist, key=lambda k: k.low)
        if k0.low > prev_trough.low * (1.0 + self.price_tolerance):
            return None  # k0 不在近期低點區域

        # 計算滾動 CVD（kline fallback：taker_buy − taker_sell）
        all_bars   = hist + [k0]
        cumulative = 0.0
        cvd_map: dict[int, float] = {}
        for k in all_bars:
            cumulative       += k.taker_buy_volume - (k.volume - k.taker_buy_volume)
            cvd_map[k.open_time] = cumulative

        cvd_prev     = cvd_map[prev_trough.open_time]
        cvd_k0       = cvd_map[k0.open_time]

        if self.flipped:
            # 反轉邏輯：CVD 創低（更弱）時進場
            cvd_diverge = cvd_prev - cvd_k0
        else:
            # 正常牛背離：CVD 較高（較強）時進場
            cvd_diverge = cvd_k0 - cvd_prev

        if cvd_diverge <= self.min_cvd_divergence:
            return None

        return {
            "direction":       "long",
            "k0_idx":          idx - 1,
            "k0_low":          k0.low,
            "prev_trough_low": prev_trough.low,
            "cvd_k0":          cvd_k0,
            "cvd_prev":        cvd_prev,
            "cvd_divergence":  cvd_diverge,
            "is_flipped":      self.flipped,
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
            label         = "MR_CVDD_F" if k0_meta.get("is_flipped") else "MR_CVDD",
            meta          = {
                "prev_trough_low": k0_meta["prev_trough_low"],
                "cvd_k0":          k0_meta["cvd_k0"],
                "cvd_prev":        k0_meta["cvd_prev"],
                "cvd_divergence":  k0_meta["cvd_divergence"],
                "k0_idx":          k0_meta["k0_idx"],
                "is_flipped":      k0_meta.get("is_flipped"),
            },
            tick_map      = tick_map,
            trigger_price = signal_bar.high,
            min_micro_cvd = self.min_micro_cvd,
        )


# ── EntryManagementStage ─────────────────────────────────────────────────────

class EntryManagementStage(PipelineStage):
    """
    Stage 3：出入場管理。

    進場觸發邏輯由 AlphaStage（_mr_long_entry）完成：
      - trigger_price = signal_bar.HIGH
      - Micro-CVD 雙重驗證（execution bar tick 流）

    本 Stage 負責：
      1. ATR(atr_period) 停損計算，覆蓋 AlphaStage 的初步停損
           stop = signal_bar.low − ATR × atr_k
      2. 最大停損距離上限（防止高波動期 ATR 過大）
           cap_stop = entry_price × (1 − max_sl_pct)
           final_stop = max(stop, cap_stop)   ← 取較高（距離較小）
      3. 出場骨架（metadata 寫入 alpha_meta["exit_plan"]）：
           - 主目標出場 (TP)：由 RRStage 計算 2RR
           - 風險止損 (SL)：本 Stage 計算
           - 時間止損 (Time)：後續因子衰退分析後設置（預留欄位）
           - 資訊止損 (Info)：後續事件驅動規則（預留欄位）
           - Regime 變化出場：後續部屬（預留欄位）
    """

    name = "EntryManagementStage"

    def __init__(
        self,
        atr_period:   int   = 14,
        atr_k:        float = 1.0,
        max_sl_pct:   float = 0.03,   # 最大停損距離：入場價的 3%
        min_stop_pct: float = 0.0015, # 停損距離最小值（相對入場價），防止費用侵蝕
    ) -> None:
        self.atr_period   = atr_period
        self.atr_k        = atr_k
        self.max_sl_pct   = max_sl_pct
        self.min_stop_pct = min_stop_pct
        self._atr_comp    = ATRComponent(period=atr_period)

    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        if ctx.entry_price is None or not ctx.alpha_meta:
            return None

        k0_meta = ctx.alpha_meta.get("k0_meta", {})
        k0_idx  = k0_meta.get("k0_idx")
        if k0_idx is None:
            return None

        k0    = ctx.klines[k0_idx]
        entry = ctx.entry_price

        # ── ATR 停損 ────────────────────────────────────────────────────────────
        atr_result = ctx.shared.get_or_compute(
            self._atr_comp.component_id,
            lambda: self._atr_comp.compute(ctx.klines, ctx.idx, ctx.tick_map),
        )
        atr = atr_result["atr"]

        raw_stop = k0.low - atr * self.atr_k
        cap_stop = entry * (1.0 - self.max_sl_pct)
        stop     = max(raw_stop, cap_stop)   # 取較高（距離入場較近）

        # 停損距離下限（防止費用侵蝕）
        floor_stop = entry * (1.0 - self.min_stop_pct)
        if stop > floor_stop:   # stop 太高（距離太小），拒絕此筆交易
            return None

        if entry <= stop:
            return None

        ctx.stop_price = stop

        # ── 出場骨架（metadata）────────────────────────────────────────────────
        k0_range = k0.high - k0.low
        k0_range_atr = k0_range / atr if atr > 0 else 0.0

        ctx.alpha_meta["exit_plan"] = {
            "tp":           "2RR_baseline",       # RRStage 計算
            "sl":           stop,
            "sl_basis":     "ATR",
            "atr":          atr,
            "atr_k":        self.atr_k,
            "stop_pct":     (entry - stop) / entry, # 供診斷用
            "risk_pct":     (entry - stop) / entry, # 別名
            "k0_range_atr": k0_range_atr,
            "time":         None,                 # TODO: 因子衰退週期設置後填入
            "info":         None,                 # TODO: 事件驅動出場規則
            "regime":       None,                 # TODO: Regime 變化出場
        }
        return ctx


# ── FeeCoverRatioStage ────────────────────────────────────────────────────────

class FeeCoverRatioStage(PipelineStage):
    """
    費用覆蓋率過濾器，對標 WickReversalV4Strategy._risk_covers_cost()。

    公式：
      round_trip_cost = 2 × (taker_fee_rate + slippage_rate) × entry_price
      pass if risk × rr >= round_trip_cost × fee_cover_ratio
      等同  if risk >= round_trip_cost × fee_cover_ratio / rr

    直觀含義：
      毛利（risk × rr）至少要覆蓋往返成本的 fee_cover_ratio 倍。
      fee_cover_ratio=1.2 代表毛利須為往返費用的 1.2 倍以上。

    通過後填入：
      ctx.expected_fee  — (entry + tp) × qty × 費率（雙邊估算）
      ctx.net_reward    — 毛利 - 總費用
      ctx.fee_approved  — True
    """

    name = "FeeCoverRatioStage"

    def __init__(
        self,
        taker_fee_rate:  float = 0.0005,
        slippage_rate:   float = 0.0002,
        fee_cover_ratio: float = 1.2,
    ) -> None:
        self.taker_fee_rate  = taker_fee_rate
        self.slippage_rate   = slippage_rate
        self.fee_cover_ratio = fee_cover_ratio

    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        if None in (ctx.entry_price, ctx.stop_price, ctx.tp_price, ctx.qty, ctx.expected_rr):
            return None

        entry = ctx.entry_price
        stop  = ctx.stop_price
        tp    = ctx.tp_price
        qty   = ctx.qty
        rr    = ctx.expected_rr

        risk  = abs(entry - stop)
        if risk <= 0 or rr <= 0 or entry <= 0:
            return None

        round_trip_cost = 2.0 * (self.taker_fee_rate + self.slippage_rate) * entry
        min_risk = round_trip_cost * self.fee_cover_ratio / rr

        if risk < min_risk:
            return None

        # 填入費用欄位（與 FeeStage 欄位相容）
        rate         = self.taker_fee_rate + self.slippage_rate
        total_fee    = (entry * qty + tp * qty) * rate
        gross_reward = abs(tp - entry) * qty
        net_reward   = gross_reward - total_fee

        ctx.expected_fee = total_fee
        ctx.net_reward   = net_reward
        ctx.fee_approved = True
        return ctx


# ── Factory ───────────────────────────────────────────────────────────────────

def build_mean_reversion_pipeline(
    *,
    # ── Gate / Cooldown（pipeline 最前端）────────────────────────────────────
    max_positions: int = 1,
    cooldown_ms:   int = 300_000,   # 5 分鐘冷卻期
    # ── Stage 1：Regime ────────────────────────────────────────────────────────
    # MarketVolatilityRegimeComponent
    mv_rv_period:  int = 60,
    mv_atr_short:  int = 10,
    mv_atr_long:   int = 60,
    mv_er_period:  int = 30,
    mv_adx_period: int = 14,
    mv_lookback:   int = 100,
    # VWAPDeviationRegimeComponent
    vwap_window:          int            = 120,
    vwap_lookback:        int            = 300,
    vwap_oe_low:          float          = 2.0,
    vwap_oe_high:         float          = 2.5,
    allowed_vwap_zones:   Sequence[str]  = ("extended_low", "overextended_low"),
    # SessionComponent
    allowed_sessions:     Sequence[str]  = ("asian", "london", "ny", "overlap"),
    # ── Stage 2：Alpha（三因子 OR 組合）────────────────────────────────────────
    # 因子 a：LowerWickRatio（影線吸收因子，取代 LWDE）
    lw_min_wick_ratio: float = 0.50,
    # 因子 b：CVDDivergence（買賣盤背離）
    cvd_window:             int   = 20,
    cvd_price_tolerance:    float = 0.002,
    cvd_min_divergence:     float = 0.0,
    cvd_flipped:            bool  = True,   # 預設翻轉（因原始 IC 為負）
    # 因子 c：ReversalBarUp（型態因子）
    sma_period:           int   = 20,
    min_lower_wick_ratio: float = 0.5,
    min_close_pos:        float = 0.6,
    # 共用參數
    sl_offset:     float = 0.0,
    min_micro_cvd: float = 0.0,   # execution bar Micro-CVD 最低門檻
    # ── Stage 3：EntryManagement ──────────────────────────────────────────────
    atr_period:   int   = 14,
    atr_k:        float = 1.0,
    max_sl_pct:   float = 0.03,
    min_stop_pct: float = 0.0015,   # 停損距離下限，防止費用侵蝕
    # ── Stage 4：RR + Fee（WickReversalV4 基準）─────────────────────────────
    rr_ratio:        float                   = 1.5,      # V8 優化值：1.5RR
    time_decay_bars: int                     = 30,       # V8 優化值：30 根 K 棒時間離場
    capital_cfg:     Optional[CapitalConfig] = None,
    taker_fee_rate:  float                   = 0.00032,  # 0.032% taker fee
    slippage_rate:   float                   = 0.00002,  # 0.2 bps slippage
    fee_cover_ratio: float                   = 1.2,      # WickReversalV4 多單基準
    enabled_signals: Sequence[str]           = ("reversal",), # 預設僅使用 V8 表現最佳的 RBU
) -> TradingPipeline:
    """
    均值回歸 Pipeline 工廠函式。

    Gate     PositionGateStage     — 同時間最多 max_positions 筆（預設 1）
    Gate     CooldownStage         — 出場後 cooldown_ms 冷卻（預設 5 分鐘）
    Stage 1  RegimeStage          — MarketVolatilityRegimeComponent（僅 MEAN_REVERSION）
                                 + VWAPDeviationRegimeComponent（僅低乖離區帶）
                                 + SessionComponent（亞洲/倫敦/紐約/Overlap）
    Stage 2  AlphaStage          — LowerWickDeltaEffSignal
                                 ‖ CVDDivergenceSignal
                                 ‖ ReversalBarUpSignal
                                   （OR 組合，trigger = signal_bar.HIGH, Micro-CVD 驗證）
    Stage 3  EntryManagementStage — ATR(14) 停損 + max_sl_pct cap
                                    出場骨架：TP/SL/Time/Info(TODO)/Regime(TODO)
    Stage 4  RRStage + FeeCoverRatioStage

    範例（1m K線，Binance USDT Perp）：

        from strategies.pipeline.mean_reversion import build_mean_reversion_pipeline
        from strategies.pipeline import PipelineDef, MultiPipelineRunner
        from strategies.modules import CapitalConfig

        pipeline = build_mean_reversion_pipeline(
            allowed_vwap_zones=("extended_low", "overextended_low"),
            capital_cfg=CapitalConfig(max_risk_pct=1.0, leverage=20),
        )
        defn = PipelineDef("mean_rev", pipeline=pipeline, allocation_weight=1.0)
        runner = MultiPipelineRunner(defs=[defn])
    """
    # ── Stage 1 components ────────────────────────────────────────────────────
    mv_comp = MarketVolatilityRegimeComponent(
        rv_period  = mv_rv_period,
        atr_short  = mv_atr_short,
        atr_long   = mv_atr_long,
        er_period  = mv_er_period,
        adx_period = mv_adx_period,
        lookback   = mv_lookback,
    )
    vwap_comp = VWAPDeviationRegimeComponent(
        window            = vwap_window,
        lookback          = vwap_lookback,
        overextended_low  = vwap_oe_low,
        overextended_high = vwap_oe_high,
    )
    session_comp = SessionComponent()

    # ── Stage 2 signals ───────────────────────────────────────────────────────
    lower_wick_sig = LowerWickRatioSignal(
        min_wick_ratio = lw_min_wick_ratio,
        sl_offset      = sl_offset,
        min_micro_cvd  = min_micro_cvd,
    )
    cvd_sig = CVDDivergenceSignal(
        window             = cvd_window,
        price_tolerance    = cvd_price_tolerance,
        min_cvd_divergence = cvd_min_divergence,
        sl_offset          = sl_offset,
        min_micro_cvd      = min_micro_cvd,
        flipped            = cvd_flipped,
    )
    reversal_sig = ReversalBarUpSignal(
        sma_period           = sma_period,
        min_lower_wick_ratio = min_lower_wick_ratio,
        min_close_pos        = min_close_pos,
        sl_offset            = sl_offset,
        min_micro_cvd        = min_micro_cvd,
    )

    # ── 篩選啟用的訊號 ────────────────────────────────────────────────────────
    all_modules = {
        "lower_wick": lower_wick_sig,
        "cvd":        cvd_sig,
        "reversal":   reversal_sig,
    }
    active_modules = [all_modules[k] for k in enabled_signals if k in all_modules]
    if not active_modules:
        active_modules = [reversal_sig] # Fallback

    gate     = PositionGateStage(max_positions=max_positions)
    cooldown = CooldownStage(cooldown_ms=cooldown_ms)

    return TradingPipeline([
        gate,
        cooldown,
        RegimeStage(
            components = [mv_comp, vwap_comp, session_comp],
            allowed    = {
                "market_vol_regime": ["MEAN_REVERSION"],
                "vwap_dev":          list(allowed_vwap_zones),
                "session":           list(allowed_sessions),
            },
        ),
        AlphaStage(
            modules = active_modules,
            mode    = "OR",
        ),
        EntryManagementStage(
            atr_period   = atr_period,
            atr_k        = atr_k,
            max_sl_pct   = max_sl_pct,
            min_stop_pct = min_stop_pct,
        ),
        RRStage(
            exit_cfg    = ExitConfig(
                tp_rr_ratio     = rr_ratio,
                time_decay_bars = time_decay_bars,
            ),
            capital_cfg = capital_cfg or CapitalConfig(),
            min_rr      = rr_ratio,
        ),
        FeeCoverRatioStage(
            taker_fee_rate  = taker_fee_rate,
            slippage_rate   = slippage_rate,
            fee_cover_ratio = fee_cover_ratio,
        ),
    ])


def build_mean_reversion_pipeline_def(
    name:              str             = "mean_reversion",
    allocation_weight: float           = 1.0,
    tags:              Sequence[str]   = ("mean_reversion", "reversal", "long_only"),
    **pipeline_kwargs,
) -> PipelineDef:
    """
    便利包裝：直接回傳 PipelineDef（可傳入 MultiPipelineRunner）。

    **pipeline_kwargs 全部轉給 build_mean_reversion_pipeline()。
    """
    pipeline = build_mean_reversion_pipeline(**pipeline_kwargs)
    return PipelineDef(
        name              = name,
        pipeline          = pipeline,
        allocation_weight = allocation_weight,
        tags              = list(tags),
    )


# ── UI 可用的具名包裝類別 ─────────────────────────────────────────────────────

class MeanReversionPipelineStrategy(MultiPipelineStrategy):
    """
    均值回歸 Pipeline 的 UI 可用包裝。

    無參數實例化（UI 的 STRATEGY_REGISTRY 以 cls() 建立策略），
    使用 build_mean_reversion_pipeline() 的預設參數。

    倉位大小（qty）由回測引擎根據 UI 的 Capital / Leverage / Max Risk%
    重新計算；策略只輸出 fill_price / stop_price / signal_type。
    """

    name = "Mean Reversion Pipeline"

    def __init__(self) -> None:
        defn   = build_mean_reversion_pipeline_def()
        runner = MultiPipelineRunner(defs=[defn])
        super().__init__(
            runner         = runner,
            exit_mod       = ExitModule(ExitConfig(tp_rr_ratio=2.0)),
            initial_equity = 10_000.0,
        )

