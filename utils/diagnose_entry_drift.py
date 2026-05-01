"""
診斷 tick 模式進場漂移問題。

檢查兩類異常：
  1. tick/kline 不一致：tick price 超出 bar OHLC 範圍
  2. entry 過度漂移：fill_price 遠離 k0_body_high

用法：
  python -m utils.diagnose_entry_drift --symbol BTCUSDT --days 1
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys

import numpy as np

# 確保專案根在 sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import tick_cache, kline_cache
from core.data_paths import set_data_root_override
from core.tick_cache import load_meta, load_range, build_bar_map
from utils.tick_data_backtest import _build_klines_from_ticks
from strategies import STRATEGY_REGISTRY

def diagnose():
    parser = argparse.ArgumentParser(description="Diagnose entry drift and tick/kline consistency.")
    parser.add_argument("--symbol", default="BTCUSDT", help="e.g. BTCUSDT")
    parser.add_argument("--strategy", default="Wick Reversal 1m v4", help="Strategy name from registry")
    parser.add_argument("--interval", default="1m", help="e.g. 1m")
    parser.add_argument("--days", type=int, default=1, help="Number of recent days to check")
    parser.add_argument("--data-root", default=None, help="Override ORDERFLOW_DATA_ROOT")
    args = parser.parse_args()

    if args.data_root:
        set_data_root_override(args.data_root)

    symbol = args.symbol.upper()
    interval = args.interval
    
    meta = load_meta(symbol)
    if meta is None:
        print(f"❌ 無 {symbol} 的快取中繼資料")
        return

    end_ms = meta["end_ms"]
    start_ms = max(meta["start_ms"], end_ms - args.days * 24 * 3600 * 1000)
    
    print(f"📊 正在診斷 {symbol} ({args.days} 天)...")
    print(f"   範圍: {datetime.fromtimestamp(start_ms/1000, tz=timezone.utc)} ~ {datetime.fromtimestamp(end_ms/1000, tz=timezone.utc)}")

    # ── 載入指定範圍 tick ─────────────────────────────────────────────────
    data = load_range(symbol, start_ms, end_ms)
    if data is None or len(data) == 0:
        print("❌ 載入範圍內無 tick 資料")
        return

    # ── 從 tick 重建 kline（與 UI tick 回測模式一致）────────────────────
    klines = _build_klines_from_ticks(symbol, data, interval=interval)
    if not klines:
        print("❌ 無法從 tick 重建 kline")
        return

    kline_times = [(k.open_time, k.close_time) for k in klines]
    tick_map = build_bar_map(data, kline_times)
    print(f"📊 klines(from tick)={len(klines)}, tick 覆蓋={len(tick_map)}/{len(klines)}")

    # ══════════════════════════════════════════════════════════════════════
    # 檢查 1：tick price 是否超出 bar OHLC
    # ══════════════════════════════════════════════════════════════════════
    kline_by_ot = {k.open_time: k for k in klines}
    mismatch_count = 0
    mismatch_details = []

    for ot, bar_ticks in tick_map.items():
        k = kline_by_ot.get(ot)
        if k is None:
            continue
        prices = bar_ticks[:, 1]
        tick_min = float(prices.min())
        tick_max = float(prices.max())
        # 允許 0.01 USDT 容差
        if tick_min < k.low - 0.01 or tick_max > k.high + 0.01:
            mismatch_count += 1
            mismatch_details.append({
                "open_time": ot,
                "kline_low": k.low,
                "kline_high": k.high,
                "tick_min": tick_min,
                "tick_max": tick_max,
                "drift_low": k.low - tick_min,
                "drift_high": tick_max - k.high,
            })

    print(f"\n{'='*60}")
    print(f"檢查 1: Tick/Kline 價格一致性")
    print(f"{'='*60}")
    if mismatch_count == 0:
        print("✅ 所有 bar 的 tick 價格都在 OHLC 範圍內")
    else:
        print(f"⚠️  {mismatch_count} 根 bar 的 tick 價格超出 OHLC 範圍!")
        for d in mismatch_details[:10]:
            dt = datetime.fromtimestamp(d["open_time"] / 1000, tz=timezone.utc)
            print(f"  {dt.strftime('%Y-%m-%d %H:%M')} | "
                  f"Kline [{d['kline_low']:.1f}, {d['kline_high']:.1f}] | "
                  f"Tick [{d['tick_min']:.1f}, {d['tick_max']:.1f}] | "
                  f"drift_low={d['drift_low']:.2f}, drift_high={d['drift_high']:.2f}")
        if mismatch_count > 10:
            print(f"  ... 還有 {mismatch_count - 10} 筆")

    # ══════════════════════════════════════════════════════════════════════
    # 檢查 2：策略 entry fill_price 的偏離
    # ══════════════════════════════════════════════════════════════════════
    strategy_cls = STRATEGY_REGISTRY.get(args.strategy)
    if strategy_cls is None:
        print(f"❌ 找不到策略: {args.strategy}")
        return
        
    strategy = strategy_cls()
    signals = strategy.on_history(klines, tick_map=tick_map)

    entry_sigs = [s for s in signals if "entry" in s.signal_type]
    
    print(f"\n{'='*60}")
    print(f"檢查 2: Entry fill_price 漂移 (使用 {args.strategy})")
    print(f"{'='*60}")
    print(f"進場信號數: {len(entry_sigs)}")

    drift_cases = []
    for es in entry_sigs:
        base_p = es.price       # 觸發價基準
        fill_p = es.fill_price  # 實際成交

        if fill_p is None:
            continue

        drift = fill_p - base_p
        drift_pct = drift / base_p * 100 if base_p > 0 else 0

        # 找到 entry bar 的 kline，檢查 fill_price 是否在 bar OHLC 內
        entry_bar = kline_by_ot.get(es.open_time)
        fill_in_bar = (
            entry_bar is not None
            and entry_bar.low - 0.01 <= fill_p <= entry_bar.high + 0.01
        )

        drift_cases.append({
            "open_time": es.open_time,
            "trigger_price": base_p,
            "fill_price": fill_p,
            "drift": drift,
            "drift_pct": drift_pct,
            "bar_low": entry_bar.low if entry_bar else None,
            "bar_high": entry_bar.high if entry_bar else None,
            "fill_in_bar": fill_in_bar,
        })

    if not drift_cases:
        print("  無 tick 模式進場信號")
        return

    # 排序 by drift desc
    drift_cases.sort(key=lambda d: abs(d["drift"]), reverse=True)

    # 統計
    drifts = [d["drift"] for d in drift_cases]
    avg_drift = sum(drifts) / len(drifts)
    max_drift = max(drifts)
    out_of_bar = sum(1 for d in drift_cases if not d["fill_in_bar"])

    print(f"  平均漂移: {avg_drift:.2f} USDT")
    print(f"  最大漂移: {max_drift:.2f} USDT")
    print(f"  fill_price 超出 bar OHLC: {out_of_bar}/{len(drift_cases)} 筆")

    # 漂移顯著的案例
    big_drifts = [d for d in drift_cases if abs(d["drift_pct"]) > 0.1]
    if big_drifts:
        print(f"\n  ⚠️  漂移 > 0.1% 的 {len(big_drifts)} 筆:")
        for d in big_drifts[:15]:
            dt = datetime.fromtimestamp(d["open_time"] / 1000, tz=timezone.utc)
            bar_info = (
                f"bar [{d['bar_low']:.1f}, {d['bar_high']:.1f}]"
                if d["bar_low"] is not None else "bar N/A"
            )
            in_bar = "✅" if d["fill_in_bar"] else "❌ 超出bar"
            print(f"    {dt.strftime('%Y-%m-%d %H:%M')} | "
                  f"trigger={d['trigger_price']:.1f} → fill={d['fill_price']:.1f} "
                  f"({d['drift']:+.1f}, {d['drift_pct']:+.3f}%) | "
                  f"{bar_info} {in_bar}")
    else:
        print("  ✅ 無大幅漂移 (> 0.1%) 的案例")

if __name__ == "__main__":
    diagnose()
