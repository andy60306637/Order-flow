"""
快速策略回測工具 (CLI Based) v1.1
==========================
支援 K-Line 與 Tick 級別回測，邏輯與 UI 引擎完全一致。
新增功能：支援 JSON 報告與 CSV 交易明細導出。

用法示例：
  python utils/fast_backtest.py --strategy "Wick Reversal 1m v4" --mode tick --start 2026-01-01 --end 2026-02-01 --out report.json
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

from backtest.engine import BacktestConfig, simulate_trades
from core import kline_cache, tick_cache
from core.data_paths import set_data_root_override
from core.tick_cache import build_bar_map
from strategies import STRATEGY_REGISTRY
from utils.tick_data_backtest import _build_klines_from_ticks

def _dt_to_ms(s: str) -> int:
    try:
        return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

def _ms_to_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

def print_table(title: str, data: dict[str, Any]):
    print(f"\n=== {title} ===")
    if not data:
        print("  (無數據)")
        return
    max_k = max(len(k) for k in data.keys())
    for k, v in data.items():
        if isinstance(v, float):
            v_str = f"{v:.4f}"
            if any(x in k for x in ["pct", "rate", "win", "報酬", "勝率", "回撤"]):
                v_str = f"{v:.2f}%"
        else:
            v_str = str(v)
        print(f"  {k:<{max_k}} : {v_str}")

def _to_builtin(obj: Any) -> Any:
    """遞迴將 NumPy 類型轉為 Python 原生類型以便 JSON 序列化"""
    if isinstance(obj, dict):
        return {k: _to_builtin(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_builtin(v) for v in obj]
    if isinstance(obj, (np.integer, np.floating)):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return _to_builtin(obj.tolist())
    return obj

def save_csv(trade_list: list[dict], path: Path):
    """導出交易明細至 CSV"""
    import csv
    if not trade_list:
        return
    
    # 過濾掉被跳過的交易
    active = [t for t in trade_list if not t.get("skipped")]
    if not active:
        return

    keys = active[0].keys()
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for t in active:
            # 轉換時間戳為可讀格式
            row = dict(t)
            if "entry_time" in row:
                row["entry_time"] = _ms_to_utc(row["entry_time"])
            if "exit_time" in row:
                row["exit_time"] = _ms_to_utc(row["exit_time"])
            writer.writerow(row)

def main():
    parser = argparse.ArgumentParser(description="Unified Fast Backtester (CLI) v1.1")
    parser.add_argument("--symbol", default="BTCUSDT", help="Symbol, e.g. BTCUSDT")
    parser.add_argument("--strategy", required=True, help="Strategy name from registry")
    parser.add_argument("--interval", default="1m", help="K-line interval, e.g. 1m, 15m")
    parser.add_argument("--mode", choices=["kline", "tick"], default="tick", help="Backtest mode")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD or ISO)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD or ISO)")
    
    # 設定
    parser.add_argument("--capital", type=float, default=10000.0, help="Initial capital")
    parser.add_argument("--leverage", type=int, default=20, help="Leverage")
    parser.add_argument("--fee", type=float, default=0.00032, help="Taker fee rate")
    parser.add_argument("--slippage", type=float, default=0.2, help="Slippage in BPS")
    parser.add_argument("--data-root", default=None, help="Override data root")
    
    # 報告導出
    parser.add_argument("--out", help="Path to save JSON report")
    parser.add_argument("--csv", help="Path to save Trade List CSV")
    
    args = parser.parse_args()

    if args.data_root:
        set_data_root_override(args.data_root)

    strategy_cls = STRATEGY_REGISTRY.get(args.strategy)
    if not strategy_cls:
        print(f"❌ 找不到策略: {args.strategy}")
        return

    start_ms = _dt_to_ms(args.start)
    end_ms = _dt_to_ms(args.end)
    symbol = args.symbol.upper()

    print(f"🚀 啟動回測: {args.strategy} | {symbol} | {args.interval} | {args.mode.upper()}")
    
    t0 = time.perf_counter()
    if args.mode == "tick":
        ticks = tick_cache.load_range(symbol, start_ms, end_ms)
        if ticks is None or len(ticks) == 0:
            print("❌ 無資料")
            return
        klines = _build_klines_from_ticks(symbol, ticks, interval=args.interval)
        tick_map = build_bar_map(ticks, [(k.open_time, k.close_time) for k in klines])
    else:
        klines = kline_cache.load_range_as_klines(symbol, args.interval, start_ms, end_ms)
        if not klines:
            print("❌ 無資料")
            return
        tick_map = None

    load_time = time.perf_counter() - t0
    strategy = strategy_cls()
    strategy.allow_bar_fallback_in_tick_mode = (args.mode == "tick")
    
    if hasattr(strategy, "configure_backtest_costs"):
        strategy.configure_backtest_costs(args.fee, args.slippage)

    signals = strategy.on_history(klines, tick_map=tick_map)
    strat_time = time.perf_counter() - t0 - load_time

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

    # 1. 輸出摘要
    summary = {
        "交易次數": results["trades"],
        "勝率": results["win_rate"],
        "獲利因子 (PF)": results["profit_factor"],
        "總淨利 (USDT)": results["total_net_pnl"],
        "報酬率": results["total_return_pct"],
        "最大回撤": results["max_drawdown_pct"],
        "資料載入": f"{load_time:.2f}s",
        "信號計算": f"{strat_time:.2f}s",
        "撮合模擬": f"{sim_time:.2f}s",
    }
    print_table("回測摘要", summary)
    
    # 2. 導出 JSON 報告
    if args.out:
        report = {
            "meta": {
                "symbol": symbol,
                "strategy": args.strategy,
                "interval": args.interval,
                "mode": args.mode,
                "start": _ms_to_utc(start_ms),
                "end": _ms_to_utc(end_ms),
                "config": {
                    "capital": args.capital,
                    "leverage": args.leverage,
                    "fee": args.fee,
                    "slippage": args.slippage
                }
            },
            "stats": {k: v for k, v in results.items() if k != "trade_list"}
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(_to_builtin(report), f, indent=2, ensure_ascii=False)
        print(f"\n📝 已儲存 JSON 報告至: {args.out}")

    # 3. 導出 CSV 明細
    if args.csv:
        save_csv(results["trade_list"], Path(args.csv))
        print(f"📄 已儲存交易明細至: {args.csv}")

    print(f"\n✨ 回測完成，總耗時: {time.perf_counter() - t0:.2f}s")

if __name__ == "__main__":
    main()
