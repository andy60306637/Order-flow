"""
backtest.engine 單元測試。

涵蓋：
  1. Maker / Taker 手續費差異
  2. 槓桿放大手續費（按名目價值計算）
  3. max_loss_pct 確實限制單筆最大虧損
  4. 多空分離統計正確
"""
import math
import unittest

from strategies.base import StrategySignal
from backtest.engine import BacktestConfig, simulate_trades, FEE_RATES, _calc_qty


# ═══════════════════════════════════════════════════════════════════════════
# 輔助函式
# ═══════════════════════════════════════════════════════════════════════════

def _make_signal(sig_type: str, price: float, stop: float = None, t: int = 0) -> StrategySignal:
    return StrategySignal(open_time=t, price=price, signal_type=sig_type,
                          label="", stop_price=stop)


class TestFeeCalculation(unittest.TestCase):
    """Maker / Taker 手續費差異 & 槓桿放大。"""

    def _single_long_signals(self, entry: float, exit_: float, stop: float):
        return [
            _make_signal("long_entry", entry, stop=stop, t=1),
            _make_signal("long_exit", exit_, t=2),
        ]

    def test_taker_fee(self):
        """Taker 費率 0.05%，手續費 = notional × 0.0005。"""
        signals = self._single_long_signals(50_000, 50_100, 49_900)
        cfg = BacktestConfig(initial_capital=10_000, max_loss_pct=0.02,
                             leverage=20, fee_mode="Taker")
        result = simulate_trades(signals, cfg)
        t = result["trade_list"][0]
        qty = t["qty"]
        expected_fee = qty * 50_000 * 0.0005 + qty * 50_100 * 0.0005
        self.assertAlmostEqual(t["total_fee"], expected_fee, places=4)
        self.assertEqual(result["fee_mode"], "Taker")

    def test_maker_fee(self):
        """Maker 費率 0.02%，手續費 = notional × 0.0002。"""
        signals = self._single_long_signals(50_000, 50_100, 49_900)
        cfg = BacktestConfig(initial_capital=10_000, max_loss_pct=0.02,
                             leverage=20, fee_mode="Maker")
        result = simulate_trades(signals, cfg)
        t = result["trade_list"][0]
        qty = t["qty"]
        expected_fee = qty * 50_000 * 0.0002 + qty * 50_100 * 0.0002
        self.assertAlmostEqual(t["total_fee"], expected_fee, places=4)

    def test_maker_cheaper_than_taker(self):
        """相同交易，Maker 手續費嚴格低於 Taker。"""
        signals = self._single_long_signals(50_000, 50_100, 49_900)
        r_maker = simulate_trades(signals, BacktestConfig(fee_mode="Maker"))
        r_taker = simulate_trades(signals, BacktestConfig(fee_mode="Taker"))
        self.assertLess(
            r_maker["trade_list"][0]["total_fee"],
            r_taker["trade_list"][0]["total_fee"],
        )

    def test_fee_based_on_notional_not_margin(self):
        """手續費必須以名目價值計算，而非保證金。"""
        signals = self._single_long_signals(50_000, 50_000, 49_900)
        cfg = BacktestConfig(initial_capital=10_000, leverage=20, fee_mode="Taker")
        result = simulate_trades(signals, cfg)
        t = result["trade_list"][0]
        # 名目價值 ≈ equity * leverage = 200,000（受風險限制可能更小）
        # 手續費必須 > margin * fee_rate（即 10000 * 0.0005 = 5）
        margin_based_fee = 10_000 * 0.0005 * 2  # 開 + 平
        self.assertGreater(t["total_fee"], margin_based_fee * 0.5,
                           "手續費應基於名目價值，不是保證金")


class TestPositionSizingAndRisk(unittest.TestCase):
    """max_loss_pct 限制 & 槓桿限制。"""

    def test_max_loss_limits_actual_loss(self):
        """停損觸發時，虧損不超過 equity × max_loss_pct + 手續費。"""
        # 設計一筆做多、停損出場的交易
        entry_p, stop_p = 50_000.0, 49_900.0
        signals = [
            _make_signal("long_entry", entry_p, stop=stop_p, t=1),
            _make_signal("long_exit", stop_p, t=2),  # 在停損價出場
        ]
        capital = 10_000.0
        mlp = 0.02  # 2%
        cfg = BacktestConfig(initial_capital=capital, max_loss_pct=mlp,
                             leverage=100, fee_mode="Taker")
        result = simulate_trades(signals, cfg)
        t = result["trade_list"][0]

        # 毛損 = qty * (entry - stop) = qty * 100
        # 應 ≈ capital * max_loss_pct = 200
        gross_loss = abs(t["gross_pnl"])
        max_allowed = capital * mlp
        self.assertAlmostEqual(gross_loss, max_allowed, delta=0.01,
                               msg="停損觸發時毛損 ≈ equity × max_loss_pct")

        # 淨損 = 毛損 + 手續費（略大於 max_allowed，但手續費合理）
        self.assertLess(abs(t["net_pnl"]), max_allowed + t["total_fee"] + 0.01)

    def test_leverage_caps_position(self):
        """槓桿限制在風險限制寬鬆時應成為瓶頸。"""
        # Stop 距離很近 → 風險限制允許極大倉位 → 槓桿限制應介入
        entry_p, stop_p = 50_000.0, 49_999.0  # 只差 1
        signals = [
            _make_signal("long_entry", entry_p, stop=stop_p, t=1),
            _make_signal("long_exit", 50_001, t=2),
        ]
        capital = 10_000.0
        leverage = 5
        cfg = BacktestConfig(initial_capital=capital, max_loss_pct=0.10,
                             leverage=leverage, fee_mode="Maker")
        result = simulate_trades(signals, cfg)
        t = result["trade_list"][0]

        max_notional = capital * leverage
        actual_notional = t["qty"] * entry_p
        self.assertAlmostEqual(actual_notional, max_notional, delta=0.01,
                               msg="槓桿限制應為倉位瓶頸")

    def test_skip_when_equity_insufficient(self):
        """資金不足時交易應被跳過。"""
        signals = [
            _make_signal("long_entry", 50_000, stop=49_900, t=1),
            _make_signal("long_exit", 50_050, t=2),
        ]
        cfg = BacktestConfig(initial_capital=0, max_loss_pct=0.02, leverage=20)
        result = simulate_trades(signals, cfg)
        self.assertTrue(result["trade_list"][0]["skipped"])

    def test_invalid_stop_distance_skip(self):
        """停損在進場同側（距離 ≤ 0）時應跳過。"""
        signals = [
            _make_signal("long_entry", 50_000, stop=50_100, t=1),  # stop > entry for long
            _make_signal("long_exit", 50_050, t=2),
        ]
        cfg = BacktestConfig(initial_capital=10_000)
        result = simulate_trades(signals, cfg)
        self.assertTrue(result["trade_list"][0]["skipped"])

    def test_calc_qty_returns_none_for_zero_equity(self):
        qty = _calc_qty(0, 50_000, 49_900, "long", 0.02, 20)
        self.assertIsNone(qty)


class TestLongShortStats(unittest.TestCase):
    """多空分離統計正確。"""

    def _build_mixed_signals(self):
        """2 筆做多（1 賺 1 虧）+ 1 筆做空（賺）。"""
        return [
            # Long 1: 賺
            _make_signal("long_entry",  50_000, stop=49_900, t=1),
            _make_signal("long_exit",   50_200, t=2),
            # Long 2: 虧
            _make_signal("long_entry",  50_000, stop=49_900, t=3),
            _make_signal("long_exit",   49_850, t=4),
            # Short 1: 賺
            _make_signal("short_entry", 50_000, stop=50_100, t=5),
            _make_signal("short_exit",  49_800, t=6),
        ]

    def test_trade_counts(self):
        signals = self._build_mixed_signals()
        cfg = BacktestConfig(initial_capital=100_000, leverage=20)
        result = simulate_trades(signals, cfg)
        self.assertEqual(result["long_trades"], 2)
        self.assertEqual(result["short_trades"], 1)
        self.assertEqual(result["trades"], 3)

    def test_long_win_rate(self):
        signals = self._build_mixed_signals()
        cfg = BacktestConfig(initial_capital=100_000, leverage=20)
        result = simulate_trades(signals, cfg)
        # 做多 2 筆中 1 勝 → 50%
        self.assertAlmostEqual(result["long_win_rate"], 50.0, places=0)

    def test_short_win_rate(self):
        signals = self._build_mixed_signals()
        cfg = BacktestConfig(initial_capital=100_000, leverage=20)
        result = simulate_trades(signals, cfg)
        # 做空 1 筆全勝 → 100%
        self.assertAlmostEqual(result["short_win_rate"], 100.0, places=0)

    def test_short_pf_inf_when_no_loss(self):
        """做空全勝時 PF = inf。"""
        signals = self._build_mixed_signals()
        cfg = BacktestConfig(initial_capital=100_000, leverage=20)
        result = simulate_trades(signals, cfg)
        self.assertEqual(result["short_profit_factor"], float("inf"))

    def test_no_trades_returns_zeros(self):
        result = simulate_trades([], BacktestConfig())
        self.assertEqual(result["trades"], 0)
        self.assertEqual(result["long_trades"], 0)
        self.assertEqual(result["short_trades"], 0)
        self.assertEqual(result["win_rate"], 0.0)


class TestEquityTracking(unittest.TestCase):
    """權益與回撤追蹤。"""

    def test_equity_decreases_on_loss(self):
        signals = [
            _make_signal("long_entry", 50_000, stop=49_900, t=1),
            _make_signal("long_exit", 49_900, t=2),  # 停損
        ]
        cfg = BacktestConfig(initial_capital=10_000, max_loss_pct=0.02, leverage=20)
        result = simulate_trades(signals, cfg)
        self.assertLess(result["final_equity"], 10_000)
        self.assertLess(result["total_return_pct"], 0)

    def test_equity_increases_on_profit(self):
        signals = [
            _make_signal("long_entry", 50_000, stop=49_900, t=1),
            _make_signal("long_exit", 50_500, t=2),
        ]
        cfg = BacktestConfig(initial_capital=10_000, max_loss_pct=0.02,
                             leverage=20, fee_mode="Maker")
        result = simulate_trades(signals, cfg)
        self.assertGreater(result["final_equity"], 10_000)

    def test_max_drawdown_positive(self):
        """有虧損交易時 max_drawdown > 0。"""
        signals = [
            _make_signal("long_entry", 50_000, stop=49_900, t=1),
            _make_signal("long_exit", 49_900, t=2),
        ]
        cfg = BacktestConfig(initial_capital=10_000, max_loss_pct=0.02, leverage=20)
        result = simulate_trades(signals, cfg)
        self.assertGreater(result["max_drawdown_pct"], 0)


if __name__ == "__main__":
    unittest.main()
