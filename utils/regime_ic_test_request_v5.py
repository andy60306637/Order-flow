"""
utils/regime_ic_test_request_v5.py

針對 GROUP_MEAN_REVERSION 群組及指定的 Stage 2 Alpha 因子進行 Regime-aware IC 測試。
極限優化版 (Ultimate Optimization):
  1. 因子預算 (Pre-compute factors once).
  2. 矩陣化 IC 計算 (Vectorized IC calculation per regime).
  3. 解決內存與 I/O 瓶頸。
"""
from __future__ import annotations

import argparse
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

import research.mr_alpha_ic_factors  # noqa: F401
from research.registry import ensure_builtin_factors, get_factor
from research.runner import _forward_return, _rank, _corr, _valid_mask, _factor_orientation, _orient, _per_period_ic, _period_ids, _ir_tstat
from core.kline_cache import load_range_as_klines
from core.tick_cache import LazyCombinedTickBarMap
from research.regime_filter import (
    RegimeFilterConfig,
    RegimeDimConfig,
    DIM_SESSION,
    DIM_MARKET_VOL,
    DIM_VWAP_ZONE,
    DIM_VOL_PROFILE,
    label_display_name,
    compute_regime_masks,
)

INDIVIDUAL_FACTORS = [
    "sweep_low_reclaim",
    "cvd_bullish_divergence",
    "negative_delta_absorption",
    "val_reclaim_long",
    "poc_reversion_potential",
    "return_shock_reclaim",
]

def get_group_factors(group_name: str) -> list[str]:
    from research.registry import list_factor_infos
    infos = list_factor_infos()
    return [info["name"] for info in infos if info["group"] == group_name]

def _dt_to_ms(s: str) -> int:
    try:
        return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

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

def main():
    parser = argparse.ArgumentParser(description="Regime-aware IC Test v5")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--start", default="2025-04-01")
    parser.add_argument("--end", default="2026-04-30")
    parser.add_argument("--out", default="docs/reports/regime_ic_test.json")
    args = parser.parse_args()

    ensure_builtin_factors()
    
    group_factors = get_group_factors("Mean-Reversion & Extreme Factors")
    all_factor_names = sorted(list(set(group_factors + INDIVIDUAL_FACTORS)))
    
    print(f"Testing {len(all_factor_names)} factors for {args.symbol} {args.interval}")
    print(f"Time Range: {args.start} to {args.end}")
    
    start_ms = _dt_to_ms(args.start)
    end_ms = _dt_to_ms(args.end)
    
    # Step 1: Load Klines
    print("\n[Step 1/5] Loading Klines...")
    klines = load_range_as_klines(args.symbol, args.interval, start_ms, end_ms)
    n_klines = len(klines)
    print(f"Loaded {n_klines} klines.")
    
    # Step 2: Pre-compute Factors
    print("\n[Step 2/5] Pre-computing all factors (Once)...")
    symbols = ["BTCUSDT_20240414_20250413", "BTCUSDT"]
    kline_times = [(k.open_time, k.close_time) for k in klines]
    tick_map = LazyCombinedTickBarMap(symbols, kline_times)
    
    factor_values = {}
    factor_infos = {}
    t0 = time.perf_counter()
    for i, name in enumerate(all_factor_names, 1):
        f_obj = get_factor(name)
        print(f"  ({i}/{len(all_factor_names)}) Computing {name}...", end="\r")
        factor_values[name] = f_obj.compute(klines, tick_map)
        factor_infos[name] = {
            "orientation": _factor_orientation(f_obj.sides),
            "group": f_obj.group
        }
    print(f"\nFactors pre-computed in {time.perf_counter()-t0:.1f}s")
    
    # Step 3: Compute Regime Masks
    print("\n[Step 3/5] Computing Regime Masks (Kline Fallback)...")
    rf_config = RegimeFilterConfig(
        mode="matrix",
        dimensions=[
            RegimeDimConfig(DIM_SESSION, enabled=True, selected_labels=["asian", "london", "ny", "overlap"]),
            RegimeDimConfig(DIM_MARKET_VOL, enabled=True, selected_labels=["MEAN_REVERSION", "NEUTRAL"]),
            RegimeDimConfig(DIM_VWAP_ZONE, enabled=True, selected_labels=["extended_low", "overextended_low", "extreme_low"]),
            RegimeDimConfig(DIM_VOL_PROFILE, enabled=True, selected_labels=["price_in_val_band"]),
        ]
    )
    masks = compute_regime_masks(klines, rf_config, tick_map=None)
    print(f"Computed {len(masks)} regime masks.")
    
    # Step 4: Prepare Returns
    print("\n[Step 4/5] Preparing Forward Returns...")
    horizons = [1, 3, 6, 12, 30]
    entry_lag = 1
    closes = np.array([k.close for k in klines], dtype=float)
    open_times = np.array([k.open_time for k in klines], dtype=np.int64)
    # Simple interval ms for BTCUSDT 1m
    interval_ms = 60000 
    
    fwd_returns = {
        h: _forward_return(closes, h, entry_lag, open_times=open_times, interval_ms=interval_ms)
        for h in horizons
    }

    # Pre-compute monthly period IDs (vectorized) for _per_period_ic
    ic_period_ids, _ic_period_labels = _period_ids(open_times, "month")

    # IS/OOS Split (0.5)
    train_ratio = 0.5
    time_cut_idx = int(n_klines * train_ratio)
    is_time_mask_global = np.zeros(n_klines, dtype=bool)
    is_time_mask_global[:time_cut_idx] = True
    oos_time_mask_global = ~is_time_mask_global
    
    # Step 5: Fast IC Analysis
    print("\n[Step 5/5] Running Matrix IC Analysis...")
    final_results = {}
    
    for regime_label, regime_mask in masks.items():
        print(f"  Analyzing {regime_label}...")
        is_time_mask = is_time_mask_global & regime_mask
        oos_time_mask = oos_time_mask_global & regime_mask
        
        summary = []
        for name in all_factor_names:
            vals = factor_values[name]
            info = factor_infos[name]
            orientation = info["orientation"]
            
            horizon_metrics = []
            for h in horizons:
                ret = fwd_returns[h]
                
                # IS Metrics
                is_period_ic = _per_period_ic(vals, ret, ic_period_ids, 30, restrict_mask=is_time_mask)
                _, _, is_ir, is_tstat = _ir_tstat(is_period_ic)

                # OOS Metrics
                oos_period_ic = _per_period_ic(vals, ret, ic_period_ids, 30, restrict_mask=oos_time_mask)
                oos_mean, _, oos_ir, oos_tstat = _ir_tstat(oos_period_ic)
                
                # Rank IC (OOS)
                oos_valid = oos_time_mask & _valid_mask(vals, ret)
                if oos_valid.sum() > 10:
                    ric = _corr(_rank(vals[oos_valid]), _rank(ret[oos_valid]))
                    oric = _orient(ric, orientation)
                else:
                    oric = 0.0
                
                horizon_metrics.append({
                    "horizon": h,
                    "oos_oriented_rank_ic": oric,
                    "oos_ic_ir": oos_ir,
                    "oos_ic_t_stat": oos_tstat
                })
            
            # Find best horizon for summary
            best_h_row = max(horizon_metrics, key=lambda x: x["oos_oriented_rank_ic"])
            summary.append({
                "factor": name,
                "group": info["group"],
                "oos_oriented_rank_ic": best_h_row["oos_oriented_rank_ic"],
                "oos_ic_ir": best_h_row["oos_ic_ir"],
                "oos_ic_t_stat": best_h_row["oos_ic_t_stat"],
                "best_horizon": best_h_row["horizon"],
                "rank_score": best_h_row["oos_oriented_rank_ic"]
            })
            
        summary.sort(key=lambda x: x["rank_score"], reverse=True)
        final_results[regime_label] = {"summary": summary}

    # Print Report
    print("\n" + "=" * 100)
    print(f"{'Regime Label':<40} | {'Factor':<25} | {'IC':>8} | {'IR':>8} | {'t-stat':>8}")
    print("-" * 100)
    for label, res in final_results.items():
        display_label = label_display_name(label)
        top3 = res["summary"][:3]
        first = True
        for row in top3:
            lbl = f"{display_label:<40}" if first else " " * 40
            print(f"{lbl} | {row['factor'][:25]:<25} | {row['oos_oriented_rank_ic']:>+8.4f} | {row['oos_ic_ir']:>8.2f} | {row['oos_ic_t_stat']:>8.2f}")
            first = False
        print("-" * 100)

    # Save
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(_to_builtin(final_results), f, indent=2, ensure_ascii=False)
    print(f"\nFull results saved to {out_path}")

if __name__ == "__main__":
    main()
