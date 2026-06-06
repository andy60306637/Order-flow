"""
BTCUSDT 30m-Signal + 4h-CVD Weekly VWAP Sigma Strategy v1（週期調整版）
=======================================================================

本檔案是 `btc_weekly_vwap_sigma_cvd.py` 的「週期調整」複製版。原版在 **1m** 訊號
時框 + **15m** rolling CVD 上運作；本版改為：

* **入場訊號時框 = 30m**：VWAP σ 帶 breach / reclaim / breakout-retest /
  收盤確認，全部在 **30m bar** 上判斷。
* **CVD 確認 = 4h**：rolling CVD divergence / acceleration 使用 **4h 窗
  = 8 根 30m bar**（`cvd_window=8`）。
* **rolling POC = 4h**：8 根 30m bar（`poc_window_bars=8`）。

重採樣設計（重要）
------------------
系統 `on_history(klines, tick_map)` 餵入的仍是 **1m** Kline 與 1m tick_map
（與原版相同的資料來源）。本策略在 `on_history` 內部：

1. 將 1m klines 依 30m 邊界聚合成 **30m bar**（OHLCV / taker_buy 累加）。
2. 將每個 30m bar 內所有 1m bar 的 ticks 依序串接成該 30m bar 的 tick 陣列。
3. 在 30m bar + 串接 tick 上跑與原版**完全相同**的事件 / 進出場 state machine。

如此可直接沿用你現有的 1m + tick 回測資料，無需另外準備 30m kline cache。

執行語意（Anti-Lookahead，與原版一致，僅 bar 單位改為 30m）
-----------------------------------------------------------
* 所有訊號僅在 **30m candle close confirmed** 後成立。
* entry / TP / breakout band-reentry exit 在「下一根 **30m** bar 的第一個 tick」
  成交（= 下一根 30m 的第一個 1m tick）。
* SL 為 **tick-level**：持倉期間逐 1m tick 比對（涵蓋整根 30m bar 內所有 ticks），
  觸發即以該 tick price 成交；該 30m bar 無 tick 時退化為 bar high/low 近似。
* weekly VWAP / sigma / CVD(4h) / rolling 4h POC 全部 expanding / rolling，
  僅使用「當下已收盤」的 30m 資料，無未來資料。

成本模型沿用規格：fee 0.032%/side、slippage 0.2 bps、leverage 20。
引擎以 `fill_price` / `fill_time` 計算 PnL，故重採樣不影響損益正確性；
StrategySignal.open_time 對齊到 1m 邊界以維持快照 UI 標記正確。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

from core.data_types import Kline
from strategies.base import StrategyBase, StrategySignal, TickBarMap
from strategies import register

# ── 常數 ──────────────────────────────────────────────────────────────────────
MS_MIN = 60_000
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
        return (epoch_day - _SUNDAY_EPOCH_DAY) // 7

    def get_period_start_ms(self, period_id: int) -> int:
        if self.reset_interval == "daily":
            return period_id * MS_DAY
        return (_SUNDAY_EPOCH_DAY + period_id * 7) * MS_DAY

    def update(self, k: Kline) -> WeeklyBands:
        pid = self.get_period_id(k.open_time)
        if pid != self._reset_id:
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
# 3. CVDFilter（窗大小由策略設定；本版以 8 根 30m bar = 4h）
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
    """tick-level CVD 與 rolling divergence / acceleration 偵測。

    * 每根 bar 的 delta = Σ tick_delta（買 +qty / 賣 -qty）。
    * cum_cvd 為連續累積 delta；cvd_window 為 rolling 窗（本版 = 8 根 30m = 4h）。
    * divergence 使用窗內（含當下）price / cum_cvd 極值：
        bullish: price 創 window low 但 cum_cvd 未創 window low
        bearish: price 創 window high 但 cum_cvd 未創 window high
    * acceleration 僅比較 rolling-CVD 方向。

    注：CVDState 欄位名沿用原版（cvd_15m 等），語意上現在代表「4h CVD」。
    """

    def __init__(self, window: int = 8) -> None:
        self.window = window
        self._cum = 0.0
        self._prev_cvd15: Optional[float] = None
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
        return 2.0 * k.taker_buy_volume - k.volume, "bar_proxy"

    def update(self, k: Kline, ticks: Optional[np.ndarray]) -> CVDState:
        delta, src = self.bar_delta(k, ticks)
        self._cum += delta
        self._cum_window.append(self._cum)
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
# 4. Rolling4hPOC（4h = window_bars 根 bar；本版以 8 根 30m bar）
# ═══════════════════════════════════════════════════════════════════════════
class Rolling4hPOC:
    """rolling 4h 成交量分佈之 POC（最大成交量價位 bin）。

    優先以 tick 累積各價格 bin 的 qty；無 tick 時把整根 bar volume 記到 typical price bin。
    僅用過去以前已完成資料（每根收盤後加入）。
    """

    def __init__(
        self,
        bin_size: float = 10.0,
        window_bars: int = 8,
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

        lo = int(raw_idxs.min())
        hi = int(raw_idxs.max())
        temp = np.bincount(raw_idxs - lo, weights=raw_qty, minlength=hi - lo + 1)
        nz = np.flatnonzero(temp)
        u_idxs = nz + lo
        u_qty = temp[nz]

        self._hist[u_idxs] += u_qty
        self._total += float(u_qty.sum())
        self._bars.append((u_idxs, u_qty))

        if lo < self._rng_lo:
            self._rng_lo = lo
        if hi > self._rng_hi:
            self._rng_hi = hi

        while len(self._bars) > self.window_bars:
            old_idxs, old_qty = self._bars.popleft()
            self._hist[old_idxs] -= old_qty
            neg_mask = self._hist[old_idxs] < 0.0
            if neg_mask.any():
                self._hist[old_idxs[neg_mask]] = 0.0
            self._total -= float(old_qty.sum())

        if self._total <= 0.0:
            return None
        poc_idx = int(np.argmax(self._hist[self._rng_lo:self._rng_hi + 1])) + self._rng_lo
        return (poc_idx + self._bin_offset) * self.bin_size


# ═══════════════════════════════════════════════════════════════════════════
# 5/6/7. Setup 狀態容器
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class _ReclaimSetup:
    side: str
    band_name: str
    band_val: float
    start_idx: int


@dataclass
class _BreakoutSetup:
    side: str
    band_name: str
    band_val: float
    start_idx: int
    phase: str = "wait_retest"
    retest_high: float = 0.0
    retest_low: float = 0.0


@dataclass
class _PendingEntry:
    side: str
    entry_type: str
    label: str
    stop: float
    target_poc: Optional[float]
    entry_band: Optional[float]
    entry_reason: str
    cvd_reason: str
    alpha_reason: str
    band_name: str


# ═══════════════════════════════════════════════════════════════════════════
# 主策略
# ═══════════════════════════════════════════════════════════════════════════
@register
class BTC30mSignal4hCVDStrategy(StrategyBase):
    name = "BTC 30m Signal + 4h CVD VWAP σ v1"

    # ── 週期調整參數 ──────────────────────────────────────────────────────────
    signal_interval_min: int = 30       # 入場訊號重採樣時框（分鐘）
    vwap_reset_interval: str = "weekly"  # "weekly" (Sun UTC+0) or "daily" (UTC+0)
    warmup_hours: float = 8.0           # reset 後禁止進場時數
    cvd_window: int = 8                 # 4h CVD = 8 根 30m bar
    obs_window: int = 8                 # reclaim / retest 觀察窗（30m bar 數；8 = 4h）
    retest_tol_pct: float = 0.0003      # breakout retest 容差 (0.03%)
    poc_bin_size: float = 10.0          # rolling 4h POC 價格 bin（USDT）
    poc_window_bars: int = 8            # 4h = 8 根 30m bar
    enable_long: bool = True
    enable_short: bool = True
    enable_mean_reversion: bool = False  # 均值回歸（reclaim）交易
    enable_breakout: bool = True         # 趨勢突破（breakout）交易

    # ── 成本模型（規格值，由 engine 套用）──────────────────────────────────
    fee_rate_per_side: float = 0.00032
    slippage_bps: float = 0.2
    leverage: int = 20

    @classmethod
    def recommended_backtest_config(cls):
        from backtest.engine import BacktestConfig
        return BacktestConfig(
            leverage=cls.leverage,
            fee_mode="自訂",
            custom_fee_rate=cls.fee_rate_per_side,
            slippage_bps=cls.slippage_bps,
        )

    # ──────────────────────────────────────────────────────────────────────
    # 1m -> 30m 重採樣
    # ──────────────────────────────────────────────────────────────────────
    def _resample(
        self,
        klines: List[Kline],
        tick_map: Optional[TickBarMap],
        use_ticks: bool,
    ) -> Tuple[List[Kline], Dict[int, List[int]], Dict[int, int]]:
        """將 1m klines / tick_map 聚合為 signal_interval_min bar。

        回傳 (klines_hi, bucket_members, one_to_bucket)
          * bucket_members: 30m bucket open_time -> 該 bucket 內「有 tick 的 1m
            open_time」清單（依時間遞增）。**tick 陣列本身不在此複製**；需要時由
            `_bucket_ticks` 以參照即時串接單一 bucket、用完即釋放，避免一次性
            物化整份 30m tick 副本（5 年區間的記憶體殺手）。
          * one_to_bucket: 1m open_time -> 30m bucket open_time（供 bar_features 廣播）
        """
        bucket_ms = self.signal_interval_min * MS_MIN
        accs: Dict[int, dict] = {}
        order: List[int] = []
        bucket_members: Dict[int, List[int]] = {}
        one_to_bucket: Dict[int, int] = {}

        for k in klines:
            b0 = (k.open_time // bucket_ms) * bucket_ms
            one_to_bucket[k.open_time] = b0
            acc = accs.get(b0)
            if acc is None:
                accs[b0] = {
                    "symbol": k.symbol, "open_time": b0, "close_time": k.close_time,
                    "open": k.open, "high": k.high, "low": k.low, "close": k.close,
                    "volume": k.volume, "taker": k.taker_buy_volume,
                    "is_closed": k.is_closed,
                }
                order.append(b0)
                bucket_members[b0] = []
            else:
                acc["high"] = max(acc["high"], k.high)
                acc["low"] = min(acc["low"], k.low)
                acc["close"] = k.close
                acc["close_time"] = k.close_time
                acc["volume"] += k.volume
                acc["taker"] += k.taker_buy_volume
                acc["is_closed"] = k.is_closed
            if use_ticks:
                t = tick_map.get(k.open_time) if tick_map is not None else None
                if t is not None and len(t) > 0:
                    # 只記 1m open_time（參照來源），不在此複製 tick 陣列
                    bucket_members[b0].append(k.open_time)

        klines_hi: List[Kline] = []
        interval = f"{self.signal_interval_min}m"
        for b0 in order:
            a = accs[b0]
            klines_hi.append(Kline(
                symbol=a["symbol"], interval=interval,
                open_time=a["open_time"], close_time=a["close_time"],
                open=a["open"], high=a["high"], low=a["low"], close=a["close"],
                volume=a["volume"], taker_buy_volume=a["taker"],
                is_closed=a["is_closed"],
            ))

        return klines_hi, bucket_members, one_to_bucket

    @staticmethod
    def _bucket_ticks(
        bucket_open_time: int,
        tick_map: TickBarMap,
        bucket_members: Dict[int, List[int]],
    ) -> Optional[np.ndarray]:
        """lazy 串接某 30m bucket 內的 1m tick 陣列（以參照，用完即釋放）。

        * 單一 member：直接回傳原 1m tick 陣列的**參照**（零複製）。
        * 多 member：僅針對這一個 bucket `np.vstack` 一次（約 30 分鐘 ticks），
          回傳後即可被 GC 回收；任一時刻最多只有一個 bucket 的副本存活，
          峰值記憶體 ≈ 原始 tick_map（1×）+ 單 bucket，而非 2× 整份。

        結果與舊版預建 `tick_map_hi[b0] = vstack(lst)` 逐位元一致
        （member 依時間遞增，串接順序相同）。
        """
        members = bucket_members.get(bucket_open_time)
        if not members:
            return None
        if len(members) == 1:
            return tick_map[members[0]]
        return np.vstack([tick_map[ot] for ot in members])

    @staticmethod
    def _align1m(ms: int) -> int:
        """對齊到 1m 邊界（供 StrategySignal.open_time，使快照標記落在正確 1m bar）。"""
        return (int(ms) // MS_MIN) * MS_MIN

    # ──────────────────────────────────────────────────────────────────────
    def on_history(
        self,
        klines: List[Kline],
        tick_map: Optional[TickBarMap] = None,
    ) -> List[StrategySignal]:
        signals: List[StrategySignal] = []
        if len(klines) < 2:
            return signals

        use_ticks_in = tick_map is not None and len(tick_map) > 0

        # ── 0：1m -> 30m 重採樣（tick 不複製，僅記錄 bucket 成員）─────────────
        hi_klines, bucket_members, one_to_bucket = self._resample(klines, tick_map, use_ticks_in)
        n = len(hi_klines)
        if n < 2:
            return signals
        use_ticks = use_ticks_in and any(bucket_members.values())

        # ── Pass 1：逐根（30m）計算 expanding / rolling 特徵 ──────────────────
        vwap_calc = WeeklyVWAPSigmaCalculator(self.warmup_hours, self.vwap_reset_interval)
        cvd_calc = CVDFilter(self.cvd_window)
        poc_calc = Rolling4hPOC(self.poc_bin_size, self.poc_window_bars)

        bands: List[WeeklyBands] = []
        cvds: List[CVDState] = []
        pocs: List[Optional[float]] = []
        for k in hi_klines:
            ticks = self._bucket_ticks(k.open_time, tick_map, bucket_members) if use_ticks else None
            bands.append(vwap_calc.update(k))
            cvds.append(cvd_calc.update(k, ticks))
            pocs.append(poc_calc.update(k, ticks))

        # 30m bar 特徵 → 廣播到該 bar 涵蓋的每個 1m open_time，使快照 overlay 可解析
        feat30: dict[int, dict] = {
            hi_klines[i].open_time: {
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
        self._last_bar_features: dict[int, dict] = {
            one_m: feat30[bucket]
            for one_m, bucket in one_to_bucket.items()
            if bucket in feat30
        }

        # ── Pass 2：事件 / 進出場 state machine（在 30m bar 上）───────────────
        long_reclaim: List[_ReclaimSetup] = []
        short_reclaim: List[_ReclaimSetup] = []
        long_breakout: List[_BreakoutSetup] = []
        short_breakout: List[_BreakoutSetup] = []

        pending_entry: Optional[_PendingEntry] = None
        pending_exit: Optional[dict] = None

        in_pos = False
        self._reset_trade_state()

        for i in range(n):
            k = hi_klines[i]
            ticks = self._bucket_ticks(k.open_time, tick_map, bucket_members) if use_ticks else None

            if pending_exit is not None:
                fp, ft = self._first_tick(k, ticks)
                self._emit_exit(signals, k, self._side, fp, ft,
                                pending_exit["price_ref"], pending_exit["label"],
                                pending_exit["exit_reason"])
                pending_exit = None
                in_pos = False
                self._reset_trade_state()

            entry_tick_start = 0
            if pending_entry is not None and not in_pos:
                fp, ft = self._first_tick(k, ticks)
                pe = pending_entry
                pending_entry = None
                ok = self._try_open(signals, k, fp, ft, pe)
                if ok:
                    in_pos = True
                    entry_tick_start = 1

            if in_pos:
                exited, defer = self._manage_position(
                    signals, k, ticks, entry_tick_start, bands[i])
                if exited:
                    in_pos = False
                    self._reset_trade_state()
                elif defer is not None:
                    pending_exit = defer
                continue

            if pending_entry is None:
                pe = self._detect(
                    i, hi_klines, bands, cvds, pocs,
                    long_reclaim, short_reclaim, long_breakout, short_breakout)
                if pe is not None:
                    pending_entry = pe
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

        if b.is_weekly_warmup:
            return None
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
                continue
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
                                      f"30m candles, bullish candle confirmed"),
                        cvd_reason="bullish_cvd_divergence(4h): price lower low without CVD lower low in 4h window",
                        alpha_reason=f"reclaim close={k.close:.2f} > band={s.band_val:.2f}",
                        band_name=s.band_name))
                    continue
            else:
                ok = (k.close < s.band_val and bearish and c.bearish_cvd_divergence)
                if ok:
                    out.append(_PendingEntry(
                        side="short", entry_type="short_reclaim", label="SR",
                        stop=k.high, target_poc=poc, entry_band=None,
                        entry_reason=(f"short_reclaim: high breached {s.band_name}, close "
                                      f"reclaimed below {s.band_name} within {self.obs_window} "
                                      f"30m candles, bearish candle confirmed"),
                        cvd_reason="bearish_cvd_divergence(4h): price higher high without CVD higher high in 4h window",
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
                            cvd_reason="bullish_cvd_acceleration(4h): CVD_4h current > previous during upside breakout",
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
                            cvd_reason="bearish_cvd_acceleration(4h): CVD_4h current < previous during downside breakout",
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
        if pe.target_poc is not None:
            if pe.side == "long" and not (pe.target_poc > fp):
                return False
            if pe.side == "short" and not (pe.target_poc < fp):
                return False
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
            "wick_type": pe.entry_type,
            "entry_reason": pe.entry_reason,
            "cvd_reason": pe.cvd_reason,
            "alpha_reason": pe.alpha_reason,
            "entry_band": pe.band_name,
            "entry_risk": risk,
            "sl_reason": f"stop_loss @ {pe.stop:.2f}",
            "poc": pe.target_poc,
            "tp": pe.target_poc,
            "signal_tf": f"{self.signal_interval_min}m",
        }
        sig_type = "long_entry" if pe.side == "long" else "short_entry"
        signals.append(StrategySignal(
            open_time=self._align1m(ft), price=fp, signal_type=sig_type,
            label=pe.label, stop_price=pe.stop, fill_price=fp, fill_time=ft,
            meta=meta,
        ))
        return True

    # ──────────────────────────────────────────────────────────────────────
    # ExitEngine：tick-level SL + close-confirmed TP / breakout 退場
    # ──────────────────────────────────────────────────────────────────────
    def _manage_position(self, signals, k, ticks, tick_start, current_bands: WeeklyBands) -> Tuple[bool, Optional[dict]]:
        side = self._side
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
            self._update_excursion(k.high, k.low, side)
            if side == "long" and k.low <= self._stop_price:
                self._emit_exit(signals, k, side, self._stop_price, k.close_time,
                                self._stop_price, "SL", "stop_loss", bar_approx=True)
                return True, None
            if side == "short" and k.high >= self._stop_price:
                self._emit_exit(signals, k, side, self._stop_price, k.close_time,
                                self._stop_price, "SL", "stop_loss", bar_approx=True)
                return True, None

        if self._take_profit is not None:
            if side == "long" and k.close >= self._take_profit:
                return False, {"label": "TP", "price_ref": self._take_profit,
                               "exit_reason": "take_profit_4h_poc"}
            if side == "short" and k.close <= self._take_profit:
                return False, {"label": "TP", "price_ref": self._take_profit,
                               "exit_reason": "take_profit_4h_poc"}
        if self._entry_band_name:
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
            open_time=self._align1m(fill_time), price=price_ref, signal_type=sig_type,
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
