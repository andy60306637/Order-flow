"""快速比對 tick 與 kline 快取的時間/價格範圍。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import tick_cache, kline_cache
from core.data_types import Kline
from core.data_paths import set_data_root_override

def main():
    parser = argparse.ArgumentParser(description="Check tick and kline cache alignment.")
    parser.add_argument("--symbol", default="BTCUSDT", help="e.g. BTCUSDT")
    parser.add_argument("--interval", default="1m", help="e.g. 1m")
    parser.add_argument("--data-root", default=None, help="Override ORDERFLOW_DATA_ROOT")
    args = parser.parse_args()

    if args.data_root:
        set_data_root_override(args.data_root)

    symbol = args.symbol.upper()
    interval = args.interval

    data, meta = tick_cache.load_raw(symbol)
    if data is not None:
        print(f"=== Tick 快取 [{symbol}] ===")
        print(f"  筆數: {len(data):,}")
        t0 = datetime.fromtimestamp(meta["start_ms"] / 1000, tz=timezone.utc)
        t1 = datetime.fromtimestamp(meta["end_ms"] / 1000, tz=timezone.utc)
        print(f"  時間: {t0} ~ {t1}")
        print(f"  價格範圍: {data[:, 1].min():.1f} ~ {data[:, 1].max():.1f}")
        print(f"  前5筆價格: {data[:5, 1]}")
        print(f"  後5筆價格: {data[-5:, 1]}")
    else:
        print(f"❌ 無 Tick 快取: {symbol}")

    rows = kline_cache.load(symbol, interval)
    if rows:
        klines = [Kline.from_rest(symbol, interval, r) for r in rows]
        print(f"\n=== Kline 快取 [{symbol} {interval}] ===")
        print(f"  根數: {len(klines):,}")
        kt0 = datetime.fromtimestamp(klines[0].open_time / 1000, tz=timezone.utc)
        kt1 = datetime.fromtimestamp(klines[-1].open_time / 1000, tz=timezone.utc)
        print(f"  時間: {kt0} ~ {kt1}")
        lows = [k.low for k in klines]
        highs = [k.high for k in klines]
        print(f"  價格範圍: {min(lows):.1f} ~ {max(highs):.1f}")

        if data is not None:
            # 尋找重疊區域的一分鐘進行價格對比
            for k in klines:
                mask = (data[:, 0] >= k.open_time) & (data[:, 0] <= k.close_time)
                bar_ticks = data[mask]
                if len(bar_ticks) > 0:
                    print(f"\n=== 同一分鐘的價格對比 ===")
                    dt = datetime.fromtimestamp(k.open_time / 1000, tz=timezone.utc)
                    print(f"  時間: {dt}")
                    print(f"  Kline OHLC: O={k.open:.1f} H={k.high:.1f} L={k.low:.1f} C={k.close:.1f}")
                    print(f"  Tick 價格:  min={bar_ticks[:, 1].min():.1f}  max={bar_ticks[:, 1].max():.1f}  n={len(bar_ticks)}")
                    print(f"  差距: {bar_ticks[:, 1].min() - k.low:.1f} USDT")
                    break
    else:
        print(f"❌ 無 Kline 快取: {symbol} {interval}")

if __name__ == "__main__":
    main()
