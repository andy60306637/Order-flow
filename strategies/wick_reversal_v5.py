"""
Wick Reversal v5

Long + short, tick-first, price-regime-aware variant.
Extends v4 with per-price-regime parameter switching (R0/R1/R2).

Entry logic identical to v4:
  Long  k0: body in upper half, lower-wick > body, lower-wick zone delta_eff <= 0
  Short k0: body in lower half, upper-wick > body, upper-wick zone delta_eff >= 0
  - entry triggers inside zoom window (long_zoom_bars / short_zoom_bars) after k0
  - zoom window invalidated when guardian line is breached:
      long:  k.low  < k0_body_low
      short: k.high > k0_body_high
  - stop: long  → k0.low  - max(entry*pct_floor, lower_wick*wick_mult) capped at entry*pct_cap
          short → k0.high + max(entry*pct_floor, upper_wick*wick_mult) capped at entry*pct_cap

Regime switching (new in v5):
  Price breaks: R0 < regime_price_break_0 <= R1 < regime_price_break_1 <= R2
  Each regime has its own sl_pct_floor/wick_mult/pct_cap, k0_vol_gate, rr_wick_a/b/c,
  and min_fee_cover_ratio. Dispatched via _rp(name, price).

  Calibrated defaults (optimizer, 3-year tick shards Apr 2023–Apr 2026):
    R0 (<50k):   long_k0_vol_gate=1200, long_rr=3.0/2.0/1.0, short_rr_b=1.0
    R1 (50k-85k):long_k0_vol_gate=1200, long_rr=2.5/2.5/2.0, short fee_cover=2.0
    R2 (>85k):   long_k0_vol_gate=800,  long_rr=3.0/1.5/1.0, short fee_cover=2.0

  Backtest results (regime mode):
    Y1 (2023-04~2024-04): trades=376  WR=37.5%  PF=0.833  DD=76.7%
    Y2 (2024-04~2025-04): trades=210  WR=38.6%  PF=1.081  DD=28.5%
    Y3 (2025-04~2026-04): trades=125  WR=52.8%  PF=1.510  DD=17.1%
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np

from core.data_types import Kline
from strategies.base import StrategyBase, StrategySignal, TickBarMap
from strategies import register


def _kline_delta(k: Kline) -> float:
    return 2.0 * k.taker_buy_volume - k.volume


def _kline_delta_eff(k: Kline) -> float:
    if k.volume == 0:
        return 0.0
    return _kline_delta(k) / k.volume


@register
class WickReversalV5Strategy(StrategyBase):
    name = "Wick Reversal 1m v5"
    allow_bar_fallback_in_tick_mode: bool = True

    # ── 做多參數 ──────────────────────────────────────────────────────────────
    enable_long: bool = True                        # 啟用做多
    long_zoom_bars: int = 1                         # k0 後允許進場的最大觀察根數
    long_sl_pct_floor: float = 0.0003               # 停損距離最小值（入場價 0.03%）
    long_sl_wick_mult: float = 0.2                  # 下影線乘數（k0.low 下方緩衝）
    long_sl_pct_cap: float = 0.003                  # 停損距離上限（入場價 0.3%）
    long_rr_ratio: float = 2                        # 盈虧比
    long_td_consec_bars: int = 3                    # 連續反向 delta 才觸發 TD
    long_k0_vol_gate: float = 800.0                 # k0 最低成交量門檻
    long_delta_eff_threshold: float = 0.8           # 進場 delta_eff 門檻
    long_vol_sma_period: int = 20                   # 成交量 SMA 窗期；0=不過濾
    long_vol_sma_mult: float = 1.0                  # 成交量門標倍率
    lower_wick_absorption_delta_eff_max: float = 0.0
    lower_wick_absorption_min_vol_ratio: float = 0.15
    lower_wick_absorption_bar_delta_max: float = 0.0
    # ── 做多 cost filter / dynamic RR ─────────────────────────────────────
    long_min_fee_cover_ratio: float = 1.2           # 最低費用覆蓋倍率
    long_body_floor_pct: float = 0.00001            # body floor 百分比
    long_wick_type_a_threshold: float = 4.0         # wick A 級門檻
    long_wick_type_b_threshold: float = 3.0         # wick B 級門檻
    long_rr_wick_a: float = 3.0                     # A 級 RR
    long_rr_wick_b: float = 1.5                     # B 級 RR
    long_rr_wick_c: float = 1.0                     # C 級 RR
    # ── 做空參數（鏡像）──────────────────────────────────────────────────────
    enable_short: bool = True                       # 啟用做空
    short_zoom_bars: int = 1                        # k0 後允許進場的最大觀察根數
    short_sl_pct_floor: float = 0.001              # 停損距離最小值（入場價 0.1%）
    short_sl_wick_mult: float = 0.2                # 上影線乘數（k0.high 上方緩衝）
    short_sl_pct_cap: float = 0.003                # 停損距離上限（入場價 0.3%）
    short_rr_ratio: float = 1.0                    # 盈虧比
    short_td_consec_bars: int = 2                  # 連續反向 delta 才觸發 TD
    short_k0_vol_gate: float = 300.0               # k0 最低成交量門檻
    short_delta_eff_threshold: float = 0.8         # 進場 delta_eff 門檻（負向）
    short_vol_sma_period: int = 20                 # 成交量 SMA 窗期；0=不過濾
    short_vol_sma_mult: float = 1.6                # 成交量門標倍率
    upper_wick_absorption_delta_eff_min: float = 0.0
    upper_wick_absorption_min_vol_ratio: float = 0.15
    upper_wick_absorption_bar_delta_min: float = 0.0
    # ── 做空 cost filter / dynamic RR ─────────────────────────────────────
    short_min_fee_cover_ratio: float = 2.0         # 最低費用覆蓋倍率
    short_body_floor_pct: float = 0.00001          # body floor 百分比
    short_wick_type_a_threshold: float = 4.0       # wick A 級門檻
    short_wick_type_b_threshold: float = 3.0       # wick B 級門檻
    enable_short_wick_a: bool = True
    enable_short_wick_b: bool = True
    enable_short_wick_c: bool = False
    short_a_min_upper_wick_pct: float = 0.0
    short_rr_wick_a: float = 4.5                   # A 級 RR
    short_rr_wick_b: float = 2.5                   # B 級 RR
    short_rr_wick_c: float = 2.0                   # C 級 RR
    # ── S4B 專屬 filter ───────────────────────────────────────────────────
    short_b_min_upper_wick_pct: float = 0.0
    short_b_min_k0_vol: float = 0.0
    short_b_min_runup_pct: float = 0.0
    short_b_runup_lookback: int = 3
    # ── cost helper 參數 ──────────────────────────────────────────────────
    taker_fee_rate: float = 0.00032
    slippage_rate: float = 0.00002
    # ── Regime 切換 ───────────────────────────────────────────────────────
    # 價格區間：R0 < break_0 <= R1 < break_1 <= R2
    enable_regime_mode: bool = True
    regime_price_break_0: float = 50000.0
    regime_price_break_1: float = 85000.0
    # R0 long（低價區間 <50k，optimizer calibrated）
    r0_long_sl_pct_floor: float = 0.001
    r0_long_sl_wick_mult: float = 0.2
    r0_long_sl_pct_cap: float = 0.002
    r0_long_k0_vol_gate: float = 1200.0
    r0_long_rr_wick_a: float = 3.0
    r0_long_rr_wick_b: float = 2.0
    r0_long_rr_wick_c: float = 1.0
    r0_long_min_fee_cover_ratio: float = 1.2
    # R1 long（中價區間 50k-85k，optimizer calibrated）
    r1_long_sl_pct_floor: float = 0.001
    r1_long_sl_wick_mult: float = 0.2
    r1_long_sl_pct_cap: float = 0.003
    r1_long_k0_vol_gate: float = 1200.0
    r1_long_rr_wick_a: float = 2.5
    r1_long_rr_wick_b: float = 2.5
    r1_long_rr_wick_c: float = 2.0
    r1_long_min_fee_cover_ratio: float = 1.2
    # R2 long（高價區間 >85k，optimizer calibrated）
    r2_long_sl_pct_floor: float = 0.0003
    r2_long_sl_wick_mult: float = 0.2
    r2_long_sl_pct_cap: float = 0.003
    r2_long_k0_vol_gate: float = 800.0
    r2_long_rr_wick_a: float = 3.0
    r2_long_rr_wick_b: float = 1.5
    r2_long_rr_wick_c: float = 1.0
    r2_long_min_fee_cover_ratio: float = 1.2
    # R0 short（低價區間 <50k，optimizer calibrated）
    r0_short_sl_pct_floor: float = 0.001
    r0_short_sl_wick_mult: float = 0.2
    r0_short_sl_pct_cap: float = 0.003
    r0_short_k0_vol_gate: float = 500.0
    r0_short_rr_wick_a: float = 4.5
    r0_short_rr_wick_b: float = 1.0
    r0_short_rr_wick_c: float = 2.0
    r0_short_min_fee_cover_ratio: float = 1.2
    # R1 short（中價區間 50k-85k，optimizer calibrated）
    r1_short_sl_pct_floor: float = 0.0008
    r1_short_sl_wick_mult: float = 0.15
    r1_short_sl_pct_cap: float = 0.003
    r1_short_k0_vol_gate: float = 300.0
    r1_short_rr_wick_a: float = 4.5
    r1_short_rr_wick_b: float = 2.5
    r1_short_rr_wick_c: float = 2.0
    r1_short_min_fee_cover_ratio: float = 2.0
    # R2 short（高價區間 >85k，optimizer calibrated）
    r2_short_sl_pct_floor: float = 0.001
    r2_short_sl_wick_mult: float = 0.2
    r2_short_sl_pct_cap: float = 0.003
    r2_short_k0_vol_gate: float = 500.0
    r2_short_rr_wick_a: float = 4.5
    r2_short_rr_wick_b: float = 2.5
    r2_short_rr_wick_c: float = 2.0
    r2_short_min_fee_cover_ratio: float = 2.0

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

        long_k0:  Optional[Kline] = None
        long_k0_idx  = -1
        short_k0: Optional[Kline] = None
        short_k0_idx = -1

        in_position = False
        side = ""
        entry_price = 0.0
        stop_price  = 0.0
        target_price = 0.0
        trailing = False
        td_consec = 0

        self._trailing  = False
        self._td_consec = 0
        self._stop_price = 0.0
        self._fallback_bar_count = 0
        self.k0_records: list = []

        for i, k in enumerate(klines):
            # ── Step 1：持倉管理 ────────────────────────────────────────────
            if in_position:
                exited = False

                if side == "long":
                    if use_ticks:
                        self._trailing  = trailing
                        self._td_consec = td_consec
                        self._stop_price = stop_price
                        exited = self._tick_exit_long(k, tick_map, signals, target_price)
                        if not exited:
                            trailing  = self._trailing
                            td_consec = self._td_consec
                            stop_price = self._stop_price
                    else:
                        exited, trailing, td_consec, stop_price = self._bar_exit_long(
                            k, signals, stop_price, target_price, trailing, td_consec,
                        )
                else:
                    if use_ticks:
                        self._trailing  = trailing
                        self._td_consec = td_consec
                        self._stop_price = stop_price
                        exited = self._tick_exit_short(k, tick_map, signals, target_price)
                        if not exited:
                            trailing  = self._trailing
                            td_consec = self._td_consec
                            stop_price = self._stop_price
                    else:
                        exited, trailing, td_consec, stop_price = self._bar_exit_short(
                            k, signals, stop_price, target_price, trailing, td_consec,
                        )

                if exited:
                    in_position = False
                    side = ""
                    trailing  = False
                    td_consec = 0
                    long_k0  = None
                    short_k0 = None
                else:
                    continue

            # ── Step 2a：做多 k0 zoom 進場判定 ─────────────────────────────
            if long_k0 is not None and i > long_k0_idx:
                bars_after = i - long_k0_idx
                if bars_after > self.long_zoom_bars:
                    long_k0 = None
                elif k.low < min(long_k0.open, long_k0.close):
                    long_k0 = None
                else:
                    if use_ticks:
                        entered, entry_price, stop_price, target_price = self._tick_entry(
                            k, i, klines, tick_map, signals, long_k0,
                        )
                    else:
                        entered, entry_price, stop_price, target_price = self._bar_entry(
                            k, i, klines, signals, long_k0,
                        )
                    if entered:
                        in_position = True
                        side = "long"
                        trailing  = False
                        td_consec = 0
                        long_k0  = None
                        short_k0 = None
                        continue

            # ── Step 2b：做空 k0 zoom 進場判定 ─────────────────────────────
            if short_k0 is not None and i > short_k0_idx:
                bars_after = i - short_k0_idx
                if bars_after > self.short_zoom_bars:
                    short_k0 = None
                elif k.high > max(short_k0.open, short_k0.close):
                    short_k0 = None
                else:
                    if use_ticks:
                        entered, entry_price, stop_price, target_price = self._tick_entry_short(
                            k, i, klines, tick_map, signals, short_k0,
                        )
                    else:
                        entered, entry_price, stop_price, target_price = self._bar_entry_short(
                            k, i, klines, signals, short_k0,
                        )
                    if entered:
                        in_position = True
                        side = "short"
                        trailing  = False
                        td_consec = 0
                        for _r in reversed(self.k0_records):
                            if _r["k0_open_time"] == short_k0.open_time and _r["entry_open_time"] is None:
                                _r["entry_open_time"] = k.open_time
                                break
                        short_k0 = None
                        long_k0  = None
                        continue

            # ── Step 3：k0 偵測 ─────────────────────────────────────────────
            if not in_position:
                cur_ticks = tick_map.get(k.open_time) if tick_map is not None else None
                if self.enable_long and self._is_k0_long(k, cur_ticks):
                    long_k0     = k
                    long_k0_idx = i
                    signals.append(StrategySignal(
                        open_time=k.open_time,
                        price=k.low,
                        signal_type="k0_long",
                        label="k0",
                    ))
                self._cur_i = i
                self._cur_klines = klines
                if self.enable_short and self._is_k0_short(k, cur_ticks):
                    short_k0     = k
                    short_k0_idx = i
                    _body = abs(k.close - k.open)
                    _body_hi = max(k.open, k.close)
                    _uw = k.high - _body_hi
                    _denom = max(_body, self._short_body_floor(k.close))
                    _rec: dict = {
                        "k0_open_time": k.open_time,
                        "wick_type": self._classify_short_k0_wick(k),
                        "upper_wick": _uw,
                        "body": _body,
                        "wick_body_ratio": _uw / _denom,
                        "upper_wick_pct": _uw / max(k.close, 1e-9),
                        "k0_volume": k.volume,
                        "entry_open_time": None,
                    }
                    if cur_ticks is not None and len(cur_ticks) > 0:
                        _wt = cur_ticks[cur_ticks[:, 1] >= _body_hi]
                        _wvol = float(np.sum(_wt[:, 2])) if len(_wt) > 0 else 0.0
                        _tvol = float(np.sum(cur_ticks[:, 2]))
                        _rec["absorption_vol_ratio"] = _wvol / _tvol if _tvol > 0 else 0.0
                    else:
                        _rec["absorption_vol_ratio"] = None
                    self.k0_records.append(_rec)
                    signals.append(StrategySignal(
                        open_time=k.open_time,
                        price=k.high,
                        signal_type="k0_short",
                        label="k0s",
                    ))

        return signals

    # ── 進場：Bar 模式 ─────────────────────────────────────────────────────────
    def _bar_entry(
        self,
        k: Kline,
        i: int,
        klines: List[Kline],
        signals: List[StrategySignal],
        k0: Kline,
    ) -> tuple[bool, float, float, float]:
        k0_body_high = max(k0.open, k0.close)
        if k.high < k0_body_high:
            return False, 0.0, 0.0, 0.0
        if _kline_delta_eff(k) <= self.long_delta_eff_threshold:
            return False, 0.0, 0.0, 0.0
        if not self._vol_sma_ok(klines, i, k.volume, self.long_vol_sma_period, self.long_vol_sma_mult):
            return False, 0.0, 0.0, 0.0

        entry_p = k0_body_high
        stop_p = k0.low - self._calc_long_stop_dist(k0, entry_p)
        rr = self._resolve_long_rr(k0, entry_p)
        risk = entry_p - stop_p
        if risk <= 0:
            return False, 0.0, 0.0, 0.0
        if not self._risk_covers_cost(entry_p, risk, rr, self._rp('long_min_fee_cover_ratio', entry_p)):
            return False, 0.0, 0.0, 0.0
        target_p = entry_p + risk * rr
        wick_type = self._classify_long_k0_wick(k0)
        signals.append(StrategySignal(
            open_time=k.open_time,
            price=entry_p,
            signal_type="long_entry",
            label=f"L5{wick_type}",
            stop_price=stop_p,
        ))
        return True, entry_p, stop_p, target_p

    # ── 進場：Tick 模式 ────────────────────────────────────────────────────────
    def _tick_entry(
        self,
        k: Kline,
        i: int,
        klines: List[Kline],
        tick_map: TickBarMap,
        signals: List[StrategySignal],
        k0: Kline,
    ) -> tuple[bool, float, float, float]:
        k0_body_high = max(k0.open, k0.close)
        k0_body_low = min(k0.open, k0.close)

        ticks = tick_map.get(k.open_time)
        if ticks is None or len(ticks) == 0:
            if not self.allow_bar_fallback_in_tick_mode:
                return False, 0.0, 0.0, 0.0
            self._fallback_bar_count += 1
            return self._bar_entry(k, i, klines, signals, k0)

        prev_vol = klines[i - 1].volume if i > 0 else 0.0
        if not self._vol_sma_ok(klines, i, prev_vol, self.long_vol_sma_period, self.long_vol_sma_mult):
            return False, 0.0, 0.0, 0.0

        cum_buy_vol = 0.0
        cum_vol = 0.0

        for t in ticks:
            price = float(t[1])
            qty = float(t[2])
            is_bm = t[3] > 0.5

            cum_vol += qty
            if not is_bm:
                cum_buy_vol += qty

            if price < k0_body_low:
                return False, 0.0, 0.0, 0.0

            if cum_vol == 0:
                continue

            cum_delta_eff = (2.0 * cum_buy_vol - cum_vol) / cum_vol

            if price > k0_body_high and cum_delta_eff > self.long_delta_eff_threshold:
                fill_p = price
                stop_p = k0.low - self._calc_long_stop_dist(k0, fill_p)
                rr = self._resolve_long_rr(k0, fill_p)
                risk = fill_p - stop_p
                if risk <= 0:
                    continue
                if not self._risk_covers_cost(fill_p, risk, rr, self._rp('long_min_fee_cover_ratio', fill_p)):
                    continue
                target_p = fill_p + risk * rr
                wick_type = self._classify_long_k0_wick(k0)
                signals.append(StrategySignal(
                    open_time=k.open_time,
                    price=k0_body_high,
                    signal_type="long_entry",
                    label=f"L5{wick_type}",
                    stop_price=stop_p,
                    fill_price=fill_p,
                ))
                return True, fill_p, stop_p, target_p

        return False, 0.0, 0.0, 0.0

    # ── k0 判定 ────────────────────────────────────────────────────────────────
    def _is_k0_long(
        self,
        k: Kline,
        ticks: Optional[np.ndarray] = None,
    ) -> bool:
        rng = k.high - k.low
        if rng <= 0:
            return False
        if k.volume < self._rp('long_k0_vol_gate', k.close):
            return False
        mid = (k.high + k.low) / 2.0
        body_low = min(k.open, k.close)
        body = abs(k.close - k.open)
        lower_wick = body_low - k.low
        if not (body_low >= mid and lower_wick > 0 and lower_wick > body):
            return False
        return self._has_lower_wick_absorption(k, ticks, body_low)

    def _has_lower_wick_absorption(
        self,
        k: Kline,
        ticks: Optional[np.ndarray],
        body_low: float,
    ) -> bool:
        if ticks is not None and len(ticks) > 0:
            wick_ticks = ticks[ticks[:, 1] <= body_low]
            if len(wick_ticks) == 0:
                return False
            wick_vol = float(np.sum(wick_ticks[:, 2]))
            total_vol = float(np.sum(ticks[:, 2]))
            if wick_vol <= 0 or total_vol <= 0:
                return False
            wick_buy_vol = float(np.sum(wick_ticks[wick_ticks[:, 3] < 0.5, 2]))
            wick_delta = 2.0 * wick_buy_vol - wick_vol
            wick_delta_eff = wick_delta / wick_vol
            return (
                wick_vol / total_vol >= self.lower_wick_absorption_min_vol_ratio
                and wick_delta_eff <= self.lower_wick_absorption_delta_eff_max
            )
        return _kline_delta(k) <= self.lower_wick_absorption_bar_delta_max

    # ── Cost / RR helpers ─────────────────────────────────────────────────────
    def _round_trip_cost(self, price: float) -> float:
        return 2.0 * (self.taker_fee_rate + self.slippage_rate) * price

    def _long_body_floor(self, price: float) -> float:
        return max(price * self.long_body_floor_pct, 1e-9)

    def _short_body_floor(self, price: float) -> float:
        return max(price * self.short_body_floor_pct, 1e-9)

    def _get_regime(self, price: float) -> int:
        if price < self.regime_price_break_0:
            return 0
        if price < self.regime_price_break_1:
            return 1
        return 2

    def _rp(self, name: str, price: float):
        """Return regime-specific param when enable_regime_mode=True, else global."""
        if self.enable_regime_mode:
            r = self._get_regime(price)
            regime_attr = f'r{r}_{name}'
            if hasattr(self, regime_attr):
                return getattr(self, regime_attr)
        return getattr(self, name)

    def _calc_long_stop_dist(self, k0: Kline, entry_price: float) -> float:
        lower_wick = min(k0.open, k0.close) - k0.low
        dist = max(
            entry_price * self._rp('long_sl_pct_floor', entry_price),
            lower_wick * self._rp('long_sl_wick_mult', entry_price),
        )
        return min(dist, entry_price * self._rp('long_sl_pct_cap', entry_price))

    def _calc_short_stop_dist(self, k0: Kline, entry_price: float) -> float:
        upper_wick = k0.high - max(k0.open, k0.close)
        dist = max(
            entry_price * self._rp('short_sl_pct_floor', entry_price),
            upper_wick * self._rp('short_sl_wick_mult', entry_price),
        )
        return min(dist, entry_price * self._rp('short_sl_pct_cap', entry_price))

    def _classify_long_k0_wick(self, k0: Kline) -> str:
        body = abs(k0.close - k0.open)
        lower_wick = min(k0.open, k0.close) - k0.low
        denom = max(body, self._long_body_floor(k0.close))
        ratio = lower_wick / denom
        if ratio >= self.long_wick_type_a_threshold:
            return "A"
        if ratio >= self.long_wick_type_b_threshold:
            return "B"
        return "C"

    def _classify_short_k0_wick(self, k0: Kline) -> str:
        body = abs(k0.close - k0.open)
        upper_wick = k0.high - max(k0.open, k0.close)
        denom = max(body, self._short_body_floor(k0.close))
        ratio = upper_wick / denom
        if ratio >= self.short_wick_type_a_threshold:
            return "A"
        if ratio >= self.short_wick_type_b_threshold:
            return "B"
        return "C"

    def _resolve_long_rr(self, k0: Kline, entry_price: float = 0.0) -> float:
        wtype = self._classify_long_k0_wick(k0)
        p = entry_price or k0.close
        if wtype == "A":
            return self._rp('long_rr_wick_a', p)
        if wtype == "B":
            return self._rp('long_rr_wick_b', p)
        return self._rp('long_rr_wick_c', p)

    def _resolve_short_rr(self, k0: Kline, entry_price: float = 0.0) -> float:
        wtype = self._classify_short_k0_wick(k0)
        p = entry_price or k0.close
        if wtype == "A":
            return self._rp('short_rr_wick_a', p)
        if wtype == "B":
            return self._rp('short_rr_wick_b', p)
        return self._rp('short_rr_wick_c', p)

    def _is_short_wick_enabled(self, wick_type: str) -> bool:
        if wick_type == "A":
            return self.enable_short_wick_a
        if wick_type == "B":
            return self.enable_short_wick_b
        return self.enable_short_wick_c

    def _short_k0_regime_ok(self, k0: Kline, wick_type: str) -> bool:
        upper_wick = k0.high - max(k0.open, k0.close)
        if wick_type == "A":
            if self.short_a_min_upper_wick_pct <= 0:
                return True
            return (upper_wick / max(k0.close, 1e-9)) >= self.short_a_min_upper_wick_pct
        if wick_type == "B":
            if self.short_b_min_upper_wick_pct > 0:
                if (upper_wick / max(k0.close, 1e-9)) < self.short_b_min_upper_wick_pct:
                    return False
            if self.short_b_min_k0_vol > 0 and k0.volume < self.short_b_min_k0_vol:
                return False
            if self.short_b_min_runup_pct > 0:
                i = getattr(self, "_cur_i", 0)
                kls = getattr(self, "_cur_klines", None)
                if kls is not None and i >= self.short_b_runup_lookback:
                    lb = i - self.short_b_runup_lookback
                    low_ref = min(kls[j].low for j in range(lb, i))
                    runup = (k0.high - low_ref) / max(low_ref, 1e-9)
                    if runup < self.short_b_min_runup_pct:
                        return False
            return True
        return True

    def _risk_covers_cost(self, entry_price: float, risk: float, rr: float, fee_cover_ratio: float) -> bool:
        if rr <= 0 or risk <= 0 or entry_price <= 0:
            return False
        min_risk = self._round_trip_cost(entry_price) * fee_cover_ratio / rr
        return risk >= min_risk

    # ── Vol SMA 工具 ───────────────────────────────────────────────────────────
    def _vol_sma_ok(self, klines: List[Kline], cur_idx: int, cur_vol: float,
                    period: int, mult: float) -> bool:
        if period <= 0 or cur_idx < period:
            return True
        s = cur_idx - period
        sma = sum(klines[j].volume for j in range(s, cur_idx)) / period
        return cur_vol > sma * mult

    # ── 出場：Tick 模式 ────────────────────────────────────────────────────────
    def _tick_exit_long(
        self,
        k: Kline,
        tick_map: TickBarMap,
        signals: List[StrategySignal],
        target_price: float,
    ) -> bool:
        ticks = tick_map.get(k.open_time)
        if ticks is None or len(ticks) == 0:
            if not self.allow_bar_fallback_in_tick_mode:
                return False, 0.0, 0.0, 0.0
            self._fallback_bar_count += 1
            return self._bar_exit_simple_long(k, signals, target_price)

        cum_buy_vol = 0.0
        cum_vol = 0.0
        cum_delta = 0.0

        for t in ticks:
            price = t[1]
            qty = t[2]
            is_bm = t[3] > 0.5

            cum_vol += qty
            if not is_bm:
                cum_buy_vol += qty
            cum_delta = 2.0 * cum_buy_vol - cum_vol

            if price <= self._stop_price:
                label = "TS" if self._trailing else "SL"
                signals.append(StrategySignal(
                    open_time=k.open_time,
                    price=self._stop_price,
                    signal_type="long_exit",
                    label=label,
                    fill_price=price,
                ))
                return True

            if self._trailing:
                continue

            if price >= target_price:
                if cum_delta > 0:
                    self._trailing = True
                    self._stop_price = target_price
                    self._td_consec = 0
                else:
                    signals.append(StrategySignal(
                        open_time=k.open_time,
                        price=target_price,
                        signal_type="long_exit",
                        label="TP",
                    ))
                    return True

        if self._trailing:
            if cum_delta <= 0:
                self._td_consec += 1
                if self._td_consec >= self.long_td_consec_bars:
                    signals.append(StrategySignal(
                        open_time=k.open_time,
                        price=k.close,
                        signal_type="long_exit",
                        label="TD",
                    ))
                    return True
            else:
                self._td_consec = 0

        return False

    # ── 出場：Bar 模式 ─────────────────────────────────────────────────────────
    def _bar_exit_long(
        self,
        k: Kline,
        signals: List[StrategySignal],
        stop_price: float,
        target_price: float,
        trailing: bool,
        td_consec: int,
    ) -> tuple[bool, bool, int, float]:
        if k.low <= stop_price:
            label = "TS" if trailing else "SL"
            signals.append(StrategySignal(
                open_time=k.open_time,
                price=stop_price,
                signal_type="long_exit",
                label=label,
            ))
            return True, trailing, td_consec, stop_price

        if trailing:
            if _kline_delta(k) <= 0:
                td_consec += 1
                if td_consec >= self.long_td_consec_bars:
                    signals.append(StrategySignal(
                        open_time=k.open_time,
                        price=k.close,
                        signal_type="long_exit",
                        label="TD",
                    ))
                    return True, trailing, td_consec, stop_price
            else:
                td_consec = 0
        elif k.high >= target_price:
            if _kline_delta(k) > 0:
                trailing = True
                stop_price = target_price
                td_consec = 0
            else:
                signals.append(StrategySignal(
                    open_time=k.open_time,
                    price=target_price,
                    signal_type="long_exit",
                    label="TP",
                ))
                return True, trailing, td_consec, stop_price

        return False, trailing, td_consec, stop_price

    def _bar_exit_simple_long(
        self,
        k: Kline,
        signals: List[StrategySignal],
        target_price: float,
    ) -> bool:
        exited, self._trailing, self._td_consec, self._stop_price = self._bar_exit_long(
            k, signals, self._stop_price, target_price, self._trailing, self._td_consec,
        )
        return exited

    # ══════════════════════════════════════════════════════════════════════════
    # 做空鏡像方法
    # ══════════════════════════════════════════════════════════════════════════

    def _is_k0_short(
        self,
        k: Kline,
        ticks: Optional[np.ndarray] = None,
    ) -> bool:
        rng = k.high - k.low
        if rng <= 0:
            return False
        if k.volume < self._rp('short_k0_vol_gate', k.close):
            return False
        mid = (k.high + k.low) / 2.0
        body_high = max(k.open, k.close)
        body = abs(k.close - k.open)
        upper_wick = k.high - body_high
        if not (body_high <= mid and upper_wick > 0 and upper_wick > body):
            return False
        if not self._has_upper_wick_absorption(k, ticks, body_high):
            return False
        wick_type = self._classify_short_k0_wick(k)
        return self._is_short_wick_enabled(wick_type) and self._short_k0_regime_ok(k, wick_type)

    def _has_upper_wick_absorption(
        self,
        k: Kline,
        ticks: Optional[np.ndarray],
        body_high: float,
    ) -> bool:
        if ticks is not None and len(ticks) > 0:
            wick_ticks = ticks[ticks[:, 1] >= body_high]
            if len(wick_ticks) == 0:
                return False
            wick_vol = float(np.sum(wick_ticks[:, 2]))
            total_vol = float(np.sum(ticks[:, 2]))
            if wick_vol <= 0 or total_vol <= 0:
                return False
            wick_buy_vol = float(np.sum(wick_ticks[wick_ticks[:, 3] < 0.5, 2]))
            wick_delta = 2.0 * wick_buy_vol - wick_vol
            wick_delta_eff = wick_delta / wick_vol
            return (
                wick_vol / total_vol >= self.upper_wick_absorption_min_vol_ratio
                and wick_delta_eff >= self.upper_wick_absorption_delta_eff_min
            )
        return _kline_delta(k) >= self.upper_wick_absorption_bar_delta_min

    def _bar_entry_short(
        self,
        k: Kline,
        i: int,
        klines: List[Kline],
        signals: List[StrategySignal],
        k0: Kline,
    ) -> tuple[bool, float, float, float]:
        k0_body_low = min(k0.open, k0.close)
        if k.low > k0_body_low:
            return False, 0.0, 0.0, 0.0
        if _kline_delta_eff(k) >= -self.short_delta_eff_threshold:
            return False, 0.0, 0.0, 0.0
        if not self._vol_sma_ok(klines, i, k.volume, self.short_vol_sma_period, self.short_vol_sma_mult):
            return False, 0.0, 0.0, 0.0

        entry_p = k0_body_low
        stop_p  = k0.high + self._calc_short_stop_dist(k0, entry_p)
        wick_type = self._classify_short_k0_wick(k0)
        if not self._is_short_wick_enabled(wick_type):
            return False, 0.0, 0.0, 0.0
        rr = self._resolve_short_rr(k0, entry_p)
        risk = stop_p - entry_p
        if risk <= 0:
            return False, 0.0, 0.0, 0.0
        if not self._risk_covers_cost(entry_p, risk, rr, self._rp('short_min_fee_cover_ratio', entry_p)):
            return False, 0.0, 0.0, 0.0
        target_p = entry_p - risk * rr
        signals.append(StrategySignal(
            open_time=k.open_time,
            price=entry_p,
            signal_type="short_entry",
            label=f"S5{wick_type}",
            stop_price=stop_p,
        ))
        return True, entry_p, stop_p, target_p

    def _tick_entry_short(
        self,
        k: Kline,
        i: int,
        klines: List[Kline],
        tick_map: TickBarMap,
        signals: List[StrategySignal],
        k0: Kline,
    ) -> tuple[bool, float, float, float]:
        k0_body_low  = min(k0.open, k0.close)
        k0_body_high = max(k0.open, k0.close)

        ticks = tick_map.get(k.open_time)
        if ticks is None or len(ticks) == 0:
            if not self.allow_bar_fallback_in_tick_mode:
                return False, 0.0, 0.0, 0.0
            self._fallback_bar_count += 1
            return self._bar_entry_short(k, i, klines, signals, k0)

        prev_vol = klines[i - 1].volume if i > 0 else 0.0
        if not self._vol_sma_ok(klines, i, prev_vol, self.short_vol_sma_period, self.short_vol_sma_mult):
            return False, 0.0, 0.0, 0.0

        cum_buy_vol = 0.0
        cum_vol     = 0.0

        for t in ticks:
            price = float(t[1])
            qty   = float(t[2])
            is_bm = t[3] > 0.5

            cum_vol += qty
            if not is_bm:
                cum_buy_vol += qty

            if price > k0_body_high:
                return False, 0.0, 0.0, 0.0

            if cum_vol == 0:
                continue

            cum_delta_eff = (2.0 * cum_buy_vol - cum_vol) / cum_vol

            if price < k0_body_low and cum_delta_eff < -self.short_delta_eff_threshold:
                fill_p = price
                stop_p = k0.high + self._calc_short_stop_dist(k0, fill_p)
                wick_type = self._classify_short_k0_wick(k0)
                if not self._is_short_wick_enabled(wick_type):
                    continue
                rr = self._resolve_short_rr(k0, fill_p)
                risk   = stop_p - fill_p
                if risk <= 0:
                    continue
                if not self._risk_covers_cost(fill_p, risk, rr, self._rp('short_min_fee_cover_ratio', fill_p)):
                    continue
                target_p = fill_p - risk * rr
                signals.append(StrategySignal(
                    open_time=k.open_time,
                    price=k0_body_low,
                    signal_type="short_entry",
                    label=f"S5{wick_type}",
                    stop_price=stop_p,
                    fill_price=fill_p,
                ))
                return True, fill_p, stop_p, target_p

        return False, 0.0, 0.0, 0.0

    def _tick_exit_short(
        self,
        k: Kline,
        tick_map: TickBarMap,
        signals: List[StrategySignal],
        target_price: float,
    ) -> bool:
        ticks = tick_map.get(k.open_time)
        if ticks is None or len(ticks) == 0:
            if not self.allow_bar_fallback_in_tick_mode:
                return False
            self._fallback_bar_count += 1
            return self._bar_exit_simple_short(k, signals, target_price)

        cum_buy_vol = 0.0
        cum_vol     = 0.0
        cum_delta   = 0.0

        for t in ticks:
            price = t[1]
            qty   = t[2]
            is_bm = t[3] > 0.5

            cum_vol += qty
            if not is_bm:
                cum_buy_vol += qty
            cum_delta = 2.0 * cum_buy_vol - cum_vol

            if price >= self._stop_price:
                label = "TS" if self._trailing else "SL"
                signals.append(StrategySignal(
                    open_time=k.open_time,
                    price=self._stop_price,
                    signal_type="short_exit",
                    label=label,
                    fill_price=price,
                ))
                return True

            if self._trailing:
                continue

            if price <= target_price:
                if cum_delta < 0:
                    self._trailing   = True
                    self._stop_price = target_price
                    self._td_consec  = 0
                else:
                    signals.append(StrategySignal(
                        open_time=k.open_time,
                        price=target_price,
                        signal_type="short_exit",
                        label="TP",
                    ))
                    return True

        if self._trailing:
            if cum_delta >= 0:
                self._td_consec += 1
                if self._td_consec >= self.short_td_consec_bars:
                    signals.append(StrategySignal(
                        open_time=k.open_time,
                        price=k.close,
                        signal_type="short_exit",
                        label="TD",
                    ))
                    return True
            else:
                self._td_consec = 0

        return False

    def _bar_exit_short(
        self,
        k: Kline,
        signals: List[StrategySignal],
        stop_price: float,
        target_price: float,
        trailing: bool,
        td_consec: int,
    ) -> tuple[bool, bool, int, float]:
        if k.high >= stop_price:
            label = "TS" if trailing else "SL"
            signals.append(StrategySignal(
                open_time=k.open_time,
                price=stop_price,
                signal_type="short_exit",
                label=label,
            ))
            return True, trailing, td_consec, stop_price

        if trailing:
            if _kline_delta(k) >= 0:
                td_consec += 1
                if td_consec >= self.short_td_consec_bars:
                    signals.append(StrategySignal(
                        open_time=k.open_time,
                        price=k.close,
                        signal_type="short_exit",
                        label="TD",
                    ))
                    return True, trailing, td_consec, stop_price
            else:
                td_consec = 0
        elif k.low <= target_price:
            if _kline_delta(k) < 0:
                trailing   = True
                stop_price = target_price
                td_consec  = 0
            else:
                signals.append(StrategySignal(
                    open_time=k.open_time,
                    price=target_price,
                    signal_type="short_exit",
                    label="TP",
                ))
                return True, trailing, td_consec, stop_price

        return False, trailing, td_consec, stop_price

    def _bar_exit_simple_short(
        self,
        k: Kline,
        signals: List[StrategySignal],
        target_price: float,
    ) -> bool:
        exited, self._trailing, self._td_consec, self._stop_price = self._bar_exit_short(
            k, signals, self._stop_price, target_price, self._trailing, self._td_consec,
        )
        return exited
