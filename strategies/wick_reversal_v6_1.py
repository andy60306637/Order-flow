from __future__ import annotations

from typing import Any, List, Optional, Tuple
import numpy as np

from core.data_types import Kline
from strategies.base import StrategySignal, TickBarMap
from strategies import register
from strategies.wick_reversal_v6 import (
    WickReversalV6Strategy, _range, _bhi, _blo, _in_session, _atr_series, _sma_series,
)

# Helpers missing from v6 exports but needed
def _kline_delta(k: Kline) -> float:
    return 2.0 * k.taker_buy_volume - k.volume

@register
class WickReversalV6_1Strategy(WickReversalV6Strategy):
    name = "Wick Reversal 15m v6.1"
    
    # New parameters for v6.1
    entry_atr_cap: float = 0.35
    stop_atr_mult: float = 0.25
    trailing_stop_mode: str = "lock_tp"
    
    def _activate_trailing(self, side: str, target_p: float) -> None:
        self._trailing = True
        self._td_consec = 0
        if side == "long":
            self._peak_trade_delta = max(0.0, self._tcd)
        else:
            self._peak_trade_delta = min(0.0, self._tcd)
        if self.trailing_stop_mode == "breakeven_cost":
            if side == "long":
                self._stop_price = self._entry_price + self._rt_cost(self._entry_price)
            else:
                self._stop_price = self._entry_price - self._rt_cost(self._entry_price)
        else:
            self._stop_price = target_p

    def _exit_meta(self) -> dict:
        risk = self._entry_risk
        mae = self._mae * risk
        mfe = self._mfe * risk
        return {
            "MAE":        self._mae,
            "MFE":        self._mfe,
            "mae":        mae,
            "mfe":        mfe,
            "mae_r":      self._mae,
            "mfe_r":      self._mfe,
            "entry_risk": risk,
        }

    # =========================================================================
    # Entry
    # =========================================================================
    def _try_entry_long(
        self,
        k: Kline,
        tick_map: Optional[TickBarMap],
        signals: List[StrategySignal],
        k0: Kline,
        use_ticks: bool,
        atr: float,
        k0_meta: Optional[dict[str, Any]] = None,
        entry_delay_bars: int | str = "",
    ) -> tuple:
        k0_rng = _range(k0)
        body_high = max(k0.open, k0.close)
        blo = _blo(k0)
        
        # New Entry Zone: body reclaim + ATR cap
        entry_cap = min(k0_rng * self.entry_extension_a, atr * self.entry_atr_cap)
        max_entry = body_high + entry_cap
        
        # Hybrid Stop
        range_stop = k0.low - k0_rng * self.stop_extension_b
        atr_stop = k0.low - atr * self.stop_atr_mult
        stop_p = min(range_stop, atr_stop)

        if use_ticks and tick_map is not None:
            ticks = tick_map.get(k.open_time)
            if ticks is not None and len(ticks) > 0:
                for t in ticks:
                    price = float(t[1])
                    qty   = float(t[2])
                    is_bm = t[3] > 0.5
                    
                    self._zcv += qty
                    if not is_bm:
                        self._zcbv += qty
                        
                    if price < blo:
                        return False, True, 0.0, 0.0, 0.0
                        
                    # Trigger: price > body_high AND price <= max_entry
                    if price > body_high:
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
                            open_time=k.open_time, price=body_high,
                            signal_type="long_entry", label="L6.1",
                            stop_price=stop_p, fill_price=price,
                            fill_time=int(t[0]), meta=meta,
                        ))
                        return True, False, price, stop_p, tp
                return False, False, 0.0, 0.0, 0.0
            if not self.allow_bar_fallback_in_tick_mode:
                return False, False, 0.0, 0.0, 0.0
            self._fallback_bar_count += 1

        # bar mode fallback
        if k.low < blo:
            return False, True, 0.0, 0.0, 0.0
        if k.high < body_high:
            return False, False, 0.0, 0.0, 0.0
            
        entry_p = max(body_high, k.low)
        if entry_p > max_entry:
            return False, False, 0.0, 0.0, 0.0
            
        risk = entry_p - stop_p
        if not self._risk_ok(entry_p, risk):
            return False, False, 0.0, 0.0, 0.0
        tp = entry_p + risk * self.rr
        meta = dict(k0_meta or {})
        meta.update({"entry_delay_bars": entry_delay_bars, "zoom_delta_eff": ""})
        signals.append(StrategySignal(
            open_time=k.open_time, price=entry_p,
            signal_type="long_entry", label="L6.1",
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
        atr: float,
        k0_meta: Optional[dict[str, Any]] = None,
        entry_delay_bars: int | str = "",
    ) -> tuple:
        k0_rng = _range(k0)
        body_low = min(k0.open, k0.close)
        bhi = _bhi(k0)
        
        # New Entry Zone
        entry_cap = min(k0_rng * self.entry_extension_a, atr * self.entry_atr_cap)
        min_entry = body_low - entry_cap
        
        # Hybrid Stop
        range_stop = k0.high + k0_rng * self.stop_extension_b
        atr_stop = k0.high + atr * self.stop_atr_mult
        stop_p = max(range_stop, atr_stop)

        if use_ticks and tick_map is not None:
            ticks = tick_map.get(k.open_time)
            if ticks is not None and len(ticks) > 0:
                for t in ticks:
                    price = float(t[1])
                    qty   = float(t[2])
                    is_bm = t[3] > 0.5
                    
                    self._zcv += qty
                    if not is_bm:
                        self._zcbv += qty
                        
                    if price > bhi:
                        return False, True, 0.0, 0.0, 0.0
                        
                    # Trigger: price < body_low AND price >= min_entry
                    if price < body_low:
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
                            open_time=k.open_time, price=body_low,
                            signal_type="short_entry", label="S6.1",
                            stop_price=stop_p, fill_price=price,
                            fill_time=int(t[0]), meta=meta,
                        ))
                        return True, False, price, stop_p, tp
                return False, False, 0.0, 0.0, 0.0
            if not self.allow_bar_fallback_in_tick_mode:
                return False, False, 0.0, 0.0, 0.0
            self._fallback_bar_count += 1

        # bar mode fallback
        if k.high > bhi:
            return False, True, 0.0, 0.0, 0.0
        if k.low > body_low:
            return False, False, 0.0, 0.0, 0.0
            
        entry_p = min(body_low, k.high)
        if entry_p < min_entry:
            return False, False, 0.0, 0.0, 0.0
            
        risk = stop_p - entry_p
        if not self._risk_ok(entry_p, risk):
            return False, False, 0.0, 0.0, 0.0
        tp = entry_p - risk * self.rr
        meta = dict(k0_meta or {})
        meta.update({"entry_delay_bars": entry_delay_bars, "zoom_delta_eff": ""})
        signals.append(StrategySignal(
            open_time=k.open_time, price=entry_p,
            signal_type="short_entry", label="S6.1",
            stop_price=stop_p,
            meta=meta,
        ))
        return True, False, entry_p, stop_p, tp

    # =========================================================================
    # Exit
    # =========================================================================
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

        for t in ticks:
            price = float(t[1])
            qty   = float(t[2])
            is_bm = t[3] > 0.5
            self._update_excursion(price, price, "long")

            # Trade-level cum_delta update
            self._tcv += qty
            if not is_bm:
                self._tcbv += qty
            self._tcd = 2.0 * self._tcbv - self._tcv
            
            self._peak_trade_delta = max(self._peak_trade_delta, self._tcd)

            if price <= self._stop_price:
                lbl = "TS" if self._trailing else "SL"
                meta = self._exit_meta()
                meta.update({"final_trade_delta": self._tcd, "trailing_stop_mode": self.trailing_stop_mode})
                signals.append(StrategySignal(
                    open_time=k.open_time, price=self._stop_price,
                    signal_type="long_exit", label=lbl,
                    fill_price=price, fill_time=int(t[0]),
                    meta=meta,
                ))
                return True

            if self._trailing:
                if (
                    self._peak_trade_delta > 0
                    and self._tcd < self._peak_trade_delta * (1 - self.trade_delta_drawdown_pct)
                ):
                    meta = self._exit_meta()
                    meta.update({"final_trade_delta": self._tcd, "trailing_stop_mode": self.trailing_stop_mode})
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=price,
                        signal_type="long_exit", label="TDD",
                        fill_price=price, fill_time=int(t[0]),
                        meta=meta,
                    ))
                    return True
                continue

            if price >= target_p:
                if self._tcd > 0:
                    self._activate_trailing("long", target_p)
                else:
                    meta = self._exit_meta()
                    meta.update({"final_trade_delta": self._tcd, "trailing_stop_mode": self.trailing_stop_mode})
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=target_p,
                        signal_type="long_exit", label="TP",
                        fill_time=int(t[0]),
                        meta=meta,
                    ))
                    return True

        if self._trailing:
            if self._tcd <= 0:
                self._td_consec += 1
                if self._td_consec >= self.td_consec_bars:
                    meta = self._exit_meta()
                    meta.update({"final_trade_delta": self._tcd, "trailing_stop_mode": self.trailing_stop_mode})
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=k.close,
                        signal_type="long_exit", label="TD",
                        meta=meta,
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

        for t in ticks:
            price = float(t[1])
            qty   = float(t[2])
            is_bm = t[3] > 0.5
            self._update_excursion(price, price, "short")

            # Trade-level cum_delta update
            self._tcv += qty
            if not is_bm:
                self._tcbv += qty
            self._tcd = 2.0 * self._tcbv - self._tcv
            
            self._peak_trade_delta = min(self._peak_trade_delta, self._tcd)

            if price >= self._stop_price:
                lbl = "TS" if self._trailing else "SL"
                meta = self._exit_meta()
                meta.update({"final_trade_delta": self._tcd, "trailing_stop_mode": self.trailing_stop_mode})
                signals.append(StrategySignal(
                    open_time=k.open_time, price=self._stop_price,
                    signal_type="short_exit", label=lbl,
                    fill_price=price, fill_time=int(t[0]),
                    meta=meta,
                ))
                return True

            if self._trailing:
                if (
                    self._peak_trade_delta < 0
                    and self._tcd > self._peak_trade_delta * (1 - self.trade_delta_drawdown_pct)
                ):
                    meta = self._exit_meta()
                    meta.update({"final_trade_delta": self._tcd, "trailing_stop_mode": self.trailing_stop_mode})
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=price,
                        signal_type="short_exit", label="TDD",
                        fill_price=price, fill_time=int(t[0]),
                        meta=meta,
                    ))
                    return True
                continue

            if price <= target_p:
                if self._tcd < 0:
                    self._activate_trailing("short", target_p)
                else:
                    meta = self._exit_meta()
                    meta.update({"final_trade_delta": self._tcd, "trailing_stop_mode": self.trailing_stop_mode})
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=target_p,
                        signal_type="short_exit", label="TP",
                        fill_time=int(t[0]),
                        meta=meta,
                    ))
                    return True

        if self._trailing:
            if self._tcd >= 0:
                self._td_consec += 1
                if self._td_consec >= self.td_consec_bars:
                    meta = self._exit_meta()
                    meta.update({"final_trade_delta": self._tcd, "trailing_stop_mode": self.trailing_stop_mode})
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=k.close,
                        signal_type="short_exit", label="TD",
                        meta=meta,
                    ))
                    return True
            else:
                self._td_consec = 0
        return False

    def _bar_exit_long(
        self,
        k: Kline,
        signals: List[StrategySignal],
        target_p: float,
    ) -> bool:
        self._update_excursion(k.high, k.low, "long")
        
        self._tcv += k.volume
        self._tcbv += k.taker_buy_volume
        self._tcd = 2.0 * self._tcbv - self._tcv
        
        self._peak_trade_delta = max(self._peak_trade_delta, self._tcd)

        if k.low <= self._stop_price:
            lbl = "TS" if self._trailing else "SL"
            meta = self._exit_meta()
            meta.update({"final_trade_delta": self._tcd, "trailing_stop_mode": self.trailing_stop_mode})
            signals.append(StrategySignal(
                open_time=k.open_time, price=self._stop_price,
                signal_type="long_exit", label=lbl,
                meta=meta,
            ))
            return True
            
        if self._trailing:
            if (
                self._peak_trade_delta > 0
                and self._tcd < self._peak_trade_delta * (1 - self.trade_delta_drawdown_pct)
            ):
                meta = self._exit_meta()
                meta.update({"final_trade_delta": self._tcd, "trailing_stop_mode": self.trailing_stop_mode})
                signals.append(StrategySignal(
                    open_time=k.open_time, price=k.close,
                    signal_type="long_exit", label="TDD",
                    meta=meta,
                ))
                return True
            if self._tcd <= 0:
                self._td_consec += 1
                if self._td_consec >= self.td_consec_bars:
                    meta = self._exit_meta()
                    meta.update({"final_trade_delta": self._tcd, "trailing_stop_mode": self.trailing_stop_mode})
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=k.close,
                        signal_type="long_exit", label="TD",
                        meta=meta,
                    ))
                    return True
            else:
                self._td_consec = 0
        elif k.high >= target_p:
            if self._tcd > 0:
                self._activate_trailing("long", target_p)
            else:
                meta = self._exit_meta()
                meta.update({"final_trade_delta": self._tcd, "trailing_stop_mode": self.trailing_stop_mode})
                signals.append(StrategySignal(
                    open_time=k.open_time, price=target_p,
                    signal_type="long_exit", label="TP",
                    meta=meta,
                ))
                return True
        return False

    def _bar_exit_short(
        self,
        k: Kline,
        signals: List[StrategySignal],
        target_p: float,
    ) -> bool:
        self._update_excursion(k.high, k.low, "short")

        self._tcv += k.volume
        self._tcbv += k.taker_buy_volume
        self._tcd = 2.0 * self._tcbv - self._tcv
        
        self._peak_trade_delta = min(self._peak_trade_delta, self._tcd)

        if k.high >= self._stop_price:
            lbl = "TS" if self._trailing else "SL"
            meta = self._exit_meta()
            meta.update({"final_trade_delta": self._tcd, "trailing_stop_mode": self.trailing_stop_mode})
            signals.append(StrategySignal(
                open_time=k.open_time, price=self._stop_price,
                signal_type="short_exit", label=lbl,
                meta=meta,
            ))
            return True
            
        if self._trailing:
            if (
                self._peak_trade_delta < 0
                and self._tcd > self._peak_trade_delta * (1 - self.trade_delta_drawdown_pct)
            ):
                meta = self._exit_meta()
                meta.update({"final_trade_delta": self._tcd, "trailing_stop_mode": self.trailing_stop_mode})
                signals.append(StrategySignal(
                    open_time=k.open_time, price=k.close,
                    signal_type="short_exit", label="TDD",
                    meta=meta,
                ))
                return True
            if self._tcd >= 0:
                self._td_consec += 1
                if self._td_consec >= self.td_consec_bars:
                    meta = self._exit_meta()
                    meta.update({"final_trade_delta": self._tcd, "trailing_stop_mode": self.trailing_stop_mode})
                    signals.append(StrategySignal(
                        open_time=k.open_time, price=k.close,
                        signal_type="short_exit", label="TD",
                        meta=meta,
                    ))
                    return True
            else:
                self._td_consec = 0
        elif k.low <= target_p:
            if self._tcd < 0:
                self._activate_trailing("short", target_p)
            else:
                meta = self._exit_meta()
                meta.update({"final_trade_delta": self._tcd, "trailing_stop_mode": self.trailing_stop_mode})
                signals.append(StrategySignal(
                    open_time=k.open_time, price=target_p,
                    signal_type="short_exit", label="TP",
                    meta=meta,
                ))
                return True
        return False

    # =========================================================================
    # Main Loop (Overrides v6 to pass atr into _try_entry_long/short)
    # =========================================================================
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
        long_k0_atr: float = 0.0
        long_k0_meta: dict[str, Any] = {}
        short_k0: Optional[Kline] = None
        short_k0_idx = -1
        short_k0_atr: float = 0.0
        short_k0_meta: dict[str, Any] = {}

        in_pos = False
        side = ""
        target_p = 0.0

        self._trailing = False
        self._stop_price = 0.0
        self._entry_price = 0.0
        self._tcv = 0.0            
        self._tcbv = 0.0           
        self._tcd = 0.0            
        self._zcv = 0.0            
        self._zcbv = 0.0           
        self._peak_trade_delta = 0.0  
        self._fallback_bar_count = 0
        self._td_consec = 0
        self._entry_risk = 0.0
        self._mae = 0.0
        self._mfe = 0.0

        for i, k in enumerate(klines):
            atr = atr_s[i]
            sma = sma_s[i]
            n = self._dyn_n(atr, sma)

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
                    long_k0_atr = 0.0
                    short_k0_atr = 0.0
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

            if long_k0 is not None and i > long_k0_idx:
                if i - long_k0_idx > self.zoom_bars:
                    long_k0 = None
                    long_k0_atr = 0.0
                    long_k0_meta = {}
                else:
                    entered, killed, fp, sp, tp = self._try_entry_long(
                        k, tick_map, signals, long_k0, use_ticks, long_k0_atr,
                        long_k0_meta, i - long_k0_idx)
                    if killed:
                        long_k0 = None
                        long_k0_atr = 0.0
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
                        long_k0_atr = 0.0
                        short_k0_atr = 0.0
                        long_k0_meta = {}
                        short_k0_meta = {}
                        continue

            if short_k0 is not None and i > short_k0_idx:
                if i - short_k0_idx > self.zoom_bars:
                    short_k0 = None
                    short_k0_atr = 0.0
                    short_k0_meta = {}
                else:
                    entered, killed, fp, sp, tp = self._try_entry_short(
                        k, tick_map, signals, short_k0, use_ticks, short_k0_atr,
                        short_k0_meta, i - short_k0_idx)
                    if killed:
                        short_k0 = None
                        short_k0_atr = 0.0
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
                        short_k0_atr = 0.0
                        long_k0_atr = 0.0
                        short_k0_meta = {}
                        long_k0_meta = {}
                        continue

            if not in_pos:
                if self.enable_session_filter and not _in_session(k.open_time):
                    continue
                ticks = tick_map.get(k.open_time) if tick_map else None
                long_ok, long_wick_type = self._is_k0_long(k, i, klines, atr, n, ticks)
                if self.enable_long and long_ok:
                    long_k0 = k
                    long_k0_idx = i
                    long_k0_atr = atr
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
                    short_k0_atr = atr
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
class WickReversalV61_1mStrategy(WickReversalV6_1Strategy):
    """1m variant of v6.1 with the same core logic and independent strategy name."""
    name = "Wick Reversal 1m v6.1"
