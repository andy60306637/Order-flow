"""
strategies/pipeline/asian_overextended_cvd_reversal.py — Asian Session 超乖離 CVD 反轉策略

策略邏輯：
  亞洲盤（asian session）中，NEUTRAL 市場波動率、VWAP overextended_low 環境下，
  價格位於 Value Area 下方或觸碰下沿時，以 CVD 背離信號捕捉超乖離均值回歸多單。
  入場觸發要求：以信號棒成立後的下一根 K 棒 tick 資料確認，需有 tick 高於信號棒高點。

  CVD 背離邏輯對齊 research/factors.py CvdDivergenceLongFactor：
    delta[i]         = 2×taker_buy_volume[i] − volume[i]
    rolling_delta[i] = mean(delta[i−n+1 : i+1])   n 根滾動均值，n=window
    prev_delta[i]    = rolling_delta[i−n]          n 棒前的同指標
    price_fell       = close[k0] < close[k0−n]
    cvd_rose         = rolling_delta[k0] > prev_delta[k0]

  outside_value_area label 定義（對齊 research/regime_filter.py）：
    close < VAL OR close > VAH；本策略 overextended_low 環境下等價為
    val − touch_band ≤ close < val（price_in_val_band 的 VAL 下側），
    即 VolumeProfileRegimeComponent 的 price_in_val_band 中 close < val 的部分。

Pipeline 流程：
  Gate     PositionGateStage                    — 同時間最多 1 筆
  Stage 1  RegimeStage                          — 四維度聯合初步過濾（效率用）
             MarketVolatility                   → NEUTRAL
             VWAPDeviation                      → overextended_low
             VolumeProfile (Extended)           → below_VAL | below_POC | outside_value_area
             Session                            → asian
           AsianCVDOERegimeComboStage           — 跨維度精確 4-tuple 驗證（最終守門）
  Stage 2  AlphaStage[AsianCVDDivergenceLong]  — CVD 背離 + 執行棒 tick 觸發確認
  Stage 3  EntryManagementStage                — ATR(14) 停損 + max_sl_pct cap
  Stage 4  RRStage(1.5R)                       — qty 計算（短 horizon 6 bars 對齊）
  Stage 5  FeeCoverRatioStage                  — 0.032% taker + 0.2bps 滑點, cover ratio 1.5

Regime 組合清單（ASIAN_CVD_OE_ALLOWED_REGIMES）：
  (session, market_vol_regime, vwap_dev,          vol_profile         )
  asian  ×  NEUTRAL  ×  overextended_low  ×  below_VAL
  asian  ×  NEUTRAL  ×  overextended_low  ×  below_POC
  asian  ×  NEUTRAL  ×  overextended_low  ×  outside_value_area

  RegimeStage 先用各維度 union 做快速預篩，
  AsianCVDOERegimeComboStage 再做精確 4-tuple 匹配。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

import numpy as np

from core.data_types import Kline
from strategies.base import StrategySignal, TickBarMap
from strategies.modules.capital_management import CapitalConfig
from strategies.modules.exit_management import ExitConfig, ExitModule
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
from strategies.pipeline.ny_cvd_divergence_mr import NYCVDDivergenceLongSignal


# ── Regime 組合白名單 ─────────────────────────────────────────────────────────
# 欄位順序：(session, market_vol_regime, vwap_dev, vol_profile)

ASIAN_CVD_OE_ALLOWED_REGIMES: list[tuple[str, str, str, str]] = [
    ("asian", "NEUTRAL", "overextended_low", "below_VAL"),
    ("asian", "NEUTRAL", "overextended_low", "below_POC"),
    ("asian", "NEUTRAL", "overextended_low", "outside_value_area"),
]

# 各維度 union → 供 RegimeStage 快速預篩
_ALLOWED_SESSIONS    = frozenset(t[0] for t in ASIAN_CVD_OE_ALLOWED_REGIMES)
_ALLOWED_MARKET_VOLS = frozenset(t[1] for t in ASIAN_CVD_OE_ALLOWED_REGIMES)
_ALLOWED_VWAP_ZONES  = frozenset(t[2] for t in ASIAN_CVD_OE_ALLOWED_REGIMES)
_ALLOWED_VP_LABELS   = frozenset(t[3] for t in ASIAN_CVD_OE_ALLOWED_REGIMES)
_ALLOWED_COMBOS      = frozenset(ASIAN_CVD_OE_ALLOWED_REGIMES)


# ── ExtendedVolumeProfileRegimeComponent ─────────────────────────────────────

class ExtendedVolumeProfileRegimeComponent(VolumeProfileRegimeComponent):
    """
    VolumeProfileRegimeComponent 擴充版，新增 outside_value_area label。

    對齊 research/regime_filter.py 定義：
      outside_value_area = close < VAL OR close > VAH

    本策略的 overextended_low VWAP 環境下，等價為：
      val − touch_band ≤ close < val
      （price_in_val_band 中收盤在 VAL 下側的子集）

    未受影響的其他 label（below_VAL / below_POC / in_value_area /
    above_POC / above_VAH / price_in_val_band）語意不變。
    """

    dimension = "vol_profile"

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        result = super().compute(klines, idx, tick_map)
        # price_in_val_band 分為 VAL 上下兩側；close < val 側歸類為 outside_value_area
        if result.get("label") == "price_in_val_band":
            val = result.get("val")
            if val is not None and klines[idx].close < val:
                return {**result, "label": "outside_value_area"}
        return result


# ── AsianCVDOERegimeComboStage ────────────────────────────────────────────────

class AsianCVDOERegimeComboStage(PipelineStage):
    """
    跨維度 Regime 組合過濾器（Asian CVD OE 策略專用）。

    RegimeStage 已對每個維度獨立做初步過濾（取各維度 union，效率用）。
    本 Stage 做最終精確驗證：
      (session × market_vol_regime × vwap_dev × vol_profile)
    必須完整符合 allowed_combos 清單中的其中一列，否則阻斷 pipeline。
    """

    name = "AsianCVDOERegimeComboStage"

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


# ── AsianCVDDivergenceLongSignal ──────────────────────────────────────────────

class AsianCVDDivergenceLongSignal(NYCVDDivergenceLongSignal):
    """
    Asian Session CVD 背離多單訊號。

    detect_k0 邏輯繼承自 NYCVDDivergenceLongSignal（對齊 CvdDivergenceLongFactor）。

    entry_conditions 擴充 tick 觸發驗證：
      執行棒（信號棒次棒）需有至少一筆 tick 高於信號棒高點，方可進場。
      tick_map 為 None 時退化至 kline 模式（直接放行，相容無 tick 回測）。

    進場觸發：signal_bar.HIGH。
    停損初步值：signal_bar.LOW − sl_offset，由 EntryManagementStage 以 ATR 覆蓋。
    """

    name = "AsianCVDDivergenceLong"

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
            label         = "MR_ASIAN_CVD_OE",
            meta          = {
                "k0_idx":         k0_meta["k0_idx"],
                "rolling_delta":  k0_meta["rolling_delta"],
                "prev_delta":     k0_meta["prev_delta"],
                "cvd_divergence": k0_meta["cvd_divergence"],
            },
            tick_map      = tick_map,
            trigger_price = trigger,
            min_micro_cvd = self.min_micro_cvd,
        )


# ── Factory ───────────────────────────────────────────────────────────────────

def build_asian_cvd_oe_pipeline(
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
    # ExtendedVolumeProfileRegimeComponent 參數
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
    # ── Stage 2: Alpha（AsianCVDDivergenceLong + tick 觸發）─────────────────
    cvd_window:    int   = 20,
    sl_offset:     float = 0.0,
    min_micro_cvd: float = 0.0,
    # ── Stage 2.5: Enhancer（預留插槽，空時無額外開銷）──────────────────────
    enhancer_modules: list | None = None,
    # ── Stage 3: EntryManagement（ATR 停損）─────────────────────────────────
    atr_period:   int   = 14,
    atr_k:        float = 1.0,
    max_sl_pct:   float = 0.03,
    min_stop_pct: float = 0.0015,
    # ── Stage 4: RR（1.5R，對齊 6 bars 短 horizon）──────────────────────────
    rr_ratio:    float                   = 1.5,
    capital_cfg: Optional[CapitalConfig] = None,
    # ── Stage 5: 費用覆蓋率─────────────────────────────────────────────────
    taker_fee_rate:  float = 0.00032,
    slippage_rate:   float = 0.00002,
    fee_cover_ratio: float = 1.5,
) -> TradingPipeline:
    """
    Asian Overextended CVD Reversal Pipeline 工廠函式。

    allowed_combos 是單一真相來源：
      - RegimeStage 自動從中推導各維度 union 做快速預篩
      - AsianCVDOERegimeComboStage 做最終精確 4-tuple 匹配

    rr_ratio 預設 1.5 對齊 6 bars 短 horizon；如需延長持倉可調高。
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
    vp_comp = ExtendedVolumeProfileRegimeComponent(
        interval       = vp_interval,
        window         = vp_window,
        tick_size      = vp_tick_size,
        value_area_pct = vp_value_area_pct,
        touch_band_pct = vp_touch_band_pct,
    )
    session_comp = SessionComponent()

    signal = AsianCVDDivergenceLongSignal(
        window        = cvd_window,
        sl_offset     = sl_offset,
        min_micro_cvd = min_micro_cvd,
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
        AsianCVDOERegimeComboStage(allowed_combos=frozenset(allowed_combos)),
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


def build_asian_cvd_oe_pipeline_def(
    name:              str           = "asian_cvd_oe_reversal",
    allocation_weight: float         = 1.0,
    tags:              Sequence[str] = ("mean_reversion", "cvd_divergence", "asian", "long_only", "overextended"),
    **pipeline_kwargs,
) -> PipelineDef:
    """
    便利包裝：直接回傳 PipelineDef（可傳入 MultiPipelineRunner）。

    **pipeline_kwargs 全部轉給 build_asian_cvd_oe_pipeline()。
    """
    pipeline = build_asian_cvd_oe_pipeline(**pipeline_kwargs)
    return PipelineDef(
        name              = name,
        pipeline          = pipeline,
        allocation_weight = allocation_weight,
        tags              = list(tags),
    )


# ── UI 可用的具名包裝類別 ─────────────────────────────────────────────────────

class AsianCVDOEPipelineStrategy(MultiPipelineStrategy):
    """
    Asian Overextended CVD Reversal Pipeline 的 UI 可用包裝。

    無參數實例化（UI 的 STRATEGY_REGISTRY 以 cls() 建立策略），
    使用 build_asian_cvd_oe_pipeline() 的預設參數與 ASIAN_CVD_OE_ALLOWED_REGIMES。
    """

    name = "Asian CVD OE Reversal Pipeline"

    def __init__(self) -> None:
        defn   = build_asian_cvd_oe_pipeline_def()
        runner = MultiPipelineRunner(defs=[defn])
        super().__init__(
            runner         = runner,
            exit_mod       = ExitModule(ExitConfig(tp_rr_ratio=1.5)),
            initial_equity = 10_000.0,
        )
