"""
Auction Value Sweep Strategy

以昨日 24H UTC Session Volume Profile 的 VAL/POC/VAH 為框架，
在 VAL 或 VAH 出現 K0（同 wick_reversal_v4_ratio 品質邏輯）時
用 Tick 精確入場，以 POC 為第一目標（TP1 → trailing with BE stop），
並支援三種突破回踩停利模式。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional

import numpy as np

from core.data_types import Kline
from core.volume_profile import VolumeProfile, build_composite_profile
from strategies.base import StrategyBase, StrategySignal, TickBarMap
from strategies import register

_BASELINE = 87500.0
_MS_1D = 86_400_000
_VP_INTERVAL_MS_MAP = {
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "8h": 8 * 60 * 60_000,
    "24h": _MS_1D,
}


def _kline_delta(k: Kline) -> float:
    return 2.0 * k.taker_buy_volume - k.volume


def _kline_delta_eff(k: Kline) -> float:
    if k.volume == 0:
        return 0.0
    return _kline_delta(k) / k.volume


@register
class AuctionValueSweepStrategy(StrategyBase):
    name = "Auction Value Sweep"
    allow_bar_fallback_in_tick_mode: bool = True

    # ── VP params ────────────────────────────────────────────────────────────
    vp_tick_size: float = 1.0
    value_area_pct: float = 0.70
    vp_interval: str = "15m"

    # ── Session filter ───────────────────────────────────────────────────────
    enable_session_filter: bool = True
    session_start_utc_hour: int = 0
    session_end_utc_hour: int = 21

    # ── TP mode (break & retest only) ────────────────────────────────────────
    tp_mode: int = 0  # 0=A equal range, 1=B wick×1.618, 2=C pure trail

    # ── ratio baseline ────────────────────────────────────────────────────────
    baseline_price: float = _BASELINE
    ratio_map_eps: float = 1e-9
    sl_offset_map_exponent: float = 1.0
    sl_offset_map_strength: float = 1.0
    sl_offset_map_min_mult: float = 1e-6
    sl_offset_map_max_mult: float = 0.0
    k0_vol_gate_map_exponent: float = -1.0
    k0_vol_gate_map_strength: float = 1.0
    k0_vol_gate_map_min_mult: float = 1e-6
    k0_vol_gate_map_max_mult: float = 0.0
    min_fee_cover_map_exponent: float = 1.0
    min_fee_cover_map_strength: float = 1.0
    min_fee_cover_map_min_mult: float = 1e-6
    min_fee_cover_map_max_mult: float = 0.0

    # ── 做多參數 ──────────────────────────────────────────────────────────────
    enable_long: bool = True
    long_zoom_bars: int = 1
    long_sl_offset: float = 10.0
    long_td_consec_bars: int = 1
    long_k0_vol_gate: float = 500.0
    long_delta_eff_threshold: float = 0.8
    long_vol_sma_period: int = 20
    long_vol_sma_mult: float = 1.0
    lower_wick_absorption_delta_eff_max: float = 0.0
    lower_wick_absorption_min_vol_ratio: float = 0.15
    lower_wick_absorption_bar_delta_max: float = 0.0
    long_min_fee_cover_ratio: float = 1.2
    long_body_floor_pct: float = 0.00001
    long_wick_type_a_threshold: float = 4.0
    long_wick_type_b_threshold: float = 3.0
    long_rr_wick_a: float = 3.0
    long_rr_wick_b: float = 1.5
    long_rr_wick_c: float = 2.0

    # ── 做空參數（鏡像）──────────────────────────────────────────────────────
    enable_short: bool = True
    short_zoom_bars: int = 1
    short_sl_offset: float = 10.0
    short_td_consec_bars: int = 2
    short_k0_vol_gate: float = 300.0
    short_delta_eff_threshold: float = 0.8
    short_vol_sma_period: int = 20
    short_vol_sma_mult: float = 1.2
    upper_wick_absorption_delta_eff_min: float = 0.0
    upper_wick_absorption_min_vol_ratio: float = 0.15
    upper_wick_absorption_bar_delta_min: float = 0.0
    short_min_fee_cover_ratio: float = 2.0
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
    short_b_min_upper_wick_pct: float = 0.0
    short_b_min_k0_vol: float = 0.0
    short_b_min_runup_pct: float = 0.0
    short_b_runup_lookback: int = 3

    # ── cost helper params ────────────────────────────────────────────────────
    taker_fee_rate: float = 0.00032
    slippage_rate: float = 0.00002

    # ── VP cache helpers ──────────────────────────────────────────────────────
    def _build_daily_vp_cache(
        self,
        klines: List[Kline],
        tick_map: Optional[TickBarMap],
    ) -> Dict[int, VolumeProfile]:
        if tick_map is None:
            return {}
        bucket_ms = self._vp_bucket_ms()
        day_buckets: Dict[int, List[int]] = defaultdict(list)
        for k in klines:
            day_key = (k.open_time // bucket_ms) * bucket_ms
            day_buckets[day_key].append(k.open_time)
        result: Dict[int, VolumeProfile] = {}
        for day_key, open_times in day_buckets.items():
            vp = build_composite_profile(
                tick_map,
                open_times,
                tick_size=self.vp_tick_size,
                value_area_pct=self.value_area_pct,
            )
            if vp is not None:
                result[day_key] = vp
        return result

    def _vp_bucket_ms(self) -> int:
        interval = str(self.vp_interval).strip().lower()
        bucket_ms = _VP_INTERVAL_MS_MAP.get(interval)
        if bucket_ms is None:
            allowed = ", ".join(_VP_INTERVAL_MS_MAP.keys())
            raise ValueError(f"vp_interval must be one of: {allowed}")
        return bucket_ms

    def _vp_for_bar(
        self,
        k: Kline,
        daily_vp: Dict[int, VolumeProfile],
    ) -> Optional[VolumeProfile]:
        bucket_ms = self._vp_bucket_ms()
        day_start = (k.open_time // bucket_ms) * bucket_ms
        return daily_vp.get(day_start - bucket_ms)

    def _in_session(self, k: Kline) -> bool:
        if not self.enable_session_filter:
            return True
        hour = (k.open_time // 3_600_000) % 24
        return self.session_start_utc_hour <= hour <= self.session_end_utc_hour

    def _assert_ticks_time_ascending(self, tick_map: TickBarMap) -> None:
        for bar_open_time, ticks in tick_map.items():
            if ticks is None or len(ticks) <= 1:
                continue
            trade_times = ticks[:, 0]
            if np.any(np.diff(trade_times) < 0):
                raise ValueError(
                    f"tick_map[{int(bar_open_time)}] must be sorted by trade timestamp ascending"
                )

    # ── Scenario detection ────────────────────────────────────────────────────
    def _detect_long_scenario(
        self,
        k: Kline,
        vp: VolumeProfile,
        ticks: Optional[np.ndarray],
    ) -> Optional[str]:
        body_low = min(k.open, k.close)
        if k.low <= vp.val and body_low >= vp.val and self._is_k0_long(k, ticks):
            return "val_reject"
        if k.low <= vp.vah and body_low >= vp.vah and self._is_k0_long(k, ticks):
            return "vah_retest"
        return None

    def _detect_short_scenario(
        self,
        k: Kline,
        vp: VolumeProfile,
        ticks: Optional[np.ndarray],
    ) -> Optional[str]:
        body_high = max(k.open, k.close)
        if k.high >= vp.vah and body_high <= vp.vah and self._is_k0_short(k, ticks):
            return "vah_reject"
        if k.high >= vp.val and body_high <= vp.val and self._is_k0_short(k, ticks):
            return "val_retest"
        return None

    # ── Target computation ────────────────────────────────────────────────────
    def _compute_long_target(
        self,
        scenario: str,
        vp: VolumeProfile,
        k0: Kline,
        fill_p: float,
    ) -> float:
        if scenario == "val_reject":
            return vp.poc_price
        va_range = max(vp.vah - vp.val, 0.0)
        if self.tp_mode == 0:
            return vp.vah + va_range
        if self.tp_mode == 1:
            return fill_p + (k0.high - k0.low) * 1.618
        return fill_p + 1e9  # mode C sentinel

    def _compute_short_target(
        self,
        scenario: str,
        vp: VolumeProfile,
        k0: Kline,
        fill_p: float,
    ) -> float:
        if scenario == "vah_reject":
            return vp.poc_price
        va_range = max(vp.vah - vp.val, 0.0)
        if self.tp_mode == 0:
            return vp.val - va_range
        if self.tp_mode == 1:
            return fill_p - (k0.high - k0.low) * 1.618
        return fill_p - 1e9  # mode C sentinel

    # ── Fee distance filter ───────────────────────────────────────────────────
    def _fee_distance_ok_long(
        self, fill_p: float, target_p: float, k0_close: float
    ) -> bool:
        if target_p <= fill_p:
            return False
        if target_p >= fill_p + 1e8:
            return True  # sentinel is always ok
        expected = target_p - fill_p
        cost = self._round_trip_cost(fill_p)
        return expected >= cost * self._eff_fee_cover(k0_close, self.long_min_fee_cover_ratio)

    def _fee_distance_ok_short(
        self, fill_p: float, target_p: float, k0_close: float
    ) -> bool:
        if target_p >= fill_p:
            return False
        if target_p <= fill_p - 1e8:
            return True  # sentinel is always ok
        expected = fill_p - target_p
        cost = self._round_trip_cost(fill_p)
        return expected >= cost * self._eff_fee_cover(k0_close, self.short_min_fee_cover_ratio)

    # ── Main loop ─────────────────────────────────────────────────────────────
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
        if use_ticks:
            self._assert_ticks_time_ascending(tick_map)
        daily_vp = self._build_daily_vp_cache(klines, tick_map) if use_ticks else {}

        long_k0:       Optional[Kline] = None
        long_k0_idx    = -1
        long_scenario: Optional[str] = None
        long_vp:       Optional[VolumeProfile] = None
        short_k0:      Optional[Kline] = None
        short_k0_idx   = -1
        short_scenario: Optional[str] = None
        short_vp:      Optional[VolumeProfile] = None

        in_position  = False
        side         = ""
        stop_price   = 0.0
        target_price = 0.0
        trailing     = False
        td_consec    = 0
        is_rejection = False

        self._trailing          = False
        self._td_consec         = 0
        self._stop_price        = 0.0
        self._entry_price       = 0.0
        self._fallback_bar_count = 0
        self._cur_i             = 0
        self._cur_klines        = klines

        for i, k in enumerate(klines):
            # ── Step 1: exit management ─────────────────────────────────────
            if in_position:
                exited = False

                if side == "long":
                    if use_ticks:
                        self._trailing   = trailing
                        self._td_consec  = td_consec
                        self._stop_price = stop_price
                        exited = self._tick_exit_long_avs(
                            k, tick_map, signals, target_price, is_rejection,
                        )
                        if not exited:
                            trailing   = self._trailing
                            td_consec  = self._td_consec
                            stop_price = self._stop_price
                    else:
                        exited, trailing, td_consec, stop_price = self._bar_exit_long_avs(
                            k, signals, stop_price, target_price,
                            trailing, td_consec, is_rejection,
                        )
                else:
                    if use_ticks:
                        self._trailing   = trailing
                        self._td_consec  = td_consec
                        self._stop_price = stop_price
                        exited = self._tick_exit_short_avs(
                            k, tick_map, signals, target_price, is_rejection,
                        )
                        if not exited:
                            trailing   = self._trailing
                            td_consec  = self._td_consec
                            stop_price = self._stop_price
                    else:
                        exited, trailing, td_consec, stop_price = self._bar_exit_short_avs(
                            k, signals, stop_price, target_price,
                            trailing, td_consec, is_rejection,
                        )

                if exited:
                    in_position  = False
                    side         = ""
                    trailing     = False
                    td_consec    = 0
                    is_rejection = False
                    long_k0      = None
                    short_k0     = None
                else:
                    continue

            # ── Step 2a: long zoom entry ────────────────────────────────────
            if long_k0 is not None and i > long_k0_idx:
                bars_after = i - long_k0_idx
                if bars_after > self.long_zoom_bars:
                    long_k0 = None
                elif k.low < min(long_k0.open, long_k0.close):
                    long_k0 = None
                else:
                    if use_ticks:
                        entered, fill_p, stop_p, target_p = self._tick_entry_long_avs(
                            k, i, klines, tick_map, signals,
                            long_k0, long_scenario, long_vp,
                        )
                    else:
                        entered, fill_p, stop_p, target_p = self._bar_entry_long_avs(
                            k, i, klines, signals,
                            long_k0, long_scenario, long_vp,
                        )
                    if entered:
                        in_position       = True
                        side              = "long"
                        stop_price        = stop_p
                        target_price      = target_p
                        is_rejection      = long_scenario == "val_reject"
                        trailing          = (
                            self.tp_mode == 2 and long_scenario != "val_reject"
                        )
                        td_consec         = 0
                        self._entry_price = fill_p
                        long_k0  = None
                        short_k0 = None
                        continue

            # ── Step 2b: short zoom entry ───────────────────────────────────
            if short_k0 is not None and i > short_k0_idx:
                bars_after = i - short_k0_idx
                if bars_after > self.short_zoom_bars:
                    short_k0 = None
                elif k.high > max(short_k0.open, short_k0.close):
                    short_k0 = None
                else:
                    if use_ticks:
                        entered, fill_p, stop_p, target_p = self._tick_entry_short_avs(
                            k, i, klines, tick_map, signals,
                            short_k0, short_scenario, short_vp,
                        )
                    else:
                        entered, fill_p, stop_p, target_p = self._bar_entry_short_avs(
                            k, i, klines, signals,
                            short_k0, short_scenario, short_vp,
                        )
                    if entered:
                        in_position       = True
                        side              = "short"
                        stop_price        = stop_p
                        target_price      = target_p
                        is_rejection      = short_scenario == "vah_reject"
                        trailing          = (
                            self.tp_mode == 2 and short_scenario != "vah_reject"
                        )
                        td_consec         = 0
                        self._entry_price = fill_p
                        long_k0  = None
                        short_k0 = None
                        continue

            # ── Step 3: K0 detection ────────────────────────────────────────
            if not in_position:
                if not daily_vp:
                    continue
                vp = self._vp_for_bar(k, daily_vp)
                if vp is None:
                    continue
                if not self._in_session(k):
                    continue

                cur_ticks = tick_map.get(k.open_time) if tick_map is not None else None

                if self.enable_long:
                    sc = self._detect_long_scenario(k, vp, cur_ticks)
                    if sc is not None:
                        long_k0       = k
                        long_k0_idx   = i
                        long_scenario = sc
                        long_vp       = vp
                        signals.append(StrategySignal(
                            open_time=k.open_time,
                            price=k.low,
                            signal_type="k0_long",
                            label="k0",
                        ))

                self._cur_i      = i
                self._cur_klines = klines

                if self.enable_short:
                    sc = self._detect_short_scenario(k, vp, cur_ticks)
                    if sc is not None:
                        short_k0       = k
                        short_k0_idx   = i
                        short_scenario = sc
                        short_vp       = vp
                        signals.append(StrategySignal(
                            open_time=k.open_time,
                            price=k.high,
                            signal_type="k0_short",
                            label="k0s",
                        ))

        return signals

    # ── Long entry: bar mode ──────────────────────────────────────────────────
    def _bar_entry_long_avs(
        self,
        k: Kline,
        i: int,
        klines: List[Kline],
        signals: List[StrategySignal],
        k0: Kline,
        scenario: str,
        vp: VolumeProfile,
    ) -> tuple[bool, float, float, float]:
        k0_body_high = max(k0.open, k0.close)
        if k.high < k0_body_high:
            return False, 0.0, 0.0, 0.0
        if _kline_delta_eff(k) <= self.long_delta_eff_threshold:
            return False, 0.0, 0.0, 0.0
        if not self._vol_sma_ok(klines, i, k.volume, self.long_vol_sma_period, self.long_vol_sma_mult):
            return False, 0.0, 0.0, 0.0

        fill_p = k0_body_high
        stop_p = k0.low - self._eff_sl_offset(k0.close, self.long_sl_offset)
        if fill_p <= stop_p:
            return False, 0.0, 0.0, 0.0
        target_p = self._compute_long_target(scenario, vp, k0, fill_p)
        if not self._fee_distance_ok_long(fill_p, target_p, k0.close):
            return False, 0.0, 0.0, 0.0
        wick_type = self._classify_long_k0_wick(k0)
        signals.append(StrategySignal(
            open_time=k.open_time,
            price=fill_p,
            signal_type="long_entry",
            label=f"L4{wick_type}",
            stop_price=stop_p,
        ))
        return True, fill_p, stop_p, target_p

    # ── Long entry: tick mode ─────────────────────────────────────────────────
    def _tick_entry_long_avs(
        self,
        k: Kline,
        i: int,
        klines: List[Kline],
        tick_map: TickBarMap,
        signals: List[StrategySignal],
        k0: Kline,
        scenario: str,
        vp: VolumeProfile,
    ) -> tuple[bool, float, float, float]:
        k0_body_high = max(k0.open, k0.close)
        k0_body_low  = min(k0.open, k0.close)

        ticks = tick_map.get(k.open_time)
        if ticks is None or len(ticks) == 0:
            if not self.allow_bar_fallback_in_tick_mode:
                return False, 0.0, 0.0, 0.0
            self._fallback_bar_count += 1
            return self._bar_entry_long_avs(k, i, klines, signals, k0, scenario, vp)

        prev_vol = klines[i - 1].volume if i > 0 else 0.0
        if not self._vol_sma_ok(klines, i, prev_vol, self.long_vol_sma_period, self.long_vol_sma_mult):
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

            if price < k0_body_low:
                return False, 0.0, 0.0, 0.0

            if cum_vol == 0:
                continue

            cum_delta_eff = (2.0 * cum_buy_vol - cum_vol) / cum_vol

            if price > k0_body_high and cum_delta_eff > self.long_delta_eff_threshold:
                fill_p = price
                stop_p = k0.low - self._eff_sl_offset(k0.close, self.long_sl_offset)
                if fill_p <= stop_p:
                    continue
                target_p = self._compute_long_target(scenario, vp, k0, fill_p)
                if not self._fee_distance_ok_long(fill_p, target_p, k0.close):
                    continue
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

    # ── Short entry: bar mode ─────────────────────────────────────────────────
    def _bar_entry_short_avs(
        self,
        k: Kline,
        i: int,
        klines: List[Kline],
        signals: List[StrategySignal],
        k0: Kline,
        scenario: str,
        vp: VolumeProfile,
    ) -> tuple[bool, float, float, float]:
        k0_body_low = min(k0.open, k0.close)
        if k.low > k0_body_low:
            return False, 0.0, 0.0, 0.0
        if _kline_delta_eff(k) >= -self.short_delta_eff_threshold:
            return False, 0.0, 0.0, 0.0
        if not self._vol_sma_ok(klines, i, k.volume, self.short_vol_sma_period, self.short_vol_sma_mult):
            return False, 0.0, 0.0, 0.0

        fill_p    = k0_body_low
        stop_p    = k0.high + self._eff_sl_offset(k0.close, self.short_sl_offset)
        wick_type = self._classify_short_k0_wick(k0)
        if not self._is_short_wick_enabled(wick_type):
            return False, 0.0, 0.0, 0.0
        if fill_p >= stop_p:
            return False, 0.0, 0.0, 0.0
        target_p = self._compute_short_target(scenario, vp, k0, fill_p)
        if not self._fee_distance_ok_short(fill_p, target_p, k0.close):
            return False, 0.0, 0.0, 0.0
        signals.append(StrategySignal(
            open_time=k.open_time,
            price=fill_p,
            signal_type="short_entry",
            label=f"S4{wick_type}",
            stop_price=stop_p,
        ))
        return True, fill_p, stop_p, target_p

    # ── Short entry: tick mode ────────────────────────────────────────────────
    def _tick_entry_short_avs(
        self,
        k: Kline,
        i: int,
        klines: List[Kline],
        tick_map: TickBarMap,
        signals: List[StrategySignal],
        k0: Kline,
        scenario: str,
        vp: VolumeProfile,
    ) -> tuple[bool, float, float, float]:
        k0_body_low  = min(k0.open, k0.close)
        k0_body_high = max(k0.open, k0.close)

        ticks = tick_map.get(k.open_time)
        if ticks is None or len(ticks) == 0:
            if not self.allow_bar_fallback_in_tick_mode:
                return False, 0.0, 0.0, 0.0
            self._fallback_bar_count += 1
            return self._bar_entry_short_avs(k, i, klines, signals, k0, scenario, vp)

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
                fill_p    = price
                stop_p    = k0.high + self._eff_sl_offset(k0.close, self.short_sl_offset)
                wick_type = self._classify_short_k0_wick(k0)
                if not self._is_short_wick_enabled(wick_type):
                    continue
                if fill_p >= stop_p:
                    continue
                target_p = self._compute_short_target(scenario, vp, k0, fill_p)
                if not self._fee_distance_ok_short(fill_p, target_p, k0.close):
                    continue
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

    # ── Long exit: tick mode ──────────────────────────────────────────────────
    def _tick_exit_long_avs(
        self,
        k: Kline,
        tick_map: TickBarMap,
        signals: List[StrategySignal],
        target_price: float,
        is_rejection: bool,
    ) -> bool:
        ticks = tick_map.get(k.open_time)
        if ticks is None or len(ticks) == 0:
            if not self.allow_bar_fallback_in_tick_mode:
                return False
            self._fallback_bar_count += 1
            return self._bar_exit_simple_long_avs(k, signals, target_price, is_rejection)

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
                if is_rejection:
                    # TP1: switch to trailing with break-even stop
                    self._trailing   = True
                    self._stop_price = self._entry_price
                    self._td_consec  = 0
                else:
                    # Break & retest mode A/B: hard exit
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

    # ── Long exit: bar mode ───────────────────────────────────────────────────
    def _bar_exit_long_avs(
        self,
        k: Kline,
        signals: List[StrategySignal],
        stop_price: float,
        target_price: float,
        trailing: bool,
        td_consec: int,
        is_rejection: bool,
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
            if is_rejection:
                # TP1: switch to trailing with break-even stop
                trailing   = True
                stop_price = self._entry_price
                td_consec  = 0
            else:
                signals.append(StrategySignal(
                    open_time=k.open_time,
                    price=target_price,
                    signal_type="long_exit",
                    label="TP",
                ))
                return True, trailing, td_consec, stop_price

        return False, trailing, td_consec, stop_price

    def _bar_exit_simple_long_avs(
        self,
        k: Kline,
        signals: List[StrategySignal],
        target_price: float,
        is_rejection: bool,
    ) -> bool:
        exited, self._trailing, self._td_consec, self._stop_price = self._bar_exit_long_avs(
            k, signals, self._stop_price, target_price,
            self._trailing, self._td_consec, is_rejection,
        )
        return exited

    # ── Short exit: tick mode ─────────────────────────────────────────────────
    def _tick_exit_short_avs(
        self,
        k: Kline,
        tick_map: TickBarMap,
        signals: List[StrategySignal],
        target_price: float,
        is_rejection: bool,
    ) -> bool:
        ticks = tick_map.get(k.open_time)
        if ticks is None or len(ticks) == 0:
            if not self.allow_bar_fallback_in_tick_mode:
                return False
            self._fallback_bar_count += 1
            return self._bar_exit_simple_short_avs(k, signals, target_price, is_rejection)

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
                if is_rejection:
                    # TP1: switch to trailing with break-even stop
                    self._trailing   = True
                    self._stop_price = self._entry_price
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

    # ── Short exit: bar mode ──────────────────────────────────────────────────
    def _bar_exit_short_avs(
        self,
        k: Kline,
        signals: List[StrategySignal],
        stop_price: float,
        target_price: float,
        trailing: bool,
        td_consec: int,
        is_rejection: bool,
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
            if is_rejection:
                # TP1: switch to trailing with break-even stop
                trailing   = True
                stop_price = self._entry_price
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

    def _bar_exit_simple_short_avs(
        self,
        k: Kline,
        signals: List[StrategySignal],
        target_price: float,
        is_rejection: bool,
    ) -> bool:
        exited, self._trailing, self._td_consec, self._stop_price = self._bar_exit_short_avs(
            k, signals, self._stop_price, target_price,
            self._trailing, self._td_consec, is_rejection,
        )
        return exited

    # ── Ratio scaling (copied from v4_ratio) ──────────────────────────────────
    def _price_ratio(self, price: float) -> float:
        p    = max(price, self.ratio_map_eps)
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
        ratio       = self._price_ratio(k0_price)
        raw_mult    = ratio ** exponent
        mapped_mult = 1.0 + strength * (raw_mult - 1.0)
        floor       = max(min_mult, self.ratio_map_eps)
        mapped_mult = max(mapped_mult, floor)
        if max_mult > 0:
            mapped_mult = min(mapped_mult, max_mult)
        return mapped_mult

    def _eff_sl_offset(self, k0_price: float, base_offset: float) -> float:
        mult = self._map_with_price_ratio(
            k0_price,
            self.sl_offset_map_exponent,
            self.sl_offset_map_strength,
            self.sl_offset_map_min_mult,
            self.sl_offset_map_max_mult,
        )
        return base_offset * mult

    def _eff_vol_gate(self, k0_price: float, base_gate: float) -> float:
        mult = self._map_with_price_ratio(
            k0_price,
            self.k0_vol_gate_map_exponent,
            self.k0_vol_gate_map_strength,
            self.k0_vol_gate_map_min_mult,
            self.k0_vol_gate_map_max_mult,
        )
        return base_gate * mult

    def _eff_fee_cover(self, k0_price: float, base_ratio: float) -> float:
        mult = self._map_with_price_ratio(
            k0_price,
            self.min_fee_cover_map_exponent,
            self.min_fee_cover_map_strength,
            self.min_fee_cover_map_min_mult,
            self.min_fee_cover_map_max_mult,
        )
        return base_ratio * mult

    def _round_trip_cost(self, price: float) -> float:
        return 2.0 * (self.taker_fee_rate + self.slippage_rate) * price

    def _long_body_floor(self, price: float) -> float:
        return max(price * self.long_body_floor_pct, 1e-9)

    def _short_body_floor(self, price: float) -> float:
        return max(price * self.short_body_floor_pct, 1e-9)

    def _classify_long_k0_wick(self, k0: Kline) -> str:
        body       = abs(k0.close - k0.open)
        lower_wick = min(k0.open, k0.close) - k0.low
        denom      = max(body, self._long_body_floor(k0.close))
        ratio      = lower_wick / denom
        if ratio >= self.long_wick_type_a_threshold:
            return "A"
        if ratio >= self.long_wick_type_b_threshold:
            return "B"
        return "C"

    def _classify_short_k0_wick(self, k0: Kline) -> str:
        body       = abs(k0.close - k0.open)
        upper_wick = k0.high - max(k0.open, k0.close)
        denom      = max(body, self._short_body_floor(k0.close))
        ratio      = upper_wick / denom
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
                i   = getattr(self, "_cur_i", 0)
                kls = getattr(self, "_cur_klines", None)
                if kls is not None and i >= self.short_b_runup_lookback:
                    lb      = i - self.short_b_runup_lookback
                    low_ref = min(kls[j].low for j in range(lb, i))
                    runup   = (k0.high - low_ref) / max(low_ref, 1e-9)
                    if runup < self.short_b_min_runup_pct:
                        return False
            return True
        return True

    def _risk_covers_cost(
        self, entry_price: float, risk: float, rr: float, fee_cover_ratio: float
    ) -> bool:
        if rr <= 0 or risk <= 0 or entry_price <= 0:
            return False
        min_risk = self._round_trip_cost(entry_price) * fee_cover_ratio / rr
        return risk >= min_risk

    def _vol_sma_ok(
        self,
        klines: List[Kline],
        cur_idx: int,
        cur_vol: float,
        period: int,
        mult: float,
    ) -> bool:
        if period <= 0 or cur_idx < period:
            return True
        s   = cur_idx - period
        sma = sum(klines[j].volume for j in range(s, cur_idx)) / period
        return cur_vol > sma * mult

    # ── K0 quality checks ─────────────────────────────────────────────────────
    def _is_k0_long(
        self, k: Kline, ticks: Optional[np.ndarray] = None
    ) -> bool:
        rng = k.high - k.low
        if rng <= 0:
            return False
        if k.volume < self._eff_vol_gate(k.close, self.long_k0_vol_gate):
            return False
        mid        = (k.high + k.low) / 2.0
        body_low   = min(k.open, k.close)
        body       = abs(k.close - k.open)
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
            wick_vol   = float(np.sum(wick_ticks[:, 2]))
            total_vol  = float(np.sum(ticks[:, 2]))
            if wick_vol <= 0 or total_vol <= 0:
                return False
            wick_buy_vol   = float(np.sum(wick_ticks[wick_ticks[:, 3] < 0.5, 2]))
            wick_delta     = 2.0 * wick_buy_vol - wick_vol
            wick_delta_eff = wick_delta / wick_vol
            return (
                wick_vol / total_vol >= self.lower_wick_absorption_min_vol_ratio
                and wick_delta_eff <= self.lower_wick_absorption_delta_eff_max
            )
        return _kline_delta(k) <= self.lower_wick_absorption_bar_delta_max

    def _is_k0_short(
        self, k: Kline, ticks: Optional[np.ndarray] = None
    ) -> bool:
        rng = k.high - k.low
        if rng <= 0:
            return False
        if k.volume < self._eff_vol_gate(k.close, self.short_k0_vol_gate):
            return False
        mid        = (k.high + k.low) / 2.0
        body_high  = max(k.open, k.close)
        body       = abs(k.close - k.open)
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
            wick_vol   = float(np.sum(wick_ticks[:, 2]))
            total_vol  = float(np.sum(ticks[:, 2]))
            if wick_vol <= 0 or total_vol <= 0:
                return False
            wick_buy_vol   = float(np.sum(wick_ticks[wick_ticks[:, 3] < 0.5, 2]))
            wick_delta     = 2.0 * wick_buy_vol - wick_vol
            wick_delta_eff = wick_delta / wick_vol
            return (
                wick_vol / total_vol >= self.upper_wick_absorption_min_vol_ratio
                and wick_delta_eff >= self.upper_wick_absorption_delta_eff_min
            )
        return _kline_delta(k) >= self.upper_wick_absorption_bar_delta_min
