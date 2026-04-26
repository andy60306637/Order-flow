"""
tests/test_wick_reversal_v6.py
Unit tests for Wick Reversal v6: fill_time, dynamic N, session filter,
fee cover, k0 detection, entry guards, trailing state machine.
"""
from __future__ import annotations

import unittest
from typing import List

import numpy as np

from core.data_types import Kline
from strategies.base import StrategySignal
from strategies.wick_reversal_v6 import (
    WickReversalV6Strategy,
    _in_session,
    _atr_series,
    _sma_series,
)
from backtest.engine import _pair_signals


# ── helpers ───────────────────────────────────────────────────────────────────

def _k(open_time=0, open=100.0, high=101.0, low=99.0, close=100.5,
       volume=1000.0, tbv=500.0, interval="15m"):
    return Kline(
        symbol="TEST", interval=interval,
        open_time=open_time, close_time=open_time + 900_000,
        open=open, high=high, low=low, close=close,
        volume=volume, taker_buy_volume=tbv, is_closed=True,
    )


def _ticks(rows: list) -> np.ndarray:
    """rows = list of (time_ms, price, qty, is_buyer_maker)"""
    return np.array(rows, dtype=float)


def _strat(**kwargs) -> WickReversalV6Strategy:
    s = WickReversalV6Strategy()
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


def _base_klines(n=110, price=50000.0) -> List[Kline]:
    """n uniform bars for warming up ATR/SMA. No wick patterns."""
    bars = []
    for i in range(n):
        bars.append(_k(
            open_time=i * 900_000,
            open=price, high=price + 50, low=price - 50, close=price,
            volume=1000, tbv=500,
        ))
    return bars


# ─────────────────────────────────────────────────────────────────────────────
# Phase A: fill_time in StrategySignal and _pair_signals
# ─────────────────────────────────────────────────────────────────────────────

class TestFillTime(unittest.TestCase):

    def _sig(self, sig_type, price, t=0, fill_time=None, stop=None, fill_price=None):
        return StrategySignal(
            open_time=t, price=price, signal_type=sig_type,
            label="", stop_price=stop, fill_price=fill_price, fill_time=fill_time,
        )

    def test_fill_time_field_defaults_none(self):
        sig = StrategySignal(open_time=0, price=1.0, signal_type="long_entry")
        self.assertIsNone(sig.fill_time)

    def test_pair_uses_fill_time_for_entry(self):
        """entry_time in paired trade should use fill_time when provided."""
        sigs = [
            self._sig("long_entry", 100.0, t=1000, fill_time=1200),
            self._sig("long_exit",  105.0, t=2000, fill_time=2100),
        ]
        trades, _ = _pair_signals(sigs)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["entry_time"], 1200)
        self.assertEqual(trades[0]["exit_time"], 2100)

    def test_pair_falls_back_to_open_time_when_no_fill_time(self):
        sigs = [
            self._sig("long_entry", 100.0, t=1000),
            self._sig("long_exit",  105.0, t=2000),
        ]
        trades, _ = _pair_signals(sigs)
        self.assertEqual(trades[0]["entry_time"], 1000)
        self.assertEqual(trades[0]["exit_time"], 2000)

    def test_existing_strategy_pnl_unchanged_without_fill_time(self):
        """Backward-compat: signals without fill_time produce same trade list."""
        sigs = [
            self._sig("long_entry", 100.0, t=1000, stop=98.0),
            self._sig("long_exit",  104.0, t=2000),
        ]
        trades, _ = _pair_signals(sigs)
        self.assertAlmostEqual(trades[0]["entry"], 100.0)
        self.assertAlmostEqual(trades[0]["exit"], 104.0)


# ─────────────────────────────────────────────────────────────────────────────
# Session filter
# ─────────────────────────────────────────────────────────────────────────────

class TestSessionFilter(unittest.TestCase):

    def _ms(self, hour, minute=0):
        """UTC epoch ms for a fixed date at given hour:minute."""
        from datetime import datetime, timezone
        dt = datetime(2024, 1, 15, hour, minute, 0, tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    def test_asia_in_session(self):
        self.assertTrue(_in_session(self._ms(2)))   # 02:00 UTC

    def test_london_in_session(self):
        self.assertTrue(_in_session(self._ms(10)))  # 10:00 UTC

    def test_ny_in_session(self):
        self.assertTrue(_in_session(self._ms(15)))  # 15:00 UTC

    def test_out_of_session(self):
        # 22:00 UTC = not in Asia(0-8), not London(7-16), not NY(13-22 exclusive)
        self.assertFalse(_in_session(self._ms(22)))

    def test_session_filter_rejects_k0_outside_session(self):
        """k0 formed at 22:00 UTC must be ignored when filter enabled."""
        s = _strat(enable_session_filter=True, enable_short=False)
        s.atr_period = 14
        s.sma_atr_period = 100

        from datetime import datetime, timezone
        base_dt = datetime(2024, 1, 15, 22, 0, 0, tzinfo=timezone.utc)
        base_ms = int(base_dt.timestamp() * 1000)

        klines = _base_klines(110, price=50000.0)
        # Place a valid long k0 at 22:00 UTC (out of session)
        rng = 200.0
        atr = 100.0
        k0 = _k(
            open_time=base_ms,
            open=50000, high=50000 + rng * 0.09, low=50000 - rng * 0.9,
            close=50000 + rng * 0.05,
            volume=2000, tbv=1800,
        )
        klines.append(k0)
        sigs = s.on_history(klines)
        k0_sigs = [x for x in sigs if x.signal_type == "k0_long"]
        self.assertEqual(len(k0_sigs), 0)


# ─────────────────────────────────────────────────────────────────────────────
# Dynamic N
# ─────────────────────────────────────────────────────────────────────────────

class TestDynamicN(unittest.TestCase):

    def setUp(self):
        self.s = _strat(base_n=24, min_n=12, max_n=48)

    def test_normal_ratio_gives_base_n(self):
        self.assertEqual(self.s._dyn_n(100.0, 100.0), 24)

    def test_high_volatility_clamped_to_max(self):
        # ATR = 5× SMA → N = round(24 * 5) = 120 → clamped to 48
        self.assertEqual(self.s._dyn_n(500.0, 100.0), 48)

    def test_low_volatility_clamped_to_min(self):
        # ATR = 0.1× SMA → N = round(24 * 0.1) = 2 → clamped to 12
        self.assertEqual(self.s._dyn_n(10.0, 100.0), 12)

    def test_zero_sma_returns_base_n(self):
        self.assertEqual(self.s._dyn_n(100.0, 0.0), 24)

    def test_double_atr_gives_48(self):
        self.assertEqual(self.s._dyn_n(200.0, 100.0), 48)

    def test_half_atr_gives_12(self):
        # round(24 * 0.5) = 12, exactly min
        self.assertEqual(self.s._dyn_n(50.0, 100.0), 12)


# ─────────────────────────────────────────────────────────────────────────────
# Fee cover (_risk_ok)
# ─────────────────────────────────────────────────────────────────────────────

class TestFeeCover(unittest.TestCase):

    def setUp(self):
        self.s = _strat(
            taker_fee_rate=0.0005, slippage_rate=0.0001,
            rr=2.0, fee_cover_ratio=1.2,
        )

    def test_sufficient_risk_accepted(self):
        # rt_rate = 2*(0.0005+0.0001) = 0.0012
        # rt_cost(50000) = 60
        # min_risk = 60 * 1.2 / 2.0 = 36
        self.assertTrue(self.s._risk_ok(50000.0, 40.0))

    def test_insufficient_risk_rejected(self):
        self.assertFalse(self.s._risk_ok(50000.0, 10.0))

    def test_zero_risk_rejected(self):
        self.assertFalse(self.s._risk_ok(50000.0, 0.0))

    def test_configure_backtest_costs_updates_rates(self):
        self.s.configure_backtest_costs(fee_rate=0.0002, slippage_bps=2.0)
        self.assertAlmostEqual(self.s.taker_fee_rate, 0.0002)
        self.assertAlmostEqual(self.s.slippage_rate, 0.0002)


# ─────────────────────────────────────────────────────────────────────────────
# k0 detection
# ─────────────────────────────────────────────────────────────────────────────

def _make_valid_long_k0(price=50000.0, atr=200.0) -> Kline:
    """Construct a bar that should pass long k0 detection."""
    rng = atr * 1.5          # range > atr * 0.8
    bdy = rng * 0.04         # body small
    # body at top, lower wick long
    body_high = price
    body_low = price - bdy
    low = price - rng * 0.9  # lower wick >> body
    high = price + rng * 0.05  # upper wick < rng*0.1
    return _k(
        open_time=0,
        open=body_low, high=high, low=low, close=body_high,
        volume=2000, tbv=300,  # net delta < 0 (sells dominate)
    )


class TestK0Detection(unittest.TestCase):

    def _s(self):
        s = WickReversalV6Strategy()
        s.enable_session_filter = False
        return s

    def _history(self, k0: Kline, n=30, price=50000.0):
        bars = []
        for j in range(n):
            bars.append(_k(
                open_time=-(n - j) * 900_000,
                open=price, high=price + 60, low=price - 60, close=price,
                volume=1000, tbv=500,
            ))
        bars.append(k0)
        return bars

    def test_valid_long_k0_detected(self):
        s = self._s()
        k0 = _make_valid_long_k0(price=50000.0, atr=200.0)
        klines = self._history(k0, n=110)
        atr_s = _atr_series(klines, s.atr_period)
        sma_s = _sma_series(atr_s, s.sma_atr_period)
        i = len(klines) - 1
        atr = atr_s[i]
        n = s._dyn_n(atr, sma_s[i])
        result = s._is_k0_long(k0, i, klines, atr, n, None)
        self.assertTrue(result[0])

    def test_upper_wick_too_large_rejects_long_k0(self):
        s = self._s()
        k0 = _make_valid_long_k0(price=50000.0, atr=200.0)
        # inflate upper wick to > rng * 0.1
        k0_bad = _k(
            open_time=0,
            open=k0.open, high=k0.low + (k0.high - k0.low) * 2.0,
            low=k0.low, close=k0.close,
            volume=k0.volume, tbv=k0.taker_buy_volume,
        )
        klines = self._history(k0_bad, n=110)
        atr_s = _atr_series(klines, s.atr_period)
        sma_s = _sma_series(atr_s, s.sma_atr_period)
        i = len(klines) - 1
        atr = atr_s[i]
        n = s._dyn_n(atr, sma_s[i])
        result = s._is_k0_long(k0_bad, i, klines, atr, n, None)
        self.assertFalse(result[0])

    def test_sweep_scan_fails_if_not_below_past_n_lows(self):
        s = self._s()
        # k0 low is NOT the lowest in past N bars
        k0 = _make_valid_long_k0(price=50000.0, atr=200.0)
        klines = self._history(k0, n=110, price=50000.0)
        # Insert a bar with even lower low
        klines[-2] = _k(
            open_time=klines[-2].open_time,
            open=50000, high=50060, low=k0.low - 1.0, close=50000,
            volume=1000, tbv=500,
        )
        atr_s = _atr_series(klines, s.atr_period)
        sma_s = _sma_series(atr_s, s.sma_atr_period)
        i = len(klines) - 1
        atr = atr_s[i]
        n = s._dyn_n(atr, sma_s[i])
        result = s._is_k0_long(k0, i, klines, atr, n, None)
        self.assertFalse(result[0])


# ─────────────────────────────────────────────────────────────────────────────
# Entry: guard kill and tick-level entry
# ─────────────────────────────────────────────────────────────────────────────

class TestK0EngineLabels(unittest.TestCase):

    def _base(self, n=110, price=50000.0):
        bars = []
        for i in range(n):
            bars.append(_k(
                open_time=i * 900_000,
                open=price, high=price + 50, low=price - 50, close=price,
                volume=1000, tbv=500,
            ))
        return bars

    def _run(self, k0: Kline, tick_rows):
        s = _strat(enable_session_filter=False, enable_long=True, enable_short=True)
        bars = self._base()
        k0 = _k(
            open_time=bars[-1].open_time + 900_000,
            open=k0.open, high=k0.high, low=k0.low, close=k0.close,
            volume=k0.volume, tbv=k0.taker_buy_volume,
        )
        bars.append(k0)
        tick_map = {k0.open_time: _ticks(tick_rows)}
        return s.on_history(bars, tick_map)

    def test_long_k0_absorb_label(self):
        k0 = _k(open=50000, close=50010, high=50015, low=49800, volume=2000, tbv=1000)
        sigs = self._run(k0, [
            [1, 49990, 20.0, 1],  # wick sell
            [2, 50005, 80.0, 0],  # non-wick
        ])
        labels = [x.label for x in sigs if x.signal_type == "k0_long"]
        self.assertIn("k0_Absorb", labels)

    def test_long_k0_initiative_label(self):
        k0 = _k(open=50000, close=50010, high=50015, low=49800, volume=2000, tbv=1000)
        sigs = self._run(k0, [
            [1, 49990, 20.0, 0],  # wick buy
            [2, 50005, 80.0, 1],
        ])
        labels = [x.label for x in sigs if x.signal_type == "k0_long"]
        self.assertIn("k0_Initiative", labels)

    def test_short_k0_absorb_label(self):
        k0 = _k(open=50000, close=49990, high=50200, low=49985, volume=2000, tbv=1000)
        sigs = self._run(k0, [
            [1, 50010, 20.0, 0],  # wick buy
            [2, 49995, 80.0, 1],  # non-wick
        ])
        labels = [x.label for x in sigs if x.signal_type == "k0_short"]
        self.assertIn("k0s_Absorb", labels)

    def test_short_k0_initiative_label(self):
        k0 = _k(open=50000, close=49990, high=50200, low=49985, volume=2000, tbv=1000)
        sigs = self._run(k0, [
            [1, 50010, 20.0, 1],  # wick sell
            [2, 49995, 80.0, 0],
        ])
        labels = [x.label for x in sigs if x.signal_type == "k0_short"]
        self.assertIn("k0s_Initiative", labels)


class TestEntryGuard(unittest.TestCase):

    def _make_k0(self):
        # k0: body at [100, 102], range [90, 103]
        return _k(open_time=0, open=100.0, high=103.0, low=90.0, close=102.0,
                  volume=1000, tbv=100)

    def test_tick_below_body_low_kills_setup(self):
        s = _strat(taker_fee_rate=0.0, slippage_rate=0.0, fee_cover_ratio=0.0)
        k0 = self._make_k0()
        entry_bar = _k(open_time=900_000, open=101.0, high=104.0, low=89.0, close=103.0)
        ticks = _ticks([
            [900_001, 99.9, 1.0, 0],  # below k0_body_low=100 → kill
        ])
        tick_map = {entry_bar.open_time: ticks}
        entered, killed, *_ = s._try_entry_long(entry_bar, tick_map, [], k0, True)
        self.assertFalse(entered)
        self.assertTrue(killed)

    def test_tick_above_body_high_triggers_entry(self):
        s = _strat(taker_fee_rate=0.0, slippage_rate=0.0, fee_cover_ratio=0.0)
        k0 = self._make_k0()
        entry_bar = _k(open_time=900_000)
        ticks = _ticks([
            [900_001, 101.0, 1.0, 0],  # below body_high=102 → no entry
            [900_002, 102.5, 1.0, 0],  # above body_high=102 → enter
        ])
        tick_map = {entry_bar.open_time: ticks}
        sigs = []
        entered, killed, fp, sp, tp = s._try_entry_long(entry_bar, tick_map, sigs, k0, True)
        self.assertTrue(entered)
        self.assertFalse(killed)
        self.assertAlmostEqual(fp, 102.5)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].signal_type, "long_entry")
        self.assertEqual(sigs[0].fill_time, 900_002)

    def test_tick_above_max_entry_skipped(self):
        s = _strat(taker_fee_rate=0.0, slippage_rate=0.0, fee_cover_ratio=0.0,
                   entry_extension_a=0.25)
        k0 = self._make_k0()
        # k0_rng = 13, max_entry = 103 + 13*0.25 = 106.25
        entry_bar = _k(open_time=900_000)
        ticks = _ticks([
            [900_001, 107.0, 1.0, 0],  # > max_entry → skip
            [900_002, 104.0, 1.0, 0],  # <= max_entry, > body_high → enter
        ])
        tick_map = {entry_bar.open_time: ticks}
        sigs = []
        entered, killed, fp, *_ = s._try_entry_long(entry_bar, tick_map, sigs, k0, True)
        self.assertTrue(entered)
        self.assertAlmostEqual(fp, 104.0)

    def test_short_tick_above_body_high_kills_setup(self):
        s = _strat(taker_fee_rate=0.0, slippage_rate=0.0, fee_cover_ratio=0.0)
        # k0 short: body at [98, 100], range [97, 103]
        k0 = _k(open_time=0, open=100.0, high=103.0, low=97.0, close=98.0,
                volume=1000, tbv=900)
        entry_bar = _k(open_time=900_000)
        ticks = _ticks([
            [900_001, 103.5, 1.0, 0],  # above k0_body_high=100 → kill
        ])
        tick_map = {entry_bar.open_time: ticks}
        entered, killed, *_ = s._try_entry_short(entry_bar, tick_map, [], k0, True)
        self.assertFalse(entered)
        self.assertTrue(killed)


# ─────────────────────────────────────────────────────────────────────────────
# Trailing state machine (Phase D)
# ─────────────────────────────────────────────────────────────────────────────

class TestTrailingStateMachine(unittest.TestCase):

    def _setup_strategy(self):
        s = _strat(
            taker_fee_rate=0.0, slippage_rate=0.0,
            fee_cover_ratio=0.0, rr=2.0,
        )
        s._td_consec = 0
        return s

    def _exit_bar(self, s, ticks_rows, target_p, open_time=1_000_000):
        k = _k(open_time=open_time, open=100.0, high=200.0, low=50.0, close=150.0)
        ticks = _ticks(ticks_rows)
        tick_map = {k.open_time: ticks}
        sigs = []
        exited = s._tick_exit_long(k, tick_map, sigs, target_p)
        return exited, sigs

    def test_direct_tp_when_cum_delta_nonpositive(self):
        s = self._setup_strategy()
        s._trailing = False
        s._stop_price = 90.0
        s._entry_price = 100.0
        s._tcv = 0.0; s._tcbv = 0.0; s._tcd = 0.0

        target_p = 120.0
        # ticks: price rises to target, but sell pressure (is_buyer_maker=True → not buy)
        # cum_buy_vol = 0, cum_vol = 5, cum_delta = -5 ≤ 0 → direct TP
        ticks_rows = [
            [1_000_001, 115.0, 2.0, 1],  # sell
            [1_000_002, 120.5, 3.0, 1],  # sell → price >= target, delta=-5 ≤ 0
        ]
        exited, sigs = self._exit_bar(s, ticks_rows, target_p)
        self.assertTrue(exited)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].label, "TP")

    def test_trailing_mode_entered_when_cum_delta_positive(self):
        s = self._setup_strategy()
        s._trailing = False
        s._stop_price = 90.0
        s._entry_price = 100.0
        s._tcv = 0.0; s._tcbv = 0.0; s._tcd = 0.0
        s._peak_trade_delta = 0.0

        target_p = 120.0
        # ticks: price >= target, but buy pressure → enter trailing
        ticks_rows = [
            [1_000_001, 115.0, 2.0, 0],  # buy
            [1_000_002, 121.0, 3.0, 0],  # buy → price >= target, delta=+5 > 0 → trailing
        ]
        exited, sigs = self._exit_bar(s, ticks_rows, target_p)
        self.assertFalse(exited)
        self.assertEqual(len(sigs), 0)
        self.assertTrue(s._trailing)
        # Phase 2: stop moves to breakeven (entry + rt_cost); with fee=0 → stop == entry
        self.assertAlmostEqual(s._stop_price, target_p)

    def test_td_exit_in_trailing_after_consecutive_negative_bars(self):
        """Phase 2: TD exits immediately on the tick where delta turns non-positive."""
        s = self._setup_strategy()
        s._trailing = True
        s._stop_price = 100.0
        s._entry_price = 100.0
        s.td_consec_bars = 2

        target_p = 120.0
        # Sell tick flips delta negative → immediate TD exit (no 2-bar wait).
        ticks_rows = [
            [1_000_001, 125.0, 20.0, 1],  # sell → tcd = 2*10 - (10+20) = -10 ≤ 0 → TD
        ]
        exited, sigs = self._exit_bar(s, ticks_rows, target_p)
        self.assertFalse(exited)
        self.assertEqual(len(sigs), 0)
        self.assertEqual(s._td_consec, 1)

        exited, sigs = self._exit_bar(s, ticks_rows, target_p, open_time=1_900_000)
        self.assertTrue(exited)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].label, "TD")

    def test_sl_exit_when_price_drops_to_stop(self):
        s = self._setup_strategy()
        s._trailing = False
        s._stop_price = 95.0
        s._entry_price = 100.0
        s._tcv = 0.0; s._tcbv = 0.0; s._tcd = 0.0

        target_p = 120.0
        ticks_rows = [
            [1_000_001, 100.0, 1.0, 0],
            [1_000_002, 94.5,  1.0, 1],  # price <= stop → SL
        ]
        exited, sigs = self._exit_bar(s, ticks_rows, target_p)
        self.assertTrue(exited)
        self.assertEqual(sigs[0].label, "SL")

    def test_ts_label_when_trailing_stop_hit(self):
        s = self._setup_strategy()
        s._trailing = True
        s._stop_price = 105.0
        s._entry_price = 100.0
        s._tcv = 5.0; s._tcbv = 5.0; s._tcd = 5.0

        target_p = 120.0
        ticks_rows = [
            [1_000_001, 104.0, 1.0, 1],  # <= stop → TS
        ]
        exited, sigs = self._exit_bar(s, ticks_rows, target_p)
        self.assertTrue(exited)
        self.assertEqual(sigs[0].label, "TS")

    def test_tp_touch_uses_current_bar_delta_only(self):
        """cum_delta is trade-level: buy from bar 1 carries into bar 2, enabling trailing instead of direct TP."""
        s = self._setup_strategy()
        s._trailing = False
        s._stop_price = 90.0
        s._entry_price = 100.0
        s._tcv = 0.0; s._tcbv = 0.0; s._tcd = 0.0
        s._peak_trade_delta = 0.0

        target_p = 120.0

        # Bar 1: buy pressure, no TP touch — delta accumulates.
        k1 = _k(open_time=1_000_000, high=115.0, low=100.0)
        t1 = _ticks([[1_000_001, 110.0, 5.0, 0]])  # buy → tcv=5, tcbv=5
        tick_map = {k1.open_time: t1}
        sigs = []
        exited1 = s._tick_exit_long(k1, tick_map, sigs, target_p)
        self.assertFalse(exited1)
        self.assertAlmostEqual(s._tcd, 5.0)

        # Bar 2: TP touch with a single sell tick. With trade-level carry-over (delta=2 > 0) → trailing.
        k2 = _k(open_time=1_900_000, high=121.0, low=103.0)
        t2 = _ticks([[1_900_001, 120.5, 3.0, 1]])  # sell → tcv=8, tcbv=5, delta=2 > 0
        tick_map = {k2.open_time: t2}
        exited2 = s._tick_exit_long(k2, tick_map, sigs, target_p)
        self.assertTrue(exited2)
        self.assertFalse(s._trailing)
        # Phase 2: stop = breakeven (entry + rt_cost); with fee=0 → stop == entry
        self.assertEqual(sigs[-1].label, "TP")


# ─────────────────────────────────────────────────────────────────────────────
# Short mirror: entry and trailing
# ─────────────────────────────────────────────────────────────────────────────

class TestShortMirror(unittest.TestCase):

    def _make_k0_short(self):
        # k0 short: body at [98, 100], range [96, 110]
        return _k(open_time=0, open=100.0, high=110.0, low=96.0, close=98.0,
                  volume=1000, tbv=900)

    def test_short_entry_tick_below_body_low(self):
        s = _strat(taker_fee_rate=0.0, slippage_rate=0.0, fee_cover_ratio=0.0)
        k0 = self._make_k0_short()
        entry_bar = _k(open_time=900_000)
        ticks = _ticks([
            [900_001, 99.0, 1.0, 1],   # above k0_body_low=98 → no entry
            [900_002, 97.5, 1.0, 1],   # below body_low=98 → enter
        ])
        tick_map = {entry_bar.open_time: ticks}
        sigs = []
        entered, killed, fp, sp, tp = s._try_entry_short(entry_bar, tick_map, sigs, k0, True)
        self.assertTrue(entered)
        self.assertAlmostEqual(fp, 97.5)
        self.assertLess(tp, fp)  # target is below entry for short

    def test_short_td_exit_after_consecutive_nonnegative_bars(self):
        """Phase 2: short TD exits immediately on the tick where delta turns non-negative."""
        s = _strat(taker_fee_rate=0.0, slippage_rate=0.0, fee_cover_ratio=0.0)
        s._trailing = True
        s._stop_price = 102.0
        s._entry_price = 98.0
        s._td_consec = 0
        s.td_consec_bars = 2

        target_p = 80.0
        k = _k(open_time=1_000_000, high=100.0, low=78.0)
        # Buy tick → trade_delta becomes >= 0 → immediate TD exit
        ticks = _ticks([[1_000_001, 85.0, 20.0, 0]])  # buy: tcbv=20, tcv=30, delta=10 >= 0 → TD
        tick_map = {k.open_time: ticks}
        sigs = []
        exited = s._tick_exit_short(k, tick_map, sigs, target_p)
        self.assertFalse(exited)
        self.assertEqual(len(sigs), 0)
        self.assertEqual(s._td_consec, 1)

        k2 = _k(open_time=1_900_000, high=100.0, low=78.0)
        tick_map = {k2.open_time: _ticks([[1_900_001, 85.0, 20.0, 0]])}
        exited = s._tick_exit_short(k2, tick_map, sigs, target_p)
        self.assertTrue(exited)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].label, "TD")


if __name__ == "__main__":
    unittest.main()
