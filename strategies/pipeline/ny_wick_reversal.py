"""
strategies/pipeline/ny_wick_reversal.py — NY Session 反轉棒均值回歸策略

策略邏輯：
  紐約盤（ny session）中，NEUTRAL 市場波動率、VWAP overextended_low 或 extreme_low
  環境下，價格位於 VAL 附近（price_in_val_band）或 POC 附近（price_in_poc_band）時，
  以反轉棒型態（三選一 Primary）捕捉均值回歸多單。
  入場觸發要求：信號棒成立後的下一根 K 棒 tick 資料需有 tick 高於信號棒高點。

  三個 Primary 訊號（任一成立即觸發，OR 模式）：
    1. LowerWickRatio (LWR)：
         lower_wick / range ≥ min_wick_ratio（0.50）
    2. LowerWickToBodyRatio (LWTB)：對齊 research/factors.py LowerWickToBodyRatioFactor
         lower_wick / body ≥ min_lwtb_ratio（1.0，下影線不小於實體）
    3. ReversalBarUp (RBU)：對齊 ReversalBarUpSignal
         range > SMA(20) AND wick_ratio ≥ 0.50 AND close_pos ≥ 0.60

  price_in_poc_band label 定義（對齊 research/regime_filter.py:746 / component.py:840）：
    |close − poc| ≤ close × touch_band_pct
    由 POCBandVolumeProfileRegimeComponent 在 below_POC / above_POC / in_value_area
    任一父類 label 的基礎上進行覆寫。

Pipeline 流程：
  Gate     PositionGateStage              — 同時間最多 1 筆
  Stage 1  RegimeStage                   — 四維度聯合初步過濾（效率用）
             MarketVolatility            → NEUTRAL
             VWAPDeviation               → overextended_low | extreme_low
             VolumeProfile (POCBand)     → price_in_val_band | price_in_poc_band
             Session                     → ny
           NYWickRevRegimeComboStage     — 跨維度精確 4-tuple 驗證（最終守門）
  Stage 2  AlphaStage[NYWickRevLong]    — LWR OR LWTB OR RBU + 執行棒 tick 觸發確認
  Stage 3  EntryManagementStage         — ATR(14) 停損 + max_sl_pct cap
  Stage 4  RRStage(2.0R)               — qty 計算（12-24 bars horizon）
  Stage 5  FeeCoverRatioStage           — 0.032% taker + 0.2bps 滑點, cover ratio 1.5

Regime 組合清單（NY_WICK_REV_ALLOWED_REGIMES）：
  (session, market_vol_regime, vwap_dev,          vol_profile         )
  ny  ×  NEUTRAL  ×  overextended_low  ×  price_in_val_band
  ny  ×  NEUTRAL  ×  overextended_low  ×  price_in_poc_band
  ny  ×  NEUTRAL  ×  extreme_low       ×  price_in_val_band
  ny  ×  NEUTRAL  ×  extreme_low       ×  price_in_poc_band

  RegimeStage 先用各維度 union 做快速預篩，
  NYWickRevRegimeComboStage 再做精確 4-tuple 匹配。
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
    MarketVolatilityRegimeComponent,
    SessionComponent,
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


# ── Regime 組合白名單 ─────────────────────────────────────────────────────────
# 欄位順序：(session, market_vol_regime, vwap_dev, vol_profile)

NY_WICK_REV_ALLOWED_REGIMES: list[tuple[str, str, str, str]] = [
    ("ny", "NEUTRAL", "overextended_low", "price_in_val_band"),
    ("ny", "NEUTRAL", "overextended_low", "price_in_poc_band"),
    ("ny", "NEUTRAL", "extreme_low",      "price_in_val_band"),
    ("ny", "NEUTRAL", "extreme_low",      "price_in_poc_band"),
]

# 各維度 union → 供 RegimeStage 快速預篩
_ALLOWED_SESSIONS    = frozenset(t[0] for t in NY_WICK_REV_ALLOWED_REGIMES)
_ALLOWED_MARKET_VOLS = frozenset(t[1] for t in NY_WICK_REV_ALLOWED_REGIMES)
_ALLOWED_VWAP_ZONES  = frozenset(t[2] for t in NY_WICK_REV_ALLOWED_REGIMES)
_ALLOWED_VP_LABELS   = frozenset(t[3] for t in NY_WICK_REV_ALLOWED_REGIMES)
_ALLOWED_COMBOS      = frozenset(NY_WICK_REV_ALLOWED_REGIMES)


# ── POCBandVolumeProfileRegimeComponent ──────────────────────────────────────

class POCBandVolumeProfileRegimeComponent(VolumeProfileRegimeComponent):
    """
    VolumeProfileRegimeComponent 擴充版，新增 price_in_poc_band label。

    對齊 research/regime_filter.py:746 / strategies/pipeline/component.py:840：
      price_in_poc_band = |close − poc| ≤ close × touch_band_pct

    父類回傳 below_POC、above_POC 或 in_value_area 時，若符合 POC band 條件，
    改為 price_in_poc_band。price_in_val_band / below_VAL / above_VAH 不受影響。
    """

    dimension = "vol_profile"

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        result = super().compute(klines, idx, tick_map)
        if result.get("label") in ("below_POC", "above_POC", "in_value_area"):
            poc   = result.get("poc_price")
            close = klines[idx].close
            if poc is not None:
                band = close * self._comp.touch_band_pct
                if abs(close - poc) <= band:
                    return {**result, "label": "price_in_poc_band"}
        return result


# ── NYWickRevRegimeComboStage ─────────────────────────────────────────────────

class NYWickRevRegimeComboStage(PipelineStage):
    """
    跨維度 Regime 組合過濾器（NY Wick Reversal 策略專用）。

    RegimeStage 已對每個維度獨立做初步過濾（取各維度 union，效率用）。
    本 Stage 做最終精確驗證：
      (session × market_vol_regime × vwap_dev × vol_profile)
    必須完整符合 allowed_combos 清單中的其中一列，否則阻斷 pipeline。
    """

    name = "NYWickRevRegimeComboStage"

    def __init__(
        self,
        allowed_combos: frozenset[tuple[str, str, str, str]] = _ALLOWED_COMBOS,
    ) -> None:
        self._allowed = allowed_combos

    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        dims       = ctx.regime_meta.get("regime_dimensions", {})
        session    = dims.get("session")
        market_vol = dims.get("market_vol_regime")
        vwap_zone  = dims.get("vwap_dev")
        vol_prof   = dims.get("vol_profile")

        if None in (session, market_vol, vwap_zone, vol_prof):
            return None

        return ctx if (session, market_vol, vwap_zone, vol_prof) in self._allowed else None


# ── NYWickReversalLongSignal ──────────────────────────────────────────────────

class NYWickReversalLongSignal(SignalModule):
    """
    NY Wick Reversal 多單訊號（複合型，三選一 Primary + tick 觸發）。

    detect_k0 以 OR 模式依序檢查三個 Primary 條件（任一成立即觸發）：

      1. LowerWickRatio (LWR)
         lower_wick / range ≥ min_wick_ratio

      2. LowerWickToBodyRatio (LWTB)，對齊 LowerWickToBodyRatioFactor：
         lower_wick = min(open, close) − low
         body       = |close − open|
         lower_wick / body ≥ min_lwtb_ratio（body > 0 時才計算）

      3. ReversalBarUp (RBU)，對齊 ReversalBarUpSignal：
         range > mean(range, sma_period)
         AND wick_ratio ≥ min_reversal_wick
         AND close_pos  ≥ min_close_pos

    entry_conditions 加入 tick 觸發驗證：
      執行棒（信號棒次棒）需有至少一筆 tick 高於信號棒高點，方可進場。
      tick_map 為 None 時退化至 kline 模式（直接放行，相容無 tick 回測）。

    進場觸發：signal_bar.HIGH。
    停損初步值：signal_bar.LOW − sl_offset，由 EntryManagementStage 以 ATR 覆蓋。
    """

    name = "NYWickReversalLong"

    def __init__(
        self,
        # LWR
        min_wick_ratio:    float = 0.50,
        # LWTB
        min_lwtb_ratio:    float = 1.0,
        # RBU
        sma_period:        int   = 20,
        min_reversal_wick: float = 0.50,
        min_close_pos:     float = 0.60,
        # 共用
        sl_offset:         float = 0.0,
        min_micro_cvd:     float = 0.0,
    ) -> None:
        self.min_wick_ratio    = min_wick_ratio
        self.min_lwtb_ratio    = min_lwtb_ratio
        self.sma_period        = sma_period
        self.min_reversal_wick = min_reversal_wick
        self.min_close_pos     = min_close_pos
        self.sl_offset         = sl_offset
        self.min_micro_cvd     = min_micro_cvd

    def can_trade(self, klines: list[Kline], idx: int) -> bool:
        # RBU 需要 sma_period 根歷史計算平均 range
        return idx >= self.sma_period + 1

    def detect_k0(self, klines: list[Kline], idx: int) -> Optional[dict]:
        k0  = klines[idx - 1]
        rng = k0.high - k0.low
        if rng <= 0:
            return None

        body_lo    = min(k0.open, k0.close)
        lower_wick = body_lo - k0.low
        body       = abs(k0.close - k0.open)
        wick_ratio = lower_wick / rng
        close_pos  = (k0.close - k0.low) / rng
        lwtb_ratio = lower_wick / body if body > 0 else 0.0

        signal_type: Optional[str] = None

        # Primary 1: LowerWickRatio
        if wick_ratio >= self.min_wick_ratio:
            signal_type = "LWR"

        # Primary 2: LowerWickToBodyRatio（對齊 LowerWickToBodyRatioFactor）
        if signal_type is None and lwtb_ratio >= self.min_lwtb_ratio:
            signal_type = "LWTB"

        # Primary 3: ReversalBarUp（對齊 ReversalBarUpSignal）
        if signal_type is None:
            hist = klines[max(0, idx - 1 - self.sma_period) : idx - 1]
            if len(hist) >= self.sma_period:
                avg_rng = sum(k.high - k.low for k in hist) / self.sma_period
                if (rng > avg_rng
                        and wick_ratio >= self.min_reversal_wick
                        and close_pos  >= self.min_close_pos):
                    signal_type = "RBU"

        if signal_type is None:
            return None

        return {
            "direction":   "long",
            "k0_idx":      idx - 1,
            "k0_low":      k0.low,
            "signal_type": signal_type,
            "wick_ratio":  wick_ratio,
            "lwtb_ratio":  lwtb_ratio,
            "close_pos":   close_pos,
        }

    def entry_conditions(
        self,
        klines:   list[Kline],
        k0_idx:   int,
        k0_meta:  dict,
        tick_map: Optional[TickBarMap] = None,
    ) -> Optional[StrategySignal]:
        signal_bar = klines[k0_meta["k0_idx"]]
        trigger    = signal_bar.high

        # Tick 觸發驗證：執行棒需有 tick 高於信號棒高點
        if tick_map is not None:
            exec_bar = klines[k0_idx]
            ticks    = tick_map.get(exec_bar.open_time)
            if ticks is not None and len(ticks) > 0:
                if not np.any(ticks[:, 1] > trigger):
                    return None

        return _mr_long_entry(
            klines, k0_idx, k0_meta["k0_low"], self.sl_offset,
            label         = "MR_NY_WICK_REV",
            meta          = {
                "k0_idx":      k0_meta["k0_idx"],
                "signal_type": k0_meta["signal_type"],
                "wick_ratio":  k0_meta["wick_ratio"],
                "lwtb_ratio":  k0_meta["lwtb_ratio"],
                "close_pos":   k0_meta["close_pos"],
            },
            tick_map      = tick_map,
            trigger_price = trigger,
            min_micro_cvd = self.min_micro_cvd,
        )


# ── Factory ───────────────────────────────────────────────────────────────────

def build_ny_wick_reversal_pipeline(
    *,
    # ── Gate ──────────────────────────────────────────────────────────────────
    max_positions: int = 1,
    # ── Stage 1: Regime（allowed_combos 為單一真相來源）─────────────────────
    allowed_combos: frozenset[tuple[str, str, str, str]] = _ALLOWED_COMBOS,
    # VWAPDeviationRegimeComponent 參數
    vwap_window:   int   = 120,
    vwap_lookback: int   = 300,
    vwap_oe_low:   float = 2.0,
    vwap_oe_high:  float = 2.5,
    # POCBandVolumeProfileRegimeComponent 參數
    vp_interval:       str   = "1h",
    vp_window:         int   = 24,
    vp_tick_size:      float = 1.0,
    vp_value_area_pct: float = 0.70,
    vp_touch_band_pct: float = 0.001,
    # MarketVolatilityRegimeComponent 參數
    mv_rv_period:  int = 60,
    mv_atr_short:  int = 10,
    mv_atr_long:   int = 60,
    mv_er_period:  int = 30,
    mv_adx_period: int = 14,
    mv_lookback:   int = 100,
    # ── Stage 2: Alpha（NYWickReversalLong）───────────────────────────────────
    min_wick_ratio:    float = 0.50,
    min_lwtb_ratio:    float = 1.0,
    sma_period:        int   = 20,
    min_reversal_wick: float = 0.50,
    min_close_pos:     float = 0.60,
    sl_offset:         float = 0.0,
    min_micro_cvd:     float = 0.0,
    # ── Stage 2.5: Enhancer（預留插槽，空時無額外開銷）──────────────────────
    enhancer_modules: list | None = None,
    # ── Stage 3: EntryManagement（ATR 停損）─────────────────────────────────
    atr_period:   int   = 14,
    atr_k:        float = 1.0,
    max_sl_pct:   float = 0.03,
    min_stop_pct: float = 0.0015,
    # ── Stage 4: RR（2.0R，對齊 12-24 bars horizon）──────────────────────────
    rr_ratio:    float                   = 2.0,
    capital_cfg: Optional[CapitalConfig] = None,
    # ── Stage 5: 費用覆蓋率─────────────────────────────────────────────────
    taker_fee_rate:  float = 0.00032,
    slippage_rate:   float = 0.00002,
    fee_cover_ratio: float = 1.5,
) -> TradingPipeline:
    """
    NY Wick Reversal Pipeline 工廠函式。

    allowed_combos 是單一真相來源：
      - RegimeStage 自動從中推導各維度 union 做快速預篩
      - NYWickRevRegimeComboStage 做最終精確 4-tuple 匹配
    """
    _sessions    = {t[0] for t in allowed_combos}
    _market_vols = {t[1] for t in allowed_combos}
    _vwap_zones  = {t[2] for t in allowed_combos}
    _vp_labels   = {t[3] for t in allowed_combos}

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
    vp_comp = POCBandVolumeProfileRegimeComponent(
        interval       = vp_interval,
        window         = vp_window,
        tick_size      = vp_tick_size,
        value_area_pct = vp_value_area_pct,
        touch_band_pct = vp_touch_band_pct,
    )
    session_comp = SessionComponent()

    signal = NYWickReversalLongSignal(
        min_wick_ratio    = min_wick_ratio,
        min_lwtb_ratio    = min_lwtb_ratio,
        sma_period        = sma_period,
        min_reversal_wick = min_reversal_wick,
        min_close_pos     = min_close_pos,
        sl_offset         = sl_offset,
        min_micro_cvd     = min_micro_cvd,
    )

    return TradingPipeline([
        PositionGateStage(max_positions=max_positions),
        RegimeStage(
            components = [mv_comp, vwap_comp, vp_comp, session_comp],
            allowed    = {
                "market_vol_regime": list(_market_vols),
                "vwap_dev":          list(_vwap_zones),
                "vol_profile":       list(_vp_labels),
                "session":           list(_sessions),
            },
        ),
        NYWickRevRegimeComboStage(allowed_combos=frozenset(allowed_combos)),
        AlphaStage(
            modules = [signal],
            mode    = "OR",
        ),
        EnhancerStage(modules=enhancer_modules),
        EntryManagementStage(
            atr_period   = atr_period,
            atr_k        = atr_k,
            max_sl_pct   = max_sl_pct,
            min_stop_pct = min_stop_pct,
        ),
        RRStage(
            exit_cfg    = ExitConfig(tp_rr_ratio=rr_ratio),
            capital_cfg = capital_cfg or CapitalConfig(),
            min_rr      = rr_ratio,
        ),
        FeeCoverRatioStage(
            taker_fee_rate  = taker_fee_rate,
            slippage_rate   = slippage_rate,
            fee_cover_ratio = fee_cover_ratio,
        ),
    ])


def build_ny_wick_reversal_pipeline_def(
    name:              str           = "ny_wick_reversal",
    allocation_weight: float         = 1.0,
    tags:              Sequence[str] = ("mean_reversion", "wick_reversal", "ny", "long_only"),
    **pipeline_kwargs,
) -> PipelineDef:
    """
    便利包裝：直接回傳 PipelineDef（可傳入 MultiPipelineRunner）。

    **pipeline_kwargs 全部轉給 build_ny_wick_reversal_pipeline()。
    """
    pipeline = build_ny_wick_reversal_pipeline(**pipeline_kwargs)
    return PipelineDef(
        name              = name,
        pipeline          = pipeline,
        allocation_weight = allocation_weight,
        tags              = list(tags),
    )


# ── UI 可用的具名包裝類別 ─────────────────────────────────────────────────────

class NYWickReversalPipelineStrategy(MultiPipelineStrategy):
    """
    NY Wick Reversal Pipeline 的 UI 可用包裝。

    無參數實例化（UI 的 STRATEGY_REGISTRY 以 cls() 建立策略），
    使用 build_ny_wick_reversal_pipeline() 的預設參數與 NY_WICK_REV_ALLOWED_REGIMES。
    """

    name = "NY Wick Reversal Pipeline"

    def __init__(self) -> None:
        defn   = build_ny_wick_reversal_pipeline_def()
        runner = MultiPipelineRunner(defs=[defn])
        super().__init__(
            runner         = runner,
            exit_mod       = ExitModule(ExitConfig(tp_rr_ratio=2.0)),
            initial_equity = 10_000.0,
        )
