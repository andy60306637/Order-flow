from __future__ import annotations

import unittest

import numpy as np

from core.data_types import Kline
from research.registry import ensure_builtin_factors, list_factors
from research.runner import analyze_factors


def _k(i: int, close: float, volume: float = 100.0, buy: float = 50.0) -> Kline:
    return Kline(
        symbol="BTCUSDT",
        interval="1m",
        open_time=i * 60_000,
        close_time=i * 60_000 + 59_999,
        open=close - 1.0,
        high=close + 2.0,
        low=close - 2.0,
        close=close,
        volume=volume,
        taker_buy_volume=buy,
        is_closed=True,
    )


class ResearchRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        ensure_builtin_factors()

    def test_builtin_registry_contains_ohlcv_and_tick_factors(self) -> None:
        names = list_factors(include_tick=True)
        self.assertIn("range_pct", names)
        self.assertIn("delta_eff", names)
        self.assertIn("wick_delta_eff", names)

    def test_analyze_factors_computes_ic_and_quantiles(self) -> None:
        closes = [100, 101, 103, 106, 110, 115, 121, 128]
        klines = [_k(i, close, volume=100 + i * 10, buy=55 + i) for i, close in enumerate(closes)]

        result = analyze_factors(
            klines=klines,
            tick_map=None,
            factor_names=["return_1", "range_pct", "delta_eff"],
            horizons=[1, 3],
            quantiles=3,
            use_tick_features=False,
        )

        self.assertEqual(result.rows, len(klines))
        self.assertGreaterEqual(len(result.summary), 3)
        metric = next(
            row for row in result.metrics
            if row["factor"] == "return_1" and row["horizon"] == 1
        )
        self.assertGreater(metric["sample_count"], 3)
        self.assertTrue(np.isfinite(metric["ic"]))
        self.assertTrue(result.quantiles)

    def test_tick_required_factor_is_marked_unavailable_without_tick_map(self) -> None:
        klines = [_k(i, 100 + i) for i in range(6)]
        result = analyze_factors(
            klines=klines,
            tick_map=None,
            factor_names=["wick_delta_eff"],
            horizons=[1],
            quantiles=5,
            use_tick_features=True,
        )

        self.assertEqual(result.summary, [])
        self.assertEqual(result.unavailable[0]["factor"], "wick_delta_eff")
        self.assertEqual(result.unavailable[0]["reason"], "tick_data_unavailable")

    def test_tick_factor_uses_tick_map_when_available(self) -> None:
        klines = [_k(i, 100 + i, volume=10.0, buy=5.0) for i in range(8)]
        tick_map = {
            k.open_time: np.array([
                [k.open_time + 1, k.low, 2.0, 0.0],
                [k.open_time + 2, k.high, 3.0, 1.0],
            ], dtype=np.float64)
            for k in klines
        }

        result = analyze_factors(
            klines=klines,
            tick_map=tick_map,
            factor_names=["wick_volume_ratio", "wick_delta_eff"],
            horizons=[1],
            quantiles=3,
            use_tick_features=True,
        )

        self.assertEqual(result.unavailable, [])
        self.assertEqual({row["factor"] for row in result.summary}, {"wick_volume_ratio", "wick_delta_eff"})


if __name__ == "__main__":
    unittest.main()
