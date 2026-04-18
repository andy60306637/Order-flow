"""
Regime-aware optimizer for WickReversalV4.

針對三個 BTC 價格區間分別跑 CoordinateOptimizer，輸出 r0/r1/r2 各自最佳化後的參數集，
寫入策略預設值並儲存完整報告。

執行：
  python utils/optimize_wick_reversal_v4_regime.py
"""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.optimize_wick_reversal_v4 import (
    CoordinateOptimizer,
    StrategyRunner,
    _brief,
    _default_side_params,
    _dt_to_ms,
    _to_builtin,
)
from strategies.wick_reversal_v4 import WickReversalV4Strategy

# ── Regime 設定 ─────────────────────────────────────────────────────────────
# 每個 regime 對應一個 shard dataset（代表那個價格水位的歷史數據）
REGIMES = [
    {
        "idx": 0,
        "label": "R0 (<50k)",
        "symbol": "BTCUSDT_20230414_20240413",
        "train_start": "2023-04-14",
        "split_date":  "2024-01-01",
        "end_date":    "2024-04-14",
    },
    {
        "idx": 1,
        "label": "R1 (50k-85k)",
        "symbol": "BTCUSDT_20240414_20250413",
        "train_start": "2024-04-14",
        "split_date":  "2025-01-01",
        "end_date":    "2025-04-14",
    },
    {
        "idx": 2,
        "label": "R2 (>85k)",
        "symbol": "BTCUSDT",
        "train_start": "2025-04-14",
        "split_date":  "2026-01-01",
        "end_date":    "2026-04-14",
    },
]

# 每個 regime 可搜尋的參數範圍（與 optimize_wick_reversal_v4._grid_for_side 相同邏輯，
# 但 key 會加上 r{i}_ 前綴）
_BASE_GRID_LONG = {
    "long_sl_pct_floor": [0.0003, 0.0005, 0.0008, 0.001],
    "long_sl_wick_mult": [0.05, 0.10, 0.15, 0.20],
    "long_sl_pct_cap":   [0.002, 0.003, 0.004, 0.005],
    "long_k0_vol_gate": [300.0, 500.0, 800.0, 1200.0],
    "long_delta_eff_threshold": [0.8, 1.0, 1.2, 1.4],
    "long_vol_sma_mult": [1.0, 1.2, 1.4, 1.6],
    "lower_wick_absorption_min_vol_ratio": [0.10, 0.15, 0.20, 0.25],
    "lower_wick_absorption_delta_eff_max": [0.0, -0.05, -0.10, -0.15],
    "long_td_consec_bars": [1, 2, 3],
    "long_min_fee_cover_ratio": [1.2, 1.5, 2.0, 2.5],
    "long_rr_wick_a": [2.0, 2.5, 3.0, 3.5],
    "long_rr_wick_b": [1.0, 1.5, 2.0, 2.5],
    "long_rr_wick_c": [0.8, 1.0, 1.5, 2.0],
}

_BASE_GRID_SHORT = {
    "short_sl_pct_floor": [0.0003, 0.0005, 0.0008, 0.001],
    "short_sl_wick_mult": [0.05, 0.10, 0.15, 0.20],
    "short_sl_pct_cap":   [0.002, 0.003, 0.004, 0.005],
    "short_k0_vol_gate": [300.0, 500.0, 800.0, 1200.0],
    "short_delta_eff_threshold": [0.8, 1.0, 1.2, 1.4],
    "short_vol_sma_mult": [1.0, 1.2, 1.4, 1.6],
    "upper_wick_absorption_min_vol_ratio": [0.10, 0.15, 0.20, 0.25],
    "upper_wick_absorption_delta_eff_min": [0.0, 0.05, 0.10, 0.15],
    "enable_short_wick_a": [True],
    "enable_short_wick_b": [True],
    "enable_short_wick_c": [False, True],
    "short_a_min_upper_wick_pct": [0.0, 0.0008, 0.0010, 0.0011],
    "short_td_consec_bars": [1, 2, 3],
    "short_min_fee_cover_ratio": [1.2, 1.5, 2.0, 2.5],
    "short_rr_wick_a": [2.0, 2.5, 3.0, 4.5],
    "short_rr_wick_b": [1.0, 1.5, 2.0, 2.5],
    "short_rr_wick_c": [0.5, 0.8, 1.0, 1.5],
}

# regime-variable 參數的 key（無前綴版，用於寫入 r{i}_ 位置）
_REGIME_LONG_KEYS = [
    "long_sl_pct_floor", "long_sl_wick_mult", "long_sl_pct_cap",
    "long_k0_vol_gate", "long_rr_wick_a", "long_rr_wick_b", "long_rr_wick_c",
    "long_min_fee_cover_ratio",
]
_REGIME_SHORT_KEYS = [
    "short_sl_pct_floor", "short_sl_wick_mult", "short_sl_pct_cap",
    "short_k0_vol_gate", "short_rr_wick_a", "short_rr_wick_b", "short_rr_wick_c",
    "short_min_fee_cover_ratio",
]


def _make_regime_baseline(regime_idx: int, side: str) -> dict[str, Any]:
    """取全局預設值，加入 enable_regime_mode=True 以及所有 r{i}_* 前綴的 regime 參數。"""
    base = _default_side_params(side)
    # 加入 regime 控制
    base["enable_regime_mode"] = True
    base["regime_price_break_0"] = 50000.0
    base["regime_price_break_1"] = 85000.0
    # 為所有 regime 添加預設值（確保未被優化的 regime 有合法值）
    s = WickReversalV4Strategy()
    for ri in range(3):
        keys = _REGIME_LONG_KEYS if side == "long" else _REGIME_SHORT_KEYS
        for k in keys:
            base[f'r{ri}_{k}'] = getattr(s, k)
    return base


def _make_regime_grid(regime_idx: int, side: str) -> dict[str, list[Any]]:
    """Grid 只包含 r{regime_idx}_* 前綴的 key（只優化目標 regime）。"""
    base_grid = _BASE_GRID_LONG if side == "long" else _BASE_GRID_SHORT
    prefix = f'r{regime_idx}_'
    return {prefix + k: v for k, v in base_grid.items()}


def run_regime_optimization(
    regime: dict,
    passes: int = 3,
    topn: int = 8,
) -> dict[str, Any]:
    ri = regime["idx"]
    label = regime["label"]
    symbol = regime["symbol"]
    train_start_ms = _dt_to_ms(regime["train_start"])
    split_ms = _dt_to_ms(regime["split_date"])
    end_ms = _dt_to_ms(regime["end_date"])

    print(f"\n{'='*60}")
    print(f"  Regime {ri} ({label})  [{symbol}]")
    print(f"  train={regime['train_start']}  split={regime['split_date']}  end={regime['end_date']}")
    print(f"{'='*60}")

    runner = StrategyRunner(symbol, "1m", train_start_ms, split_ms, end_ms)
    print(f"  train_bars={len(runner.train.klines)}  val_bars={len(runner.validation.klines)}")

    results: dict[str, Any] = {"regime": regime}

    for side in ("long", "short"):
        print(f"\n  -- {side} side --")
        baseline = _make_regime_baseline(ri, side)
        grid = _make_regime_grid(ri, side)
        opt = CoordinateOptimizer(runner, side)
        result = opt.search(baseline, grid, passes=passes, top_n_validation=topn)

        best = result["best"]
        print(f"  train:  {_brief(best['train'])}")
        print(f"  val:    {_brief(best['validation'])}")
        print(f"  full:   {_brief(best['full'])}")
        print(f"  best r{ri} params (regime-specific only):")
        for k, v in best["params"].items():
            if k.startswith(f'r{ri}_'):
                print(f"    {k} = {v}")

        results[side] = {
            "best_params": best["params"],
            "train": _brief(best["train"]),
            "validation": _brief(best["validation"]),
            "full": _brief(best["full"]),
            "validation_table": [
                {"params": row["params"], "train": _brief(row["train"]), "validation": _brief(row["validation"])}
                for row in result["validation_table"]
            ],
        }

    return results


def _extract_regime_params(regime_results: list[dict]) -> dict[str, Any]:
    """從 3 個 regime 結果提取 r{i}_* 參數，組合成完整策略參數 dict。"""
    combined: dict[str, Any] = {}
    s = WickReversalV4Strategy()
    # 全局非 regime 參數直接取策略預設
    for attr in vars(type(s)):
        if not attr.startswith('_') and isinstance(getattr(s, attr, None), (int, float, bool)):
            combined[attr] = getattr(s, attr)
    combined["enable_regime_mode"] = True

    for rr in regime_results:
        ri = rr["regime"]["idx"]
        for side in ("long", "short"):
            side_result = rr[side]
            best_params = side_result["best_params"]
            # 只取 r{i}_* 前綴的 params
            for k, v in best_params.items():
                if k.startswith(f'r{ri}_'):
                    combined[k] = v
    return combined


def main() -> None:
    ap = argparse.ArgumentParser(description="Regime-aware optimizer for WickReversalV4")
    ap.add_argument("--passes", type=int, default=3)
    ap.add_argument("--topn",   type=int, default=8)
    ap.add_argument("--out", default="docs/reports/wick_v4_regime_opt.json")
    args = ap.parse_args()

    all_regime_results = []
    for regime in REGIMES:
        rr = run_regime_optimization(regime, passes=args.passes, topn=args.topn)
        all_regime_results.append(rr)

    combined_params = _extract_regime_params(all_regime_results)

    print(f"\n{'='*60}")
    print("  REGIME PARAMS SUMMARY")
    print(f"{'='*60}")
    for ri in range(3):
        print(f"\n  Regime {ri}:")
        for k, v in combined_params.items():
            if k.startswith(f'r{ri}_'):
                print(f"    {k} = {v}")

    report = {
        "regimes": _to_builtin(all_regime_results),
        "combined_params": _to_builtin(combined_params),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nsaved={out_path}")


if __name__ == "__main__":
    main()
