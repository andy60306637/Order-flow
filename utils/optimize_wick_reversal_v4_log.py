from __future__ import annotations

import argparse
import json
import math
import sys
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestConfig, simulate_trades
from core.tick_cache import build_tick_slice_accessor, load_meta, load_range
from strategies.wick_reversal_v4_log import WickReversalV4LogStrategy
from utils.optimize_wick_reversal_v4 import _dt_to_ms, _load_backbone_klines, _to_builtin

_BAND_PARAM_DIR = PROJECT_ROOT / "config" / "wick_reversal_v4_band_files" / "BTCUSDT"


def _safe_float(v: Any) -> float:
    out = float(v)
    if math.isinf(out):
        return 999.0 if out > 0 else -999.0
    if math.isnan(out):
        return 0.0
    return out


def _side_stats(trade_list: list[dict], side: str) -> dict[str, float]:
    trades = [t for t in trade_list if not t.get("skipped") and t.get("dir") == side]
    if not trades:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "total_net_pnl": 0.0,
            "avg_net_pnl": 0.0,
        }
    wins = sum(1 for t in trades if t["net_pnl"] > 0)
    gp = sum(t["net_pnl"] for t in trades if t["net_pnl"] > 0)
    gl = abs(sum(t["net_pnl"] for t in trades if t["net_pnl"] < 0))
    return {
        "trades": len(trades),
        "win_rate": wins / len(trades) * 100.0,
        "profit_factor": gp / gl if gl > 0 else 999.0,
        "total_net_pnl": sum(t["net_pnl"] for t in trades),
        "avg_net_pnl": sum(t["net_pnl"] for t in trades) / len(trades),
    }


def _label_breakdown(trade_list: list[dict]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    labels = sorted({t.get("entry_label", "") for t in trade_list if not t.get("skipped")})
    for label in labels:
        group = [t for t in trade_list if not t.get("skipped") and t.get("entry_label", "") == label]
        if not group:
            continue
        wins = sum(1 for t in group if t["net_pnl"] > 0)
        gp = sum(t["net_pnl"] for t in group if t["net_pnl"] > 0)
        gl = abs(sum(t["net_pnl"] for t in group if t["net_pnl"] < 0))
        out[label] = {
            "trades": len(group),
            "win_rate": wins / len(group) * 100.0,
            "profit_factor": gp / gl if gl > 0 else 999.0,
            "total_net_pnl": sum(t["net_pnl"] for t in group),
            "avg_net_pnl": sum(t["net_pnl"] for t in group) / len(group),
        }
    return out


def _score_stats(stats: dict[str, Any]) -> float:
    trades = int(stats["trades"])
    if trades == 0:
        return -1e9
    ret = _safe_float(stats["total_return_pct"])
    dd = _safe_float(stats["max_drawdown_pct"])
    pf = min(_safe_float(stats["profit_factor"]), 3.0)
    win = _safe_float(stats["win_rate"])
    trade_bonus = min(trades, 180) * 0.04
    trade_penalty = 0.0 if trades >= 35 else (35 - trades) * 1.5
    return ret - dd * 1.1 + (pf - 1.0) * 18.0 + win * 0.08 + trade_bonus - trade_penalty


@dataclass
class Dataset:
    name: str
    klines: list
    tick_map: Any
    tick_count: int


class StrategyRunner:
    def __init__(self, symbol: str, interval: str, train_start_ms: int, split_ms: int, end_ms: int):
        meta = load_meta(symbol)
        if meta is None:
            raise RuntimeError(f"tick cache not found for {symbol}")
        self.symbol = symbol.upper()
        self.interval = interval
        self.meta = meta
        self.train = self._build_dataset("train", train_start_ms, split_ms)
        self.validation = self._build_dataset("validation", split_ms, end_ms)
        self.full = self._build_dataset("full", train_start_ms, end_ms)
        self.cfg = BacktestConfig(
            initial_capital=10_000.0,
            leverage=20,
            fee_mode="Taker",
            custom_fee_rate=0.00032,
            slippage_bps=0.2,
            compound=True,
        )

    def _build_dataset(self, name: str, start_ms: int, end_ms: int) -> Dataset:
        ticks = load_range(self.symbol, start_ms, end_ms)
        klines = _load_backbone_klines(self.symbol, self.interval, start_ms, end_ms)
        tick_map = build_tick_slice_accessor(ticks, [(k.open_time, k.close_time) for k in klines])
        return Dataset(name=name, klines=klines, tick_map=tick_map, tick_count=int(len(ticks)))

    def run(self, params: dict[str, Any], dataset: Dataset) -> dict[str, Any]:
        strategy = WickReversalV4LogStrategy()
        for key, value in params.items():
            setattr(strategy, key, value)
        strategy.allow_bar_fallback_in_tick_mode = False
        signals = strategy.on_history(dataset.klines, tick_map=dataset.tick_map)
        stats = simulate_trades(signals, deepcopy(self.cfg))
        stats["score"] = _score_stats(stats)
        stats["side_long"] = _side_stats(stats["trade_list"], "long")
        stats["side_short"] = _side_stats(stats["trade_list"], "short")
        stats["label_breakdown"] = _label_breakdown(stats["trade_list"])
        stats["fallback_bar_count"] = getattr(strategy, "_fallback_bar_count", 0)
        return _to_builtin(stats)


class CoordinateOptimizer:
    def __init__(self, runner: StrategyRunner, label: str):
        self.runner = runner
        self.label = label
        self.eval_cache: dict[tuple[str, tuple[tuple[str, Any], ...]], dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []

    @staticmethod
    def _key(params: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
        return tuple(sorted(params.items()))

    def evaluate(self, params: dict[str, Any], dataset: Dataset) -> dict[str, Any]:
        cache_key = (dataset.name, self._key(params))
        if cache_key in self.eval_cache:
            return self.eval_cache[cache_key]
        stats = self.runner.run(params, dataset)
        self.history.append(
            {
                "dataset": dataset.name,
                "label": self.label,
                "params": deepcopy(params),
                "stats": deepcopy(stats),
            }
        )
        self.eval_cache[cache_key] = stats
        return stats

    def search(
        self,
        baseline_params: dict[str, Any],
        param_grid: dict[str, list[Any]],
        passes: int = 1,
        top_n_validation: int = 6,
    ) -> dict[str, Any]:
        current = deepcopy(baseline_params)
        best_train = self.evaluate(current, self.runner.train)

        for _ in range(passes):
            changed = False
            for name, values in param_grid.items():
                local_best_params = deepcopy(current)
                local_best_stats = best_train
                for candidate in values:
                    trial = deepcopy(current)
                    trial[name] = candidate
                    stats = self.evaluate(trial, self.runner.train)
                    if stats["score"] > local_best_stats["score"]:
                        local_best_params = trial
                        local_best_stats = stats
                if local_best_params != current:
                    current = local_best_params
                    best_train = local_best_stats
                    changed = True
            if not changed:
                break

        train_records = [r for r in self.history if r["dataset"] == "train"]
        dedup: dict[tuple[tuple[str, Any], ...], dict[str, Any]] = {}
        for record in train_records:
            dedup[self._key(record["params"])] = record

        top_candidates = sorted(
            dedup.values(),
            key=lambda x: x["stats"]["score"],
            reverse=True,
        )[:top_n_validation]

        best_final: dict[str, Any] | None = None
        validation_table: list[dict[str, Any]] = []
        for row in top_candidates:
            val_stats = self.evaluate(row["params"], self.runner.validation)
            full_stats = self.evaluate(row["params"], self.runner.full)
            merged = {
                "params": deepcopy(row["params"]),
                "train": deepcopy(row["stats"]),
                "validation": deepcopy(val_stats),
                "full": deepcopy(full_stats),
            }
            validation_table.append(merged)
            if best_final is None:
                best_final = merged
                continue
            current_key = (
                merged["validation"]["score"],
                merged["validation"]["profit_factor"],
                merged["validation"]["total_net_pnl"],
            )
            best_key = (
                best_final["validation"]["score"],
                best_final["validation"]["profit_factor"],
                best_final["validation"]["total_net_pnl"],
            )
            if current_key > best_key:
                best_final = merged

        assert best_final is not None
        return {
            "best": best_final,
            "validation_table": validation_table,
            "history": self.history,
        }


def _avg_band_value(low_band: dict[str, Any], high_band: dict[str, Any], key: str) -> Any:
    v1 = low_band[key]
    v2 = high_band[key]
    if isinstance(v1, bool):
        return v1 if v1 == v2 else v1
    if isinstance(v1, int) and not isinstance(v1, bool):
        return int(round((int(v1) + int(v2)) / 2.0))
    return (float(v1) + float(v2)) / 2.0


def _load_v4_high_efficiency_seed() -> dict[str, Any]:
    strategy = WickReversalV4LogStrategy()
    params = {
        name: getattr(strategy, name)
        for name in [
            "min_rng_pct",
            "sl_offset_pct",
            "use_dynamic_vol",
            "k0_vol_sma_period",
            "k0_vol_sma_mult",
            "enable_long",
            "long_zoom_bars",
            "long_rr_ratio",
            "long_td_consec_bars",
            "long_k0_vol_gate",
            "long_delta_eff_threshold",
            "long_vol_sma_period",
            "long_vol_sma_mult",
            "lower_wick_absorption_delta_eff_max",
            "lower_wick_absorption_min_vol_ratio",
            "lower_wick_absorption_bar_delta_max",
            "long_min_fee_cover_ratio",
            "long_body_floor_pct",
            "long_wick_type_a_threshold",
            "long_wick_type_b_threshold",
            "long_rr_wick_a",
            "long_rr_wick_b",
            "long_rr_wick_c",
            "enable_short",
            "short_zoom_bars",
            "short_rr_ratio",
            "short_td_consec_bars",
            "short_k0_vol_gate",
            "short_delta_eff_threshold",
            "short_vol_sma_period",
            "short_vol_sma_mult",
            "upper_wick_absorption_delta_eff_min",
            "upper_wick_absorption_min_vol_ratio",
            "upper_wick_absorption_bar_delta_min",
            "short_min_fee_cover_ratio",
            "short_body_floor_pct",
            "short_wick_type_a_threshold",
            "short_wick_type_b_threshold",
            "enable_short_wick_a",
            "enable_short_wick_b",
            "enable_short_wick_c",
            "short_a_min_upper_wick_pct",
            "short_rr_wick_a",
            "short_rr_wick_b",
            "short_rr_wick_c",
            "short_b_min_upper_wick_pct",
            "short_b_min_k0_vol",
            "short_b_min_runup_pct",
            "short_b_runup_lookback",
            "taker_fee_rate",
            "slippage_rate",
        ]
    }

    low_path = _BAND_PARAM_DIR / "70000_80000.json"
    high_path = _BAND_PARAM_DIR / "80000_90000.json"
    low_band = json.loads(low_path.read_text(encoding="utf-8"))["params"]
    high_band = json.loads(high_path.read_text(encoding="utf-8"))["params"]

    passthrough_keys = [
        "long_zoom_bars",
        "long_rr_ratio",
        "long_td_consec_bars",
        "long_k0_vol_gate",
        "long_delta_eff_threshold",
        "long_vol_sma_period",
        "long_vol_sma_mult",
        "lower_wick_absorption_delta_eff_max",
        "lower_wick_absorption_min_vol_ratio",
        "lower_wick_absorption_bar_delta_max",
        "long_min_fee_cover_ratio",
        "long_body_floor_pct",
        "long_wick_type_a_threshold",
        "long_wick_type_b_threshold",
        "long_rr_wick_a",
        "long_rr_wick_b",
        "long_rr_wick_c",
        "short_zoom_bars",
        "short_rr_ratio",
        "short_td_consec_bars",
        "short_k0_vol_gate",
        "short_delta_eff_threshold",
        "short_vol_sma_period",
        "short_vol_sma_mult",
        "upper_wick_absorption_delta_eff_min",
        "upper_wick_absorption_min_vol_ratio",
        "upper_wick_absorption_bar_delta_min",
        "short_min_fee_cover_ratio",
        "short_body_floor_pct",
        "short_wick_type_a_threshold",
        "short_wick_type_b_threshold",
        "enable_short_wick_a",
        "enable_short_wick_b",
        "enable_short_wick_c",
        "short_a_min_upper_wick_pct",
        "short_rr_wick_a",
        "short_rr_wick_b",
        "short_rr_wick_c",
        "short_b_min_upper_wick_pct",
        "short_b_min_k0_vol",
        "short_b_min_runup_pct",
        "short_b_runup_lookback",
        "taker_fee_rate",
        "slippage_rate",
    ]
    for key in passthrough_keys:
        params[key] = _avg_band_value(low_band, high_band, key)

    band_centers = [75_000.0, 85_000.0]
    long_sl_pct = sum(
        band[key] / center
        for band, key, center in [
            (low_band, "long_sl_offset", band_centers[0]),
            (high_band, "long_sl_offset", band_centers[1]),
        ]
    ) / 2.0
    short_sl_pct = sum(
        band[key] / center
        for band, key, center in [
            (low_band, "short_sl_offset", band_centers[0]),
            (high_band, "short_sl_offset", band_centers[1]),
        ]
    ) / 2.0
    params["sl_offset_pct"] = round((long_sl_pct + short_sl_pct) / 2.0, 6)
    params["use_dynamic_vol"] = False
    params["k0_vol_sma_period"] = 20
    params["k0_vol_sma_mult"] = 1.4
    params["min_rng_pct"] = getattr(strategy, "min_rng_pct")
    params["enable_long"] = True
    params["enable_short"] = True
    return params


def _shared_grid() -> dict[str, list[Any]]:
    return {
        "min_rng_pct": [0.0006, 0.0008, 0.0010, 0.0012, 0.0014],
        "sl_offset_pct": [0.00016, 0.00020, 0.00024, 0.00028, 0.00032],
        "use_dynamic_vol": [False, True],
        "k0_vol_sma_period": [10, 20, 30],
        "k0_vol_sma_mult": [1.0, 1.2, 1.4, 1.6, 2.0],
    }


def _long_grid() -> dict[str, list[Any]]:
    return {
        "long_td_consec_bars": [1, 2, 3],
        "long_k0_vol_gate": [500.0, 800.0, 1000.0, 1200.0],
        "long_delta_eff_threshold": [0.6, 0.8, 1.0],
        "long_vol_sma_mult": [1.0, 1.2, 1.4],
        "lower_wick_absorption_delta_eff_max": [0.0, -0.05, -0.10],
        "lower_wick_absorption_min_vol_ratio": [0.10, 0.15, 0.20],
        "long_min_fee_cover_ratio": [1.2, 1.5, 2.0],
        "long_rr_wick_a": [3.0, 3.5, 4.0],
        "long_rr_wick_b": [1.5, 2.0, 2.5],
        "long_rr_wick_c": [1.0, 1.5, 2.0],
    }


def _short_grid() -> dict[str, list[Any]]:
    return {
        "short_td_consec_bars": [1, 2, 3],
        "short_k0_vol_gate": [200.0, 300.0, 500.0, 800.0],
        "short_delta_eff_threshold": [0.6, 0.8, 1.0],
        "short_vol_sma_mult": [1.0, 1.2, 1.4, 1.6],
        "upper_wick_absorption_delta_eff_min": [0.0, 0.05, 0.10],
        "upper_wick_absorption_min_vol_ratio": [0.10, 0.15, 0.20],
        "short_min_fee_cover_ratio": [1.5, 2.0, 2.5],
        "short_a_min_upper_wick_pct": [0.0010, 0.0011, 0.0012, 0.0014],
        "short_rr_wick_a": [3.0, 3.5, 4.5],
        "short_rr_wick_b": [1.5, 2.0, 2.5],
        "short_rr_wick_c": [1.0, 1.5, 2.0],
    }


def _brief(stats: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "trades",
        "win_rate",
        "profit_factor",
        "total_net_pnl",
        "final_equity",
        "total_return_pct",
        "max_drawdown_pct",
        "score",
    ]
    return {k: _to_builtin(stats[k]) for k in keys}


def main() -> None:
    ap = argparse.ArgumentParser(description="Optimize Wick Reversal v4 Log on 2025-04 to 2026-04 tick data.")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--train-start", default="2025-04-14")
    ap.add_argument("--split-date", default="2026-01-01")
    ap.add_argument("--end-date", default="2026-04-14")
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--topn", type=int, default=6)
    ap.add_argument("--out", default="docs/reports/wick_reversal_v4_log_optimization.json")
    args = ap.parse_args()

    runner = StrategyRunner(
        args.symbol,
        args.interval,
        _dt_to_ms(args.train_start),
        _dt_to_ms(args.split_date),
        _dt_to_ms(args.end_date),
    )

    seed = _load_v4_high_efficiency_seed()

    shared_optimizer = CoordinateOptimizer(runner, "shared")
    shared_result = shared_optimizer.search(seed, _shared_grid(), passes=args.passes, top_n_validation=args.topn)
    shared_best = deepcopy(shared_result["best"]["params"])

    long_baseline = deepcopy(shared_best)
    long_baseline["enable_long"] = True
    long_baseline["enable_short"] = False
    short_baseline = deepcopy(shared_best)
    short_baseline["enable_long"] = False
    short_baseline["enable_short"] = True

    long_optimizer = CoordinateOptimizer(runner, "long")
    short_optimizer = CoordinateOptimizer(runner, "short")
    long_result = long_optimizer.search(long_baseline, _long_grid(), passes=args.passes, top_n_validation=args.topn)
    short_result = short_optimizer.search(short_baseline, _short_grid(), passes=args.passes, top_n_validation=args.topn)

    combined_optimized = deepcopy(shared_best)
    for key, value in long_result["best"]["params"].items():
        if key.startswith("long_") or key.startswith("lower_"):
            combined_optimized[key] = value
    for key, value in short_result["best"]["params"].items():
        if key.startswith("short_") or key.startswith("upper_"):
            combined_optimized[key] = value
    combined_optimized["enable_long"] = True
    combined_optimized["enable_short"] = True

    combined_baseline = deepcopy(seed)
    combined_summary = {
        "train_baseline": runner.run(combined_baseline, runner.train),
        "train_optimized": runner.run(combined_optimized, runner.train),
        "validation_baseline": runner.run(combined_baseline, runner.validation),
        "validation_optimized": runner.run(combined_optimized, runner.validation),
        "full_baseline": runner.run(combined_baseline, runner.full),
        "full_optimized": runner.run(combined_optimized, runner.full),
    }

    report = {
        "meta": {
            "symbol": args.symbol.upper(),
            "interval": args.interval,
            "train_start": args.train_start,
            "split_date": args.split_date,
            "end_date": args.end_date,
            "backtest_config": asdict(runner.cfg),
            "data_meta": runner.meta,
            "train_bars": len(runner.train.klines),
            "validation_bars": len(runner.validation.klines),
            "full_bars": len(runner.full.klines),
            "train_ticks": runner.train.tick_count,
            "validation_ticks": runner.validation.tick_count,
            "full_ticks": runner.full.tick_count,
            "seed_source": ["70000_80000.json", "80000_90000.json"],
        },
        "seed_params": seed,
        "shared": {
            "best_params": shared_result["best"]["params"],
            "train": _brief(shared_result["best"]["train"]),
            "validation": _brief(shared_result["best"]["validation"]),
            "full": _brief(shared_result["best"]["full"]),
            "validation_table": [
                {
                    "params": row["params"],
                    "train": _brief(row["train"]),
                    "validation": _brief(row["validation"]),
                }
                for row in shared_result["validation_table"]
            ],
        },
        "long": {
            "best_params": long_result["best"]["params"],
            "train": _brief(long_result["best"]["train"]),
            "validation": _brief(long_result["best"]["validation"]),
            "full": _brief(long_result["best"]["full"]),
            "validation_table": [
                {
                    "params": row["params"],
                    "train": _brief(row["train"]),
                    "validation": _brief(row["validation"]),
                }
                for row in long_result["validation_table"]
            ],
            "label_breakdown_full": long_result["best"]["full"]["label_breakdown"],
        },
        "short": {
            "best_params": short_result["best"]["params"],
            "train": _brief(short_result["best"]["train"]),
            "validation": _brief(short_result["best"]["validation"]),
            "full": _brief(short_result["best"]["full"]),
            "validation_table": [
                {
                    "params": row["params"],
                    "train": _brief(row["train"]),
                    "validation": _brief(row["validation"]),
                }
                for row in short_result["validation_table"]
            ],
            "label_breakdown_full": short_result["best"]["full"]["label_breakdown"],
        },
        "combined": {
            "optimized_params": combined_optimized,
            "train_baseline": _brief(combined_summary["train_baseline"]),
            "train_optimized": _brief(combined_summary["train_optimized"]),
            "validation_baseline": _brief(combined_summary["validation_baseline"]),
            "validation_optimized": _brief(combined_summary["validation_optimized"]),
            "full_baseline": _brief(combined_summary["full_baseline"]),
            "full_optimized": _brief(combined_summary["full_optimized"]),
            "full_label_breakdown_optimized": combined_summary["full_optimized"]["label_breakdown"],
        },
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(_to_builtin(report), indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"saved={out_path}")
    print("seed_full", _brief(combined_summary["full_baseline"]))
    print("shared_val_best", _brief(shared_result["best"]["validation"]))
    print("long_val_best", _brief(long_result["best"]["validation"]))
    print("short_val_best", _brief(short_result["best"]["validation"]))
    print("combined_val_optimized", _brief(combined_summary["validation_optimized"]))


if __name__ == "__main__":
    main()
