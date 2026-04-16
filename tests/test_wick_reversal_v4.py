import unittest

import numpy as np

from core.tick_cache import build_bar_ranges, TickSliceAccessor
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
            _k(0, 100.0, 110.0, 90.0, 108.0, vol=300.0, tbv=270.0),
            _k(1, 108.0, 112.0, 107.5, 111.0, tbv=80.0),
        ]
        signals = strat.on_history(bars)
        k0s = [s for s in signals if s.signal_type == "k0_long"]
        self.assertEqual(len(k0s), 0)

    def test_k0_absorption_uses_wick_zone_ticks_when_available(self):
        """有 tick 時，k0 吸收應優先看下影線區域的 wick-zone order flow。"""
        strat = self._make_strat()
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0, vol=300.0, tbv=270.0)
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
        expected_stop = k0.low - strat.long_sl_offset
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
        strat.long_zoom_bars = 2
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
        strat.long_zoom_bars = 2
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
        expected_stop = k0.low - strat.long_sl_offset               # 停損 = k0 整根最低點 - offset
        self.assertAlmostEqual(entries[0].stop_price, expected_stop)

    def test_tick_slice_accessor_matches_dict_tick_map(self):
        """range accessor 與舊 dict tick_map 對策略結果應一致。"""
        strat_dict = self._make_strat()
        strat_range = self._make_strat()
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        entry_bar = _k(1, 107.5, 115.0, 107.0, 114.0, vol=120.0, tbv=80.0)
        ticks = _tick_arr([
            (entry_bar.open_time + 1, 107.5, 0.5, 0.0),
            (entry_bar.open_time + 2, 107.8, 0.4, 1.0),
            (entry_bar.open_time + 3, 108.3, 0.6, 0.0),
            (entry_bar.open_time + 4, 109.0, 0.3, 1.0),
        ])
        tick_map = {entry_bar.open_time: ticks}
        accessor = TickSliceAccessor(
            ticks,
            build_bar_ranges(ticks, [(entry_bar.open_time, entry_bar.close_time)]),
        )

        dict_signals = strat_dict.on_history([k0, entry_bar], tick_map=tick_map)
        range_signals = strat_range.on_history([k0, entry_bar], tick_map=accessor)

        def _sig_view(sig):
            return (
                sig.open_time,
                sig.price,
                sig.signal_type,
                sig.label,
                sig.stop_price,
                sig.fill_price,
            )

        self.assertEqual(
            [_sig_view(s) for s in dict_signals],
            [_sig_view(s) for s in range_signals],
        )

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
        strat.long_zoom_bars = 5
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
        # C-grade long: rr=2.0
        # entry_p=108 (body_high), stop=80, risk=28 → target=164
        entry_bar = _k(1, 109.0, 115.0, 108.5, 114.0, vol=120.0, tbv=90.0)
        tp_bar = _k(2, 162.0, 166.0, 161.0, 165.0, tbv=150.0)  # delta=0, TP

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
        strat.enable_short = True
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
        strat.enable_short = True
        # delta = 2*10-100 = -80 < upper_wick_absorption_bar_delta_min(0) → fail
        k0 = _k(0, 92.0, 110.0, 88.0, 95.0, vol=300.0, tbv=30.0)
        next_bar = _k(1, 93.0, 94.0, 89.0, 91.0)

        signals = strat.on_history([k0, next_bar])
        k0s = [s for s in signals if s.signal_type == "k0_short"]
        self.assertEqual(len(k0s), 0)

    # ── 做空 Bar 模式進場 ──────────────────────────────────────────────────

    def test_bar_entry_short_triggers_in_zoom_window(self):
        """zoom 窗口內跌破 k0_body_low 且 delta_eff 達標即做空，進場基準為 k0_body_low。"""
        strat = self._make_strat()
        strat.enable_short = True
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
        strat.enable_short = True
        k0 = _k(0, 92.0, 110.0, 88.0, 95.0)
        entry_bar = _k(1, 91.5, 92.3, 86.0, 88.0, vol=120.0, tbv=10.0)

        signals = strat.on_history([k0, entry_bar])
        entries = [s for s in signals if s.signal_type == "short_entry"]

        self.assertEqual(len(entries), 1)
        expected_stop = k0.high + strat.short_sl_offset   # 110 + 10 = 120（含上影線）
        self.assertAlmostEqual(entries[0].stop_price, expected_stop)

    def test_bar_entry_short_fails_if_guardian_broken(self):
        """k.high > k0_body_high 表示守護線被破，k0 失效，不進場。"""
        strat = self._make_strat()
        strat.enable_short = True
        k0 = _k(0, 92.0, 110.0, 88.0, 95.0)
        # k.high=96 > k0_body_high=95 → guardian broken
        broken_bar = _k(1, 93.0, 96.0, 86.0, 88.0, vol=120.0, tbv=10.0)

        signals = strat.on_history([k0, broken_bar])
        entries = [s for s in signals if s.signal_type == "short_entry"]
        self.assertEqual(len(entries), 0)

    def test_bar_entry_short_fails_if_no_breakdown(self):
        """k.low > k0_body_low 時不進場，k0 仍保持有效，下一根再試。"""
        strat = self._make_strat()
        strat.enable_short = True
        strat.short_zoom_bars = 2
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
        strat.enable_short = True
        strat.short_zoom_bars = 2
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
        strat.enable_short = True
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
        expected_stop = k0.high + strat.short_sl_offset              # 110 + 10 = 120
        self.assertAlmostEqual(entries[0].stop_price, expected_stop)

    def test_tick_entry_short_invalidates_if_guardian_broken_first(self):
        """tick 先突破 k0_body_high（守護線），即使後來再跌破 k0_body_low 也不進場。"""
        strat = self._make_strat()
        strat.enable_short = True
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
        strat.enable_short = True
        # k0: k0.high=110, stop=120, entry=92, risk=28, target=92-28*1.0=64
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
        strat.enable_short = True
        # C-grade short: upper_wick=11, body=4, ratio=2.75 → rr=2.0
        # entry=95, stop=120, risk=25 → target=45
        k0 = _k(0, 99.0, 110.0, 88.0, 95.0)
        entry_bar = _k(1, 94.5, 95.3, 86.0, 88.0, vol=120.0, tbv=10.0)
        tp_bar = _k(2, 46.0, 48.0, 44.0, 45.5, tbv=150.0)  # delta=0, low=44<=45→TP

        signals = strat.on_history([k0, entry_bar, tp_bar])
        exits = [s for s in signals if s.signal_type == "short_exit"]

        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].label, "TP")


    # ════════════════════════════════════════════════════════════════
    # Fee Filter + Dynamic RR 測試
    # ════════════════════════════════════════════════════════════════

    # ── _round_trip_cost ─────────────────────────────────────────────────────

    def test_round_trip_cost_btc_84000(self):
        """BTC 84000 的 round trip cost 為 57.12。"""
        strat = self._make_strat()
        cost = strat._round_trip_cost(84000.0)
        self.assertAlmostEqual(cost, 57.12, places=4)

    def test_round_trip_cost_formula(self):
        """公式：2 * (taker_fee + slippage) * price。"""
        strat = self._make_strat()
        # 0.00032 + 0.00002 = 0.00034, * 2 = 0.00068
        cost = strat._round_trip_cost(100.0)
        self.assertAlmostEqual(cost, 0.068, places=6)

    # ── _classify_long_k0_wick ───────────────────────────────────────────────

    def test_classify_long_wick_type_a(self):
        """lower_wick/body >= 4.0 → A。"""
        strat = self._make_strat()
        # body=2, lower_wick=8, ratio=4.0 → A
        k0 = _k(0, 108.0, 110.0, 100.0, 110.0, vol=100.0, tbv=50.0)
        # body=|110-108|=2, lower_wick=min(108,110)-100=108-100=8
        self.assertEqual(strat._classify_long_k0_wick(k0), "A")

    def test_classify_long_wick_type_b(self):
        """3.0 <= lower_wick/body < 4.0 → B。"""
        strat = self._make_strat()
        # body=3, lower_wick=10, ratio=3.33 → B
        k0 = _k(0, 107.0, 110.0, 97.0, 110.0, vol=100.0, tbv=50.0)
        # body=|110-107|=3, lower_wick=min(107,110)-97=107-97=10, ratio=10/3≈3.33
        self.assertEqual(strat._classify_long_k0_wick(k0), "B")

    def test_classify_long_wick_type_c(self):
        """lower_wick/body < 3.0 → C。"""
        strat = self._make_strat()
        # body=8, lower_wick=10, ratio=1.25 → C
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0, vol=100.0, tbv=50.0)
        self.assertEqual(strat._classify_long_k0_wick(k0), "C")

    def test_classify_long_wick_doji_no_div_zero(self):
        """body=0 (doji) 時不會除以 0，使用 body_floor。"""
        strat = self._make_strat()
        # open=close=100 → body=0, lower_wick=100-90=10
        k0 = _k(0, 100.0, 110.0, 90.0, 100.0, vol=100.0, tbv=50.0)
        wtype = strat._classify_long_k0_wick(k0)
        self.assertIn(wtype, ["A", "B", "C"])  # 不出錯即可

    # ── _classify_short_k0_wick ──────────────────────────────────────────────

    def test_classify_short_wick_type_a(self):
        """upper_wick/body >= 4.0 → A。"""
        strat = self._make_strat()
        # body=|95-92|=3, upper_wick=110-95=15, ratio=5.0 → A
        k0 = _k(0, 92.0, 110.0, 88.0, 95.0, vol=100.0, tbv=50.0)
        self.assertEqual(strat._classify_short_k0_wick(k0), "A")

    def test_classify_short_wick_type_b(self):
        """3.0 <= upper_wick/body < 4.0 → B。"""
        strat = self._make_strat()
        # body=|95-92|=3, upper_wick=105-95=10, ratio=3.33 → B
        k0 = _k(0, 92.0, 105.0, 88.0, 95.0, vol=100.0, tbv=50.0)
        self.assertEqual(strat._classify_short_k0_wick(k0), "B")

    def test_classify_short_wick_type_c(self):
        """upper_wick/body < 3.0 → C。"""
        strat = self._make_strat()
        # body=|95-92|=3, upper_wick=100-95=5, ratio=1.67 → C
        k0 = _k(0, 92.0, 100.0, 88.0, 95.0, vol=100.0, tbv=50.0)
        self.assertEqual(strat._classify_short_k0_wick(k0), "C")

    def test_classify_short_wick_doji_no_div_zero(self):
        """body=0 (doji) 時不會除以 0。"""
        strat = self._make_strat()
        k0 = _k(0, 95.0, 110.0, 88.0, 95.0, vol=100.0, tbv=50.0)
        wtype = strat._classify_short_k0_wick(k0)
        self.assertIn(wtype, ["A", "B", "C"])

    # ── _resolve_long_rr / _resolve_short_rr ─────────────────────────────────

    def test_resolve_long_rr_returns_correct_values(self):
        """A/B/C 各自回傳對應 RR。"""
        strat = self._make_strat()
        # A-grade: body=2, lower_wick=8
        k0_a = _k(0, 108.0, 110.0, 100.0, 110.0)
        self.assertAlmostEqual(strat._resolve_long_rr(k0_a), 4.0)
        # B-grade: body=3, lower_wick=10
        k0_b = _k(0, 107.0, 110.0, 97.0, 110.0)
        self.assertAlmostEqual(strat._resolve_long_rr(k0_b), 2.5)
        # C-grade: body=8, lower_wick=10
        k0_c = _k(0, 100.0, 110.0, 90.0, 108.0)
        self.assertAlmostEqual(strat._resolve_long_rr(k0_c), 2.0)

    def test_resolve_short_rr_returns_correct_values(self):
        strat = self._make_strat()
        # A-grade: upper_wick=15, body=3
        k0_a = _k(0, 92.0, 110.0, 88.0, 95.0)
        self.assertAlmostEqual(strat._resolve_short_rr(k0_a), 4.5)
        # C-grade: upper_wick=5, body=3
        k0_c = _k(0, 92.0, 100.0, 88.0, 95.0)
        self.assertAlmostEqual(strat._resolve_short_rr(k0_c), 2.0)

    # ── _risk_covers_cost ────────────────────────────────────────────────────

    def test_risk_covers_cost_at_boundary(self):
        """BTC 84000, RR=2, fee_cover=1.2 → min_risk=34.272。"""
        strat = self._make_strat()
        # round_trip=57.12, min_risk=57.12*1.2/2=34.272
        self.assertTrue(strat._risk_covers_cost(84000.0, 34.272, 2.0, 1.2))
        self.assertFalse(strat._risk_covers_cost(84000.0, 34.0, 2.0, 1.2))

    def test_risk_covers_cost_rejects_zero_risk(self):
        strat = self._make_strat()
        self.assertFalse(strat._risk_covers_cost(84000.0, 0.0, 2.0, 1.2))

    def test_risk_covers_cost_rejects_zero_rr(self):
        strat = self._make_strat()
        self.assertFalse(strat._risk_covers_cost(84000.0, 100.0, 0.0, 1.2))

    # ── Long bar entry cost gate ─────────────────────────────────────────────

    def test_long_bar_entry_rejected_by_cost_gate(self):
        """risk 太小被 cost gate 擋住。"""
        strat = self._make_strat()
        # 做一個 risk 很小的 k0（entry_p 接近 stop_p）
        # k0: open=100, close=100.5, high=101, low=99.99
        # body_high=100.5, stop=99.99-10=89.99, risk=100.5-89.99=10.51
        # round_trip=100.5*0.00068=0.0683, min_risk=0.0683*1.2/1.0=0.082 → 10.51 passes
        # 要讓它 fail，需要很大 entry_price 但很小 risk
        strat.long_sl_offset = 0.0
        # k0: entry_p=100.5, stop=100.49, risk=0.01
        k0 = _k(0, 100.0, 101.0, 100.49, 100.5, vol=100.0, tbv=20.0)
        entry_bar = _k(1, 100.6, 101.5, 100.4, 101.0, vol=120.0, tbv=90.0)
        signals = strat.on_history([k0, entry_bar])
        entries = [s for s in signals if s.signal_type == "long_entry"]
        self.assertEqual(len(entries), 0)

    # ── Long tick entry cost gate ────────────────────────────────────────────

    def test_long_tick_entry_rejected_by_cost_gate(self):
        """tick 模式 risk 太小被 cost gate 擋住。"""
        strat = self._make_strat()
        strat.long_sl_offset = 0.0
        k0 = _k(0, 100.0, 101.0, 100.49, 100.5, vol=100.0, tbv=20.0)
        entry_bar = _k(1, 100.6, 101.5, 100.4, 101.0, vol=120.0, tbv=90.0)
        tick_map = {
            entry_bar.open_time: _tick_arr([
                (entry_bar.open_time + 1, 100.6, 0.5, 0.0),
            ])
        }
        signals = strat.on_history([k0, entry_bar], tick_map=tick_map)
        entries = [s for s in signals if s.signal_type == "long_entry"]
        self.assertEqual(len(entries), 0)

    # ── Label 測試 ───────────────────────────────────────────────────────────

    def test_long_entry_label_contains_wick_type(self):
        """進場 label 包含 wick 分級：L4A/L4B/L4C。"""
        strat = self._make_strat()
        # C-grade k0: body=8, lower_wick=10, ratio=1.25
        k0 = _k(0, 100.0, 110.0, 90.0, 108.0)
        entry_bar = _k(1, 109.0, 115.0, 108.5, 114.0, vol=120.0, tbv=90.0)
        signals = strat.on_history([k0, entry_bar])
        entries = [s for s in signals if s.signal_type == "long_entry"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].label, "L4C")

    def test_short_entry_label_contains_wick_type(self):
        """做空進場 label 包含 wick 分級：S4A/S4B/S4C。"""
        strat = self._make_strat()
        strat.enable_short = True
        # A-grade k0: upper_wick=15, body=3, ratio=5.0
        k0 = _k(0, 92.0, 110.0, 88.0, 95.0)
        entry_bar = _k(1, 91.5, 92.3, 86.0, 88.0, vol=120.0, tbv=10.0)
        signals = strat.on_history([k0, entry_bar])
        entries = [s for s in signals if s.signal_type == "short_entry"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].label, "S4A")

    # ── Dynamic RR target 驗證 ───────────────────────────────────────────────

    def test_long_bar_a_grade_uses_higher_rr(self):
        """A-grade wick 使用新版較高 RR，target 更高。"""
        strat = self._make_strat()
        # A-grade: body=2, lower_wick=8, ratio=4.0
        # open=108, close=110, low=100, high=111
        # entry=110 (body_high), stop=100-10=90, risk=20, rr=4.0 → target=190
        k0 = _k(0, 108.0, 111.0, 100.0, 110.0)
        entry_bar = _k(1, 110.5, 116.0, 110.0, 115.0, vol=120.0, tbv=90.0)
        # 需要 TP bar 在 target=190
        tp_bar = _k(2, 188.0, 192.0, 187.0, 191.0, tbv=150.0)
        signals = strat.on_history([k0, entry_bar, tp_bar])
        exits = [s for s in signals if s.signal_type == "long_exit"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].label, "TP")

    # ── Short bar entry cost gate ────────────────────────────────────────────

    def test_short_bar_entry_rejected_by_cost_gate(self):
        """做空 risk 太小被 cost gate 擋住。"""
        strat = self._make_strat()
        strat.enable_short = True
        strat.short_sl_offset = 0.0
        # k0: body_low=99.5, body_high=100.0, k0.high=100.01
        # entry=99.5, stop=100.01, risk=0.51 → 太小被擋
        k0 = _k(0, 99.5, 100.01, 88.0, 100.0, vol=100.0, tbv=50.0)
        entry_bar = _k(1, 99.0, 99.8, 98.0, 98.5, vol=120.0, tbv=10.0)
        signals = strat.on_history([k0, entry_bar])
        entries = [s for s in signals if s.signal_type == "short_entry"]
        self.assertEqual(len(entries), 0)


if __name__ == "__main__":
    unittest.main()
