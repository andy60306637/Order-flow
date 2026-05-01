---
name: fast-backtest
description: Quick strategy backtesting tool for K-line and Tick modes. Use when you need to verify strategy performance, check trade counts, or analyze win rates using CLI instead of the UI.
---

# Fast Backtest Skill

This skill allows you to run strategy backtests quickly using the project's specialized CLI tool (`utils/fast_backtest.py`). It supports both K-Line and high-precision Tick-level simulations and can generate structured reports.

## Usage

When a user asks to "test a strategy", "run a backtest", or "check performance", use the following command structure:

```bash
python utils/fast_backtest.py \
  --strategy "<STRATEGY_NAME>" \
  --mode <tick|kline> \
  --start <YYYY-MM-DD> \
  --end <YYYY-MM-DD> \
  [--symbol <SYMBOL>] \
  [--interval <INTERVAL>] \
  [--fee <FEE_RATE>] \
  [--slippage <BPS>] \
  [--capital <USDT>] \
  [--out <REPORT_JSON_PATH>] \
  [--csv <TRADE_LIST_CSV_PATH>]
```

### Parameters Guide

- **`--strategy`**: Must be a valid name from `strategies/registry.py` (e.g., "Wick Reversal 1m v4").
- **`--mode`**: 
    - `tick`: High-precision, matches UI "Tick Mode".
    - `kline`: Fast, uses OHLCV bars.
- **`--out`**: Path to save a complete JSON report (including stats).
- **`--csv`**: Path to save the trade-by-trade ledger.

## Examples

### 1. Run a Tick Backtest with Reports
"Run a tick backtest for BTCUSDT using Wick Reversal 1m v4 for last month and save the report to result.json"
```bash
python utils/fast_backtest.py --strategy "Wick Reversal 1m v4" --mode tick --start 2026-04-01 --end 2026-05-01 --out docs/reports/backtest_result.json --csv docs/reports/trade_list.csv
```

### 2. Quick Kline Test
"Quickly test SMA Cross strategy for Q1 2026"
```bash
python utils/fast_backtest.py --strategy "SMA Cross" --mode kline --start 2026-01-01 --end 2026-03-31
```

## Tips
- The JSON report generated via `--out` is compatible with other internal analysis tools.
- The CSV file via `--csv` includes entry/exit times, prices, and net PnL for each trade.
- After a run, the tool prints a summary table to the console regardless of whether files are saved.
