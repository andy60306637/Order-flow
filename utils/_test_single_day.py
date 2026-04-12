"""匯入單日 Futures aggTrades 並驗證 tick/kline 一致性。"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime, timezone
from core import tick_cache
from core.tick_cache import from_zip_file
from utils.tick_data_backtest import _build_klines_from_ticks

ZIP_PATH = PROJECT_ROOT / "tick_data" / "binance" / "futures" / "um" / "daily" / "aggTrades" / "BTCUSDT" / "BTCUSDT-aggTrades-2026-01-20.zip"

print(f"Loading {ZIP_PATH.name} ...")
ticks = from_zip_file(ZIP_PATH)
print(f"  ticks: {len(ticks):,}")
print(f"  price: {ticks[:,1].min():.1f} ~ {ticks[:,1].max():.1f}")
print(f"  qty:   {ticks[:,2].min():.4f} ~ {ticks[:,2].max():.2f}  avg={ticks[:,2].mean():.4f}")
t0 = datetime.fromtimestamp(ticks[0, 0] / 1000, tz=timezone.utc)
t1 = datetime.fromtimestamp(ticks[-1, 0] / 1000, tz=timezone.utc)
print(f"  time:  {t0} ~ {t1}")

# Save to tick_cache (覆蓋舊的 Spot 資料)
start_ms = int(ticks[:, 0].min())
end_ms = int(ticks[:, 0].max())
tick_cache.save_raw("BTCUSDT", ticks, start_ms, end_ms)
print(f"  ✅ Saved to tick_cache ({len(ticks):,} ticks)")

# ── 從 tick 重建 kline ────────────────────────────────────────
klines = _build_klines_from_ticks("BTCUSDT", ticks, "1m")
print(f"\n📊 Rebuilt {len(klines)} klines from ticks")

# ── 驗證一致性 ─────────────────────────────────────────────────
kline_times = [(k.open_time, k.close_time) for k in klines]
tick_map = tick_cache.build_bar_map(ticks, kline_times)
print(f"  tick_map coverage: {len(tick_map)}/{len(klines)}")

mismatch = 0
for k in klines:
    bar_ticks = tick_map.get(k.open_time)
    if bar_ticks is None:
        continue
    t_min = float(bar_ticks[:, 1].min())
    t_max = float(bar_ticks[:, 1].max())
    if t_min < k.low - 0.01 or t_max > k.high + 0.01:
        mismatch += 1
        if mismatch <= 3:
            dt = datetime.fromtimestamp(k.open_time / 1000, tz=timezone.utc)
            print(f"  ❌ {dt} kline[{k.low:.1f},{k.high:.1f}] tick[{t_min:.1f},{t_max:.1f}]")

if mismatch == 0:
    print("  ✅ 所有 kline OHLC 與 tick 價格完全一致")
else:
    print(f"  ❌ {mismatch} 根不一致")

# ── 跑策略看看有沒有信號 ──────────────────────────────────────
from strategies.wick_reversal_v4 import WickReversalV4Strategy
strategy = WickReversalV4Strategy()
signals = strategy.on_history(klines, tick_map=tick_map)

k0_count = sum(1 for s in signals if s.signal_type == "k0_long")
entry_count = sum(1 for s in signals if s.signal_type == "long_entry")
exit_count = sum(1 for s in signals if s.signal_type == "long_exit")
print(f"\n🎯 策略信號: k0={k0_count}, entry={entry_count}, exit={exit_count}")

# 檢查 entry fill_price 漂移
entry_sigs = [s for s in signals if s.signal_type == "long_entry"]
kline_by_ot = {k.open_time: k for k in klines}
for es in entry_sigs[:5]:
    k = kline_by_ot.get(es.open_time)
    fill = es.fill_price or es.price
    drift = fill - es.price
    in_bar = k and k.low <= fill <= k.high
    dt = datetime.fromtimestamp(es.open_time / 1000, tz=timezone.utc)
    mark = "✅" if in_bar else "❌"
    print(f"  {dt} base={es.price:.1f} fill={fill:.1f} drift={drift:.1f} {mark}")
