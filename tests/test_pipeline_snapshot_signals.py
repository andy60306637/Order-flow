from core.data_types import Kline
from strategies.base import StrategySignal
from strategies.modules import CapitalConfig, ExitConfig, ExitModule
from strategies.modules.signal_trigger import SignalModule
from strategies.pipeline import (
    AlphaStage,
    MultiPipelineRunner,
    MultiPipelineStrategy,
    PipelineDef,
    RRStage,
    TradingPipeline,
)
from backtest.engine import BacktestConfig, simulate_trades
from ui.trade_snapshot_dialog import _collect_contexts


_MS = 60_000


def _kline(i: int, open_: float, high: float, low: float, close: float) -> Kline:
    return Kline(
        symbol="BTCUSDT",
        interval="1m",
        open_time=i * _MS,
        close_time=(i + 1) * _MS - 1,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100.0,
        taker_buy_volume=60.0,
        is_closed=True,
    )


class _OneShotLongSignal(SignalModule):
    name = "OneShotLong"

    def detect_k0(self, klines: list[Kline], idx: int) -> dict | None:
        if idx != 2:
            return None
        return {"direction": "long", "k0_idx": 1, "k0_low": klines[1].low}

    def entry_conditions(
        self,
        klines: list[Kline],
        k0_idx: int,
        k0_meta: dict,
        tick_map=None,
    ) -> StrategySignal | None:
        entry_bar = klines[k0_idx]
        return StrategySignal(
            open_time=entry_bar.open_time,
            price=entry_bar.open,
            signal_type="long_entry",
            label="raw_alpha",
            stop_price=100.0,
            fill_price=102.0,
            fill_time=entry_bar.open_time + 1234,
            meta={"raw_alpha": True},
        )


def test_pipeline_strategy_emits_snapshot_context_signals():
    klines = [
        _kline(0, 100.0, 101.0, 99.0, 100.0),
        _kline(1, 100.0, 103.0, 99.0, 101.0),
        _kline(2, 101.0, 103.0, 100.5, 102.0),
        _kline(3, 102.0, 107.0, 101.0, 106.0),
    ]
    pipeline = TradingPipeline([
        AlphaStage([_OneShotLongSignal()], mode="AND"),
        RRStage(
            exit_cfg=ExitConfig(tp_rr_ratio=2.0, use_trailing_stop=False),
            capital_cfg=CapitalConfig(max_risk_pct=1.0, leverage=20),
            min_rr=1.0,
        ),
    ])
    strategy = MultiPipelineStrategy(
        MultiPipelineRunner([PipelineDef("snapshot_pipe", pipeline)]),
        exit_mod=ExitModule(ExitConfig(tp_rr_ratio=2.0, use_trailing_stop=False)),
    )

    signals = strategy.on_history(klines)
    assert [s.signal_type for s in signals] == ["k0_long", "long_entry", "long_exit"]

    entry_sig = next(s for s in signals if s.signal_type == "long_entry")
    assert entry_sig.fill_time == klines[2].open_time + 1234
    assert entry_sig.meta["raw_alpha"] is True

    stats = simulate_trades(signals, BacktestConfig())
    contexts = _collect_contexts(signals, stats["trade_list"], klines)

    assert len(contexts) == 1
    assert contexts[0]["k0_ki"] == 1
    assert contexts[0]["entry_ki"] == 2
    assert contexts[0]["exit_ki"] == 3
    assert contexts[0]["exit_signal"].label == "TP"
