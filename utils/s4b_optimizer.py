"""
S4B 專項優化器
==============
依照 doc/s4b_optimization_plan.md 的四個 Phase 執行：

Phase 1 – S4B baseline + k0 特徵分析
Phase 2 – S4B 專屬 entry filter 網格搜尋
Phase 3 – S4B 風險管理參數微調
Phase 4 – Combined (long + S4A + S4B) 驗證

用法：
    python utils/s4b_optimizer.py [options]

選項：
    --symbol        BTCUSDT (default)
    --interval      1m      (default)
    --train-start   2025-04-14
    --split-date    2026-02-01
    --end-date      2026-04-14
    --passes        2       (coordinate descent passes)
    --topn          8       (validation candidate count)
    --out           docs/reports/s4b_optimization.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestConfig, simulate_trades
from core.tick_cache import build_bar_map, load_raw
from strategies.wick_reversal_v4 import WickReversalV4Strategy
from utils.optimize_wick_reversal_v4 import (
    Dataset,
    StrategyRunner,
    _brief,
    _dt_to_ms,
    _label_breakdown,
    _safe_float,
    _score_stats,
    _side_stats,
    _slice_ticks,
    _to_builtin,
)
from utils.tick_data_backtest import _build_klines_from_ticks


# ─────────────────────────────────────────────────────────────────────────────
# 工具函數
# ─────────────────────────────────────────────────────────────────────────────

def _exit_label_dist(trade_list: list[dict], entry_label: str) -> dict[str, Any]:
    trades = [
        t for t in trade_list
        if not t.get("skipped") and t.get("entry_label", "") == entry_label
    ]
    if not trades:
        return {}
    total = len(trades)
    counts: dict[str, int] = {}
    for t in trades:
        el = t.get("exit_label", "?")
        counts[el] = counts.get(el, 0) + 1
    return {k: {"count": v, "pct": round(v / total * 100, 1)} for k, v in sorted(counts.items())}


def _feature_stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    n = len(values)
    s = sorted(values)
    mean = sum(s) / n
    p25 = s[int(n * 0.25)]
    p50 = s[int(n * 0.50)]
    p75 = s[int(n * 0.75)]
    return {"n": n, "mean": round(mean, 6), "p25": round(p25, 6),
            "p50": round(p50, 6), "p75": round(p75, 6),
            "min": round(s[0], 6), "max": round(s[-1], 6)}


def _s4b_feature_analysis(k0_records: list[dict], trade_list: list[dict]) -> dict[str, Any]:
    """
    將 k0_records 與 trade_list 交叉比對，輸出 S4B 勝負兩組的特徵分布。
    """
    # 建立 entry_open_time → trade 的映射（只看 S4B）
    trade_by_entry: dict[int, dict] = {}
    for t in trade_list:
        if not t.get("skipped") and t.get("entry_label", "") == "S4B":
            trade_by_entry[t["entry_time"]] = t

    win_features: dict[str, list[float]] = {
        "upper_wick_pct": [], "wick_body_ratio": [],
        "k0_volume": [], "absorption_vol_ratio": [],
    }
    loss_features: dict[str, list[float]] = deepcopy(win_features)
    no_entry: list[dict] = []

    for rec in k0_records:
        if rec.get("wick_type") != "B":
            continue
        ent = rec.get("entry_open_time")
        if ent is None:
            no_entry.append(rec)
            continue
        trade = trade_by_entry.get(ent)
        if trade is None:
            no_entry.append(rec)
            continue
        bucket = win_features if trade["net_pnl"] > 0 else loss_features
        bucket["upper_wick_pct"].append(rec["upper_wick_pct"])
        bucket["wick_body_ratio"].append(rec["wick_body_ratio"])
        bucket["k0_volume"].append(rec["k0_volume"])
        if rec.get("absorption_vol_ratio") is not None:
            bucket["absorption_vol_ratio"].append(rec["absorption_vol_ratio"])

    return {
        "win": {k: _feature_stats(v) for k, v in win_features.items()},
        "loss": {k: _feature_stats(v) for k, v in loss_features.items()},
        "no_entry_k0_count": len(no_entry),
        "total_k0_detected": len([r for r in k0_records if r.get("wick_type") == "B"]),
    }


# ─────────────────────────────────────────────────────────────────────────────
# S4B-only runner
# ─────────────────────────────────────────────────────────────────────────────

def _s4b_base_params() -> dict[str, Any]:
    """S4B-only baseline：關閉 long、S4A、S4C，只保留 S4B。"""
    s = WickReversalV4Strategy()
    params = {
        "enable_long": False,
        "enable_short": True,
        "enable_short_wick_a": False,
        "enable_short_wick_b": True,
        "enable_short_wick_c": False,
        "short_zoom_bars": s.short_zoom_bars,
        "short_sl_offset": s.short_sl_offset,
        "short_td_consec_bars": s.short_td_consec_bars,
        "short_k0_vol_gate": s.short_k0_vol_gate,
        "short_delta_eff_threshold": s.short_delta_eff_threshold,
        "short_vol_sma_period": s.short_vol_sma_period,
        "short_vol_sma_mult": s.short_vol_sma_mult,
        "upper_wick_absorption_delta_eff_min": s.upper_wick_absorption_delta_eff_min,
        "upper_wick_absorption_min_vol_ratio": s.upper_wick_absorption_min_vol_ratio,
        "short_min_fee_cover_ratio": s.short_min_fee_cover_ratio,
        "short_body_floor_pct": s.short_body_floor_pct,
        "short_wick_type_a_threshold": s.short_wick_type_a_threshold,
        "short_wick_type_b_threshold": s.short_wick_type_b_threshold,
        "short_a_min_upper_wick_pct": s.short_a_min_upper_wick_pct,
        "short_rr_wick_a": s.short_rr_wick_a,
        "short_rr_wick_b": s.short_rr_wick_b,
        "short_rr_wick_c": s.short_rr_wick_c,
        # S4B 專屬
        "short_b_min_upper_wick_pct": 0.0,
        "short_b_min_k0_vol": 0.0,
        "short_b_min_runup_pct": 0.0,
        "short_b_runup_lookback": 3,
    }
    return params


def _run_with_k0_meta(
    params: dict[str, Any],
    dataset: Dataset,
    cfg: BacktestConfig,
) -> tuple[dict[str, Any], list[dict]]:
    """執行策略並回傳 (stats, k0_records)。"""
    strategy = WickReversalV4Strategy()
    for k, v in params.items():
        setattr(strategy, k, v)
    signals = strategy.on_history(dataset.klines, tick_map=dataset.tick_map)
    k0_records = list(strategy.k0_records)  # 複製
    stats = simulate_trades(signals, deepcopy(cfg))
    stats["score"] = _score_stats(stats)
    stats["side_short"] = _side_stats(stats["trade_list"], "short")
    stats["label_breakdown"] = _label_breakdown(stats["trade_list"])
    return _to_builtin(stats), k0_records


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2：S4B entry filter 網格搜尋
# ─────────────────────────────────────────────────────────────────────────────

def _phase2_grid() -> dict[str, list[Any]]:
    return {
        "short_b_min_upper_wick_pct": [0.0, 0.0006, 0.0008, 0.0010, 0.0012, 0.0015],
        "short_b_min_k0_vol": [0.0, 200.0, 300.0, 500.0, 700.0],
        "short_b_min_runup_pct": [0.0, 0.003, 0.005, 0.008, 0.010],
        "upper_wick_absorption_min_vol_ratio": [0.10, 0.15, 0.20, 0.25, 0.30],
        "short_k0_vol_gate": [200.0, 300.0, 500.0, 700.0],
        "short_delta_eff_threshold": [0.6, 0.8, 1.0, 1.2],
        "short_vol_sma_mult": [1.0, 1.2, 1.4, 1.6],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3：S4B risk management 網格搜尋
# ─────────────────────────────────────────────────────────────────────────────

def _phase3_grid() -> dict[str, list[Any]]:
    return {
        "short_rr_wick_b": [1.5, 2.0, 2.5, 3.0, 3.5],
        "short_sl_offset": [5.0, 7.5, 10.0, 12.5, 15.0],
        "short_td_consec_bars": [1, 2, 3],
        "short_min_fee_cover_ratio": [1.2, 1.5, 2.0, 2.5],
    }


# ─────────────────────────────────────────────────────────────────────────────
# coordinate descent（與 optimize_wick_reversal_v4.py 同邏輯）
# ─────────────────────────────────────────────────────────────────────────────

class S4BOptimizer:
    def __init__(self, runner: StrategyRunner):
        self.runner = runner
        self.cache: dict[tuple, dict] = {}

    def _key(self, params: dict, ds_name: str) -> tuple:
        return (ds_name, tuple(sorted(params.items())))

    def evaluate(self, params: dict, dataset: Dataset) -> dict:
        key = self._key(params, dataset.name)
        if key in self.cache:
            return self.cache[key]
        stats, _ = _run_with_k0_meta(params, dataset, self.runner.cfg)
        self.cache[key] = stats
        return stats

    def search(
        self,
        base_params: dict,
        grid: dict[str, list],
        passes: int = 2,
        top_n: int = 8,
    ) -> dict[str, Any]:
        current = deepcopy(base_params)
        best_train = self.evaluate(current, self.runner.train)

        for _ in range(passes):
            changed = False
            for name, values in grid.items():
                local_best = deepcopy(current)
                local_best_stats = best_train
                for v in values:
                    trial = deepcopy(current)
                    trial[name] = v
                    stats = self.evaluate(trial, self.runner.train)
                    if stats["score"] > local_best_stats["score"]:
                        local_best = trial
                        local_best_stats = stats
                if local_best != current:
                    current = local_best
                    best_train = local_best_stats
                    changed = True
            if not changed:
                break

        # 收集 top-N train 候選 → validation 篩選
        all_train = [
            {"params": p_tuple_to_dict(k[1]), "train": s}
            for k, s in self.cache.items()
            if k[0] == "train"
        ]
        dedup: dict[tuple, dict] = {}
        for row in all_train:
            key = tuple(sorted(row["params"].items()))
            if key not in dedup or row["train"]["score"] > dedup[key]["train"]["score"]:
                dedup[key] = row
        top = sorted(dedup.values(), key=lambda x: x["train"]["score"], reverse=True)[:top_n]

        best_final = None
        val_table = []
        for row in top:
            val_stats = self.evaluate(row["params"], self.runner.validation)
            full_stats = self.evaluate(row["params"], self.runner.full)
            entry = {
                "params": row["params"],
                "train": _brief(row["train"]),
                "validation": _brief(val_stats),
                "full": _brief(full_stats),
            }
            val_table.append(entry)
            if best_final is None:
                best_final = entry
                continue
            cur_key = (val_stats["score"], val_stats.get("profit_factor", 0))
            bst_key = (
                best_final["validation"].get("score", 0),
                best_final["validation"].get("profit_factor", 0),
            )
            if cur_key > bst_key:
                best_final = entry

        return {"best": best_final, "validation_table": val_table}


def p_tuple_to_dict(t: tuple) -> dict:
    return dict(t)


# ─────────────────────────────────────────────────────────────────────────────
# Combined 驗證
# ─────────────────────────────────────────────────────────────────────────────

def _combined_params(s4b_params: dict) -> dict:
    """把 S4B 最佳參數合入完整策略（long + S4A + S4B）。"""
    s = WickReversalV4Strategy()
    combined = {
        "enable_long": True,
        "enable_short": True,
        "enable_short_wick_a": True,
        "enable_short_wick_b": True,
        "enable_short_wick_c": False,
        # long defaults
        "long_zoom_bars": s.long_zoom_bars,
        "long_sl_offset": s.long_sl_offset,
        "long_td_consec_bars": s.long_td_consec_bars,
        "long_k0_vol_gate": s.long_k0_vol_gate,
        "long_delta_eff_threshold": s.long_delta_eff_threshold,
        "long_vol_sma_period": s.long_vol_sma_period,
        "long_vol_sma_mult": s.long_vol_sma_mult,
        "lower_wick_absorption_delta_eff_max": s.lower_wick_absorption_delta_eff_max,
        "lower_wick_absorption_min_vol_ratio": s.lower_wick_absorption_min_vol_ratio,
        "long_min_fee_cover_ratio": s.long_min_fee_cover_ratio,
        "long_rr_wick_a": s.long_rr_wick_a,
        "long_rr_wick_b": s.long_rr_wick_b,
        "long_rr_wick_c": s.long_rr_wick_c,
        # short shared defaults
        "short_zoom_bars": s.short_zoom_bars,
        "short_k0_vol_gate": s.short_k0_vol_gate,
        "short_delta_eff_threshold": s.short_delta_eff_threshold,
        "short_vol_sma_period": s.short_vol_sma_period,
        "short_a_min_upper_wick_pct": s.short_a_min_upper_wick_pct,
        "short_rr_wick_a": s.short_rr_wick_a,
        "short_rr_wick_c": s.short_rr_wick_c,
    }
    # 覆蓋 S4B 優化後的參數
    for k, v in s4b_params.items():
        combined[k] = v
    combined["enable_long"] = True
    combined["enable_short"] = True
    combined["enable_short_wick_a"] = True
    return combined


def _run_combined(params: dict, dataset: Dataset, cfg: BacktestConfig) -> dict:
    strategy = WickReversalV4Strategy()
    for k, v in params.items():
        setattr(strategy, k, v)
    signals = strategy.on_history(dataset.klines, tick_map=dataset.tick_map)
    stats = simulate_trades(signals, deepcopy(cfg))
    stats["score"] = _score_stats(stats)
    stats["side_long"] = _side_stats(stats["trade_list"], "long")
    stats["side_short"] = _side_stats(stats["trade_list"], "short")
    stats["label_breakdown"] = _label_breakdown(stats["trade_list"])
    return _to_builtin(stats)


# ─────────────────────────────────────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="S4B 專項優化器 (tick-level backtest)")
    ap.add_argument("--symbol",      default="BTCUSDT")
    ap.add_argument("--interval",    default="1m")
    ap.add_argument("--train-start", default="2025-04-14")
    ap.add_argument("--split-date",  default="2026-02-01")
    ap.add_argument("--end-date",    default="2026-04-14")
    ap.add_argument("--passes",      type=int, default=2)
    ap.add_argument("--topn",        type=int, default=8)
    ap.add_argument("--out", default="docs/reports/s4b_optimization.json")
    args = ap.parse_args()

    train_ms = _dt_to_ms(args.train_start)
    split_ms = _dt_to_ms(args.split_date)
    end_ms   = _dt_to_ms(args.end_date)

    print(f"[S4B] 載入 tick 資料：{args.symbol} …")
    runner = StrategyRunner(args.symbol, args.interval, train_ms, split_ms, end_ms)

    base_params = _s4b_base_params()

    # ── Phase 1：Baseline + 特徵分析 ────────────────────────────────────────
    print("[Phase 1] S4B baseline + 特徵分析 …")
    train_stats, train_k0 = _run_with_k0_meta(base_params, runner.train,      runner.cfg)
    val_stats,   val_k0   = _run_with_k0_meta(base_params, runner.validation,  runner.cfg)
    full_stats,  full_k0  = _run_with_k0_meta(base_params, runner.full,        runner.cfg)

    train_feature = _s4b_feature_analysis(train_k0, train_stats["trade_list"])
    val_feature   = _s4b_feature_analysis(val_k0,   val_stats["trade_list"])

    def _s4b_summary(stats: dict) -> dict:
        s4b = next(
            (v for k, v in stats["label_breakdown"].items() if k == "S4B"),
            {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
             "total_net_pnl": 0.0, "avg_net_pnl": 0.0},
        )
        exit_dist = _exit_label_dist(stats["trade_list"], "S4B")
        return {
            **s4b,
            "max_drawdown_pct": stats["max_drawdown_pct"],
            "total_return_pct": stats["total_return_pct"],
            "exit_label_dist": exit_dist,
        }

    phase1 = {
        "train":      _s4b_summary(train_stats),
        "validation": _s4b_summary(val_stats),
        "full":       _s4b_summary(full_stats),
        "feature_analysis": {
            "train": train_feature,
            "validation": val_feature,
        },
    }

    _print_phase1(phase1)

    # ── Phase 2：Entry filter 搜尋 ──────────────────────────────────────────
    print("\n[Phase 2] S4B entry filter 網格搜尋 …")
    opt = S4BOptimizer(runner)
    # 先把 baseline 放進 cache
    opt.cache[opt._key(base_params, "train")] = train_stats
    p2_result = opt.search(base_params, _phase2_grid(), passes=args.passes, top_n=args.topn)
    best_p2 = p2_result["best"]["params"]

    print(f"  Phase2 best val score: {p2_result['best']['validation'].get('score', '?'):.2f}")

    # ── Phase 3：Risk management 微調 ────────────────────────────────────────
    print("\n[Phase 3] S4B risk management 搜尋 …")
    opt3 = S4BOptimizer(runner)
    p3_result = opt3.search(best_p2, _phase3_grid(), passes=args.passes, top_n=args.topn)
    best_p3 = p3_result["best"]["params"]

    print(f"  Phase3 best val score: {p3_result['best']['validation'].get('score', '?'):.2f}")

    # Phase 3 最終分數細節
    p3_train_stats, p3_train_k0  = _run_with_k0_meta(best_p3, runner.train,      runner.cfg)
    p3_val_stats,   p3_val_k0    = _run_with_k0_meta(best_p3, runner.validation,  runner.cfg)
    p3_full_stats,  _            = _run_with_k0_meta(best_p3, runner.full,        runner.cfg)

    phase3_detail = {
        "best_params": best_p3,
        "train":      _s4b_summary(p3_train_stats),
        "validation": _s4b_summary(p3_val_stats),
        "full":       _s4b_summary(p3_full_stats),
        "feature_analysis": {
            "train": _s4b_feature_analysis(p3_train_k0, p3_train_stats["trade_list"]),
            "validation": _s4b_feature_analysis(p3_val_k0, p3_val_stats["trade_list"]),
        },
        "validation_table": p3_result["validation_table"],
    }

    # ── Phase 4：Combined 驗證 ───────────────────────────────────────────────
    print("\n[Phase 4] Combined 策略驗證 …")
    combined_base_params = _combined_params(base_params)
    combined_opt_params  = _combined_params(best_p3)

    combined_train_base = _run_combined(combined_base_params, runner.train,      runner.cfg)
    combined_val_base   = _run_combined(combined_base_params, runner.validation,  runner.cfg)
    combined_full_base  = _run_combined(combined_base_params, runner.full,        runner.cfg)

    combined_train_opt  = _run_combined(combined_opt_params,  runner.train,      runner.cfg)
    combined_val_opt    = _run_combined(combined_opt_params,  runner.validation,  runner.cfg)
    combined_full_opt   = _run_combined(combined_opt_params,  runner.full,        runner.cfg)

    def _comb_brief(s: dict) -> dict:
        return {
            **_brief(s),
            "label_breakdown": s["label_breakdown"],
            "side_long": s["side_long"],
            "side_short": s["side_short"],
        }

    phase4 = {
        "baseline_params": combined_base_params,
        "optimized_params": combined_opt_params,
        "train":      {"baseline": _comb_brief(combined_train_base), "optimized": _comb_brief(combined_train_opt)},
        "validation": {"baseline": _comb_brief(combined_val_base),   "optimized": _comb_brief(combined_val_opt)},
        "full":       {"baseline": _comb_brief(combined_full_base),   "optimized": _comb_brief(combined_full_opt)},
    }

    _print_phase4(phase4)

    # ── 組合報告 ────────────────────────────────────────────────────────────
    report = {
        "meta": {
            "symbol": args.symbol.upper(),
            "interval": args.interval,
            "train_start": args.train_start,
            "split_date": args.split_date,
            "end_date": args.end_date,
            "backtest_config": asdict(runner.cfg),
            "train_bars": len(runner.train.klines),
            "validation_bars": len(runner.validation.klines),
            "full_bars": len(runner.full.klines),
        },
        "phase1_baseline": phase1,
        "phase2_filter": {
            "best_params": best_p2,
            "validation_table": p2_result["validation_table"],
        },
        "phase3_risk": phase3_detail,
        "phase4_combined": phase4,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(_to_builtin(report), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n[完成] 報告已儲存至 {out_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Console 輸出
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(v: Any, digits: int = 2) -> str:
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def _print_phase1(p1: dict) -> None:
    print("\n" + "=" * 60)
    print("Phase 1 — S4B Baseline")
    print("=" * 60)
    hdr = f"{'':12s} {'Trades':>7} {'WinR%':>7} {'PF':>6} {'NetPnL':>10} {'DD%':>7} {'Ret%':>8}"
    print(hdr)
    print("-" * 60)
    for seg in ("train", "validation", "full"):
        d = p1[seg]
        print(
            f"{seg:12s} {d.get('trades', 0):>7} "
            f"{_fmt(d.get('win_rate', 0)):>7} "
            f"{_fmt(d.get('profit_factor', 0)):>6} "
            f"{_fmt(d.get('total_net_pnl', 0)):>10} "
            f"{_fmt(d.get('max_drawdown_pct', 0)):>7} "
            f"{_fmt(d.get('total_return_pct', 0)):>8}"
        )
    print("\n  Exit label distribution (train):")
    for label, info in p1["train"].get("exit_label_dist", {}).items():
        print(f"    {label}: {info['count']} ({info['pct']}%)")
    print("\n  K0 Feature — Win vs Loss (train):")
    fa = p1["feature_analysis"]["train"]
    for feat in ("upper_wick_pct", "wick_body_ratio", "k0_volume", "absorption_vol_ratio"):
        w = fa["win"].get(feat, {})
        l = fa["loss"].get(feat, {})
        print(f"    {feat}:")
        print(f"      Win  n={w.get('n',0):>4}  p50={_fmt(w.get('p50', 0), 5)}  mean={_fmt(w.get('mean', 0), 5)}")
        print(f"      Loss n={l.get('n',0):>4}  p50={_fmt(l.get('p50', 0), 5)}  mean={_fmt(l.get('mean', 0), 5)}")


def _print_phase4(p4: dict) -> None:
    print("\n" + "=" * 60)
    print("Phase 4 — Combined 驗證")
    print("=" * 60)
    for seg in ("train", "validation", "full"):
        b = p4[seg]["baseline"]
        o = p4[seg]["optimized"]
        print(f"\n  [{seg}]")
        print(f"    Baseline : trades={b['trades']} PF={_fmt(b['profit_factor'])} ret={_fmt(b['total_return_pct'])}% dd={_fmt(b['max_drawdown_pct'])}%")
        print(f"    Optimized: trades={o['trades']} PF={_fmt(o['profit_factor'])} ret={_fmt(o['total_return_pct'])}% dd={_fmt(o['max_drawdown_pct'])}%")
        lb_o = o.get("label_breakdown", {})
        for lbl in ("S4A", "S4B"):
            info = lb_o.get(lbl, {})
            if info:
                print(f"      {lbl}: trades={info.get('trades',0)} WR={_fmt(info.get('win_rate',0))}% PF={_fmt(info.get('profit_factor',0))}")


if __name__ == "__main__":
    main()
