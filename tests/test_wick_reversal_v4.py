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
    vol: float = 100.0,
    tbv: float = 50.0,
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


def _tick_arr(rows: list[tuple[int, float, float, float]]) -> np.ndarray:
    return np.array(rows, dtype=np.float64)


class TestWickReversalV4(unittest.TestCase):
    def _make_strat(self) -> WickReversalV4Strategy:
        strat = WickReversalV4Strategy()
        strat.long_vol_sma_period = 0
        strat.long_delta_eff_threshold = 0.0
        strat.k0_range_sma_period = 0
        return strat

    # ── k0 偵測 ──────────────────────────────────────────────────────────────

    def test_k0_long_is_color_agnostic(self):
        """綠 K 只要實體在上半部且有明顯下影線，也可作為 long k0。"""
        strat = self._make_strat()
        bars = [
            _k(0, 100.0, 110.0, 90.0, 108.0),   # 綠 K，仍符合 k0 long 形態
            _k(1, 108.0, 112.0, 107.5, 111.0, tbv=80.0),
        ]
        signals = strat.on_history(bars)
        k0s = [s for s in signals if s.signal_type == "k0_long"]
        self.assertEqual(len(k0s), 1)
        self.assertEqual(k0s[0].open_time, bars[0].open_time)

    def test_k0_requires_absorption_without_ticks(self):
        """無 tick 時，若整根 bar 為明顯主動買盤，不應視為 lower-wick absorption。"""
        strat = self._make_strat()
        bars = [
            _k(0, 100.0, 110.0, 90.0, 108.0, vol=100.0, tbv=90.0),
            _k(1, 108.0, 112.0, 107.5, 111.0, tbv=80.0),
        ]
        signals = strat.on_history(bars)
        k0s = [s for s in signals if s.signal_type == "k0_long"]
        self.assertEqual(len(k0s), 0)

    def test_k0_absorption_uses_wick_zone_ticks_when_available(self):
        """有 tick 時，k0 吸收應優先看下影線區域的 wick-zone order flow。"""
        strat = self._make_strat()
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0, vol=100.0, tbv=90.0)
        next_bar = _k(1, 108.0, 112.0, 107.5, 111.0, tbv=80.0)
        tick_map = {
            k0.open_time: _tick_arr([
                (k0.open_time + 1, 92.0, 20.0, 1.0),   # wick zone，buyer_maker（賣方主動）
                (k0.open_time + 2, 95.0, 10.0, 1.0),   # wick zone，buyer_maker
                (k0.open_time + 3, 101.0, 20.0, 0.0),
                (k0.open_time + 4, 107.0, 20.0, 0.0),
                (k0.open_time + 5, 108.0, 30.0, 0.0),
            ])
        }
        signals = strat.on_history([k0, next_bar], tick_map=tick_map)
        k0s = [s for s in signals if s.signal_type == "k0_long"]
        self.assertEqual(len(k0s), 1)

    # ── Bar 模式進場 ─────────────────────────────────────────────────────────

    def test_bar_entry_triggers_in_zoom_window(self):
        """zoom 窗口內突破 k0.high 且 delta_eff 達標即進場，進場價為 k0.high。"""
        strat = self._make_strat()
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        entry_bar = _k(1, 109.0, 115.0, 108.5, 114.0, vol=120.0, tbv=90.0)  # bullish delta

        signals = strat.on_history([k0, entry_bar])
        entries = [s for s in signals if s.signal_type == "long_entry"]

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].open_time, entry_bar.open_time)
        self.assertEqual(entries[0].price, k0.high)
        self.assertIsNone(entries[0].fill_price)

    def test_bar_entry_stop_is_k0_low_minus_offset(self):
        """Bar 模式停損為 k0.low - sl_offset。"""
        strat = self._make_strat()
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        entry_bar = _k(1, 109.0, 115.0, 108.5, 114.0, vol=120.0, tbv=90.0)

        signals = strat.on_history([k0, entry_bar])
        entries = [s for s in signals if s.signal_type == "long_entry"]

        self.assertEqual(len(entries), 1)
        expected_stop = k0.low - strat.sl_offset
        self.assertAlmostEqual(entries[0].stop_price, expected_stop)

    def test_bar_entry_fails_if_structure_broken(self):
        """k.low < k0.low 表示守護線被破，k0 失效，不進場。"""
        strat = self._make_strat()
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        broken_bar = _k(1, 109.0, 115.0, 89.0, 114.0, vol=120.0, tbv=90.0)

        signals = strat.on_history([k0, broken_bar])
        entries = [s for s in signals if s.signal_type == "long_entry"]
        self.assertEqual(len(entries), 0)

    def test_bar_entry_fails_if_no_breakout(self):
        """k.high < k0.high 時不進場，k0 仍保持有效。"""
        strat = self._make_strat()
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        no_break = _k(1, 109.0, 109.5, 108.5, 109.0, vol=120.0, tbv=90.0)  # high < k0.high
        entry_bar = _k(2, 109.0, 115.0, 108.5, 114.0, vol=120.0, tbv=90.0)

        signals = strat.on_history([k0, no_break, entry_bar])
        entries = [s for s in signals if s.signal_type == "long_entry"]

        # k0 remains active through zoom; entry happens at bar 2
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].open_time, entry_bar.open_time)

    def test_bar_entry_expires_after_zoom(self):
        """超出 zoom_bars 後 k0 失效，不再進場。"""
        strat = self._make_strat()
        strat.zoom_bars = 2
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        bars = [
            k0,
            _k(1, 109.0, 109.5, 108.5, 109.0, vol=100.0, tbv=60.0),  # no break
            _k(2, 109.0, 109.8, 108.5, 109.5, vol=100.0, tbv=60.0),  # no break, last zoom
            _k(3, 109.0, 115.0, 108.5, 114.0, vol=120.0, tbv=90.0),  # beyond zoom
        ]
        signals = strat.on_history(bars)
        entries = [s for s in signals if s.signal_type == "long_entry"]
        self.assertEqual(len(entries), 0)

    # ── Tick 模式進場 ────────────────────────────────────────────────────────

    def test_tick_entry_uses_first_tick_at_k0_high_with_delta(self):
        """tick 模式：第一筆 >= k0.high 且 cum_delta_eff > threshold 時，以 tick 價入場。"""
        strat = self._make_strat()
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        entry_bar = _k(1, 109.0, 115.0, 108.5, 114.0, vol=120.0, tbv=80.0)

        tick_map = {
            entry_bar.open_time: _tick_arr([
                (entry_bar.open_time + 1, 109.2, 0.5, 1.0),  # below k0.high, buyer_maker
                (entry_bar.open_time + 2, 109.8, 0.4, 0.0),  # below k0.high, buyer taker
                (entry_bar.open_time + 3, 110.3, 0.6, 0.0),  # >= k0.high=110, buyer taker → trigger
                (entry_bar.open_time + 4, 111.0, 0.3, 1.0),
            ])
        }

        signals = strat.on_history([k0, entry_bar], tick_map=tick_map)
        entries = [s for s in signals if s.signal_type == "long_entry"]

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].price, k0.high)          # 圖表基準價 = k0.high
        self.assertAlmostEqual(entries[0].fill_price, 110.3) # 實際成交 tick
        expected_stop = k0.low - strat.sl_offset
        self.assertAlmostEqual(entries[0].stop_price, expected_stop)

    def test_tick_entry_invalidates_if_structure_broken_first(self):
        """tick 先打破 k0.low，即使後來再突破 k0.high 也不進場。"""
        strat = self._make_strat()
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        entry_bar = _k(1, 109.0, 115.0, 89.0, 114.0)

        tick_map = {
            entry_bar.open_time: _tick_arr([
                (entry_bar.open_time + 1, 89.5, 0.5, 1.0),  # price < k0.low → broken
                (entry_bar.open_time + 2, 110.5, 0.6, 0.0),
            ])
        }

        signals = strat.on_history([k0, entry_bar], tick_map=tick_map)
        entries = [s for s in signals if s.signal_type == "long_entry"]
        self.assertEqual(len(entries), 0)

    def test_tick_entry_waits_for_delta_threshold(self):
        """tick 觸及 k0.high 但累計 delta_eff 不足時，繼續等待；後續 tick delta 達標才進場。"""
        strat = self._make_strat()
        strat.long_delta_eff_threshold = 0.5   # 需要 delta_eff > 0.5
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        entry_bar = _k(1, 109.0, 115.0, 108.5, 114.0, vol=100.0, tbv=70.0)

        tick_map = {
            entry_bar.open_time: _tick_arr([
                # 累計到此：buy=0.3, vol=0.8 → delta_eff = (0.6-0.8)/0.8 = -0.25，不足
                (entry_bar.open_time + 1, 110.1, 0.8, 1.0),  # price >= k0.high, all seller taker
                # 累計：buy=0.3+0.7=1.0, vol=0.8+1.0=1.8 → delta_eff = (2-1.8)/1.8 ≈ +0.11，不足
                (entry_bar.open_time + 2, 110.2, 1.0, 0.0),
                # 累計：buy=1.0+1.5=2.5, vol=1.8+1.5=3.3 → delta_eff = (5-3.3)/3.3 ≈ +0.52，達標
                (entry_bar.open_time + 3, 110.5, 1.5, 0.0),
            ])
        }

        signals = strat.on_history([k0, entry_bar], tick_map=tick_map)
        entries = [s for s in signals if s.signal_type == "long_entry"]

        self.assertEqual(len(entries), 1)
        self.assertAlmostEqual(entries[0].fill_price, 110.5)  # 第三筆 tick 才達標

    # ── k0 新 k0 覆蓋 ────────────────────────────────────────────────────────

    def test_new_k0_overrides_old_k0(self):
        """出現新 k0 時覆蓋舊 k0，最終以新 k0.high 作為進場基準。"""
        strat = self._make_strat()
        strat.zoom_bars = 5
        k0_old = _k(0, 100.0, 110.0, 90.0, 108.0)
        k0_new = _k(1, 104.0, 114.0, 94.0, 112.0)  # 也符合 k0 形態，覆蓋舊 k0
        entry_bar = _k(2, 112.0, 118.0, 111.5, 117.0, vol=150.0, tbv=120.0)

        signals = strat.on_history([k0_old, k0_new, entry_bar])
        entries = [s for s in signals if s.signal_type == "long_entry"]

        self.assertEqual(len(entries), 1)
        # 應以 k0_new.high 為進場基準
        self.assertAlmostEqual(entries[0].price, k0_new.high)

    # ── 出場邏輯（smoke test）────────────────────────────────────────────────

    def test_sl_exit_bar_mode(self):
        """Bar 模式：k.low <= stop_price 觸發 SL。"""
        strat = self._make_strat()
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        entry_bar = _k(1, 109.0, 115.0, 108.5, 114.0, vol=120.0, tbv=90.0)
        # stop = k0.low - sl_offset = 90 - 10 = 80
        sl_bar = _k(2, 81.0, 82.0, 79.0, 80.5)  # low <= 80 → SL

        signals = strat.on_history([k0, entry_bar, sl_bar])
        exits = [s for s in signals if s.signal_type == "long_exit"]

        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].label, "SL")

    def test_tp_exit_bar_mode(self):
        """Bar 模式：k.high >= target_price 且 delta <= 0 → TP。"""
        strat = self._make_strat()
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        # entry=110, stop=80, risk=30, target=140
        entry_bar = _k(1, 109.0, 115.0, 108.5, 114.0, vol=120.0, tbv=90.0)
        tp_bar = _k(2, 138.0, 145.0, 137.0, 142.0, vol=100.0, tbv=50.0)  # delta=0, TP

        signals = strat.on_history([k0, entry_bar, tp_bar])
        exits = [s for s in signals if s.signal_type == "long_exit"]

        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].label, "TP")


if __name__ == "__main__":
    unittest.main()
