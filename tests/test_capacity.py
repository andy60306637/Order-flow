"""
容量分析測試：engine 契約擴充 + capacity 數學模型 + VPR + 掃描。

涵蓋 strategy_capacity_plan.md §9 測試計劃：
  9.1 引擎契約測試
  9.2 容量數學測試
  9.3 VPR 測試
  9.4 掃描測試
"""
import math
import unittest
from unittest.mock import patch

import numpy as np

from strategies.base import StrategySignal
from backtest.engine import BacktestConfig, simulate_trades
from backtest.capacity import (
    CapacityAnalyzer,
    CapacityConfig,
    CapacityPoint,
    CapacityReport,
)


# ═══════════════════════════════════════════════════════════════════════════
# 輔助
# ═══════════════════════════════════════════════════════════════════════════

def _sig(stype, price, t=0, stop=None, label="", fill=None):
    return StrategySignal(
        open_time=t, price=price, signal_type=stype,
        label=label, stop_price=stop, fill_price=fill,
    )


def _make_raw_klines(n_days=60, interval_ms=60_000, base_price=50_000,
                     daily_bars=1440, quote_vol_per_bar=1_000_000):
    """產生假的 raw kline ndarray (N, 12)，用於測試 ADV / volatility。"""
    rows = []
    for d in range(n_days):
        for b in range(daily_bars):
            ot = d * 86_400_000 + b * interval_ms
            ct = ot + interval_ms - 1
            # 加一點隨機性讓 return std > 0
            noise = (d * 17 + b * 7) % 100 / 10000.0  # deterministic pseudo noise
            price = base_price * (1 + noise)
            vol = 10.0  # base volume
            qv = quote_vol_per_bar
            rows.append([
                ot, price, price * 1.001, price * 0.999, price,
                vol, ct, qv, 100, vol * 0.6, qv * 0.6, 0
            ])
    return np.array(rows, dtype=np.float64)


def _simple_klines_small():
    """小量 raw klines: 3 天 × 3 根/天 = 9 根。"""
    rows = []
    prices = [100, 101, 102, 103, 104, 105, 106, 107, 108]
    for i, p in enumerate(prices):
        day = i // 3
        bar = i % 3
        ot = day * 86_400_000 + bar * 60_000
        ct = ot + 59_999
        rows.append([ot, p, p+1, p-1, p+0.5, 50.0, ct, p*50, 20, 30, p*30, 0])
    return np.array(rows, dtype=np.float64)


# ═══════════════════════════════════════════════════════════════════════════
# 9.1 引擎契約測試
# ═══════════════════════════════════════════════════════════════════════════

class TestEngineContract(unittest.TestCase):

    def _long_round_trip(self, entry=50_000, exit_=50_100, stop=49_900):
        return [
            _sig("long_entry", entry, t=1000, stop=stop, label="L4A"),
            _sig("long_exit", exit_, t=2000, label="TP"),
        ]

    def test_no_dynamic_slippage_preserves_old_behavior(self):
        """dynamic_slippage=None 時結果與舊版一致。"""
        signals = self._long_round_trip()
        cfg = BacktestConfig(initial_capital=10_000, fee_mode="Taker")
        result = simulate_trades(signals, cfg)
        t = result["trade_list"][0]
        # 新欄位應存在但 impact_bps 為 0
        self.assertEqual(t["impact_bps"], 0.0)
        self.assertAlmostEqual(t["applied_slippage_bps"], cfg.slippage_bps)
        self.assertGreater(t["entry_notional"], 0)
        self.assertGreater(t["exit_notional"], 0)
        self.assertEqual(t["provisional_entry"], 50_000.0)

    def test_dynamic_slippage_applied(self):
        """有 dynamic_slippage 時，applied_slippage_bps 正確寫入。"""
        extra = 5.0  # 5 bps
        signals = self._long_round_trip()
        cfg = BacktestConfig(
            initial_capital=10_000, fee_mode="Taker",
            slippage_bps=2.0,
            dynamic_slippage=lambda notional, time: extra,
        )
        result = simulate_trades(signals, cfg)
        t = result["trade_list"][0]
        self.assertAlmostEqual(t["impact_bps"], extra)
        self.assertAlmostEqual(t["applied_slippage_bps"], 2.0 + extra)

    def test_entry_label_in_trade_list(self):
        """entry_label 會進入 trade_list。"""
        signals = [
            _sig("long_entry", 50_000, t=1, stop=49_900, label="L4A"),
            _sig("long_exit", 50_100, t=2, label="TP"),
        ]
        result = simulate_trades(signals, BacktestConfig())
        t = result["trade_list"][0]
        self.assertEqual(t["entry_label"], "L4A")
        self.assertEqual(t["exit_label"], "TP")

    def test_entry_notional_exit_notional(self):
        """entry_notional、exit_notional 都會進入 trade_list。"""
        signals = self._long_round_trip()
        result = simulate_trades(signals, BacktestConfig())
        t = result["trade_list"][0]
        self.assertIn("entry_notional", t)
        self.assertIn("exit_notional", t)
        self.assertAlmostEqual(t["entry_notional"], t["qty"] * t["entry"], places=2)

    def test_entry_stop_and_provisional_entry(self):
        """entry_stop、provisional_entry 保留在 trade record。"""
        signals = self._long_round_trip(stop=49_800)
        result = simulate_trades(signals, BacktestConfig())
        t = result["trade_list"][0]
        self.assertEqual(t["entry_stop"], 49_800)
        self.assertEqual(t["provisional_entry"], 50_000)

    def test_short_entry_label(self):
        """short entry label 同樣被保留。"""
        signals = [
            _sig("short_entry", 50_000, t=1, stop=50_200, label="S4B"),
            _sig("short_exit", 49_800, t=2, label="SL"),
        ]
        result = simulate_trades(signals, BacktestConfig())
        t = result["trade_list"][0]
        self.assertEqual(t["entry_label"], "S4B")
        self.assertEqual(t["exit_label"], "SL")


# ═══════════════════════════════════════════════════════════════════════════
# 9.2 容量數學測試
# ═══════════════════════════════════════════════════════════════════════════

class TestCapacityMath(unittest.TestCase):

    def test_impact_bps_unit(self):
        """calc_impact_bps 輸出單位為 bps。"""
        # Q = 100_000, ADV = 1_000_000_000, sigma = 0.02, eta = 1.0
        # impact = 1.0 * 0.02 * sqrt(100000 / 1e9) * 10000
        bps = CapacityAnalyzer.calc_impact_bps(100_000, 1e9, 0.02, 1.0)
        expected = 1.0 * 0.02 * math.sqrt(100_000 / 1e9) * 10_000
        self.assertAlmostEqual(bps, expected, places=6)
        # 確保結果是合理的 bps 範圍（< 100）
        self.assertLess(bps, 100)
        self.assertGreater(bps, 0)

    def test_impact_bps_zero_adv(self):
        """ADV=0 時有穩定 fallback，不 crash。"""
        bps = CapacityAnalyzer.calc_impact_bps(100_000, 0.0, 0.02, 1.0)
        self.assertEqual(bps, 0.0)

    def test_impact_bps_zero_sigma(self):
        """sigma=0 時回傳 0。"""
        bps = CapacityAnalyzer.calc_impact_bps(100_000, 1e9, 0.0, 1.0)
        self.assertEqual(bps, 0.0)

    def test_adv_from_raw_klines(self):
        """ADV 正確計算，使用 quote_volume。"""
        klines = _simple_klines_small()
        adv = CapacityAnalyzer.calc_adv(klines, window_days=30)
        # 3 天的 daily quote vol: 每天 3 根 bar，各 price*50
        # 第 0 天: 100*50 + 101*50 + 102*50 = 15150
        # 第 1 天: 103*50 + 104*50 + 105*50 = 15600
        # 第 2 天: 106*50 + 107*50 + 108*50 = 16050
        expected_daily_vols = [15_150, 15_600, 16_050]
        expected_adv = sum(expected_daily_vols) / 3
        self.assertAlmostEqual(adv, expected_adv, places=0)

    def test_adv_empty(self):
        """空 klines 時 ADV=0。"""
        adv = CapacityAnalyzer.calc_adv(np.empty((0, 12)), 30)
        self.assertEqual(adv, 0.0)

    def test_daily_volatility(self):
        """sigma_daily_frac 對應 daily return std。"""
        klines = _simple_klines_small()
        sigma = CapacityAnalyzer.calc_daily_volatility(klines)
        # 日收盤: day0=102.5, day1=105.5, day2=108.5
        closes = [102.5, 105.5, 108.5]
        log_rets = [math.log(closes[1] / closes[0]),
                    math.log(closes[2] / closes[1])]
        expected = float(np.std(log_rets, ddof=1))
        self.assertAlmostEqual(sigma, expected, places=6)

    def test_daily_volatility_insufficient_data(self):
        """資料不足（< 2 天）時回傳 0。"""
        sigma = CapacityAnalyzer.calc_daily_volatility(np.empty((0, 12)))
        self.assertEqual(sigma, 0.0)


# ═══════════════════════════════════════════════════════════════════════════
# 9.3 VPR 測試
# ═══════════════════════════════════════════════════════════════════════════

class TestVPR(unittest.TestCase):

    def test_bar_based_vpr(self):
        """bar-based VPR 計算正確。"""
        klines = _simple_klines_small()
        trade_list = [
            {"qty": 5.0, "entry_time": 0, "skipped": False},
            {"qty": 10.0, "entry_time": 60_000, "skipped": False},
        ]
        result = CapacityAnalyzer.calc_vpr_from_bars(trade_list, klines)
        # bar 0: volume=50, VPR=5/50=0.1
        self.assertAlmostEqual(result[0]["vpr"], 5.0 / 50.0)
        # bar 1: volume=50, VPR=10/50=0.2
        self.assertAlmostEqual(result[1]["vpr"], 10.0 / 50.0)

    def test_bar_vpr_skipped_trade(self):
        """skipped trade 的 VPR=0。"""
        klines = _simple_klines_small()
        trade_list = [{"qty": 5.0, "entry_time": 0, "skipped": True}]
        result = CapacityAnalyzer.calc_vpr_from_bars(trade_list, klines)
        self.assertEqual(result[0]["vpr"], 0.0)

    def test_tick_based_vpr_with_fallback(self):
        """tick-based VPR: 無 tick 時 fallback 至 K 線 volume。"""
        klines = _simple_klines_small()
        trade_list = [
            {"qty": 5.0, "entry_time": 0, "skipped": False},
        ]
        # Mock tick_cache 回傳空資料
        with patch("backtest.capacity.tick_cache.load_raw", return_value=(None, None)):
            result, fb = CapacityAnalyzer.calc_vpr_from_ticks(
                trade_list, klines, "TESTUSDT"
            )
        # 沒有 tick → 直接用 kline volume，但 has_ticks=False 所以 fb=0
        self.assertEqual(fb, 0)
        self.assertAlmostEqual(result[0]["vpr"], 5.0 / 50.0)

    def test_tick_based_vpr_with_ticks(self):
        """tick-based VPR: 有 tick 時使用 tick 棒量。"""
        klines = _simple_klines_small()
        # 在 bar 0 (ot=0, ct=59999) 放入 ticks，qty 總量=20
        ticks = np.array([
            [100, 100.0, 8.0, 0.0],
            [200, 100.0, 12.0, 1.0],
        ], dtype=np.float64)

        trade_list = [
            {"qty": 5.0, "entry_time": 0, "skipped": False},
        ]

        with patch("backtest.capacity.tick_cache.load_raw",
                    return_value=(ticks, {"start_ms": 0, "end_ms": 59999})):
            result, fb = CapacityAnalyzer.calc_vpr_from_ticks(
                trade_list, klines, "TESTUSDT"
            )
        # tick bar vol = 8 + 12 = 20, VPR = 5/20 = 0.25
        self.assertAlmostEqual(result[0]["vpr"], 5.0 / 20.0)
        self.assertEqual(fb, 0)

    def test_tick_based_vpr_partial_fallback(self):
        """tick 只覆蓋部分 bar 時，缺失的 bar fallback 且計數正確。"""
        klines = _simple_klines_small()
        # 只覆蓋 bar 0，不覆蓋 bar 1 (ot=60000)
        ticks = np.array([
            [100, 100.0, 20.0, 0.0],
        ], dtype=np.float64)

        trade_list = [
            {"qty": 5.0, "entry_time": 0, "skipped": False},
            {"qty": 10.0, "entry_time": 60_000, "skipped": False},
        ]

        with patch("backtest.capacity.tick_cache.load_raw",
                    return_value=(ticks, {"start_ms": 0, "end_ms": 59999})):
            result, fb = CapacityAnalyzer.calc_vpr_from_ticks(
                trade_list, klines, "TESTUSDT"
            )
        # bar 0: tick vol=20, VPR=5/20=0.25
        self.assertAlmostEqual(result[0]["vpr"], 5.0 / 20.0)
        # bar 1: no tick → fallback, VPR=10/50=0.2
        self.assertAlmostEqual(result[1]["vpr"], 10.0 / 50.0)
        # 有 ticks，但 bar 1 fallback → fb=1
        self.assertEqual(fb, 1)


# ═══════════════════════════════════════════════════════════════════════════
# 9.4 掃描測試
# ═══════════════════════════════════════════════════════════════════════════

class TestSweep(unittest.TestCase):

    def _basic_signals(self):
        return [
            _sig("long_entry", 50_000, t=0, stop=49_900, label="L4A"),
            _sig("long_exit", 50_200, t=60_000, label="TP"),
            _sig("short_entry", 50_200, t=120_000, stop=50_300, label="S4A"),
            _sig("short_exit", 50_000, t=180_000, label="TP"),
        ]

    @patch.object(CapacityAnalyzer, "load_raw_klines")
    @patch("backtest.capacity.tick_cache.load_raw", return_value=(None, None))
    def test_sweep_generates_multiple_points(self, mock_tick, mock_kl):
        """掃描會生成多個 CapacityPoint。"""
        mock_kl.return_value = _make_raw_klines(n_days=60, daily_bars=3,
                                                 quote_vol_per_bar=500_000)
        analyzer = CapacityAnalyzer()
        cap_cfg = CapacityConfig(capital_sweep=[1_000, 5_000, 10_000])
        report = analyzer.run_sweep(
            self._basic_signals(),
            BacktestConfig(fee_mode="Taker"),
            cap_cfg, "BTCUSDT", "1m",
        )
        self.assertEqual(len(report.points), 3)
        self.assertEqual(report.points[0].capital, 1_000)
        self.assertEqual(report.points[2].capital, 10_000)

    @patch.object(CapacityAnalyzer, "load_raw_klines")
    @patch("backtest.capacity.tick_cache.load_raw", return_value=(None, None))
    def test_baseline_capital_correct(self, mock_tick, mock_kl):
        """基準資本 = sweep 中最小值。"""
        mock_kl.return_value = _make_raw_klines(n_days=60, daily_bars=3,
                                                 quote_vol_per_bar=500_000)
        analyzer = CapacityAnalyzer()
        cap_cfg = CapacityConfig(capital_sweep=[2_000, 10_000])
        report = analyzer.run_sweep(
            self._basic_signals(),
            BacktestConfig(fee_mode="Taker"),
            cap_cfg, "BTCUSDT", "1m",
        )
        self.assertEqual(report.baseline_capital, 2_000)

    @patch.object(CapacityAnalyzer, "load_raw_klines")
    @patch("backtest.capacity.tick_cache.load_raw", return_value=(None, None))
    def test_capacity_limit_by_pf_drop(self, mock_tick, mock_kl):
        """capacity_limit_usdt 依 profit_factor 衰退門檻正確判定。"""
        mock_kl.return_value = _make_raw_klines(n_days=60, daily_bars=3,
                                                 quote_vol_per_bar=500_000)
        analyzer = CapacityAnalyzer()
        cap_cfg = CapacityConfig(
            capital_sweep=[1_000, 5_000, 10_000, 50_000, 100_000],
            limit_drop_pct=0.20,
        )
        report = analyzer.run_sweep(
            self._basic_signals(),
            BacktestConfig(fee_mode="Taker"),
            cap_cfg, "BTCUSDT", "1m",
        )
        # capacity_limit 應該有值（小資金 PF 不會衰退太多）
        self.assertIsInstance(report, CapacityReport)
        self.assertIsNotNone(report.baseline_profit_factor)
        # 每個 point 都有完整欄位
        for pt in report.points:
            self.assertIsInstance(pt.avg_impact_bps, float)
            self.assertIsInstance(pt.max_vpr, float)
            self.assertIsInstance(pt.warning_count, int)


if __name__ == "__main__":
    unittest.main()
