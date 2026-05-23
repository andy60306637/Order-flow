"""
utils/ic_mr_alpha.py — 均值回歸 Pipeline Alpha 因子 IC 測試

測試 Stage 2 三個因子的預測能力：
  mr_lwde_eff         下影線 × Delta 效率
  mr_rbu_strength     Reversal Bar Up 形態強度
  mr_cvdd_divergence  CVD 正背離強度

用法：
  uv run utils/ic_mr_alpha.py --start 2026-01-01 --end 2026-02-28
  uv run utils/ic_mr_alpha.py --start 2026-01-01 --end 2026-02-28 --horizons 1,3,6,12,30 --out docs/reports/ablation/ic_mr_alpha.json
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

import research.mr_alpha_ic_factors  # noqa: F401 — 觸發 @register_factor
from backtest.time_slice import TimeSlice
from research.registry import ensure_builtin_factors
from research.runner import ResearchConfig, run_research

MR_FACTORS = ["mr_lwde_eff", "mr_rbu_strength", "mr_cvdd_divergence"]
FACTOR_LABELS = {
    "mr_lwde_eff":         "LWDE (wick×imbal)",
    "mr_rbu_strength":     "RBU  (wick×cpos)",
    "mr_cvdd_divergence":  "CVDD (divergence)",
}


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


def _grade(ic: float, t: float) -> str:
    if abs(t) < 1.65:
        return "不顯著"
    if ic >= 0.05:
        return "優"
    if ic >= 0.02:
        return "良"
    if ic >= 0.0:
        return "弱正"
    return "負向"


def print_report(result: dict) -> None:
    summary = result.get("summary", [])
    metrics = result.get("metrics", [])

    print("\n" + "=" * 72)
    print("  均值回歸 Alpha 因子 IC 測試報告")
    print("=" * 72)
    print(f"{'因子':<22} {'OOS IC':>8} {'OOS IR':>8} {'t-stat':>8} {'最佳Horizon':>12} {'評級':>8}")
    print("-" * 72)

    for row in summary:
        factor = row["factor"]
        label = FACTOR_LABELS.get(factor, factor)
        ic = row.get("oos_oriented_rank_ic") or row.get("oriented_rank_ic", 0.0)
        ir = row.get("oos_ic_ir") or row.get("ic_ir", 0.0)
        tstat = row.get("oos_ic_t_stat") or row.get("ic_t_stat", 0.0)
        horizon = row.get("oos_best_horizon") or row.get("best_horizon", "?")
        grade = _grade(ic or 0.0, tstat or 0.0)
        print(f"{label:<22} {ic:>+8.4f} {ir:>8.2f} {tstat:>8.2f} {str(horizon):>12} {grade:>8}")

    print("-" * 72)
    print(f"\n樣本數：{result.get('rows', 0):,} 根 K 棒")

    # 各 Horizon 明細
    if metrics:
        print("\n--- Horizon 明細（OOS Rank IC） ---")
        horizons = sorted({m["horizon"] for m in metrics})
        header = f"{'因子':<22}" + "".join(f"  H{h:>3}" for h in horizons)
        print(header)
        print("-" * (22 + 7 * len(horizons)))
        for factor in MR_FACTORS:
            label = FACTOR_LABELS.get(factor, factor)
            row_vals = {m["horizon"]: m for m in metrics if m["factor"] == factor}
            vals = ""
            for h in horizons:
                m = row_vals.get(h)
                if m:
                    ic_h = m.get("oos_oriented_rank_ic") or m.get("oriented_rank_ic", float("nan"))
                    vals += f"  {ic_h:>+5.3f}" if ic_h == ic_h else "    nan"
                else:
                    vals += "    n/a"
            print(f"{label:<22}{vals}")

    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="MR Alpha IC Test")
    parser.add_argument("--symbol",      default="BTCUSDT")
    parser.add_argument("--interval",    default="1m")
    parser.add_argument("--start",       required=True, help="YYYY-MM-DD")
    parser.add_argument("--end",         required=True, help="YYYY-MM-DD")
    parser.add_argument("--horizons",    default="1,3,6,12,30", help="Forward horizons (bars)")
    parser.add_argument("--quantiles",   type=int, default=5)
    parser.add_argument("--train-ratio", type=float, default=0.5)
    parser.add_argument("--out",         default=None, help="Save JSON to path")
    args = parser.parse_args()

    ensure_builtin_factors()

    start_ms = _dt_to_ms(args.start)
    end_ms = _dt_to_ms(args.end)
    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]

    print(f"IC 測試：{args.symbol} {args.interval}  {args.start} → {args.end}")
    print(f"Horizons: {horizons}  因子: {MR_FACTORS}")

    config = ResearchConfig(
        symbol=args.symbol,
        interval=args.interval,
        slices=[TimeSlice(label="Full", segments=[(start_ms, end_ms)])],
        factor_names=MR_FACTORS,
        horizons=horizons,
        quantiles=args.quantiles,
        entry_lag=1,
        train_ratio=args.train_ratio,
        use_tick_features=False,
    )

    t0 = time.perf_counter()
    try:
        result_obj = run_research(config)
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)

    result = result_obj.to_dict()
    elapsed = time.perf_counter() - t0

    print_report(result)
    print(f"完成（{elapsed:.1f}s）")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(_to_builtin(result), f, indent=2, ensure_ascii=False)
        print(f"JSON 已存至 {out_path}")


if __name__ == "__main__":
    main()
