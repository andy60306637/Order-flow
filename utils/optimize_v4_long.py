"""
WickReversalV4 LONG parameter grid search optimizer.
Optimized for the user's specific constraints.
"""
from __future__ import annotations

import itertools
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestConfig, simulate_trades
from core.tick_cache import load_raw, build_bar_map
from strategies import STRATEGY_REGISTRY
from utils.tick_data_backtest import _build_1m_klines_from_ticks


# ── 參數網格 ─────────────────────────────────────────────────────────────────

GRID = {
    "long_k0_vol_gate":           [200.0, 300.0],
    "long_vol_sma_mult":          [1.2, 1.5, 1.8],
    "long_rr_wick_a":             [3.5, 4.0, 4.5],
    "long_rr_wick_b":             [2.0, 2.5],
    "long_rr_wick_c":             [1.5, 2.0],
    "long_delta_eff_threshold":   [0.7, 0.8, 0.9],
}

# 回測參數
BT_CFG = BacktestConfig(
    initial_capital=10000.0,
    max_loss_pct=0.02,
    leverage=20,
    fee_mode="自訂",
    custom_fee_rate=0.00032,
    slippage_bps=0.2,
    compound=False,  # 固定倉位
)

MIN_TRADES = 10


def run_one(strategy_cls, klines, tick_map, params: dict) -> dict | None:
    strat = strategy_cls()
    strat.enable_long = True
    strat.enable_short = False
    # Static parameters from best run
    strat.long_sl_offset = 10.0
    strat.long_min_fee_cover_ratio = 2.0
    strat.long_zoom_bars = 1
    
    for k, v in params.items():
        setattr(strat, k, v)
    
    # Sync internal fee rates for consistency if needed, though simulate_trades uses BT_CFG
    strat.taker_fee_rate = BT_CFG.custom_fee_rate
    strat.slippage_rate = BT_CFG.slippage_bps * 1e-4

    signals = strat.on_history(klines, tick_map=tick_map)
    stats = simulate_trades(signals, BT_CFG)
    if stats["trades"] < MIN_TRADES:
        return None
    return stats


def score(stats: dict) -> tuple:
    return (
        stats["profit_factor"],
        stats["win_rate"],
        -stats["max_drawdown_pct"],
        stats["total_net_pnl"],
    )


def main():
    symbol = "BTCUSDT"
    
    print(f"Loading ticks for {symbol} from cache...")
    ticks, meta = load_raw(symbol)
    if ticks is None:
        print("Error: No tick data found in cache.")
        return

    klines = _build_1m_klines_from_ticks(symbol, ticks)
    tick_map = build_bar_map(ticks, [(k.open_time, k.close_time) for k in klines])
    print(f"bars={len(klines)} tick_coverage={len(tick_map)}/{len(klines)}\n")

    strategy_cls = STRATEGY_REGISTRY["Wick Reversal 1m v4"]

    keys = list(GRID.keys())
    values = list(GRID.values())
    total = 1
    for v in values:
        total *= len(v)
    print(f"Grid size: {total} combinations (min_trades={MIN_TRADES})\n")

    results = []
    for idx, combo in enumerate(itertools.product(*values), 1):
        params = dict(zip(keys, combo))
        if idx % 100 == 0:
            print(f"  [{idx}/{total}] running ...")
        stats = run_one(strategy_cls, klines, tick_map, params)
        if stats is None:
            continue
        results.append((params, stats))

    if not results:
        print("No valid results (all below min_trades threshold).")
        return

    results.sort(key=lambda x: score(x[1]), reverse=True)

    print(f"\n{'='*72}")
    print(f"TOP 10 results (from {len(results)} valid combos / {total} total)")
    print(f"{'='*72}\n")

    for rank, (params, stats) in enumerate(results[:10], 1):
        print(f"#{rank:02d}  PF={stats['profit_factor']:.3f}  WR={stats['win_rate']:.1f}%  "
              f"PnL={stats['total_net_pnl']:.1f}  DD={stats['max_drawdown_pct']:.1f}%  "
              f"Trades={stats['trades']}")
        print(f"     params: {params}")
        print()

    # Best result detail
    best_params, best_stats = results[0]
    print(f"BEST PARAMS Detail:")
    for k, v in best_params.items():
        print(f"  {k} = {v}")


if __name__ == "__main__":
    main()
