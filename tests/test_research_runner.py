from __future__ import annotations

import unittest

import numpy as np

from core.data_types import Kline
from research.base import GROUP_MEAN_REVERSION, GROUP_MICROSTRUCTURE
from research.registry import ensure_builtin_factors, get_factor_info, list_factors
from research.runner import analyze_factors


def _k(i: int, close: float, lower_wick: float = 1.0, upper_wick: float = 1.0,
       volume: float = 100.0, buy: float = 50.0) -> Kline:
    open_ = close - 0.5
    body_lo = min(open_, close)
    body_hi = max(open_, close)
    return Kline(
        symbol="BTCUSDT",
        interval="1m",
        open_time=i * 60_000,
        close_time=i * 60_000 + 59_999,
        open=open_,
        high=body_hi + upper_wick,
        low=body_lo - lower_wick,
        close=close,
        volume=volume,
        taker_buy_volume=buy,
        is_closed=True,
    )


class ResearchRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        ensure_builtin_factors()

    def test_builtin_registry_contains_wick_and_tick_factors(self) -> None:
        names = list_factors(include_tick=True)
        self.assertIn("lower_wick_to_body_ratio", names)
        self.assertIn("upper_wick_to_body_ratio", names)
        self.assertIn("lower_wick_delta_eff", names)

    def test_builtin_factor_metadata_classifies_side_and_group(self) -> None:
        lower = get_factor_info("lower_wick_to_body_ratio")
        upper = get_factor_info("upper_wick_to_body_ratio")
        delta = get_factor_info("lower_wick_delta_eff")

        assert lower is not None and upper is not None and delta is not None
        self.assertEqual(lower["side"], "Long")
        self.assertEqual(upper["side"], "Short")
        self.assertEqual(lower["group"], GROUP_MEAN_REVERSION)
        self.assertEqual(delta["group"], GROUP_MICROSTRUCTURE)

    def test_analyze_factors_computes_ic_quantiles_and_correlations(self) -> None:
        rng = np.random.default_rng(7)
        n = 80
        lower_wicks = rng.uniform(0.1, 5.0, size=n)
        upper_wicks = rng.uniform(0.1, 5.0, size=n)
        # Construct close path so that bigger lower wick -> stronger next-bar bounce
        rets = 0.001 * lower_wicks - 0.001 * upper_wicks + rng.normal(0, 0.0005, size=n)
        closes = 100.0 * np.cumprod(1.0 + rets)
        klines = [
            _k(i, float(closes[i]), lower_wick=float(lower_wicks[i]), upper_wick=float(upper_wicks[i]))
            for i in range(n)
        ]

        result = analyze_factors(
            klines=klines,
            tick_map=None,
            factor_names=["lower_wick_to_body_ratio", "upper_wick_to_body_ratio"],
            horizons=[1, 3],
            quantiles=4,
            use_tick_features=False,
            entry_lag=1,
            min_period_samples=10,
            train_ratio=0.5,
        )

        self.assertEqual(result.rows, n)
        self.assertEqual(len(result.summary), 2)
        for row in result.summary:
            self.assertIn("oriented_rank_ic", row)
            self.assertIn("ic_ir", row)
            self.assertIn("orientation", row)
        # In-sample and out-of-sample quantile rows both present.
        samples = {row["sample"] for row in result.quantiles}
        self.assertEqual(samples, {"in_sample", "out_of_sample"})
        # Factor correlation matrix has at least one pair.
        self.assertEqual(len(result.factor_correlations), 1)
        pair = result.factor_correlations[0]
        self.assertIn("pearson", pair)
        self.assertIn("spearman", pair)

    def test_forward_return_uses_entry_lag(self) -> None:
        # With lag=1 and h=1, fwd[i] = close[i+2]/close[i+1] - 1.
        from research.runner import _forward_return

        close = np.array([100.0, 101.0, 102.0, 103.0, 104.0])
        out = _forward_return(close, horizon=1, entry_lag=1)
        self.assertAlmostEqual(out[0], 102.0 / 101.0 - 1.0, places=8)
        self.assertAlmostEqual(out[1], 103.0 / 102.0 - 1.0, places=8)
        self.assertAlmostEqual(out[2], 104.0 / 103.0 - 1.0, places=8)
        # last two entries cannot be evaluated -> NaN
        self.assertTrue(np.isnan(out[3]))
        self.assertTrue(np.isnan(out[4]))

    def test_orientation_flips_short_factor_sign(self) -> None:
        from research.runner import _factor_orientation, _orient

        self.assertEqual(_factor_orientation(("long",)), 1)
        self.assertEqual(_factor_orientation(("short",)), -1)
        self.assertEqual(_factor_orientation(("long", "short")), 0)
        self.assertAlmostEqual(_orient(0.05, -1), -0.05)
        self.assertAlmostEqual(_orient(-0.05, -1), 0.05)
        self.assertAlmostEqual(_orient(-0.05, 0), 0.05)

    def test_tick_required_factor_is_marked_unavailable_without_tick_map(self) -> None:
        klines = [_k(i, 100 + i) for i in range(6)]
        result = analyze_factors(
            klines=klines,
            tick_map=None,
            factor_names=["lower_wick_delta_eff"],
            horizons=[1],
            quantiles=5,
            use_tick_features=True,
        )

        self.assertEqual(result.summary, [])
        self.assertEqual(result.unavailable[0]["factor"], "lower_wick_delta_eff")
        self.assertEqual(result.unavailable[0]["reason"], "tick_data_unavailable")


if __name__ == "__main__":
    unittest.main()
