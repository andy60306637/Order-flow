import os
import json
import pandas as pd
from datetime import datetime
from research.runner import ResearchConfig, run_research
from backtest.time_slice import TimeSlice
from research.registry import list_factors

def run_ic_test(symbol, interval, start_ms, end_ms, factor_names=None, horizons=[1, 3, 6, 12, 24], use_ticks=True):
    if factor_names is None:
        factor_names = list_factors(include_tick=use_ticks)
    
    print(f"Starting IC Test for {symbol} {interval} from {start_ms} to {end_ms}")
    print(f"Factors: {factor_names}")
    
    sl = TimeSlice(segments=[(start_ms, end_ms)])
    config = ResearchConfig(
        symbol=symbol,
        interval=interval,
        slices=[sl],
        factor_names=factor_names,
        horizons=horizons,
        use_tick_features=use_ticks
    )
    
    result = run_research(config)
    
    # Create report directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = f"docs/reports/factor_analysis/{symbol}_{interval}_{timestamp}"
    os.makedirs(report_dir, exist_ok=True)
    
    # Save full result as JSON
    with open(os.path.join(report_dir, "full_result.json"), "w") as f:
        json.dump(result.to_dict(), f, indent=2)
    
    # Save key tables as CSV
    pd.DataFrame(result.summary).to_csv(os.path.join(report_dir, "summary.csv"), index=False)
    pd.DataFrame(result.metrics).to_csv(os.path.join(report_dir, "metrics.csv"), index=False)
    pd.DataFrame(result.quantiles).to_csv(os.path.join(report_dir, "quantiles.csv"), index=False)
    pd.DataFrame(result.factor_correlations).to_csv(os.path.join(report_dir, "correlations.csv"), index=False)
    
    print(f"Report saved to: {report_dir}")
    return report_dir

if __name__ == "__main__":
    # Example usage for testing
    import time
    # Last 3 days approximately
    end = int(time.time() * 1000)
    start = end - (3 * 24 * 60 * 60 * 1000)
    # run_ic_test("BTCUSDT", "1m", start, end)
