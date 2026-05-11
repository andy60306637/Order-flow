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
    GROUP_CVD_DIVERGENCE,
    GROUP_EXHAUSTION_RECLAIM,
    GROUP_LIQUIDITY_SWEEP,
    GROUP_MEAN_REVERSION,
    GROUP_ORDER_FLOW_ABSORPTION,
    GROUP_VOLUME_PROFILE_ALPHA,
    FactorBase,
    klines_to_arrays,
    safe_divide,
)
from research.factors import (
    _atr,
    _rolling_min,
    _rolling_volume_profile,
    _rolling_zscore,
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


# ---------------------------------------------------------------------------
# Stage 2 Alpha Factors — Long-only, continuous scores
# ---------------------------------------------------------------------------


@register_factor
class SweepLowReclaimFactor(FactorBase):
    """
    流動性掃盪後收回 (Liquidity Sweep + Reclaim)。

    bar 的 low 跌破過去 WINDOW 根低點（rolling_low_prev，不含當前 bar），
    但 close 收回 rolling_low_prev 之上。

    score = ((rolling_low_prev - low) / ATR) × ((close - rolling_low_prev) / ATR)

    條件不成立時 score = 0；ATR 或 rolling_low_prev 為 NaN 時同樣為 0。
    """

    name = "sweep_low_reclaim"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_LIQUIDITY_SWEEP

    WINDOW: int = 20

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        if not klines:
            return np.empty(0, dtype=np.float64)
        arr = klines_to_arrays(klines)
        low, close = arr["low"], arr["close"]
        n = len(low)
        out = np.zeros(n, dtype=np.float64)

        atr = _atr(arr, 14)

        # rolling_low_prev[i] = min(low[i-WINDOW], ..., low[i-1]) — no current bar
        prev_low = np.empty(n, dtype=np.float64)
        prev_low[0] = np.nan
        prev_low[1:] = low[:-1]
        rolling_low_prev = _rolling_min(prev_low, self.WINDOW)

        mask = (
            np.isfinite(rolling_low_prev)
            & np.isfinite(atr)
            & (atr > 0)
            & (low < rolling_low_prev)
            & (close > rolling_low_prev)
        )
        if mask.any():
            sweep_depth = (rolling_low_prev[mask] - low[mask]) / atr[mask]
            reclaim = (close[mask] - rolling_low_prev[mask]) / atr[mask]
            out[mask] = sweep_depth * reclaim

        return out


@register_factor
class CvdBullishDivergenceFactor(FactorBase):
    """
    CVD 多頭背離強度。

    當前 low 接近或低於過去 WINDOW 根低點，但全域累積 CVD 高於前低時的 CVD。

    score = max((cvd_current - cvd_at_previous_low) / rolling_sum(abs(delta), WINDOW), 0)

    條件不成立時 score = 0。
    """

    name = "cvd_bullish_divergence"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_CVD_DIVERGENCE

    WINDOW: int = 20
    PRICE_TOLERANCE: float = 0.002

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        if not klines:
            return np.empty(0, dtype=np.float64)
        arr = klines_to_arrays(klines)
        low = arr["low"]
        tbv = arr["taker_buy_volume"]
        vol = arr["volume"]
        n = len(low)
        out = np.zeros(n, dtype=np.float64)

        delta = 2.0 * tbv - vol
        cvd = np.cumsum(delta)
        abs_delta = np.abs(delta)

        # rolling sum of abs(delta) over WINDOW bars ending at i (inclusive)
        cumsum_abs = np.cumsum(abs_delta)
        roll_abs = np.empty(n, dtype=np.float64)
        roll_abs[:] = cumsum_abs  # first WINDOW bars: sum from bar 0
        roll_abs[self.WINDOW:] = cumsum_abs[self.WINDOW:] - cumsum_abs[: n - self.WINDOW]

        N = self.WINDOW
        for i in range(N + 1, n):
            window_start = i - N
            window_lows = low[window_start:i]
            prev_low_local = int(np.argmin(window_lows))
            prev_low_idx = window_start + prev_low_local
            prev_low_val = low[prev_low_idx]

            if low[i] > prev_low_val * (1.0 + self.PRICE_TOLERANCE):
                continue

            cvd_diff = cvd[i] - cvd[prev_low_idx]
            if cvd_diff <= 0:
                continue

            abs_sum = roll_abs[i]
            if abs_sum <= 0:
                continue

            out[i] = cvd_diff / abs_sum

        return out


@register_factor
class NegativeDeltaAbsorptionFactor(FactorBase):
    """
    負向 Delta 吸收訊號。

    delta_z 明顯為負（賣壓），但 close_position 高且下影線長，
    代表空頭被吸收、多頭主控收盤。

    score = abs(delta_z) × close_position × lower_wick_ratio
        條件: delta_z < -1.0, close_position > 0.6, lower_wick_ratio > 0.3
    條件不成立時 score = 0。
    """

    name = "negative_delta_absorption"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_ORDER_FLOW_ABSORPTION

    ZSCORE_WINDOW: int = 50

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        if not klines:
            return np.empty(0, dtype=np.float64)
        arr = klines_to_arrays(klines)
        high, low, open_, close = arr["high"], arr["low"], arr["open"], arr["close"]
        n = len(close)
        out = np.zeros(n, dtype=np.float64)

        delta = 2.0 * arr["taker_buy_volume"] - arr["volume"]
        delta_z = _rolling_zscore(delta, self.ZSCORE_WINDOW)

        rng = high - low
        close_position = safe_divide(close - low, rng)
        body_lo = np.minimum(open_, close)
        lower_wick_ratio = safe_divide(body_lo - low, rng)

        mask = (
            np.isfinite(delta_z)
            & np.isfinite(close_position)
            & np.isfinite(lower_wick_ratio)
            & (delta_z < -1.0)
            & (close_position > 0.6)
            & (lower_wick_ratio > 0.3)
        )
        if mask.any():
            out[mask] = np.abs(delta_z[mask]) * close_position[mask] * lower_wick_ratio[mask]

        return out


@register_factor
class ValReclaimLongFactor(FactorBase):
    """
    Value Area Low 收回（多頭）。

    low < VAL 且 close > VAL，代表跌破成交密集帶下緣後收回。

    score = ((VAL - low) / ATR) × ((close - VAL) / ATR)

    VAL 由 WINDOW 根 K 棒的 rolling volume profile 計算（70% 成交量集中帶底部）。
    VAL 缺失或條件不成立時 score = 0。
    """

    name = "val_reclaim_long"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_VOLUME_PROFILE_ALPHA

    WINDOW: int = 20
    N_BINS: int = 24

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        if not klines:
            return np.empty(0, dtype=np.float64)
        arr = klines_to_arrays(klines)
        low, close = arr["low"], arr["close"]
        n = len(close)
        out = np.zeros(n, dtype=np.float64)

        atr = _atr(arr, 14)
        _, val = _rolling_volume_profile(arr["high"], low, arr["volume"], self.WINDOW, self.N_BINS)

        mask = (
            np.isfinite(val)
            & np.isfinite(atr)
            & (atr > 0)
            & (low < val)
            & (close > val)
        )
        if mask.any():
            sweep_depth = (val[mask] - low[mask]) / atr[mask]
            reclaim = (close[mask] - val[mask]) / atr[mask]
            out[mask] = sweep_depth * reclaim

        return out


@register_factor
class PocReversionPotentialFactor(FactorBase):
    """
    POC 回歸空間（多頭 reward potential）。

    POC 在 close 上方時，score 反映距離 POC 的 ATR 倍數，
    代表價格回歸 Point of Control 的潛在空間。

    score = clip((POC - close) / ATR, 0, MAX_DISTANCE_ATR)

    不應單獨作為 entry signal；用於確認上方回歸空間。
    """

    name = "poc_reversion_potential"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_VOLUME_PROFILE_ALPHA

    WINDOW: int = 20
    N_BINS: int = 24
    MAX_DISTANCE_ATR: float = 5.0

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        if not klines:
            return np.empty(0, dtype=np.float64)
        arr = klines_to_arrays(klines)
        close = arr["close"]
        n = len(close)
        out = np.zeros(n, dtype=np.float64)

        atr = _atr(arr, 14)
        poc, _ = _rolling_volume_profile(arr["high"], arr["low"], arr["volume"], self.WINDOW, self.N_BINS)

        valid = np.isfinite(poc) & np.isfinite(atr) & (atr > 0)
        if valid.any():
            dist = (poc[valid] - close[valid]) / atr[valid]
            out[valid] = np.clip(dist, 0.0, self.MAX_DISTANCE_ATR)

        return out


@register_factor
class ReturnShockReclaimFactor(FactorBase):
    """
    短期報酬震盪後收回。

    N 根報酬 ret_N_z 明顯為負（急跌），但當前 close_position 高，
    代表急跌後多頭承接、收盤位置強。

    score = abs(ret_N_z) × close_position
        條件: ret_N_z < -2.0, close_position > 0.6
    條件不成立時 score = 0。
    """

    name = "return_shock_reclaim"
    sides = (FACTOR_SIDE_LONG,)
    group = GROUP_EXHAUSTION_RECLAIM

    N: int = 10
    ZSCORE_WINDOW: int = 100

    def compute(self, klines: list[Kline], tick_map: TickBarMap | None = None) -> np.ndarray:
        if not klines:
            return np.empty(0, dtype=np.float64)
        arr = klines_to_arrays(klines)
        high, low, close = arr["high"], arr["low"], arr["close"]
        n = len(close)
        out = np.zeros(n, dtype=np.float64)

        prev_close = np.empty(n, dtype=np.float64)
        prev_close[: self.N] = np.nan
        prev_close[self.N :] = close[: n - self.N]
        ret_N = safe_divide(close - prev_close, prev_close)
        ret_N_z = _rolling_zscore(ret_N, self.ZSCORE_WINDOW)

        rng = high - low
        close_position = safe_divide(close - low, rng)

        mask = (
            np.isfinite(ret_N_z)
            & np.isfinite(close_position)
            & (ret_N_z < -2.0)
            & (close_position > 0.6)
        )
        if mask.any():
            out[mask] = np.abs(ret_N_z[mask]) * close_position[mask]

        return out
