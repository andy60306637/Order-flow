import os
import json
import pandas as pd
import numpy as np
from datetime import datetime
from research.runner import ResearchConfig, run_research
from backtest.time_slice import TimeSlice
from research.registry import ensure_builtin_factors

def date_to_ms(date_str):
    return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp() * 1000)

def run_task():
    ensure_builtin_factors()
    
    symbol = "BTCUSDT"
    interval = "1m"
    start_date = "2023-01-01"
    end_date = "2023-06-30"
    
    start_ms = date_to_ms(start_date)
    end_ms = date_to_ms(end_date)
    
    factors = [
        "body_position_ratio", 
        "lower_wick_to_body_ratio", 
        "upper_wick_to_body_ratio",
        "volume_z_score", 
        "lower_wick_delta_eff",
        "upper_wick_delta_eff",
        "lower_wick_volume_ratio",
        "upper_wick_volume_ratio",
        "breakout_cum_delta_eff",
        "friction_cover_ratio"
    ]
    
    print(f"Executing IC Test for {symbol} {interval} ({start_date} to {end_date})")
    
    sl = TimeSlice(label="ResearchRange", segments=[(start_ms, end_ms)])
    config = ResearchConfig(
        symbol=symbol,
        interval=interval,
        slices=[sl],
        factor_names=factors,
        horizons=[1, 3, 6, 12, 24],
        use_tick_features=True
    )
    
    try:
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
            
        print(f"SUCCESS: Analysis complete. Results saved to {report_path}")
        
        # Print a short summary to terminal
        print("\n--- Factor Performance Summary (Top 5 by Oriented Rank IC) ---")
        df_summary = pd.DataFrame(result.summary).sort_values("oriented_rank_ic", ascending=False)
        print(df_summary[["factor", "side", "best_horizon", "oriented_rank_ic", "ic_ir"]].head(5).to_string(index=False))
        
    except Exception as e:
        print(f"ERROR: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    run_task()
