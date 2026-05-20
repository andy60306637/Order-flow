"""
strategies/pipeline/vp_reclaim.py — VP Reclaim 均值回歸策略

策略邏輯：
  前一棒（sweep bar）盤中跌破 VAL（Volume Area Low），
  信號棒（k0 = reclaim bar）收盤回到 VAL 上方，代表賣方動能耗盡、多方吸收。
  以信號棒下一根 K 棒 tick 觸發入場，捕捉 VAL 以上的均值回歸空間。

  對齊 research/factors.py VolumeProfileValReclaimLongFactor（vp_val_reclaim_long）：
    sweep  bar：klines[idx-2].low < VAL（50-bar rolling VP，以 k0 位置計算代理）
    reclaim bar：k0.close > VAL
    score：       k0 的 lower_wick_ratio ≥ min_wick_ratio

  入場觸發：k0 成立後的下一根 K 棒 tick 資料需有 tick 高於 k0 高點；
            若無 tick 資料則降級為 kline 模式（pass through）。

  Regime 維度（獨立過濾，無 combo stage）：
    market_vol_regime ∈ {NEUTRAL}
    vwap_dev          ∈ {extended_low, overextended_low, extreme_low}
    vol_profile       ∈ {below_VAL_reclaim, price_in_val_band, below_POC, in_value_area}

  below_VAL_reclaim label（VPReclaimVolumeProfileRegimeComponent 新增）：
    執行棒（idx）的價格在 VAL 附近或以上，且前兩棒呈現 sweep+reclaim 型態：
      klines[idx-2].low < val  AND  klines[idx-1].close > val
    父類 label 為 {price_in_val_band, below_POC, in_value_area} 時覆寫為此標籤。

Pipeline 流程：
  Gate     PositionGateStage              — 同時間最多 1 筆
  Stage 1  RegimeStage                   — market_vol × vwap_dev × vol_profile 獨立過濾
  Stage 2  AlphaStage[VPReclaimLong]    — sweep+reclaim 型態 + lower_wick_ratio + tick 觸發
  Stage 2.5 EnhancerStage               — 預留插槽（empty）
  Stage 3  EntryManagementStage          — ATR(14) 停損 + max_sl_pct cap
  Stage 4  RRStage(2.0R)               — qty 計算（6-24 bars horizon）
  Stage 5  FeeCoverRatioStage           — 0.032% taker + 0.2bps 滑點, cover ratio 1.5
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

import numpy as np

from core.data_types import Kline
from strategies.base import TickBarMap
from strategies.modules.capital_management import CapitalConfig
from strategies.modules.exit_management import ExitConfig, ExitModule
from strategies.modules.signal_trigger import SignalModule
from strategies.pipeline.component import (
    MarketVolatilityRegimeComponent,
    RegimeClassifier,
    VolumeProfileComponent,
)
from strategies.pipeline.context import PipelineContext
from strategies.pipeline.definition import PipelineDef
from strategies.pipeline.pipeline import TradingPipeline
from strategies.pipeline.runner import MultiPipelineRunner
from strategies.pipeline.stages import (
    AlphaStage,
    EnhancerStage,
    PipelineStage,
    PositionGateStage,
    RegimeStage,
    RRStage,
)
from strategies.pipeline.strategy import MultiPipelineStrategy
from strategies.pipeline.mean_reversion import (
    EntryManagementStage,
    FeeCoverRatioStage,
    VWAPDeviationRegimeComponent,
    _mr_long_entry,
)
from strategies.pipeline.mean_reversion_reclaim import VolumeProfileRegimeComponent


# ── Regime 維度白名單（獨立過濾，單一真相來源）────────────────────────────────

_ALLOWED_MARKET_VOLS: frozenset[str] = frozenset({"NEUTRAL"})

_ALLOWED_VWAP_ZONES: frozenset[str] = frozenset({
    "extended_low",
    "overextended_low",
    "extreme_low",
})

_ALLOWED_VP_LABELS: frozenset[str] = frozenset({
    "below_VAL_reclaim",
    "price_in_val_band",
    "below_POC",
    "in_value_area",
})


# ── VPReclaimVolumeProfileRegimeComponent ────────────────────────────────────

class VPReclaimVolumeProfileRegimeComponent(VolumeProfileRegimeComponent):
    """
    在 VolumeProfileRegimeComponent 基礎上新增 below_VAL_reclaim 標籤。

    below_VAL_reclaim 定義（對齊 vp_val_reclaim_long 因子的兩棒邏輯）：
      執行棒（idx）的基礎 label 為 {price_in_val_band, below_POC, in_value_area}，
      且前兩棒呈現 sweep + reclaim 型態：
        klines[idx-2].low < val  （sweep bar 跌破 VAL）
        klines[idx-1].close > val（reclaim bar 收盤回到 VAL 上方）
      → 覆寫為 below_VAL_reclaim。

    使用當前 idx 的 VAL 作為 sweep/reclaim 判斷的代理值（VAL 相鄰棒間變化小）。
    """

    dimension = "vol_profile"

    _RECLAIM_ELIGIBLE = frozenset({"price_in_val_band", "below_POC", "in_value_area"})

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        result = super().compute(klines, idx, tick_map)

        if idx < 2:
            return result
        if result.get("label") not in self._RECLAIM_ELIGIBLE:
            return result

        val = result.get("val")
        if val is None:
            return result

        sweep_bar   = klines[idx - 2]
        reclaim_bar = klines[idx - 1]

        if sweep_bar.low < val and reclaim_bar.close > val:
            return {**result, "label": "below_VAL_reclaim"}

        return result


# ── VPReclaimLongSignal ───────────────────────────────────────────────────────

class VPReclaimLongSignal(SignalModule):
    """
    VP Reclaim 多單訊號。

    對齊 research/factors.py VolumeProfileValReclaimLongFactor（vp_val_reclaim_long）：
      - sweep bar (klines[idx-2])：low < VAL（以 k0 位置的 VP 計算代理）
      - k0 = reclaim bar (klines[idx-1])：close > VAL
      - score：k0 的 lower_wick_ratio ≥ min_wick_ratio

    使用 50-bar 滾動 VP 計算 VAL（對齊因子的 window=50）。

    入場觸發（entry_conditions）：
      執行棒 tick 資料需有至少一個 tick 高於 k0 高點，否則阻斷。
      若 tick_map 為 None（非 tick 模式），直接通過。
    """

    name = "VPReclaimLong"

    def __init__(
        self,
        vp_interval:     str   = "1h",
        vp_window:       int   = 24,
        tick_size:       float = 1.0,
        value_area_pct:  float = 0.70,
        touch_band_pct:  float = 0.001,
        min_wick_ratio:  float = 0.30,
        sl_offset:       float = 0.0,
        min_micro_cvd:   float = 0.0,
    ) -> None:
        self._vp_comp = VolumeProfileComponent(
            interval       = vp_interval,
            window         = vp_window,
            tick_size      = tick_size,
            value_area_pct = value_area_pct,
            touch_band_pct = touch_band_pct,
        )
        self.min_wick_ratio = min_wick_ratio
        self.sl_offset      = sl_offset
        self.min_micro_cvd  = min_micro_cvd

    def can_trade(self, klines: list[Kline], idx: int) -> bool:
        return idx >= self._vp_comp.window + 2

    def detect_k0(self, klines: list[Kline], idx: int) -> dict | None:
        k0     = klines[idx - 1]   # reclaim bar
        k_prev = klines[idx - 2]   # sweep bar

        # 計算 k0 位置的 Volume Profile（不走 SharedContext，與 ValReclaimLongSignal 一致）
        vp  = self._vp_comp.compute(klines, idx - 1)
        if vp.get("source") == "insufficient_data":
            return None

        val = vp.get("val")
        poc = vp.get("poc_price")
        if val is None or poc is None:
            return None

        # vp_val_reclaim_long 兩棒條件
        if k_prev.low >= val:   # sweep bar 未跌破 VAL
            return None
        if k0.close <= val:     # reclaim bar 未收回 VAL 上方
            return None

        # POC 必須在 VAL 上方（確保 TP 空間存在）
        if poc <= val:
            return None

        # Score：k0 的 lower_wick_ratio（對齊因子）
        rng = k0.high - k0.low
        if rng <= 0:
            return None
        body_lo    = min(k0.open, k0.close)
        wick_ratio = (body_lo - k0.low) / rng

        if wick_ratio < self.min_wick_ratio:
            return None

        return {
            "direction":    "long",
            "k0_idx":       idx - 1,
            "k0_low":       k0.low,
            "val":          val,
            "poc":          poc,
            "wick_ratio":   wick_ratio,
            "sweep_depth":  val - k_prev.low,
            "reclaim_size": k0.close - val,
        }

    def entry_conditions(
        self,
        klines:   list[Kline],
        k0_idx:   int,
        k0_meta:  dict,
        tick_map  = None,
    ):
        signal_bar = klines[k0_meta["k0_idx"]]
        trigger    = signal_bar.high

        if tick_map is not None:
            exec_bar = klines[k0_idx]
            ticks    = tick_map.get(exec_bar.open_time)
            if ticks is not None and len(ticks) > 0:
                if not np.any(ticks[:, 1] > trigger):
                    return None

        return _mr_long_entry(
            klines,
            k0_idx,
            k0_meta["k0_low"],
            self.sl_offset,
            label         = "MR_VP_RECLAIM",
            meta          = {
                "val":          k0_meta["val"],
                "poc":          k0_meta["poc"],
                "wick_ratio":   k0_meta["wick_ratio"],
                "sweep_depth":  k0_meta["sweep_depth"],
                "reclaim_size": k0_meta["reclaim_size"],
                "k0_idx":       k0_meta["k0_idx"],
            },
            tick_map      = tick_map,
            trigger_price = trigger,
            min_micro_cvd = self.min_micro_cvd,
        )


# ── Factory ──────────────────────────────────────────────────────────────────

def build_vp_reclaim_pipeline(
    *,
    # Gate
    max_positions:    int = 1,
    # Regime — MarketVolatility
    mv_rv_period:  int = 60,
    mv_atr_short:  int = 10,
    mv_atr_long:   int = 60,
    mv_er_period:  int = 30,
    mv_adx_period: int = 14,
    mv_lookback:   int = 100,
    allowed_market_vols: frozenset[str] = _ALLOWED_MARKET_VOLS,
    # Regime — VWAP deviation
    vwap_window:      int   = 120,
    vwap_lookback:    int   = 300,
    vwap_oe_low:      float = 2.0,
    vwap_oe_high:     float = 2.5,
    allowed_vwap_zones: frozenset[str] = _ALLOWED_VWAP_ZONES,
    # Regime — Volume Profile (Reclaim)
    vp_interval:       str   = "1h",
    vp_window:         int   = 24,
    vp_tick_size:      float = 1.0,
    vp_value_area_pct: float = 0.70,
    vp_touch_band_pct: float = 0.001,
    allowed_vp_labels: frozenset[str] = _ALLOWED_VP_LABELS,
    # Signal
    min_wick_ratio: float = 0.30,
    sl_offset:      float = 0.0,
    min_micro_cvd:  float = 0.0,
    # Enhancer（預留插槽，空時無額外開銷）
    enhancer_modules: list | None = None,
    # Entry management
    atr_period:   int   = 14,
    atr_k:        float = 1.0,
    max_sl_pct:   float = 0.03,
    min_stop_pct: float = 0.0015,
    # RR
    rr_ratio:    float                   = 2.0,
    capital_cfg: Optional[CapitalConfig] = None,
    # Fee
    taker_fee_rate:  float = 0.00032,
    slippage_rate:   float = 0.00002,
    fee_cover_ratio: float = 1.5,
) -> TradingPipeline:
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

    vp_comp = VPReclaimVolumeProfileRegimeComponent(
        interval       = vp_interval,
        window         = vp_window,
        tick_size      = vp_tick_size,
        value_area_pct = vp_value_area_pct,
        touch_band_pct = vp_touch_band_pct,
    )

    regime_stage = RegimeStage(
        components = [mv_comp, vwap_comp, vp_comp],
        allowed    = {
            "market_vol_regime": allowed_market_vols,
            "vwap_dev":          allowed_vwap_zones,
            "vol_profile":       allowed_vp_labels,
        },
    )

    signal = VPReclaimLongSignal(
        vp_interval    = vp_interval,
        vp_window      = vp_window,
        tick_size      = vp_tick_size,
        value_area_pct = vp_value_area_pct,
        touch_band_pct = vp_touch_band_pct,
        min_wick_ratio = min_wick_ratio,
        sl_offset      = sl_offset,
        min_micro_cvd  = min_micro_cvd,
    )
    alpha_stage    = AlphaStage(modules=[signal])
    enhancer_stage = EnhancerStage(modules=enhancer_modules)

    entry_stage = EntryManagementStage(
        atr_period   = atr_period,
        atr_k        = atr_k,
        max_sl_pct   = max_sl_pct,
        min_stop_pct = min_stop_pct,
    )

    rr_stage = RRStage(
        exit_cfg    = ExitConfig(tp_rr_ratio=rr_ratio),
        capital_cfg = capital_cfg or CapitalConfig(),
        min_rr      = rr_ratio,
    )

    fee_stage = FeeCoverRatioStage(
        taker_fee_rate  = taker_fee_rate,
        slippage_rate   = slippage_rate,
        fee_cover_ratio = fee_cover_ratio,
    )

    return TradingPipeline([
        PositionGateStage(max_positions=max_positions),
        regime_stage,
        alpha_stage,
        enhancer_stage,
        entry_stage,
        rr_stage,
        fee_stage,
    ])


def build_vp_reclaim_pipeline_def(
    name:              str           = "VP Reclaim",
    allocation_weight: float         = 1.0,
    tags:              Sequence[str] = ("mean_reversion", "vp_reclaim", "long_only"),
    **pipeline_kwargs,
) -> PipelineDef:
    """
    便利包裝：直接回傳 PipelineDef（可傳入 MultiPipelineRunner）。

    **pipeline_kwargs 全部轉給 build_vp_reclaim_pipeline()。
    """
    pipeline = build_vp_reclaim_pipeline(**pipeline_kwargs)
    return PipelineDef(
        name              = name,
        pipeline          = pipeline,
        allocation_weight = allocation_weight,
        tags              = list(tags),
    )


# ── UI 可用的具名包裝類別 ──────────────────────────────────────────────────────

class VPReclaimPipelineStrategy(MultiPipelineStrategy):
    """
    VP Reclaim Pipeline 的 UI 可用包裝。

    無參數實例化（UI 的 STRATEGY_REGISTRY 以 cls() 建立策略），
    使用 build_vp_reclaim_pipeline() 的預設參數。
    """

    name = "VP Reclaim Pipeline"

    def __init__(self) -> None:
        defn   = build_vp_reclaim_pipeline_def()
        runner = MultiPipelineRunner(defs=[defn])
        super().__init__(
            runner         = runner,
            exit_mod       = ExitModule(ExitConfig(tp_rr_ratio=2.0)),
            initial_equity = 10_000.0,
        )
