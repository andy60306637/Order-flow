"""
strategies/pipeline/ny_cvd_divergence_mr.py — NY Session CVD 背離均值回歸策略

策略邏輯：
  紐約盤（ny session）中，NEUTRAL 市場波動率環境下，VWAP 乖離進入 extended_low 區帶，
  且價格位於 Value Area 內或近 VAL 區域時，以 CVD 背離信號捕捉短期均值回歸多單。

  CVD 背離邏輯對齊 research/factors.py CvdDivergenceLongFactor：
    delta[i]         = 2×taker_buy_volume[i] − volume[i]
    rolling_delta[i] = mean(delta[i−n+1 : i+1])   n 根滾動均值，n=window
    prev_delta[i]    = rolling_delta[i−n]          n 棒前的同指標
    price_fell       = close[k0] < close[k0−n]
    cvd_rose         = rolling_delta[k0] > prev_delta[k0]

Pipeline 流程：
  Gate     PositionGateStage              — 同時間最多 1 筆
  Stage 1  RegimeStage                   — 四維度聯合初步過濾（效率用）
             MarketVolatility            → NEUTRAL
             VWAPDeviation               → extended_low
             VolumeProfile               → below_POC | below_VAL | price_in_val_band | in_value_area
             Session                     → ny
           NYCVDRegimeComboStage         — 跨維度精確 4-tuple 驗證（最終守門）
  Stage 2  AlphaStage[NYCVDDivLong]     — CVD 背離信號（price_fell + cvd_rose）
  Stage 3  EntryManagementStage         — ATR(14) 停損 + max_sl_pct cap
  Stage 4  RRStage(2.0R)               — qty 計算
  Stage 5  FeeCoverRatioStage           — 0.032% taker + 0.2bps 滑點, cover ratio 1.5

Regime 組合清單（NY_CVD_ALLOWED_REGIMES）：
  (session, market_vol_regime, vwap_dev,     vol_profile      )
  ny  ×  NEUTRAL  ×  extended_low  ×  below_POC
  ny  ×  NEUTRAL  ×  extended_low  ×  below_VAL
  ny  ×  NEUTRAL  ×  extended_low  ×  price_in_val_band
  ny  ×  NEUTRAL  ×  extended_low  ×  in_value_area

  RegimeStage 先用各維度 union 做快速預篩，
  NYCVDRegimeComboStage 再做精確 4-tuple 匹配。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

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

NY_CVD_ALLOWED_REGIMES: list[tuple[str, str, str, str]] = [
    ("ny", "NEUTRAL", "extended_low", "below_POC"),
    ("ny", "NEUTRAL", "extended_low", "below_VAL"),
    ("ny", "NEUTRAL", "extended_low", "price_in_val_band"),
    ("ny", "NEUTRAL", "extended_low", "in_value_area"),
]

# 各維度 union → 供 RegimeStage 快速預篩
_ALLOWED_SESSIONS    = frozenset(t[0] for t in NY_CVD_ALLOWED_REGIMES)
_ALLOWED_MARKET_VOLS = frozenset(t[1] for t in NY_CVD_ALLOWED_REGIMES)
_ALLOWED_VWAP_ZONES  = frozenset(t[2] for t in NY_CVD_ALLOWED_REGIMES)
_ALLOWED_VP_LABELS   = frozenset(t[3] for t in NY_CVD_ALLOWED_REGIMES)
_ALLOWED_COMBOS      = frozenset(NY_CVD_ALLOWED_REGIMES)


# ── NYCVDRegimeComboStage ─────────────────────────────────────────────────────

class NYCVDRegimeComboStage(PipelineStage):
    """
    跨維度 Regime 組合過濾器（NY CVD 策略專用）。

    RegimeStage 已對每個維度獨立做初步過濾（取各維度 union，效率用）。
    本 Stage 做最終精確驗證：
      (session × market_vol_regime × vwap_dev × vol_profile)
    必須完整符合 allowed_combos 清單中的其中一列，否則阻斷 pipeline。

    需放在 RegimeStage 之後、AlphaStage 之前。
    """

    name = "NYCVDRegimeComboStage"

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


# ── NYCVDDivergenceLongSignal ─────────────────────────────────────────────────

class NYCVDDivergenceLongSignal(SignalModule):
    """
    NY CVD 背離多單訊號，邏輯對齊 CvdDivergenceLongFactor（research/factors.py）。

    信號棒（klines[idx-1] = k0）觸發條件：
      delta[i]       = 2×taker_buy_volume[i] − volume[i]
      rolling_delta  = mean(delta[k0−n+1 : k0+1])        n 根滾動均值
      prev_delta     = mean(delta[k0−2n+1 : k0−n+1])     n 棒前的同指標
      price_fell     = k0.close < klines[k0−n].close
      cvd_rose       = rolling_delta > prev_delta

    進場觸發：k0.HIGH（突破信號棒高點）。
    停損初步值：k0.LOW − sl_offset，由 EntryManagementStage 以 ATR 覆蓋。
    """

    name = "NYCVDDivergenceLong"

    def __init__(
        self,
        window:        int   = 20,
        sl_offset:     float = 0.0,
        min_micro_cvd: float = 0.0,
    ) -> None:
        self.window        = window
        self.sl_offset     = sl_offset
        self.min_micro_cvd = min_micro_cvd

    def can_trade(self, klines: list[Kline], idx: int) -> bool:
        # 需要 2*window 棒歷史：rolling_delta(k0) + prev_delta(k0)
        return idx >= 2 * self.window

    def detect_k0(self, klines: list[Kline], idx: int) -> Optional[dict]:
        n  = self.window
        k0 = klines[idx - 1]

        # rolling_delta at k0: mean(delta[idx-n : idx])，即 n 根棒含 k0
        win_k0 = klines[idx - n : idx]
        if len(win_k0) < n:
            return None
        rolling_delta_k0 = sum(
            k.taker_buy_volume - (k.volume - k.taker_buy_volume) for k in win_k0
        ) / n

        # prev_delta at k0: mean(delta[idx-2n : idx-n])，即 k0 再往前 n 根
        win_prev = klines[idx - 2 * n : idx - n]
        if len(win_prev) < n:
            return None
        rolling_delta_prev = sum(
            k.taker_buy_volume - (k.volume - k.taker_buy_volume) for k in win_prev
        ) / n

        price_n_ago = klines[idx - 1 - n].close

        price_fell = k0.close < price_n_ago
        cvd_rose   = rolling_delta_k0 > rolling_delta_prev

        if not (price_fell and cvd_rose):
            return None

        return {
            "direction":      "long",
            "k0_idx":         idx - 1,
            "k0_low":         k0.low,
            "rolling_delta":  rolling_delta_k0,
            "prev_delta":     rolling_delta_prev,
            "cvd_divergence": rolling_delta_k0 - rolling_delta_prev,
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
            label         = "MR_NY_CVD_DIV",
            meta          = {
                "k0_idx":         k0_meta["k0_idx"],
                "rolling_delta":  k0_meta["rolling_delta"],
                "prev_delta":     k0_meta["prev_delta"],
                "cvd_divergence": k0_meta["cvd_divergence"],
            },
            tick_map      = tick_map,
            trigger_price = signal_bar.high,
            min_micro_cvd = self.min_micro_cvd,
        )


# ── Factory ───────────────────────────────────────────────────────────────────

def build_ny_cvd_divergence_mr_pipeline(
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
    # VolumeProfileRegimeComponent 參數
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
    # ── Stage 2: Alpha（NYCVDDivergenceLong）────────────────────────────────
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
    # ── Stage 4: RR（固定 2.0R）─────────────────────────────────────────────
    rr_ratio:    float                   = 2.0,
    capital_cfg: Optional[CapitalConfig] = None,
    # ── Stage 5: 費用覆蓋率─────────────────────────────────────────────────
    taker_fee_rate:  float = 0.00032,
    slippage_rate:   float = 0.00002,
    fee_cover_ratio: float = 1.5,
) -> TradingPipeline:
    """
    NY CVD Divergence MR Pipeline 工廠函式。

    allowed_combos 是單一真相來源：
      - RegimeStage 自動從中推導各維度 union 做快速預篩
      - NYCVDRegimeComboStage 做最終精確 4-tuple 匹配
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
    vp_comp = VolumeProfileRegimeComponent(
        interval       = vp_interval,
        window         = vp_window,
        tick_size      = vp_tick_size,
        value_area_pct = vp_value_area_pct,
        touch_band_pct = vp_touch_band_pct,
    )
    session_comp = SessionComponent()

    signal = NYCVDDivergenceLongSignal(
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
        NYCVDRegimeComboStage(allowed_combos=frozenset(allowed_combos)),
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


def build_ny_cvd_divergence_mr_pipeline_def(
    name:              str           = "ny_cvd_divergence_mr",
    allocation_weight: float         = 1.0,
    tags:              Sequence[str] = ("mean_reversion", "cvd_divergence", "ny", "long_only"),
    **pipeline_kwargs,
) -> PipelineDef:
    """
    便利包裝：直接回傳 PipelineDef（可傳入 MultiPipelineRunner）。

    **pipeline_kwargs 全部轉給 build_ny_cvd_divergence_mr_pipeline()。
    """
    pipeline = build_ny_cvd_divergence_mr_pipeline(**pipeline_kwargs)
    return PipelineDef(
        name              = name,
        pipeline          = pipeline,
        allocation_weight = allocation_weight,
        tags              = list(tags),
    )


# ── UI 可用的具名包裝類別 ─────────────────────────────────────────────────────

class NYCVDDivergenceMRPipelineStrategy(MultiPipelineStrategy):
    """
    NY CVD Divergence MR Pipeline 的 UI 可用包裝。

    無參數實例化（UI 的 STRATEGY_REGISTRY 以 cls() 建立策略），
    使用 build_ny_cvd_divergence_mr_pipeline() 的預設參數與 NY_CVD_ALLOWED_REGIMES。
    """

    name = "NY CVD Divergence MR Pipeline"

    def __init__(self) -> None:
        defn   = build_ny_cvd_divergence_mr_pipeline_def()
        runner = MultiPipelineRunner(defs=[defn])
        super().__init__(
            runner         = runner,
            exit_mod       = ExitModule(ExitConfig(tp_rr_ratio=2.0)),
            initial_equity = 10_000.0,
        )
