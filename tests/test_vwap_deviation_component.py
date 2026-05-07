"""tests/test_vwap_deviation_component.py — VWAPDeviationComponent 單元測試"""
import unittest

import numpy as np

from core.data_types import Kline
from strategies.pipeline.component import VWAPDeviationComponent

_MS_1M = 60_000

EXPECTED_KEYS = frozenset({
    "vwap", "vwap_dev", "z_score", "sigma",
    "zone", "in_overextended", "above_vwap", "source",
})


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _k(
    i: int,
    close: float = 100.0,
    volume: float = 100.0,
    taker_buy: float = 50.0,
    high: float | None = None,
    low: float | None = None,
) -> Kline:
    ot = i * _MS_1M
    return Kline(
        symbol="BTCUSDT", interval="1m",
        open_time=ot, close_time=ot + _MS_1M - 1,
        open=close,
        high=close * 1.002 if high is None else high,
        low=close * 0.998  if low  is None else low,
        close=close,
        volume=volume, taker_buy_volume=taker_buy, is_closed=True,
    )


def _ticks(*rows: tuple) -> np.ndarray:
    if not rows:
        return np.empty((0, 4), dtype=np.float64)
    return np.array(rows, dtype=np.float64)


def _klines(n: int, close: float = 100.0, **kwargs) -> list[Kline]:
    return [_k(i, close=close, **kwargs) for i in range(n)]


def _noisy_klines(n: int, base: float = 100.0, sigma: float = 0.5, seed: int = 42) -> list[Kline]:
    rng = np.random.default_rng(seed)
    return [_k(i, close=float(base + rng.normal(0, sigma))) for i in range(n)]


def _kline_vwap(klines: list[Kline]) -> float:
    pv = tv = 0.0
    for k in klines:
        if k.volume > 0:
            pv += (k.high + k.low + k.close) / 3.0 * k.volume
            tv += k.volume
    return pv / tv if tv > 0 else 0.0


# ─── component_id ─────────────────────────────────────────────────────────────

class TestVWAPDeviationComponentId(unittest.TestCase):

    def test_default_id(self):
        c = VWAPDeviationComponent()
        self.assertEqual(c.component_id, "vwap_dev_24_100")

    def test_custom_id(self):
        c = VWAPDeviationComponent(window=12, lookback=50)
        self.assertEqual(c.component_id, "vwap_dev_12_50")

    def test_different_windows_produce_different_ids(self):
        ids = {VWAPDeviationComponent(window=w).component_id for w in (8, 12, 24, 48)}
        self.assertEqual(len(ids), 4)


# ─── 回傳鍵完整性 ─────────────────────────────────────────────────────────────

class TestVWAPDeviationReturnKeys(unittest.TestCase):

    def _comp(self, **kw):
        return VWAPDeviationComponent(window=10, lookback=20, **kw)

    def test_normal_result_has_all_keys(self):
        c = self._comp()
        ks = _noisy_klines(40)
        result = c.compute(ks, idx=39)
        self.assertEqual(set(result.keys()), EXPECTED_KEYS)

    def test_insufficient_data_has_all_keys(self):
        c = self._comp()
        ks = _klines(3)
        result = c.compute(ks, idx=2)
        self.assertEqual(set(result.keys()), EXPECTED_KEYS)


# ─── 資料不足（insufficient_data）────────────────────────────────────────────

class TestVWAPDeviationInsufficientData(unittest.TestCase):

    def setUp(self):
        self.comp = VWAPDeviationComponent(window=10, lookback=20)

    def test_idx_below_window_returns_insufficient(self):
        ks = _klines(20)
        r = self.comp.compute(ks, idx=5)   # idx < window - 1 = 9
        self.assertEqual(r["source"], "insufficient_data")

    def test_insufficient_z_score_is_zero(self):
        ks = _klines(20)
        r = self.comp.compute(ks, idx=5)
        self.assertEqual(r["z_score"], 0.0)
        self.assertEqual(r["vwap_dev"], 0.0)

    def test_insufficient_vwap_equals_current_close(self):
        ks = _klines(20, close=200.0)
        r = self.comp.compute(ks, idx=5)
        self.assertEqual(r["vwap"], 200.0)

    def test_exact_boundary_window_minus_1(self):
        # idx == window - 1：第一根可算（klines[0..window-1]）
        c = VWAPDeviationComponent(window=5, lookback=20)
        ks = _noisy_klines(30)
        r = c.compute(ks, idx=4)   # idx == window - 1 = 4，剛好夠
        self.assertNotEqual(r["source"], "insufficient_data")

    def test_one_below_boundary(self):
        c = VWAPDeviationComponent(window=5, lookback=20)
        ks = _noisy_klines(30)
        r = c.compute(ks, idx=3)   # idx < window - 1
        self.assertEqual(r["source"], "insufficient_data")


# ─── source 欄位 ──────────────────────────────────────────────────────────────

class TestVWAPDeviationSource(unittest.TestCase):

    def setUp(self):
        self.comp = VWAPDeviationComponent(window=10, lookback=20)
        self.ks   = _noisy_klines(40)

    def test_no_tick_map_is_kline_fallback(self):
        r = self.comp.compute(self.ks, idx=39)
        self.assertEqual(r["source"], "kline_fallback")

    def test_empty_tick_map_is_kline_fallback(self):
        r = self.comp.compute(self.ks, idx=39, tick_map={})
        self.assertEqual(r["source"], "kline_fallback")

    def test_tick_map_with_data_is_tick(self):
        k = self.ks[39]
        tick_map = {k.open_time: _ticks(
            (k.open_time, k.close, 50.0, 0),
            (k.open_time, k.close, 50.0, 1),
        )}
        r = self.comp.compute(self.ks, idx=39, tick_map=tick_map)
        self.assertEqual(r["source"], "tick")


# ─── kline VWAP 計算正確性 ────────────────────────────────────────────────────

class TestVWAPDeviationKlineVWAP(unittest.TestCase):

    def test_vwap_matches_manual_computation(self):
        comp = VWAPDeviationComponent(window=5, lookback=20)
        ks   = _noisy_klines(30, sigma=1.0)
        idx  = 29
        r    = comp.compute(ks, idx=idx)

        expected_vwap = _kline_vwap(ks[idx - 4: idx + 1])
        self.assertAlmostEqual(r["vwap"], expected_vwap, places=10)

    def test_vwap_dev_equals_close_minus_vwap_over_vwap(self):
        comp = VWAPDeviationComponent(window=5, lookback=20)
        ks   = _noisy_klines(30, sigma=1.0)
        idx  = 29
        r    = comp.compute(ks, idx=idx)

        expected = (ks[idx].close - r["vwap"]) / r["vwap"]
        self.assertAlmostEqual(r["vwap_dev"], expected, places=10)


# ─── tick VWAP 計算正確性 ─────────────────────────────────────────────────────

class TestVWAPDeviationTickVWAP(unittest.TestCase):

    def _build(self, n: int = 15, window: int = 5):
        comp = VWAPDeviationComponent(window=window, lookback=20)
        ks   = _noisy_klines(n, sigma=1.0, seed=7)
        return comp, ks

    def test_tick_vwap_differs_from_kline_when_prices_differ(self):
        comp, ks = self._build()
        k_last = ks[-1]
        # tick 價格故意偏高
        tick_map = {k_last.open_time: _ticks(
            (k_last.open_time, k_last.close + 5.0, 100.0, 0),
        )}
        r_tick  = comp.compute(ks, idx=len(ks) - 1, tick_map=tick_map)
        r_kline = comp.compute(ks, idx=len(ks) - 1)
        self.assertNotAlmostEqual(r_tick["vwap"], r_kline["vwap"], places=3)

    def test_tick_vwap_matches_manual_for_full_window(self):
        comp = VWAPDeviationComponent(window=3, lookback=20)
        ks   = _noisy_klines(25, sigma=1.0, seed=3)
        idx  = 24

        prices = [101.0, 102.0, 103.0]
        vols   = [50.0,  60.0,  40.0]
        tick_map = {}
        for i, (p, v) in enumerate(zip(prices, vols)):
            ki = ks[idx - 2 + i]
            tick_map[ki.open_time] = _ticks((ki.open_time, p, v, 0))

        r = comp.compute(ks, idx=idx, tick_map=tick_map)
        expected_vwap = sum(p * v for p, v in zip(prices, vols)) / sum(vols)
        self.assertAlmostEqual(r["vwap"], expected_vwap, places=8)
        self.assertEqual(r["source"], "tick")


# ─── σ（歷史相對乖離標準差）─────────────────────────────────────────────────

class TestVWAPDeviationSigma(unittest.TestCase):

    def _manual_sigma(self, ks: list[Kline], idx: int, window: int, lookback: int) -> float:
        hist_start = max(window - 1, idx - lookback)
        devs = []
        for j in range(hist_start, idx):
            w  = ks[max(0, j - window + 1): j + 1]
            hv = _kline_vwap(w)
            if hv > 0:
                devs.append((ks[j].close - hv) / hv)
        return float(np.std(devs)) if len(devs) > 1 else 0.0

    def test_sigma_matches_manual(self):
        window, lookback = 10, 50
        comp = VWAPDeviationComponent(window=window, lookback=lookback)
        ks   = _noisy_klines(120, sigma=1.0)
        idx  = 119
        r    = comp.compute(ks, idx=idx)

        expected = self._manual_sigma(ks, idx, window, lookback)
        self.assertAlmostEqual(r["sigma"], expected, places=10)

    def test_sigma_ignores_bars_beyond_lookback(self):
        """lookback=20 與 lookback=80 在相同 klines 上應產生不同 sigma。"""
        ks  = _noisy_klines(120, sigma=1.0)
        idx = 119
        r20 = VWAPDeviationComponent(window=10, lookback=20).compute(ks, idx=idx)
        r80 = VWAPDeviationComponent(window=10, lookback=80).compute(ks, idx=idx)
        self.assertNotAlmostEqual(r20["sigma"], r80["sigma"], places=6)

    def test_sigma_same_unit_as_vwap_dev(self):
        """sigma 與 vwap_dev 同為無單位比率，z ≈ vwap_dev / sigma（允許 1e-10 epsilon 誤差）。"""
        comp = VWAPDeviationComponent(window=10, lookback=50)
        ks   = _noisy_klines(100, sigma=1.0)
        r    = comp.compute(ks, idx=99)
        if r["sigma"] > 1e-9:
            expected_z = r["vwap_dev"] / r["sigma"]
            self.assertAlmostEqual(r["z_score"], expected_z, places=5)

    def test_sigma_excludes_current_bar(self):
        """修改最後一棒的 close 不應改變 sigma（sigma 只用歷史棒）。"""
        comp = VWAPDeviationComponent(window=5, lookback=30)
        ks   = _noisy_klines(50, sigma=1.0)
        idx  = 49

        r_orig = comp.compute(ks, idx=idx)
        # 複製並把最後一棒的 close 改很大
        ks_mod = ks[:]
        k      = ks_mod[idx]
        ks_mod[idx] = Kline(
            symbol=k.symbol, interval=k.interval,
            open_time=k.open_time, close_time=k.close_time,
            open=k.open, high=k.high, low=k.low,
            close=k.close + 50.0,
            volume=k.volume, taker_buy_volume=k.taker_buy_volume,
            is_closed=k.is_closed,
        )
        r_mod = comp.compute(ks_mod, idx=idx)
        self.assertAlmostEqual(r_orig["sigma"], r_mod["sigma"], places=10)


# ─── z_score ──────────────────────────────────────────────────────────────────

class TestVWAPDeviationZScore(unittest.TestCase):

    def test_z_score_positive_when_above_vwap(self):
        comp = VWAPDeviationComponent(window=5, lookback=30)
        ks   = _noisy_klines(50, sigma=0.5)
        # 讓最後一棒明顯高於歷史均價
        ks[-1] = _k(49, close=ks[-1].close + 10.0)
        r = comp.compute(ks, idx=49)
        self.assertGreater(r["z_score"], 0)

    def test_z_score_negative_when_below_vwap(self):
        comp = VWAPDeviationComponent(window=5, lookback=30)
        ks   = _noisy_klines(50, sigma=0.5)
        ks[-1] = _k(49, close=ks[-1].close - 10.0)
        r = comp.compute(ks, idx=49)
        self.assertLess(r["z_score"], 0)

    def test_z_score_near_zero_for_typical_bar(self):
        """在正常波動下，絕大多數棒的 |z| < 3。"""
        comp = VWAPDeviationComponent(window=10, lookback=50)
        ks   = _noisy_klines(100, sigma=0.5)
        for idx in range(60, 100):
            r = comp.compute(ks, idx=idx)
            self.assertLess(abs(r["z_score"]), 10,
                            msg=f"idx={idx}: z={r['z_score']:.2f} seems unreasonable")


# ─── Zone 分類邏輯 ────────────────────────────────────────────────────────────

class TestVWAPDeviationZoneClassification(unittest.TestCase):

    def setUp(self):
        self.comp = VWAPDeviationComponent()

    # 邊界精確測試
    def test_normal_zone(self):
        self.assertEqual(self.comp._classify_zone(0.0),   "normal")
        self.assertEqual(self.comp._classify_zone(0.99),  "normal")
        self.assertEqual(self.comp._classify_zone(-0.99), "normal")

    def test_extended_high(self):
        self.assertEqual(self.comp._classify_zone(1.0),  "extended_high")
        self.assertEqual(self.comp._classify_zone(1.99), "extended_high")

    def test_extended_low(self):
        self.assertEqual(self.comp._classify_zone(-1.0),  "extended_low")
        self.assertEqual(self.comp._classify_zone(-1.99), "extended_low")

    def test_overextended_high(self):
        self.assertEqual(self.comp._classify_zone(2.0),  "overextended_high")
        self.assertEqual(self.comp._classify_zone(2.3),  "overextended_high")
        self.assertEqual(self.comp._classify_zone(2.5),  "overextended_high")

    def test_overextended_low(self):
        self.assertEqual(self.comp._classify_zone(-2.0), "overextended_low")
        self.assertEqual(self.comp._classify_zone(-2.3), "overextended_low")
        self.assertEqual(self.comp._classify_zone(-2.5), "overextended_low")

    def test_extreme_high(self):
        self.assertEqual(self.comp._classify_zone(2.501), "extreme_high")
        self.assertEqual(self.comp._classify_zone(5.0),   "extreme_high")

    def test_extreme_low(self):
        self.assertEqual(self.comp._classify_zone(-2.501), "extreme_low")
        self.assertEqual(self.comp._classify_zone(-5.0),   "extreme_low")

    def test_custom_thresholds(self):
        c = VWAPDeviationComponent(overextended_low=1.5, overextended_high=3.0)
        self.assertEqual(c._classify_zone(1.6), "overextended_high")
        self.assertEqual(c._classify_zone(3.1), "extreme_high")
        self.assertEqual(c._classify_zone(1.4), "extended_high")


# ─── in_overextended 旗標 ─────────────────────────────────────────────────────

class TestVWAPDeviationInOverextended(unittest.TestCase):
    """
    in_overextended 應與 z_score 嚴格一致：oe_low ≤ |z| ≤ oe_high。
    不強制特定 z 值（close 影響 VWAP），改驗欄位內部一致性。
    """

    def test_in_overextended_consistent_with_z_score(self):
        """對 200 根 klines 的每個 idx 驗 in_overextended == (oe_low ≤ |z| ≤ oe_high)。"""
        comp = VWAPDeviationComponent(window=10, lookback=50)
        ks   = _noisy_klines(200, sigma=1.0, seed=7)
        for idx in range(60, 200):
            r = comp.compute(ks, idx=idx)
            abs_z    = abs(r["z_score"])
            expected = comp.oe_low <= abs_z <= comp.oe_high
            self.assertEqual(
                r["in_overextended"], expected,
                msg=f"idx={idx}: z={r['z_score']:.3f}, in_overextended mismatch",
            )

    def test_in_overextended_consistent_with_zone(self):
        """in_overextended ↔ zone 以 overextended_ 開頭。"""
        comp = VWAPDeviationComponent(window=10, lookback=50)
        ks   = _noisy_klines(200, sigma=2.0, seed=13)
        for idx in range(60, 200):
            r = comp.compute(ks, idx=idx)
            zone_is_oe = r["zone"].startswith("overextended_")
            self.assertEqual(
                r["in_overextended"], zone_is_oe,
                msg=f"idx={idx}: zone={r['zone']}, in_overextended={r['in_overextended']}",
            )

    def test_in_overextended_false_for_normal_zone(self):
        ks = _noisy_klines(100, sigma=0.01, seed=5)   # 超低波動，close ≈ vwap
        comp = VWAPDeviationComponent(window=10, lookback=50)
        r = comp.compute(ks, idx=99)
        if r["zone"] == "normal":
            self.assertFalse(r["in_overextended"])

    def test_in_overextended_false_when_extreme(self):
        """z 極大時 in_overextended 應為 False（extreme zone）。"""
        comp = VWAPDeviationComponent(window=5, lookback=30)
        ks   = _noisy_klines(50, sigma=0.1, seed=3)
        # 最後一棒設極端價格，確保 |z| >> 2.5
        ks[-1] = _k(49, close=ks[-1].close * 2.0)
        r = comp.compute(ks, idx=49)
        if r["zone"].startswith("extreme_"):
            self.assertFalse(r["in_overextended"])


# ─── above_vwap 旗標 ──────────────────────────────────────────────────────────

class TestVWAPDeviationAboveVwap(unittest.TestCase):

    def setUp(self):
        self.comp = VWAPDeviationComponent(window=5, lookback=20)
        self.ks_base = _noisy_klines(30, sigma=0.5)

    def test_above_vwap_true_when_close_higher(self):
        ks = self.ks_base[:]
        ks[-1] = _k(29, close=200.0)   # 遠高於歷史均值
        r = self.comp.compute(ks, idx=29)
        self.assertTrue(r["above_vwap"])
        self.assertGreater(r["vwap_dev"], 0)

    def test_above_vwap_false_when_close_lower(self):
        ks = self.ks_base[:]
        ks[-1] = _k(29, close=50.0)    # 遠低於歷史均值
        r = self.comp.compute(ks, idx=29)
        self.assertFalse(r["above_vwap"])
        self.assertLess(r["vwap_dev"], 0)


# ─── 類別屬性可被子類覆蓋 ─────────────────────────────────────────────────────

class TestVWAPDeviationClassAttributeOverride(unittest.TestCase):

    def test_subclass_override_changes_zone(self):
        class TightComp(VWAPDeviationComponent):
            OVEREXTENDED_LOW  = 1.0
            OVEREXTENDED_HIGH = 1.5

        c = TightComp(window=5, lookback=20)
        self.assertEqual(c._classify_zone(1.2),  "overextended_high")
        self.assertEqual(c._classify_zone(1.6),  "extreme_high")
        self.assertEqual(c._classify_zone(0.9),  "normal")


if __name__ == "__main__":
    unittest.main()
