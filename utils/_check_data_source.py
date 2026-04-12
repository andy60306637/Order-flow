"""快速比對 tick 與 kline 快取的時間/價格範圍。"""
from datetime import datetime, timezone
import numpy as np

from core import tick_cache, kline_cache
from core.data_types import Kline

data, meta = tick_cache.load_raw("BTCUSDT")
if data is not None:
    print("=== Tick 快取 ===")
    print(f"  筆數: {len(data):,}")
    t0 = datetime.fromtimestamp(meta["start_ms"] / 1000, tz=timezone.utc)
    t1 = datetime.fromtimestamp(meta["end_ms"] / 1000, tz=timezone.utc)
    print(f"  時間: {t0} ~ {t1}")
    print(f"  價格範圍: {data[:, 1].min():.1f} ~ {data[:, 1].max():.1f}")
    print(f"  前5筆價格: {data[:5, 1]}")
    print(f"  後5筆價格: {data[-5:, 1]}")

rows = kline_cache.load("BTCUSDT", "1m")
if rows:
    klines = [Kline.from_rest("BTCUSDT", "1m", r) for r in rows]
    print(f"\n=== Kline 快取 ===")
    print(f"  根數: {len(klines):,}")
    kt0 = datetime.fromtimestamp(klines[0].open_time / 1000, tz=timezone.utc)
    kt1 = datetime.fromtimestamp(klines[-1].open_time / 1000, tz=timezone.utc)
    print(f"  時間: {kt0} ~ {kt1}")
    lows = [k.low for k in klines]
    highs = [k.high for k in klines]
    print(f"  價格範圍: {min(lows):.1f} ~ {max(highs):.1f}")

    if data is not None:
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
