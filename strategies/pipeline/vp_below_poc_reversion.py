"""
strategies/pipeline/vp_below_poc_reversion.py — VP Below POC 均值回歸策略

策略邏輯：
  以 VWAP 偏低（extended_low / overextended_low / extreme_low）且 Volume Profile
  POC 在當前收盤以上（below_POC / price_in_poc_band / below_VAL）為切入環境，
  使用 ATR 正規化距離（vp_below_poc_long 因子）及 POC 回歸潛力（poc_reversion_potential
  因子）雙重確認，以信號棒下一根 K 棒 tick 觸發入場，POC 作為 TP 目標上限。

  兩個 Primary 確認指標（AND 模式）：
    1. VP Dist（對齊 research/factors.py VolumeProfileBelowPocLongFactor）：
         (poc_50bar - close) / ATR(14) ≥ min_vp_dist
    2. POC Potential（對齊 research/mr_alpha_ic_factors.py PocReversionPotentialFactor）：
         clip((poc_20bar - close) / ATR(14), 0, 5.0) ≥ min_poc_potential

  入場觸發：信號棒成立後的下一根 K 棒 tick 資料需有 tick 高於信號棒高點；
            若無 tick 資料則降級為 kline 模式（pass through）。

  TP 調整（VPBelowPOCTPAdjustStage，dual entry + target filter 角色）：
    TP = min(baseline 2.0R TP, POC)
    若 actual_rr < min_rr_adj 則阻斷。

  Regime 維度（獨立過濾，無 session / market_vol 限制，無 combo stage）：
    vwap_dev    ∈ {extended_low, overextended_low, extreme_low}
    vol_profile ∈ {below_POC, price_in_poc_band, below_VAL}

Pipeline 流程：
  Gate     PositionGateStage              — 同時間最多 1 筆
  Stage 1  RegimeStage                   — vwap_dev × vol_profile 獨立過濾
  Stage 2  AlphaStage[VPBelowPOCLong]   — VP 距離 + POC 潛力 + tick 觸發確認
  Stage 3  EntryManagementStage          — ATR(14) 停損 + max_sl_pct cap
  Stage 4  RRStage(2.0R)               — qty 計算
  Stage 5  VPBelowPOCTPAdjustStage      — TP = min(POC, 2.0R), min_rr_adj 保護
  Stage 6  FeeCoverRatioStage           — 0.032% taker + 0.2bps 滑點, cover ratio 1.5
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

import numpy as np

from strategies.modules.capital_management import CapitalConfig
from strategies.modules.exit_management import ExitConfig, ExitModule
from strategies.modules.signal_trigger import SignalModule
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
from strategies.pipeline.ny_wick_reversal import POCBandVolumeProfileRegimeComponent


# ── Regime 維度白名單（獨立過濾，單一真相來源）────────────────────────────────

_ALLOWED_VWAP_ZONES: frozenset[str] = frozenset({
    "extended_low",
    "overextended_low",
    "extreme_low",
})

_ALLOWED_VP_LABELS: frozenset[str] = frozenset({
    "below_POC",
    "price_in_poc_band",
    "below_VAL",
})


# ── VPBelowPOCLongSignal ─────────────────────────────────────────────────────

class VPBelowPOCLongSignal(SignalModule):
    """
    VP Below POC 多單訊號。

    兩個 Primary 指標同時成立（AND）才觸發：
      1. VP Dist：(poc_primary - k0.close) / ATR ≥ min_vp_dist
         對齊 VolumeProfileBelowPocLongFactor（50-bar 滾動 VP）
      2. POC Potential：clip((poc_secondary - k0.close) / ATR, 0, max_poc_dist_atr) ≥ min_poc_potential
         對齊 PocReversionPotentialFactor（20-bar 滾動 VP, N_BINS=24）

    入場觸發（entry_conditions）：
      執行棒 tick 資料需有至少一個 tick 高於信號棒高點，否則阻斷。
      若 tick_map 為 None（非 tick 模式），直接通過。
    """

    name = "VPBelowPOCLong"

    def __init__(
        self,
        vp_window_primary:   int   = 50,
        vp_window_secondary: int   = 20,
        n_bins:              int   = 24,
        atr_period:          int   = 14,
        min_vp_dist:         float = 0.30,
        min_poc_potential:   float = 0.20,
        max_poc_dist_atr:    float = 5.0,
        sl_offset:           float = 0.0,
        min_micro_cvd:       float = 0.0,
    ) -> None:
        self.vp_window_primary   = vp_window_primary
        self.vp_window_secondary = vp_window_secondary
        self.n_bins              = n_bins
        self.atr_period          = atr_period
        self.min_vp_dist         = min_vp_dist
        self.min_poc_potential   = min_poc_potential
        self.max_poc_dist_atr    = max_poc_dist_atr
        self.sl_offset           = sl_offset
        self.min_micro_cvd       = min_micro_cvd

    # ── 靜態工具 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _rolling_poc(klines: list, end_idx: int, window: int, n_bins: int = 24) -> float | None:
        """Histogram-based POC over last `window` bars ending at end_idx (exclusive)."""
        start = max(0, end_idx - window)
        bars = klines[start:end_idx]
        if len(bars) < 3:
            return None
        price_min = min(b.low for b in bars)
        price_max = max(b.high for b in bars)
        price_range = price_max - price_min
        if price_range <= 0:
            return (price_min + price_max) / 2.0
        bin_size = price_range / n_bins
        bin_vols: list[float] = [0.0] * n_bins
        for bar in bars:
            lo_bin = max(0, min(int((bar.low  - price_min) / bin_size), n_bins - 1))
            hi_bin = max(0, min(int((bar.high - price_min) / bin_size), n_bins - 1))
            n_covered = max(hi_bin - lo_bin + 1, 1)
            vpb = bar.volume / n_covered
            for b in range(lo_bin, hi_bin + 1):
                bin_vols[b] += vpb
        poc_bin = bin_vols.index(max(bin_vols))
        return price_min + (poc_bin + 0.5) * bin_size

    @staticmethod
    def _rolling_atr(klines: list, end_idx: int, period: int = 14) -> float | None:
        """Simple ATR over last `period` bars ending at end_idx (exclusive)."""
        start = max(1, end_idx - period - 1)
        bars = klines[start:end_idx]
        if len(bars) < 2:
            return None
        trs = [
            max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - bars[i - 1].close),
                abs(bars[i].low  - bars[i - 1].close),
            )
            for i in range(1, len(bars))
        ]
        tail = trs[-period:]
        return sum(tail) / len(tail)

    # ── SignalModule 介面 ─────────────────────────────────────────────────────

    def can_trade(self, klines: list, idx: int) -> bool:
        return idx >= self.vp_window_primary + self.atr_period + 1

    def detect_k0(self, klines: list, idx: int) -> dict | None:
        k0 = klines[idx - 1]

        # Primary: 50-bar VP dist (aligned with vp_below_poc_long)
        poc_primary = self._rolling_poc(klines, idx, self.vp_window_primary, self.n_bins)
        if poc_primary is None or poc_primary <= k0.close:
            return None

        atr = self._rolling_atr(klines, idx, self.atr_period)
        if atr is None or atr <= 0:
            return None

        vp_dist = (poc_primary - k0.close) / atr
        if vp_dist < self.min_vp_dist:
            return None

        # Secondary: 20-bar VP reversion potential (aligned with poc_reversion_potential)
        poc_secondary = self._rolling_poc(klines, idx, self.vp_window_secondary, self.n_bins)
        if poc_secondary is not None and poc_secondary > k0.close:
            poc_potential = min((poc_secondary - k0.close) / atr, self.max_poc_dist_atr)
        else:
            poc_potential = 0.0

        if poc_potential < self.min_poc_potential:
            return None

        return {
            "direction":     "long",
            "k0_idx":        idx - 1,
            "k0_low":        k0.low,
            "poc_primary":   poc_primary,
            "poc_secondary": poc_secondary,
            "vp_dist":       vp_dist,
            "poc_potential": poc_potential,
            "atr":           atr,
        }

    def entry_conditions(
        self,
        klines:   list,
        k0_idx:   int,
        k0_meta:  dict,
        tick_map=None,
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
            label         = "MR_VP_BELOW_POC",
            meta          = {
                "vp_dist":       k0_meta["vp_dist"],
                "poc_potential": k0_meta["poc_potential"],
                "poc_primary":   k0_meta["poc_primary"],
                "k0_idx":        k0_meta["k0_idx"],
            },
            tick_map      = tick_map,
            trigger_price = trigger,
            min_micro_cvd = self.min_micro_cvd,
        )


# ── VPBelowPOCTPAdjustStage ──────────────────────────────────────────────────

class VPBelowPOCTPAdjustStage(PipelineStage):
    """
    POC-based TP 調整器（dual entry + target filter 角色）。

    前置：RRStage 已設定 ctx.tp_price（= entry + 2.0R）與 ctx.qty。
    本 Stage 從 SharedContext 讀取 POC，取 min(poc, baseline_tp) 作為最終 TP：

      TP = min(valid candidates > entry_price)

    候選：poc（若 poc > entry）、baseline_tp（恆有效）。
    若 actual_rr < min_rr_adj 則阻斷。
    需放在 RRStage 之後、FeeCoverRatioStage 之前。
    """

    name = "VPBelowPOCTPAdjustStage"

    def __init__(
        self,
        vp_comp:    POCBandVolumeProfileRegimeComponent,
        min_rr_adj: float = 0.8,
    ) -> None:
        self._vp        = vp_comp
        self.min_rr_adj = min_rr_adj

    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        if None in (ctx.entry_price, ctx.stop_price, ctx.tp_price):
            return None

        entry = ctx.entry_price
        stop  = ctx.stop_price
        risk  = abs(entry - stop)
        if risk < 1e-10:
            return None

        candidates: list[float] = [ctx.tp_price]

        vp  = ctx.shared.get_or_compute(
            self._vp.component_id,
            lambda: self._vp.compute(ctx.klines, ctx.idx, ctx.tick_map),
        )
        poc = vp.get("poc_price")
        if poc is not None and poc > entry:
            candidates.append(poc)

        tp        = min(candidates)
        actual_rr = (tp - entry) / risk

        if actual_rr < self.min_rr_adj:
            return None

        ctx.tp_price    = tp
        ctx.expected_rr = actual_rr

        if ctx.alpha_meta is not None:
            ctx.alpha_meta["tp_detail"] = {
                "poc_tp":    poc,
                "rr_tp":     candidates[0],
                "final_tp":  tp,
                "actual_rr": actual_rr,
            }
        return ctx


# ── Factory ──────────────────────────────────────────────────────────────────

def build_vp_below_poc_reversion_pipeline(
    *,
    # Gate
    max_positions:    int = 1,
    # Regime — VWAP deviation
    vwap_window:      int   = 120,
    vwap_lookback:    int   = 300,
    vwap_oe_low:      float = 2.0,
    vwap_oe_high:     float = 2.5,
    allowed_vwap_zones: frozenset[str] = _ALLOWED_VWAP_ZONES,
    # Regime — Volume Profile (POC band)
    vp_interval:        str   = "1h",
    vp_window:          int   = 24,
    vp_tick_size:       float = 1.0,
    vp_value_area_pct:  float = 0.70,
    vp_touch_band_pct:  float = 0.001,
    allowed_vp_labels:  frozenset[str] = _ALLOWED_VP_LABELS,
    # Signal
    vp_window_primary:   int   = 50,
    vp_window_secondary: int   = 20,
    n_bins:              int   = 24,
    sig_atr_period:      int   = 14,
    min_vp_dist:         float = 0.30,
    min_poc_potential:   float = 0.20,
    max_poc_dist_atr:    float = 5.0,
    sl_offset:           float = 0.0,
    min_micro_cvd:       float = 0.0,
    # Enhancer（預留插槽，空時無額外開銷）
    enhancer_modules:    list | None = None,
    # Entry management
    atr_period:   int   = 14,
    atr_k:        float = 1.0,
    max_sl_pct:   float = 0.03,
    min_stop_pct: float = 0.0015,
    # RR + TP adjust
    rr_ratio:    float                  = 2.0,
    min_rr_adj:  float                  = 0.8,
    capital_cfg: Optional[CapitalConfig] = None,
    # Fee
    taker_fee_rate:  float = 0.00032,
    slippage_rate:   float = 0.00002,
    fee_cover_ratio: float = 1.5,
) -> TradingPipeline:
    gate = PositionGateStage(max_positions=max_positions)

    vwap_comp = VWAPDeviationRegimeComponent(
        window=vwap_window,
        lookback=vwap_lookback,
        overextended_low=vwap_oe_low,
        overextended_high=vwap_oe_high,
    )

    vp_comp = POCBandVolumeProfileRegimeComponent(
        interval=vp_interval,
        window=vp_window,
        tick_size=vp_tick_size,
        value_area_pct=vp_value_area_pct,
        touch_band_pct=vp_touch_band_pct,
    )

    regime_stage = RegimeStage(
        components=[vwap_comp, vp_comp],
        allowed={
            "vwap_dev":   allowed_vwap_zones,
            "vol_profile": allowed_vp_labels,
        },
    )

    signal = VPBelowPOCLongSignal(
        vp_window_primary   = vp_window_primary,
        vp_window_secondary = vp_window_secondary,
        n_bins              = n_bins,
        atr_period          = sig_atr_period,
        min_vp_dist         = min_vp_dist,
        min_poc_potential   = min_poc_potential,
        max_poc_dist_atr    = max_poc_dist_atr,
        sl_offset           = sl_offset,
        min_micro_cvd       = min_micro_cvd,
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
    )

    tp_stage = VPBelowPOCTPAdjustStage(
        vp_comp    = vp_comp,
        min_rr_adj = min_rr_adj,
    )

    fee_stage = FeeCoverRatioStage(
        taker_fee_rate  = taker_fee_rate,
        slippage_rate   = slippage_rate,
        fee_cover_ratio = fee_cover_ratio,
    )

    return TradingPipeline([
        gate, regime_stage, alpha_stage, enhancer_stage,
        entry_stage, rr_stage, tp_stage, fee_stage,
    ])


def build_vp_below_poc_reversion_pipeline_def(
    name:              str            = "VP Below POC Reversion",
    allocation_weight: float          = 1.0,
    tags:              Sequence[str]  = ("mean_reversion", "vp_below_poc", "long_only"),
    **pipeline_kwargs,
) -> PipelineDef:
    """
    便利包裝：直接回傳 PipelineDef（可傳入 MultiPipelineRunner）。

    **pipeline_kwargs 全部轉給 build_vp_below_poc_reversion_pipeline()。
    """
    pipeline = build_vp_below_poc_reversion_pipeline(**pipeline_kwargs)
    return PipelineDef(
        name              = name,
        pipeline          = pipeline,
        allocation_weight = allocation_weight,
        tags              = list(tags),
    )


# ── UI 可用的具名包裝類別 ──────────────────────────────────────────────────────

class VPBelowPOCReversionPipelineStrategy(MultiPipelineStrategy):
    """
    VP Below POC Reversion Pipeline 的 UI 可用包裝。

    無參數實例化（UI 的 STRATEGY_REGISTRY 以 cls() 建立策略），
    使用 build_vp_below_poc_reversion_pipeline() 的預設參數。
    """

    name = "VP Below POC Reversion Pipeline"

    def __init__(self) -> None:
        defn   = build_vp_below_poc_reversion_pipeline_def()
        runner = MultiPipelineRunner(defs=[defn])
        super().__init__(
            runner         = runner,
            exit_mod       = ExitModule(ExitConfig(tp_rr_ratio=2.0)),
            initial_equity = 10_000.0,
        )
