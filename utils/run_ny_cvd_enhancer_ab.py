"""
utils/run_ny_cvd_enhancer_ab.py
NY CVD Divergence MR -- P0 Robustness Validation (28-month window)

3 variants on extended 2024-01 ~ 2026-04 dataset:
  0. Baseline                    (reference)
  P2. A_loose (wick=0.35)        structure filter, more trades
  P1b. A_loose+C (z=1.0)        best combo from round-2

Usage:
  python utils/run_ny_cvd_enhancer_ab.py
  python utils/run_ny_cvd_enhancer_ab.py --start 2024-01-01 --end 2026-04-01
  python utils/run_ny_cvd_enhancer_ab.py --mode kline
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestConfig, simulate_trades
from core import kline_cache, tick_cache
from core.tick_cache import build_bar_map
from strategies.modules.exit_management import ExitConfig, ExitModule
from strategies.pipeline.ny_cvd_divergence_mr import build_ny_cvd_divergence_mr_pipeline_def
from strategies.pipeline.runner import MultiPipelineRunner
from strategies.pipeline.strategy import MultiPipelineStrategy
from utils.tick_data_backtest import _build_klines_from_ticks

# -- constants ----------------------------------------------------------------

TAKER_FEE   = 0.00032
SLIPPAGE_BP = 0.2
SLIPPAGE_RT = SLIPPAGE_BP / 10_000   # 0.00002

VARIANTS: list[dict] = [
    # reference
    {
        "label":  "Baseline",
        "kwargs": {},
    },
    # P2: structure filter only — more trades, lower quality bar
    {
        "label":  "P2: A_loose (wick=0.35)",
        "kwargs": {
            "use_reversal_bar_up":        True,
            "reversal_bar_min_wick":      0.35,
            "reversal_bar_min_close_pos": 0.50,
        },
    },
    # P1b: best combo — loose structure + volume confirmation
    {
        "label":  "P1b: A_loose+C (z=1.0)",
        "kwargs": {
            "use_reversal_bar_up":        True,
            "reversal_bar_min_wick":      0.35,
            "reversal_bar_min_close_pos": 0.50,
            "use_buy_volume_zscore":      True,
            "buy_vol_zscore_min":         1.0,
        },
    },
]

# -- helpers ------------------------------------------------------------------

def _dt_to_ms(s: str) -> int:
    try:
        return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

def _ms_to_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

def _to_builtin(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _to_builtin(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_builtin(v) for v in obj]
    if isinstance(obj, (np.integer, np.floating)):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return _to_builtin(obj.tolist())
    return obj

def _save_csv(trade_list: list[dict], path: Path) -> None:
    active = [t for t in trade_list if not t.get("skipped")]
    if not active:
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=active[0].keys())
        writer.writeheader()
        for t in active:
            row = dict(t)
            for key in ("entry_time", "exit_time"):
                if key in row and isinstance(row[key], int):
                    row[key] = _ms_to_utc(row[key])
            writer.writerow(row)

def _pf_tag(pf: float) -> str:
    if pf >= 1.5: return "[++]"
    if pf >= 1.2: return "[+] "
    if pf >= 1.0: return "[~] "
    return "[-] "

def _build_strategy(variant_kwargs: dict) -> MultiPipelineStrategy:
    defn = build_ny_cvd_divergence_mr_pipeline_def(
        taker_fee_rate = TAKER_FEE,
        slippage_rate  = SLIPPAGE_RT,
        **variant_kwargs,
    )
    runner = MultiPipelineRunner(defs=[defn])
    return MultiPipelineStrategy(
        runner         = runner,
        exit_mod       = ExitModule(ExitConfig(tp_rr_ratio=2.0)),
        initial_equity = 10_000.0,
    )

# -- main ---------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="NY CVD Enhancer A/B/C/D Cross-Validation")
    parser.add_argument("--symbol",   default="BTCUSDT")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--mode",     choices=["kline", "tick"], default="tick")
    parser.add_argument("--start",    default="2024-01-01")
    parser.add_argument("--end",      default="2026-04-01")
    parser.add_argument("--capital",  type=float, default=10_000.0)
    parser.add_argument("--leverage", type=int,   default=20)
    args = parser.parse_args()

    symbol   = args.symbol.upper()
    start_ms = _dt_to_ms(args.start)
    end_ms   = _dt_to_ms(args.end)
    out_dir  = PROJECT_ROOT / "docs" / "reports" / "backtest"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # -- load data (once) --
    print(f"\n[DATA] Loading {symbol} {args.start} -> {args.end}  mode={args.mode.upper()}")
    t_load = time.perf_counter()
    if args.mode == "tick":
        ticks = tick_cache.load_range(symbol, start_ms, end_ms)
        if ticks is None or len(ticks) == 0:
            print("ERROR: no tick data")
            return
        klines   = _build_klines_from_ticks(symbol, ticks, interval=args.interval)
        tick_map = build_bar_map(ticks, [(k.open_time, k.close_time) for k in klines])
    else:
        klines   = kline_cache.load_range_as_klines(symbol, args.interval, start_ms, end_ms)
        if not klines:
            print("ERROR: no kline data")
            return
        tick_map = None
    print(f"       {len(klines):,} bars  ({time.perf_counter()-t_load:.1f}s)")

    cfg = BacktestConfig(
        initial_capital = args.capital,
        leverage        = args.leverage,
        fee_mode        = "自訂",
        custom_fee_rate = TAKER_FEE,
        slippage_bps    = SLIPPAGE_BP,
        compound        = True,
    )

    # -- run each variant --
    all_results: list[dict] = []

    for v in VARIANTS:
        label  = v["label"]
        kwargs = v["kwargs"]
        print(f"\n[RUN] {label} ...")
        t0 = time.perf_counter()

        strategy = _build_strategy(kwargs)
        strategy.allow_bar_fallback_in_tick_mode = (args.mode == "tick")

        signals = strategy.on_history(klines, tick_map=tick_map)
        results = simulate_trades(signals, cfg)
        elapsed = time.perf_counter() - t0

        slug     = label.replace(":", "").replace(" ", "_").lower()
        json_out = out_dir / f"{symbol}_ny_cvd_{slug}_{ts}.json"
        csv_out  = out_dir / f"{symbol}_ny_cvd_{slug}_{ts}_trades.csv"

        report = {
            "meta": {
                "variant":  label,
                "symbol":   symbol,
                "interval": args.interval,
                "mode":     args.mode,
                "start":    _ms_to_utc(start_ms),
                "end":      _ms_to_utc(end_ms),
                "config":   {
                    "capital":  args.capital,
                    "leverage": args.leverage,
                    "fee":      TAKER_FEE,
                    "slippage": SLIPPAGE_BP,
                    "kwargs":   kwargs,
                },
            },
            "stats": {k: v for k, v in results.items() if k != "trade_list"},
        }
        with open(json_out, "w", encoding="utf-8") as f:
            json.dump(_to_builtin(report), f, indent=2, ensure_ascii=False)
        _save_csv(results.get("trade_list", []), csv_out)

        r = results
        print(
            f"       trades={r.get('trades',0):>4}  "
            f"win={r.get('win_rate',0):.1f}%  "
            f"PF={r.get('profit_factor',0):.3f}  "
            f"pnl={r.get('total_net_pnl',0):+.2f}  "
            f"mdd={r.get('max_drawdown_pct',0):.1f}%  "
            f"({elapsed:.1f}s)"
        )
        all_results.append({
            "label":         label,
            "trades":        r.get("trades", 0),
            "win_rate":      r.get("win_rate", 0.0),
            "profit_factor": r.get("profit_factor", 0.0),
            "total_net_pnl": r.get("total_net_pnl", 0.0),
            "total_return":  r.get("total_return_pct", 0.0),
            "max_drawdown":  r.get("max_drawdown_pct", 0.0),
            "sharpe":        r.get("sharpe_ratio", 0.0),
            "final_equity":  r.get("final_equity", args.capital),
            "elapsed":       elapsed,
            "json_path":     str(json_out),
            "csv_path":      str(csv_out),
            "raw":           r,
        })

    # -- comparison table --
    baseline  = all_results[0]["raw"]
    b_trades  = max(baseline.get("trades", 1), 1)

    sep = "=" * 96
    print(f"\n{sep}")
    print(f"  NY CVD Divergence MR -- Enhancer A/B/C/D Cross-Validation")
    print(f"  {symbol} | {args.interval} | {args.mode.upper()} | {args.start} -> {args.end}")
    print(f"  fee={TAKER_FEE*100:.3f}%  slip={SLIPPAGE_BP}bps  capital={args.capital:,.0f}  leverage={args.leverage}x")
    print(sep)
    print(f"  {'Variant':<26} {'Trades':>7} {'Keep%':>7} {'WinRate':>8} {'PF':>7} {'NetPnL':>11} {'Ret%':>8} {'MDD%':>7} {'Sharpe':>8}")
    print("-" * 96)

    for r in all_results:
        pct = (r["trades"] / b_trades * 100) if r["label"] != "Baseline" else 100.0
        tag = _pf_tag(r["profit_factor"])
        line = (
            f"  {r['label']:<26} "
            f"{r['trades']:>7} "
            f"{pct:>6.1f}% "
            f"{r['win_rate']:>7.1f}% "
            f"{r['profit_factor']:>6.3f}{tag} "
            f"{r['total_net_pnl']:>+10.2f} "
            f"{r['total_return']:>7.2f}% "
            f"{r['max_drawdown']:>6.1f}% "
            f"{r['sharpe']:>8.4f}"
        )
        print(line)

    print(sep)

    best = max(all_results[1:], key=lambda x: x["profit_factor"], default=None)
    if best:
        dpf  = best["profit_factor"] - all_results[0]["profit_factor"]
        dpnl = best["total_net_pnl"] - all_results[0]["total_net_pnl"]
        print(f"\n  Best single Enhancer: [{best['label']}]")
        print(f"  PF delta={dpf:+.3f}  NetPnL delta={dpnl:+.2f} USDT  kept={best['trades']}/{b_trades} ({best['trades']/b_trades*100:.1f}%)")

    print("\n  Output files:")
    for r in all_results:
        print(f"    {r['label']:<26}  {r['json_path']}")
        print(f"    {'':26}  {r['csv_path']}")

    total_t = sum(r["elapsed"] for r in all_results)
    print(f"\n  Done. Total elapsed: {total_t:.1f}s")


if __name__ == "__main__":
    main()
