"""
BTCUSDT Weekly VWAP Sigma + CVD + 1m Alpha Strategy v1
======================================================

嚴格依照規格實作的多空策略。設計目標：可在本系統 **tick 級回測**
（`on_history(klines, tick_map)`）下順利運行，並輸出 **MAE / MFE**（R 倍數）
供 backtest dashboard 之 `mae_r` / `mfe_r` 欄位使用。

系統介接約定（重要）
--------------------
* `klines`  : 由 tick 聚合的 **1m** Kline 序列（chronological，最舊→最新）。
* `tick_map`: dict[open_time_ms] -> ndarray(N, 4)
              col0 = trade_time(ms), col1 = price, col2 = qty,
              col3 = is_buyer_maker (1.0 / 0.0)。
* Delta 判斷方式（**非 OHLCV proxy**）：
      is_buyer_maker == 0.0  -> 買方主動 (taker buy)  -> **positive delta** (+qty)
      is_buyer_maker == 1.0  -> 賣方主動 (taker sell) -> **negative delta** (-qty)
  此即 Binance aggTrade 的 `m` 欄位語意。CVD 全部由此 tick-level direction 累加，
  僅在某 1m bar 完全沒有 tick 時，才退化為 bar proxy (2*taker_buy_volume - volume)
  並於 meta 標記 `cvd_source="bar_proxy"`。

成本模型（CostModel）
--------------------
策略本身只產生訊號；fee / slippage 由 `backtest.engine.BacktestConfig` 套用。
規格要求：fee_rate = 0.032%/side、slippage = 0.2 bps。對應建議設定：
    BacktestConfig(fee_mode="自訂", custom_fee_rate=0.00032,
                   slippage_bps=0.2, leverage=20)
見 `BTCWeeklyVWAPSigmaCVDStrategy.recommended_backtest_config()`。

成交近似標記（Anti-Lookahead）
------------------------------
* 所有 1m signal 僅在 candle **close confirmed** 後成立。
* entry / TP / breakout band-reentry exit 皆在 signal candle close 後的
  **下一根 1m bar 的第一個 tick** 成交（= 下一個 tick），fill_price 取該 tick price。
* SL 為 **tick-level** 觸發：持倉期間逐 tick 比對，觸發即以該 tick price 成交
  （若該 bar 無 tick 則退化為 bar low/high 觸發，meta 標記 sl_exec="bar_approx"）。
* weekly VWAP / sigma / CVD(15m) / rolling 4h POC 全部為 expanding / rolling，
  僅使用「當下已收盤」資料，無未來資料。

模組對應（可獨立測試）
----------------------
1. TickTo1mCandleBuilder        -> 由系統提供（klines 已是 1m）
2. WeeklyVWAPSigmaCalculator    -> class WeeklyVWAPSigmaCalculator
3. CVDFilter                    -> class CVDFilter
4. Rolling4hPOC                 -> class Rolling4hPOC
5. SigmaEventDetector           -> _detect_band_events()
6. ReclaimAlphaDetector         -> _ReclaimSetup + _step_reclaim()
7. BreakoutRetestAlphaDetector  -> _BreakoutSetup + _step_breakout()
8. EntryDecisionEngine          -> on_history main loop (conflict / warmup / poc 守門)
9. ExitEngine                   -> _manage_position()
10. CostModel                   -> recommended_backtest_config()
11. TradeLogger                 -> StrategySignal.meta (reason 欄位)
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import numpy as np

from core.data_types import Kline
from strategies.base import StrategyBase, StrategySignal, TickBarMap
from strategies import register

# ── 常數 ──────────────────────────────────────────────────────────────────────
MS_DAY = 86_400_000
MS_HOUR = 3_600_000
# 1970-01-04 為星期日（epoch day 3）；以此為 weekly reset 對齊基準。
_SUNDAY_EPOCH_DAY = 3


# ═══════════════════════════════════════════════════════════════════════════
# 2. WeeklyVWAPSigmaCalculator
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class WeeklyBands:
    weekly_vwap: float
    weekly_std: float
    upper_1sigma: float
    upper_2sigma: float
    lower_1sigma: float
    lower_2sigma: float
    weekly_reset_id: int
    is_weekly_warmup: bool


class WeeklyVWAPSigmaCalculator:
    """可選週期（每日/每週日 00:00 UTC）reset 的 expanding volume-weighted VWAP 與 sigma。

    typical_price = (high + low + close) / 3
    vwap          = Σ(tp·v) / Σv
    std           = sqrt( Σ(v·(tp-vwap)^2) / Σv )
    """

    def __init__(self, warmup_hours: float = 8.0, reset_interval: str = "weekly") -> None:
        self.warmup_ms = int(warmup_hours * MS_HOUR)
        self.reset_interval = reset_interval.lower()
        self._reset_id: int = -1
        self._period_start_ms: int = 0
        self._sum_v = 0.0
        self._sum_tpv = 0.0
        self._sum_tp2v = 0.0

    def get_period_id(self, open_time_ms: int) -> int:
        epoch_day = open_time_ms // MS_DAY
        if self.reset_interval == "daily":
            return epoch_day
        # weekly: 1970-01-04 為星期日（epoch day 3）
        return (epoch_day - _SUNDAY_EPOCH_DAY) // 7

    def get_period_start_ms(self, period_id: int) -> int:
        if self.reset_interval == "daily":
            return period_id * MS_DAY
        return (_SUNDAY_EPOCH_DAY + period_id * 7) * MS_DAY

    def update(self, k: Kline) -> WeeklyBands:
        pid = self.get_period_id(k.open_time)
        if pid != self._reset_id:
            # 新的週期 -> reset 累加器
            self._reset_id = pid
            self._period_start_ms = self.get_period_start_ms(pid)
            self._sum_v = self._sum_tpv = self._sum_tp2v = 0.0

        tp = (k.high + k.low + k.close) / 3.0
        v = k.volume
        self._sum_v += v
        self._sum_tpv += tp * v
        self._sum_tp2v += tp * tp * v

        if self._sum_v > 0:
            vwap = self._sum_tpv / self._sum_v
            var = max(0.0, self._sum_tp2v / self._sum_v - vwap * vwap)
            std = var ** 0.5
        else:
            vwap = tp
            std = 0.0

        warmup = (k.open_time - self._period_start_ms) < self.warmup_ms
        return WeeklyBands(
            weekly_vwap=vwap, weekly_std=std,
            upper_1sigma=vwap + std, upper_2sigma=vwap + 2 * std,
            lower_1sigma=vwap - std, lower_2sigma=vwap - 2 * std,
            weekly_reset_id=pid, is_weekly_warmup=warmup,
        )


# ═══════════════════════════════════════════════════════════════════════════
# 3. CVDFilter
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class CVDState:
    cvd_15m: float
    cvd_high_15m: float
    cvd_low_15m: float
    price_high_15m: float
    price_low_15m: float
    bullish_cvd_divergence: bool
    bearish_cvd_divergence: bool
    bullish_cvd_acceleration: bool
    bearish_cvd_acceleration: bool
    cvd_source: str


class CVDFilter:
    """tick-level CVD 與 15m rolling divergence / acceleration 偵測。

    * 每根 1m bar 的 delta = Σ tick_delta（買 +qty / 賣 -qty）。
    * cum_cvd 為連續累積 delta；cvd_15m = cum_cvd[i] - cum_cvd[i-15]（rolling sum）。
    * divergence 使用 15m window（含當下）內的 price / cum_cvd 極值：
        bullish: price 創 window low 但 cum_cvd 未創 window low
        bearish: price 創 window high 但 cum_cvd 未創 window high
    * acceleration 僅比較 cvd_15m 的方向（突破的價格條件由 breakout detector 負責）：
        bullish: cvd_15m[i] > cvd_15m[i-1]
        bearish: cvd_15m[i] < cvd_15m[i-1]
    """

    def __init__(self, window: int = 15) -> None:
        self.window = window
        self._cum = 0.0
        self._prev_cvd15: Optional[float] = None
        # rolling buffers（保留近 window 根）
        self._highs: deque[float] = deque(maxlen=window)
        self._lows: deque[float] = deque(maxlen=window)
        self._cums: deque[float] = deque(maxlen=window)
        self._cum_window: deque[float] = deque(maxlen=window + 1)

    @staticmethod
    def bar_delta(k: Kline, ticks: Optional[np.ndarray]) -> Tuple[float, str]:
        """回傳 (delta, source)。優先用 tick-level direction，無 tick 才用 bar proxy。"""
        if ticks is not None and len(ticks) > 0:
            qty = ticks[:, 2]
            is_bm = ticks[:, 3] > 0.5            # True = 賣方主動 -> -qty
            sell = float(np.sum(qty[is_bm]))
            buy = float(np.sum(qty[~is_bm]))
            return buy - sell, "tick"
        # bar proxy：taker_buy 為主動買，volume - taker_buy 為主動賣
        return 2.0 * k.taker_buy_volume - k.volume, "bar_proxy"

    def update(self, k: Kline, ticks: Optional[np.ndarray]) -> CVDState:
        delta, src = self.bar_delta(k, ticks)
        self._cum += delta
        self._cum_window.append(self._cum)
        # cvd_15m = 近 window 根 delta 之和 = cum[i] - cum[i-window]
        if len(self._cum_window) > self.window:
            cvd_15m = self._cum_window[-1] - self._cum_window[0]
        else:
            cvd_15m = self._cum  # 暖機期：從序列起點累積

        self._highs.append(k.high)
        self._lows.append(k.low)
        self._cums.append(self._cum)

        price_high = max(self._highs)
        price_low = min(self._lows)
        cvd_high = max(self._cums)
        cvd_low = min(self._cums)

        # 創新極值（含當下；當下為極值代表「創 lower low / higher high」）
        bull_div = (k.low <= price_low) and (self._cum > cvd_low)
        bear_div = (k.high >= price_high) and (self._cum < cvd_high)

        if self._prev_cvd15 is None:
            bull_acc = bear_acc = False
        else:
            bull_acc = cvd_15m > self._prev_cvd15
            bear_acc = cvd_15m < self._prev_cvd15
        self._prev_cvd15 = cvd_15m

        return CVDState(
            cvd_15m=cvd_15m, cvd_high_15m=cvd_high, cvd_low_15m=cvd_low,
            price_high_15m=price_high, price_low_15m=price_low,
            bullish_cvd_divergence=bull_div, bearish_cvd_divergence=bear_div,
            bullish_cvd_acceleration=bull_acc, bearish_cvd_acceleration=bear_acc,
            cvd_source=src,
        )


# ═══════════════════════════════════════════════════════════════════════════
# 4. Rolling4hPOC
# ═══════════════════════════════════════════════════════════════════════════
class Rolling4hPOC:
    """rolling 4h（240 根 1m）成交量分佈之 POC（最大成交量價位 bin）。

    優先以 tick 累積各價格 bin 的 qty；無 tick 時把整根 bar volume 記到 typical price bin。
    僅用過去以前已完成資料（每根收盤後加入）。

    實作：preallocated numpy array + mini-bincount + active-range argmax
    * _hist: shape (n_bins,) float64，index = (bin_number - _bin_offset)
    * 每 bar 的 tick 價格範圍極窄（±50 USDT → ~10 bins）；先用 mini-bincount
      在這個小範圍內彙整，再 flatnonzero 取 unique bins → fancy scatter-add
      避免 np.unique（O(n log n) sort）和大型 bincount（50,000-elem 臨時陣列）
    * deque 每個元素儲存已彙整的 (unique_idxs, agg_qty)，subtract 同樣 fancy indexing
    * argmax 限縮在 lazy-expanding active range [_rng_lo, _rng_hi]，避免掃 50,000 bins
    """

    def __init__(
        self,
        bin_size: float = 10.0,
        window_bars: int = 240,
        price_min: float = 1_000.0,
        price_max: float = 500_000.0,
    ) -> None:
        self.bin_size = bin_size
        self.window_bars = window_bars
        self._bin_offset = int(round(price_min / bin_size))
        self._n_bins = int(round(price_max / bin_size)) - self._bin_offset + 2
        self._hist = np.zeros(self._n_bins, dtype=np.float64)
        self._bars: deque[tuple[np.ndarray, np.ndarray]] = deque()
        self._total: float = 0.0
        # lazy-expanding active bin range（只擴不縮；4h POC 時間窗最寬幾千 bins，遠小於 50,000）
        self._rng_lo: int = self._n_bins
        self._rng_hi: int = 0

    def _to_idx(self, prices: np.ndarray) -> np.ndarray:
        return np.clip(
            np.round(prices / self.bin_size).astype(np.int64) - self._bin_offset,
            0, self._n_bins - 1,
        )

    def update(self, k: Kline, ticks: Optional[np.ndarray]) -> Optional[float]:
        if ticks is not None and len(ticks) > 0:
            raw_idxs = self._to_idx(ticks[:, 1])
            raw_qty = ticks[:, 2]
        else:
            tp = (k.high + k.low + k.close) / 3.0
            raw_idxs = self._to_idx(np.array([tp]))
            raw_qty = np.array([k.volume])

        # Mini-bincount：在 per-bar 極窄範圍（通常 ~10 bins）彙整，避免 np.unique 排序開銷
        lo = int(raw_idxs.min())
        hi = int(raw_idxs.max())
        temp = np.bincount(raw_idxs - lo, weights=raw_qty, minlength=hi - lo + 1)
        nz = np.flatnonzero(temp)
        u_idxs = nz + lo          # unique bin indices（通常 ~10 個）
        u_qty = temp[nz]

        # scatter-add（unique index → += 正確）
        self._hist[u_idxs] += u_qty
        self._total += float(u_qty.sum())
        self._bars.append((u_idxs, u_qty))

        # 擴展 active range
        if lo < self._rng_lo:
            self._rng_lo = lo
        if hi > self._rng_hi:
            self._rng_hi = hi

        # 滑出視窗
        while len(self._bars) > self.window_bars:
            old_idxs, old_qty = self._bars.popleft()
            self._hist[old_idxs] -= old_qty
            neg_mask = self._hist[old_idxs] < 0.0
            if neg_mask.any():
                self._hist[old_idxs[neg_mask]] = 0.0
            self._total -= float(old_qty.sum())

        if self._total <= 0.0:
            return None
        # argmax 限縮在 active range；range 只擴不縮，掃描長度最多等於整個回測的價格範圍
        poc_idx = int(np.argmax(self._hist[self._rng_lo:self._rng_hi + 1])) + self._rng_lo
        return (poc_idx + self._bin_offset) * self.bin_size


# ═══════════════════════════════════════════════════════════════════════════
# 5/6/7. Setup 狀態容器
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class _ReclaimSetup:
    side: str            # "long" / "short"
    band_name: str       # "lower_1sigma" / "lower_2sigma" / "upper_1sigma" / "upper_2sigma"
    band_val: float      # 被觸及當下的 band 值（固定參考）
    start_idx: int       # breach candle index


@dataclass
class _BreakoutSetup:
    side: str
    band_name: str
    band_val: float
    start_idx: int
    phase: str = "wait_retest"   # -> "wait_continuation"
    retest_high: float = 0.0
    retest_low: float = 0.0


@dataclass
class _PendingEntry:
    side: str
    entry_type: str          # long_reclaim / short_reclaim / long_breakout / short_breakout
    label: str
    stop: float
    target_poc: Optional[float]   # reclaim 用；breakout 為 None
    entry_band: Optional[float]   # breakout 用；reclaim 為 None
    entry_reason: str
    cvd_reason: str
    alpha_reason: str
    band_name: str


# ═══════════════════════════════════════════════════════════════════════════
# 主策略
# ═══════════════════════════════════════════════════════════════════════════
@register
class BTCWeeklyVWAPSigmaCVDStrategy(StrategyBase):
    name = "BTC Weekly VWAP σ + CVD + 1m Alpha v1"

    # ── 參數表（規格固定值，可調以做實驗）─────────────────────────────────
    vwap_reset_interval: str = "weekly"  # "weekly" (Sun UTC+0) or "daily" (UTC+0)
    warmup_hours: float = 8.0          # reset 後禁止進場時數
    cvd_window: int = 15               # 15m rolling CVD
    obs_window: int = 8                # reclaim / retest 觀察窗（1m bar 數）
    retest_tol_pct: float = 0.0003     # breakout retest 容差 (0.03%)
    poc_bin_size: float = 10.0         # rolling 4h POC 價格 bin（USDT）
    poc_window_bars: int = 240         # 4h = 240 根 1m
    enable_long: bool = True
    enable_short: bool = True
    enable_mean_reversion: bool = False   # 均值回歸（reclaim）交易
    enable_breakout: bool = True         # 趨勢突破（breakout）交易

    # ── 成本模型（規格值，由 engine 套用）──────────────────────────────────
    fee_rate_per_side: float = 0.00032
    slippage_bps: float = 0.2
    leverage: int = 20

    @classmethod
    def recommended_backtest_config(cls):
        """回傳符合規格成本/槓桿的 BacktestConfig。"""
        from backtest.engine import BacktestConfig
        return BacktestConfig(
            leverage=cls.leverage,
            fee_mode="自訂",
            custom_fee_rate=cls.fee_rate_per_side,
            slippage_bps=cls.slippage_bps,
        )

    # ──────────────────────────────────────────────────────────────────────
    def on_history(
        self,
        klines: List[Kline],
        tick_map: Optional[TickBarMap] = None,
    ) -> List[StrategySignal]:
        signals: List[StrategySignal] = []
        n = len(klines)
        if n < 2:
            return signals

        use_ticks = tick_map is not None and len(tick_map) > 0

        # ── Pass 1：逐根計算 expanding / rolling 特徵（只用已收盤資料）──────
        vwap_calc = WeeklyVWAPSigmaCalculator(self.warmup_hours, self.vwap_reset_interval)
        cvd_calc = CVDFilter(self.cvd_window)
        poc_calc = Rolling4hPOC(self.poc_bin_size, self.poc_window_bars)

        bands: List[WeeklyBands] = []
        cvds: List[CVDState] = []
        pocs: List[Optional[float]] = []
        for k in klines:
            ticks = tick_map.get(k.open_time) if use_ticks else None
            bands.append(vwap_calc.update(k))
            cvds.append(cvd_calc.update(k, ticks))
            pocs.append(poc_calc.update(k, ticks))

        # Store per-bar features so the snapshot system can overlay sigma bands and CVD
        self._last_bar_features: dict[int, dict] = {
            klines[i].open_time: {
                "vwap": bands[i].weekly_vwap,
                "u1":   bands[i].upper_1sigma,
                "u2":   bands[i].upper_2sigma,
                "l1":   bands[i].lower_1sigma,
                "l2":   bands[i].lower_2sigma,
                "wu":   bands[i].is_weekly_warmup,
                "cvd15": cvds[i].cvd_15m,
                "bd":   cvds[i].bullish_cvd_divergence,
                "berd": cvds[i].bearish_cvd_divergence,
                "ba":   cvds[i].bullish_cvd_acceleration,
                "bera": cvds[i].bearish_cvd_acceleration,
            }
            for i in range(n)
        }

        # ── Pass 2：事件 / 進出場 state machine ──────────────────────────────
        long_reclaim: List[_ReclaimSetup] = []
        short_reclaim: List[_ReclaimSetup] = []
        long_breakout: List[_BreakoutSetup] = []
        short_breakout: List[_BreakoutSetup] = []

        pending_entry: Optional[_PendingEntry] = None
        pending_exit: Optional[dict] = None

        in_pos = False
        self._reset_trade_state()

        for i in range(n):
            k = klines[i]
            ticks = tick_map.get(k.open_time) if use_ticks else None

            # 1) 執行上一根 close 確認的 pending exit（下一個 tick = 本根第一 tick）
            if pending_exit is not None:
                fp, ft = self._first_tick(k, ticks)
                self._emit_exit(signals, k, self._side, fp, ft,
                                pending_exit["price_ref"], pending_exit["label"],
                                pending_exit["exit_reason"])
                pending_exit = None
                in_pos = False
                self._reset_trade_state()

            # 2) 執行上一根 close 確認的 pending entry（下一個 tick）
            entry_tick_start = 0
            if pending_entry is not None and not in_pos:
                fp, ft = self._first_tick(k, ticks)
                pe = pending_entry
                pending_entry = None
                ok = self._try_open(signals, k, fp, ft, pe)
                if ok:
                    in_pos = True
                    entry_tick_start = 1   # 同根後續 tick 才開始 SL 監控
                # 不論成交與否，setup 已於確認時清空

            # 3) 持倉管理：tick-level SL + close-confirmed TP / breakout 退場
            if in_pos:
                exited, defer = self._manage_position(
                    signals, k, ticks, entry_tick_start, bands[i])
                if exited:
                    in_pos = False
                    self._reset_trade_state()
                elif defer is not None:
                    pending_exit = defer   # 下一根第一 tick 出場
                # 持倉時不偵測新 setup
                continue

            # 4) 空手：偵測 band 事件 + alpha 觀察 -> 確認則設定 pending_entry
            if pending_entry is None:
                pe = self._detect(
                    i, klines, bands, cvds, pocs,
                    long_reclaim, short_reclaim, long_breakout, short_breakout)
                if pe is not None:
                    pending_entry = pe
                    # 進場確認後清空所有觀察（單倉位）
                    long_reclaim.clear(); short_reclaim.clear()
                    long_breakout.clear(); short_breakout.clear()

        return signals

    # ──────────────────────────────────────────────────────────────────────
    # 偵測：SigmaEvent + Reclaim + BreakoutRetest（含 conflict / warmup 守門）
    # ──────────────────────────────────────────────────────────────────────
    def _detect(
        self, i, klines, bands, cvds, pocs,
        long_reclaim, short_reclaim, long_breakout, short_breakout,
    ) -> Optional[_PendingEntry]:
        k = klines[i]
        b = bands[i]
        c = cvds[i]
        candidates: List[_PendingEntry] = []

        # ── 先推進既有觀察窗（reclaim / breakout）── 收集確認的進場 ──────────
        if self.enable_long:
            if self.enable_mean_reversion:
                candidates += self._step_reclaim(i, k, b, c, pocs[i], long_reclaim, "long")
            if self.enable_breakout:
                candidates += self._step_breakout(i, k, b, c, long_breakout, "long")
        if self.enable_short:
            if self.enable_mean_reversion:
                candidates += self._step_reclaim(i, k, b, c, pocs[i], short_reclaim, "short")
            if self.enable_breakout:
                candidates += self._step_breakout(i, k, b, c, short_breakout, "short")

        # ── 註冊新的 band 事件（breach / breakout）──────────────────────────
        if self.enable_long:
            if self.enable_mean_reversion:
                self._register_reclaim_breach(i, k, b, long_reclaim, "long")
            if self.enable_breakout:
                self._register_breakout(i, k, b, long_breakout, "long")
        if self.enable_short:
            if self.enable_mean_reversion:
                self._register_reclaim_breach(i, k, b, short_reclaim, "short")
            if self.enable_breakout:
                self._register_breakout(i, k, b, short_breakout, "short")

        # ── 守門：weekly warmup ─────────────────────────────────────────────
        if b.is_weekly_warmup:
            return None
        # ── 守門：conflict（同根多於一個進場訊號 -> 不交易）──────────────────
        if len(candidates) != 1:
            return None
        return candidates[0]

    # ── Reclaim：註冊 breach ────────────────────────────────────────────────
    def _register_reclaim_breach(self, i, k, b: WeeklyBands, store, side):
        if side == "long":
            if k.low <= b.lower_2sigma:
                if not any(s.band_name == "lower_2sigma" for s in store):
                    store.append(_ReclaimSetup(side, "lower_2sigma", b.lower_2sigma, i))
            elif k.low <= b.lower_1sigma:
                if not any(s.band_name == "lower_1sigma" for s in store):
                    store.append(_ReclaimSetup(side, "lower_1sigma", b.lower_1sigma, i))
        else:
            if k.high >= b.upper_2sigma:
                if not any(s.band_name == "upper_2sigma" for s in store):
                    store.append(_ReclaimSetup(side, "upper_2sigma", b.upper_2sigma, i))
            elif k.high >= b.upper_1sigma:
                if not any(s.band_name == "upper_1sigma" for s in store):
                    store.append(_ReclaimSetup(side, "upper_1sigma", b.upper_1sigma, i))

    # ── Reclaim：推進觀察窗，回傳確認的進場 ──────────────────────────────────
    def _step_reclaim(self, i, k, b, c, poc, store, side) -> List[_PendingEntry]:
        out: List[_PendingEntry] = []
        alive: List[_ReclaimSetup] = []
        for s in store:
            if i <= s.start_idx:
                alive.append(s)
                continue
            if i - s.start_idx > self.obs_window:
                continue  # 失效
            bullish = k.close > k.open
            bearish = k.close < k.open
            if side == "long":
                ok = (k.close > s.band_val and bullish and c.bullish_cvd_divergence)
                if ok:
                    out.append(_PendingEntry(
                        side="long", entry_type="long_reclaim", label="LR",
                        stop=k.low, target_poc=poc, entry_band=None,
                        entry_reason=(f"long_reclaim: low breached {s.band_name}, close "
                                      f"reclaimed {s.band_name} within {self.obs_window} "
                                      f"candles, bullish candle confirmed"),
                        cvd_reason="bullish_cvd_divergence: price lower low without CVD lower low in 15m window",
                        alpha_reason=f"reclaim close={k.close:.2f} > band={s.band_val:.2f}",
                        band_name=s.band_name))
                    continue  # setup 消耗
            else:
                ok = (k.close < s.band_val and bearish and c.bearish_cvd_divergence)
                if ok:
                    out.append(_PendingEntry(
                        side="short", entry_type="short_reclaim", label="SR",
                        stop=k.high, target_poc=poc, entry_band=None,
                        entry_reason=(f"short_reclaim: high breached {s.band_name}, close "
                                      f"reclaimed below {s.band_name} within {self.obs_window} "
                                      f"candles, bearish candle confirmed"),
                        cvd_reason="bearish_cvd_divergence: price higher high without CVD higher high in 15m window",
                        alpha_reason=f"reclaim close={k.close:.2f} < band={s.band_val:.2f}",
                        band_name=s.band_name))
                    continue
            alive.append(s)
        store[:] = alive
        return out

    # ── Breakout：註冊突破 ──────────────────────────────────────────────────
    def _register_breakout(self, i, k, b: WeeklyBands, store, side):
        if side == "long":
            if k.close > b.upper_2sigma:
                if not any(s.band_name == "upper_2sigma" for s in store):
                    store.append(_BreakoutSetup(side, "upper_2sigma", b.upper_2sigma, i))
            elif k.close > b.upper_1sigma:
                if not any(s.band_name == "upper_1sigma" for s in store):
                    store.append(_BreakoutSetup(side, "upper_1sigma", b.upper_1sigma, i))
        else:
            if k.close < b.lower_2sigma:
                if not any(s.band_name == "lower_2sigma" for s in store):
                    store.append(_BreakoutSetup(side, "lower_2sigma", b.lower_2sigma, i))
            elif k.close < b.lower_1sigma:
                if not any(s.band_name == "lower_1sigma" for s in store):
                    store.append(_BreakoutSetup(side, "lower_1sigma", b.lower_1sigma, i))

    # ── Breakout：推進 retest -> continuation ───────────────────────────────
    def _step_breakout(self, i, k, b, c, store, side) -> List[_PendingEntry]:
        out: List[_PendingEntry] = []
        alive: List[_BreakoutSetup] = []
        for s in store:
            if i <= s.start_idx:
                alive.append(s)
                continue
            if i - s.start_idx > self.obs_window:
                continue
            if side == "long":
                tol_line = s.band_val * (1 + self.retest_tol_pct)
                if s.phase == "wait_retest":
                    if k.low <= tol_line and k.close >= s.band_val:
                        s.phase = "wait_continuation"
                        s.retest_high = k.high
                        s.retest_low = k.low
                    alive.append(s)
                    continue
                else:  # wait_continuation
                    if k.close > s.retest_high and c.bullish_cvd_acceleration:
                        out.append(_PendingEntry(
                            side="long", entry_type="long_breakout", label="LB",
                            stop=s.retest_low, target_poc=None, entry_band=s.band_val,
                            entry_reason=(f"long_breakout: close broke above {s.band_name}, "
                                          f"retest held band within tolerance, continuation confirmed"),
                            cvd_reason="bullish_cvd_acceleration: CVD_15m current > previous during upside breakout",
                            alpha_reason=f"continuation close={k.close:.2f} > retest_high={s.retest_high:.2f}",
                            band_name=s.band_name))
                        continue
                    alive.append(s)
                    continue
            else:
                tol_line = s.band_val * (1 - self.retest_tol_pct)
                if s.phase == "wait_retest":
                    if k.high >= tol_line and k.close <= s.band_val:
                        s.phase = "wait_continuation"
                        s.retest_high = k.high
                        s.retest_low = k.low
                    alive.append(s)
                    continue
                else:
                    if k.close < s.retest_low and c.bearish_cvd_acceleration:
                        out.append(_PendingEntry(
                            side="short", entry_type="short_breakout", label="SB",
                            stop=s.retest_high, target_poc=None, entry_band=s.band_val,
                            entry_reason=(f"short_breakout: close broke below {s.band_name}, "
                                          f"retest held band within tolerance, continuation confirmed"),
                            cvd_reason="bearish_cvd_acceleration: CVD_15m current < previous during downside breakout",
                            alpha_reason=f"continuation close={k.close:.2f} < retest_low={s.retest_low:.2f}",
                            band_name=s.band_name))
                        continue
                    alive.append(s)
                    continue
        store[:] = alive
        return out

    # ──────────────────────────────────────────────────────────────────────
    # 進場執行（含 POC 守門、risk 守門）
    # ──────────────────────────────────────────────────────────────────────
    def _try_open(self, signals, k, fp, ft, pe: _PendingEntry) -> bool:
        # POC 守門（僅 reclaim）
        if pe.target_poc is not None:
            if pe.side == "long" and not (pe.target_poc > fp):
                return False
            if pe.side == "short" and not (pe.target_poc < fp):
                return False
        # risk 守門
        risk = (fp - pe.stop) if pe.side == "long" else (pe.stop - fp)
        if risk <= 0:
            return False

        self._side = pe.side
        self._entry_price = fp
        self._stop_price = pe.stop
        self._entry_risk = risk
        self._take_profit = pe.target_poc
        self._entry_band_name = pe.band_name if pe.entry_band is not None else ""
        self._entry_type = pe.entry_type
        self._mae = 0.0
        self._mfe = 0.0

        meta = {
            "wick_type": pe.entry_type,     # -> engine 依此分組（entry_type breakdown）
            "entry_reason": pe.entry_reason,
            "cvd_reason": pe.cvd_reason,
            "alpha_reason": pe.alpha_reason,
            "entry_band": pe.band_name,
            "entry_risk": risk,
            "sl_reason": f"stop_loss @ {pe.stop:.2f}",
            "poc": pe.target_poc,           # rolling 4h POC（reclaim 用 TP 目標；breakout 為 None）
            "tp": pe.target_poc,            # 明確 TP 價位，供快照 UI 顯示
        }
        sig_type = "long_entry" if pe.side == "long" else "short_entry"
        signals.append(StrategySignal(
            open_time=k.open_time, price=fp, signal_type=sig_type,
            label=pe.label, stop_price=pe.stop, fill_price=fp, fill_time=ft,
            meta=meta,
        ))
        return True

    # ──────────────────────────────────────────────────────────────────────
    # 8. ExitEngine：tick-level SL + close-confirmed TP / breakout 退場
    #    回傳 (exited_now, deferred_exit_dict)
    # ──────────────────────────────────────────────────────────────────────
    def _manage_position(self, signals, k, ticks, tick_start, current_bands: WeeklyBands) -> Tuple[bool, Optional[dict]]:
        side = self._side
        # ── tick-level SL ───────────────────────────────────────────────────
        if ticks is not None and len(ticks) > 0:
            arr = ticks[tick_start:] if tick_start else ticks
            for t in arr:
                price = float(t[1])
                self._update_excursion(price, price, side)
                if side == "long" and price <= self._stop_price:
                    self._emit_exit(signals, k, side, price, int(t[0]),
                                    self._stop_price, "SL", "stop_loss")
                    return True, None
                if side == "short" and price >= self._stop_price:
                    self._emit_exit(signals, k, side, price, int(t[0]),
                                    self._stop_price, "SL", "stop_loss")
                    return True, None
        else:
            # 無 tick：bar 近似 SL
            self._update_excursion(k.high, k.low, side)
            if side == "long" and k.low <= self._stop_price:
                self._emit_exit(signals, k, side, self._stop_price, k.close_time,
                                self._stop_price, "SL", "stop_loss", bar_approx=True)
                return True, None
            if side == "short" and k.high >= self._stop_price:
                self._emit_exit(signals, k, side, self._stop_price, k.close_time,
                                self._stop_price, "SL", "stop_loss", bar_approx=True)
                return True, None

        # ── close-confirmed 退場（下一根第一 tick 成交）──────────────────────
        if self._take_profit is not None:
            # reclaim trade -> TP = rolling_4h_poc（進場時固定）
            if side == "long" and k.close >= self._take_profit:
                return False, {"label": "TP", "price_ref": self._take_profit,
                               "exit_reason": "take_profit_4h_poc"}
            if side == "short" and k.close <= self._take_profit:
                return False, {"label": "TP", "price_ref": self._take_profit,
                               "exit_reason": "take_profit_4h_poc"}
        if self._entry_band_name:
            # breakout trade -> 當前 sigma band reentry 退場（使用每根 bar 更新的動態值）
            live_band = getattr(current_bands, self._entry_band_name, None)
            if live_band is not None:
                if side == "long" and k.close < live_band:
                    return False, {"label": "BR", "price_ref": k.close,
                                   "exit_reason": "breakout_band_reentry"}
                if side == "short" and k.close > live_band:
                    return False, {"label": "BR", "price_ref": k.close,
                                   "exit_reason": "breakout_band_reentry"}
        return False, None

    # ──────────────────────────────────────────────────────────────────────
    # 工具
    # ──────────────────────────────────────────────────────────────────────
    def _emit_exit(self, signals, k, side, fill_price, fill_time, price_ref,
                   label, exit_reason, bar_approx: bool = False):
        risk = self._entry_risk
        mae = self._mae * risk
        mfe = self._mfe * risk
        meta = {
            "MAE": self._mae, "MFE": self._mfe,
            "mae": mae, "mfe": mfe,
            "mae_r": self._mae, "mfe_r": self._mfe,
            "entry_risk": risk,
            "exit_reason": exit_reason,
            "tp_reason": (exit_reason if label == "TP" else ""),
        }
        if bar_approx:
            meta["sl_exec"] = "bar_approx"
        sig_type = "long_exit" if side == "long" else "short_exit"
        signals.append(StrategySignal(
            open_time=k.open_time, price=price_ref, signal_type=sig_type,
            label=label, fill_price=fill_price, fill_time=fill_time, meta=meta,
        ))

    def _update_excursion(self, high: float, low: float, side: str) -> None:
        if self._entry_risk <= 0 or self._entry_price <= 0:
            return
        if side == "long":
            adverse = max(0.0, self._entry_price - low) / self._entry_risk
            favorable = max(0.0, high - self._entry_price) / self._entry_risk
        else:
            adverse = max(0.0, high - self._entry_price) / self._entry_risk
            favorable = max(0.0, self._entry_price - low) / self._entry_risk
        self._mae = max(self._mae, adverse)
        self._mfe = max(self._mfe, favorable)

    @staticmethod
    def _first_tick(k: Kline, ticks: Optional[np.ndarray]) -> Tuple[float, int]:
        if ticks is not None and len(ticks) > 0:
            return float(ticks[0][1]), int(ticks[0][0])
        return k.open, k.open_time

    def _reset_trade_state(self):
        self._side = ""
        self._entry_price = 0.0
        self._stop_price = 0.0
        self._entry_risk = 0.0
        self._take_profit: Optional[float] = None
        self._entry_band_name: str = ""
        self._entry_type = ""
        self._mae = 0.0
        self._mfe = 0.0
