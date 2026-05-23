
import pytest
from unittest.mock import MagicMock
from strategies.pipeline.mean_reversion import EntryManagementStage, PipelineContext
from core.data_types import Kline

def test_entry_management_min_stop_pct_filtering():
    # Setup stage with 0.15% min_stop_pct
    stage = EntryManagementStage(min_stop_pct=0.0015, max_sl_pct=0.03)
    
    # Mock context
    ctx = PipelineContext(
        idx=100,
        equity=10000.0,
        klines=[Kline(
            symbol="BTCUSDT",
            interval="1m",
            open_time=0,
            close_time=60000,
            open=10000,
            high=10100,
            low=9990,
            close=10050,
            volume=100,
            taker_buy_volume=50,
            is_closed=True
        )] * 101,
        shared=MagicMock()
    )
    ctx.entry_price = 10000
    # signal bar is the one at k0_idx
    ctx.alpha_meta = {
        "k0_meta": {"k0_idx": 100}
    }
    
    # 1. Test case: stop_pct is 0.1% (too small, should be filtered)
    # ATR will be computed. We'll mock it to return a value that results in a small stop distance.
    # raw_stop = k0.low - atr * atr_k = 9990 - 0 * 1.0 = 9990
    # cap_stop = 10000 * (1 - 0.03) = 9700
    # stop = max(9990, 9700) = 9990
    # stop_pct = (10000 - 9990) / 10000 = 0.001 (0.1%)
    # min_stop_pct = 0.0015
    # Since 0.001 < 0.0015, it should return None.
    
    ctx.shared.get_or_compute.return_value = {"atr": 0.0}
    result = stage.process(ctx)
    assert result is None
    
    # 2. Test case: stop_pct is 0.2% (enough, should pass)
    # Mock ATR to return a value that results in a 0.2% stop distance.
    # entry = 10000, stop = 9980
    # raw_stop = k0.low - atr * atr_k = 9990 - atr * 1.0 = 9980 => atr = 10
    ctx.shared.get_or_compute.return_value = {"atr": 10.0}
    result = stage.process(ctx)
    assert result is not None
    assert ctx.stop_price == 9980
    assert ctx.alpha_meta["exit_plan"]["stop_pct"] == 0.002

if __name__ == "__main__":
    pytest.main([__file__])
