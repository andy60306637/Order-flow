"""
Wick Reversal v4 Ratio

v4 的價格比例化版本：以 BTC 85000–90000（midpoint 87500）為 baseline，
將下列參數依當前 k0 收盤價自動縮放：

  sl_offset           → 正比縮放  (保持停損距離佔價格比例不變)
  k0_vol_gate         → 反比縮放  (BTC 成交量隨價格上升而下降)
  min_fee_cover_ratio → 正比縮放  (高價位對費率覆蓋要求更嚴)

其餘參數與邏輯和 v4 完全相同，在本檔案獨立維護。
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np

from core.data_types import Kline
from strategies.base import StrategyBase, StrategySignal, TickBarMap
from strategies import register

_BASELINE = 87500.0  # BTC 85k–90k midpoint


def _kline_delta(k: Kline) -> float:
    return 2.0 * k.taker_buy_volume - k.volume


def _kline_delta_eff(k: Kline) -> float:
    if k.volume == 0:
        return 0.0
    return _kline_delta(k) / k.volume


@register
class WickReversalV4RatioStrategy(StrategyBase):
    name = "Wick Reversal 1m v4 Ratio"
    allow_bar_fallback_in_tick_mode: bool = True

    # ── ratio baseline ────────────────────────────────────────────────────────
    baseline_price: float = _BASELINE  # BTC 85k–90k midpoint
    ratio_map_eps: float = 1e-9
    # Ratio mapping knobs (defaults keep current v4_ratio behavior).
    sl_offset_map_exponent: float = 1.0
    sl_offset_map_strength: float = 1.0
    sl_offset_map_min_mult: float = 1e-6
    sl_offset_map_max_mult: float = 0.0   # 0 => no upper clamp
    k0_vol_gate_map_exponent: float = -1.0
    k0_vol_gate_map_strength: float = 1.0
    k0_vol_gate_map_min_mult: float = 1e-6
    k0_vol_gate_map_max_mult: float = 0.0  # 0 => no upper clamp
    min_fee_cover_map_exponent: float = 1.0
    min_fee_cover_map_strength: float = 1.0
    min_fee_cover_map_min_mult: float = 1e-6
    min_fee_cover_map_max_mult: float = 0.0  # 0 => no upper clamp

    # ── 做多參數 ──────────────────────────────────────────────────────────────
    enable_long: bool = True
    long_zoom_bars: int = 1
    long_sl_offset: float = 10.0                   # baseline @ 87500
    long_rr_ratio: float = 2
    long_td_consec_bars: int = 1
    long_k0_vol_gate: float = 500.0                # baseline @ 87500 (BTC)
    long_delta_eff_threshold: float = 0.8
    long_vol_sma_period: int = 20
    long_vol_sma_mult: float = 1.0
    lower_wick_absorption_delta_eff_max: float = 0.0
    lower_wick_absorption_min_vol_ratio: float = 0.15
    lower_wick_absorption_bar_delta_max: float = 0.0
    # ── 做多 cost filter / dynamic RR ─────────────────────────────────────
    long_min_fee_cover_ratio: float = 1.2          # baseline @ 87500
    long_body_floor_pct: float = 0.00001
    long_wick_type_a_threshold: float = 4.0
    long_wick_type_b_threshold: float = 3.0
    long_rr_wick_a: float = 3.0
    long_rr_wick_b: float = 1.5
    long_rr_wick_c: float = 2.0
    # ── 做空參數（鏡像）──────────────────────────────────────────────────────
    enable_short: bool = True
    short_zoom_bars: int = 1
    short_sl_offset: float = 10.0                  # baseline @ 87500
    short_rr_ratio: float = 1.0
    short_td_consec_bars: int = 2
    short_k0_vol_gate: float = 300.0               # baseline @ 87500 (BTC)
    short_delta_eff_threshold: float = 0.8
    short_vol_sma_period: int = 20
    short_vol_sma_mult: float = 1.2
    upper_wick_absorption_delta_eff_min: float = 0.0
    upper_wick_absorption_min_vol_ratio: float = 0.15
    upper_wick_absorption_bar_delta_min: float = 0.0
    # ── 做空 cost filter / dynamic RR ─────────────────────────────────────
    short_min_fee_cover_ratio: float = 2.0         # baseline @ 87500
    short_body_floor_pct: float = 0.00001
    short_wick_type_a_threshold: float = 4.0
    short_wick_type_b_threshold: float = 3.0
    enable_short_wick_a: bool = True
    enable_short_wick_b: bool = True
    enable_short_wick_c: bool = False
    short_a_min_upper_wick_pct: float = 0.0011
    short_rr_wick_a: float = 4.5
    short_rr_wick_b: float = 2.5
    short_rr_wick_c: float = 2.0
    # ── S4B 專屬 filter ───────────────────────────────────────────────────
    short_b_min_upper_wick_pct: float = 0.0
    short_b_min_k0_vol: float = 0.0
    short_b_min_runup_pct: float = 0.0
    short_b_runup_lookback: int = 3
    # ── cost helper 參數 ──────────────────────────────────────────────────
    taker_fee_rate: float = 0.00032
    slippage_rate: float = 0.00002

    # ── ratio 縮放工具 ────────────────────────────────────────────────────────
    def _price_ratio(self, price: float) -> float:
        p = max(price, self.ratio_map_eps)
        base = max(self.baseline_price, self.ratio_map_eps)
        return p / base

    def _map_with_price_ratio(
        self,
        k0_price: float,
        exponent: float,
        strength: float,
        min_mult: float,
        max_mult: float,
    ) -> float:
        """
        Generic mapping function for ratio-mode parameters.
        raw = ratio ** exponent
        mapped = 1 + strength * (raw - 1)
        """
        ratio = self._price_ratio(k0_price)
        raw_mult = ratio ** exponent
        mapped_mult = 1.0 + strength * (raw_mult - 1.0)
        floor = max(min_mult, self.ratio_map_eps)
        mapped_mult = max(mapped_mult, floor)
        if max_mult > 0:
            mapped_mult = min(mapped_mult, max_mult)
        return mapped_mult

    def _eff_sl_offset(self, k0_price: float, base_offset: float) -> float:
        """sl_offset 正比縮放：停損距離佔價格比例不變"""
        mult = self._map_with_price_ratio(
            k0_price,
            self.sl_offset_map_exponent,
            self.sl_offset_map_strength,
            self.sl_offset_map_min_mult,
            self.sl_offset_map_max_mult,
        )
        return base_offset * mult

    def _eff_vol_gate(self, k0_price: float, base_gate: float) -> float:
        """vol_gate 反比縮放：BTC 成交量隨價格上升而下降"""
        mult = self._map_with_price_ratio(
            k0_price,
            self.k0_vol_gate_map_exponent,
            self.k0_vol_gate_map_strength,
            self.k0_vol_gate_map_min_mult,
            self.k0_vol_gate_map_max_mult,
        )
        return base_gate * mult

    def _eff_fee_cover(self, k0_price: float, base_ratio: float) -> float:
        """fee_cover_ratio 正比縮放：高價位對費率覆蓋要求更嚴"""
        mult = self._map_with_price_ratio(
            k0_price,
            self.min_fee_cover_map_exponent,
            self.min_fee_cover_map_strength,
            self.min_fee_cover_map_min_mult,
            self.min_fee_cover_map_max_mult,
        )
        return base_ratio * mult

    # ── 主迴圈 ────────────────────────────────────────────────────────────────
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
                        entered, _, stop_price, target_price = self._tick_entry(
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
        stop_p = k0.low - self._eff_sl_offset(k0.close, self.long_sl_offset)
        rr = self._resolve_long_rr(k0)
        risk = entry_p - stop_p
        if risk <= 0:
            return False, 0.0, 0.0, 0.0
        if not self._risk_covers_cost(entry_p, risk, rr, self._eff_fee_cover(k0.close, self.long_min_fee_cover_ratio)):
            return False, 0.0, 0.0, 0.0
        target_p = entry_p + risk * rr
        wick_type = self._classify_long_k0_wick(k0)
        signals.append(StrategySignal(
            open_time=k.open_time,
            price=entry_p,
            signal_type="long_entry",
            label=f"L4{wick_type}",
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
                stop_p = k0.low - self._eff_sl_offset(k0.close, self.long_sl_offset)
                rr = self._resolve_long_rr(k0)
                risk = fill_p - stop_p
                if risk <= 0:
                    continue
                if not self._risk_covers_cost(fill_p, risk, rr, self._eff_fee_cover(k0.close, self.long_min_fee_cover_ratio)):
                    continue
                target_p = fill_p + risk * rr
                wick_type = self._classify_long_k0_wick(k0)
                signals.append(StrategySignal(
                    open_time=k.open_time,
                    price=k0_body_high,
                    signal_type="long_entry",
                    label=f"L4{wick_type}",
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
        if k.volume < self._eff_vol_gate(k.close, self.long_k0_vol_gate):
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

    def _resolve_long_rr(self, k0: Kline) -> float:
        wtype = self._classify_long_k0_wick(k0)
        if wtype == "A":
            return self.long_rr_wick_a
        if wtype == "B":
            return self.long_rr_wick_b
        return self.long_rr_wick_c

    def _resolve_short_rr(self, k0: Kline) -> float:
        wtype = self._classify_short_k0_wick(k0)
        if wtype == "A":
            return self.short_rr_wick_a
        if wtype == "B":
            return self.short_rr_wick_b
        return self.short_rr_wick_c

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
            if self.short_b_min_k0_vol > 0 and k0.volume < self._eff_vol_gate(k0.close, self.short_b_min_k0_vol):
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
                return False
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
        if k.volume < self._eff_vol_gate(k.close, self.short_k0_vol_gate):
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
        stop_p  = k0.high + self._eff_sl_offset(k0.close, self.short_sl_offset)
        wick_type = self._classify_short_k0_wick(k0)
        if not self._is_short_wick_enabled(wick_type):
            return False, 0.0, 0.0, 0.0
        rr = self._resolve_short_rr(k0)
        risk = stop_p - entry_p
        if risk <= 0:
            return False, 0.0, 0.0, 0.0
        if not self._risk_covers_cost(entry_p, risk, rr, self._eff_fee_cover(k0.close, self.short_min_fee_cover_ratio)):
            return False, 0.0, 0.0, 0.0
        target_p = entry_p - risk * rr
        signals.append(StrategySignal(
            open_time=k.open_time,
            price=entry_p,
            signal_type="short_entry",
            label=f"S4{wick_type}",
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
                stop_p = k0.high + self._eff_sl_offset(k0.close, self.short_sl_offset)
                wick_type = self._classify_short_k0_wick(k0)
                if not self._is_short_wick_enabled(wick_type):
                    continue
                rr = self._resolve_short_rr(k0)
                risk   = stop_p - fill_p
                if risk <= 0:
                    continue
                if not self._risk_covers_cost(fill_p, risk, rr, self._eff_fee_cover(k0.close, self.short_min_fee_cover_ratio)):
                    continue
                target_p = fill_p - risk * rr
                signals.append(StrategySignal(
                    open_time=k.open_time,
                    price=k0_body_low,
                    signal_type="short_entry",
                    label=f"S4{wick_type}",
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
