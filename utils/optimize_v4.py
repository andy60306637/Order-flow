"""
WickReversalV4 parameter grid search optimizer.
Loads tick data once, then sweeps parameter combinations.
Ranks results by profit_factor, then win_rate.
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
from core.tick_cache import _parse_agg_trades_csv_lines, build_bar_map
from strategies import STRATEGY_REGISTRY
from utils.tick_data_backtest import _build_1m_klines_from_ticks


# ── 資料載入 ────────────────────────────────────────────────────────────────

def load_ticks(tick_dir: Path, symbol: str) -> np.ndarray:
    import zipfile
    paths = sorted(tick_dir.glob(f"{symbol.upper()}*.zip"))
    if not paths:
        raise FileNotFoundError(f"no tick zips for {symbol} in {tick_dir}")
    parts = []
    for p in paths:
        with zipfile.ZipFile(p) as zf:
            csv_names = sorted(n for n in zf.namelist() if n.lower().endswith(".csv"))
            if not csv_names:
                continue
            with zf.open(csv_names[0]) as fh:
                arr = _parse_agg_trades_csv_lines(fh)
            if len(arr):
                parts.append(arr)
    if not parts:
        return np.empty((0, 4), dtype=np.float64)
    ticks = np.concatenate(parts, axis=0)
    order = np.argsort(ticks[:, 0], kind="stable")
    ticks = ticks[order]
    if len(ticks) > 1:
        diff = np.diff(ticks[:, :3], axis=0)
        keep = np.ones(len(ticks), dtype=bool)
        keep[1:] = np.any(diff != 0, axis=1)
        ticks = ticks[keep]
    return ticks


# ── 參數網格 ─────────────────────────────────────────────────────────────────

GRID = {
    "zoom_bars":                          [1, 3, 5],
    "long_delta_eff_threshold":           [0.5, 0.6, 0.7, 0.8],
    "long_vol_sma_mult":                  [1.0, 1.2, 1.5, 2.0],
    "k_delta_close_pos_min":              [0.7, 0.8, 0.9],
    "k0_min_range_sma_mult":              [0.5, 1.0, 1.5],
    "lower_wick_absorption_min_vol_ratio":[0.10, 0.15, 0.25],
    "lower_wick_absorption_delta_eff_max":[-0.2, 0.0],
    "td_consec_bars":                     [1, 2],
}

# 回測參數（固定）
BT_CFG = BacktestConfig(
    initial_capital=1650.0,
    max_loss_pct=0.02,
    leverage=20,
    fee_mode="Taker",
    custom_fee_rate=0.00032,
    slippage_bps=0.2,
    compound=True,
)

MIN_TRADES = 30   # 交易次數太少的結果不納入排名


def run_one(strategy_cls, klines, tick_map, params: dict) -> dict | None:
    strat = strategy_cls()
    for k, v in params.items():
        setattr(strat, k, v)
    signals = strat.on_history(klines, tick_map=tick_map)
    stats = simulate_trades(signals, BT_CFG)
    if stats["trades"] < MIN_TRADES:
        return None
    return stats


def monthly_pnl_sign(trade_list: list[dict]) -> dict[str, float]:
    monthly: dict[str, float] = defaultdict(float)
    for t in trade_list:
        if t.get("skipped"):
            continue
        m = datetime.fromtimestamp(
            t["entry_time"] / 1000, tz=timezone.utc
        ).strftime("%Y-%m")
        monthly[m] += t["net_pnl"]
    return monthly


def score(stats: dict) -> tuple:
    """主排序鍵：profit_factor desc, win_rate desc, max_drawdown asc"""
    return (
        stats["profit_factor"],
        stats["win_rate"],
        -stats["max_drawdown_pct"],
        stats["total_net_pnl"],
    )


def main():
    symbol = "BTCUSDT_260626"
    tick_dir = PROJECT_ROOT / "tick_data"

    print(f"Loading ticks for {symbol} ...")
    ticks = load_ticks(tick_dir, symbol)
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
        if idx % 200 == 0:
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
    print(f"TOP 20 results (from {len(results)} valid combos / {total} total)")
    print(f"{'='*72}\n")

    for rank, (params, stats) in enumerate(results[:20], 1):
        monthly = monthly_pnl_sign(stats["trade_list"])
        pos_months = sum(1 for v in monthly.values() if v > 0)
        total_months = len(monthly)
        print(f"#{rank:02d}  PF={stats['profit_factor']:.3f}  WR={stats['win_rate']:.1f}%  "
              f"PnL={stats['total_net_pnl']:.1f}  DD={stats['max_drawdown_pct']:.1f}%  "
              f"Trades={stats['trades']}  GreenMonths={pos_months}/{total_months}")
        print(f"     SL={stats['sl_count']} TP={stats['tp_count']} "
              f"TS={stats['ts_count']} TD={stats['td_count']}")
        # print params delta from defaults
        defaults = {
            "zoom_bars": 5,
            "long_delta_eff_threshold": 0.6,
            "long_vol_sma_mult": 1.2,
            "k_delta_close_pos_min": 0.8,
            "k0_min_range_sma_mult": 1.0,
            "lower_wick_absorption_min_vol_ratio": 0.15,
            "lower_wick_absorption_delta_eff_max": 0.0,
            "td_consec_bars": 2,
        }
        changed = {k: v for k, v in params.items() if v != defaults.get(k)}
        print(f"     params: {changed}")
        print()

    # ── 最佳結果詳細月報 ───────────────────────────────────────────────────
    best_params, best_stats = results[0]
    print(f"{'='*72}")
    print(f"BEST PARAMS (detailed)")
    print(f"{'='*72}")
    for k, v in best_params.items():
        print(f"  {k} = {v}")
    print()
    print(f"  trades          = {best_stats['trades']}")
    print(f"  win_rate        = {best_stats['win_rate']:.2f}%")
    print(f"  profit_factor   = {best_stats['profit_factor']:.4f}")
    print(f"  total_net_pnl   = {best_stats['total_net_pnl']:.4f}")
    print(f"  max_drawdown    = {best_stats['max_drawdown_pct']:.2f}%")
    print(f"  avg_win         = {best_stats['avg_win']:.4f}")
    print(f"  avg_loss        = {best_stats['avg_loss']:.4f}")
    print(f"  SL={best_stats['sl_count']} TP={best_stats['tp_count']} "
          f"TS={best_stats['ts_count']} TD={best_stats['td_count']}")
    print()
    print("  Monthly breakdown:")
    monthly = monthly_pnl_sign(best_stats["trade_list"])
    for m in sorted(monthly):
        sign = "+" if monthly[m] >= 0 else ""
        print(f"    {m}: {sign}{monthly[m]:.2f}")


if __name__ == "__main__":
    main()
