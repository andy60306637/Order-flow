from __future__ import annotations

import numpy as np
import pytest

from core.data_types import Kline
from strategies.pipeline import MarketVolatilityRegimeComponent
from strategies.pipeline.context import PipelineContext
from strategies.pipeline.stages import RegimeStage


def _kline(i: int, close: float = 100.0) -> Kline:
    open_time = 1_700_000_000_000 + i * 60_000
    return Kline(
        symbol="BTCUSDT",
        interval="1m",
        open_time=open_time,
        close_time=open_time + 59_999,
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=10.0,
        taker_buy_volume=5.0,
        is_closed=True,
    )


def _klines(n: int = 220) -> list[Kline]:
    return [_kline(i, 100.0 + i * 0.01) for i in range(n)]


class _StubMarketVolatilityRegimeComponent(MarketVolatilityRegimeComponent):
    def __init__(
        self,
        *,
        rv60_pct: float,
        atr10: float,
        atr60: float,
        er30: float,
        adx14: float,
        bb_width_pct: float,
    ) -> None:
        super().__init__()
        self._values = {
            "rv60_pct": rv60_pct,
            "atr10": atr10,
            "atr60": atr60,
            "er30": er30,
            "adx14": adx14,
            "bb_width_pct": bb_width_pct,
        }

    def _rv_percentile(self, closes: np.ndarray, period: int, lookback: int) -> float:
        return self._values["rv60_pct"]

    def _wilder_atr(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        period: int,
    ) -> float:
        return self._values["atr10"] if period == self.atr_short else self._values["atr60"]

    def _efficiency_ratio(self, closes: np.ndarray, period: int) -> float:
        return self._values["er30"]

    def _adx(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        period: int,
    ) -> float:
        return self._values["adx14"]

    def _bb_width_percentile(self, closes: np.ndarray, bb_period: int, lookback: int) -> float:
        return self._values["bb_width_pct"]


def _compute_stub(**values: float) -> dict:
    klines = _klines()
    return _StubMarketVolatilityRegimeComponent(**values).compute(klines, len(klines) - 1)


def test_package_exports_market_volatility_regime_component() -> None:
    component = MarketVolatilityRegimeComponent()

    assert component.component_id == "market_vol_regime"
    assert component.dimension == "market_vol_regime"


def test_insufficient_history_returns_neutral_schema() -> None:
    result = MarketVolatilityRegimeComponent().compute(_klines(10), 9)

    assert result == {
        "label": "NEUTRAL",
        "regime": "NEUTRAL",
        "rv60_pct": 50.0,
        "atr10_atr60": 1.0,
        "er30": 0.5,
        "adx14": 0.0,
        "bb_width_pct": 50.0,
        "atr10": 0.0,
        "atr60": 0.0,
    }


def test_classifies_mean_reversion() -> None:
    result = _compute_stub(
        rv60_pct=50.0,
        atr10=1.0,
        atr60=1.0,
        er30=0.20,
        adx14=20.0,
        bb_width_pct=50.0,
    )

    assert result["label"] == "MEAN_REVERSION"
    assert result["atr10_atr60"] == pytest.approx(1.0)


def test_classifies_breakout_trend() -> None:
    result = _compute_stub(
        rv60_pct=70.0,
        atr10=1.4,
        atr60=1.0,
        er30=0.50,
        adx14=30.0,
        bb_width_pct=60.0,
    )

    assert result["label"] == "BREAKOUT_TREND"


def test_classifies_chaotic_high_vol() -> None:
    result = _compute_stub(
        rv60_pct=90.0,
        atr10=1.6,
        atr60=1.0,
        er30=0.20,
        adx14=30.0,
        bb_width_pct=80.0,
    )

    assert result["label"] == "CHAOTIC_HIGH_VOL"


def test_classifies_compression_wait() -> None:
    result = _compute_stub(
        rv60_pct=20.0,
        atr10=1.3,
        atr60=1.0,
        er30=0.50,
        adx14=30.0,
        bb_width_pct=10.0,
    )

    assert result["label"] == "COMPRESSION_WAIT"


def test_classifies_neutral() -> None:
    result = _compute_stub(
        rv60_pct=70.0,
        atr10=1.1,
        atr60=1.0,
        er30=0.35,
        adx14=20.0,
        bb_width_pct=50.0,
    )

    assert result["label"] == "NEUTRAL"


def test_regime_stage_uses_market_volatility_dimension() -> None:
    klines = _klines()
    ctx = PipelineContext(klines=klines, idx=len(klines) - 1, equity=1_000.0)
    component = _StubMarketVolatilityRegimeComponent(
        rv60_pct=70.0,
        atr10=1.4,
        atr60=1.0,
        er30=0.50,
        adx14=30.0,
        bb_width_pct=60.0,
    )

    result = RegimeStage(
        [component],
        {"market_vol_regime": ["BREAKOUT_TREND"]},
    ).process(ctx)

    assert result is ctx
    assert ctx.regime is None
    assert ctx.regime_meta["regime_dimensions"]["market_vol_regime"] == "BREAKOUT_TREND"
    assert ctx.regime_meta["market_vol_regime"]["regime"] == "BREAKOUT_TREND"
