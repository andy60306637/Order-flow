import unittest
import sys
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import kline_cache, tick_cache
from backtest.engine import simulate_trades, BacktestConfig
from strategies.pipeline import (
    TradingPipeline, PipelineDef, MultiPipelineRunner, MultiPipelineStrategy,
    RegimeComponent, ATRComponent, SessionComponent,
    RegimeStage, AlphaStage, RRStage, FeeStage,
)
from strategies.modules import ExitConfig, CapitalConfig
from strategies.modules.signal_trigger import StrategySignalModule
from strategies.wick_reversal_v4 import WickReversalV4Strategy

class TestPipelineIntegration(unittest.TestCase):
    def test_pipeline_tick_backtest(self):
        symbol = "BTCUSDT"
        interval = "1m"
        start_date = "2026-04-01"
        end_date = "2026-04-02"

        # 1. Load Data
        start_ms = int(datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc).timestamp() * 1000)
        end_ms = int(datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc).timestamp() * 1000)
        
        klines = kline_cache.load_range_as_klines(symbol, interval, start_ms, end_ms)
        self.assertTrue(len(klines) > 0, "Should have loaded klines")

        ticks = tick_cache.load_range(symbol, start_ms, end_ms)
        self.assertTrue(len(ticks) > 0, "Should have loaded ticks")
        
        kline_times = [(k.open_time, k.close_time) for k in klines]
        tick_map = tick_cache.build_bar_map(ticks, kline_times)

        # 2. Setup Pipeline
        regime_comp = RegimeComponent(ema_period=50, slope_threshold=0.0003)
        atr_comp = ATRComponent(period=14)
        session_comp = SessionComponent()
        
        wick_strategy = WickReversalV4Strategy()
        wick_strategy.long_vol_sma_period = 0
        wick_strategy.short_vol_sma_period = 0
        wick_strategy.long_k0_vol_gate = 0.0
        wick_strategy.short_k0_vol_gate = 0.0
        wick_strategy.long_delta_eff_threshold = 0.0
        wick_strategy.short_delta_eff_threshold = 0.0
        
        wick_signal = StrategySignalModule(wick_strategy)

        pipeline = TradingPipeline([
            RegimeStage(
                components=[regime_comp, session_comp],
                allowed={
                    "trend": ["trending_bull", "trending_bear", "ranging", "volatile"],
                    "session": ["asian", "london", "ny", "overlap", "off"],
                },
            ),
            AlphaStage(modules=[wick_signal], mode="AND"),
            RRStage(
                exit_cfg=ExitConfig(tp_rr_ratio=2.0),
                capital_cfg=CapitalConfig(max_risk_pct=1.0, leverage=20),
                min_rr=0.1,
                atr_component=atr_comp,
            ),
            FeeStage(taker_rate=0.0005, slippage_rate=0.0002, min_net_rr=0.1),
        ])

        defn = PipelineDef(name="test_wick_v4", pipeline=pipeline, allocation_weight=1.0)
        runner = MultiPipelineRunner(defs=[defn])
        
        strategy = MultiPipelineStrategy(
            runner=runner,
            initial_equity=10000.0
        )

        # 3. Run Backtest
        signals = strategy.on_history(klines, tick_map=tick_map)
        self.assertTrue(len(signals) > 0, "Should have generated signals")

        cfg = BacktestConfig(
            initial_capital=10000.0,
            leverage=20,
            fee_mode="自訂",
            custom_fee_rate=0.0005,
            slippage_bps=2.0,
            compound=True
        )
        results = simulate_trades(signals, cfg)
        trades = results["trade_list"]
        active_trades = [t for t in trades if not t.get("skipped")]
        
        self.assertTrue(len(active_trades) > 0, "Should have completed active trades")
        print(f"\n[Test] Completed {len(active_trades)} active trades using Pipeline in tick mode.")

if __name__ == "__main__":
    unittest.main()
