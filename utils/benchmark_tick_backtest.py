from __future__ import annotations

import argparse
import ctypes
import gc
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestConfig, simulate_trades
from core.tick_cache import (
    build_bar_map,
    build_tick_slice_accessor,
    load_meta,
    load_range,
    load_raw,
)
from strategies import STRATEGY_REGISTRY
from utils.tick_data_backtest import _build_klines_from_ticks

_DAY_MS = 24 * 60 * 60 * 1000


def _rss_bytes() -> int | None:
    if os.name == "nt":
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        psapi = ctypes.WinDLL("Psapi.dll", use_last_error=True)
        kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        ok = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        )
        if ok:
            return int(counters.WorkingSetSize)
        return None

    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return int(rss)
        return int(rss * 1024)
    except Exception:
        return None


def _fmt_rss(rss_bytes: int | None) -> str:
    if rss_bytes is None:
        return "n/a"
    return f"{rss_bytes / 1_048_576:.1f} MB"


def _slice_period(ticks, meta: dict[str, int] | None, days: int):
    if len(ticks) == 0:
        return ticks
    end_ms = int(meta["end_ms"]) if meta else int(ticks[-1, 0])
    start_ms = max(int(ticks[0, 0]), end_ms - days * _DAY_MS)
    mask = (ticks[:, 0] >= start_ms) & (ticks[:, 0] <= end_ms)
    return ticks[mask]


def _stage_timing(fn):
    rss_before = _rss_bytes()
    started = time.perf_counter()
    value = fn()
    elapsed = time.perf_counter() - started
    rss_after = _rss_bytes()
    return value, elapsed, rss_before, rss_after


def _run_once(args, days: int, tick_access: str) -> dict[str, Any]:
    gc.collect()

    meta = load_meta(args.symbol)
    if meta is None:
        raise SystemExit(f"no tick cache metadata found for {args.symbol}")

    end_ms = int(meta["end_ms"])
    start_ms = max(int(meta["start_ms"]), end_ms - days * _DAY_MS)
    if args.load_mode == "legacy":
        (loaded, raw_meta), load_time, _, rss_after_load = _stage_timing(
            lambda: load_raw(args.symbol)
        )
        if loaded is None or len(loaded) == 0:
            raise SystemExit(f"no cached tick data found for {args.symbol}")
        ticks, slice_time, _, rss_after_slice = _stage_timing(
            lambda: _slice_period(loaded, raw_meta, days)
        )
    else:
        ticks, load_time, _, rss_after_load = _stage_timing(
            lambda: load_range(args.symbol, start_ms, end_ms)
        )
        slice_time = 0.0
        rss_after_slice = rss_after_load

    if len(ticks) == 0:
        raise SystemExit(f"no ticks found for last {days} days of {args.symbol}")

    klines, kline_time, _, rss_after_kline = _stage_timing(
        lambda: _build_klines_from_ticks(args.symbol, ticks, interval=args.interval)
    )
    kline_times = [(k.open_time, k.close_time) for k in klines]

    if tick_access == "range":
        tick_map, mapping_time, _, rss_after_mapping = _stage_timing(
            lambda: build_tick_slice_accessor(ticks, kline_times)
        )
    else:
        tick_map, mapping_time, _, rss_after_mapping = _stage_timing(
            lambda: build_bar_map(ticks, kline_times)
        )

    strategy_cls = STRATEGY_REGISTRY.get(args.strategy)
    if strategy_cls is None:
        names = ", ".join(sorted(STRATEGY_REGISTRY))
        raise SystemExit(f"unknown strategy: {args.strategy}. available: {names}")

    strategy = strategy_cls()
    signals, strategy_time, _, rss_after_strategy = _stage_timing(
        lambda: strategy.on_history(klines, tick_map=tick_map)
    )

    cfg = BacktestConfig(
        initial_capital=args.initial_capital,
        max_loss_pct=args.max_loss_pct,
        leverage=args.leverage,
        fee_mode=args.fee_mode,
        custom_fee_rate=args.custom_fee_rate,
        slippage_bps=args.slippage_bps,
        funding_rate=args.funding_rate,
        maint_margin=args.maint_margin,
        compound=True,
    )
    stats, simulate_time, _, rss_after_sim = _stage_timing(
        lambda: simulate_trades(signals, cfg)
    )

    peak_rss = max(
        rss for rss in (
            rss_after_load,
            rss_after_slice,
            rss_after_kline,
            rss_after_mapping,
            rss_after_strategy,
            rss_after_sim,
        ) if rss is not None
    ) if any(rss is not None for rss in (
        rss_after_load,
        rss_after_slice,
        rss_after_kline,
        rss_after_mapping,
        rss_after_strategy,
        rss_after_sim,
    )) else None

    first_bar = datetime.fromtimestamp(klines[0].open_time / 1000, tz=timezone.utc)
    last_bar = datetime.fromtimestamp(klines[-1].open_time / 1000, tz=timezone.utc)
    return {
        "days": days,
        "tick_access": tick_access,
        "load_mode": args.load_mode,
        "ticks": len(ticks),
        "bars": len(klines),
        "tick_coverage": len(tick_map),
        "range_utc": f"{first_bar.isoformat()} -> {last_bar.isoformat()}",
        "timings_sec": {
            "tick_load": load_time,
            "range_filter": slice_time,
            "bar_build": kline_time,
            "tick_to_bar_mapping": mapping_time,
            "strategy_execution": strategy_time,
            "backtest_simulate": simulate_time,
        },
        "peak_rss_bytes": peak_rss,
        "stats": {
            "trades": stats["trades"],
            "profit_factor": stats["profit_factor"],
            "total_net_pnl": stats["total_net_pnl"],
            "max_drawdown_pct": stats["max_drawdown_pct"],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark tick backtest stages for cached NPZ data."
    )
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--strategy", default="Wick Reversal 1m v4")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--days", default="7,90,365",
                        help="comma-separated day windows, default: 7,90,365")
    parser.add_argument("--tick-access", choices=["map", "range", "both"], default="both")
    parser.add_argument(
        "--load-mode", choices=["auto", "legacy"], default="auto",
        help="auto=use shard-aware load_range, legacy=load full NPZ then slice",
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--initial-capital", type=float, default=10_000.0)
    parser.add_argument("--max-loss-pct", type=float, default=0.02)
    parser.add_argument("--leverage", type=int, default=20)
    parser.add_argument("--fee-mode", default="Taker")
    parser.add_argument("--custom-fee-rate", type=float, default=0.00032)
    parser.add_argument("--slippage-bps", type=float, default=0.2)
    parser.add_argument("--funding-rate", type=float, default=0.0)
    parser.add_argument("--maint-margin", type=float, default=0.005)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    days_list = [int(x.strip()) for x in args.days.split(",") if x.strip()]
    access_modes = ["map", "range"] if args.tick_access == "both" else [args.tick_access]

    results: list[dict[str, Any]] = []
    for days in days_list:
        for access in access_modes:
            for run_idx in range(1, args.repeat + 1):
                result = _run_once(args, days=days, tick_access=access)
                result["run"] = run_idx
                results.append(result)

                if not args.json:
                    print(
                        f"[{args.load_mode}/{access}] {days}d run={run_idx} "
                        f"ticks={result['ticks']:,} bars={result['bars']:,} "
                        f"coverage={result['tick_coverage']}/{result['bars']} "
                        f"peak_rss={_fmt_rss(result['peak_rss_bytes'])}"
                    )
                    for key, value in result["timings_sec"].items():
                        print(f"  {key}: {value:.4f}s")
                    stats = result["stats"]
                    print(
                        f"  trades={stats['trades']} pf={stats['profit_factor']:.4f} "
                        f"net={stats['total_net_pnl']:.4f} dd={stats['max_drawdown_pct']:.4f}"
                    )
                    print(f"  range={result['range_utc']}")

    if args.json:
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
