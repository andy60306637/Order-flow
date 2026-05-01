---
name: fast-backtest
description: Quick strategy backtesting tool for K-line and Tick modes. Use when you need to verify strategy performance, check trade counts, or analyze win rates using CLI instead of the UI.
---

# Fast Backtest Skill

This skill allows you to run strategy backtests quickly using the project's specialized CLI tool (`utils/fast_backtest.py`). It supports both K-Line and high-precision Tick-level simulations.

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
  [--capital <USDT>]
```

### Parameters Guide

- **`--strategy`**: Must be a valid name from `strategies/registry.py` (e.g., "Wick Reversal 1m v4", "Wick Reversal 15m v6.1").
- **`--mode`**: 
    - `tick`: High-precision, matches UI "Tick Mode". Uses sharded data.
    - `kline`: Fast, uses OHLCV bars from cache.
- **`--start` / `--end`**: Time range for the simulation.
- **`--fee`**: Taker fee rate as a decimal (e.g., `0.00032` for 0.032%).
- **`--slippage`**: Slippage in BPS (e.g., `0.2`).
- **`--interval`**: Timeframe (e.g., `1m`, `15m`). Note that v6 strategies typically require `15m`.

## Examples

### 1. Run a Tick Backtest
"Run a tick backtest for BTCUSDT using Wick Reversal 1m v4 from 2026-01-01 to 2026-02-01 with 0.032% fee"
```bash
python utils/fast_backtest.py --strategy "Wick Reversal 1m v4" --mode tick --start 2026-01-01 --end 2026-02-01 --fee 0.00032
```

### 2. Run a Fast Kline Backtest
"Quickly test SMA Cross strategy for the last month"
```bash
python utils/fast_backtest.py --strategy "SMA Cross" --mode kline --start 2026-04-01 --end 2026-05-01
```

## Tips
- Always check `strategies/registry.py` if unsure about the exact strategy name.
- For v6 strategies (v6, v6.1), ensure `--interval 15m` is used if not specified.
- The output provides a summary table and an exit type distribution.
