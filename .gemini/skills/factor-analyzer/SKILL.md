# Factor Analyzer Skill

This skill allows you to perform IC (Information Coefficient) testing, quantile analysis, and stability testing for research factors, and save the resulting reports.

## Execution Flow

1.  **Define Research Parameters:** Identify the target `symbol`, `interval`, `time_slice`, and the list of `factors` to analyze.
2.  **Configure Research:** Use `ResearchConfig` from `research/runner.py` to set horizons (e.g., `[1, 3, 6, 12, 24]`), quantiles, and entry lag.
3.  **Execute Analysis:** Call `run_research` to perform the calculations.
4.  **Save Reports:** Save the `ResearchResult` to a JSON file and export key metrics to CSV files in a designated report directory.
5.  **Interpret Results:** Follow the standard interpretation process (Rank IC > 0.03, t-stat > 2.0, IR > 0.5, and quantile monotonicity).

## Parameters

- `symbol`: The trading pair (e.g., "BTCUSDT").
- `interval`: K-line interval (e.g., "1m").
- `start_date`: Start date for analysis (YYYY-MM-DD).
- `end_date`: End date for analysis (YYYY-MM-DD).
- `factors`: List of factor names to analyze.
- `horizons`: Future K-line counts to predict (default: [1, 3, 6, 12, 24]).
- `use_ticks`: Boolean, whether to include tick-level factors.

## Example Report Structure
Reports should be saved in `docs/reports/factor_analysis/{symbol}_{interval}_{timestamp}/`:
- `summary.csv`: High-level factor performance.
- `metrics.csv`: Detailed IC metrics by horizon.
- `quantiles.csv`: Quantile return and win rate analysis.
- `correlations.csv`: Pairwise factor correlations.
- `full_result.json`: Complete raw data.
