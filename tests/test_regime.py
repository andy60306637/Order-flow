"""tests/test_regime.py — 市場狀態偵測模組單元測試"""
import math
import unittest

from core.data_types import Kline
from core.regime import (
    RegimeFeatures,
    compute_regime_features,
    detect_regime,
    enrich_trades_with_regime,
    _calc_er,
    _calc_ema_slope,
    _calc_hh_hl_score,
    _calc_breakout_ratio,
    _calc_delta_persistence,
    _find_swing_highs,
    _find_swing_lows,
)

import numpy as np

_MS_1M = 60_000


def _k(
    i: int,
    o: float,
    h: float,
    l: float,
    c: float,
    vol: float = 500.0,
    tbv: float = 250.0,
) -> Kline:
    ot = i * _MS_1M
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


def _trend_up_klines(n: int = 60, start: float = 50000.0, step: float = 50.0) -> list[Kline]:
    """線性上漲的 K 棒序列：每根收盤比前一根高 step，delta 偏多。"""
    klines = []
    for i in range(n):
        c = start + i * step
        o = c - step * 0.3
        h = c + step * 0.2
        l = o - step * 0.1
        tbv = 400.0  # 買方主導
        klines.append(_k(i, o, h, l, c, vol=500.0, tbv=tbv))
    return klines


def _trend_down_klines(n: int = 60, start: float = 55000.0, step: float = 50.0) -> list[Kline]:
    """線性下跌的 K 棒序列：每根收盤比前一根低 step，delta 偏空。"""
    klines = []
    for i in range(n):
        c = start - i * step
        o = c + step * 0.3
        h = o + step * 0.1
        l = c - step * 0.2
        tbv = 100.0  # 賣方主導
        klines.append(_k(i, o, h, l, c, vol=500.0, tbv=tbv))
    return klines


def _range_klines(n: int = 60, center: float = 50000.0, amplitude: float = 300.0) -> list[Kline]:
    """震盪的 K 棒序列：close 以正弦波在 center ± amplitude 之間震盪。"""
    klines = []
    for i in range(n):
        c = center + amplitude * math.sin(2 * math.pi * i / 8)
        o = center + amplitude * math.sin(2 * math.pi * (i - 1) / 8)
        h = max(o, c) + 50.0
        l = min(o, c) - 50.0
        # delta 也跟著震盪
        tbv = 250.0 + 150.0 * math.sin(2 * math.pi * i / 8)
        klines.append(_k(i, o, h, l, c, vol=500.0, tbv=tbv))
    return klines


# ─── Feature calculator unit tests ───────────────────────────────────────────

class TestCalcEr(unittest.TestCase):

    def test_perfect_trend(self):
        closes = np.arange(1.0, 22.0)  # [1, 2, ..., 21]，21 根，ER=1.0
        er = _calc_er(closes, window=20)
        self.assertIsNotNone(er)
        self.assertAlmostEqual(er, 1.0, places=5)

    def test_flat(self):
        closes = np.ones(25)
        er = _calc_er(closes, window=20)
        self.assertIsNotNone(er)
        self.assertAlmostEqual(er, 0.0, places=5)

    def test_oscillating(self):
        closes = np.array([float(1 + (i % 2)) for i in range(25)])
        er = _calc_er(closes, window=20)
        self.assertIsNotNone(er)
        self.assertLess(er, 0.15)  # 震盪中應趨近 0

    def test_insufficient_data(self):
        closes = np.arange(5.0)
        er = _calc_er(closes, window=20)
        self.assertIsNone(er)


class TestCalcEmaSlope(unittest.TestCase):

    def test_uptrend_positive_slope(self):
        closes = np.linspace(50000.0, 53000.0, 60)
        slope = _calc_ema_slope(closes, ema_period=20, slope_lookback=5)
        self.assertIsNotNone(slope)
        self.assertGreater(slope, 0.0)

    def test_downtrend_negative_slope(self):
        closes = np.linspace(55000.0, 52000.0, 60)
        slope = _calc_ema_slope(closes, ema_period=20, slope_lookback=5)
        self.assertIsNotNone(slope)
        self.assertLess(slope, 0.0)

    def test_flat_near_zero(self):
        closes = np.full(40, 50000.0)
        slope = _calc_ema_slope(closes, ema_period=20, slope_lookback=5)
        self.assertIsNotNone(slope)
        self.assertAlmostEqual(slope, 0.0, places=8)

    def test_insufficient_data(self):
        closes = np.arange(10.0)
        slope = _calc_ema_slope(closes, ema_period=20, slope_lookback=5)
        self.assertIsNone(slope)


class TestFindSwingPoints(unittest.TestCase):

    def test_swing_highs_basic(self):
        values = np.array([1, 2, 3, 2, 1, 2, 3, 2, 1], dtype=float)
        sh = _find_swing_highs(values, pivot_bars=2)
        self.assertIn(2, sh)

    def test_swing_lows_basic(self):
        values = np.array([3, 2, 1, 2, 3, 2, 1, 2, 3], dtype=float)
        sl = _find_swing_lows(values, pivot_bars=2)
        self.assertIn(2, sl)

    def test_monotone_no_swings(self):
        values = np.arange(10.0)
        sh = _find_swing_highs(values, pivot_bars=2)
        self.assertEqual(len(sh), 0)


class TestCalcHhHlScore(unittest.TestCase):

    def test_uptrend_positive_score(self):
        n = 60
        # 明顯上升趨勢：每根 high 和 low 都比前一根高
        highs = np.linspace(50100.0, 56000.0, n)
        lows  = np.linspace(49900.0, 55500.0, n)
        score = _calc_hh_hl_score(highs, lows, struct_window=50, pivot_bars=3)
        if score is not None:
            self.assertGreater(score, 0.0)

    def test_downtrend_negative_score(self):
        n = 60
        highs = np.linspace(56000.0, 50100.0, n)
        lows  = np.linspace(55500.0, 49900.0, n)
        score = _calc_hh_hl_score(highs, lows, struct_window=50, pivot_bars=3)
        if score is not None:
            self.assertLess(score, 0.0)

    def test_insufficient_swings_returns_none(self):
        highs = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        lows  = np.array([0.5, 1.5, 2.5, 3.5, 4.5])
        score = _calc_hh_hl_score(highs, lows, struct_window=50, pivot_bars=3)
        self.assertIsNone(score)


class TestCalcBreakoutRatio(unittest.TestCase):

    def test_insufficient_data(self):
        closes = highs = lows = np.arange(5.0)
        r_up, r_down, cnt_up, cnt_down = _calc_breakout_ratio(highs, lows, closes, 30, 10)
        self.assertIsNone(r_up)
        self.assertIsNone(r_down)

    def test_trending_up_breakouts_follow(self):
        n = 70
        closes = np.linspace(50000.0, 57000.0, n)
        highs  = closes + 100.0
        lows   = closes - 100.0
        r_up, r_down, cnt_up, cnt_down = _calc_breakout_ratio(highs, lows, closes, 30, 10)
        # 強勢上漲中應有向上突破
        if r_up is not None and cnt_up >= 2:
            self.assertGreaterEqual(r_up, 0.0)

    def test_no_crash_flat(self):
        closes = np.full(50, 50000.0)
        highs  = closes + 10.0
        lows   = closes - 10.0
        r_up, r_down, cnt_up, cnt_down = _calc_breakout_ratio(highs, lows, closes, 30, 10)
        # 只確認不 crash；flat 市場突破次數可能為 0
        self.assertIsInstance(cnt_up, int)
        self.assertIsInstance(cnt_down, int)


class TestCalcDeltaPersistence(unittest.TestCase):

    def test_all_positive_delta(self):
        klines = [_k(i, 50000.0, 50100.0, 49900.0, 50050.0, vol=500.0, tbv=400.0)
                  for i in range(20)]
        dp_up, dp_down, consistency = _calc_delta_persistence(klines, 10)
        self.assertGreater(dp_up, dp_down)
        self.assertGreater(consistency, 0.5)

    def test_all_negative_delta(self):
        klines = [_k(i, 50000.0, 50100.0, 49900.0, 49950.0, vol=500.0, tbv=100.0)
                  for i in range(20)]
        dp_up, dp_down, consistency = _calc_delta_persistence(klines, 10)
        self.assertGreater(dp_down, dp_up)

    def test_alternating_low_consistency(self):
        klines = []
        for i in range(20):
            tbv = 400.0 if i % 2 == 0 else 100.0
            klines.append(_k(i, 50000.0, 50100.0, 49900.0, 50000.0, vol=500.0, tbv=tbv))
        _, _, consistency = _calc_delta_persistence(klines, 10)
        self.assertLess(consistency, 0.3)

    def test_empty_klines(self):
        dp_up, dp_down, consistency = _calc_delta_persistence([], 10)
        self.assertEqual(dp_up, 0.0)
        self.assertEqual(dp_down, 0.0)
        self.assertEqual(consistency, 0.0)


# ─── detect_regime integration tests ─────────────────────────────────────────

class TestDetectRegime(unittest.TestCase):

    def test_trend_up(self):
        klines = _trend_up_klines(n=60)
        label = detect_regime(klines)
        self.assertEqual(label, "trend_up")

    def test_trend_down(self):
        klines = _trend_down_klines(n=60)
        label = detect_regime(klines)
        self.assertEqual(label, "trend_down")

    def test_range(self):
        klines = _range_klines(n=60)
        label = detect_regime(klines)
        # 震盪應為 range 或 neutral（訊號不明確時 neutral 也合理）
        self.assertIn(label, ("range", "neutral"))

    def test_insufficient_data_returns_neutral(self):
        klines = _trend_up_klines(n=5)
        label = detect_regime(klines)
        self.assertEqual(label, "neutral")

    def test_all_same_close_no_crash(self):
        klines = [_k(i, 50000.0, 50100.0, 49900.0, 50000.0) for i in range(60)]
        label = detect_regime(klines)
        self.assertIn(label, ("range", "neutral"))

    def test_returns_valid_label(self):
        klines = _trend_up_klines(n=60)
        label = detect_regime(klines)
        self.assertIn(label, ("trend_up", "trend_down", "range", "neutral"))


# ─── compute_regime_features tests ───────────────────────────────────────────

class TestComputeRegimeFeatures(unittest.TestCase):

    def test_returns_regime_features_type(self):
        klines = _trend_up_klines(n=60)
        feat = compute_regime_features(klines)
        self.assertIsInstance(feat, RegimeFeatures)

    def test_label_matches_detect_regime(self):
        klines = _trend_up_klines(n=60)
        feat = compute_regime_features(klines)
        label = detect_regime(klines)
        self.assertEqual(feat.label, label)

    def test_scores_non_negative(self):
        klines = _trend_up_klines(n=60)
        feat = compute_regime_features(klines)
        self.assertGreaterEqual(feat.score_up, 0.0)
        self.assertGreaterEqual(feat.score_down, 0.0)
        self.assertGreaterEqual(feat.score_range, 0.0)

    def test_active_voters_range(self):
        klines = _trend_up_klines(n=60)
        feat = compute_regime_features(klines)
        self.assertGreaterEqual(feat.active_voters, 0)
        self.assertLessEqual(feat.active_voters, 5)

    def test_insufficient_data_all_none(self):
        klines = _trend_up_klines(n=5)
        feat = compute_regime_features(klines)
        self.assertIsNone(feat.er)
        self.assertIsNone(feat.ema_slope)
        self.assertEqual(feat.active_voters, 0)
        self.assertEqual(feat.label, "neutral")

    def test_er_in_range(self):
        klines = _trend_up_klines(n=60)
        feat = compute_regime_features(klines)
        if feat.er is not None:
            self.assertGreaterEqual(feat.er, 0.0)
            self.assertLessEqual(feat.er, 1.0)

    def test_delta_consistency_in_range(self):
        klines = _trend_up_klines(n=60)
        feat = compute_regime_features(klines)
        self.assertGreaterEqual(feat.delta_consistency, 0.0)
        self.assertLessEqual(feat.delta_consistency, 1.0)

    def test_uptrend_features_direction(self):
        klines = _trend_up_klines(n=60)
        feat = compute_regime_features(klines)
        # 上升趨勢：ema_slope 應為正
        if feat.ema_slope is not None:
            self.assertGreater(feat.ema_slope, 0.0)
        # 上升趨勢：多方 delta 應 >= 空方
        self.assertGreaterEqual(feat.delta_persistence_up, feat.delta_persistence_down)


# ─── enrich_trades_with_regime tests ─────────────────────────────────────────

class TestEnrichTradesWithRegime(unittest.TestCase):

    def _make_trade(self, entry_time: int, direction: str = "long") -> dict:
        return {
            "dir": direction,
            "entry": 50000.0,
            "exit": 51000.0,
            "entry_time": entry_time,
            "exit_time": entry_time + _MS_1M * 5,
            "net_pnl": 100.0,
        }

    def test_adds_regime_field(self):
        klines = _trend_up_klines(n=60)
        trades = [self._make_trade(klines[40].open_time)]
        enrich_trades_with_regime(trades, klines, lookback=50)
        self.assertIn("regime", trades[0])
        self.assertIn(trades[0]["regime"], ("trend_up", "trend_down", "range", "neutral"))

    def test_trend_up_klines_regime(self):
        klines = _trend_up_klines(n=60)
        # 進場時間設在第 55 根，讓前 50 根都是上漲
        trades = [self._make_trade(klines[55].open_time)]
        enrich_trades_with_regime(trades, klines, lookback=50)
        self.assertEqual(trades[0]["regime"], "trend_up")

    def test_trend_down_klines_regime(self):
        klines = _trend_down_klines(n=60)
        trades = [self._make_trade(klines[55].open_time, "short")]
        enrich_trades_with_regime(trades, klines, lookback=50)
        self.assertEqual(trades[0]["regime"], "trend_down")

    def test_empty_trade_list(self):
        klines = _trend_up_klines(n=60)
        result = enrich_trades_with_regime([], klines)
        self.assertEqual(result, [])

    def test_empty_klines(self):
        trades = [self._make_trade(_MS_1M * 10)]
        result = enrich_trades_with_regime(trades, [])
        # 無 klines 時保持原 trade_list 不 crash
        self.assertEqual(len(result), 1)
        self.assertNotIn("regime", result[0])

    def test_entry_time_before_klines(self):
        klines = _trend_up_klines(n=60)
        trades = [self._make_trade(0)]  # open_time=0，比任何 kline 都早
        enrich_trades_with_regime(trades, klines, lookback=50)
        self.assertEqual(trades[0]["regime"], "neutral")

    def test_multiple_trades_different_regimes(self):
        up   = _trend_up_klines(n=60)
        down = _trend_down_klines(n=60, start=up[-1].close)
        # 把 down klines 的 open_time 接在 up 後面
        offset = up[-1].open_time + _MS_1M
        down_shifted = [
            _k(i, k.open, k.high, k.low, k.close, k.volume, k.taker_buy_volume)
            for i, k in enumerate(down)
        ]
        for i, k in enumerate(down_shifted):
            k.open_time  = offset + i * _MS_1M    # type: ignore[attr-defined]
            k.close_time = k.open_time + _MS_1M - 1  # type: ignore[attr-defined]

        klines = up + down_shifted
        trades = [
            self._make_trade(up[55].open_time, "long"),
            self._make_trade(down_shifted[55].open_time, "short"),
        ]
        enrich_trades_with_regime(trades, klines, lookback=50)
        self.assertEqual(trades[0]["regime"], "trend_up")
        self.assertEqual(trades[1]["regime"], "trend_down")

    def test_returns_same_list_object(self):
        klines = _trend_up_klines(n=60)
        trades = [self._make_trade(klines[40].open_time)]
        result = enrich_trades_with_regime(trades, klines)
        self.assertIs(result, trades)  # 原地修改，回傳同一物件


if __name__ == "__main__":
    unittest.main()
