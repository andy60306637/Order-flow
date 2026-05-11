"""
research/mr_alpha_ic_factors.py

均值回歸 Pipeline Stage 2 三個 Alpha 因子的 IC 測試包裝。

每個因子以「連續值」形式輸出（不套入閾值過濾），讓 IC 框架測量
原始指標與後續報酬之間的相關性。factor[i] 對應 klines[i] 的形態，
配合 entry_lag=1 使用，與 Pipeline 的「signal bar → execution bar」
對齊。

因子說明：
  mr_lwde_eff         wick_ratio × imbalance（LWDE 效率值，無門檻）
  mr_rbu_strength     lower_wick_ratio × close_pos（RBU 形態強度，無振幅門檻）
  mr_cvdd_divergence  CVD 正背離強度（僅當 bar 接近視窗低點時非 NaN）
"""
from __future__ import annotations

import numpy as np

from core.data_types import Kline
from research.base import (
    FACTOR_SIDE_LONG,
    GROUP_MEAN_REVERSION,
    FactorBase,
)
from research.registry import register_factor
from strategies.base import TickBarMap


@register_factor
class MrLwdeEffFactor(FactorBase):
    """
    下影線 × Delta 效率（LowerWickDeltaEff 連續值版本）。

    每根 K 棒計算：eff = wick_ratio × imbalance
      lower_wick  = min(open, close) - low
      wick_ratio  = lower_wick / range
      imbalance   = (taker_buy - taker_sell) / volume
      eff         = wick_ratio × imbalance

    IC 語義：eff 越高 → 下影線長且買方主導 → 預期後續上漲報酬越高。
    負值（下影線短或賣方主導）應預測更差的報酬。
    """

    name = "mr_lwde_eff"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_MEAN_REVERSION

    def compute(
        self,
        klines: list[Kline],
        tick_map: TickBarMap | None = None,
    ) -> np.ndarray:
        n = len(klines)
        out = np.full(n, np.nan, dtype=np.float64)

        for i, k in enumerate(klines):
            rng = k.high - k.low
            if rng <= 0 or k.volume <= 0:
                continue
            body_lo = min(k.open, k.close)
            lower_wick = body_lo - k.low
            wick_ratio = lower_wick / rng
            imbalance = (k.taker_buy_volume - (k.volume - k.taker_buy_volume)) / (k.volume + 1e-10)
            out[i] = wick_ratio * imbalance

        return out


@register_factor
class MrRbuStrengthFactor(FactorBase):
    """
    Reversal Bar Up 形態強度（ReversalBarUp 連續值版本）。

    每根 K 棒計算：strength = lower_wick_ratio × close_pos
      lower_wick_ratio = (min(open,close) - low) / range
      close_pos        = (close - low) / range

    不套入振幅過濾（rng > avg_rng），保留所有 K 棒的連續值。
    IC 語義：strength 越高 → 下影長且收盤位置高 → 空頭被吸收後
    買方主控，預期後續報酬越高。
    """

    name = "mr_rbu_strength"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_MEAN_REVERSION

    def compute(
        self,
        klines: list[Kline],
        tick_map: TickBarMap | None = None,
    ) -> np.ndarray:
        n = len(klines)
        out = np.full(n, np.nan, dtype=np.float64)

        for i, k in enumerate(klines):
            rng = k.high - k.low
            if rng <= 0:
                continue
            body_lo = min(k.open, k.close)
            lower_wick_ratio = (body_lo - k.low) / rng
            close_pos = (k.close - k.low) / rng
            out[i] = lower_wick_ratio * close_pos

        return out


@register_factor
class MrCvddDivergenceFactor(FactorBase):
    """
    CVD 正背離強度（CVDDivergence 連續值版本）。

    對每根 K 棒（作為潛在信號棒），計算：
      1. 視窗（20 根）內的最低收盤低點（trough）
      2. 若當前 bar 的 low 在 trough.low × (1 + 0.002) 以內
         → 計算滾動 CVD 背離：cvd_divergence = cvd_current - cvd_trough
      3. 否則輸出 NaN

    IC 語義：cvd_divergence 越大 → 在同等價格低點位置，
    累積買盤越強 → 空頭動能衰竭，預期後續報酬越高。
    """

    name = "mr_cvdd_divergence"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_MEAN_REVERSION

    WINDOW: int = 20
    PRICE_TOLERANCE: float = 0.002

    def compute(
        self,
        klines: list[Kline],
        tick_map: TickBarMap | None = None,
    ) -> np.ndarray:
        n = len(klines)
        out = np.full(n, np.nan, dtype=np.float64)

        # 預先計算每根 K 棒的 kline delta（避免重複計算）
        deltas = np.array(
            [k.taker_buy_volume - (k.volume - k.taker_buy_volume) for k in klines],
            dtype=np.float64,
        )

        for i in range(self.WINDOW + 1, n):
            k0 = klines[i]

            # 視窗：i 之前的 WINDOW 根（不含 i，避免前視偏差）
            hist_start = i - self.WINDOW
            hist = klines[hist_start:i]

            # 找視窗最低點
            trough = min(hist, key=lambda k: k.low)
            if k0.low > trough.low * (1.0 + self.PRICE_TOLERANCE):
                continue  # bar 不在低點附近

            # 計算滾動 CVD
            trough_idx = hist_start + hist.index(trough)
            window_deltas = deltas[hist_start : i + 1]
            cvd = np.cumsum(window_deltas)

            cvd_trough = cvd[trough_idx - hist_start]
            cvd_k0 = cvd[-1]
            out[i] = cvd_k0 - cvd_trough

        return out
