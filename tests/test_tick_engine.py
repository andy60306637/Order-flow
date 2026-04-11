"""
Tick 級別引擎單元測試。

涵蓋：
  1. tick_cache.build_bar_map 分桶正確性
  2. _tick_entry：做多/做空 tick 進場邏輯
  3. _tick_exit：SL / TP / TS / TD tick 出場邏輯
  4. _bar_exit_simple fallback 計數
  5. Tick 模式 vs Bar 模式差異
  6. 邊界情境（空 tick_map、partial coverage、防守線破壞）
  7. fill_price 語義正確性
  8. Vol SMA look-ahead 防護
"""
import unittest

import numpy as np

from core.data_types import Kline
from strategies.wick_reversal import WickReversalStrategy
from strategies.base import StrategySignal
from core.tick_cache import build_bar_map


# ═══════════════════════════════════════════════════════════════════════════
# 輔助建構函式
# ═══════════════════════════════════════════════════════════════════════════

_MS_1M = 60_000


def _k(i: int, o: float, h: float, l: float, c: float,
       vol: float = 100.0, tbv: float = 50.0,
       base_time: int = 0) -> Kline:
    """建構第 i 根 1m K 棒。"""
    ot = base_time + i * _MS_1M
    return Kline(
        symbol="BTCUSDT", interval="1m",
        open_time=ot, close_time=ot + _MS_1M - 1,
        open=o, high=h, low=l, close=c,
        volume=vol, taker_buy_volume=tbv, is_closed=True,
    )


def _k0_long(i: int, base: float = 50000.0, rng: float = 100.0,
             vol: float = 100.0, tbv: float = 50.0) -> Kline:
    """看多 k0：看跌 + 長下引線 + 收在上半部。"""
    o = base + rng * 0.4
    c = base + rng * 0.1
    h = base + rng * 0.5
    l = base - rng * 0.5
    return _k(i, o, h, l, c, vol=vol, tbv=tbv)


def _k0_short(i: int, base: float = 50000.0, rng: float = 100.0,
              vol: float = 100.0, tbv: float = 50.0) -> Kline:
    """看空 k0：看漲 + 長上引線 + 收在下半部。"""
    o = base - rng * 0.4
    c = base - rng * 0.1
    h = base + rng * 0.5
    l = base - rng * 0.5
    return _k(i, o, h, l, c, vol=vol, tbv=tbv)


def _make_ticks(bar_idx: int, tick_data: list[tuple],
                base_time: int = 0) -> tuple[int, np.ndarray]:
    """建構某根 K 棒內的 tick 資料。

    tick_data: list of (offset_ms, price, qty, is_buyer_maker_bool)
    回傳 (open_time_ms, ndarray shape (N,4))。
    """
    ot = base_time + bar_idx * _MS_1M
    arr = np.empty((len(tick_data), 4), dtype=np.float64)
    for j, (dt, p, q, bm) in enumerate(tick_data):
        arr[j] = [ot + dt, p, q, 1.0 if bm else 0.0]
    return ot, arr


def _build_tick_map(entries: list[tuple[int, np.ndarray]]) -> dict[int, np.ndarray]:
    """從 (open_time, ticks) 列表建構 tick_map。"""
    return {ot: ticks for ot, ticks in entries}


# ═══════════════════════════════════════════════════════════════════════════
# 1. build_bar_map 測試
# ═══════════════════════════════════════════════════════════════════════════

class TestBuildBarMap(unittest.TestCase):
    """tick_cache.build_bar_map 分桶正確性。"""

    def test_basic_mapping(self):
        """每根 K 棒的 tick 被正確歸類。"""
        ticks = np.array([
            [0,     50000, 1, 0],  # bar 0
            [100,   50001, 1, 1],  # bar 0
            [60000, 50010, 2, 0],  # bar 1
            [60500, 50020, 3, 1],  # bar 1
        ], dtype=np.float64)
        kline_times = [(0, 59999), (60000, 119999)]
        result = build_bar_map(ticks, kline_times)
        self.assertEqual(len(result), 2)
        self.assertEqual(len(result[0]), 2)
        self.assertEqual(len(result[60000]), 2)

    def test_no_ticks_for_bar(self):
        """某根 K 棒完全無 tick → 不在 map 中。"""
        ticks = np.array([
            [0, 50000, 1, 0],
        ], dtype=np.float64)
        kline_times = [(0, 59999), (60000, 119999)]
        result = build_bar_map(ticks, kline_times)
        self.assertIn(0, result)
        self.assertNotIn(60000, result)

    def test_empty_ticks(self):
        """空 tick 陣列 → 空 map。"""
        ticks = np.empty((0, 4), dtype=np.float64)
        kline_times = [(0, 59999)]
        result = build_bar_map(ticks, kline_times)
        self.assertEqual(result, {})

    def test_boundary_tick_inclusion(self):
        """tick time 恰好等於 close_time 的 tick 應被包含。"""
        ticks = np.array([
            [59999, 50000, 1, 0],  # 恰好在 close_time
        ], dtype=np.float64)
        kline_times = [(0, 59999)]
        result = build_bar_map(ticks, kline_times)
        self.assertEqual(len(result[0]), 1)

    def test_tick_outside_all_bars(self):
        """tick 不在任何 K 棒範圍內 → 不歸類。"""
        ticks = np.array([
            [200000, 50000, 1, 0],
        ], dtype=np.float64)
        kline_times = [(0, 59999), (60000, 119999)]
        result = build_bar_map(ticks, kline_times)
        self.assertEqual(len(result), 0)


# ═══════════════════════════════════════════════════════════════════════════
# 2. Tick 進場測試
# ═══════════════════════════════════════════════════════════════════════════

class TestTickEntryLong(unittest.TestCase):
    """Tick 模式做多進場。"""

    def _setup_strategy(self, delta_thresh=0.0, vol_period=0):
        strat = WickReversalStrategy()
        strat.long_delta_eff_threshold = delta_thresh
        strat.short_delta_eff_threshold = delta_thresh
        strat.long_vol_sma_period = vol_period
        strat.short_vol_sma_period = vol_period
        return strat

    def test_tick_entry_long_basic(self):
        """tick 價格突破 k0.high + cum_delta_eff > 0 → 成功進場。"""
        strat = self._setup_strategy()
        k0 = _k0_long(0)
        # bar 1: 在 bar 級別 OHLC 突破 k0.high
        bar1 = _k(1, 50020, 50060, 49960, 50055, vol=100, tbv=50)

        # tick 模擬：累計買量多 → cum_delta_eff > 0
        _, ticks1 = _make_ticks(1, [
            (100,  50020, 10, False),  # taker buy
            (200,  50060, 20, False),  # taker buy, price >= k0.high=50050
        ])
        tick_map = {bar1.open_time: ticks1}

        signals = strat.on_history([k0, bar1], tick_map=tick_map)
        entries = [s for s in signals if s.signal_type == "long_entry"]

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].price, k0.high,
                         "signal.price 應為 k0.high（圖表標記）")
        self.assertEqual(entries[0].fill_price, 50060,
                         "fill_price 應為觸發 tick 的實際價格")

    def test_tick_entry_long_defense_broken(self):
        """price < k0.low → 防守線破壞，不進場。"""
        strat = self._setup_strategy()
        k0 = _k0_long(0)
        bar1 = _k(1, 50020, 50060, 49940, 50055, vol=100, tbv=80)

        _, ticks1 = _make_ticks(1, [
            (100, 49940, 10, True),   # price < k0.low=49950 → 防守線破
            (200, 50060, 20, False),  # 即使之後突破也不行
        ])
        tick_map = {bar1.open_time: ticks1}

        signals = strat.on_history([k0, bar1], tick_map=tick_map)
        entries = [s for s in signals if s.signal_type == "long_entry"]
        self.assertEqual(len(entries), 0)

    def test_tick_entry_long_delta_insufficient(self):
        """突破但 cum_delta_eff 未達閾值 → 不進場。"""
        strat = self._setup_strategy(delta_thresh=0.8)
        k0 = _k0_long(0)
        bar1 = _k(1, 50020, 50060, 49960, 50055, vol=100, tbv=50)

        # 所有 tick 都是 buyer_maker（taker sell）→ cum_delta_eff = -1
        _, ticks1 = _make_ticks(1, [
            (100, 50055, 10, True),  # taker sell
            (200, 50060, 10, True),  # taker sell, price >= k0.high
        ])
        tick_map = {bar1.open_time: ticks1}

        signals = strat.on_history([k0, bar1], tick_map=tick_map)
        entries = [s for s in signals if s.signal_type == "long_entry"]
        self.assertEqual(len(entries), 0,
                         "delta_eff 不足時不應進場")

    def test_tick_entry_long_fill_price_higher_than_k0_high(self):
        """fill_price 可能比 k0.high 更高（穿越價）。"""
        strat = self._setup_strategy()
        k0 = _k0_long(0)
        bar1 = _k(1, 50020, 50080, 49960, 50070, vol=100, tbv=80)

        # 第一筆 tick 直接跳到 50080
        _, ticks1 = _make_ticks(1, [
            (100, 50080, 30, False),
        ])
        tick_map = {bar1.open_time: ticks1}

        signals = strat.on_history([k0, bar1], tick_map=tick_map)
        entries = [s for s in signals if s.signal_type == "long_entry"]
        self.assertEqual(len(entries), 1)
        self.assertGreater(entries[0].fill_price, k0.high)


class TestTickEntryShort(unittest.TestCase):
    """Tick 模式做空進場。"""

    def _setup_strategy(self, delta_thresh=0.0, vol_period=0):
        strat = WickReversalStrategy()
        strat.long_delta_eff_threshold = delta_thresh
        strat.short_delta_eff_threshold = delta_thresh
        strat.long_vol_sma_period = vol_period
        strat.short_vol_sma_period = vol_period
        return strat

    def test_tick_entry_short_basic(self):
        """tick 價格 <= k0.low + cum_delta_eff < 0 → 成功進場。"""
        strat = self._setup_strategy()
        k0 = _k0_short(0)  # k0.low=49950, k0.high=50050
        bar1 = _k(1, 49980, 50040, 49940, 49945, vol=100, tbv=50)

        # tick 模擬：累計賣量多 → cum_delta_eff < 0
        _, ticks1 = _make_ticks(1, [
            (100, 49980, 10, True),   # taker sell
            (200, 49940, 20, True),   # taker sell, price <= k0.low
        ])
        tick_map = {bar1.open_time: ticks1}

        signals = strat.on_history([k0, bar1], tick_map=tick_map)
        entries = [s for s in signals if s.signal_type == "short_entry"]

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].price, k0.low)
        self.assertEqual(entries[0].fill_price, 49940)

    def test_tick_entry_short_defense_broken(self):
        """price > k0.high → 做空防守線破壞。"""
        strat = self._setup_strategy()
        k0 = _k0_short(0)
        bar1 = _k(1, 49980, 50060, 49940, 49945, vol=100, tbv=20)

        _, ticks1 = _make_ticks(1, [
            (100, 50060, 10, True),   # price > k0.high → 防守線破
        ])
        tick_map = {bar1.open_time: ticks1}

        signals = strat.on_history([k0, bar1], tick_map=tick_map)
        entries = [s for s in signals if s.signal_type == "short_entry"]
        self.assertEqual(len(entries), 0)


# ═══════════════════════════════════════════════════════════════════════════
# 3. Tick 出場測試
# ═══════════════════════════════════════════════════════════════════════════

class TestTickExitLong(unittest.TestCase):
    """Tick 模式做多出場。"""

    def _setup_and_enter(self, exit_bar_ticks: list[tuple],
                         extra_bars: list[Kline] = None,
                         extra_tick_entries: list = None):
        """建構 k0 + 進場 + 出場場景。回傳 (signals, strategy)。

        exit_bar_ticks: bar index 2 的 tick 資料
        """
        strat = WickReversalStrategy()
        strat.long_delta_eff_threshold = 0.0
        strat.short_delta_eff_threshold = 0.0
        strat.long_vol_sma_period = 0
        strat.short_vol_sma_period = 0

        k0 = _k0_long(0)  # k0.high=50050, k0.low=49950
        # bar 1: 進場（tick 突破 k0.high）
        bar1 = _k(1, 50020, 50060, 49960, 50055, vol=100, tbv=80)
        _, ticks1 = _make_ticks(1, [
            (100, 50060, 30, False),  # taker buy, price >= k0.high
        ])

        bars = [k0, bar1]
        tick_map = {bar1.open_time: ticks1}

        if extra_bars:
            bars.extend(extra_bars)
        if extra_tick_entries:
            for ot, ticks in extra_tick_entries:
                tick_map[ot] = ticks

        # 加一根出場用 bar
        if not extra_bars:
            bar2 = _k(2, 50055, 50080, 49930, 50070, vol=100, tbv=50)
            bars.append(bar2)
            _, ticks2 = _make_ticks(2, exit_bar_ticks)
            tick_map[bar2.open_time] = ticks2

        signals = strat.on_history(bars, tick_map=tick_map)
        return signals, strat

    def test_tick_sl_long(self):
        """tick 價格 <= stop_price → SL 出場。"""
        # 進場後 entry=50060(fill), stop=49950-10=49940
        signals, _ = self._setup_and_enter([
            (100, 49935, 10, True),  # price <= 49940 → SL
        ])
        sl_exits = [s for s in signals if s.label == "SL"]
        self.assertEqual(len(sl_exits), 1)
        self.assertEqual(sl_exits[0].signal_type, "long_exit")
        # fill_price 是實際穿越的 tick 價
        self.assertEqual(sl_exits[0].fill_price, 49935)

    def test_tick_tp_long_delta_negative(self):
        """tick 價格 >= target + cum_delta <= 0 → 直接 TP 出場。"""
        # entry=50060, stop=49940, risk=120, target=50060+120=50180
        target = 50060 + (50060 - 49940)  # 50180
        signals, _ = self._setup_and_enter([
            (100, target + 5, 10, True),  # taker sell → cum_delta < 0, price >= target
        ])
        tp_exits = [s for s in signals if s.label == "TP"]
        self.assertEqual(len(tp_exits), 1)

    def test_tick_tp_long_delta_positive_enters_trailing(self):
        """tick 價格 >= target + cum_delta > 0 → 進入 trailing 模式，不出場。"""
        target = 50060 + (50060 - 49940)  # 50180
        # bar 2: 觸及 target，cum_delta > 0 → 不出場，進 trailing
        bar2 = _k(2, 50100, target + 20, 50050, target + 10, vol=100, tbv=80)
        _, ticks2 = _make_ticks(2, [
            (100, target + 5, 30, False),  # taker buy → cum_delta > 0
        ])
        # bar 3: 繼續 → 不出場（delta 仍順向）
        bar3 = _k(3, target + 10, target + 20, target + 5, target + 15, vol=100, tbv=80)
        _, ticks3 = _make_ticks(3, [
            (100, target + 15, 30, False),  # taker buy，順向
        ])

        signals, strat = self._setup_and_enter(
            [],
            extra_bars=[bar2, bar3],
            extra_tick_entries=[(bar2.open_time, ticks2), (bar3.open_time, ticks3)],
        )
        tp_exits = [s for s in signals if s.label == "TP"]
        td_exits = [s for s in signals if s.label == "TD"]
        sl_exits = [s for s in signals if s.label in ("SL", "TS")]
        self.assertEqual(len(tp_exits), 0, "不應直接 TP")
        self.assertEqual(len(td_exits), 0, "順向 delta 不應 TD")
        self.assertEqual(len(sl_exits), 0, "不應觸發 SL/TS")

    def test_tick_td_exit_long(self):
        """trailing 模式下連續 2 根反向 delta → TD 出場。"""
        target = 50060 + (50060 - 49940)  # 50180

        # bar 2: 觸及 target + cum_delta > 0 → 進入追蹤
        bar2 = _k(2, 50100, target + 20, 50050, target + 10, vol=100, tbv=80)
        _, ticks2 = _make_ticks(2, [
            (100, target + 5, 30, False),  # cum_delta > 0 → trailing
        ])
        # bar 3: 反向 delta (td_consec=1)
        bar3 = _k(3, target + 10, target + 15, target + 5, target + 8,
                  vol=100, tbv=20)
        _, ticks3 = _make_ticks(3, [
            (100, target + 8, 30, True),  # taker sell → cum_delta < 0
        ])
        # bar 4: 反向 delta (td_consec=2) → TD 出場
        bar4 = _k(4, target + 7, target + 12, target + 4, target + 6,
                  vol=100, tbv=20)
        _, ticks4 = _make_ticks(4, [
            (100, target + 6, 30, True),  # taker sell → cum_delta < 0
        ])

        signals, strat = self._setup_and_enter(
            [],
            extra_bars=[bar2, bar3, bar4],
            extra_tick_entries=[
                (bar2.open_time, ticks2),
                (bar3.open_time, ticks3),
                (bar4.open_time, ticks4),
            ],
        )
        td_exits = [s for s in signals if s.label == "TD"]
        self.assertEqual(len(td_exits), 1)
        self.assertEqual(td_exits[0].price, bar4.close,
                         "TD exit price 應為 k.close")

    def test_tick_trailing_stop_exit(self):
        """trailing 模式下 price <= target_price（被提升為 stop）→ TS 出場。"""
        target = 50060 + (50060 - 49940)  # 50180

        # bar 2: 進入 trailing
        bar2 = _k(2, 50100, target + 20, 50050, target + 10, vol=100, tbv=80)
        _, ticks2 = _make_ticks(2, [
            (100, target + 5, 30, False),
        ])
        # bar 3: price 跌穿 trailing stop (= target)
        bar3 = _k(3, target - 5, target + 2, target - 20, target - 10,
                  vol=100, tbv=40)
        _, ticks3 = _make_ticks(3, [
            (100, target - 15, 30, True),  # price <= target (trailing stop)
        ])

        signals, _ = self._setup_and_enter(
            [],
            extra_bars=[bar2, bar3],
            extra_tick_entries=[
                (bar2.open_time, ticks2),
                (bar3.open_time, ticks3),
            ],
        )
        ts_exits = [s for s in signals if s.label == "TS"]
        self.assertEqual(len(ts_exits), 1)
        self.assertEqual(ts_exits[0].fill_price, target - 15,
                         "TS fill_price 應為實際穿越的 tick 價")


class TestTickExitShort(unittest.TestCase):
    """Tick 模式做空出場。"""

    def _setup_short_enter(self):
        strat = WickReversalStrategy()
        strat.long_delta_eff_threshold = 0.0
        strat.short_delta_eff_threshold = 0.0
        strat.long_vol_sma_period = 0
        strat.short_vol_sma_period = 0
        return strat

    def test_tick_sl_short(self):
        """做空：price >= stop_price → SL 出場。"""
        strat = self._setup_short_enter()
        k0 = _k0_short(0)   # k0.high=50050, k0.low=49950
        # bar 1: 進場
        bar1 = _k(1, 49980, 50040, 49940, 49945, vol=100, tbv=20)
        _, ticks1 = _make_ticks(1, [
            (100, 49940, 30, True),  # taker sell, price <= k0.low
        ])
        # 進場: fill=49940, stop=50050+10=50060
        # bar 2: SL
        bar2 = _k(2, 49950, 50070, 49940, 50065, vol=100, tbv=50)
        _, ticks2 = _make_ticks(2, [
            (100, 50065, 10, False),  # price >= 50060 → SL
        ])

        tick_map = {bar1.open_time: ticks1, bar2.open_time: ticks2}
        signals = strat.on_history([k0, bar1, bar2], tick_map=tick_map)
        sl_exits = [s for s in signals if s.label == "SL"]
        self.assertEqual(len(sl_exits), 1)
        self.assertEqual(sl_exits[0].signal_type, "short_exit")


# ═══════════════════════════════════════════════════════════════════════════
# 4. Fallback bar count 測試
# ═══════════════════════════════════════════════════════════════════════════

class TestBarExitSimpleFallback(unittest.TestCase):
    """tick_map 存在但某些 K 棒缺 tick → 回退到 _bar_exit_simple 邏輯。"""

    def test_fallback_no_exit_on_neutral_bar(self):
        """持倉中缺 tick 的 K 棒若 OHLC 未觸 SL/TP → 不出場。"""
        strat = WickReversalStrategy()
        strat.long_delta_eff_threshold = 0.0
        strat.long_vol_sma_period = 0

        k0 = _k0_long(0)
        # bar 1: 有 tick → 進場
        bar1 = _k(1, 50020, 50060, 49960, 50055, vol=100, tbv=80)
        _, ticks1 = _make_ticks(1, [
            (100, 50060, 30, False),
        ])
        # bar 2: 無 tick（不在 tick_map）→ 回退 bar 邏輯，OHLC 中性不觸 SL/TP
        bar2 = _k(2, 50050, 50070, 50030, 50060, vol=100, tbv=50)
        # bar 3: 也沒有 tick，依然中性
        bar3 = _k(3, 50060, 50075, 50040, 50065, vol=100, tbv=50)

        tick_map = {bar1.open_time: ticks1}
        signals = strat.on_history([k0, bar1, bar2, bar3], tick_map=tick_map)

        exits = [s for s in signals if "exit" in s.signal_type]
        self.assertEqual(len(exits), 0, "中性 K 棒不應觸發出場")

    def test_fallback_sl_on_bar_ohlc(self):
        """持倉中缺 tick 的 K 棒若 bar.low <= stop → 觸發 SL。"""
        strat = WickReversalStrategy()
        strat.long_delta_eff_threshold = 0.0
        strat.long_vol_sma_period = 0

        k0 = _k0_long(0)
        bar1 = _k(1, 50020, 50060, 49960, 50055, vol=100, tbv=80)
        _, ticks1 = _make_ticks(1, [(100, 50060, 30, False)])
        # bar 2: 無 tick，但 low 觸及 stop → SL
        bar2 = _k(2, 50055, 50060, 49930, 49935, vol=100, tbv=20)

        tick_map = {bar1.open_time: ticks1}
        signals = strat.on_history([k0, bar1, bar2], tick_map=tick_map)

        sl_exits = [s for s in signals if s.label == "SL"]
        self.assertEqual(len(sl_exits), 1, "bar 回退邏輯應觸發 SL")
        self.assertIsNone(sl_exits[0].fill_price,
                          "bar 回退模式不應有 fill_price")


# ═══════════════════════════════════════════════════════════════════════════
# 5. Tick vs Bar 模式差異
# ═══════════════════════════════════════════════════════════════════════════

class TestTickVsBarDifferences(unittest.TestCase):
    """驗證 tick 與 bar 模式的關鍵行為差異。"""

    def _make_strat(self):
        strat = WickReversalStrategy()
        strat.long_delta_eff_threshold = 0.0
        strat.short_delta_eff_threshold = 0.0
        strat.long_vol_sma_period = 0
        strat.short_vol_sma_period = 0
        return strat

    def test_bar_entry_uses_k0_high_as_fill(self):
        """Bar 模式：fill_price 為 None（由 engine 用 signal.price=k0.high）。"""
        strat = self._make_strat()
        k0 = _k0_long(0)
        bar1 = _k(1, 50020, 50060, 49960, 50055, vol=100, tbv=80)

        signals = strat.on_history([k0, bar1])  # 無 tick_map
        entries = [s for s in signals if s.signal_type == "long_entry"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].price, k0.high)
        self.assertIsNone(entries[0].fill_price,
                          "Bar 模式不應填寫 fill_price")

    def test_tick_entry_has_fill_price(self):
        """Tick 模式：fill_price 為實際 tick 成交價。"""
        strat = self._make_strat()
        k0 = _k0_long(0)
        bar1 = _k(1, 50020, 50060, 49960, 50055, vol=100, tbv=80)
        _, ticks1 = _make_ticks(1, [
            (100, 50060, 30, False),
        ])
        tick_map = {bar1.open_time: ticks1}

        signals = strat.on_history([k0, bar1], tick_map=tick_map)
        entries = [s for s in signals if s.signal_type == "long_entry"]
        self.assertEqual(len(entries), 1)
        self.assertIsNotNone(entries[0].fill_price)
        self.assertEqual(entries[0].fill_price, 50060)

    def test_tick_mode_may_reject_bar_mode_entry(self):
        """bar 模式用全棒 delta_eff 判斷通過，tick 模式用累計 delta 可能不通過。"""
        strat_bar = WickReversalStrategy()
        strat_bar.long_delta_eff_threshold = 0.3
        strat_bar.long_vol_sma_period = 0

        strat_tick = WickReversalStrategy()
        strat_tick.long_delta_eff_threshold = 0.3
        strat_tick.long_vol_sma_period = 0

        k0 = _k0_long(0)
        # 整棒 delta_eff: tbv=70, vol=100 → delta=40, eff=0.4 > 0.3 ✓
        bar1 = _k(1, 50020, 50060, 49960, 50055, vol=100, tbv=70)

        # Bar 模式應進場
        bar_signals = strat_bar.on_history([k0, bar1])
        bar_entries = [s for s in bar_signals if s.signal_type == "long_entry"]
        self.assertEqual(len(bar_entries), 1, "Bar 模式應進場")

        # Tick 模式：突破時刻 cum_delta_eff 可能不足
        # 前半段強賣，最後瞬間才翻為買 → 突破時 cum_delta_eff < 0.3
        _, ticks1 = _make_ticks(1, [
            (100, 50020, 40, True),   # 大量 taker sell
            (200, 50060, 10, False),  # 突破時 cum_buy=10, cum_vol=50
            # cum_delta = 2*10-50 = -30, eff = -30/50 = -0.6 → 不達標
        ])
        tick_map = {bar1.open_time: ticks1}

        tick_signals = strat_tick.on_history([k0, bar1], tick_map=tick_map)
        tick_entries = [s for s in tick_signals if s.signal_type == "long_entry"]
        self.assertEqual(len(tick_entries), 0,
                         "Tick 模式因累計 delta 不足應不進場")


# ═══════════════════════════════════════════════════════════════════════════
# 6. 空 tick_map / partial coverage
# ═══════════════════════════════════════════════════════════════════════════

class TestPartialTickCoverage(unittest.TestCase):
    """tick_map 存在但某些 bar 缺 tick 時的行為。"""

    def test_entry_bar_missing_ticks_falls_back_to_bar(self):
        """進場 K 棒的 open_time 不在 tick_map → 回退到 bar 模式進場。"""
        strat = WickReversalStrategy()
        strat.long_delta_eff_threshold = 0.0
        strat.long_vol_sma_period = 0

        k0 = _k0_long(0)
        # bar 1: OHLC 滿足進場條件，但 tick_map 中無此 bar 的 ticks
        bar1 = _k(1, 50020, 50060, 49960, 50055, vol=100, tbv=80)

        # 空的 tick_map（有其他 bar 的 ticks 但不包含 bar1）
        _, other_ticks = _make_ticks(5, [(0, 50000, 1, False)])
        tick_map = {5 * _MS_1M: other_ticks}

        signals = strat.on_history([k0, bar1], tick_map=tick_map)
        entries = [s for s in signals if s.signal_type == "long_entry"]
        # 當 tick_map 存在但 bar1 不在裡面時，走 bar 模式進場
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0].fill_price,
                          "回退 bar 模式不應有 fill_price")

    def test_empty_tick_map_same_as_bar_mode(self):
        """tick_map = {} → 等同 bar 模式。"""
        strat = WickReversalStrategy()
        strat.long_delta_eff_threshold = 0.0
        strat.long_vol_sma_period = 0

        k0 = _k0_long(0)
        bar1 = _k(1, 50020, 50060, 49960, 50055, vol=100, tbv=80)

        bar_signals = strat.on_history([k0, bar1])
        tick_signals = strat.on_history([k0, bar1], tick_map={})

        bar_entries = [s for s in bar_signals if "entry" in s.signal_type]
        tick_entries = [s for s in tick_signals if "entry" in s.signal_type]
        # {} is falsy, so use_ticks=False → same as bar mode
        self.assertEqual(len(bar_entries), len(tick_entries))


# ═══════════════════════════════════════════════════════════════════════════
# 7. Vol SMA look-ahead 防護
# ═══════════════════════════════════════════════════════════════════════════

class TestVolSmaLookAhead(unittest.TestCase):
    """Tick 模式 vol SMA 使用前 N 根已收棒；不包含當根（消除 look-ahead）。"""

    def test_vol_sma_ok_uses_previous_bars(self):
        """直接呼叫 _vol_sma_ok，確認用的是 i 之前的 period 根。"""
        strat = WickReversalStrategy()

        # 建構 5 根 klines，volume = [100, 100, 100, 100, 999]
        bars = [_k(i, 50000, 50010, 49990, 50005, vol=100) for i in range(4)]
        bars.append(_k(4, 50000, 50010, 49990, 50005, vol=999))

        # cur_idx=4, period=4 → 應只用 bars[0:4] 的 volume → SMA=100
        # cur_vol=prev_bar.vol=100
        result = strat._vol_sma_ok(bars, 4, 100.0, 4, 1.2)
        # 100 > 100*1.2=120 → False（volume 不足）
        self.assertFalse(result)

        # cur_vol=150 → 150 > 120 → True
        result2 = strat._vol_sma_ok(bars, 4, 150.0, 4, 1.2)
        self.assertTrue(result2)

    def test_vol_sma_period_zero_always_passes(self):
        """period=0 → 永遠通過。"""
        strat = WickReversalStrategy()
        bars = [_k(0, 50000, 50010, 49990, 50005, vol=100)]
        self.assertTrue(strat._vol_sma_ok(bars, 0, 0.0, 0, 1.0))


# ═══════════════════════════════════════════════════════════════════════════
# 8. Tick 模式多筆交易 round-trip
# ═══════════════════════════════════════════════════════════════════════════

class TestTickMultipleRoundTrips(unittest.TestCase):
    """多次讀入 → 進場 → 出場 → 再進場 cycle。"""

    def test_two_consecutive_trades(self):
        """第一筆 SL 出場後，新的 k0 可再次觸發進場。"""
        strat = WickReversalStrategy()
        strat.long_delta_eff_threshold = 0.0
        strat.short_delta_eff_threshold = 0.0
        strat.long_vol_sma_period = 0
        strat.short_vol_sma_period = 0

        # Trade 1: k0@0, enter@1, SL@2
        k0_1 = _k0_long(0)
        bar1 = _k(1, 50020, 50060, 49960, 50055, vol=100, tbv=80)
        _, ticks1 = _make_ticks(1, [(100, 50060, 30, False)])
        bar2 = _k(2, 50050, 50055, 49930, 49935, vol=100, tbv=20)
        _, ticks2 = _make_ticks(2, [(100, 49935, 10, True)])

        # Trade 2: k0@3, enter@4
        k0_2 = _k0_long(3)
        bar4 = _k(4, 50020, 50060, 49960, 50055, vol=100, tbv=80)
        _, ticks4 = _make_ticks(4, [(100, 50060, 30, False)])

        bars = [k0_1, bar1, bar2, k0_2, bar4]
        tick_map = {
            bar1.open_time: ticks1,
            bar2.open_time: ticks2,
            bar4.open_time: ticks4,
        }

        signals = strat.on_history(bars, tick_map=tick_map)
        entries = [s for s in signals if "entry" in s.signal_type]
        exits = [s for s in signals if "exit" in s.signal_type]

        self.assertEqual(len(entries), 2, "應有 2 筆進場")
        self.assertEqual(len(exits), 1, "只有第 1 筆出場（第 2 筆未結束）")


if __name__ == "__main__":
    unittest.main()
