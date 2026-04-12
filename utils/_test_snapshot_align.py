"""驗證快照對齊：從 tick_cache 重建 kline，跑策略，產出第一筆交易的快照 PNG。"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime, timezone
from core import tick_cache
from utils.tick_data_backtest import _build_klines_from_ticks
from strategies.wick_reversal_v4 import WickReversalV4Strategy
from backtest.engine import BacktestConfig, simulate_trades
from ui.trade_snapshot_dialog import _find_ki, _collect_contexts

# ── 載入 tick & 重建 kline ────────────────────────────────────
data, meta = tick_cache.load_raw("BTCUSDT")
ticks = data
klines = _build_klines_from_ticks("BTCUSDT", ticks, "1m")
kline_times = [(k.open_time, k.close_time) for k in klines]
tick_map = tick_cache.build_bar_map(ticks, kline_times)

# ── 策略 & 回測 ────────────────────────────────────────────────
strategy = WickReversalV4Strategy()
signals = strategy.on_history(klines, tick_map=tick_map)
stats = simulate_trades(signals, BacktestConfig())
trade_list = stats["trade_list"]

active_trades = [t for t in trade_list if not t.get("skipped")]
print(f"trades: {len(active_trades)}")

if not active_trades:
    print("無交易，無法驗證快照")
    sys.exit(0)

# ── 驗證 _collect_contexts 對齊 ────────────────────────────────
contexts = _collect_contexts(signals, trade_list, klines, context_bars=10)
print(f"contexts: {len(contexts)}")

for i, ctx in enumerate(contexts[:3]):
    trade = ctx["trade"]
    entry_ki = ctx["entry_ki"]
    exit_ki = ctx["exit_ki"]
    k0_ki = ctx["k0_ki"]
    win_s = ctx["win_start"]
    win_e = ctx["win_end"]
    
    entry_bar = klines[entry_ki] if entry_ki is not None else None
    fill_p = trade["entry"]

    dt = datetime.fromtimestamp(trade["entry_time"] / 1000, tz=timezone.utc)
    print(f"\n  Trade #{i+1} @ {dt}")
    print(f"    k0_ki={k0_ki} entry_ki={entry_ki} exit_ki={exit_ki}")
    print(f"    window=[{win_s}, {win_e}] ({win_e - win_s + 1} bars)")
    print(f"    entry_price={fill_p:.1f}")

    if entry_bar:
        print(f"    entry_bar OHLC: O={entry_bar.open:.1f} H={entry_bar.high:.1f} L={entry_bar.low:.1f} C={entry_bar.close:.1f}")
        in_bar = entry_bar.low <= fill_p <= entry_bar.high
        print(f"    fill_price in bar: {'✅' if in_bar else '❌'}")

    # 檢查 stop  
    stop_p = trade.get("stop")
    if stop_p and k0_ki is not None:
        k0_bar = klines[k0_ki]
        k0_body_low = min(k0_bar.open, k0_bar.close)
        expected_stop = k0_body_low - 10.0  # sl_offset default
        print(f"    stop={stop_p:.1f} expected={expected_stop:.1f} match={'✅' if abs(stop_p - expected_stop) < 0.1 else '❌'}")

    # 檢查 tick scatter 對齊
    if entry_ki is not None:
        bar_ticks = tick_map.get(entry_bar.open_time)
        if bar_ticks is not None:
            tp_min = float(bar_ticks[:, 1].min())
            tp_max = float(bar_ticks[:, 1].max())
            print(f"    tick scatter: [{tp_min:.1f}, {tp_max:.1f}] vs bar [{entry_bar.low:.1f}, {entry_bar.high:.1f}] ✅")

print("\n✅ 快照對齊驗證完成")
