import unittest

import numpy as np

from core.data_types import Kline
from strategies.wick_reversal_v4 import WickReversalV4Strategy


_MS_1M = 60_000


def _k(
    i: int,
    o: float,
    h: float,
    l: float,
    c: float,
    vol: float = 300.0,
    tbv: float = 150.0,
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


class TestWickReversalV4StrictTickMode(unittest.TestCase):
    def test_missing_entry_ticks_do_not_fallback_to_bar_entry(self):
        strat = WickReversalV4Strategy()
        strat.allow_bar_fallback_in_tick_mode = False
        strat.long_vol_sma_period = 0
        strat.long_delta_eff_threshold = 0.0

        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        entry_bar = _k(1, 109.0, 115.0, 108.5, 114.0, vol=120.0, tbv=90.0)

        signals = strat.on_history(
            [k0, entry_bar],
            tick_map={k0.open_time: np.empty((0, 4), dtype=np.float64)},
        )

        entries = [sig for sig in signals if sig.signal_type == "long_entry"]
        self.assertEqual(entries, [])
        self.assertEqual(strat._fallback_bar_count, 0)

    def test_missing_exit_ticks_do_not_fallback_to_bar_exit(self):
        strat = WickReversalV4Strategy()
        strat.allow_bar_fallback_in_tick_mode = False
        strat.long_vol_sma_period = 0
        strat.long_delta_eff_threshold = 0.0

        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        entry_bar = _k(1, 109.0, 115.0, 108.5, 114.0, vol=120.0, tbv=90.0)
        exit_bar = _k(2, 160.0, 166.0, 159.0, 165.0, vol=120.0, tbv=30.0)
        tick_map = {
            entry_bar.open_time: _ticks(
                entry_bar.open_time,
                [
                    (1, 108.3, 0.6, 0.0),
                    (2, 109.0, 0.3, 1.0),
                ],
            ),
        }

        signals = strat.on_history([k0, entry_bar, exit_bar], tick_map=tick_map)

        exits = [sig for sig in signals if sig.signal_type == "long_exit"]
        self.assertEqual(exits, [])
        self.assertEqual(strat._fallback_bar_count, 0)


if __name__ == "__main__":
    unittest.main()
