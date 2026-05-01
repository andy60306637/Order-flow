---
name: fast-ic-backtest
description: Quick factor IC and quantile analysis tool. Use when you need to evaluate alpha factors, check IC/IR metrics, or analyze factor stability using CLI instead of the UI Research Lab.
---

# Fast IC Backtest Skill

This skill allows you to perform vectorized factor analysis (Information Coefficient, Quantile returns, IR, t-stat) quickly using the project's specialized CLI tool (`utils/fast_ic_backtest.py`). It matches the logic and reporting of the UI Research Lab.

## Usage

When a user asks to "test a factor", "check IC", "run factor analysis", or "evaluate alpha", use the following command structure:

```bash
python utils/fast_ic_backtest.py \
  --symbol <SYMBOL> \
  --start <YYYY-MM-DD> \
  --end <YYYY-MM-DD> \
  [--interval <INTERVAL>] \
  [--factors <FACTOR_NAMES>] \
  [--horizons <HORIZONS>] \
  [--quantiles <N>] \
  [--train-ratio <FRACTION>] \
  [--out <REPORT_JSON_PATH>] \
  [--pkg <EXPORT_DIR>] \
  [--md <SUMMARY_MD_PATH>]
```

### Parameters Guide

- **`--symbol`**: Trading pair (default: BTCUSDT).
- **`--factors`**: Comma-separated list of factor names (e.g., "volume_z_score,lower_wick_to_body_ratio"). If omitted, all registered factors are analyzed.
- **`--horizons`**: Forward horizons in bars to calculate returns (default: 1,3,6,12).
- **`--quantiles`**: Number of buckets for quantile analysis (default: 5).
- **`--train-ratio`**: Fraction of the range used as the in-sample (train) split (default: 0.5). Metrics are reported for both In-Sample and Out-of-Sample.
- **`--out`**: Path to save a single JSON report.
- **`--pkg`**: Directory path to save a UI-style export package (contains `full_result.json` and CSV sections like `summary.csv`, `metrics.csv`, `quantiles.csv`).
- **`--md`**: Path to save a Markdown summary report with top-performing factors.

## Examples

### 1. Evaluate specific factors and export package
"Evaluate volume_z_score and body_position_ratio for BTCUSDT from Jan to March 2026 and export all data to docs/reports/jan_march_factors"
```bash
python utils/fast_ic_backtest.py --symbol BTCUSDT --start 2026-01-01 --end 2026-03-31 --factors volume_z_score,body_position_ratio --pkg docs/reports/jan_march_factors --md docs/reports/jan_march_summary.md
```

### 2. Full Factor Scan
"Analyze all factors for BTCUSDT 15m for the last 6 months"
```bash
python utils/fast_ic_backtest.py --symbol BTCUSDT --interval 15m --start 2025-11-01 --end 2026-05-01
```

## Tips
- The tool uses a vectorized engine, making it extremely fast for scanning hundreds of factors.
- The summary table printed to the console focuses on **Out-of-Sample (OOS)** performance to ensure robustness.
- The exported CSVs in the `--pkg` directory are perfect for further analysis in Excel or Python notebooks.
