"""匯入單日 Futures aggTrades 並驗證 tick/kline 一致性。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import tick_cache
from core.tick_cache import from_zip_file
from core.data_paths import set_data_root_override
from utils.tick_data_backtest import _build_klines_from_ticks
from strategies import STRATEGY_REGISTRY

def main():
    parser = argparse.ArgumentParser(description="Test consistency of a single aggTrades zip file.")
    parser.add_argument("zip_path", help="Path to the aggTrades zip file")
    parser.add_argument("--symbol", default=None, help="Force symbol (default: parsed from filename)")
    parser.add_argument("--strategy", default="Wick Reversal 1m v4", help="Strategy to test signals")
    parser.add_argument("--data-root", default=None, help="Override ORDERFLOW_DATA_ROOT")
    parser.add_argument("--save", action="store_true", help="Save to tick_cache (CAUTION: overwrites)")
    args = parser.parse_args()

    if args.data_root:
        set_data_root_override(args.data_root)

    zip_path = Path(args.zip_path)
    if not zip_path.exists():
        print(f"❌ 找不到檔案: {zip_path}")
        return

    # 解析 symbol
    symbol = args.symbol
    if not symbol:
        symbol = zip_path.name.split("-aggTrades-")[0].upper()

    print(f"📦 正在載入 {zip_path.name} (商品: {symbol}) ...")
    ticks = from_zip_file(zip_path)
    print(f"  筆數: {len(ticks):,}")
    print(f"  價格: {ticks[:,1].min():.1f} ~ {ticks[:,1].max():.1f}")
    print(f"  數量: {ticks[:,2].min():.4f} ~ {ticks[:,2].max():.2f}  avg={ticks[:,2].mean():.4f}")
    t0 = datetime.fromtimestamp(ticks[0, 0] / 1000, tz=timezone.utc)
    t1 = datetime.fromtimestamp(ticks[-1, 0] / 1000, tz=timezone.utc)
    print(f"  時間: {t0} ~ {t1}")

    if args.save:
        start_ms = int(ticks[:, 0].min())
        end_ms = int(ticks[:, 0].max())
        tick_cache.save_raw(symbol, ticks, start_ms, end_ms)
        print(f"  ✅ 已儲存至 tick_cache")

    # ── 從 tick 重建 kline ────────────────────────────────────────
    klines = _build_klines_from_ticks(symbol, ticks, "1m")
    print(f"\n📊 從 Tick 重建了 {len(klines)} 根 1m K 棒")

    # ── 驗證一致性 ─────────────────────────────────────────────────
    kline_times = [(k.open_time, k.close_time) for k in klines]
    tick_map = tick_cache.build_bar_map(ticks, kline_times)
    print(f"  tick_map 覆蓋率: {len(tick_map)}/{len(klines)}")

    mismatch = 0
    for k in klines:
        bar_ticks = tick_map.get(k.open_time)
        if bar_ticks is None:
            continue
        t_min = float(bar_ticks[:, 1].min())
        t_max = float(bar_ticks[:, 1].max())
        # 允許 0.01 容差
        if t_min < k.low - 0.01 or t_max > k.high + 0.01:
            mismatch += 1
            if mismatch <= 3:
                dt = datetime.fromtimestamp(k.open_time / 1000, tz=timezone.utc)
                print(f"  ❌ {dt} kline[{k.low:.1f},{k.high:.1f}] tick[{t_min:.1f},{t_max:.1f}]")

    if mismatch == 0:
        print("  ✅ 所有 K 棒 OHLC 與 Tick 價格完全一致")
    else:
        print(f"  ❌ {mismatch} 根 K 棒價格不一致")

    # ── 跑策略看看有沒有信號 ──────────────────────────────────────
    strategy_cls = STRATEGY_REGISTRY.get(args.strategy)
    if strategy_cls:
        strategy = strategy_cls()
        signals = strategy.on_history(klines, tick_map=tick_map)

        k0_count = sum(1 for s in signals if "k0" in s.signal_type)
        entry_count = sum(1 for s in signals if "entry" in s.signal_type)
        exit_count = sum(1 for s in signals if "exit" in s.signal_type)
        print(f"\n🎯 策略信號 ({args.strategy}): k0={k0_count}, entry={entry_count}, exit={exit_count}")

        # 檢查 entry fill_price 漂移
        entry_sigs = [s for s in signals if "entry" in s.signal_type]
        kline_by_ot = {k.open_time: k for k in klines}
        for es in entry_sigs[:5]:
            k = kline_by_ot.get(es.open_time)
            fill = es.fill_price or es.price
            drift = fill - es.price
            in_bar = k and k.low - 0.01 <= fill <= k.high + 0.01
            dt = datetime.fromtimestamp(es.open_time / 1000, tz=timezone.utc)
            mark = "✅" if in_bar else "❌"
            print(f"  {dt} base={es.price:.1f} fill={fill:.1f} drift={drift:+.1f} {mark}")

if __name__ == "__main__":
    main()
