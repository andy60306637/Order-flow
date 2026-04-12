"""
比較 UI tick 回測路徑 vs CLI 路徑，驗證兩者輸出完全一致。

UI 路徑:  tick_cache.load_range → _build_klines_from_ticks → build_bar_map → strategy
CLI 路徑: _load_ticks_from_zip_dir → _build_klines_from_ticks → build_bar_map → strategy
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import tick_cache
from core.tick_cache import build_bar_map
from utils.tick_data_backtest import _build_klines_from_ticks, _load_ticks_from_zip_dir
from strategies.wick_reversal_v4 import WickReversalV4Strategy
from backtest.engine import BacktestConfig, simulate_trades

SINGLE_ZIP = PROJECT_ROOT / "tick_data" / "binance" / "futures" / "um" / "daily" / "aggTrades" / "BTCUSDT" / "BTCUSDT-aggTrades-2026-01-20.zip"
SYMBOL   = "BTCUSDT"
INTERVAL = "1m"

# ══════════════════════════════════════════════════════════════════
# CLI 路徑（ground truth）：直接讀單一 ZIP
# ══════════════════════════════════════════════════════════════════
print("=== CLI 路徑 ===")
from core.tick_cache import from_zip_file
cli_ticks  = from_zip_file(SINGLE_ZIP)
cli_klines = _build_klines_from_ticks(SYMBOL, cli_ticks, INTERVAL)
cli_tm     = build_bar_map(cli_ticks, [(k.open_time, k.close_time) for k in cli_klines])

cli_strategy = WickReversalV4Strategy()
cli_signals  = cli_strategy.on_history(cli_klines, tick_map=cli_tm)
cli_stats    = simulate_trades(cli_signals, BacktestConfig())

cli_entries = [(s.open_time, round(s.fill_price or s.price, 2)) for s in cli_signals if s.signal_type == "long_entry"]
print(f"  klines:  {len(cli_klines)}")
print(f"  tick_map:{len(cli_tm)}")
print(f"  entries: {len(cli_entries)}")
print(f"  trades:  {cli_stats['trades']}")
print(f"  net_pnl: {cli_stats['total_net_pnl']:.4f}")

# ══════════════════════════════════════════════════════════════════
# UI 路徑（從 tick_cache 重建）
# ══════════════════════════════════════════════════════════════════
print("\n=== UI 路徑 ===")
# tick_cache 已含本日資料（從 _test_single_day.py 存入）
cached, meta = tick_cache.load_raw(SYMBOL)
start_ms = int(cached[:, 0].min())
end_ms   = int(cached[:, 0].max())
ui_ticks  = tick_cache.load_range(SYMBOL, start_ms, end_ms)
ui_klines = _build_klines_from_ticks(SYMBOL, ui_ticks, INTERVAL)
ui_tm     = build_bar_map(ui_ticks, [(k.open_time, k.close_time) for k in ui_klines])

ui_strategy = WickReversalV4Strategy()
ui_signals  = ui_strategy.on_history(ui_klines, tick_map=ui_tm)
ui_stats    = simulate_trades(ui_signals, BacktestConfig())

ui_entries = [(s.open_time, round(s.fill_price or s.price, 2)) for s in ui_signals if s.signal_type == "long_entry"]
print(f"  klines:  {len(ui_klines)}")
print(f"  tick_map:{len(ui_tm)}")
print(f"  entries: {len(ui_entries)}")
print(f"  trades:  {ui_stats['trades']}")
print(f"  net_pnl: {ui_stats['total_net_pnl']:.4f}")

# ══════════════════════════════════════════════════════════════════
# 比對
# ══════════════════════════════════════════════════════════════════
print("\n=== 比對結果 ===")
ok = True

if len(cli_klines) != len(ui_klines):
    print(f"❌ klines 數量不一致: cli={len(cli_klines)} ui={len(ui_klines)}")
    ok = False
else:
    print(f"✅ klines 數量一致: {len(cli_klines)}")

if cli_entries != ui_entries:
    print(f"❌ entry 信號不一致!")
    for i, (c, u) in enumerate(zip(cli_entries, ui_entries)):
        if c != u:
            print(f"   [{i}] cli={c}  ui={u}")
    ok = False
else:
    print(f"✅ entry 信號完全一致: {len(cli_entries)} 筆")

if abs(cli_stats["total_net_pnl"] - ui_stats["total_net_pnl"]) > 0.001:
    print(f"❌ net_pnl 不一致: cli={cli_stats['total_net_pnl']:.4f} ui={ui_stats['total_net_pnl']:.4f}")
    ok = False
else:
    print(f"✅ net_pnl 一致: {cli_stats['total_net_pnl']:.4f}")

# entry fill_price 全部在 bar 內
all_in_bar = True
kline_by_ot = {k.open_time: k for k in ui_klines}
from datetime import datetime, timezone
for s in ui_signals:
    if s.signal_type != "long_entry":
        continue
    fill = s.fill_price or s.price
    k = kline_by_ot.get(s.open_time)
    if k and not (k.low - 0.01 <= fill <= k.high + 0.01):
        dt = datetime.fromtimestamp(s.open_time / 1000, tz=timezone.utc)
        print(f"❌ fill_price 超出 bar: {dt} fill={fill:.1f} bar=[{k.low:.1f},{k.high:.1f}]")
        all_in_bar = False

if all_in_bar:
    print(f"✅ 所有 fill_price 在 bar 範圍內")

print(f"\n{'✅ UI 路徑與 CLI 路徑完全一致' if ok else '❌ 存在差異，請確認'}")
