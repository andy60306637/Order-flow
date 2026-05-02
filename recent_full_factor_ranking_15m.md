# Factor Research Audit Report
Generated at: 2026-05-02 14:00:06 UTC

## Configuration
- Symbol: `BTCUSDT`
- Interval: `15m`
- Horizons: `[1, 3, 6, 12]`
- Quantiles: `5`
- Train Ratio: `0.5`
- Analyzed Rows: `46025`

## Top Factors (OOS Oriented Rank IC)
| Factor | Group | OOS IC | OOS IR | OOS t-stat | Best Horizon |
| :--- | :--- | ---: | ---: | ---: | ---: |
| ma_trend_alignment_crossover | Price Action & Chart Patterns | 0.3109 | 0.00 | 0.00 | 1 |
| sweep_pin_bar_short | Price Action & Chart Patterns | 0.0741 | 0.00 | 0.00 | 3 |
| reversal_bar_up | Mean-Reversion & Extreme Factors | 0.0585 | 0.55 | 1.55 | 1 |
| atr_percentile_100 | Volatility & Compression Factors | 0.0465 | 0.78 | 2.34 | 12 |
| reversal_bar_down | Mean-Reversion & Extreme Factors | 0.0342 | 0.44 | 1.24 | 1 |
| volume_ma_20 | Volume & Liquidity Factors | 0.0256 | 0.47 | 1.41 | 12 |
| sweep_pin_bar_long | Price Action & Chart Patterns | 0.0238 | 0.00 | 0.00 | 1 |
| realized_vol_5m | Volatility & Compression Factors | 0.0212 | 0.59 | 1.77 | 12 |
| bb_width_percentile_100 | Volatility & Compression Factors | 0.0201 | 0.09 | 0.26 | 12 |
| volume_1m | Volume & Liquidity Factors | 0.0193 | 0.48 | 1.45 | 12 |