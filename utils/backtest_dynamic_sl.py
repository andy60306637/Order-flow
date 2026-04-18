"""
三年份 tick shard 回測比較腳本（Dynamic SL 驗證用）

執行：
  python utils/backtest_dynamic_sl.py

比較三個 shard 數據集的回測結果，驗證動態停損在不同年份的 risk 一致性。
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestConfig, simulate_trades
from core.data_types import Kline
from core.tick_cache import build_bar_map, load_range_sharded
from strategies import STRATEGY_REGISTRY

# ── Shard 設定 ─────────────────────────────────────────────────────────────

SHARDS = [
    {
        "label": "Y1 (2023-04 ~ 2024-04)",
        "symbol": "BTCUSDT_20230414_20240413",
        "start_ms": 1681430400024,
        "end_ms":   1713052799813,
    },
    {
        "label": "Y2 (2024-04 ~ 2025-04)",
        "symbol": "BTCUSDT_20240414_20250413",
        "start_ms": 1713052800028,
        "end_ms":   1744588799956,
    },
    {
        "label": "Y3 (2025-04 ~ present)",
        "symbol": "BTCUSDT",
        "start_ms": 1744588800013,
        "end_ms":   1776124799989,  # from BTCUSDT_shards.json
    },
]

STRATEGY_NAME = "Wick Reversal 1m v4"
BAR_MS = 60_000  # 1m

BACKTEST_CFG = BacktestConfig(
    initial_capital=1650.0,
    max_loss_pct=0.02,
    leverage=20,
    fee_mode="Taker",
    custom_fee_rate=0.00032,
    slippage_bps=0.2,
    funding_rate=0.0,
    maint_margin=0.005,
    compound=True,
)


# ── Kline 重建 ─────────────────────────────────────────────────────────────

def _build_klines(symbol: str, ticks: np.ndarray) -> list[Kline]:
    if len(ticks) == 0:
        return []
    buckets = (ticks[:, 0].astype(np.int64) // BAR_MS) * BAR_MS
    open_times, starts = np.unique(buckets, return_index=True)
    klines: list[Kline] = []
    for idx, ot in enumerate(open_times):
        lo = starts[idx]
        hi = starts[idx + 1] if idx + 1 < len(starts) else len(ticks)
        chunk = ticks[lo:hi]
        prices = chunk[:, 1]
        qty = chunk[:, 2]
        is_bm = chunk[:, 3] > 0.5
        klines.append(Kline(
            symbol=symbol.upper(),
            interval="1m",
            open_time=int(ot),
            close_time=int(ot + BAR_MS - 1),
            open=float(prices[0]),
            high=float(np.max(prices)),
            low=float(np.min(prices)),
            close=float(prices[-1]),
            volume=float(np.sum(qty)),
            taker_buy_volume=float(np.sum(qty[~is_bm])),
            is_closed=True,
        ))
    return klines


# ── Risk 分布統計 ──────────────────────────────────────────────────────────

def _risk_stats(trade_list: list[dict]) -> dict:
    """trade_list fields: entry, entry_stop, exit, ..."""
    if not trade_list:
        return {}
    risks = []
    for t in trade_list:
        ep = t.get("entry", 0)
        sp = t.get("entry_stop", 0)
        if ep > 0 and sp > 0:
            risks.append(abs(ep - sp) / ep)
    if not risks:
        return {}
    arr = np.array(risks)
    return {
        "n": len(arr),
        "mean_pct": float(np.mean(arr)) * 100,
        "median_pct": float(np.median(arr)) * 100,
        "p5_pct": float(np.percentile(arr, 5)) * 100,
        "p95_pct": float(np.percentile(arr, 95)) * 100,
        "std_pct": float(np.std(arr)) * 100,
    }


def _monthly_breakdown(trade_list: list[dict]) -> list[str]:
    monthly = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for t in trade_list:
        month = datetime.fromtimestamp(
            t["entry_time"] / 1000, tz=timezone.utc
        ).strftime("%Y-%m")
        monthly[month]["trades"] += 1
        monthly[month]["pnl"] += t["net_pnl"]
        if t["net_pnl"] > 0:
            monthly[month]["wins"] += 1
    lines = []
    for month in sorted(monthly):
        row = monthly[month]
        wr = row["wins"] / row["trades"] * 100 if row["trades"] > 0 else 0
        lines.append(f"  {month}: trades={row['trades']:3d}  pnl={row['pnl']:+.4f}  wr={wr:.1f}%")
    return lines


# ── 主程式 ─────────────────────────────────────────────────────────────────

def run_shard(shard: dict, strategy_cls, strategy_factory: Optional[Callable] = None) -> Optional[dict]:
    symbol = shard["symbol"]
    label  = shard["label"]
    start_ms = shard["start_ms"]
    end_ms   = shard["end_ms"]

    print(f"\n{'='*60}")
    print(f"  {label}  [{symbol}]")
    print(f"{'='*60}")

    t0 = time.perf_counter()
    ticks = load_range_sharded(symbol, start_ms, end_ms)
    if ticks is None or len(ticks) == 0:
        print(f"  [SKIP] no shard data found for {symbol}")
        return None

    # 截尾至 end_ms（Y3 end_ms 設為很大，自然以資料最後一筆為準）
    mask = ticks[:, 0] <= end_ms
    ticks = ticks[mask]

    print(f"  ticks loaded: {len(ticks):,}  ({time.perf_counter()-t0:.1f}s)")

    klines = _build_klines(symbol, ticks)
    kline_times = [(k.open_time, k.close_time) for k in klines]
    tick_map = build_bar_map(ticks, kline_times)
    print(f"  bars={len(klines)}  tick_coverage={len(tick_map)}/{len(klines)}")

    strategy = strategy_factory() if strategy_factory else strategy_cls()
    t1 = time.perf_counter()
    signals = strategy.on_history(klines, tick_map=tick_map)
    print(f"  signals={len(signals)}  ({time.perf_counter()-t1:.1f}s)")

    stats = simulate_trades(signals, BACKTEST_CFG)
    tl = stats.get("trade_list", [])

    first_bar = datetime.fromtimestamp(klines[0].open_time / 1000, tz=timezone.utc)
    last_bar  = datetime.fromtimestamp(klines[-1].open_time / 1000, tz=timezone.utc)
    print(f"  range: {first_bar.date()} → {last_bar.date()}")
    print(f"  trades={stats['trades']}  win_rate={stats['win_rate']:.3f}")
    print(f"  profit_factor={stats['profit_factor']:.4f}")
    print(f"  total_net_pnl={stats['total_net_pnl']:.4f}")
    print(f"  max_drawdown_pct={stats['max_drawdown_pct']:.4f}")

    rs = _risk_stats(tl)
    if rs:
        print(f"  risk_dist(stop_dist/entry): "
              f"mean={rs['mean_pct']:.3f}%  median={rs['median_pct']:.3f}%  "
              f"std={rs['std_pct']:.3f}%  "
              f"p5={rs['p5_pct']:.3f}%  p95={rs['p95_pct']:.3f}%")
    else:
        print(f"  risk_dist: n/a (no stop_price in trade_list)")

    print("  monthly:")
    for line in _monthly_breakdown(tl):
        print(line)

    return {"label": label, "stats": stats, "risk_stats": rs}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--regime-params", default="",
                    help="path to regime opt JSON (enables regime mode with optimized params)")
    args = ap.parse_args()

    strategy_cls = STRATEGY_REGISTRY.get(STRATEGY_NAME)
    if strategy_cls is None:
        raise SystemExit(f"strategy not found: {STRATEGY_NAME}")

    regime_params: dict | None = None
    if args.regime_params:
        with open(args.regime_params, encoding="utf-8") as f:
            data = json.load(f)
        regime_params = data.get("combined_params", {})
        print(f"Strategy: {STRATEGY_NAME}  [REGIME MODE]")
        print(f"  regime_breaks: {regime_params.get('regime_price_break_0')} / {regime_params.get('regime_price_break_1')}")
        for ri in range(3):
            rr_a = regime_params.get(f'r{ri}_long_rr_wick_a', '?')
            rr_b = regime_params.get(f'r{ri}_long_rr_wick_b', '?')
            rr_c = regime_params.get(f'r{ri}_long_rr_wick_c', '?')
            srr_a = regime_params.get(f'r{ri}_short_rr_wick_a', '?')
            print(f"  R{ri}: long_rr={rr_a}/{rr_b}/{rr_c}  short_rr_a={srr_a}")
    else:
        inst = strategy_cls()
        print(f"Strategy: {STRATEGY_NAME}  [GLOBAL MODE]")
        print(f"  long_sl_pct_floor={inst.long_sl_pct_floor}  long_sl_wick_mult={inst.long_sl_wick_mult}  long_sl_pct_cap={inst.long_sl_pct_cap}")
        print(f"  short_sl_pct_floor={inst.short_sl_pct_floor}  short_sl_wick_mult={inst.short_sl_wick_mult}  short_sl_pct_cap={inst.short_sl_pct_cap}")

    def _make_strategy():
        s = strategy_cls()
        if regime_params:
            for k, v in regime_params.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        return s

    results = []
    for shard in SHARDS:
        r = run_shard(shard, strategy_cls, strategy_factory=_make_strategy)
        if r:
            results.append(r)

    print(f"\n{'='*60}")
    print("  CROSS-YEAR SUMMARY")
    print(f"{'='*60}")
    for r in results:
        s = r["stats"]
        rs = r["risk_stats"]
        risk_info = f"risk_mean={rs['mean_pct']:.3f}%  risk_std={rs['std_pct']:.3f}%" if rs else "risk=n/a"
        print(f"  {r['label']}")
        print(f"    trades={s['trades']}  wr={s['win_rate']:.3f}  PF={s['profit_factor']:.3f}  "
              f"pnl={s['total_net_pnl']:.4f}  dd={s['max_drawdown_pct']:.4f}")
        print(f"    {risk_info}")


if __name__ == "__main__":
    main()
