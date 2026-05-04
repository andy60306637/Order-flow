# Factor Research Audit Report
Generated at: 2026-05-02 18:17:45 UTC

## Configuration
- Symbol: `BTCUSDT`
- Interval: `15m`
- Horizons: `[1, 3, 6, 12]`
- Quantiles: `5`
- Train Ratio: `0.5`
- Analyzed Rows: `186281`

## Top Factors (OOS Oriented Rank IC)
| Factor | Group | OOS IC | OOS IR | OOS t-stat | Best Horizon |
| :--- | :--- | ---: | ---: | ---: | ---: |
| adx_15m | Regime & Condition Filters | 0.0206 | 0.41 | 2.33 | 12 |
| session_us_flag | Regime & Condition Filters | 0.0117 | 0.31 | 1.79 | 3 |
| chop_index_15m | Regime & Condition Filters | 0.0069 | 0.24 | 1.39 | 12 |
| volatility_zscore_15m_20 | Regime & Condition Filters | 0.0066 | 0.20 | 1.16 | 12 |
| session_asia_flag | Regime & Condition Filters | 0.0041 | 0.16 | 0.92 | 12 |
| hh_hl_structure_15m | Regime & Condition Filters | 0.0000 | 0.00 | 0.00 | 1 |
| ll_lh_structure_15m | Regime & Condition Filters | -0.0000 | 0.00 | 0.00 | 1 |
| volume_zscore_15m_20 | Regime & Condition Filters | -0.0001 | -0.03 | -0.18 | 1 |
| session_london_flag | Regime & Condition Filters | -0.0019 | -0.13 | -0.75 | 1 |
| weekend_flag | Regime & Condition Filters | -0.0041 | -0.30 | -1.73 | 1 |