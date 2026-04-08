"""
整合測試：以手工建構的 Kline 序列驗證策略 + 引擎端到端行為。

場景：
  1. Look-ahead 防護：pending entry 在次根成交
  2. SL / TP 同棒優先級
  3. Zoom 過期
  4. 權益耗盡
  5. 資金費跨多天持倉
  6. 空 / 極短 klines 輸入
  7. fill_price 一致性
"""
import unittest
from dataclasses import replace as dc_replace

from core.data_types import Kline
from strategies.base import StrategySignal
from strategies.wick_reversal import WickReversalStrategy
from backtest.engine import BacktestConfig, simulate_trades


# ═══════════════════════════════════════════════════════════════════════════
# Kline 建構工具
# ═══════════════════════════════════════════════════════════════════════════

_MS_1M = 60_000
_MS_8H = 8 * 3600 * 1000


def _k(i: int, o: float, h: float, l: float, c: float,
       vol: float = 100.0, tbv: float = 50.0,
       base_time: int = 0) -> Kline:
    """快速建構第 i 根 1m K 棒。"""
    ot = base_time + i * _MS_1M
    return Kline(
        symbol="BTCUSDT",
        interval="1m",
        open_time=ot,
        close_time=ot + _MS_1M - 1,
        open=o, high=h, low=l, close=c,
        volume=vol,
        taker_buy_volume=tbv,
        is_closed=True,
    )


def _make_signal(sig_type: str, price: float, stop: float = None,
                 t: int = 0, fill_price: float = None) -> StrategySignal:
    return StrategySignal(open_time=t, price=price, signal_type=sig_type,
                          label="", stop_price=stop, fill_price=fill_price)


def _k0_long(i: int, base: float = 50000.0, rng: float = 100.0,
             vol: float = 100.0, tbv: float = 50.0) -> Kline:
    """建構看多 k0：看跌 K 棒 + 長下引線 + 收在上半部。"""
    # open > close, close >= mid, (close - low) > body
    o = base + rng * 0.4   # 50040
    c = base + rng * 0.1   # 50010
    h = base + rng * 0.5   # 50050
    l = base - rng * 0.5   # 49950
    return _k(i, o, h, l, c, vol=vol, tbv=tbv)


def _k0_short(i: int, base: float = 50000.0, rng: float = 100.0,
              vol: float = 100.0, tbv: float = 50.0) -> Kline:
    """建構看空 k0：看漲 K 棒 + 長上引線 + 收在下半部。"""
    o = base - rng * 0.4
    c = base - rng * 0.1
    h = base + rng * 0.5
    l = base - rng * 0.5
    return _k(i, o, h, l, c, vol=vol, tbv=tbv)


# ═══════════════════════════════════════════════════════════════════════════
# 測試
# ═══════════════════════════════════════════════════════════════════════════

class TestSameBarExecution(unittest.TestCase):
    """驗證同棒（即時）成交邏輯。"""

    def test_entry_fires_on_same_bar_as_signal(self):
        """突破 + delta 滿足的那根 K 棒，訊號的 open_time 應等於該棒。"""
        strat = WickReversalStrategy()
        bars = [_k0_long(0)]
        # bar 1: 突破 k0.high + delta > 0 → 應同棒進場
        bars.append(_k(1, 50020, 50060, 49960, 50055, tbv=80))

        signals = strat.on_history(bars)
        entries = [s for s in signals if s.signal_type == "long_entry"]

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].open_time, bars[1].open_time,
                         "entry signal 應標記在偵測到訊號的同棒")

    def test_td_exit_fires_on_same_bar_as_delta_reversal(self):
        """追蹤模式下 delta 反轉，TD 出場應在同棒以 close 成交。"""
        strat = WickReversalStrategy()
        k0 = _k0_long(0, base=50000, rng=100)
        k0_high = k0.high  # 50050
        stop = k0.low - 10  # 49940
        risk = k0_high - stop
        target = k0_high + risk * 1.0

        bars = [k0]
        # bar 1: 突破 + delta > 0 → 進場 entry=k0_high
        bars.append(_k(1, 50020, 50060, 49960, 50055, tbv=80))
        # bar 2: 觸及 target + delta > 0 → 進入追蹤模式
        bars.append(_k(2, k0_high, target + 10, 49980, target + 5, tbv=80))
        # bar 3: delta ≤ 0，low > 追蹤止損 → TD 出場，同棒 close 成交
        bars.append(_k(3, target + 3, target + 5, target + 1, target + 2, tbv=20))

        signals = strat.on_history(bars)
        td_exits = [s for s in signals if s.label == "TD"]

        self.assertEqual(len(td_exits), 1)
        self.assertEqual(td_exits[0].open_time, bars[3].open_time,
                         "TD exit 應在 delta 反轉的同棒發出")
        self.assertEqual(td_exits[0].price, bars[3].close,
                         "TD exit 成交價應為同棒收盤價")


class TestZoomExpiry(unittest.TestCase):
    """zoom 窗口過期後不應進場。"""

    def test_no_entry_after_zoom_window(self):
        strat = WickReversalStrategy()
        bars = [_k0_long(0)]
        # bars 1-5: 在 zoom 窗口內，但不觸發條件
        for i in range(1, 6):
            bars.append(_k(i, 50000, 50010, 49990, 50005, tbv=50))
        # bar 6: 超過 zoom_bars=5，即使條件滿足也不應進場
        bars.append(_k(6, 50020, 50060, 49960, 50055, tbv=80))

        signals = strat.on_history(bars)
        entries = [s for s in signals if "entry" in s.signal_type]
        self.assertEqual(len(entries), 0, "zoom 過期後不應觸發進場")


class TestEquityDepletion(unittest.TestCase):
    """連續虧損導致權益耗盡。"""

    def test_stop_when_equity_zero(self):
        """一系列做多停損 → equity 持續衰減，最終趨近零。"""
        signals = []
        ms_8h = _MS_8H
        for i in range(50):
            t_entry = i * ms_8h
            t_exit = t_entry + _MS_1M
            signals.append(
                _make_signal("long_entry", 50_000, stop=49_000, t=t_entry)
            )
            signals.append(
                _make_signal("long_exit", 49_000, t=t_exit)  # 停損
            )

        cfg = BacktestConfig(initial_capital=1_000, max_loss_pct=0.50,
                             leverage=20, funding_rate=0.0)
        result = simulate_trades(signals, cfg)

        # 每筆虧 ~50%，50 筆後 equity 應趨近 0
        self.assertLess(result["final_equity"], 1.0,
                        "50 筆連虧後 equity 應趨近 0")


class TestFundingMultiDay(unittest.TestCase):
    """跨多天持倉 → 多次資金費扣除。"""

    def test_48h_hold_deducts_6_fundings(self):
        """持倉 48h = 6 次 8h funding。"""
        signals = [
            _make_signal("long_entry", 50_000, stop=49_900, t=0),
            _make_signal("long_exit", 50_200, t=_MS_8H * 6),
        ]
        cfg = BacktestConfig(initial_capital=100_000, funding_rate=0.0001,
                             leverage=20)
        result = simulate_trades(signals, cfg)
        t = result["trade_list"][0]
        expected = t["qty"] * t["entry"] * 0.0001 * 6
        self.assertAlmostEqual(t["funding_cost"], expected, places=2)


class TestEdgeCases(unittest.TestCase):
    """邊界情境。"""

    def test_empty_klines(self):
        strat = WickReversalStrategy()
        self.assertEqual(strat.on_history([]), [])

    def test_single_kline(self):
        strat = WickReversalStrategy()
        bars = [_k(0, 50000, 50050, 49950, 50020)]
        signals = strat.on_history(bars)
        # 只有 1 根 → n < 2 → 回傳空
        self.assertEqual(signals, [])

    def test_two_klines_no_crash(self):
        strat = WickReversalStrategy()
        bars = [_k(0, 50000, 50050, 49950, 50020),
                _k(1, 50020, 50060, 49960, 50040)]
        signals = strat.on_history(bars)
        # 不應 crash
        self.assertIsInstance(signals, list)



if __name__ == "__main__":
    unittest.main()
