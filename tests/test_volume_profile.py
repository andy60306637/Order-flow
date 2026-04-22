"""tests/test_volume_profile.py — Volume Profile 引擎單元測試"""
import math
import unittest

import numpy as np

from core.data_types import Kline
from core.volume_profile import (
    VolumeProfile,
    VolumeProfileLevel,
    build_volume_profile,
    build_bar_profiles,
    build_composite_profile,
    build_rolling_profiles,
    _calc_value_area,
)

_MS_1M = 60_000

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ticks(*rows) -> np.ndarray:
    """Build ndarray(N, 4) from (time, price, qty, is_buyer_maker) tuples."""
    if not rows:
        return np.empty((0, 4), dtype=np.float64)
    return np.array(rows, dtype=np.float64)


def _k(i: int, c: float = 50000.0) -> Kline:
    ot = i * _MS_1M
    return Kline(
        symbol="BTCUSDT", interval="1m",
        open_time=ot, close_time=ot + _MS_1M - 1,
        open=c, high=c + 50, low=c - 50, close=c,
        volume=100.0, taker_buy_volume=50.0, is_closed=True,
    )


# ─── VolumeProfileLevel ───────────────────────────────────────────────────────

class TestVolumeProfileLevel(unittest.TestCase):

    def test_delta(self):
        lv = VolumeProfileLevel(price=100.0, total_vol=10.0, buy_vol=7.0, sell_vol=3.0)
        self.assertAlmostEqual(lv.delta, 4.0)

    def test_delta_pct(self):
        lv = VolumeProfileLevel(price=100.0, total_vol=10.0, buy_vol=7.0, sell_vol=3.0)
        self.assertAlmostEqual(lv.delta_pct, 0.4)

    def test_delta_pct_zero_total(self):
        lv = VolumeProfileLevel(price=100.0, total_vol=0.0)
        self.assertEqual(lv.delta_pct, 0.0)

    def test_negative_delta(self):
        lv = VolumeProfileLevel(price=100.0, total_vol=10.0, buy_vol=2.0, sell_vol=8.0)
        self.assertAlmostEqual(lv.delta, -6.0)
        self.assertAlmostEqual(lv.delta_pct, -0.6)


# ─── _calc_value_area ─────────────────────────────────────────────────────────

class TestCalcValueArea(unittest.TestCase):

    def test_single_level(self):
        vol = np.array([100.0])
        lo, hi = _calc_value_area(vol, 0, 0.70)
        self.assertEqual(lo, 0)
        self.assertEqual(hi, 0)

    def test_two_levels_expands_to_larger(self):
        # POC idx=0, right side is bigger -> should expand right first
        vol = np.array([50.0, 80.0])
        lo, hi = _calc_value_area(vol, 0, 0.70)
        # Total=130, target=91. POC=50, right=80 -> 50+80=130 >= 91
        self.assertEqual(lo, 0)
        self.assertEqual(hi, 1)

    def test_expands_to_reach_target(self):
        # Uniform distribution: each level = 10, total = 50, target 70% = 35
        vol = np.array([10.0, 10.0, 10.0, 10.0, 10.0])
        poc = 2  # middle
        lo, hi = _calc_value_area(vol, poc, 0.70)
        # Must include enough to reach 35: poc(10)+lo(10)+hi(10)+lo(10) = 40 >= 35
        covered = float(vol[lo:hi+1].sum())
        self.assertGreaterEqual(covered, 50.0 * 0.70)

    def test_target_pct_100_covers_all(self):
        vol = np.array([5.0, 20.0, 5.0])
        lo, hi = _calc_value_area(vol, 1, 1.0)
        self.assertEqual(lo, 0)
        self.assertEqual(hi, 2)

    def test_already_at_boundary(self):
        vol = np.array([100.0])
        lo, hi = _calc_value_area(vol, 0, 0.70)
        self.assertEqual(lo, 0)
        self.assertEqual(hi, 0)


# ─── build_volume_profile ─────────────────────────────────────────────────────

class TestBuildVolumeProfile(unittest.TestCase):

    def test_empty_ticks_returns_none(self):
        vp = build_volume_profile(_ticks(), tick_size=1.0)
        self.assertIsNone(vp)

    def test_single_tick(self):
        ticks = _ticks((1000, 50000.0, 1.5, 0.0))
        vp = build_volume_profile(ticks, tick_size=1.0)
        self.assertIsNotNone(vp)
        self.assertAlmostEqual(vp.poc_price, 50000.0)
        self.assertAlmostEqual(vp.total_volume, 1.5)
        self.assertAlmostEqual(vp.vah, 50000.0)
        self.assertAlmostEqual(vp.val, 50000.0)

    def test_poc_at_max_volume_level(self):
        # 50000 = 5 units, 50001 = 1 unit, 49999 = 1 unit  -> POC at 50000
        ticks = _ticks(
            (1000, 50000.0, 2.0, 0.0),
            (1001, 50000.0, 3.0, 1.0),
            (1002, 50001.0, 1.0, 0.0),
            (1003, 49999.0, 1.0, 1.0),
        )
        vp = build_volume_profile(ticks, tick_size=1.0)
        self.assertAlmostEqual(vp.poc_price, 50000.0)

    def test_buy_sell_split(self):
        ticks = _ticks(
            (1000, 50000.0, 3.0, 0.0),  # buyer-initiated
            (1001, 50000.0, 2.0, 1.0),  # seller-initiated
        )
        vp = build_volume_profile(ticks, tick_size=1.0)
        lv = vp.level_at(50000.0)
        self.assertIsNotNone(lv)
        self.assertAlmostEqual(lv.buy_vol, 3.0)
        self.assertAlmostEqual(lv.sell_vol, 2.0)
        self.assertAlmostEqual(lv.delta, 1.0)

    def test_tick_size_bucketing(self):
        # 50000.3 and 50000.7 should fall into same 1.0 bucket: 50000.0
        ticks = _ticks(
            (1000, 50000.3, 1.0, 0.0),
            (1001, 50000.7, 1.0, 0.0),
        )
        vp = build_volume_profile(ticks, tick_size=1.0)
        self.assertEqual(len(vp.levels), 1)
        self.assertAlmostEqual(vp.poc_price, 50000.0)
        self.assertAlmostEqual(vp.total_volume, 2.0)

    def test_tick_size_0_1(self):
        ticks = _ticks(
            (1000, 50000.1, 1.0, 0.0),
            (1001, 50000.2, 1.0, 0.0),
        )
        vp = build_volume_profile(ticks, tick_size=0.1)
        self.assertEqual(len(vp.levels), 2)

    def test_value_area_covers_target_pct(self):
        # 5 levels with vol [10, 20, 50, 20, 10] = 110 total. target 70% = 77
        # POC at 50002 (vol=50). Expand: 50001/50003 both 20, pick arbitrary...
        #   cum = 50+20+20 = 90 >= 77 -> should cover
        ticks = _ticks(
            (1000, 50000.0, 10.0, 0.0),
            (1001, 50001.0, 20.0, 0.0),
            (1002, 50002.0, 50.0, 0.0),
            (1003, 50003.0, 20.0, 0.0),
            (1004, 50004.0, 10.0, 0.0),
        )
        vp = build_volume_profile(ticks, tick_size=1.0, value_area_pct=0.70)
        va_vol = sum(
            lv.total_vol for p, lv in vp.levels.items()
            if vp.val <= p <= vp.vah
        )
        self.assertGreaterEqual(va_vol, 110.0 * 0.70)
        self.assertLessEqual(vp.val, vp.poc_price)
        self.assertGreaterEqual(vp.vah, vp.poc_price)

    def test_hvn_identified(self):
        # One dominant level at 50000 (vol=100), others at 10 each
        ticks = _ticks(
            (1000, 50000.0, 100.0, 0.0),
            (1001, 50001.0, 10.0, 0.0),
            (1002, 50002.0, 10.0, 0.0),
        )
        vp = build_volume_profile(ticks, tick_size=1.0, hvn_threshold=1.5)
        self.assertIn(50000.0, vp.hvn_prices)
        self.assertNotIn(50001.0, vp.hvn_prices)

    def test_lvn_identified(self):
        # One thin level at 50001 (vol=1), others dominant
        ticks = _ticks(
            (1000, 50000.0, 100.0, 0.0),
            (1001, 50001.0, 1.0, 0.0),
            (1002, 50002.0, 100.0, 0.0),
        )
        vp = build_volume_profile(ticks, tick_size=1.0, lvn_threshold=0.5)
        self.assertIn(50001.0, vp.lvn_prices)
        self.assertNotIn(50000.0, vp.lvn_prices)

    def test_level_at_returns_none_for_unknown_price(self):
        ticks = _ticks((1000, 50000.0, 1.0, 0.0))
        vp = build_volume_profile(ticks, tick_size=1.0)
        self.assertIsNone(vp.level_at(99999.0))

    def test_total_volume_equals_sum_of_levels(self):
        ticks = _ticks(
            (1000, 50000.0, 3.5, 0.0),
            (1001, 50001.0, 2.5, 1.0),
            (1002, 50002.0, 1.0, 0.0),
        )
        vp = build_volume_profile(ticks, tick_size=1.0)
        level_sum = sum(lv.total_vol for lv in vp.levels.values())
        self.assertAlmostEqual(vp.total_volume, level_sum, places=8)

    def test_buy_sell_sum_equals_total(self):
        ticks = _ticks(
            (1000, 50000.0, 3.0, 0.0),
            (1001, 50000.0, 2.0, 1.0),
        )
        vp = build_volume_profile(ticks, tick_size=1.0)
        for lv in vp.levels.values():
            self.assertAlmostEqual(lv.buy_vol + lv.sell_vol, lv.total_vol, places=8)

    def test_value_area_pct_1_covers_all_levels(self):
        ticks = _ticks(
            (1000, 50000.0, 10.0, 0.0),
            (1001, 50010.0, 5.0,  0.0),
            (1002, 49990.0, 3.0,  1.0),
        )
        vp = build_volume_profile(ticks, tick_size=1.0, value_area_pct=1.0)
        self.assertAlmostEqual(vp.val, 49990.0)
        self.assertAlmostEqual(vp.vah, 50010.0)

    def test_levels_sorted_ascending(self):
        ticks = _ticks(
            (1000, 50002.0, 1.0, 0.0),
            (1001, 50000.0, 2.0, 0.0),
            (1002, 50001.0, 3.0, 0.0),
        )
        vp = build_volume_profile(ticks, tick_size=1.0)
        prices = list(vp.levels.keys())
        self.assertEqual(prices, sorted(prices))

    def test_zero_qty_ticks_returns_none(self):
        ticks = _ticks(
            (1000, 50000.0, 0.0, 0.0),
            (1001, 50001.0, 0.0, 1.0),
        )
        vp = build_volume_profile(ticks, tick_size=1.0)
        self.assertIsNone(vp)


# ─── VolumeProfile convenience methods ───────────────────────────────────────

class TestVolumeProfileMethods(unittest.TestCase):

    def _make_vp(self) -> VolumeProfile:
        ticks = _ticks(
            (1000, 49990.0, 5.0,  0.0),
            (1001, 50000.0, 100.0, 0.0),  # POC
            (1002, 50010.0, 5.0,  1.0),
        )
        vp = build_volume_profile(ticks, tick_size=1.0)
        assert vp is not None
        return vp

    def test_poc_is_max_volume(self):
        vp = self._make_vp()
        self.assertAlmostEqual(vp.poc_price, 50000.0)

    def test_is_in_value_area(self):
        vp = self._make_vp()
        self.assertTrue(vp.is_in_value_area(vp.poc_price))
        self.assertTrue(vp.is_in_value_area(vp.val))
        self.assertTrue(vp.is_in_value_area(vp.vah))

    def test_nearest_support_below_poc(self):
        vp = self._make_vp()
        sup = vp.nearest_support(50005.0)
        self.assertIsNotNone(sup)
        self.assertLess(sup, 50005.0)

    def test_nearest_resistance_above_poc(self):
        vp = self._make_vp()
        res = vp.nearest_resistance(49995.0)
        self.assertIsNotNone(res)
        self.assertGreater(res, 49995.0)

    def test_nearest_support_none_below_lowest(self):
        ticks = _ticks((1000, 50000.0, 1.0, 0.0))
        vp = build_volume_profile(ticks, tick_size=1.0)
        sup = vp.nearest_support(49000.0)  # below all levels
        self.assertIsNone(sup)

    def test_nearest_resistance_none_above_highest(self):
        ticks = _ticks((1000, 50000.0, 1.0, 0.0))
        vp = build_volume_profile(ticks, tick_size=1.0)
        res = vp.nearest_resistance(51000.0)  # above all levels
        self.assertIsNone(res)

    def test_value_area_volume_ge_target(self):
        vp = self._make_vp()
        self.assertGreaterEqual(vp.value_area_volume, vp.total_volume * vp.value_area_pct)


# ─── build_bar_profiles ───────────────────────────────────────────────────────

class TestBuildBarProfiles(unittest.TestCase):

    def test_returns_profile_per_bar(self):
        tick_map = {
            1 * _MS_1M: _ticks((1000, 50000.0, 1.0, 0.0)),
            2 * _MS_1M: _ticks((2000, 50001.0, 2.0, 1.0)),
        }
        profiles = build_bar_profiles(tick_map, tick_size=1.0)
        self.assertIn(1 * _MS_1M, profiles)
        self.assertIn(2 * _MS_1M, profiles)
        self.assertEqual(len(profiles), 2)

    def test_empty_bar_excluded(self):
        tick_map = {
            1 * _MS_1M: _ticks((1000, 50000.0, 1.0, 0.0)),
            2 * _MS_1M: _ticks(),
        }
        profiles = build_bar_profiles(tick_map, tick_size=1.0)
        self.assertIn(1 * _MS_1M, profiles)
        self.assertNotIn(2 * _MS_1M, profiles)

    def test_empty_tick_map(self):
        profiles = build_bar_profiles({}, tick_size=1.0)
        self.assertEqual(profiles, {})

    def test_profiles_are_independent(self):
        tick_map = {
            1 * _MS_1M: _ticks((1000, 50000.0, 5.0, 0.0)),
            2 * _MS_1M: _ticks((2000, 51000.0, 3.0, 1.0)),
        }
        profiles = build_bar_profiles(tick_map, tick_size=1.0)
        self.assertAlmostEqual(profiles[1 * _MS_1M].poc_price, 50000.0)
        self.assertAlmostEqual(profiles[2 * _MS_1M].poc_price, 51000.0)


# ─── build_composite_profile ─────────────────────────────────────────────────

class TestBuildCompositeProfile(unittest.TestCase):

    def test_merges_multiple_bars(self):
        tick_map = {
            1 * _MS_1M: _ticks((1000, 50000.0, 10.0, 0.0)),
            2 * _MS_1M: _ticks((2000, 50000.0, 10.0, 1.0)),
        }
        vp = build_composite_profile(tick_map, [1 * _MS_1M, 2 * _MS_1M], tick_size=1.0)
        self.assertIsNotNone(vp)
        self.assertAlmostEqual(vp.total_volume, 20.0)

    def test_empty_open_times_returns_none(self):
        tick_map = {1 * _MS_1M: _ticks((1000, 50000.0, 1.0, 0.0))}
        vp = build_composite_profile(tick_map, [], tick_size=1.0)
        self.assertIsNone(vp)

    def test_missing_open_times_returns_none(self):
        tick_map = {}
        vp = build_composite_profile(tick_map, [1 * _MS_1M], tick_size=1.0)
        self.assertIsNone(vp)

    def test_partial_missing_bars_uses_available(self):
        tick_map = {
            1 * _MS_1M: _ticks((1000, 50000.0, 5.0, 0.0)),
            # bar 2 missing
        }
        vp = build_composite_profile(tick_map, [1 * _MS_1M, 2 * _MS_1M], tick_size=1.0)
        self.assertIsNotNone(vp)
        self.assertAlmostEqual(vp.total_volume, 5.0)

    def test_buy_sell_preserved_after_merge(self):
        tick_map = {
            1 * _MS_1M: _ticks((1000, 50000.0, 3.0, 0.0)),  # buy
            2 * _MS_1M: _ticks((2000, 50000.0, 2.0, 1.0)),  # sell
        }
        vp = build_composite_profile(tick_map, [1 * _MS_1M, 2 * _MS_1M], tick_size=1.0)
        lv = vp.level_at(50000.0)
        self.assertAlmostEqual(lv.buy_vol, 3.0)
        self.assertAlmostEqual(lv.sell_vol, 2.0)


# ─── build_rolling_profiles ───────────────────────────────────────────────────

class TestBuildRollingProfiles(unittest.TestCase):

    def _make_tick_map(self, n: int) -> dict:
        """n bars each with 1 tick at price = bar_index * 1.0"""
        return {
            i * _MS_1M: _ticks((i * 1000, float(i), 1.0, 0.0))
            for i in range(n)
        }

    def test_returns_profile_for_each_kline(self):
        n = 5
        klines = [_k(i) for i in range(n)]
        tick_map = self._make_tick_map(n)
        profiles = build_rolling_profiles(tick_map, klines, window=3, tick_size=1.0)
        self.assertEqual(len(profiles), n)

    def test_empty_klines_returns_empty(self):
        profiles = build_rolling_profiles({}, [], window=3, tick_size=1.0)
        self.assertEqual(profiles, {})

    def test_window_size_1_equals_bar_profiles(self):
        n = 3
        klines = [_k(i) for i in range(n)]
        tick_map = self._make_tick_map(n)
        rolling = build_rolling_profiles(tick_map, klines, window=1, tick_size=1.0)
        bar    = build_bar_profiles(tick_map, tick_size=1.0)
        for k in klines:
            ot = k.open_time
            if ot in rolling and ot in bar:
                self.assertAlmostEqual(rolling[ot].total_volume, bar[ot].total_volume)

    def test_window_larger_than_series_uses_all(self):
        n = 3
        klines = [_k(i) for i in range(n)]
        tick_map = self._make_tick_map(n)
        profiles = build_rolling_profiles(tick_map, klines, window=100, tick_size=1.0)
        # Last bar should include all n bars' ticks
        last_ot = klines[-1].open_time
        self.assertIn(last_ot, profiles)
        self.assertAlmostEqual(profiles[last_ot].total_volume, float(n))

    def test_profile_open_times_match_klines(self):
        n = 4
        klines = [_k(i) for i in range(n)]
        tick_map = self._make_tick_map(n)
        profiles = build_rolling_profiles(tick_map, klines, window=2, tick_size=1.0)
        for k in klines:
            self.assertIn(k.open_time, profiles)


if __name__ == "__main__":
    unittest.main()
