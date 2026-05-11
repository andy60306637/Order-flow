from __future__ import annotations

import unittest

import numpy as np

from core.data_types import Kline
from research.base import (
    GROUP_CRYPTO_DERIVATIVES,
    GROUP_MEAN_REVERSION,
    GROUP_MICROSTRUCTURE,
    FACTOR_SIDE_LONG,
    FACTOR_SIDES,
    FactorBase,
)
from research.registry import ensure_builtin_factors, get_factor, get_factor_info, list_factors, register_factor
from research.runner import analyze_factors


@register_factor
class _TestOosGoodFactor(FactorBase):
    name = "_test_oos_good_factor"
    sides = (FACTOR_SIDE_LONG,)

    def compute(self, klines: list[Kline], tick_map=None) -> np.ndarray:
        return np.array([k.taker_buy_volume for k in klines], dtype=np.float64)


@register_factor
class _TestOosBadFactor(FactorBase):
    name = "_test_oos_bad_factor"
    sides = (FACTOR_SIDE_LONG,)

    def compute(self, klines: list[Kline], tick_map=None) -> np.ndarray:
        return np.array([k.volume for k in klines], dtype=np.float64)


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
        self.assertIn("lower_wick_delta_eff_mr", names)
        self.assertIn("delta_eff_long", names)
        self.assertIn("delta_eff_short", names)
        self.assertIn("funding_rate", names)
        self.assertIn("open_interest_delta_15m", names)
        self.assertIn("liq_imbalance_1m", names)

    def test_builtin_factor_metadata_classifies_side_and_group(self) -> None:
        lower = get_factor_info("lower_wick_to_body_ratio")
        upper = get_factor_info("upper_wick_to_body_ratio")
        delta = get_factor_info("lower_wick_delta_eff")
        delta_mr = get_factor_info("lower_wick_delta_eff_mr")
        delta_long = get_factor_info("delta_eff_long")
        delta_short = get_factor_info("delta_eff_short")
        mr_lwde = get_factor_info("mr_lwde_eff")
        mr_rbu = get_factor_info("mr_rbu_strength")
        mr_cvdd = get_factor_info("mr_cvdd_divergence")

        assert lower is not None and upper is not None and delta is not None and delta_mr is not None
        assert delta_long is not None and delta_short is not None
        assert mr_lwde is not None and mr_rbu is not None and mr_cvdd is not None
        self.assertEqual(lower["side"], "Long")
        self.assertEqual(upper["side"], "Short")
        self.assertEqual(delta_mr["side"], "Long")
        self.assertEqual(delta_long["side"], "Long")
        self.assertEqual(delta_short["side"], "Short")
        self.assertEqual(lower["group"], GROUP_MEAN_REVERSION)
        self.assertEqual(delta["group"], GROUP_MICROSTRUCTURE)
        self.assertEqual(delta_mr["group"], GROUP_MEAN_REVERSION)
        self.assertEqual(mr_lwde["group"], GROUP_MEAN_REVERSION)
        self.assertEqual(mr_rbu["group"], GROUP_MEAN_REVERSION)
        self.assertEqual(mr_cvdd["group"], GROUP_MEAN_REVERSION)
        funding = get_factor_info("funding_rate")
        assert funding is not None
        self.assertEqual(funding["group"], GROUP_CRYPTO_DERIVATIVES)

    def test_lower_wick_delta_eff_mean_reversion_alias_matches_source_factor(self) -> None:
        source = get_factor("lower_wick_delta_eff")
        alias = get_factor("lower_wick_delta_eff_mr")
        assert source is not None and alias is not None
        klines = [_k(0, 100.0)]
        tick_map = {
            klines[0].open_time: np.array([
                [0.0, 99.0, 2.0, 0.0],
                [1.0, 99.4, 1.0, 1.0],
                [2.0, 101.0, 10.0, 0.0],
            ], dtype=np.float64)
        }

        np.testing.assert_allclose(
            alias.compute(klines, tick_map),
            source.compute(klines, tick_map),
        )

    def test_delta_eff_atomic_long_short_factors_are_opposites(self) -> None:
        klines = [
            _k(0, 100.0, volume=100.0, buy=75.0),
            _k(1, 101.0, volume=100.0, buy=25.0),
            _k(2, 102.0, volume=0.0, buy=0.0),
        ]
        long_factor = get_factor("delta_eff_long")
        short_factor = get_factor("delta_eff_short")

        assert long_factor is not None and short_factor is not None
        long_values = long_factor.compute(klines)
        short_values = short_factor.compute(klines)

        np.testing.assert_allclose(long_values[:2], np.array([0.5, -0.5]))
        np.testing.assert_allclose(short_values[:2], np.array([-0.5, 0.5]))
        self.assertTrue(np.isnan(long_values[2]))
        self.assertTrue(np.isnan(short_values[2]))

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
            self.assertIn("rank_score", row)
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

    def test_forward_return_masks_discontinuous_time_gaps(self) -> None:
        from research.runner import _forward_return

        close = np.array([100.0, 101.0, 102.0, 103.0, 104.0])
        open_times = np.array([0, 60_000, 120_000, 600_000, 660_000], dtype=np.int64)
        out = _forward_return(close, horizon=1, entry_lag=1, open_times=open_times, interval_ms=60_000)

        self.assertAlmostEqual(out[0], 102.0 / 101.0 - 1.0, places=8)
        self.assertTrue(np.isnan(out[1]))
        self.assertTrue(np.isnan(out[2]))
        self.assertTrue(np.isnan(out[3]))
        self.assertTrue(np.isnan(out[4]))

    def test_orientation_flips_short_factor_sign(self) -> None:
        from research.runner import _factor_orientation, _orient

        self.assertEqual(_factor_orientation(("long",)), 1)
        self.assertEqual(_factor_orientation(("short",)), -1)
        self.assertEqual(_factor_orientation(("long", "short")), 0)
        self.assertAlmostEqual(_orient(0.05, -1), -0.05)
        self.assertAlmostEqual(_orient(-0.05, -1), 0.05)
        self.assertAlmostEqual(_orient(-0.05, 0), -0.05)

    def test_short_factor_reports_positive_oriented_ir(self) -> None:
        from research.runner import _metric_row

        values = np.arange(10, dtype=np.float64)
        returns = -values
        row = _metric_row(
            factor="short_test",
            horizon=1,
            values=values,
            returns=returns,
            orientation=-1,
            is_period_ic=[-0.04, -0.02, -0.03, -0.01],
            oos_period_ic=[-0.03, -0.02, -0.04, -0.01],
            oos_mask=np.ones(10, dtype=bool),
        )

        self.assertLess(row["ic_ir"], 0.0)
        self.assertGreater(row["oriented_ic_ir"], 0.0)
        self.assertGreater(row["oos_oriented_ic_ir"], 0.0)

    def test_summary_ranking_uses_oos_directional_score(self) -> None:
        n = 120
        target = np.linspace(-1.0, 1.0, n)
        returns = target * 0.001
        closes = [100.0]
        for r in returns[:-1]:
            closes.append(closes[-1] * (1.0 + float(r)))

        klines = []
        for i in range(n):
            in_sample = i < n // 2
            bad_value = target[i] if in_sample else -target[i]
            good_value = 0.0 if in_sample else target[i]
            klines.append(_k(
                i,
                float(closes[i]),
                volume=float(bad_value + 10.0),
                buy=float(good_value + 10.0),
            ))

        result = analyze_factors(
            klines=klines,
            tick_map=None,
            factor_names=["_test_oos_bad_factor", "_test_oos_good_factor"],
            horizons=[1],
            quantiles=4,
            use_tick_features=False,
            entry_lag=0,
            min_period_samples=10,
            train_ratio=0.5,
        )

        self.assertEqual(result.summary[0]["factor"], "_test_oos_good_factor")
        self.assertGreater(result.summary[0]["rank_score"], result.summary[1]["rank_score"])

    def test_mixed_factor_is_not_directionally_rankable(self) -> None:
        from research.runner import _summary_row

        row = _summary_row(
            factor="mixed",
            requires_ticks=False,
            sides=FACTOR_SIDES,
            group=GROUP_MEAN_REVERSION,
            orientation=0,
            metrics=[{
                "horizon": 1,
                "rank_ic": -0.1,
                "oriented_rank_ic": -0.1,
                "ic_ir": -1.0,
                "ic_t_stat": -2.0,
                "oriented_ic_ir": -1.0,
                "oriented_ic_t_stat": -2.0,
                "sample_count": 100,
                "oos_rank_ic": -0.2,
                "oos_oriented_rank_ic": -0.2,
                "oos_ic_ir": -1.5,
                "oos_ic_t_stat": -3.0,
                "oos_oriented_ic_ir": -1.5,
                "oos_oriented_ic_t_stat": -3.0,
                "oos_sample_count": 50,
            }],
        )

        self.assertFalse(row["directional"])
        self.assertEqual(row["rank_score"], -1.0e9)

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
