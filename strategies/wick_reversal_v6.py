"""
Wick Reversal v6 – 15 m, tick-first, session-filtered.

k0 shape (long):
  range > ATR(14) * atr_range_mult
  lower_wick >= body * wick_body_ratio   (body floored)
  upper_wick < range * opposite_wick_cap
  k0_low < min(past N bars' lows)  — N = clamp(round(base_n * ATR/SMA_ATR), min_n, max_n)
  wick-zone engine in lower-wick zone (tick-level if available, else bar delta)

Entry (long):
  Zoom window: zoom_bars bars after k0
  Kill condition: any tick (or bar.low) < k0_body_low → setup invalid
  Trigger: tick price > k0_body_high AND price <= max_entry_price
  max_entry_price = k0_high + range * entry_extension_a

Stop / target:
  stop  = k0_low  − range * stop_extension_b
  risk  = fill_price − stop
  target= fill_price + risk * rr

Entry filter (tick mode):
  zoom_delta_eff = (2×zcbv − zcv)/zcv must exceed zoom_entry_delta_eff_threshold (long)
  or be below −zoom_entry_delta_eff_threshold (short)

Trailing (trade-level cum_delta, cross-bar accumulation from entry):
  On TP touch:  cum_delta <= 0 → direct TP exit
                cum_delta >  0 → trailing; stop ← entry + round_trip_cost (breakeven)
  In trailing (tick-level): exit if cum_delta <= 0
                              OR cum_delta < peak × (1 − trade_delta_drawdown_pct)

Short is mirror of long.
"""
# Current implementation: trailing is v4-compatible. TP-touch momentum is based on
# the current bar's final/rolling cum_delta, the trailing stop moves to target_p,
# and TD requires td_consec_bars completed reverse-delta bars.
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

import numpy as np

from core.data_types import Kline
from strategies.base import StrategyBase, StrategySignal, TickBarMap
from strategies import register


# ── module-level helpers ──────────────────────────────────────────────────────

def _range(k: Kline) -> float:
    return k.high - k.low

def _body(k: Kline) -> float:
    return abs(k.close - k.open)

def _bhi(k: Kline) -> float:
    return max(k.open, k.close)

def _blo(k: Kline) -> float:
    return min(k.open, k.close)


def _in_session(ms: int) -> bool:
    """Return True if UTC hour falls in Asia [0,8), London [7,16), or NY [13,22)."""
    h = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).hour
    return h < 8 or 7 <= h < 16 or 13 <= h < 22


def _atr_series(klines: List[Kline], period: int) -> List[float]:
    n = len(klines)
    tr = [0.0] * n
    for i in range(n):
        rng = klines[i].high - klines[i].low
        if i == 0:
            tr[i] = rng
        else:
            tr[i] = max(rng,
                        abs(klines[i].high - klines[i - 1].close),
                        abs(klines[i].low  - klines[i - 1].close))
    out, s = [0.0] * n, 0.0
    for i in range(n):
        s += tr[i]
        if i >= period:
            s -= tr[i - period]
        out[i] = s / min(i + 1, period)
    return out


def _sma_series(vals: List[float], period: int) -> List[float]:
    n = len(vals)
    out, s = [0.0] * n, 0.0
    for i in range(n):
        s += vals[i]
        if i >= period:
            s -= vals[i - period]
        out[i] = s / min(i + 1, period)
    return out


def _percentile_rank(vals: List[float], idx: int, lookback: int) -> float:
    start = max(0, idx - lookback + 1)
    sample = [v for v in vals[start:idx + 1] if v > 0]
    if not sample:
        return 0.0
    cur = vals[idx]
    return sum(1 for v in sample if v <= cur) / len(sample) * 100.0


_BODY_FLOOR_PCT = 1e-5


@register
class WickReversalV6Strategy(StrategyBase):
    # NOTE:
    # - k0 wick engine supports both Absorb and Initiative classifications.
    # - zoom_delta_eff gate filters entry: buy momentum must exceed threshold across zoom window.
    # - trailing stop moves to target price after favorable TP-touch momentum.
    # - TD is evaluated after each bar with td_consec_bars reverse-delta tolerance.
    name = "Wick Reversal 15m v6"
    allow_bar_fallback_in_tick_mode: bool = False

    # ATR / dynamic N
    atr_period:     int   = 14
    sma_atr_period: int   = 100
    base_n:         int   = 24
    min_n:          int   = 12
    max_n:          int   = 48

    # k0 shape
    # atr_range_mult:    float = 1.2
    atr_range_mult:    float = 1.1
    # wick_body_ratio:   float = 5.0
    wick_body_ratio:   float = 3.0
    opposite_wick_cap: float = 0.1
    wick_min_vol_ratio: float = 0.15
    initiative_delta_eff_threshold: float = 0.4

    # entry / stop
    # zoom_bars:         int   = 4
    zoom_bars:         int   = 1
    entry_extension_a: float = 0.25
    stop_extension_b:  float = 0.10
    rr:                float = 2.5
    fee_cover_ratio:   float = 3
    td_consec_bars:    int   = 2
    # zoom_entry_delta_eff_threshold: float = 0.2
    zoom_entry_delta_eff_threshold: float = 0.3
    trade_delta_drawdown_pct:       float = 0.3

    # cost (may be overridden by configure_backtest_costs)
    taker_fee_rate: float = 0.00032
    slippage_rate:  float = 0.00002

    # toggles
    enable_long:          bool = True
    enable_short:         bool = True
    enable_session_filter: bool = True

    # mutable state — initialised in on_history; class defaults prevent AttributeError
    _td_consec:           int   = 0
    _trailing:            bool  = False
    _stop_price:          float = 0.0
    _entry_price:         float = 0.0
    _tcv:                 float = 0.0
    _tcbv:                float = 0.0
    _tcd:                 float = 0.0
    _zcv:                 float = 0.0
    _zcbv:                float = 0.0
    _peak_trade_delta:    float = 0.0
    _fallback_bar_count:  int   = 0
    _entry_risk:          float = 0.0
    _mae:                 float = 0.0
    _mfe:                 float = 0.0

    # ── Phase B hook ──────────────────────────────────────────────────────────

    def configure_backtest_costs(self, fee_rate: float, slippage_bps: float) -> None:
        """Sync fee/slippage with backtest config so _risk_ok uses the same basis."""
        self.taker_fee_rate = fee_rate
        self.slippage_rate = slippage_bps * 1e-4

    # ── cost helpers ──────────────────────────────────────────────────────────

    def _rt_rate(self) -> float:
        return 2.0 * (self.taker_fee_rate + self.slippage_rate)

    def _rt_cost(self, price: float) -> float:
        return self._rt_rate() * price

    def _risk_ok(self, price: float, risk: float) -> bool:
        return risk > 0 and (risk * self.rr) >= self._rt_cost(price) * self.fee_cover_ratio

    def _bfloor(self, price: float) -> float:
        return max(price * _BODY_FLOOR_PCT, 1e-9)

    def _wick_volume_ratio(
        self,
        ticks: Optional[np.ndarray],
        is_long: bool,
        boundary: float,
    ) -> float | str:
        if ticks is None or len(ticks) == 0:
            return ""
        total = float(np.sum(ticks[:, 2]))
        if total <= 0:
            return ""
        wt = ticks[ticks[:, 1] <= boundary] if is_long else ticks[ticks[:, 1] >= boundary]
        if len(wt) == 0:
            return 0.0
        return float(np.sum(wt[:, 2])) / total

    def _k0_meta(
        self,
        *,
        side: str,
        wick_type: str,
        k: Kline,
        i: int,
        klines: List[Kline],
        atr_s: List[float],
        ticks: Optional[np.ndarray],
    ) -> dict[str, Any]:
        atr = atr_s[i] if i < len(atr_s) else 0.0
        is_long = side == "long"
        boundary = _blo(k) if is_long else _bhi(k)
        try:
            from core.regime import detect_regime
            trend_regime = detect_regime(klines[max(0, i - 49): i + 1])
        except Exception:
            trend_regime = ""
        return {
            "side": side,
            "wick_type": wick_type,
            "session_hour": datetime.fromtimestamp(k.open_time / 1000.0, tz=timezone.utc).hour,
            "atr_percentile": _percentile_rank(atr_s, i, self.sma_atr_period),
            "trend_regime": trend_regime,
            "k0_range_atr": (_range(k) / atr) if atr > 0 else "",
            "wick_volume_ratio": self._wick_volume_ratio(ticks, is_long, boundary),
        }

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

    def _exit_meta(self) -> dict[str, float]:
        return {"MAE": self._mae, "MFE": self._mfe}

    # ── dynamic N ────────────────────────────────────────────────────────────

    def _dyn_n(self, atr: float, sma: float) -> int:
        ratio = (atr / sma) if sma > 0 else 1.0
        return max(self.min_n, min(self.max_n, round(self.base_n * ratio)))

    # ── absorption filters ────────────────────────────────────────────────────

    def _abs_long(self, k: Kline, ticks: Optional[np.ndarray], blo: float) -> Tuple[bool, str]:
        if ticks is not None and len(ticks) > 0:
            wt = ticks[ticks[:, 1] <= blo]
            if len(wt) == 0:
                return False, ""
            wvol = float(np.sum(wt[:, 2]))
            tvol = float(np.sum(ticks[:, 2]))
            if wvol <= 0 or tvol <= 0:
                return False, ""
            if (wvol / tvol) < self.wick_min_vol_ratio:
                return False, ""
            wbuy = float(np.sum(wt[wt[:, 3] < 0.5, 2]))
            wick_delta_eff = (2.0 * wbuy - wvol) / wvol
            if wick_delta_eff <= 0.0:
                return True, "Absorb"
            if wick_delta_eff >= self.initiative_delta_eff_threshold:
                return True, "Initiative"
            return False, ""

        # Bar-level fallback: use whole-bar delta_eff thresholds.
        if k.volume <= 0:
            return False, ""
        wick_delta_eff = (2.0 * k.taker_buy_volume - k.volume) / k.volume
        if wick_delta_eff <= 0.0:
            return True, "Absorb"
        if wick_delta_eff >= self.initiative_delta_eff_threshold:
            return True, "Initiative"
        return False, ""

    def _abs_short(self, k: Kline, ticks: Optional[np.ndarray], bhi: float) -> Tuple[bool, str]:
        if ticks is not None and len(ticks) > 0:
            wt = ticks[ticks[:, 1] >= bhi]
            if len(wt) == 0:
                return False, ""
            wvol = float(np.sum(wt[:, 2]))
            tvol = float(np.sum(ticks[:, 2]))
            if wvol <= 0 or tvol <= 0:
                return False, ""
            if (wvol / tvol) < self.wick_min_vol_ratio:
                return False, ""
            wbuy = float(np.sum(wt[wt[:, 3] < 0.5, 2]))
            wick_delta_eff = (2.0 * wbuy - wvol) / wvol
            if wick_delta_eff >= 0.0:
                return True, "Absorb"
            if wick_delta_eff <= -self.initiative_delta_eff_threshold:
                return True, "Initiative"
            return False, ""

        # Bar-level fallback: use whole-bar delta_eff thresholds.
        if k.volume <= 0:
            return False, ""
        wick_delta_eff = (2.0 * k.taker_buy_volume - k.volume) / k.volume
        if wick_delta_eff >= 0.0:
            return True, "Absorb"
        if wick_delta_eff <= -self.initiative_delta_eff_threshold:
            return True, "Initiative"
        return False, ""

    # ── k0 detection ─────────────────────────────────────────────────────────

    def _is_k0_long(
        self,
        k: Kline,
        i: int,
        klines: List[Kline],
        atr: float,
        n: int,
        ticks: Optional[np.ndarray],
    ) -> Tuple[bool, str]:
        rng = _range(k)
        if rng <= atr * self.atr_range_mult:
            return False, ""
        blo = _blo(k)
        lw = blo - k.low
        if lw <= 0:
            return False, ""
        if lw < max(_body(k), self._bfloor(k.close)) * self.wick_body_ratio:
            return False, ""
        if (k.high - _bhi(k)) >= rng * self.opposite_wick_cap:
            return False, ""
        if i < n:
            return False, ""
        if k.low >= min(klines[j].low for j in range(i - n, i)):
            return False, ""
        return self._abs_long(k, ticks, blo)

    def _is_k0_short(
        self,
        k: Kline,
        i: int,
        klines: List[Kline],
        atr: float,
        n: int,
        ticks: Optional[np.ndarray],
    ) -> Tuple[bool, str]:
        rng = _range(k)
        if rng <= atr * self.atr_range_mult:
            return False, ""
        bhi = _bhi(k)
        uw = k.high - bhi
        if uw <= 0:
            return False, ""
        if uw < max(_body(k), self._bfloor(k.close)) * self.wick_body_ratio:
            return False, ""
        if (_blo(k) - k.low) >= rng * self.opposite_wick_cap:
            return False, ""
        if i < n:
            return False, ""
        if k.high <= max(klines[j].high for j in range(i - n, i)):
            return False, ""
        return self._abs_short(k, ticks, bhi)

    # ── entry ─────────────────────────────────────────────────────────────────

    def _try_entry_long(
        self,
        k: Kline,
        tick_map: Optional[TickBarMap],
        signals: List[StrategySignal],
        k0: Kline,
        use_ticks: bool,
        k0_meta: Optional[dict[str, Any]] = None,
        entry_delay_bars: int | str = "",
    ) -> tuple:
        """Return (entered, setup_killed, fill_p, stop_p, target_p)."""
        k0_rng = _range(k0)
        bhi = _bhi(k0)
        blo = _blo(k0)
        max_entry = k0.high + k0_rng * self.entry_extension_a
        stop_p = k0.low - k0_rng * self.stop_extension_b

        if use_ticks and tick_map is not None:
            ticks = tick_map.get(k.open_time)
            if ticks is not None and len(ticks) > 0:
                for t in ticks:
                    price = float(t[1])
                    qty   = float(t[2])
                    is_bm = t[3] > 0.5
                    # Accumulate all ticks into zoom window delta.
                    self._zcv += qty
                    if not is_bm:
                        self._zcbv += qty
                    if price < blo:
                        return False, True, 0.0, 0.0, 0.0
                    if price > bhi:
                        if price > max_entry:
                            continue
                        risk = price - stop_p
                        if not self._risk_ok(price, risk):
                            continue
                        zoom_de = (2.0 * self._zcbv - self._zcv) / self._zcv if self._zcv > 0 else 0.0
                        if zoom_de <= self.zoom_entry_delta_eff_threshold:
                            continue
                        tp = price + risk * self.rr
                        meta = dict(k0_meta or {})
                        meta.update({"entry_delay_bars": entry_delay_bars, "zoom_delta_eff": zoom_de})
                        signals.append(StrategySignal(
                            open_time=k.open_time, price=bhi,
                            signal_type="long_entry", label="L6",
                            stop_price=stop_p, fill_price=price,
                            fill_time=int(t[0]), meta=meta,
                        ))
                        return True, False, price, stop_p, tp
                return False, False, 0.0, 0.0, 0.0
            if not self.allow_bar_fallback_in_tick_mode:
                return False, False, 0.0, 0.0, 0.0
            self._fallback_bar_count += 1

        # bar mode
        if k.low < blo:
            return False, True, 0.0, 0.0, 0.0
        if k.high < bhi:
            return False, False, 0.0, 0.0, 0.0
        entry_p = bhi
        risk = entry_p - stop_p
        if not self._risk_ok(entry_p, risk):
            return False, False, 0.0, 0.0, 0.0
        tp = entry_p + risk * self.rr
        meta = dict(k0_meta or {})
        meta.update({"entry_delay_bars": entry_delay_bars, "zoom_delta_eff": ""})
        signals.append(StrategySignal(
            open_time=k.open_time, price=entry_p,
            signal_type="long_entry", label="L6",
            stop_price=stop_p,
            meta=meta,
        ))
        return True, False, entry_p, stop_p, tp

    def _try_entry_short(
        self,
        k: Kline,
        tick_map: Optional[TickBarMap],
        signals: List[StrategySignal],
        k0: Kline,
        use_ticks: bool,
        k0_meta: Optional[dict[str, Any]] = None,
        entry_delay_bars: int | str = "",
    ) -> tuple:
        """Return (entered, setup_killed, fill_p, stop_p, target_p)."""
        k0_rng = _range(k0)
        bhi = _bhi(k0)
        blo = _blo(k0)
        min_entry = k0.low - k0_rng * self.entry_extension_a
        stop_p = k0.high + k0_rng * self.stop_extension_b

        if use_ticks and tick_map is not None:
            ticks = tick_map.get(k.open_time)
            if ticks is not None and len(ticks) > 0:
                for t in ticks:
                    price = float(t[1])
                    qty   = float(t[2])
                    is_bm = t[3] > 0.5
                    # Accumulate all ticks into zoom window delta.
                    self._zcv += qty
                    if not is_bm:
                        self._zcbv += qty
                    if price > bhi:
                        return False, True, 0.0, 0.0, 0.0
                    if price < blo:
                        if price < min_entry:
                            continue
                        risk = stop_p - price
                        if not self._risk_ok(price, risk):
                            continue
                        zoom_de = (2.0 * self._zcbv - self._zcv) / self._zcv if self._zcv > 0 else 0.0
                        if zoom_de >= -self.zoom_entry_delta_eff_threshold:
                            continue
                        tp = price - risk * self.rr
                        meta = dict(k0_meta or {})
                        meta.update({"entry_delay_bars": entry_delay_bars, "zoom_delta_eff": zoom_de})
                        signals.append(StrategySignal(
                            open_time=k.open_time, price=blo,
                            signal_type="short_entry", label="S6",
                            stop_price=stop_p, fill_price=price,
                            fill_time=int(t[0]), meta=meta,
                        ))
                        return True, False, price, stop_p, tp
                return False, False, 0.0, 0.0, 0.0
            if not self.allow_bar_fallback_in_tick_mode:
                return False, False, 0.0, 0.0, 0.0
            self._fallback_bar_count += 1

        # bar mode
        if k.high > bhi:
            return False, True, 0.0, 0.0, 0.0
        if k.low > blo:
            return False, False, 0.0, 0.0, 0.0
        entry_p = blo
        risk = stop_p - entry_p
        if not self._risk_ok(entry_p, risk):
            return False, False, 0.0, 0.0, 0.0
        tp = entry_p - risk * self.rr
        meta = dict(k0_meta or {})
        meta.update({"entry_delay_bars": entry_delay_bars, "zoom_delta_eff": ""})
        signals.append(StrategySignal(
            open_time=k.open_time, price=entry_p,
            signal_type="short_entry", label="S6",
            stop_price=stop_p,
            meta=meta,
        ))
        return True, False, entry_p, stop_p, tp

    # ── exit: tick mode ───────────────────────────────────────────────────────

    def _tick_exit_long(
        self,
        k: Kline,
        tick_map: Optional[TickBarMap],
        signals: List[StrategySignal],
        target_p: float,
    ) -> bool:
        ticks = tick_map.get(k.open_time) if tick_map else None
        if ticks is None or len(ticks) == 0:
            if not self.allow_bar_fallback_in_tick_mode:
                return False
            self._fallback_bar_count += 1
            return self._bar_exit_long(k, signals, target_p)

        cum_buy_vol = 0.0
        cum_vol = 0.0
        cum_delta = 0.0

        for t in ticks:
            price = float(t[1])
            qty   = float(t[2])
            is_bm = t[3] > 0.5
            self._update_excursion(price, price, "long")

            cum_vol += qty
            if not is_bm:
                cum_buy_vol += qty
            cum_delta = 2.0 * cum_buy_vol - cum_vol

            if price <= self._stop_price:
                lbl = "TS" if self._trailing else "SL"
                signals.append(StrategySignal(
                    open_time=k.open_time, price=self._stop_price,
                    signal_type="long_exit", label=lbl,
                    fill_price=price, fill_time=int(t[0]),
                    meta=self._exit_meta(),
                ))
                return True

            if self._trailing:
                continue

            if price >= target_p:
                if cum_delta > 0:
                    self._trailing = True
                    self._stop_price = target_p
                    self._td_consec = 0
                else:
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=target_p,
                        signal_type="long_exit", label="TP",
                        fill_time=int(t[0]),
                        meta=self._exit_meta(),
                    ))
                    return True

        self._tcd = cum_delta
        if self._trailing:
            if cum_delta <= 0:
                self._td_consec += 1
                if self._td_consec >= self.td_consec_bars:
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=k.close,
                        signal_type="long_exit", label="TD",
                        meta=self._exit_meta(),
                    ))
                    return True
            else:
                self._td_consec = 0
        return False

    def _tick_exit_short(
        self,
        k: Kline,
        tick_map: Optional[TickBarMap],
        signals: List[StrategySignal],
        target_p: float,
    ) -> bool:
        ticks = tick_map.get(k.open_time) if tick_map else None
        if ticks is None or len(ticks) == 0:
            if not self.allow_bar_fallback_in_tick_mode:
                return False
            self._fallback_bar_count += 1
            return self._bar_exit_short(k, signals, target_p)

        cum_buy_vol = 0.0
        cum_vol = 0.0
        cum_delta = 0.0

        for t in ticks:
            price = float(t[1])
            qty   = float(t[2])
            is_bm = t[3] > 0.5
            self._update_excursion(price, price, "short")

            cum_vol += qty
            if not is_bm:
                cum_buy_vol += qty
            cum_delta = 2.0 * cum_buy_vol - cum_vol

            if price >= self._stop_price:
                lbl = "TS" if self._trailing else "SL"
                signals.append(StrategySignal(
                    open_time=k.open_time, price=self._stop_price,
                    signal_type="short_exit", label=lbl,
                    fill_price=price, fill_time=int(t[0]),
                    meta=self._exit_meta(),
                ))
                return True

            if self._trailing:
                continue

            if price <= target_p:
                if cum_delta < 0:
                    self._trailing = True
                    self._stop_price = target_p
                    self._td_consec = 0
                else:
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=target_p,
                        signal_type="short_exit", label="TP",
                        fill_time=int(t[0]),
                        meta=self._exit_meta(),
                    ))
                    return True

        self._tcd = cum_delta
        if self._trailing:
            if cum_delta >= 0:
                self._td_consec += 1
                if self._td_consec >= self.td_consec_bars:
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=k.close,
                        signal_type="short_exit", label="TD",
                        meta=self._exit_meta(),
                    ))
                    return True
            else:
                self._td_consec = 0
        return False

    # ── exit: bar mode (fallback approximation) ───────────────────────────────

    def _bar_exit_long(
        self,
        k: Kline,
        signals: List[StrategySignal],
        target_p: float,
    ) -> bool:
        bar_delta = _kline_delta(k)
        self._tcd = bar_delta
        self._update_excursion(k.high, k.low, "long")

        if k.low <= self._stop_price:
            lbl = "TS" if self._trailing else "SL"
            signals.append(StrategySignal(
                open_time=k.open_time, price=self._stop_price,
                signal_type="long_exit", label=lbl,
                meta=self._exit_meta(),
            ))
            return True
        if self._trailing:
            if bar_delta <= 0:
                self._td_consec += 1
                if self._td_consec >= self.td_consec_bars:
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=k.close,
                        signal_type="long_exit", label="TD",
                        meta=self._exit_meta(),
                    ))
                    return True
            else:
                self._td_consec = 0
        elif k.high >= target_p:
            if bar_delta > 0:
                self._trailing = True
                self._stop_price = target_p
                self._td_consec = 0
            else:
                signals.append(StrategySignal(
                    open_time=k.open_time, price=target_p,
                    signal_type="long_exit", label="TP",
                    meta=self._exit_meta(),
                ))
                return True
        return False

    def _bar_exit_short(
        self,
        k: Kline,
        signals: List[StrategySignal],
        target_p: float,
    ) -> bool:
        bar_delta = _kline_delta(k)
        self._tcd = bar_delta
        self._update_excursion(k.high, k.low, "short")

        if k.high >= self._stop_price:
            lbl = "TS" if self._trailing else "SL"
            signals.append(StrategySignal(
                open_time=k.open_time, price=self._stop_price,
                signal_type="short_exit", label=lbl,
                meta=self._exit_meta(),
            ))
            return True
        if self._trailing:
            if bar_delta >= 0:
                self._td_consec += 1
                if self._td_consec >= self.td_consec_bars:
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=k.close,
                        signal_type="short_exit", label="TD",
                        meta=self._exit_meta(),
                    ))
                    return True
            else:
                self._td_consec = 0
        elif k.low <= target_p:
            if bar_delta < 0:
                self._trailing = True
                self._stop_price = target_p
                self._td_consec = 0
            else:
                signals.append(StrategySignal(
                    open_time=k.open_time, price=target_p,
                    signal_type="short_exit", label="TP",
                    meta=self._exit_meta(),
                ))
                return True
        return False

    # ── main loop ─────────────────────────────────────────────────────────────

    def on_history(
        self,
        klines: List[Kline],
        tick_map: Optional[TickBarMap] = None,
    ) -> List[StrategySignal]:
        signals: List[StrategySignal] = []
        nb = len(klines)
        min_req = max(self.atr_period, self.sma_atr_period) + 1
        if nb < min_req:
            return signals

        use_ticks = tick_map is not None and len(tick_map) > 0

        atr_s = _atr_series(klines, self.atr_period)
        sma_s = _sma_series(atr_s, self.sma_atr_period)

        long_k0: Optional[Kline] = None
        long_k0_idx = -1
        long_k0_meta: dict[str, Any] = {}
        short_k0: Optional[Kline] = None
        short_k0_idx = -1
        short_k0_meta: dict[str, Any] = {}

        in_pos = False
        side = ""
        target_p = 0.0

        self._trailing = False
        self._stop_price = 0.0
        self._entry_price = 0.0
        self._tcv = 0.0            # trade-level cum vol (cross-bar from entry)
        self._tcbv = 0.0           # trade-level cum buy vol
        self._tcd = 0.0            # snapshot of last trade_delta (for logging)
        self._zcv = 0.0            # zoom window cum vol (resets on new k0)
        self._zcbv = 0.0           # zoom window cum buy vol
        self._peak_trade_delta = 0.0  # high-water mark of trade_delta (long) / low-water (short)
        self._fallback_bar_count = 0
        self._td_consec = 0
        self._entry_risk = 0.0
        self._mae = 0.0
        self._mfe = 0.0

        for i, k in enumerate(klines):
            atr = atr_s[i]
            sma = sma_s[i]
            n = self._dyn_n(atr, sma)

            # ── manage open position ──────────────────────────────────────
            if in_pos:
                if side == "long":
                    exited = (self._tick_exit_long(k, tick_map, signals, target_p)
                              if use_ticks else
                              self._bar_exit_long(k, signals, target_p))
                else:
                    exited = (self._tick_exit_short(k, tick_map, signals, target_p)
                              if use_ticks else
                              self._bar_exit_short(k, signals, target_p))
                if exited:
                    in_pos = False
                    side = ""
                    long_k0 = None
                    short_k0 = None
                    long_k0_meta = {}
                    short_k0_meta = {}
                    self._trailing = False
                    self._tcv = self._tcbv = self._tcd = 0.0
                    self._peak_trade_delta = 0.0
                    self._td_consec = 0
                    self._entry_risk = 0.0
                    self._mae = 0.0
                    self._mfe = 0.0
                else:
                    continue

            # ── long zoom entry ───────────────────────────────────────────
            if long_k0 is not None and i > long_k0_idx:
                if i - long_k0_idx > self.zoom_bars:
                    long_k0 = None
                    long_k0_meta = {}
                else:
                    entered, killed, fp, sp, tp = self._try_entry_long(
                        k, tick_map, signals, long_k0, use_ticks,
                        long_k0_meta, i - long_k0_idx)
                    if killed:
                        long_k0 = None
                        long_k0_meta = {}
                    elif entered:
                        in_pos = True
                        side = "long"
                        target_p = tp
                        self._entry_price = fp
                        self._entry_risk = abs(fp - sp)
                        self._mae = 0.0
                        self._mfe = 0.0
                        self._stop_price = sp
                        self._trailing = False
                        self._tcv = self._tcbv = self._tcd = 0.0
                        self._peak_trade_delta = 0.0
                        self._td_consec = 0
                        long_k0 = None
                        short_k0 = None
                        long_k0_meta = {}
                        short_k0_meta = {}
                        continue

            # ── short zoom entry ──────────────────────────────────────────
            if short_k0 is not None and i > short_k0_idx:
                if i - short_k0_idx > self.zoom_bars:
                    short_k0 = None
                    short_k0_meta = {}
                else:
                    entered, killed, fp, sp, tp = self._try_entry_short(
                        k, tick_map, signals, short_k0, use_ticks,
                        short_k0_meta, i - short_k0_idx)
                    if killed:
                        short_k0 = None
                        short_k0_meta = {}
                    elif entered:
                        in_pos = True
                        side = "short"
                        target_p = tp
                        self._entry_price = fp
                        self._entry_risk = abs(sp - fp)
                        self._mae = 0.0
                        self._mfe = 0.0
                        self._stop_price = sp
                        self._trailing = False
                        self._tcv = self._tcbv = self._tcd = 0.0
                        self._peak_trade_delta = 0.0
                        self._td_consec = 0
                        short_k0 = None
                        long_k0 = None
                        short_k0_meta = {}
                        long_k0_meta = {}
                        continue

            # ── k0 detection ──────────────────────────────────────────────
            if not in_pos:
                if self.enable_session_filter and not _in_session(k.open_time):
                    continue
                ticks = tick_map.get(k.open_time) if tick_map else None
                long_ok, long_wick_type = self._is_k0_long(k, i, klines, atr, n, ticks)
                if self.enable_long and long_ok:
                    long_k0 = k
                    long_k0_idx = i
                    long_k0_meta = self._k0_meta(
                        side="long", wick_type=long_wick_type, k=k, i=i,
                        klines=klines, atr_s=atr_s, ticks=ticks)
                    self._zcv = 0.0
                    self._zcbv = 0.0
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=k.low,
                        signal_type="k0_long", label=f"k0_{long_wick_type}",
                    ))
                short_ok, short_wick_type = self._is_k0_short(k, i, klines, atr, n, ticks)
                if self.enable_short and short_ok:
                    short_k0 = k
                    short_k0_idx = i
                    short_k0_meta = self._k0_meta(
                        side="short", wick_type=short_wick_type, k=k, i=i,
                        klines=klines, atr_s=atr_s, ticks=ticks)
                    self._zcv = 0.0
                    self._zcbv = 0.0
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=k.high,
                        signal_type="k0_short", label=f"k0s_{short_wick_type}",
                    ))

        return signals


@register
class WickReversalV6_1mStrategy(WickReversalV6Strategy):
    """1m variant of v6 with the same core logic and independent strategy name."""
    name = "Wick Reversal 1m v6"
