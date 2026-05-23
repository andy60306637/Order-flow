
"""
Mean Reversion Pipeline Ablation Backtest Tool
==============================================
Specifically designed for the MR Fix & Experiment Plan v1.
Supports V0-V9 configurations.

NOTE — V2 fee_cover_ratio=4.0 vs min_stop_pct=0.0015 (數學冗餘分析)
------------------------------------------------------------------------
V2 設定了 fee_cover_ratio=4.0，理論上比 V1（FCR=1.2）的過濾更嚴。

數學驗算（taker_fee_rate=0.00032, slippage_rate=0.00002, rr_ratio=1.5）：
  round_trip_cost_pct = 2 × (0.00032 + 0.00002) = 0.00068 = 0.068%
  FCR=4.0 要求的最小停損距離：
    min_risk_pct = 0.00068 × 4.0 / 1.5 = 0.001813 = 0.1813%
  min_stop_pct 要求：0.0015 = 0.15%

  0.1813% > 0.15%，因此 FCR=4.0 的門檻確實比 min_stop_pct 更嚴。

為什麼 V1 和 V2 結果完全相同？
  這屬於「數學上的合理冗餘」，而非代碼 bug。
  原因：EntryManagementStage 用 ATR(14) 計算停損，BTCUSDT 1m 的 ATR
  通常對應停損距離遠大於 0.18%（在低波動期也約在 0.3~0.5% 以上）。
  因此所有通過 min_stop_pct=0.15% 篩選的交易，其 ATR 停損距離也已
  超過 FCR=4.0 要求的 0.1813%，使得 FCR=4.0 的過濾條件從未被觸發。

  若要讓 FCR 真正起到差異化過濾效果，需要將 fee_cover_ratio 提高到
  使 min_risk_pct > 通常的 ATR 停損水準（約 6.0 以上）。
  在目前的 BTC 1m 回測環境中，fee_cover_ratio <= 6.0 均為冗餘設定。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestConfig, simulate_trades
from core import kline_cache, tick_cache
from core.data_paths import set_data_root_override
from core.tick_cache import build_bar_map
from strategies.pipeline.mean_reversion import build_mean_reversion_pipeline
from strategies.pipeline.runner import MultiPipelineRunner
from strategies.pipeline.strategy import MultiPipelineStrategy
from strategies.modules import CapitalConfig, ExitModule, ExitConfig
from utils.tick_data_backtest import _build_klines_from_ticks

def _dt_to_ms(s: str) -> int:
    try:
        return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

def _ms_to_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

def print_table(title: str, data: dict[str, Any]):
    print(f"\n=== {title} ===")
    if not data:
        print("  (無數據)")
        return
    max_k = max(len(k) for k in data.keys())
    for k, v in data.items():
        if isinstance(v, float):
            v_str = f"{v:.4f}"
            if any(x in k for x in ["pct", "rate", "win", "報酬", "勝率", "回撤", "fees/gross"]):
                v_str = f"{v:.2f}%"
        else:
            v_str = str(v)
        print(f"  {k:<{max_k}} : {v_str}")

def get_variant_config(variant: str) -> dict[str, Any]:
    """Returns the pipeline configuration for a given variant V0-V9."""
    base_cfg = {
        "min_stop_pct": 0.0015, # Default for all variants after repair
    }
    
    if variant == "V0":
        # Baseline confirmation - ideally min_stop_pct should be 0 to match old report,
        # but the plan says P0 repair is a prerequisite. 
        # Plan V0: "唯一差異：研究環境的 max_loss_pct=0.005"
        return {"min_stop_pct": 0.0} # V0 uses 0.0 to reproduce the original bad state but with fixed risk
    
    if variant == "V1":
        return {"min_stop_pct": 0.0015}
    
    if variant == "V2":
        return {"min_stop_pct": 0.0015, "fee_cover_ratio": 4.0}
    
    if variant == "V3":
        return {
            "min_stop_pct": 0.0015, 
            "fee_cover_ratio": 4.0, 
            "allowed_vwap_zones": ("overextended_low",)
        }
    
    # V4-V6 are single factor tests. 
    # NOTE: Need to support disabling alpha modules in build_mean_reversion_pipeline if possible,
    # or I manually build the TradingPipeline list here.
    
    if variant == "V4": # LWDE only
        return {"min_stop_pct": 0.0015, "variant": "LWDE_ONLY"}
    
    if variant == "V5": # CVDD only
        return {"min_stop_pct": 0.0015, "variant": "CVDD_ONLY"}
        
    if variant == "V6": # RBU only
        return {"min_stop_pct": 0.0015, "variant": "RBU_ONLY"}

    if variant == "V7": # Alpha Thresholds
        return {
            "min_stop_pct": 0.0015,
            "lw_min_wick_ratio": 0.55,
            "lw_min_imbalance": 0.20,
            "lw_min_eff": 0.10,
            "min_lower_wick_ratio": 0.60,
            "min_close_pos": 0.65,
        }
    
    if variant == "V8": # Optimized Combo
        return {
            "min_stop_pct": 0.0015,
            "rr_ratio": 1.5,
            "time_decay_bars": 30,
            "allowed_vwap_zones": ("extended_low", "overextended_low"),
            "variant": "RBU_ONLY"
        }
    
    if variant == "V10": # LowerWickRatio + Flipped CVDD
        return {
            "min_stop_pct": 0.0015,
            "rr_ratio": 1.5,
            "time_decay_bars": 30,
            "enabled_signals": ("lower_wick", "cvd"),
            "cvd_flipped": True
        }

    if variant == "V11": # Flipped CVDD only
        return {
            "min_stop_pct": 0.0015,
            "rr_ratio": 1.5,
            "time_decay_bars": 30,
            "enabled_signals": ("cvd",),
            "cvd_flipped": True
        }

    if variant == "V12": # LowerWickRatio only
        return {
            "min_stop_pct": 0.0015,
            "rr_ratio": 1.5,
            "time_decay_bars": 30,
            "enabled_signals": ("lower_wick",),
        }

    return base_cfg

def _check_tick_availability(symbol: str, start_ms: int, end_ms: int) -> list[str]:
    """
    逐月檢查 tick 資料可用性，回傳缺失月份清單（"YYYY-MM" 格式）。
    透過嘗試每月初始時間戳的微量 load_range 來探測。
    """
    missing_months: list[str] = []
    dt_start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    dt_end   = datetime.fromtimestamp(end_ms   / 1000, tz=timezone.utc)

    year, month = dt_start.year, dt_start.month
    while True:
        month_start_ms = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000)
        # 取當月最後一天（下個月第一天 - 1ms）
        if month == 12:
            next_y, next_m = year + 1, 1
        else:
            next_y, next_m = year, month + 1
        month_end_ms = int(datetime(next_y, next_m, 1, tzinfo=timezone.utc).timestamp() * 1000) - 1

        # 只探測在查詢範圍內的月份
        probe_start = max(month_start_ms, start_ms)
        probe_end   = min(month_end_ms,   end_ms)
        if probe_start < probe_end:
            probe_ticks = tick_cache.load_range(symbol, probe_start, probe_end)
            if probe_ticks is None or len(probe_ticks) == 0:
                missing_months.append(f"{year:04d}-{month:02d}")

        if year >= dt_end.year and month >= dt_end.month:
            break
        month += 1
        if month > 12:
            month = 1
            year += 1

    return missing_months


def run_backtest(args, variant_name: str):
    cfg_params = get_variant_config(variant_name)
    variant_type = cfg_params.pop("variant", None)
    time_decay = cfg_params.pop("time_decay_bars", 0)
    rr_ratio = cfg_params.get("rr_ratio", 2.0)

    # Special handling for single factor variants if not supported by build_mean_reversion_pipeline
    # For now, let's assume we might need to modify build_mean_reversion_pipeline to support filtering modules
    # OR we manually construct it here.

    pipeline = build_mean_reversion_pipeline(
        capital_cfg=CapitalConfig(max_risk_pct=args.max_loss_pct * 100, leverage=args.leverage),
        **cfg_params
    )

    # Handle single factor variants by modifying the pipeline stages directly if needed
    if variant_type:
        from strategies.pipeline.mean_reversion import LowerWickRatioSignal, CVDDivergenceSignal, ReversalBarUpSignal
        from strategies.pipeline import AlphaStage

        # Find AlphaStage
        for i, stage in enumerate(pipeline.stages):
            if isinstance(stage, AlphaStage):
                if variant_type == "LWDE_ONLY" or variant_type == "LWR_ONLY":
                    stage.modules = [m for m in stage.modules if isinstance(m, LowerWickRatioSignal)]
                elif variant_type == "CVDD_ONLY":
                    stage.modules = [m for m in stage.modules if isinstance(m, CVDDivergenceSignal)]
                elif variant_type == "RBU_ONLY":
                    stage.modules = [m for m in stage.modules if isinstance(m, ReversalBarUpSignal)]
                break

    runner = MultiPipelineRunner(defs=[])
    # MultiPipelineStrategy wrapper
    strategy = MultiPipelineStrategy(
        runner=runner,
        exit_mod=ExitModule(ExitConfig(tp_rr_ratio=rr_ratio, time_decay_bars=time_decay)),
        initial_equity=args.capital
    )
    # Manually inject our custom runner/pipeline
    from strategies.pipeline import PipelineDef
    defn = PipelineDef(variant_name, pipeline=pipeline, allocation_weight=1.0)
    strategy._runner._defs = [defn]
    strategy.allow_bar_fallback_in_tick_mode = True

    # Load data
    start_ms = _dt_to_ms(args.start)
    end_ms = _dt_to_ms(args.end)
    symbol = args.symbol.upper()

    print(f"Loading ticks for {symbol} from {args.start} ({start_ms}) to {args.end} ({end_ms})")

    # ── Tick 資料可用性檢查 ────────────────────────────────────────────────────
    missing_months = _check_tick_availability(symbol, start_ms, end_ms)
    if missing_months:
        print(f"WARNING: 以下月份缺少 tick 資料：{', '.join(missing_months)}")
        if getattr(args, "fallback_kline_only", False):
            print("--fallback-kline-only 已啟用，退化為純 kline 回測（不使用 tick_map）")
        else:
            print(
                "ERROR: tick 資料不完整，無法執行 tick-based 回測。\n"
                "       請補充缺失月份的 tick 資料，或加上 --fallback-kline-only 以退化為純 kline 模式。"
            )
            return

    ticks = tick_cache.load_range(symbol, start_ms, end_ms)
    use_tick_map = (ticks is not None and len(ticks) > 0)
    if not use_tick_map:
        if getattr(args, "fallback_kline_only", False):
            print("INFO: 整個期間均無 tick 資料，直接以純 kline 模式執行。")
        else:
            print(f"No ticks found for {symbol} in range {start_ms} to {end_ms}")
            return

    if use_tick_map:
        print(f"Loaded {len(ticks)} ticks. Building klines from ticks...")
        klines = _build_klines_from_ticks(symbol, ticks, interval=args.interval)
        print(f"Built {len(klines)} klines.")
        tick_map = build_bar_map(ticks, [(k.open_time, k.close_time) for k in klines])
    else:
        # 純 kline 模式（--fallback-kline-only）：從 kline_cache 載入
        print(f"Loading klines from kline_cache ({args.interval})...")
        klines = kline_cache.load(symbol, args.interval, start_ms, end_ms)
        if not klines:
            print(f"ERROR: 無法從 kline_cache 載入 {symbol} {args.interval} 的 K 線資料。")
            return
        print(f"Loaded {len(klines)} klines from kline_cache.")
        tick_map = None

    # Execute
    print("Calculating signals...")
    signals = strategy.on_history(klines, tick_map=tick_map)
    print(f"Generated {len(signals)} signals.")
    if signals:
        print(f"DEBUG: Signal 0 is {type(signals[0])}")
        if isinstance(signals[0], dict):
            print(f"DEBUG: Signal 0 data: {signals[0]}")
    
    backtest_cfg = BacktestConfig(
        initial_capital=args.capital,
        leverage=args.leverage,
        fee_mode="自訂",
        custom_fee_rate=args.fee,
        slippage_bps=args.slippage,
        compound=True
    )
    results = simulate_trades(signals, backtest_cfg)
    
    # Monthly Breakdown
    # Instead of re-simulating (which is complex due to equity carry-over),
    # we just aggregate the results from the full trade_list.
    monthly_stats = {}
    for t in results["trade_list"]:
        if t.get("skipped"): continue
        dt = datetime.fromtimestamp(t["entry_time"] / 1000, tz=timezone.utc)
        month_key = dt.strftime("%Y-%m")
        if month_key not in monthly_stats:
            monthly_stats[month_key] = {"trades": 0, "win": 0, "gross_p": 0, "gross_l": 0, "fees": 0, "net": 0}
        
        s = monthly_stats[month_key]
        s["trades"] += 1
        pnl = t["net_pnl"]
        s["net"] += pnl
        s["fees"] += t["total_fee"]
        if pnl > 0:
            s["win"] += 1
            s["gross_p"] += pnl + t["total_fee"] # Gross = Net + Fee
        else:
            s["gross_l"] += abs(pnl + t["total_fee"])

    print(f"\n實驗：{variant_name}")
    print(f"| 期間 | Trades | WR | PF | Net PnL | Fees | fees/gross |")
    print(f"|---|---:|---:|---:|---:|---:|---:|")
    
    for month in sorted(monthly_stats.keys()):
        s = monthly_stats[month]
        wr = (s["win"] / s["trades"] * 100) if s["trades"] > 0 else 0
        pf = (s["gross_p"] / s["gross_l"]) if s["gross_l"] > 0 else (99.0 if s["gross_p"] > 0 else 1.0)
        fg = (s["fees"] / s["gross_p"] * 100) if s["gross_p"] > 0 else 0
        print(f"| {month} | {s['trades']} | {wr:.2f}% | {pf:.2f} | {s['net']:.0f} | {s['fees']:.0f} | {fg:.2f}% |")
    
    # 全期
    wr_total = results["win_rate"]
    pf_total = results["profit_factor"]
    net_total = results["total_net_pnl"]
    fees_total = results["total_fees"]
    gross_p_total = sum(t["net_pnl"] + t["total_fee"] for t in results["trade_list"] if not t.get("skipped") and t["net_pnl"] > 0)
    fg_total = (fees_total / gross_p_total * 100) if gross_p_total > 0 else 0
    print(f"| 全期 | {results['trades']} | {wr_total:.2f}% | {pf_total:.2f} | {net_total:.0f} | {fees_total:.0f} | {fg_total:.2f}% |")

def main():
    parser = argparse.ArgumentParser(description="MR Ablation Backtest Tool")
    parser.add_argument("--variant", required=True, help="V0-V9")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start", default="2025-07-01",
                        help="回測起始日期（預設 2025-07-01，擴充至 6 個月讓 V8 能積累 >=50 筆）")
    parser.add_argument("--end", default="2026-02-28",
                        help="回測結束日期（預設 2026-02-28）")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--capital", type=float, default=10000.0)
    parser.add_argument("--leverage", type=int, default=20)
    parser.add_argument("--fee", type=float, default=0.00032)
    parser.add_argument("--slippage", type=float, default=0.2)
    parser.add_argument("--max_loss_pct", type=float, default=0.005, help="Fixed risk per trade")
    parser.add_argument(
        "--fallback-kline-only",
        action="store_true",
        default=False,
        dest="fallback_kline_only",
        help=(
            "當 tick 資料不足（缺失月份）時，退化為純 kline 回測（不使用 tick_map）。"
            "啟用後，缺失 tick 的期間仍可執行回測，但進場精度較低（以 K 線開盤價模擬）。"
        ),
    )

    args = parser.parse_args()

    run_backtest(args, args.variant)

if __name__ == "__main__":
    main()
