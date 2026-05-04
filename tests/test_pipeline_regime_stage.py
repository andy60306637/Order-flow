from core.data_types import Kline
from strategies.pipeline.component import RegimeComponent, SessionComponent
from strategies.pipeline.context import PipelineContext
from strategies.pipeline.stages import RegimeStage


def _kline(open_time: int) -> Kline:
    return Kline(
        symbol="BTCUSDT",
        interval="1h",
        open_time=open_time,
        close_time=open_time + 3_600_000 - 1,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=10.0,
        taker_buy_volume=5.0,
        is_closed=True,
    )


def test_regime_stage_keeps_single_component_contract() -> None:
    ctx = PipelineContext(
        klines=[_kline(1_714_557_600_000)],
        idx=0,
        equity=1_000.0,
    )

    result = RegimeStage(RegimeComponent(), ["ranging"]).process(ctx)

    assert result is ctx
    assert ctx.regime == "ranging"
    assert ctx.regime_meta["regime"] == "ranging"
    assert ctx.regime_meta["regime_dimensions"]["trend"] == "ranging"


def test_regime_stage_filters_session_dimension() -> None:
    # 2024-05-01 10:00 UTC is within the London session.
    ctx = PipelineContext(
        klines=[_kline(1_714_557_600_000)],
        idx=0,
        equity=1_000.0,
    )

    stage = RegimeStage(
        [RegimeComponent(), SessionComponent()],
        {"trend": ["ranging"], "session": ["london"]},
    )

    result = stage.process(ctx)

    assert result is ctx
    assert ctx.regime == "ranging"
    assert ctx.regime_meta["session"] == "london"
    assert ctx.regime_meta["regime_dimensions"]["session"] == "london"


def test_regime_stage_rejects_disallowed_session_dimension() -> None:
    ctx = PipelineContext(
        klines=[_kline(1_714_557_600_000)],
        idx=0,
        equity=1_000.0,
    )

    stage = RegimeStage(
        [RegimeComponent(), SessionComponent()],
        {"trend": ["ranging"], "session": ["ny"]},
    )

    assert stage.process(ctx) is None
