"""tests/test_mr_alpha_stage2_factors.py

Unit tests for the six Stage 2 long-only alpha factors:
  sweep_low_reclaim, cvd_bullish_divergence, negative_delta_absorption,
  val_reclaim_long, poc_reversion_potential, return_shock_reclaim.
"""
from __future__ import annotations

import numpy as np
import pytest

from core.data_types import Kline
from research.mr_alpha_ic_factors import (
    CvdBullishDivergenceFactor,
    NegativeDeltaAbsorptionFactor,
    PocReversionPotentialFactor,
    ReturnShockReclaimFactor,
    SweepLowReclaimFactor,
    ValReclaimLongFactor,
)

_MS_1M = 60_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _k(
    i: int,
    open_: float = 50_000.0,
    high: float = 50_100.0,
    low: float = 49_900.0,
    close: float = 50_000.0,
    volume: float = 100.0,
    tbv: float = 50.0,
) -> Kline:
    ot = i * _MS_1M
    return Kline(
        symbol="BTCUSDT",
        interval="1m",
        open_time=ot,
        close_time=ot + _MS_1M - 1,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        taker_buy_volume=tbv,
        is_closed=True,
    )


def _flat_klines(n: int, price: float = 50_000.0) -> list[Kline]:
    """Uniform bars centred on price, balanced delta."""
    return [_k(i, price, price + 100, price - 100, price) for i in range(n)]


# ---------------------------------------------------------------------------
# SweepLowReclaimFactor
# ---------------------------------------------------------------------------

class TestSweepLowReclaimFactor:
    factor = SweepLowReclaimFactor()

    def test_output_length(self):
        assert len(self.factor.compute(_flat_klines(50))) == 50

    def test_empty_input(self):
        assert len(self.factor.compute([])) == 0

    def test_no_crash_short_series(self):
        out = self.factor.compute(_flat_klines(3))
        assert len(out) == 3

    def test_no_negative_scores(self):
        out = self.factor.compute(_flat_klines(50))
        assert np.all(out[np.isfinite(out)] >= 0)

    def test_condition_met_positive_score(self):
        # 30 uniform bars, then a sweep bar that dips below the rolling low and reclaims
        klines = _flat_klines(30)                   # low = 49_900 for all
        klines.append(_k(30, 50_000, 50_100, 49_700, 50_050))  # low < 49_900, close > 49_900
        out = self.factor.compute(klines)
        assert out[-1] > 0

    def test_no_reclaim_zero_score(self):
        # Bar dips below but close does NOT reclaim — should stay 0
        klines = _flat_klines(30)
        klines.append(_k(30, 50_000, 50_100, 49_700, 49_850))  # close still < 49_900
        out = self.factor.compute(klines)
        assert out[-1] == 0.0

    def test_no_sweep_zero_score(self):
        # Bar close is above rolling low but low does NOT sweep below
        klines = _flat_klines(30)
        klines.append(_k(30, 50_000, 50_100, 49_950, 50_050))  # low=49_950 > 49_900
        out = self.factor.compute(klines)
        assert out[-1] == 0.0


# ---------------------------------------------------------------------------
# CvdBullishDivergenceFactor
# ---------------------------------------------------------------------------

class TestCvdBullishDivergenceFactor:
    factor = CvdBullishDivergenceFactor()

    def test_output_length(self):
        assert len(self.factor.compute(_flat_klines(50))) == 50

    def test_empty_input(self):
        assert len(self.factor.compute([])) == 0

    def test_no_crash_short_series(self):
        out = self.factor.compute(_flat_klines(3))
        assert len(out) == 3

    def test_non_negative_scores(self):
        out = self.factor.compute(_flat_klines(50))
        assert np.all(out[np.isfinite(out)] >= 0)

    def test_divergence_detected(self):
        # 25 flat bars (delta = 0) → previous low bar (net sell) → 5 flat bars → signal bar (net buy, lower low)
        klines = _flat_klines(25)
        klines.append(_k(25, 50_000, 50_050, 49_800, 50_000, volume=100, tbv=20))   # net sell, prev low
        klines.extend(_flat_klines(5))                                                 # filler
        klines.append(_k(31, 50_000, 50_050, 49_750, 50_000, volume=200, tbv=160))  # net buy, lower low
        out = self.factor.compute(klines)
        # CVD at bar 31 > CVD at bar 25 (previous low), low[31] <= low[25] * 1.002
        assert out[-1] > 0

    def test_no_divergence_bear_cvd_zero(self):
        # Low at same level as previous low but CVD is also lower → no divergence
        klines = _flat_klines(25)
        klines.append(_k(25, 50_000, 50_050, 49_800, 50_000, volume=100, tbv=20))   # net sell
        klines.extend(_flat_klines(5))
        klines.append(_k(31, 50_000, 50_050, 49_800, 50_000, volume=200, tbv=20))   # also net sell
        out = self.factor.compute(klines)
        assert out[-1] == 0.0


# ---------------------------------------------------------------------------
# NegativeDeltaAbsorptionFactor
# ---------------------------------------------------------------------------

class TestNegativeDeltaAbsorptionFactor:
    factor = NegativeDeltaAbsorptionFactor()

    def test_output_length(self):
        assert len(self.factor.compute(_flat_klines(100))) == 100

    def test_empty_input(self):
        assert len(self.factor.compute([])) == 0

    def test_no_crash_short_series(self):
        out = self.factor.compute(_flat_klines(3))
        assert len(out) == 3

    def test_non_negative_scores(self):
        out = self.factor.compute(_flat_klines(100))
        assert np.all(out[np.isfinite(out)] >= 0)

    def test_absorption_bar_triggers(self):
        # 60 flat bars (warm up z-score window=50), then a bar with heavy sell delta
        # but high close position (long wick, close near top) → absorption
        klines = _flat_klines(60)
        # volume=1_000, tbv=100 → delta = 200-1000 = -800 (very negative)
        # high=50_100, low=49_000, close=50_080 → close_position ≈ 0.98, lower_wick large
        klines.append(_k(60, 50_050, 50_100, 49_000, 50_080, volume=1_000, tbv=100))
        out = self.factor.compute(klines)
        assert out[-1] > 0

    def test_positive_delta_no_signal(self):
        # Strong buy delta — should NOT trigger absorption (condition delta_z < -1.0 fails)
        klines = _flat_klines(60)
        klines.append(_k(60, 50_000, 50_100, 49_900, 50_080, volume=1_000, tbv=900))
        out = self.factor.compute(klines)
        assert out[-1] == 0.0


# ---------------------------------------------------------------------------
# ValReclaimLongFactor
# ---------------------------------------------------------------------------

class TestValReclaimLongFactor:
    factor = ValReclaimLongFactor()

    def test_output_length(self):
        assert len(self.factor.compute(_flat_klines(50))) == 50

    def test_empty_input(self):
        assert len(self.factor.compute([])) == 0

    def test_no_crash_short_series(self):
        out = self.factor.compute(_flat_klines(3))
        assert len(out) == 3

    def test_non_negative_scores(self):
        out = self.factor.compute(_flat_klines(50))
        assert np.all(out[np.isfinite(out)] >= 0)

    def test_no_crash_zero_range_bars(self):
        klines = [_k(i, 50_000, 50_000, 50_000, 50_000) for i in range(30)]
        out = self.factor.compute(klines)
        assert len(out) == 30
        assert np.all(np.isfinite(out) | np.isnan(out))  # no inf

    def test_warmup_no_crash(self):
        # Fewer bars than WINDOW=20 should not crash
        klines = _flat_klines(10)
        out = self.factor.compute(klines)
        assert len(out) == 10


# ---------------------------------------------------------------------------
# PocReversionPotentialFactor
# ---------------------------------------------------------------------------

class TestPocReversionPotentialFactor:
    factor = PocReversionPotentialFactor()

    def test_output_length(self):
        assert len(self.factor.compute(_flat_klines(50))) == 50

    def test_empty_input(self):
        assert len(self.factor.compute([])) == 0

    def test_no_crash_short_series(self):
        out = self.factor.compute(_flat_klines(3))
        assert len(out) == 3

    def test_non_negative_scores(self):
        out = self.factor.compute(_flat_klines(50))
        assert np.all(out[np.isfinite(out)] >= 0)

    def test_clipped_at_max(self):
        out = self.factor.compute(_flat_klines(50))
        assert np.all(out[np.isfinite(out)] <= self.factor.MAX_DISTANCE_ATR)

    def test_poc_above_close_positive(self):
        # Create a bar well below the volume cluster so POC > close
        klines = _flat_klines(20)            # all around 50_000
        klines.append(_k(20, 48_000, 48_100, 47_900, 48_000))  # close much below cluster
        out = self.factor.compute(klines)
        # POC should be around 50_000; close=48_000 → (50_000-48_000)/ATR > 0
        assert out[-1] > 0

    def test_poc_below_close_zero(self):
        # Create a bar well above volume cluster so POC < close → clipped to 0
        klines = _flat_klines(20)
        klines.append(_k(20, 52_000, 52_100, 51_900, 52_000))  # close above cluster
        out = self.factor.compute(klines)
        assert out[-1] == 0.0


# ---------------------------------------------------------------------------
# ReturnShockReclaimFactor
# ---------------------------------------------------------------------------

class TestReturnShockReclaimFactor:
    factor = ReturnShockReclaimFactor()

    def test_output_length(self):
        assert len(self.factor.compute(_flat_klines(150))) == 150

    def test_empty_input(self):
        assert len(self.factor.compute([])) == 0

    def test_no_crash_short_series(self):
        out = self.factor.compute(_flat_klines(3))
        assert len(out) == 3

    def test_non_negative_scores(self):
        out = self.factor.compute(_flat_klines(150))
        assert np.all(out[np.isfinite(out)] >= 0)

    def test_shock_reclaim_triggers(self):
        # Rising market then a crash bar that closes near top (long lower wick)
        klines = [_k(i, 50_000 + i * 10, 50_100 + i * 10, 49_900 + i * 10, 50_000 + i * 10)
                  for i in range(110)]
        # Crash bar: close[100]=51_000; this bar close=48_000 → ret_10 ≈ -5.9%
        # But close_position high: high=51_200, low=47_000, close=51_000 → pos ≈ 0.95
        klines.append(_k(110, 51_100, 51_200, 47_000, 51_000, volume=500, tbv=300))
        out = self.factor.compute(klines)
        assert out[-1] > 0

    def test_mild_drop_no_trigger(self):
        # Very small drop, z-score not extreme enough
        klines = _flat_klines(150)
        out = self.factor.compute(klines)
        # Flat returns → z-score undefined or near 0 → no signal
        assert np.all(out[np.isfinite(out)] == 0.0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
