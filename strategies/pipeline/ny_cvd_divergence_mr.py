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
    EnhancerModule,
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


# ── NYCVDDivergenceTickEntrySignal ────────────────────────────────────────────

class NYCVDDivergenceTickEntrySignal(SignalModule):
    """
    NY CVD 背離多單訊號 v2：加入 Tick-level Entry State Machine。

    支援：
    1. entry_boundary_mode: body_high / high
    2. guardian_mode: body_low / low / None
    3. entry_delta_eff_threshold: (2*buy - vol) / vol
    4. zoom_bars: 可選 1 或 2
    """

    name = "NYCVDDivergenceTickEntry"

    def __init__(
        self,
        window:                    int   = 20,
        sl_offset:                 float = 0.0,
        entry_boundary_mode:       str   = "high",       # "body_high" | "high"
        guardian_mode:             str   = "body_low",   # "body_low" | "low" | None
        entry_delta_eff_threshold: float = 0.4,
        zoom_bars:                 int   = 1,
        allow_bar_fallback:        bool  = False,
        max_fill_slippage:         float = 1e9,          # 最大允許滑點 (USDT)
        max_fill_slippage_R:       float = 0.0,          # 最大允許滑點 (R 倍數, 0=停用)
        min_k0_range_atr:          float = 0.0,          # 最小 K0 振幅 (ATR 倍數)
        max_wait_ticks:            int   = 999999,       # 最大等待 tick 數
    ) -> None:
        self.window                    = window
        self.sl_offset                 = sl_offset
        self.entry_boundary_mode       = entry_boundary_mode
        self.guardian_mode             = guardian_mode
        self.entry_delta_eff_threshold = entry_delta_eff_threshold
        self.zoom_bars                 = zoom_bars
        self.allow_bar_fallback        = allow_bar_fallback
        self.max_fill_slippage         = max_fill_slippage
        self.max_fill_slippage_R       = max_fill_slippage_R
        self.min_k0_range_atr          = min_k0_range_atr
        self.max_wait_ticks            = max_wait_ticks
        from strategies.pipeline.component import ATRComponent
        self._atr_comp                 = ATRComponent(period=14)

    def can_trade(self, klines: list[Kline], idx: int) -> bool:
        # 需要 2*window 棒歷史 + zoom_bars 緩衝 + ATR 緩衝(14)
        return idx >= max(2 * self.window + self.zoom_bars, 14 + self.zoom_bars)

    def detect_k0(self, klines: list[Kline], idx: int) -> Optional[dict]:
        """
        支援 zoom_bars 窗口內的 k0 偵測。
        idx 是當前準備進場嘗試的 Bar 索引。
        """
        n = self.window

        # 如果有 min_k0_range_atr，先計算 ATR
        atr = 0.0
        if self.min_k0_range_atr > 0:
            atr_res = self._atr_comp.compute(klines, idx - 1)
            atr = atr_res.get("atr", 0.0)

        for offset in range(1, self.zoom_bars + 1):
            k0_idx = idx - offset
            if k0_idx < 2 * n:
                continue

            k0 = klines[k0_idx]

            # 檢查 k0 振幅
            if self.min_k0_range_atr > 0 and atr > 0:
                if (k0.high - k0.low) < atr * self.min_k0_range_atr:
                    continue

            # rolling_delta at k0
            win_k0 = klines[k0_idx - n + 1 : k0_idx + 1]
            rolling_delta_k0 = sum(
                k.taker_buy_volume - (k.volume - k.taker_buy_volume) for k in win_k0
            ) / n

            # prev_delta at k0
            win_prev = klines[k0_idx - 2 * n + 1 : k0_idx - n + 1]
            rolling_delta_prev = sum(
                k.taker_buy_volume - (k.volume - k.taker_buy_volume) for k in win_prev
            ) / n

            price_n_ago = klines[k0_idx - n].close
            price_fell  = k0.close < price_n_ago
            cvd_rose    = rolling_delta_k0 > rolling_delta_prev

            if price_fell and cvd_rose:
                # 檢查中間棒是否破了 guardian (如果 zoom_bars > 1)
                guardian_val = None
                if self.guardian_mode == "body_low":
                    guardian_val = min(k0.open, k0.close)
                elif self.guardian_mode == "low":
                    guardian_val = k0.low

                valid = True
                if offset > 1 and guardian_val is not None:
                    for m_idx in range(k0_idx + 1, idx):
                        if klines[m_idx].low < guardian_val:
                            valid = False
                            break

                if valid:
                    return {
                        "direction":      "long",
                        "k0_idx":         k0_idx,
                        "k0_low":         k0.low,
                        "rolling_delta":  rolling_delta_k0,
                        "prev_delta":     rolling_delta_prev,
                        "cvd_divergence": rolling_delta_k0 - rolling_delta_prev,
                        "zoom_offset":    offset,
                        "atr14":          atr,
                    }
        return None

    def entry_conditions(
        self,
        klines:   list[Kline],
        idx:      int,
        k0_meta:  dict,
        tick_map: Optional[TickBarMap] = None,
        diag:     Optional[dict]       = None,
    ) -> Optional[StrategySignal]:
        k0_idx = k0_meta["k0_idx"]
        k0     = klines[k0_idx]
        entry_bar = klines[idx]

        # Entry Boundary
        if self.entry_boundary_mode == "body_high":
            boundary = max(k0.open, k0.close)
        else:
            boundary = k0.high

        # Guardian
        guardian = None
        if self.guardian_mode == "body_low":
            guardian = min(k0.open, k0.close)
        elif self.guardian_mode == "low":
            guardian = k0.low

        # 計算有效滑點門檻：R 模式優先，否則用固定 USDT
        if self.max_fill_slippage_R > 0 and guardian is not None and boundary > guardian:
            _eff_max_slippage = self.max_fill_slippage_R * (boundary - guardian)
        else:
            _eff_max_slippage = self.max_fill_slippage

        meta = {
            "k0_idx":           k0_idx,
            "boundary":         boundary,
            "guardian":         guardian,
            "boundary_touched": False,
            "guardian_killed":  False,
            "delta_not_enough": False,
            "wait_ticks":       0,
            "fill_slippage":    0.0,
            "zoom_offset":      k0_meta.get("zoom_offset", 1),
            "cvd_divergence":   k0_meta.get("cvd_divergence"),
            "cvd_ratio":        k0_meta.get("cvd_divergence") / k0.volume if k0.volume > 0 else 0.0,
        }

        def _update_diag(m):
            if diag is not None:
                diag.update(m)

        ticks = None
        if tick_map is not None:
            ticks = tick_map.get(entry_bar.open_time)

        if ticks is not None and len(ticks) > 0:
            cum_buy_vol = 0.0
            cum_vol     = 0.0
            for i, tick in enumerate(ticks):
                price = float(tick[1])
                qty   = float(tick[2])
                is_bm = tick[3] > 0.5

                # 檢查最大等待 tick 數
                if (i + 1) > self.max_wait_ticks:
                    break

                cum_vol += qty
                if not is_bm:
                    cum_buy_vol += qty

                if guardian is not None and price < guardian:
                    meta["guardian_killed"] = True
                    _update_diag(meta)
                    return None

                if cum_vol > 0:
                    cum_delta_eff = (2.0 * cum_buy_vol - cum_vol) / cum_vol
                else:
                    cum_delta_eff = -1.0

                if price > boundary:
                    meta["boundary_touched"] = True
                    if cum_delta_eff >= self.entry_delta_eff_threshold:
                        slippage = price - boundary

                        # 檢查滑點限制（USDT 固定 or R 標準化，由 _eff_max_slippage 決定）
                        if slippage > _eff_max_slippage:
                            return None

                        meta["wait_ticks"]      = i + 1
                        meta["fill_slippage"]   = slippage
                        meta["entry_delta_eff"] = cum_delta_eff
                        _update_diag(meta)
                        return StrategySignal(
                            open_time   = entry_bar.open_time,
                            price       = boundary,
                            fill_price  = price,
                            stop_price  = k0.low - self.sl_offset,
                            signal_type = "long_entry",
                            label       = "L_CVD_TICK",
                            meta        = meta,
                        )
                    else:
                        meta["delta_not_enough"] = True

            _update_diag(meta)
            return None

        if self.allow_bar_fallback:
            if entry_bar.low < (guardian or -1e9):
                meta["guardian_killed"] = True
                _update_diag(meta)
                return None
            if entry_bar.high > boundary:
                # Bar 模式無法確認精確 tick delta_eff，僅作為保底
                _update_diag(meta)
                return StrategySignal(
                    open_time   = entry_bar.open_time,
                    price       = boundary,
                    direction   = "long",
                    stop_price  = k0.low - self.sl_offset,
                    signal_type = "long_entry",
                    label       = "L_CVD_BAR",
                    meta        = meta,
                )

        _update_diag(meta)
        return None


# ── Enhancer Modules ──────────────────────────────────────────────────────────

class ReversalBarUpEnhancer(EnhancerModule):
    """
    確認 k0 訊號棒有反轉棒結構（純 Kline，無需 tick 資料）：
      range > avg_range_window 棒均值  ← 活躍棒，非縮量
      lower_wick_ratio >= min_wick_ratio   ← 下影線佔 range 比例
      close_pos        >= min_close_pos    ← 收盤位於棒內偏高位置
    """

    name = "ReversalBarUpEnhancer"

    def __init__(
        self,
        min_wick_ratio:   float = 0.50,
        min_close_pos:    float = 0.60,
        avg_range_window: int   = 20,
    ) -> None:
        self.min_wick_ratio   = min_wick_ratio
        self.min_close_pos    = min_close_pos
        self.avg_range_window = avg_range_window

    def evaluate(self, ctx: PipelineContext) -> bool:
        k0_meta = ctx.alpha_meta.get("k0_meta", {})
        k0_idx  = k0_meta.get("k0_idx")
        if k0_idx is None:
            return False
        k0  = ctx.klines[k0_idx]
        rng = k0.high - k0.low
        if rng <= 0:
            return False
        start   = max(0, k0_idx - self.avg_range_window)
        avg_rng = sum(
            k.high - k.low for k in ctx.klines[start : k0_idx + 1]
        ) / max(k0_idx - start + 1, 1)
        body_lo    = min(k0.open, k0.close)
        lower_wick = (body_lo - k0.low) / rng
        close_pos  = (k0.close - k0.low) / rng
        return (
            rng > avg_rng
            and lower_wick >= self.min_wick_ratio
            and close_pos  >= self.min_close_pos
        )


class LowerWickDeltaEffEnhancer(EnhancerModule):
    """
    驗證 k0 下影線區間（low ~ body_low）tick net delta >= min_eff。

    tick_map 不可用（或該棒無 tick / 無下影線 tick）時 fallback 通過（True），
    確保無 tick 回測不會因此全面阻斷。
    """

    name = "LowerWickDeltaEffEnhancer"

    def __init__(self, min_eff: float = 0.0) -> None:
        self.min_eff = min_eff

    def evaluate(self, ctx: PipelineContext) -> bool:
        if ctx.tick_map is None:
            return True
        k0_meta = ctx.alpha_meta.get("k0_meta", {})
        k0_idx  = k0_meta.get("k0_idx")
        if k0_idx is None:
            return False
        k0    = ctx.klines[k0_idx]
        ticks = ctx.tick_map.get(k0.open_time)
        if ticks is None or len(ticks) == 0:
            return True
        body_lo = min(k0.open, k0.close)
        zone    = ticks[ticks[:, 1] <= body_lo]
        if len(zone) == 0:
            return True
        qty       = zone[:, 2]
        buy_qty   = float(np.sum(qty[zone[:, 3] == 0.0]))
        total_qty = float(np.sum(qty))
        if total_qty <= 0:
            return True
        return (2.0 * buy_qty - total_qty) / total_qty >= self.min_eff


class BuyVolumeZscoreEnhancer(EnhancerModule):
    """
    確認 k0 的 taker_buy_volume z-score 在近 window 棒中 >= min_zscore。
    過濾低量背離：Delta 改善量小且無真實買盤放量支撐的情境。
    """

    name = "BuyVolumeZscoreEnhancer"

    def __init__(self, window: int = 20, min_zscore: float = 0.5) -> None:
        self.window     = window
        self.min_zscore = min_zscore

    def evaluate(self, ctx: PipelineContext) -> bool:
        k0_meta = ctx.alpha_meta.get("k0_meta", {})
        k0_idx  = k0_meta.get("k0_idx")
        if k0_idx is None or k0_idx < self.window:
            return False
        bars    = ctx.klines[k0_idx - self.window + 1 : k0_idx + 1]
        buy_vol = np.array([k.taker_buy_volume for k in bars], dtype=np.float64)
        std     = float(np.std(buy_vol, ddof=0))
        if std <= 0.0:
            return False
        zscore = (buy_vol[-1] - float(np.mean(buy_vol))) / std
        return zscore >= self.min_zscore


class CVDDivergenceStrengthEnhancer(EnhancerModule):
    """
    確認 CVD 背離強度：min_ratio <= cvd_divergence / k0.volume <= max_ratio。

    cvd_divergence = rolling_delta_k0 − prev_delta（n 棒均值差）。
    除以 k0.volume 使其無量綱，便於跨品種 / 跨時段比較與調參。

    AlphaStage 已保證 cvd_divergence > 0（cvd_rose 條件）。
    max_ratio=None 表示無上限（預設，等同原版下限過濾）。
    max_ratio 設定時作為「反轉上限濾網」，排除過強背離（回測顯示強背離負向相關）。
    """

    name = "CVDDivergenceStrengthEnhancer"

    def __init__(
        self,
        min_ratio: float        = 0.05,
        max_ratio: float | None = None,
    ) -> None:
        self.min_ratio = min_ratio
        self.max_ratio = max_ratio

    def evaluate(self, ctx: PipelineContext) -> bool:
        k0_meta        = ctx.alpha_meta.get("k0_meta", {})
        k0_idx         = k0_meta.get("k0_idx")
        cvd_divergence = k0_meta.get("cvd_divergence")
        if k0_idx is None or cvd_divergence is None:
            return False
        k0_volume = ctx.klines[k0_idx].volume
        if k0_volume <= 0:
            return False
        ratio = cvd_divergence / k0_volume
        if ratio < self.min_ratio:
            return False
        if self.max_ratio is not None and ratio > self.max_ratio:
            return False
        return True


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
    # ── Stage 2: Alpha（NYCVDDivergenceTickEntry）───────────────────────────
    cvd_window:    int   = 20,
    sl_offset:     float = 0.0,
    min_micro_cvd: float = 0.0,
    # V2 Entry State Machine 參數
    entry_boundary_mode:       str   = "high",
    guardian_mode:             str   = "body_low",
    entry_delta_eff_threshold: float = 0.4,
    zoom_bars:                 int   = 1,
    allow_bar_fallback:        bool  = False,
    max_fill_slippage:         float = 1e9,
    max_fill_slippage_R:       float = 0.0,
    min_k0_range_atr:          float = 0.0,
    max_wait_ticks:            int   = 999999,
    # ── Stage 2.5: Enhancer ──────────────────────────────────────────────────
    # 直接傳入 enhancer_modules 可覆蓋所有 flag（向後相容）
    enhancer_modules: list | None = None,
    # A. ReversalBarUpEnhancer
    use_reversal_bar_up:        bool  = False,
    reversal_bar_min_wick:      float = 0.50,
    reversal_bar_min_close_pos: float = 0.60,
    reversal_bar_avg_window:    int   = 20,
    # B. LowerWickDeltaEffEnhancer（需 tick；tick 不可用時自動 pass）
    use_lower_wick_delta_eff: bool  = False,
    lower_wick_delta_eff_min: float = 0.0,
    # C. BuyVolumeZscoreEnhancer
    use_buy_volume_zscore: bool  = False,
    buy_vol_zscore_window: int   = 20,
    buy_vol_zscore_min:    float = 0.5,
    # D. CVDDivergenceStrengthEnhancer
    use_cvd_div_strength: bool        = True,
    # cvd_div_strength_min: float       = 0.05,
    cvd_div_strength_min: float       = 0.02,
    # cvd_div_strength_max: float | None = None,
    cvd_div_strength_max: float       = 0.15,
    # ── Stage 3: EntryManagement（ATR 停損）─────────────────────────────────
    atr_period:   int   = 14,
    atr_k:        float = 1.0,
    # max_sl_pct:   float = 0.03,
    max_sl_pct:   float = 0.005,
    min_stop_pct: float = 0.0015,
    # ── Stage 4: RR（固定 2.0R）─────────────────────────────────────────────
    # rr_ratio:    float                   = 2.0,
    rr_ratio:    float                   = 1.3,
    capital_cfg: Optional[CapitalConfig] = None,
    # ── Stage 5: 費用覆蓋率─────────────────────────────────────────────────
    taker_fee_rate:  float = 0.00032,
    slippage_rate:   float = 0.00002,
    fee_cover_ratio: float = 1.7,
) -> TradingPipeline:
    """
    NY CVD Divergence MR Pipeline 工廠函式。

    allowed_combos 是單一真相來源：
      - RegimeStage 自動從中推導各維度 union 做快速預篩
      - NYCVDRegimeComboStage 做最終精確 4-tuple 匹配

    Enhancer flags（use_* = False 預設全關，逐一啟用做交叉驗證）：
      A. use_reversal_bar_up        — k0 反轉棒結構（純 Kline）
      B. use_lower_wick_delta_eff   — 下影線 tick net delta（需 tick_map）
      C. use_buy_volume_zscore      — k0 買量 z-score 門檻
      D. use_cvd_div_strength       — CVD 背離強度（相對 k0 成交量）

    如直接傳入 enhancer_modules，flags 全部被忽略（向後相容）。
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

    signal = NYCVDDivergenceTickEntrySignal(
        window                    = cvd_window,
        sl_offset                 = sl_offset,
        entry_boundary_mode       = entry_boundary_mode,
        guardian_mode             = guardian_mode,
        entry_delta_eff_threshold = entry_delta_eff_threshold,
        zoom_bars                 = zoom_bars,
        allow_bar_fallback        = allow_bar_fallback,
        max_fill_slippage         = max_fill_slippage,
        max_fill_slippage_R       = max_fill_slippage_R,
        min_k0_range_atr          = min_k0_range_atr,
        max_wait_ticks            = max_wait_ticks,
    )

    # flag → modules 組裝（直接傳入 enhancer_modules 時略過 flags）
    if enhancer_modules is None:
        enhancer_modules = []
        if use_reversal_bar_up:
            enhancer_modules.append(ReversalBarUpEnhancer(
                min_wick_ratio   = reversal_bar_min_wick,
                min_close_pos    = reversal_bar_min_close_pos,
                avg_range_window = reversal_bar_avg_window,
            ))
        if use_lower_wick_delta_eff:
            enhancer_modules.append(LowerWickDeltaEffEnhancer(
                min_eff = lower_wick_delta_eff_min,
            ))
        if use_buy_volume_zscore:
            enhancer_modules.append(BuyVolumeZscoreEnhancer(
                window     = buy_vol_zscore_window,
                min_zscore = buy_vol_zscore_min,
            ))
        if use_cvd_div_strength:
            enhancer_modules.append(CVDDivergenceStrengthEnhancer(
                min_ratio = cvd_div_strength_min,
                max_ratio = cvd_div_strength_max,
            ))

    return TradingPipeline([
        PositionGateStage(max_positions=max_positions),
        RegimeStage(
            components = [session_comp, mv_comp, vwap_comp, vp_comp],
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
            exit_mod       = ExitModule(ExitConfig(tp_rr_ratio=1.3)),
            initial_equity = 10_000.0,
        )
