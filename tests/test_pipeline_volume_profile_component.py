"""tests/test_pipeline_volume_profile_component.py — VolumeProfileComponent 單元測試"""
import unittest

import numpy as np

from core.data_types import Kline
from strategies.pipeline.component import VolumeProfileComponent

_MS_1M = 60_000

EXPECTED_KEYS = frozenset({
    "poc_price", "vah", "val",
    "poc_band", "vah_band", "val_band",
    "price_in_poc_band", "price_in_vah_band", "price_in_val_band",
    "hvn_prices", "lvn_prices",
    "in_value_area", "above_poc", "total_volume", "source",
})


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _k(
    i: int,
    close: float = 50_000.0,
    volume: float = 100.0,
    taker_buy: float = 60.0,
    high: float | None = None,
    low: float | None = None,
) -> Kline:
    ot = i * _MS_1M
    return Kline(
        symbol="BTCUSDT", interval="1m",
        open_time=ot, close_time=ot + _MS_1M - 1,
        open=close,
        high=close + 50 if high is None else high,
        low=close - 50 if low is None else low,
        close=close,
        volume=volume, taker_buy_volume=taker_buy, is_closed=True,
    )


def _ticks(*rows) -> np.ndarray:
    if not rows:
        return np.empty((0, 4), dtype=np.float64)
    return np.array(rows, dtype=np.float64)


def _default_comp(**kwargs) -> VolumeProfileComponent:
    defaults = dict(interval="1m", window=5, tick_size=1.0)
    defaults.update(kwargs)
    return VolumeProfileComponent(**defaults)


def _klines(n: int = 5, **k_kwargs) -> list[Kline]:
    return [_k(i, **k_kwargs) for i in range(n)]


# ─── component_id ─────────────────────────────────────────────────────────────

class TestVolumeProfileComponentId(unittest.TestCase):

    def test_default_component_id(self):
        # interval=1h, window=24, va_pct=70, tb_bp=int(round(0.001*10000))=10
        comp = VolumeProfileComponent()
        self.assertEqual(comp.component_id, "volume_profile_1h_24_va70_tb10")

    def test_custom_interval_and_window(self):
        comp = VolumeProfileComponent(interval="5m", window=12)
        self.assertIn("5m", comp.component_id)
        self.assertIn("12", comp.component_id)

    def test_interval_spaces_stripped(self):
        comp = VolumeProfileComponent(interval="1 h", window=5)
        self.assertIn("1h", comp.component_id)
        self.assertNotIn(" ", comp.component_id)

    def test_interval_uppercased_normalised(self):
        comp = VolumeProfileComponent(interval="1H", window=5)
        self.assertIn("1h", comp.component_id)

    def test_value_area_pct_encoded(self):
        comp = VolumeProfileComponent(value_area_pct=0.80)
        self.assertIn("va80", comp.component_id)

    def test_touch_band_pct_encoded_in_basis_points(self):
        # 0.002 × 10000 = 20 basis points
        comp = VolumeProfileComponent(touch_band_pct=0.002)
        self.assertIn("tb20", comp.component_id)

    def test_custom_touch_band_pct_overrides_default(self):
        comp = VolumeProfileComponent(touch_band_pct=0.005)
        self.assertIn("tb50", comp.component_id)
        self.assertEqual(comp.touch_band_pct, 0.005)


# ─── DEFAULT_TOUCH_BAND_PCT class-attribute override ─────────────────────────

class TestDefaultTouchBandPct(unittest.TestCase):

    def test_subclass_can_override_default(self):
        class HighBandComp(VolumeProfileComponent):
            DEFAULT_TOUCH_BAND_PCT = 0.005

        comp = HighBandComp(interval="1m", window=5)
        self.assertAlmostEqual(comp.touch_band_pct, 0.005)
        self.assertIn("tb50", comp.component_id)

    def test_explicit_touch_band_pct_beats_class_default(self):
        class HighBandComp(VolumeProfileComponent):
            DEFAULT_TOUCH_BAND_PCT = 0.005

        comp = HighBandComp(interval="1m", window=5, touch_band_pct=0.001)
        self.assertAlmostEqual(comp.touch_band_pct, 0.001)


# ─── Result schema ────────────────────────────────────────────────────────────

class TestResultKeys(unittest.TestCase):

    def test_tick_path_result_has_all_keys(self):
        klines = _klines()
        tick_map = {
            klines[4].open_time: _ticks((4000, 50_000.0, 10.0, 0.0))
        }
        result = _default_comp().compute(klines, 4, tick_map=tick_map)
        self.assertEqual(set(result.keys()), EXPECTED_KEYS)

    def test_kline_fallback_result_has_all_keys(self):
        result = _default_comp().compute(_klines(), 4)
        self.assertEqual(set(result.keys()), EXPECTED_KEYS)

    def test_insufficient_data_result_has_all_keys(self):
        result = _default_comp().compute(_klines(volume=0.0, taker_buy=0.0), 4)
        self.assertEqual(set(result.keys()), EXPECTED_KEYS)


# ─── Source field ─────────────────────────────────────────────────────────────

class TestSourceField(unittest.TestCase):

    def test_tick_path_sets_source_tick(self):
        klines = _klines()
        tick_map = {
            klines[4].open_time: _ticks((4000, 50_000.0, 10.0, 0.0))
        }
        result = _default_comp().compute(klines, 4, tick_map=tick_map)
        self.assertEqual(result["source"], "tick")

    def test_kline_fallback_sets_source(self):
        result = _default_comp().compute(_klines(), 4)
        self.assertEqual(result["source"], "kline_fallback")

    def test_zero_volume_klines_sets_insufficient_data(self):
        result = _default_comp().compute(_klines(volume=0.0, taker_buy=0.0), 4)
        self.assertEqual(result["source"], "insufficient_data")

    def test_empty_tick_array_falls_back_to_klines(self):
        klines = _klines()
        tick_map = {klines[4].open_time: _ticks()}   # empty ndarray
        result = _default_comp().compute(klines, 4, tick_map=tick_map)
        # build_composite_profile skips empty arrays → kline fallback
        self.assertEqual(result["source"], "kline_fallback")

    def test_tick_map_missing_key_falls_back_to_klines(self):
        klines = _klines()
        tick_map = {}   # no entry for any bar
        result = _default_comp().compute(klines, 4, tick_map=tick_map)
        self.assertEqual(result["source"], "kline_fallback")


# ─── Tick path computation ────────────────────────────────────────────────────

class TestComputeTickPath(unittest.TestCase):

    def _tick_result(self, price_ticks, close=50_000.0, window=1):
        klines = _klines(close=close)
        tick_map = {klines[4].open_time: _ticks(*price_ticks)}
        comp = _default_comp(window=window)
        return comp.compute(klines, 4, tick_map=tick_map)

    def test_poc_at_highest_volume_price(self):
        result = self._tick_result([
            (4000, 49_990.0, 5.0,  0.0),
            (4001, 50_000.0, 80.0, 0.0),   # dominant
            (4002, 50_010.0, 5.0,  1.0),
        ])
        self.assertAlmostEqual(result["poc_price"], 50_000.0)

    def test_vah_ge_poc_ge_val(self):
        result = self._tick_result([
            (4000, 49_990.0, 10.0, 0.0),
            (4001, 50_000.0, 50.0, 0.0),
            (4002, 50_010.0, 10.0, 1.0),
        ])
        self.assertGreaterEqual(result["vah"], result["poc_price"])
        self.assertLessEqual(result["val"], result["poc_price"])

    def test_total_volume_matches_tick_sum(self):
        result = self._tick_result([
            (4000, 50_000.0, 3.5, 0.0),
            (4001, 50_001.0, 2.5, 1.0),
        ])
        self.assertAlmostEqual(result["total_volume"], 6.0)

    def test_hvn_and_lvn_are_lists(self):
        result = self._tick_result([(4000, 50_000.0, 10.0, 0.0)])
        self.assertIsInstance(result["hvn_prices"], list)
        self.assertIsInstance(result["lvn_prices"], list)


# ─── Kline fallback computation ───────────────────────────────────────────────

class TestComputeKlineFallback(unittest.TestCase):

    def test_total_volume_sums_all_window_bars(self):
        # 5 klines, window=5, each with volume=100
        klines = _klines(volume=100.0, taker_buy=60.0)
        result = _default_comp(window=5).compute(klines, 4)
        self.assertAlmostEqual(result["total_volume"], 500.0)

    def test_poc_price_is_finite(self):
        result = _default_comp().compute(_klines(), 4)
        self.assertTrue(np.isfinite(result["poc_price"]))

    def test_vah_ge_val(self):
        result = _default_comp().compute(_klines(), 4)
        self.assertGreaterEqual(result["vah"], result["val"])

    def test_klines_with_zero_volume_excluded_from_profile(self):
        # 4 zero-vol bars + 1 bar with volume → should still produce kline_fallback
        klines = [_k(i, volume=0.0, taker_buy=0.0) for i in range(4)]
        klines.append(_k(4, volume=100.0, taker_buy=50.0))
        result = _default_comp(window=5).compute(klines, 4)
        self.assertEqual(result["source"], "kline_fallback")
        self.assertAlmostEqual(result["total_volume"], 100.0)


# ─── Insufficient data (empty result) ────────────────────────────────────────

class TestInsufficientData(unittest.TestCase):

    def _zero_vol_result(self):
        return _default_comp().compute(_klines(volume=0.0, taker_buy=0.0), 4)

    def test_poc_price_equals_current_close(self):
        result = self._zero_vol_result()
        self.assertAlmostEqual(result["poc_price"], 50_000.0)

    def test_vah_equals_val_equals_current_close(self):
        result = self._zero_vol_result()
        self.assertAlmostEqual(result["vah"], 50_000.0)
        self.assertAlmostEqual(result["val"], 50_000.0)

    def test_boolean_flags_are_false(self):
        result = self._zero_vol_result()
        self.assertFalse(result["price_in_poc_band"])
        self.assertFalse(result["price_in_vah_band"])
        self.assertFalse(result["price_in_val_band"])
        self.assertFalse(result["in_value_area"])
        self.assertFalse(result["above_poc"])

    def test_total_volume_is_zero(self):
        result = self._zero_vol_result()
        self.assertAlmostEqual(result["total_volume"], 0.0)

    def test_hvn_and_lvn_are_empty(self):
        result = self._zero_vol_result()
        self.assertEqual(result["hvn_prices"], [])
        self.assertEqual(result["lvn_prices"], [])


# ─── Touch band ───────────────────────────────────────────────────────────────

class TestTouchBand(unittest.TestCase):

    def _result_close_at_poc(self, touch_band_pct: float, close: float = 50_000.0):
        klines = _klines(close=close)
        # Single tick at close → POC == close
        tick_map = {klines[4].open_time: _ticks((4000, close, 10.0, 0.0))}
        comp = _default_comp(window=1, touch_band_pct=touch_band_pct)
        return comp.compute(klines, 4, tick_map=tick_map)

    def test_band_width_is_two_times_band_size(self):
        close = 50_000.0
        pct = 0.01   # band_size = 500
        result = self._result_close_at_poc(pct, close)
        lo, hi = result["poc_band"]
        self.assertAlmostEqual(hi - lo, 2 * close * pct, places=6)

    def test_price_in_poc_band_true_when_close_equals_poc(self):
        result = self._result_close_at_poc(0.001)
        self.assertTrue(result["price_in_poc_band"])

    def test_price_in_poc_band_false_when_poc_far_below(self):
        # POC at 49_000, close at 50_000 → gap=1000, band_size=50 → out of band
        klines = _klines(close=50_000.0)
        tick_map = {klines[4].open_time: _ticks(
            (4000, 49_000.0, 80.0, 0.0),   # dominant POC far below
            (4001, 50_000.0, 1.0,  0.0),
        )}
        result = _default_comp(window=1, touch_band_pct=0.001).compute(klines, 4, tick_map=tick_map)
        self.assertFalse(result["price_in_poc_band"])

    def test_band_tuples_are_ordered_low_high(self):
        result = self._result_close_at_poc(0.001)
        for key in ("poc_band", "vah_band", "val_band"):
            lo, hi = result[key]
            self.assertLessEqual(lo, hi)


# ─── Flags: above_poc / in_value_area ────────────────────────────────────────

class TestFlags(unittest.TestCase):

    def test_above_poc_true_when_close_above_poc(self):
        # POC at 49_000 (dominant), close at 50_000
        klines = _klines(close=50_000.0)
        tick_map = {klines[4].open_time: _ticks(
            (4000, 49_000.0, 80.0, 0.0),
            (4001, 50_000.0, 10.0, 0.0),
        )}
        result = _default_comp(window=1).compute(klines, 4, tick_map=tick_map)
        self.assertTrue(result["above_poc"])

    def test_above_poc_false_when_close_equals_poc(self):
        # Single price level → POC == close
        klines = _klines(close=50_000.0)
        tick_map = {klines[4].open_time: _ticks((4000, 50_000.0, 10.0, 0.0))}
        result = _default_comp(window=1).compute(klines, 4, tick_map=tick_map)
        self.assertFalse(result["above_poc"])

    def test_above_poc_false_when_close_below_poc(self):
        # POC at 51_000 (dominant), close at 50_000
        klines = _klines(close=50_000.0)
        tick_map = {klines[4].open_time: _ticks(
            (4000, 51_000.0, 80.0, 0.0),
            (4001, 50_000.0, 10.0, 0.0),
        )}
        result = _default_comp(window=1).compute(klines, 4, tick_map=tick_map)
        self.assertFalse(result["above_poc"])

    def test_in_value_area_true_when_close_at_poc(self):
        klines = _klines(close=50_000.0)
        tick_map = {klines[4].open_time: _ticks((4000, 50_000.0, 10.0, 0.0))}
        result = _default_comp(window=1).compute(klines, 4, tick_map=tick_map)
        self.assertTrue(result["in_value_area"])

    def test_in_value_area_false_when_close_outside_va(self):
        # POC at 49_000, VA only covers 49_000 (single dominant level), close=50_000 outside
        klines = _klines(close=50_000.0)
        tick_map = {klines[4].open_time: _ticks(
            (4000, 49_000.0, 100.0, 0.0),  # POC, target 70% already met
            (4001, 50_000.0,   1.0, 0.0),
        )}
        result = _default_comp(window=1, value_area_pct=0.70).compute(klines, 4, tick_map=tick_map)
        self.assertFalse(result["in_value_area"])


# ─── Window slicing ───────────────────────────────────────────────────────────

class TestWindowSlicing(unittest.TestCase):

    def test_window_1_uses_only_current_bar(self):
        # Ticks only for bars 0–3 at a far-away price; bar 4 has local price.
        klines = _klines(close=50_000.0)
        tick_map = {
            klines[0].open_time: _ticks((0, 40_000.0, 100.0, 0.0)),
            klines[1].open_time: _ticks((1, 40_000.0, 100.0, 0.0)),
            klines[2].open_time: _ticks((2, 40_000.0, 100.0, 0.0)),
            klines[3].open_time: _ticks((3, 40_000.0, 100.0, 0.0)),
            klines[4].open_time: _ticks((4, 50_000.0,  10.0, 0.0)),
        }
        comp = _default_comp(window=1)
        result = comp.compute(klines, 4, tick_map=tick_map)
        self.assertAlmostEqual(result["poc_price"], 50_000.0)

    def test_window_5_accumulates_all_bars(self):
        klines = _klines(close=50_000.0)
        tick_map = {k.open_time: _ticks((k.open_time, 50_000.0, 10.0, 0.0)) for k in klines}
        comp = _default_comp(window=5)
        result = comp.compute(klines, 4, tick_map=tick_map)
        self.assertAlmostEqual(result["total_volume"], 50.0)  # 5 bars × 10

    def test_window_larger_than_available_klines_uses_all(self):
        klines = _klines(n=3, close=50_000.0)
        tick_map = {k.open_time: _ticks((k.open_time, 50_000.0, 10.0, 0.0)) for k in klines}
        comp = _default_comp(window=100)
        result = comp.compute(klines, 2, tick_map=tick_map)
        self.assertAlmostEqual(result["total_volume"], 30.0)  # 3 bars × 10

    def test_window_excludes_out_of_window_bars(self):
        # window=2 → only klines[3] and klines[4] are in window when idx=4
        klines = _klines(close=50_000.0)
        tick_map = {
            klines[0].open_time: _ticks((0, 40_000.0, 999.0, 0.0)),   # excluded
            klines[3].open_time: _ticks((3, 50_000.0,  10.0, 0.0)),
            klines[4].open_time: _ticks((4, 50_000.0,  10.0, 0.0)),
        }
        comp = _default_comp(window=2)
        result = comp.compute(klines, 4, tick_map=tick_map)
        self.assertAlmostEqual(result["poc_price"], 50_000.0)
        self.assertAlmostEqual(result["total_volume"], 20.0)


# ─── _build_from_klines (kline fallback internals) ───────────────────────────

class TestBuildFromKlines(unittest.TestCase):

    def test_taker_buy_volume_becomes_buy_vol(self):
        # taker_buy_volume = 60 out of volume = 100.
        # build_from_klines creates buy row (qty=60) and sell row (qty=40).
        # Both at same typical price → total at that level = 100.
        klines = [_k(i, close=50_000.0, volume=100.0, taker_buy=60.0) for i in range(1)]
        comp = _default_comp(window=1)
        result = comp.compute(klines, 0)
        self.assertAlmostEqual(result["total_volume"], 100.0)

    def test_zero_taker_buy_all_goes_to_sell(self):
        klines = [_k(0, volume=50.0, taker_buy=0.0)]
        comp = _default_comp(window=1)
        result = comp.compute(klines, 0)
        self.assertAlmostEqual(result["total_volume"], 50.0)

    def test_full_taker_buy_all_goes_to_buy(self):
        klines = [_k(0, volume=50.0, taker_buy=50.0)]
        comp = _default_comp(window=1)
        result = comp.compute(klines, 0)
        self.assertAlmostEqual(result["total_volume"], 50.0)


if __name__ == "__main__":
    unittest.main()
