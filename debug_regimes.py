
import sys
from pathlib import Path
from datetime import datetime, timezone
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import tick_cache
from utils.tick_data_backtest import _build_klines_from_ticks
from strategies.pipeline.component import MarketVolatilityRegimeComponent, SessionComponent
from strategies.pipeline.mean_reversion import VWAPDeviationRegimeComponent

def main():
    symbol = "BTCUSDT"
    start_ms = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime(2026, 1, 7, tzinfo=timezone.utc).timestamp() * 1000) # Just 1 week
    
    ticks = tick_cache.load_range(symbol, start_ms, end_ms)
    klines = _build_klines_from_ticks(symbol, ticks, interval="1m")
    
    mv_comp = MarketVolatilityRegimeComponent()
    vwap_comp = VWAPDeviationRegimeComponent()
    session_comp = SessionComponent()
    
    regimes = {}
    vwap_devs = {}
    sessions = {}
    
    for i in range(200, len(klines)):
        mv = mv_comp.compute(klines, i)
        vd = vwap_comp.compute(klines, i)
        ss = session_comp.compute(klines, i)
        
        regimes[mv["label"]] = regimes.get(mv["label"], 0) + 1
        vwap_devs[vd["label"]] = vwap_devs.get(vd["label"], 0) + 1
        sessions[ss["label"]] = sessions.get(ss["label"], 0) + 1
        
    print("Market Vol Regimes:", regimes)
    print("VWAP Devs:", vwap_devs)
    print("Sessions:", sessions)

if __name__ == "__main__":
    main()
