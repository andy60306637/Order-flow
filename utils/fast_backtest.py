"""
快速策略回測工具 (CLI Based)
==========================
支援 K-Line 與 Tick 級別回測，邏輯與 UI 引擎完全一致。

用法示例：
  python utils/fast_backtest.py --strategy "Wick Reversal 1m v4" --mode tick --start 2026-01-01 --end 2026-02-01 --fee 0.00032
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestConfig, simulate_trades, _resolve_fee_rate
from core import kline_cache, tick_cache
from core.data_paths import set_data_root_override
from core.tick_cache import build_bar_map, build_tick_slice_accessor
from strategies import STRATEGY_REGISTRY
from utils.tick_data_backtest import _build_klines_from_ticks

def _dt_to_ms(s: str) -> int:
    try:
        return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        # Fallback for YYYY-MM-DD
        return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

def _ms_to_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

def print_table(title: str, data: dict[str, Any]):
    print(f"\n=== {title} ===")
    max_k = max(len(k) for k in data.keys())
    for k, v in data.items():
        if isinstance(v, float):
            v_str = f"{v:.4f}"
            if "pct" in k or "rate" in k or "win" in k:
                v_str = f"{v:.2f}%"
        else:
            v_str = str(v)
        print(f"  {k:<{max_k}} : {v_str}")

def main():
    parser = argparse.ArgumentParser(description="Unified Fast Backtester (CLI)")
    parser.add_argument("--symbol", default="BTCUSDT", help="Symbol, e.g. BTCUSDT")
    parser.add_argument("--strategy", required=True, help="Strategy name from registry")
    parser.add_argument("--interval", default="1m", help="K-line interval, e.g. 1m, 15m")
    parser.add_argument("--mode", choices=["kline", "tick"], default="tick", help="Backtest mode")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD or ISO)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD or ISO)")
    
    # 費用與設定
    parser.add_argument("--capital", type=float, default=10000.0, help="Initial capital")
    parser.add_argument("--leverage", type=int, default=20, help="Leverage")
    parser.add_argument("--fee", type=float, default=0.00032, help="Taker fee rate (e.g. 0.00032 for 0.032%)")
    parser.add_argument("--slippage", type=float, default=0.2, help="Slippage in BPS (default: 0.2)")
    parser.add_argument("--data-root", default=None, help="Override ORDERFLOW_DATA_ROOT")
    
    args = parser.parse_args()

    if args.data_root:
        set_data_root_override(args.data_root)

    # 1. 取得策略類別
    strategy_cls = STRATEGY_REGISTRY.get(args.strategy)
    if not strategy_cls:
        print(f"❌ 找不到策略: {args.strategy}. 可選: {list(STRATEGY_REGISTRY.keys())}")
        return

    start_ms = _dt_to_ms(args.start)
    end_ms = _dt_to_ms(args.end)
    symbol = args.symbol.upper()

    print(f"🚀 啟動回測: {args.strategy} | {symbol} | {args.interval} | {args.mode.upper()}")
    print(f"   區間: {_ms_to_utc(start_ms)} ~ {_ms_to_utc(end_ms)}")

    t0 = time.perf_counter()

    # 2. 載入資料
    klines = []
    tick_map = None
    
    if args.mode == "tick":
        print(f"📦 正在載入 Tick 資料 (使用 sharded 載入器)...")
        ticks = tick_cache.load_range(symbol, start_ms, end_ms)
        if ticks is None or len(ticks) == 0:
            print("❌ 載入範圍內無 Tick 資料")
            return
        print(f"   已載入 {len(ticks):,} 筆 Tick")
        
        # UI 邏輯：從 Tick 重建 K 棒以確保進場對齊
        klines = _build_klines_from_ticks(symbol, ticks, interval=args.interval)
        kline_times = [(k.open_time, k.close_time) for k in klines]
        tick_map = build_bar_map(ticks, kline_times)
    else:
        print(f"📦 正在載入 Kline 資料...")
        klines = kline_cache.load_range_as_klines(symbol, args.interval, start_ms, end_ms)
        if not klines:
            print("❌ 載入範圍內無 Kline 資料")
            return
        print(f"   已載入 {len(klines):,} 根 K 棒")

    load_time = time.perf_counter() - t0

    # 3. 執行策略信號
    print(f"🧠 正在計算策略信號...")
    strategy = strategy_cls()
    strategy.allow_bar_fallback_in_tick_mode = (args.mode == "tick")
    
    # 同步費用到策略 (如果策略支援此 Hook)
    if hasattr(strategy, "configure_backtest_costs"):
        strategy.configure_backtest_costs(args.fee, args.slippage)

    signals = strategy.on_history(klines, tick_map=tick_map)
    strat_time = time.perf_counter() - t0 - load_time

    # 4. 模擬交易
    print(f"📊 正在模擬撮合...")
    cfg = BacktestConfig(
        initial_capital=args.capital,
        leverage=args.leverage,
        fee_mode="自訂",
        custom_fee_rate=args.fee,
        slippage_bps=args.slippage,
        compound=True
    )
    results = simulate_trades(signals, cfg)
    sim_time = time.perf_counter() - t0 - load_time - strat_time

    # 5. 輸出結果
    summary = {
        "交易次數": results["trades"],
        "勝率": results["win_rate"],
        "獲利因子 (PF)": results["profit_factor"],
        "總淨利 (USDT)": results["total_net_pnl"],
        "報酬率": results["total_return_pct"],
        "最大回撤": results["max_drawdown_pct"],
        "手續費總計": results["total_fees"],
        "多單次數": results["side_stats"]["long"]["trades"],
        "空單次數": results["side_stats"]["short"]["trades"],
        "資料載入耗時": f"{load_time:.2f}s",
        "信號計算耗時": f"{strat_time:.2f}s",
        "撮合模擬耗時": f"{sim_time:.2f}s",
    }
    
    print_table("回測摘要", summary)
    
    if results["trades"] > 0:
        # 顯示 Exit 分布
        exit_dist = {k: v["trades"] for k, v in results["exit_stats"].items() if v["trades"] > 0}
        print_table("出場類型分布", exit_dist)

    print(f"\n✨ 回測完成，總耗時: {time.perf_counter() - t0:.2f}s")

if __name__ == "__main__":
    main()
