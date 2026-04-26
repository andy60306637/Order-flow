from __future__ import annotations

import unittest

import numpy as np

from core.data_types import Kline
from strategies.wick_reversal_v6_1 import WickReversalV6_1Strategy


def _k(
    open_time=0,
    open=100.0,
    high=101.0,
    low=99.0,
    close=100.5,
    volume=1000.0,
    tbv=500.0,
    interval="15m",
):
    return Kline(
        symbol="TEST",
        interval=interval,
        open_time=open_time,
        close_time=open_time + 900_000,
        open=open,
        high=high,
        low=low,
        close=close,
        volume=volume,
        taker_buy_volume=tbv,
        is_closed=True,
    )


def _ticks(rows: list) -> np.ndarray:
    return np.array(rows, dtype=float)


def _strat(**kwargs) -> WickReversalV6_1Strategy:
    s = WickReversalV6_1Strategy()
    s.enable_session_filter = False
    s.taker_fee_rate = 0.0
    s.slippage_rate = 0.0
    s.fee_cover_ratio = 0.0
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


class TestV61EntryZoneAndStop(unittest.TestCase):
    def test_long_entry_uses_body_reclaim_atr_cap_and_hybrid_stop(self):
        s = _strat(entry_extension_a=0.25, entry_atr_cap=0.35, stop_extension_b=0.10, stop_atr_mult=0.25)
        k0 = _k(open=100.0, close=102.0, high=110.0, low=90.0)
        entry_bar = _k(open_time=900_000)
        tick_map = {
            entry_bar.open_time: _ticks([
                [900_001, 103.6, 1.0, 0],  # above body_high but beyond ATR-capped entry zone
                [900_002, 103.2, 1.0, 0],  # inside body_high + min(5.0, 1.4)
            ])
        }

        sigs = []
        entered, killed, fp, sp, tp = s._try_entry_long(
            entry_bar, tick_map, sigs, k0, True, atr=4.0,
            k0_meta={"wick_type": "Absorb"}, entry_delay_bars=1,
        )

        self.assertTrue(entered)
        self.assertFalse(killed)
        self.assertAlmostEqual(fp, 103.2)
        self.assertAlmostEqual(sp, 88.0)  # min(90 - 20*0.1, 90 - 4*0.25)
        self.assertGreater(tp, fp)
        self.assertEqual(sigs[0].label, "L6.1")
        self.assertEqual(sigs[0].meta["wick_type"], "Absorb")
        self.assertEqual(sigs[0].meta["entry_delay_bars"], 1)
        self.assertAlmostEqual(sigs[0].meta["zoom_delta_eff"], 1.0)

    def test_short_entry_uses_body_reclaim_atr_cap_and_hybrid_stop(self):
        s = _strat(entry_extension_a=0.25, entry_atr_cap=0.35, stop_extension_b=0.10, stop_atr_mult=0.25)
        k0 = _k(open=100.0, close=98.0, high=110.0, low=90.0)
        entry_bar = _k(open_time=900_000)
        tick_map = {
            entry_bar.open_time: _ticks([
                [900_001, 96.4, 1.0, 1],  # below ATR-capped entry zone
                [900_002, 96.8, 1.0, 1],  # inside body_low - min(5.0, 1.4)
            ])
        }

        sigs = []
        entered, killed, fp, sp, tp = s._try_entry_short(
            entry_bar, tick_map, sigs, k0, True, atr=4.0,
            k0_meta={"wick_type": "Initiative"}, entry_delay_bars=1,
        )

        self.assertTrue(entered)
        self.assertFalse(killed)
        self.assertAlmostEqual(fp, 96.8)
        self.assertAlmostEqual(sp, 112.0)  # max(110 + 20*0.1, 110 + 4*0.25)
        self.assertLess(tp, fp)
        self.assertEqual(sigs[0].label, "S6.1")
        self.assertEqual(sigs[0].meta["wick_type"], "Initiative")
        self.assertAlmostEqual(sigs[0].meta["zoom_delta_eff"], -1.0)


class TestV61TrailingAndTDD(unittest.TestCase):
    def test_activate_trailing_lock_tp_mode(self):
        s = _strat(trailing_stop_mode="lock_tp")
        s._entry_price = 100.0

        s._activate_trailing("long", 120.0)

        self.assertTrue(s._trailing)
        self.assertAlmostEqual(s._stop_price, 120.0)

    def test_activate_trailing_breakeven_cost_mode(self):
        s = _strat(trailing_stop_mode="breakeven_cost", taker_fee_rate=0.001, slippage_rate=0.0)
        s._entry_price = 100.0

        s._activate_trailing("long", 120.0)

        self.assertTrue(s._trailing)
        self.assertAlmostEqual(s._stop_price, 100.2)

    def test_tdd_does_not_fire_before_trailing_is_active(self):
        s = _strat(trade_delta_drawdown_pct=0.3)
        s._trailing = False
        s._entry_price = 100.0
        s._entry_risk = 10.0
        s._stop_price = 80.0
        s._tcv = 10.0
        s._tcbv = 10.0
        s._peak_trade_delta = 10.0
        k = _k(open_time=1_000_000, high=120.0, low=100.0, close=110.0)
        tick_map = {k.open_time: _ticks([[1_000_001, 110.0, 20.0, 1]])}
        sigs = []

        exited = s._tick_exit_long(k, tick_map, sigs, target_p=999.0)

        self.assertFalse(exited)
        self.assertEqual(sigs, [])

    def test_tdd_fires_after_trailing_drawdown(self):
        s = _strat(trade_delta_drawdown_pct=0.3)
        s._trailing = True
        s._entry_price = 100.0
        s._entry_risk = 10.0
        s._stop_price = 90.0
        s._tcv = 10.0
        s._tcbv = 10.0
        s._peak_trade_delta = 10.0
        k = _k(open_time=1_000_000, high=120.0, low=100.0, close=110.0)
        tick_map = {k.open_time: _ticks([[1_000_001, 110.0, 4.0, 1]])}
        sigs = []

        exited = s._tick_exit_long(k, tick_map, sigs, target_p=120.0)

        self.assertTrue(exited)
        self.assertEqual(sigs[0].label, "TDD")
        self.assertAlmostEqual(sigs[0].meta["final_trade_delta"], 6.0)
        self.assertEqual(sigs[0].meta["trailing_stop_mode"], "lock_tp")
        self.assertAlmostEqual(sigs[0].meta["MFE"], 1.0)


if __name__ == "__main__":
    unittest.main()
