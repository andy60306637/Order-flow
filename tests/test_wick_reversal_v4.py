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
        strat.short_delta_eff_threshold = 0.0
        return strat

    # ── 做空 k0 測試 helper ──────────────────────────────────────────────────
    # short k0: open=92, close=95, high=110, low=88
    #   body_high=95, mid=99  → body_high(95)<=mid(99) ✓
    #   upper_wick=110-95=15 > body=3 ✓
    #   bar_delta=2*50-100=0 >= upper_wick_absorption_bar_delta_min(0) ✓
    #   k0.high=110, k0.low=88
    #   stop = k0.high + sl_offset = 110 + 10 = 120

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
        """zoom 窗口內突破 k0 實體高點且 delta_eff 達標即進場，進場訊號基準價為 k0_body_high。"""
        strat = self._make_strat()
        # k0: open=100, close=108 → body_high=108, body_low=100, k0.low=90
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        entry_bar = _k(1, 108.5, 115.0, 108.2, 114.0, vol=120.0, tbv=90.0)  # high > body_high=108

        signals = strat.on_history([k0, entry_bar])
        entries = [s for s in signals if s.signal_type == "long_entry"]

        k0_body_high = max(k0.open, k0.close)  # 108
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].open_time, entry_bar.open_time)
        self.assertEqual(entries[0].price, k0_body_high)   # 進場基準 = 實體高點
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
        """k.high < k0_body_high 時不進場，k0 仍保持有效，下一根再試。"""
        strat = self._make_strat()
        # k0_body_high = max(100,108) = 108
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        no_break = _k(1, 107.0, 107.8, 106.5, 107.0, vol=120.0, tbv=90.0)  # high=107.8 < body_high=108
        entry_bar = _k(2, 108.0, 115.0, 107.5, 114.0, vol=120.0, tbv=90.0)  # high > body_high=108

        signals = strat.on_history([k0, no_break, entry_bar])
        entries = [s for s in signals if s.signal_type == "long_entry"]

        # k0 remains active through zoom; entry happens at bar 2
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].open_time, entry_bar.open_time)

    def test_bar_entry_expires_after_zoom(self):
        """超出 zoom_bars 後 k0 失效，不再進場。"""
        strat = self._make_strat()
        strat.zoom_bars = 2
        # k0_body_high = max(100,108) = 108
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        bars = [
            k0,
            _k(1, 107.0, 107.8, 106.5, 107.0, vol=100.0, tbv=60.0),  # high=107.8 < body_high, no break
            _k(2, 107.0, 107.9, 106.5, 107.5, vol=100.0, tbv=60.0),  # high=107.9 < body_high, last zoom
            _k(3, 108.0, 115.0, 107.5, 114.0, vol=120.0, tbv=90.0),  # beyond zoom, k0 already expired
        ]
        signals = strat.on_history(bars)
        entries = [s for s in signals if s.signal_type == "long_entry"]
        self.assertEqual(len(entries), 0)

    # ── Tick 模式進場 ────────────────────────────────────────────────────────

    def test_tick_entry_uses_first_tick_above_body_high_with_delta(self):
        """tick 模式：第一筆 > k0_body_high 且 cum_delta_eff > threshold 時，以 tick 價入場。
        停損為 k0.low（整根 K 棒最低點）- sl_offset，非 body_low。"""
        strat = self._make_strat()
        # k0: body_high=108, body_low=100, k0.low=90
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        entry_bar = _k(1, 107.5, 115.0, 107.0, 114.0, vol=120.0, tbv=80.0)

        tick_map = {
            entry_bar.open_time: _tick_arr([
                (entry_bar.open_time + 1, 107.5, 0.5, 0.0),  # < body_high=108, buyer taker
                (entry_bar.open_time + 2, 107.8, 0.4, 1.0),  # < body_high=108, seller taker
                (entry_bar.open_time + 3, 108.3, 0.6, 0.0),  # > body_high=108, buyer taker → trigger
                (entry_bar.open_time + 4, 109.0, 0.3, 1.0),
            ])
        }

        signals = strat.on_history([k0, entry_bar], tick_map=tick_map)
        entries = [s for s in signals if s.signal_type == "long_entry"]

        k0_body_high = max(k0.open, k0.close)  # 108
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].price, k0_body_high)      # 圖表基準價 = k0_body_high
        self.assertAlmostEqual(entries[0].fill_price, 108.3)  # 第一筆穿越 body_high 的 tick
        expected_stop = k0.low - strat.sl_offset               # 停損 = k0 整根最低點 - offset
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
        """出現新 k0 時覆蓋舊 k0，最終以新 k0_body_high 作為進場基準。"""
        strat = self._make_strat()
        strat.zoom_bars = 5
        k0_old = _k(0, 100.0, 110.0, 90.0, 108.0)
        k0_new = _k(1, 104.0, 114.0, 94.0, 112.0)  # 也符合 k0 形態，覆蓋舊 k0
        # k0_new: body_high = max(104,112) = 112
        entry_bar = _k(2, 112.5, 118.0, 111.5, 117.0, vol=150.0, tbv=120.0)  # high > 112

        signals = strat.on_history([k0_old, k0_new, entry_bar])
        entries = [s for s in signals if s.signal_type == "long_entry"]

        k0_new_body_high = max(k0_new.open, k0_new.close)  # 112
        self.assertEqual(len(entries), 1)
        # 應以 k0_new_body_high 為進場基準
        self.assertAlmostEqual(entries[0].price, k0_new_body_high)

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
        # entry_p=108 (body_high), stop=80, risk=28, rr=1.5 → target=150
        entry_bar = _k(1, 109.0, 115.0, 108.5, 114.0, vol=120.0, tbv=90.0)
        tp_bar = _k(2, 148.0, 152.0, 147.0, 151.0, vol=100.0, tbv=50.0)  # delta=0, TP

        signals = strat.on_history([k0, entry_bar, tp_bar])
        exits = [s for s in signals if s.signal_type == "long_exit"]

        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].label, "TP")


    # ════════════════════════════════════════════════════════════════
    # 做空測試（做多的對稱鏡像）
    # ════════════════════════════════════════════════════════════════

    # ── 做空 k0 偵測 ────────────────────────────────────────────────────────

    def test_k0_short_is_color_agnostic(self):
        """不看顏色，實體在下半部且有明顯上影線即可作為 short k0。"""
        strat = self._make_strat()
        # body_high=95<=mid=99, upper_wick=15>body=3, delta=0>=0
        k0 = _k(0, 92.0, 110.0, 88.0, 95.0)
        next_bar = _k(1, 93.0, 94.0, 89.0, 91.0, tbv=10.0)  # 不觸發進場

        signals = strat.on_history([k0, next_bar])
        k0s = [s for s in signals if s.signal_type == "k0_short"]

        self.assertEqual(len(k0s), 1)
        self.assertEqual(k0s[0].open_time, k0.open_time)

    def test_k0_short_requires_absorption_without_ticks(self):
        """無 tick 時，若整根 bar delta 偏賣方（< 0），不視為上影線吸收，不應為 short k0。"""
        strat = self._make_strat()
        # delta = 2*10-100 = -80 < upper_wick_absorption_bar_delta_min(0) → fail
        k0 = _k(0, 92.0, 110.0, 88.0, 95.0, vol=100.0, tbv=10.0)
        next_bar = _k(1, 93.0, 94.0, 89.0, 91.0)

        signals = strat.on_history([k0, next_bar])
        k0s = [s for s in signals if s.signal_type == "k0_short"]
        self.assertEqual(len(k0s), 0)

    # ── 做空 Bar 模式進場 ──────────────────────────────────────────────────

    def test_bar_entry_short_triggers_in_zoom_window(self):
        """zoom 窗口內跌破 k0_body_low 且 delta_eff 達標即做空，進場基準為 k0_body_low。"""
        strat = self._make_strat()
        # k0: body_low=92, body_high=95, k0.high=110
        k0 = _k(0, 92.0, 110.0, 88.0, 95.0)
        # entry: k.low=88<=body_low=92, delta_eff<0 (bearish)
        entry_bar = _k(1, 91.5, 92.3, 86.0, 88.0, vol=120.0, tbv=10.0)

        signals = strat.on_history([k0, entry_bar])
        entries = [s for s in signals if s.signal_type == "short_entry"]

        k0_body_low = min(k0.open, k0.close)  # 92
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].open_time, entry_bar.open_time)
        self.assertEqual(entries[0].price, k0_body_low)  # 進場基準 = 實體低點
        self.assertIsNone(entries[0].fill_price)

    def test_bar_entry_short_stop_is_k0_high_plus_offset(self):
        """做空停損為 k0.high（含上影線）+ sl_offset，非 k0_body_high。"""
        strat = self._make_strat()
        k0 = _k(0, 92.0, 110.0, 88.0, 95.0)
        entry_bar = _k(1, 91.5, 92.3, 86.0, 88.0, vol=120.0, tbv=10.0)

        signals = strat.on_history([k0, entry_bar])
        entries = [s for s in signals if s.signal_type == "short_entry"]

        self.assertEqual(len(entries), 1)
        expected_stop = k0.high + strat.sl_offset   # 110 + 10 = 120（含上影線）
        self.assertAlmostEqual(entries[0].stop_price, expected_stop)

    def test_bar_entry_short_fails_if_guardian_broken(self):
        """k.high > k0_body_high 表示守護線被破，k0 失效，不進場。"""
        strat = self._make_strat()
        k0 = _k(0, 92.0, 110.0, 88.0, 95.0)
        # k.high=96 > k0_body_high=95 → guardian broken
        broken_bar = _k(1, 93.0, 96.0, 86.0, 88.0, vol=120.0, tbv=10.0)

        signals = strat.on_history([k0, broken_bar])
        entries = [s for s in signals if s.signal_type == "short_entry"]
        self.assertEqual(len(entries), 0)

    def test_bar_entry_short_fails_if_no_breakdown(self):
        """k.low > k0_body_low 時不進場，k0 仍保持有效，下一根再試。"""
        strat = self._make_strat()
        # k0_body_low = 92
        k0 = _k(0, 92.0, 110.0, 88.0, 95.0)
        no_break = _k(1, 92.5, 94.0, 92.3, 93.0, vol=100.0, tbv=10.0)  # low=92.3 > 92
        entry_bar = _k(2, 91.5, 92.3, 86.0, 88.0, vol=120.0, tbv=10.0)

        signals = strat.on_history([k0, no_break, entry_bar])
        entries = [s for s in signals if s.signal_type == "short_entry"]

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].open_time, entry_bar.open_time)

    def test_bar_entry_short_expires_after_zoom(self):
        """超出 zoom_bars 後做空 k0 失效。"""
        strat = self._make_strat()
        strat.zoom_bars = 2
        k0 = _k(0, 92.0, 110.0, 88.0, 95.0)
        bars = [
            k0,
            _k(1, 92.5, 94.0, 92.3, 93.0, vol=100.0, tbv=10.0),  # no break, zoom 1
            _k(2, 92.5, 93.8, 92.2, 92.8, vol=100.0, tbv=10.0),  # no break, zoom 2 (last)
            _k(3, 91.5, 92.3, 86.0, 88.0, vol=120.0, tbv=10.0),  # beyond zoom
        ]
        signals = strat.on_history(bars)
        entries = [s for s in signals if s.signal_type == "short_entry"]
        self.assertEqual(len(entries), 0)

    # ── 做空 Tick 模式進場 ───────────────────────────────────────────────────

    def test_tick_entry_short_uses_first_tick_below_body_low(self):
        """tick 模式：第一筆 < k0_body_low 且 cum_delta_eff < -threshold 時，以 tick 價入場。
        停損為 k0.high（含上影線）+ sl_offset，非 k0_body_high。"""
        strat = self._make_strat()
        strat.short_delta_eff_threshold = 0.3
        # k0: body_low=92, body_high=95, k0.high=110
        k0 = _k(0, 92.0, 110.0, 88.0, 95.0)
        entry_bar = _k(1, 91.5, 94.0, 86.0, 88.0, vol=120.0, tbv=10.0)

        tick_map = {
            entry_bar.open_time: _tick_arr([
                (entry_bar.open_time + 1, 93.0, 0.5, 0.0),  # > body_low=92, buyer taker（不觸發）
                (entry_bar.open_time + 2, 92.5, 0.4, 1.0),  # > body_low=92, seller taker
                (entry_bar.open_time + 3, 91.5, 0.6, 1.0),  # < body_low=92, seller → check delta
                (entry_bar.open_time + 4, 90.0, 0.3, 1.0),
            ])
        }
        # After tick3: cum_buy=0.5, cum_vol=1.5, delta_eff=(1-1.5)/1.5=-0.33 < -0.3 → trigger

        signals = strat.on_history([k0, entry_bar], tick_map=tick_map)
        entries = [s for s in signals if s.signal_type == "short_entry"]

        k0_body_low = min(k0.open, k0.close)  # 92
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].price, k0_body_low)       # 圖表基準 = k0_body_low
        self.assertAlmostEqual(entries[0].fill_price, 91.5)   # 第一筆穿越 body_low 的 tick
        expected_stop = k0.high + strat.sl_offset              # 110 + 10 = 120
        self.assertAlmostEqual(entries[0].stop_price, expected_stop)

    def test_tick_entry_short_invalidates_if_guardian_broken_first(self):
        """tick 先突破 k0_body_high（守護線），即使後來再跌破 k0_body_low 也不進場。"""
        strat = self._make_strat()
        k0 = _k(0, 92.0, 110.0, 88.0, 95.0)
        entry_bar = _k(1, 93.0, 97.0, 86.0, 88.0)

        tick_map = {
            entry_bar.open_time: _tick_arr([
                (entry_bar.open_time + 1, 95.5, 0.5, 0.0),  # price > k0_body_high=95 → broken
                (entry_bar.open_time + 2, 91.0, 0.6, 1.0),  # would trigger but already failed
            ])
        }

        signals = strat.on_history([k0, entry_bar], tick_map=tick_map)
        entries = [s for s in signals if s.signal_type == "short_entry"]
        self.assertEqual(len(entries), 0)

    # ── 做空出場（smoke test）───────────────────────────────────────────────

    def test_sl_exit_short_bar_mode(self):
        """Bar 模式做空：k.high >= stop_price 觸發 SL。"""
        strat = self._make_strat()
        # k0: k0.high=110, stop=120, entry=92, risk=28, target=92-28*1.5=50
        k0 = _k(0, 92.0, 110.0, 88.0, 95.0)
        entry_bar = _k(1, 91.5, 92.3, 86.0, 88.0, vol=120.0, tbv=10.0)
        sl_bar = _k(2, 119.0, 121.0, 118.0, 120.5)  # high=121 >= stop=120 → SL

        signals = strat.on_history([k0, entry_bar, sl_bar])
        exits = [s for s in signals if s.signal_type == "short_exit"]

        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].label, "SL")
        self.assertAlmostEqual(exits[0].price, 120.0)  # stop_price = k0.high + offset = 120

    def test_tp_exit_short_bar_mode(self):
        """Bar 模式做空：k.low <= target_price 且 delta >= 0 → TP。"""
        strat = self._make_strat()
        # entry=92, stop=120, risk=28, rr=1.5 → target=92-42=50
        k0 = _k(0, 92.0, 110.0, 88.0, 95.0)
        entry_bar = _k(1, 91.5, 92.3, 86.0, 88.0, vol=120.0, tbv=10.0)
        tp_bar = _k(2, 51.0, 53.0, 48.0, 50.5, vol=100.0, tbv=50.0)  # delta=0, low=48<=50→TP

        signals = strat.on_history([k0, entry_bar, tp_bar])
        exits = [s for s in signals if s.signal_type == "short_exit"]

        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].label, "TP")


if __name__ == "__main__":
    unittest.main()
