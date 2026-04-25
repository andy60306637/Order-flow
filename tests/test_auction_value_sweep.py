import types
import unittest
from unittest.mock import patch

import numpy as np

from core.data_types import Kline
from core.volume_profile import VolumeProfile
from strategies import STRATEGY_REGISTRY
from strategies.auction_value_sweep import AuctionValueSweepStrategy
from strategies.base import StrategySignal


_MS_1M = 60_000
_MS_15M = 15 * _MS_1M
_MS_30M = 30 * _MS_1M
_MS_1H = 60 * _MS_1M
_MS_4H = 4 * _MS_1H
_MS_8H = 8 * _MS_1H
_MS_1D = 86_400_000


def _k(
    i: int,
    o: float = 100.0,
    h: float = 101.0,
    l: float = 99.0,
    c: float = 100.5,
    vol: float = 1000.0,
    tbv: float = 300.0,
    base_time: int = 0,
) -> Kline:
    ot = base_time + i * _MS_1M
    return Kline(
        symbol="BTCUSDT",
        interval="1m",
        open_time=ot,
        close_time=ot + _MS_1M - 1,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=vol,
        taker_buy_volume=tbv,
        is_closed=True,
    )


def _ticks(open_time: int, rows: list[tuple[int, float, float, float]]) -> np.ndarray:
    return np.array(
        [(open_time + dt, price, qty, maker) for dt, price, qty, maker in rows],
        dtype=np.float64,
    )


def _vp(poc: float, val: float, vah: float) -> VolumeProfile:
    return VolumeProfile(
        levels={},
        tick_size=1.0,
        poc_price=poc,
        vah=vah,
        val=val,
        total_volume=1.0,
        value_area_pct=0.70,
        hvn_prices=[],
        lvn_prices=[],
    )


class TestAuctionValueSweep(unittest.TestCase):
    def test_strategy_registered(self):
        self.assertIn("Auction Value Sweep", STRATEGY_REGISTRY)
        self.assertIs(STRATEGY_REGISTRY["Auction Value Sweep"], AuctionValueSweepStrategy)

    def test_vp_for_bar_uses_previous_utc_day_only(self):
        strat = AuctionValueSweepStrategy()

        day_start = 2 * _MS_1D
        prev_day_vp = _vp(poc=101.0, val=100.0, vah=102.0)
        same_day_vp = _vp(poc=201.0, val=200.0, vah=202.0)
        daily_vp = {
            day_start - _MS_1D: prev_day_vp,
            day_start: same_day_vp,
        }
        k = _k(0, base_time=day_start)

        self.assertIs(strat._vp_for_bar(k, daily_vp), prev_day_vp)

    def test_vp_for_bar_never_fallbacks_to_same_day(self):
        strat = AuctionValueSweepStrategy()

        day_start = 3 * _MS_1D
        same_day_vp = _vp(poc=301.0, val=300.0, vah=302.0)
        daily_vp = {day_start: same_day_vp}
        k = _k(0, base_time=day_start)

        self.assertIsNone(strat._vp_for_bar(k, daily_vp))

    def test_vp_bucket_ms_supports_expected_intervals(self):
        strat = AuctionValueSweepStrategy()
        expected = {
            "15m": _MS_15M,
            "30m": _MS_30M,
            "1h": _MS_1H,
            "4h": _MS_4H,
            "8h": _MS_8H,
            "24h": _MS_1D,
        }
        for interval, ms in expected.items():
            strat.vp_interval = interval
            self.assertEqual(strat._vp_bucket_ms(), ms)

    def test_vp_bucket_ms_rejects_unsupported_interval(self):
        strat = AuctionValueSweepStrategy()
        strat.vp_interval = "2h"
        with self.assertRaisesRegex(ValueError, "vp_interval must be one of"):
            strat._vp_bucket_ms()

    def test_vp_for_bar_uses_previous_interval_bucket_only(self):
        strat = AuctionValueSweepStrategy()
        strat.vp_interval = "1h"

        bucket_start = 5 * _MS_1H
        prev_bucket_vp = _vp(poc=151.0, val=150.0, vah=152.0)
        same_bucket_vp = _vp(poc=251.0, val=250.0, vah=252.0)
        bucket_vp = {
            bucket_start - _MS_1H: prev_bucket_vp,
            bucket_start: same_bucket_vp,
        }
        k = _k(0, base_time=bucket_start + 5 * _MS_1M)

        self.assertIs(strat._vp_for_bar(k, bucket_vp), prev_bucket_vp)

    def test_build_daily_vp_cache_groups_bars_by_interval_bucket(self):
        strat = AuctionValueSweepStrategy()
        strat.vp_interval = "30m"

        k0 = _k(0, base_time=0)
        k10 = _k(0, base_time=10 * _MS_1M)
        k20 = _k(0, base_time=20 * _MS_1M)
        k35 = _k(0, base_time=35 * _MS_1M)
        klines = [k0, k10, k20, k35]
        tick_map = {
            k.open_time: _ticks(k.open_time, [(1, 100.0, 1.0, 1.0)])
            for k in klines
        }

        called_open_times: list[tuple[int, ...]] = []

        def fake_build(_tick_map, open_times, **_kwargs):
            called_open_times.append(tuple(open_times))
            return _vp(poc=100.0, val=99.0, vah=101.0)

        with patch("strategies.auction_value_sweep.build_composite_profile", side_effect=fake_build):
            cache = strat._build_daily_vp_cache(klines, tick_map)

        self.assertEqual(set(cache.keys()), {0, _MS_30M})
        self.assertIn((k0.open_time, k10.open_time, k20.open_time), called_open_times)
        self.assertIn((k35.open_time,), called_open_times)

    def test_k0_confirms_on_closed_bar_and_entry_starts_next_bar(self):
        strat = AuctionValueSweepStrategy()
        strat.enable_session_filter = False
        strat.enable_short = False

        k0 = _k(0, o=100.0, h=110.0, l=90.0, c=105.0)
        k1 = _k(1, o=105.0, h=111.0, l=104.0, c=110.0)
        klines = [k0, k1]
        tick_map = {
            k0.open_time: _ticks(k0.open_time, [(1, 100.0, 1.0, 1.0)]),
            k1.open_time: _ticks(k1.open_time, [(1, 106.0, 1.0, 0.0)]),
        }
        fake_vp = _vp(poc=108.0, val=100.0, vah=105.0)
        called_entry_indices: list[int] = []

        def fake_build_daily_vp_cache(self, _klines, _tick_map):
            return {0: fake_vp}

        def fake_vp_for_bar(self, _kline, _daily_vp):
            return fake_vp

        def fake_detect_long_scenario(self, kline, _vp_obj, _ticks_obj):
            if kline.open_time == k0.open_time:
                return "vah_retest"
            return None

        def fake_tick_entry_long(
            self,
            kline,
            i,
            _klines,
            _tick_map,
            signals,
            k0_obj,
            _scenario,
            _vp_obj,
        ):
            called_entry_indices.append(i)
            fill_price = max(k0_obj.open, k0_obj.close) + 0.1
            stop_price = k0_obj.low - 1.0
            target_price = fill_price + 5.0
            signals.append(
                StrategySignal(
                    open_time=kline.open_time,
                    price=max(k0_obj.open, k0_obj.close),
                    signal_type="long_entry",
                    label="L4A",
                    stop_price=stop_price,
                    fill_price=fill_price,
                )
            )
            return True, fill_price, stop_price, target_price

        strat._build_daily_vp_cache = types.MethodType(fake_build_daily_vp_cache, strat)
        strat._vp_for_bar = types.MethodType(fake_vp_for_bar, strat)
        strat._detect_long_scenario = types.MethodType(fake_detect_long_scenario, strat)
        strat._tick_entry_long_avs = types.MethodType(fake_tick_entry_long, strat)

        signals = strat.on_history(klines, tick_map=tick_map)

        k0_signals = [s for s in signals if s.signal_type == "k0_long"]
        long_entries = [s for s in signals if s.signal_type == "long_entry"]

        self.assertEqual(len(k0_signals), 1)
        self.assertEqual(k0_signals[0].open_time, k0.open_time)
        self.assertEqual(called_entry_indices, [1])
        self.assertEqual(len(long_entries), 1)
        self.assertEqual(long_entries[0].open_time, k1.open_time)

    def test_unsorted_ticks_raise_value_error(self):
        strat = AuctionValueSweepStrategy()
        klines = [_k(0), _k(1)]

        tick_map = {
            klines[0].open_time: _ticks(
                klines[0].open_time,
                [
                    (20, 100.1, 1.0, 1.0),
                    (10, 100.2, 1.0, 0.0),
                ],
            ),
            klines[1].open_time: _ticks(
                klines[1].open_time,
                [
                    (10, 100.3, 1.0, 1.0),
                    (20, 100.4, 1.0, 0.0),
                ],
            ),
        }

        with self.assertRaisesRegex(ValueError, "sorted by trade timestamp ascending"):
            strat.on_history(klines, tick_map=tick_map)


if __name__ == "__main__":
    unittest.main()
