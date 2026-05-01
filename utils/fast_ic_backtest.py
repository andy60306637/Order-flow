"""
快速 IC 回測工具 (CLI Based) v1.1
==========================
針對 Research 因子進行向量化 IC 與分位數分析，邏輯與 UI Research Lab 完全一致。
支援 JSON 報告、CSV 數據包與 Markdown 摘要產出。

用法示例：
  python utils/fast_ic_backtest.py --symbol BTCUSDT --factors lower_wick_to_body_ratio,volume_z_score --start 2026-01-01 --end 2026-02-01 --pkg docs/reports/factor_pkg
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

from backtest.time_slice import TimeSlice
from core.data_paths import set_data_root_override
from research.registry import ensure_builtin_factors, list_factors
from research.runner import ResearchConfig, run_research

def _dt_to_ms(s: str) -> int:
    try:
        return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

def _ms_to_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

def _to_builtin(obj: Any) -> Any:
    """遞迴將 NumPy 類型轉為 Python 原生類型以便 JSON 序列化"""
    if isinstance(obj, dict):
        return {k: _to_builtin(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_builtin(v) for v in obj]
    if isinstance(obj, (np.integer, np.floating)):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return _to_builtin(obj.tolist())
    return obj

def print_summary(summary: list[dict[str, Any]]):
    print("\n=== Factor Summary (Out-of-Sample) ===")
    if not summary:
        print("  (No factors analyzed)")
        return
    
    headers = ["Factor", "Group", "OOS IC", "OOS IR", "OOS t-stat", "Best Horiz"]
    fmt = "{:<25} {:<20} {:>10} {:>8} {:>10} {:>10}"
    print(fmt.format(*headers))
    print("-" * 90)
    
    for row in summary:
        # 優先取 OOS 指標
        ic = row.get("oos_oriented_rank_ic")
        if ic is None: ic = row.get("oriented_rank_ic", 0)
        
        ir = row.get("oos_ic_ir")
        if ir is None: ir = row.get("ic_ir", 0)
        
        tstat = row.get("oos_ic_t_stat")
        if tstat is None: tstat = row.get("ic_t_stat", 0)
        
        horizon = row.get("oos_best_horizon")
        if horizon is None: horizon = row.get("best_horizon", "")

        print(fmt.format(
            row["factor"][:25],
            row["group"][:20],
            f"{ic:.4f}",
            f"{ir:.2f}",
            f"{tstat:.2f}",
            str(horizon)
        ))

def save_package(result: dict, target_dir: Path):
    """仿照 UI 匯出數據包 (JSON + CSVs)"""
    target_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. 保存完整的 JSON
    json_path = target_dir / "full_result.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(_to_builtin(result), f, indent=2, ensure_ascii=False)
    
    # 2. 為每個部分保存 CSV
    for key, rows in result.items():
        if key == "timeseries_ic":
            continue
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            continue
        
        csv_path = target_dir / f"{key}.csv"
        try:
            with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(_to_builtin(rows))
        except Exception as e:
            print(f"⚠️ Warning: Failed to save CSV section '{key}': {e}")

def generate_markdown(result: dict, config: ResearchConfig) -> str:
    """生成 Markdown 格式的簡短摘要"""
    lines = []
    lines.append(f"# Factor Research Audit Report")
    lines.append(f"Generated at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    lines.append(f"\n## Configuration")
    lines.append(f"- Symbol: `{config.symbol}`")
    lines.append(f"- Interval: `{config.interval}`")
    lines.append(f"- Horizons: `{config.horizons}`")
    lines.append(f"- Quantiles: `{config.quantiles}`")
    lines.append(f"- Train Ratio: `{config.train_ratio}`")
    lines.append(f"- Analyzed Rows: `{result['rows']}`")
    
    lines.append("\n## Top Factors (OOS Oriented Rank IC)")
    lines.append("| Factor | Group | OOS IC | OOS IR | OOS t-stat | Best Horizon |")
    lines.append("| :--- | :--- | ---: | ---: | ---: | ---: |")
    
    # 取前 10 名
    top_factors = sorted(result["summary"], key=lambda x: x.get("oos_oriented_rank_ic", 0), reverse=True)[:10]
    for row in top_factors:
        ic = row.get("oos_oriented_rank_ic", 0)
        ir = row.get("oos_ic_ir", 0)
        tstat = row.get("oos_ic_t_stat", 0)
        horizon = row.get("oos_best_horizon", "")
        lines.append(f"| {row['factor']} | {row['group']} | {ic:.4f} | {ir:.2f} | {tstat:.2f} | {horizon} |")
    
    return "\n".join(lines)

def main():
    parser = argparse.ArgumentParser(description="Unified Fast IC Backtester (CLI) v1.1")
    parser.add_argument("--symbol", default="BTCUSDT", help="Symbol, e.g. BTCUSDT")
    parser.add_argument("--interval", default="1m", help="K-line interval, e.g. 1m, 15m")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD or ISO)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD or ISO)")
    parser.add_argument("--factors", help="Comma-separated factor names (empty for all)")
    parser.add_argument("--horizons", default="1,3,6,12", help="Forward horizons, e.g. 1,3,6,12")
    parser.add_argument("--quantiles", type=int, default=5, help="Number of quantiles")
    parser.add_argument("--entry-lag", type=int, default=1, help="Entry lag bars")
    parser.add_argument("--train-ratio", type=float, default=0.5, help="In-sample ratio (0.1-0.9)")
    parser.add_argument("--data-root", default=None, help="Override data root")
    
    # 導出選項
    parser.add_argument("--out", help="Path to save single JSON report")
    parser.add_argument("--pkg", help="Directory path to save UI-style export package (JSON + CSVs)")
    parser.add_argument("--md", help="Path to save Markdown summary report")
    
    args = parser.parse_args()

    if args.data_root:
        set_data_root_override(args.data_root)

    ensure_builtin_factors()
    available = list_factors()
    
    if args.factors:
        factor_names = [f.strip() for f in args.factors.split(",") if f.strip()]
        valid_factors = []
        for f in factor_names:
            if f in available:
                valid_factors.append(f)
            else:
                print(f"⚠️ Warning: Unknown factor '{f}', skipping.")
        factor_names = valid_factors
        if not factor_names:
            print("❌ No valid factors provided.")
            return
    else:
        factor_names = available

    try:
        start_ms = _dt_to_ms(args.start)
        end_ms = _dt_to_ms(args.end)
    except Exception as e:
        print(f"❌ Invalid date format: {e}")
        return

    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]

    print(f"🚀 Starting IC Backtest: {args.symbol} | {args.interval} | {args.start} -> {args.end}")
    print(f"Factors: {len(factor_names)} | Horizons: {horizons} | IS/OOS Split: {args.train_ratio}")

    t0 = time.perf_counter()
    
    config = ResearchConfig(
        symbol=args.symbol,
        interval=args.interval,
        slices=[TimeSlice(label="Full Range", segments=[(start_ms, end_ms)])],
        factor_names=factor_names,
        horizons=horizons,
        quantiles=args.quantiles,
        entry_lag=args.entry_lag,
        train_ratio=args.train_ratio
    )

    try:
        result_obj = run_research(config)
        result_dict = result_obj.to_dict()
    except Exception as e:
        print(f"❌ Backtest failed: {e}")
        import traceback
        traceback.print_exc()
        return

    elapsed = time.perf_counter() - t0

    print_summary(result_dict["summary"])
    
    # 保存單個 JSON
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(_to_builtin(result_dict), f, indent=2, ensure_ascii=False)
        print(f"\n📝 Saved JSON report to: {args.out}")

    # 保存數據包 (JSON + CSVs)
    if args.pkg:
        pkg_dir = Path(args.pkg)
        save_package(result_dict, pkg_dir)
        print(f"\n📦 Saved Export Package to: {pkg_dir}/")

    # 保存 Markdown 摘要
    if args.md:
        md_path = Path(args.md)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_content = generate_markdown(result_dict, config)
        md_path.write_text(md_content, encoding="utf-8")
        print(f"\n📄 Saved Markdown summary to: {args.md}")

    print(f"\n✨ Backtest completed in {elapsed:.2f}s (Analyzed {result_dict['rows']} bars)")

if __name__ == "__main__":
    main()
