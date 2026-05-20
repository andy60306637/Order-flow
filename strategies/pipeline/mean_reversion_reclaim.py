"""
strategies/pipeline/mean_reversion_reclaim.py — VAL Reclaim 均值回歸分支策略

VAL Reclaim 策略邏輯：
  價格跌破 Value Area Low（VAL）後，買盤吸收賣壓並將價格重新拉回 VAL 之上。
  這種「回攻」型態代表空方動能耗盡，是均值回歸多單的強力進場訊號。

Pipeline 流程：
  Gate     PositionGateStage              — 同時間最多 1 筆
  Stage 1  RegimeStage                   — 四維度獨立初步過濾（效率用）
             MarketVolatility            → NEUTRAL | MEAN_REVERSION
             VWAPDeviation               → extended_low | overextended_low
             VolumeProfile               → price_in_val_band | below_POC
             Session                     → asian | ny | overlap
           ValReclaimRegimeComboStage    — 跨維度組合精確驗證（最終守門）
  Stage 2  AlphaStage[ValReclaimLong]    — VAL 回攻型態 + 執行棒 close_pos 必要條件 + 三選一確認
  Stage 3  EntryManagementStage          — ATR(14) 停損 + max_sl_pct cap
  Stage 4  RRStage(2.0R baseline)        — qty 計算
           ValReclaimTPAdjustStage       — TP = min(POC, VWAP, 2.0R)
  Stage 5  FeeCoverRatioStage            — 0.032% taker + 0.2bps 滑點, cover ratio 1.5

Regime 組合清單（VAL_RECLAIM_ALLOWED_REGIMES）：
  (session,  market_vol,      vwap_zone,        vol_profile     )
  — price_in_val_band：剛回攻 VAL，收盤緊貼 VAL 上方
  asian   ×  NEUTRAL        × overextended_low × price_in_val_band
  ny      ×  NEUTRAL        × extended_low     × price_in_val_band
  ny      ×  MEAN_REVERSION × extended_low     × price_in_val_band
  overlap ×  NEUTRAL        × extended_low     × price_in_val_band
  overlap ×  MEAN_REVERSION × extended_low     × price_in_val_band
  asian   ×  MEAN_REVERSION × extended_low     × price_in_val_band
  — below_POC：回攻後在 VAL↑ POC↓ 盤整區，給進場確認多一棒空間
  asian   ×  NEUTRAL        × overextended_low × below_POC
  ny      ×  NEUTRAL        × extended_low     × below_POC
  ny      ×  MEAN_REVERSION × extended_low     × below_POC
  overlap ×  NEUTRAL        × extended_low     × below_POC
  overlap ×  MEAN_REVERSION × extended_low     × below_POC
  asian   ×  MEAN_REVERSION × extended_low     × below_POC

  RegimeStage 先用各維度 union 做快速預篩，
  ValReclaimRegimeComboStage 再做精確 4-tuple 匹配。
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
    RegimeClassifier,
    SessionComponent,
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

# 從主策略檔案共用工具
from strategies.pipeline.mean_reversion import (
    EntryManagementStage,
    FeeCoverRatioStage,
    VWAPDeviationRegimeComponent,
    _mr_long_entry,
)


# ── Regime 組合白名單 ─────────────────────────────────────────────────────────
# 欄位順序：(session, market_vol_regime, vwap_dev, vol_profile)

VAL_RECLAIM_ALLOWED_REGIMES: list[tuple[str, str, str, str]] = [
    # price_in_val_band — 剛回攻 VAL，收盤緊貼 VAL 上方
    ("asian",   "NEUTRAL",        "overextended_low", "price_in_val_band"),
    ("ny",      "NEUTRAL",        "extended_low",     "price_in_val_band"),
    ("ny",      "MEAN_REVERSION", "extended_low",     "price_in_val_band"),
    ("overlap", "NEUTRAL",        "extended_low",     "price_in_val_band"),
    ("overlap", "MEAN_REVERSION", "extended_low",     "price_in_val_band"),
    ("asian",   "MEAN_REVERSION", "extended_low",     "price_in_val_band"),
    # below_POC — 回攻後在 VAL↑ POC↓ 盤整區（進場確認棒落在此區間）
    ("asian",   "NEUTRAL",        "overextended_low", "below_POC"),
    ("ny",      "NEUTRAL",        "extended_low",     "below_POC"),
    ("ny",      "MEAN_REVERSION", "extended_low",     "below_POC"),
    ("overlap", "NEUTRAL",        "extended_low",     "below_POC"),
    ("overlap", "MEAN_REVERSION", "extended_low",     "below_POC"),
    ("asian",   "MEAN_REVERSION", "extended_low",     "below_POC"),
]

# 各維度 union → 供 RegimeStage 快速預篩
_ALLOWED_SESSIONS    = frozenset(t[0] for t in VAL_RECLAIM_ALLOWED_REGIMES)
_ALLOWED_MARKET_VOLS = frozenset(t[1] for t in VAL_RECLAIM_ALLOWED_REGIMES)
_ALLOWED_VWAP_ZONES  = frozenset(t[2] for t in VAL_RECLAIM_ALLOWED_REGIMES)
_ALLOWED_VP_LABELS   = frozenset(t[3] for t in VAL_RECLAIM_ALLOWED_REGIMES)
_ALLOWED_COMBOS      = frozenset(VAL_RECLAIM_ALLOWED_REGIMES)


# ── ValReclaimRegimeComboStage ────────────────────────────────────────────────

class ValReclaimRegimeComboStage(PipelineStage):
    """
    跨維度 Regime 組合過濾器。

    RegimeStage 已對每個維度獨立做初步過濾（取各維度 union，效率用）。
    本 Stage 做最終精確驗證：
      (session × market_vol_regime × vwap_dev × vol_profile)
    必須完整符合 allowed_combos 清單中的其中一列，否則阻斷 pipeline。

    需放在 RegimeStage 之後、AlphaStage 之前。
    """

    name = "ValReclaimRegimeComboStage"

    def __init__(
        self,
        allowed_combos: frozenset[tuple[str, str, str, str]] = _ALLOWED_COMBOS,
    ) -> None:
        self._allowed = allowed_combos

    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        dims = ctx.regime_meta.get("regime_dimensions", {})
        session    = dims.get("session")
        market_vol = dims.get("market_vol_regime")
        vwap_zone  = dims.get("vwap_dev")
        vol_prof   = dims.get("vol_profile")

        if None in (session, market_vol, vwap_zone, vol_prof):
            return None

        return ctx if (session, market_vol, vwap_zone, vol_prof) in self._allowed else None


# ── VolumeProfileRegimeComponent ─────────────────────────────────────────────

class VolumeProfileRegimeComponent(RegimeClassifier):
    """
    Volume Profile Regime 分類器（dimension = "vol_profile"）。

    包裝 VolumeProfileComponent，根據收盤價相對 VAL/POC/VAH 位置輸出 label：
      below_VAL         close < val − touch_band（嚴格在 VAL 下方）
      price_in_val_band |close − val| ≤ touch_band（緊貼 VAL，即近 VAL 區帶）
      below_POC         val + touch_band < close < poc（VAL 上方但 POC 下方）
      in_value_area     poc ≤ close ≤ vah
      above_POC         close > poc 且 close ≤ vah（VA 內高於 POC）
      above_VAH         close > vah

    供 RegimeStage 以 allowed={"vol_profile": [...]} 過濾。
    RegimeStage 快取鍵 = VolumeProfileComponent.component_id，
    後續 Stage 可從 SharedContext 直接讀取完整 VP 結果。
    """

    dimension = "vol_profile"

    def __init__(
        self,
        interval:       str   = "1h",
        window:         int   = 24,
        tick_size:      float = 1.0,
        value_area_pct: float = 0.70,
        touch_band_pct: float = 0.001,
    ) -> None:
        self._comp = VolumeProfileComponent(
            interval       = interval,
            window         = window,
            tick_size      = tick_size,
            value_area_pct = value_area_pct,
            touch_band_pct = touch_band_pct,
        )
        self.component_id = self._comp.component_id

    def compute(
        self,
        klines:   list[Kline],
        idx:      int,
        tick_map: Optional[TickBarMap] = None,
    ) -> dict:
        result = self._comp.compute(klines, idx, tick_map)

        if result.get("source") == "insufficient_data":
            return {**result, "label": "insufficient_data"}

        close = klines[idx].close
        val   = result.get("val")
        poc   = result.get("poc_price")
        vah   = result.get("vah")
        band  = close * self._comp.touch_band_pct

        if val is None or poc is None or vah is None:
            return {**result, "label": "insufficient_data"}

        if close < val - band:
            label = "below_VAL"
        elif abs(close - val) <= band:
            label = "price_in_val_band"
        elif close < poc:
            label = "below_POC"
        elif close > vah:
            label = "above_VAH"
        elif close > poc:
            label = "above_POC"
        else:
            label = "in_value_area"

        return {**result, "label": label}


# ── ValReclaimLongSignal ──────────────────────────────────────────────────────

class ValReclaimLongSignal(SignalModule):
    """
    VAL Reclaim 多單訊號（均值回歸分支）。

    信號K棒（klines[idx−1]）必須是 VAL 回攻蠟燭：
      low < VAL  AND  close ≥ VAL
      → 空方一度掃破 VAL，但買盤吸收後收回，空方動能衰竭。

    執行K棒（klines[idx]）必要條件 + 三選一確認：
      必要：close_pos ≥ min_close_pos（執行棒收盤位於棒身上半部）
      三選一：
        a. negative_delta_absorption：delta_eff < 0
           delta_eff = (2×tbv−vol)/vol，對標 delta_eff_long 研究因子；
           賣方主導但收盤偏強（由 close_pos 統一驗證），代表賣壓被吸收。
        b. lower_wick_ratio ≥ min_entry_wick_ratio
           （下影線比例高，買盤於執行棒再次抵抗）
        c. body_ratio ≤ max_entry_body_ratio
           （小實體蠟燭，方向猶豫 / 盤整收斂）

    進場觸發：signal_bar.HIGH（需突破信號棒高點才進場）。
    停損初步值：signal_bar.LOW − sl_offset，由 EntryManagementStage 以 ATR 覆蓋。
    """

    name = "ValReclaimLong"

    def __init__(
        self,
        vp_interval:          str   = "1h",
        vp_window:            int   = 24,
        tick_size:            float = 1.0,
        value_area_pct:       float = 0.70,
        touch_band_pct:       float = 0.001,
        min_entry_wick_ratio: float = 0.30,
        max_entry_body_ratio: float = 0.60,
        min_close_pos:        float = 0.55,
        sl_offset:            float = 0.0,
        min_micro_cvd:        float = 0.0,
    ) -> None:
        self._vp_comp = VolumeProfileComponent(
            interval       = vp_interval,
            window         = vp_window,
            tick_size      = tick_size,
            value_area_pct = value_area_pct,
            touch_band_pct = touch_band_pct,
        )
        self.min_entry_wick_ratio = min_entry_wick_ratio
        self.max_entry_body_ratio = max_entry_body_ratio
        self.min_close_pos        = min_close_pos
        self.sl_offset            = sl_offset
        self.min_micro_cvd        = min_micro_cvd

    def can_trade(self, klines: list[Kline], idx: int) -> bool:
        return idx >= self._vp_comp.window + 1

    def detect_k0(self, klines: list[Kline], idx: int) -> Optional[dict]:
        k0 = klines[idx - 1]

        # 計算信號棒（idx-1）的 Volume Profile；直接 compute，不走 SharedContext
        vp = self._vp_comp.compute(klines, idx - 1)
        if vp.get("source") == "insufficient_data":
            return None

        val = vp.get("val")
        poc = vp.get("poc_price")
        if val is None or poc is None:
            return None

        # VAL 回攻：盤中跌破 VAL，但收盤回到 VAL 上方
        if k0.low >= val:
            return None
        if k0.close < val:
            return None

        # POC 必須在 VAL 上方（確保 TP 空間存在）
        if poc <= val:
            return None

        return {
            "direction":    "long",
            "k0_idx":       idx - 1,
            "k0_low":       k0.low,
            "val":          val,
            "poc":          poc,
            "reclaim_size": k0.close - val,
        }

    def entry_conditions(
        self,
        klines:   list[Kline],
        k0_idx:   int,
        k0_meta:  dict,
        tick_map: Optional[TickBarMap] = None,
    ) -> Optional[StrategySignal]:
        exec_bar = klines[k0_idx]
        rng      = exec_bar.high - exec_bar.low

        if rng > 0:
            body_lo          = min(exec_bar.open, exec_bar.close)
            lower_wick_ratio = (body_lo - exec_bar.low) / rng
            body_ratio       = abs(exec_bar.close - exec_bar.open) / rng
            close_pos        = (exec_bar.close - exec_bar.low) / rng
        else:
            lower_wick_ratio = 0.0
            body_ratio       = 1.0
            close_pos        = 0.5

        vol           = exec_bar.volume
        delta_eff     = (2.0 * exec_bar.taker_buy_volume - vol) / vol if vol > 0 else 0.0
        neg_delta_abs = delta_eff < 0  # sell-side dominated; aligned with delta_eff_long factor

        confirmed = close_pos >= self.min_close_pos and (
            neg_delta_abs
            or lower_wick_ratio >= self.min_entry_wick_ratio
            or body_ratio       <= self.max_entry_body_ratio
        )
        if not confirmed:
            return None

        signal_bar = klines[k0_meta["k0_idx"]]
        return _mr_long_entry(
            klines,
            k0_idx,
            k0_meta["k0_low"],
            self.sl_offset,
            label         = "MR_VAL_RECLAIM",
            meta          = {
                "val":              k0_meta["val"],
                "poc":              k0_meta["poc"],
                "reclaim_size":     k0_meta["reclaim_size"],
                "k0_idx":           k0_meta["k0_idx"],
                "lower_wick_ratio": lower_wick_ratio,
                "body_ratio":       body_ratio,
                "close_pos":        close_pos,
                "delta_eff":        delta_eff,
                "neg_delta_abs":    neg_delta_abs,
            },
            tick_map      = tick_map,
            trigger_price = signal_bar.high,
            min_micro_cvd = self.min_micro_cvd,
        )


# ── ValReclaimTPAdjustStage ───────────────────────────────────────────────────

class ValReclaimTPAdjustStage(PipelineStage):
    """
    VAL Reclaim TP 多目標調整器。

    前置：RRStage 已設定 ctx.tp_price（= entry + 2.0R）與 ctx.qty。
    本 Stage 從 SharedContext 讀取 POC 與 VWAP，
    取 min(poc, vwap, baseline_tp) 作為最終 TP：

      TP = min(valid candidates > entry_price)

    候選：poc（若 poc > entry）、vwap（若 vwap > entry）、baseline_tp（恆有效）。

    若 actual_rr < min_rr_adj 則阻斷（防止 TP 被壓縮至無意義）。
    需放在 RRStage 之後、FeeCoverRatioStage 之前。
    """

    name = "ValReclaimTPAdjustStage"

    def __init__(
        self,
        vp_regime_comp:   VolumeProfileRegimeComponent,
        vwap_regime_comp: VWAPDeviationRegimeComponent,
        min_rr_adj:       float = 0.8,
    ) -> None:
        self._vp        = vp_regime_comp
        self._vwap      = vwap_regime_comp
        self.min_rr_adj = min_rr_adj

    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        if None in (ctx.entry_price, ctx.stop_price, ctx.tp_price):
            return None

        entry = ctx.entry_price
        stop  = ctx.stop_price
        risk  = abs(entry - stop)
        if risk < 1e-10:
            return None

        candidates: list[float] = [ctx.tp_price]   # baseline 1.5R（恆有效）

        # POC 目標（RegimeStage 已快取）
        vp  = ctx.shared.get_or_compute(
            self._vp.component_id,
            lambda: self._vp.compute(ctx.klines, ctx.idx, ctx.tick_map),
        )
        poc = vp.get("poc_price")
        if poc is not None and poc > entry:
            candidates.append(poc)

        # VWAP 目標（RegimeStage 已快取）
        vwap_result = ctx.shared.get_or_compute(
            self._vwap.component_id,
            lambda: self._vwap.compute(ctx.klines, ctx.idx, ctx.tick_map),
        )
        vwap = vwap_result.get("vwap")
        if vwap is not None and vwap > entry:
            candidates.append(vwap)

        tp        = min(candidates)
        actual_rr = (tp - entry) / risk

        if actual_rr < self.min_rr_adj:
            return None

        ctx.tp_price    = tp
        ctx.expected_rr = actual_rr

        if ctx.alpha_meta is not None:
            ctx.alpha_meta["tp_detail"] = {
                "poc_tp":    poc,
                "vwap_tp":   vwap,
                "rr_tp":     candidates[0],
                "final_tp":  tp,
                "actual_rr": actual_rr,
            }
        return ctx


# ── Factory ───────────────────────────────────────────────────────────────────

def build_val_reclaim_pipeline(
    *,
    # ── Gate ──────────────────────────────────────────────────────────────────
    max_positions: int = 1,
    # ── Stage 1：Regime 組合過濾（單一真相來源）─────────────────────────────
    # 修改 allowed_combos 即可同時更新 RegimeStage 預篩 + ComboStage 精確篩
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
    # ── Stage 2：Alpha（ValReclaimLong 訊號）─────────────────────────────────
    min_entry_wick_ratio: float = 0.30,
    max_entry_body_ratio: float = 0.60,
    min_close_pos:        float = 0.55,
    sl_offset:            float = 0.0,
    min_micro_cvd:        float = 0.0,
    # ── Stage 2.5：Enhancer（預留插槽，空時無額外開銷）────────────────────────
    enhancer_modules:     list | None = None,
    # ── Stage 3：EntryManagement（ATR 停損）──────────────────────────────────
    atr_period:   int   = 14,
    atr_k:        float = 1.0,
    max_sl_pct:   float = 0.03,
    min_stop_pct: float = 0.0015,
    # ── Stage 4：RR baseline + TP 調整（POC / VWAP / 2.0R 取最近）──────────
    rr_ratio:      float                   = 2.0,
    min_rr_adj:    float                   = 0.8,
    # use_tp_adjust: bool                    = True,
    use_tp_adjust: bool                    = False,
    capital_cfg:   Optional[CapitalConfig] = None,
    # ── Stage 5：費用覆蓋率（0.032% taker + 0.2bps 滑點，cover ratio 1.5）──
    taker_fee_rate:  float = 0.00032,
    slippage_rate:   float = 0.00002,
    fee_cover_ratio: float = 1.5,
) -> TradingPipeline:
    """
    VAL Reclaim 均值回歸 Pipeline 工廠函式。

    allowed_combos 是單一真相來源：
      - RegimeStage 自動從中推導各維度 union 做快速預篩
      - ValReclaimRegimeComboStage 做最終精確 4-tuple 匹配

    範例：

        from strategies.pipeline.mean_reversion_reclaim import (
            build_val_reclaim_pipeline_def, VAL_RECLAIM_ALLOWED_REGIMES
        )
        from strategies.modules.capital_management import CapitalConfig

        defn = build_val_reclaim_pipeline_def(
            capital_cfg=CapitalConfig(max_risk_pct=1.0, leverage=20),
        )
    """
    # 從 allowed_combos 推導各維度 union（RegimeStage 預篩用）
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

    val_reclaim_sig = ValReclaimLongSignal(
        vp_interval          = vp_interval,
        vp_window            = vp_window,
        tick_size            = vp_tick_size,
        value_area_pct       = vp_value_area_pct,
        touch_band_pct       = vp_touch_band_pct,
        min_entry_wick_ratio = min_entry_wick_ratio,
        max_entry_body_ratio = max_entry_body_ratio,
        min_close_pos        = min_close_pos,
        sl_offset            = sl_offset,
        min_micro_cvd        = min_micro_cvd,
    )

    gate = PositionGateStage(max_positions=max_positions)

    return TradingPipeline([
        gate,
        RegimeStage(
            components = [mv_comp, vwap_comp, vp_comp, session_comp],
            allowed    = {
                "market_vol_regime": list(_market_vols),
                "vwap_dev":          list(_vwap_zones),
                "vol_profile":       list(_vp_labels),
                "session":           list(_sessions),
            },
        ),
        ValReclaimRegimeComboStage(allowed_combos=frozenset(allowed_combos)),
        AlphaStage(
            modules = [val_reclaim_sig],
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
        *(
            [ValReclaimTPAdjustStage(
                vp_regime_comp   = vp_comp,
                vwap_regime_comp = vwap_comp,
                min_rr_adj       = min_rr_adj,
            )]
            if use_tp_adjust else []
        ),
        FeeCoverRatioStage(
            taker_fee_rate  = taker_fee_rate,
            slippage_rate   = slippage_rate,
            fee_cover_ratio = fee_cover_ratio,
        ),
    ])


def build_val_reclaim_pipeline_def(
    name:              str           = "val_reclaim",
    allocation_weight: float         = 1.0,
    tags:              Sequence[str] = ("mean_reversion", "val_reclaim", "long_only"),
    **pipeline_kwargs,
) -> PipelineDef:
    """
    便利包裝：直接回傳 PipelineDef（可傳入 MultiPipelineRunner）。

    **pipeline_kwargs 全部轉給 build_val_reclaim_pipeline()。
    """
    pipeline = build_val_reclaim_pipeline(**pipeline_kwargs)
    return PipelineDef(
        name              = name,
        pipeline          = pipeline,
        allocation_weight = allocation_weight,
        tags              = list(tags),
    )


# ── UI 可用的具名包裝類別 ─────────────────────────────────────────────────────

class ValReclaimPipelineStrategy(MultiPipelineStrategy):
    """
    VAL Reclaim Pipeline 的 UI 可用包裝。

    無參數實例化（UI 的 STRATEGY_REGISTRY 以 cls() 建立策略），
    使用 build_val_reclaim_pipeline() 的預設參數與 VAL_RECLAIM_ALLOWED_REGIMES。
    """

    name = "VAL Reclaim Pipeline"

    def __init__(self) -> None:
        defn   = build_val_reclaim_pipeline_def()
        runner = MultiPipelineRunner(defs=[defn])
        super().__init__(
            runner         = runner,
            exit_mod       = ExitModule(ExitConfig(tp_rr_ratio=2.0)),
            initial_equity = 10_000.0,
        )
