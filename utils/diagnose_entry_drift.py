"""
診斷 tick 模式進場漂移問題。

檢查兩類異常：
  1. tick/kline 不一致：tick price 超出 bar OHLC 範圍
  2. entry 過度漂移：fill_price 遠離 k0_body_high

用法：
  python -m utils.diagnose_entry_drift
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

# 確保專案根在 sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import tick_cache
from core.data_types import Kline
from core import kline_cache
from utils.tick_data_backtest import _build_klines_from_ticks
from strategies.wick_reversal_v4 import WickReversalV4Strategy


def _interval_ms(interval: str) -> int:
    m = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000}
    return m.get(interval, 60_000)


def diagnose(symbol: str = "BTCUSDT", interval: str = "1m"):
    # ── 載入全部 tick ─────────────────────────────────────────────────
    data, meta = tick_cache.load_raw(symbol)
    if data is None or len(data) == 0:
        print("❌ 無 tick 資料")
        return

    # ── 從 tick 重建 kline（與 UI tick 回測模式一致）────────────────────
    klines = _build_klines_from_ticks(symbol, data, interval=interval)
    if not klines:
        print("❌ 無法從 tick 重建 kline")
        return

    kline_times = [(k.open_time, k.close_time) for k in klines]
    tick_map = tick_cache.build_bar_map(data, kline_times)
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
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(d["open_time"] / 1000, tz=timezone.utc)
            print(f"  {dt.strftime('%Y-%m-%d %H:%M')} | "
                  f"Kline [{d['kline_low']:.1f}, {d['kline_high']:.1f}] | "
                  f"Tick [{d['tick_min']:.1f}, {d['tick_max']:.1f}] | "
                  f"drift_low={d['drift_low']:.2f}, drift_high={d['drift_high']:.2f}")
        if mismatch_count > 10:
            print(f"  ... 還有 {mismatch_count - 10} 筆")

    # ══════════════════════════════════════════════════════════════════════
    # 檢查 2：策略 entry fill_price 與 k0_body_high 的偏離
    # ══════════════════════════════════════════════════════════════════════
    strategy = WickReversalV4Strategy()
    signals = strategy.on_history(klines, tick_map=tick_map)

    entry_sigs = [s for s in signals if s.signal_type == "long_entry"]
    k0_sigs = [s for s in signals if s.signal_type == "k0_long"]

    print(f"\n{'='*60}")
    print(f"檢查 2: Entry fill_price 漂移")
    print(f"{'='*60}")
    print(f"k0 信號: {len(k0_sigs)}, 進場信號: {len(entry_sigs)}")

    drift_cases = []
    for es in entry_sigs:
        base_p = es.price       # k0_body_high
        fill_p = es.fill_price  # 實際成交

        if fill_p is None:
            continue

        drift = fill_p - base_p
        drift_pct = drift / base_p * 100 if base_p > 0 else 0

        # 找到 entry bar 的 kline，檢查 fill_price 是否在 bar OHLC 內
        entry_bar = kline_by_ot.get(es.open_time)
        fill_in_bar = (
            entry_bar is not None
            and entry_bar.low <= fill_p <= entry_bar.high
        )

        drift_cases.append({
            "open_time": es.open_time,
            "k0_body_high": base_p,
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

    print(f"  平均漂移: {avg_drift:.2f} USDT ({avg_drift / drift_cases[0]['k0_body_high'] * 100:.3f}%)")
    print(f"  最大漂移: {max_drift:.2f} USDT ({max_drift / drift_cases[0]['k0_body_high'] * 100:.3f}%)")
    print(f"  fill_price 超出 bar OHLC: {out_of_bar}/{len(drift_cases)} 筆")

    # 漂移超過 100 USDT 的案例
    big_drifts = [d for d in drift_cases if abs(d["drift"]) > 100]
    if big_drifts:
        print(f"\n  ⚠️  漂移 > 100 USDT 的 {len(big_drifts)} 筆:")
        for d in big_drifts[:15]:
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(d["open_time"] / 1000, tz=timezone.utc)
            bar_info = (
                f"bar [{d['bar_low']:.1f}, {d['bar_high']:.1f}]"
                if d["bar_low"] is not None else "bar N/A"
            )
            in_bar = "✅" if d["fill_in_bar"] else "❌ 超出bar"
            print(f"    {dt.strftime('%Y-%m-%d %H:%M')} | "
                  f"k0_body_high={d['k0_body_high']:.1f} → fill={d['fill_price']:.1f} "
                  f"(+{d['drift']:.1f}, {d['drift_pct']:.2f}%) | "
                  f"{bar_info} {in_bar}")
    else:
        print("  ✅ 無大幅漂移 (> 100 USDT) 的案例")


if __name__ == "__main__":
    diagnose()
