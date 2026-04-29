import os
import json
import pandas as pd
from datetime import datetime
from research.runner import ResearchConfig, run_research
from backtest.time_slice import TimeSlice
from research.registry import list_factors, ensure_builtin_factors

def date_to_ms(date_str):
    return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp() * 1000)

def run_factor_analysis(symbol, interval, start_date, end_date, factors=None, use_ticks=True):
    ensure_builtin_factors()
    
    start_ms = date_to_ms(start_date)
    end_ms = date_to_ms(end_date)
    
    if factors is None:
        factors = list_factors(include_tick=use_ticks)
        
    print(f"Analyzing factors: {factors}")
    
    sl = TimeSlice(segments=[(start_ms, end_ms)])
    config = ResearchConfig(
        symbol=symbol,
        interval=interval,
        slices=[sl],
        factor_names=factors,
        horizons=[1, 3, 6, 12, 24],
        use_tick_features=use_ticks
    )
    
    result = run_research(config)
    
    # Save reports
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = f"docs/reports/factor_analysis/{symbol}_{interval}_{timestamp}"
    os.makedirs(report_path, exist_ok=True)
    
    # Summary
    pd.DataFrame(result.summary).to_csv(f"{report_path}/summary.csv", index=False)
    # Metrics
    pd.DataFrame(result.metrics).to_csv(f"{report_path}/metrics.csv", index=False)
    # Quantiles
    pd.DataFrame(result.quantiles).to_csv(f"{report_path}/quantiles.csv", index=False)
    # Correlations
    pd.DataFrame(result.factor_correlations).to_csv(f"{report_path}/correlations.csv", index=False)
    
    # Full JSON
    with open(f"{report_path}/full_result.json", "w") as f:
        json.dump(result.to_dict(), f, indent=2)
        
    print(f"Analysis complete. Results saved to {report_path}")
    return report_path
