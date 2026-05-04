import math
import unittest

from core.data_types import Kline
from core.micro_volatility import MicroVolatilityEngine
from strategies.pipeline import MicroVolatilityComponent


_MS_1M = 60_000


def _k(i: int, close: float = 50_000.0, volume: float = 100.0, tbv: float = 50.0) -> Kline:
    ot = i * _MS_1M
    return Kline(
        symbol="BTCUSDT",
        interval="1m",
        open_time=ot,
        close_time=ot + _MS_1M - 1,
        open=close,
        high=close + 10.0,
        low=close - 10.0,
        close=close,
        volume=volume,
        taker_buy_volume=tbv,
        is_closed=True,
    )


def _book(
    bid: float = 50_000.0,
    ask: float = 50_000.5,
    bid_depth: float = 100.0,
    ask_depth: float = 100.0,
) -> dict:
    return {
        "best_bid_price": bid,
        "best_ask_price": ask,
        "bids_volume_top_N": bid_depth,
        "asks_volume_top_N": ask_depth,
    }


def _trade(buy: float = 10.0, sell: float = 10.0) -> dict:
    return {"taker_buy_volume": buy, "taker_sell_volume": sell}


class TestMicroVolatilityEngine(unittest.TestCase):
    def test_flat_market_is_finite_and_neutral(self):
        engine = MicroVolatilityEngine(window_size=3, normalization_window=5)

        for _ in range(10):
            mfi = engine.update(_book(), _trade())

        self.assertTrue(math.isfinite(mfi))
        self.assertAlmostEqual(mfi, 0.0, places=8)
        self.assertAlmostEqual(engine.last_reading.spread_variance, 0.0, places=8)
        self.assertAlmostEqual(engine.last_reading.ofi_variance, 0.0, places=8)

    def test_liquidity_shock_increases_fragility(self):
        engine = MicroVolatilityEngine(window_size=3, normalization_window=5)

        before = 0.0
        for i in range(8):
            before = engine.update(
                _book(bid=50_000.0 + i * 0.1, ask=50_000.5 + i * 0.1),
                _trade(10.0, 10.0),
            )

        shock = engine.update(
            _book(bid=49_998.0, ask=50_003.0, bid_depth=20.0, ask_depth=15.0),
            _trade(90.0, 5.0),
        )

        self.assertGreater(shock, before)
        self.assertGreater(engine.last_reading.depth_depletion, 0.0)
        self.assertGreater(engine.last_reading.spread_variance, 0.0)
        self.assertGreater(engine.last_reading.ofi_variance, 0.0)

    def test_accepts_level_sequences(self):
        engine = MicroVolatilityEngine(window_size=2, normalization_window=3, top_n=2)
        mfi = engine.update(
            {
                "best_bid": 100.0,
                "best_ask": 100.5,
                "bids": [[100.0, 2.0], [99.5, 3.0], [99.0, 100.0]],
                "asks": [[100.5, 4.0], [101.0, 5.0], [101.5, 100.0]],
            },
            {"volume": 10.0, "taker_buy_volume": 6.0},
        )

        self.assertTrue(math.isfinite(mfi))
        self.assertEqual(engine.last_reading.total_depth, 14.0)


class TestMicroVolatilityComponent(unittest.TestCase):
    def test_component_uses_snapshot_map(self):
        klines = [_k(i) for i in range(8)]
        snapshot_map = {}
        for i, k in enumerate(klines):
            snapshot_map[k.open_time] = {
                "orderbook": _book(
                    bid=50_000.0 + i,
                    ask=50_000.5 + i,
                    bid_depth=100.0,
                    ask_depth=100.0,
                ),
                "trade": _trade(10.0 + i, 10.0),
            }

        comp = MicroVolatilityComponent(
            window_size=3,
            normalization_window=5,
            snapshot_map=snapshot_map,
        )
        result = comp.compute(klines, len(klines) - 1)

        self.assertEqual(comp.component_id, "micro_volatility_15m_l10")
        self.assertEqual(result["source"], "snapshot")
        self.assertEqual(result["updates"], len(klines))
        self.assertIn("micro_fragility_index", result)
        self.assertTrue(math.isfinite(result["micro_fragility_index"]))

    def test_component_can_disable_kline_fallback(self):
        klines = [_k(i) for i in range(3)]
        comp = MicroVolatilityComponent(use_kline_fallback=False)
        result = comp.compute(klines, len(klines) - 1)

        self.assertEqual(result["source"], "missing_snapshot")
        self.assertEqual(result["updates"], 0)
        self.assertEqual(result["micro_fragility_index"], 0.0)


if __name__ == "__main__":
    unittest.main()
