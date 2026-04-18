from __future__ import annotations

import argparse
import json
from calendar import monthrange
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
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
)
from strategies.wick_reversal_v4 import WickReversalV4Strategy
from utils.optimize_wick_reversal_v4 import (
    CoordinateOptimizer,
    _brief,
    _backbone_symbol,
    _default_side_params,
    _grid_for_side,
    _label_breakdown,
    _load_backbone_klines,
    _score_stats,
    _side_stats,
    _to_builtin,
)

CONFIG_DEFAULT = PROJECT_ROOT / "config" / "wick_reversal_v4_segment_experiments.json"


def _dt_to_ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _date_plus_months(s: str, months: int) -> str:
    base = datetime.fromisoformat(s).date()
    year = base.year + (base.month - 1 + months) // 12
    month = (base.month - 1 + months) % 12 + 1
    day = min(base.day, monthrange(year, month)[1])
    return datetime(year, month, day, tzinfo=timezone.utc).date().isoformat()


def _inclusive_end_date(end_exclusive: str) -> str:
    dt = datetime.fromisoformat(end_exclusive).date() - timedelta(days=1)
    return dt.isoformat()


def _normalize_csv_arg(raw: str, allow_all: bool = False) -> list[str]:
    if allow_all and raw.strip().lower() == "all":
        return ["all"]
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class SegmentSpec:
    name: str
    label: str
    symbol: str
    start: str
    end: str


@dataclass(frozen=True)
class WindowSpec:
    start_month: int
    end_month: int


@dataclass(frozen=True)
class PlanSpec:
    name: str
    description: str
    train: WindowSpec
    test: WindowSpec


@dataclass(frozen=True)
class ResolvedWindow:
    label: str
    start: str
    end: str
    start_ms: int
    end_ms_exclusive: int


@dataclass(frozen=True)
class ExperimentSpec:
    dataset: SegmentSpec
    plan: PlanSpec
    train: ResolvedWindow
    test: ResolvedWindow


@dataclass
class DatasetSlice:
    name: str
    start: str
    end: str
    start_ms: int
    end_ms_exclusive: int
    tick_count: int
    klines: list
    tick_map: Any


def load_experiment_config(path: Path) -> tuple[dict[str, SegmentSpec], dict[str, PlanSpec], dict[str, list[str]]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    datasets = {
        row["name"]: SegmentSpec(
            name=row["name"],
            label=row.get("label", row["name"]),
            symbol=row["symbol"],
            start=row["start"],
            end=row["end"],
        )
        for row in raw["datasets"]
    }
    plans = {
        row["name"]: PlanSpec(
            name=row["name"],
            description=row.get("description", row["name"]),
            train=WindowSpec(
                start_month=int(row["train"]["start_month"]),
                end_month=int(row["train"]["end_month"]),
            ),
            test=WindowSpec(
                start_month=int(row["test"]["start_month"]),
                end_month=int(row["test"]["end_month"]),
            ),
        )
        for row in raw["plans"]
    }
    groups = {name: list(values) for name, values in raw.get("groups", {}).items()}
    return datasets, plans, groups


def _expand_names(selected: list[str], available: dict[str, Any], groups: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in selected:
        if item in groups:
            for nested in groups[item]:
                if nested not in seen:
                    out.append(nested)
                    seen.add(nested)
            continue
        if item == "all":
            for key in available:
                if key not in seen:
                    out.append(key)
                    seen.add(key)
            continue
        if item not in available:
            raise KeyError(item)
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _resolve_window(dataset: SegmentSpec, name: str, window: WindowSpec) -> ResolvedWindow:
    start = _date_plus_months(dataset.start, window.start_month)
    end = _date_plus_months(dataset.start, window.end_month)
    return ResolvedWindow(
        label=name,
        start=start,
        end=end,
        start_ms=_dt_to_ms(start),
        end_ms_exclusive=_dt_to_ms(end),
    )


def resolve_experiments(
    datasets: dict[str, SegmentSpec],
    plans: dict[str, PlanSpec],
    groups: dict[str, list[str]],
    dataset_selection: list[str],
    plan_selection: list[str],
) -> list[ExperimentSpec]:
    dataset_names = _expand_names(dataset_selection, datasets, {})
    plan_names = _expand_names(plan_selection, plans, groups)
    resolved: list[ExperimentSpec] = []
    for dataset_name in dataset_names:
        dataset = datasets[dataset_name]
        for plan_name in plan_names:
            plan = plans[plan_name]
            train = _resolve_window(dataset, "train", plan.train)
            test = _resolve_window(dataset, "test", plan.test)
            resolved.append(ExperimentSpec(dataset=dataset, plan=plan, train=train, test=test))
    return resolved


class SegmentExperimentRunner:
    def __init__(
        self,
        symbol: str,
        interval: str,
        train: ResolvedWindow,
        test: ResolvedWindow,
        tick_access: str = "range",
    ):
        meta = load_meta(symbol)
        if meta is None:
            raise RuntimeError(f"tick cache not found for {symbol}")
        self.symbol = symbol.upper()
        self.interval = interval
        self.meta = meta
        self.tick_access = tick_access
        self.train = self._build_dataset("train", train)
        self.validation = self._build_dataset("validation", test)
        full_start_ms = min(train.start_ms, test.start_ms)
        full_end_ms_exclusive = max(train.end_ms_exclusive, test.end_ms_exclusive)
        full_start = datetime.fromtimestamp(full_start_ms / 1000, tz=timezone.utc).date().isoformat()
        full_end = datetime.fromtimestamp(full_end_ms_exclusive / 1000, tz=timezone.utc).date().isoformat()
        self.full = self._build_dataset(
            "full",
            ResolvedWindow(
                label="full",
                start=full_start,
                end=full_end,
                start_ms=full_start_ms,
                end_ms_exclusive=full_end_ms_exclusive,
            ),
        )
        self.cfg = BacktestConfig(
            initial_capital=10_000.0,
            leverage=20,
            fee_mode="自訂",
            custom_fee_rate=0.00032,
            slippage_bps=0.2,
            compound=True,
        )

    def _build_dataset(self, name: str, window: ResolvedWindow) -> DatasetSlice:
        end_ms_inclusive = window.end_ms_exclusive - 1
        ticks = load_range(self.symbol, window.start_ms, end_ms_inclusive)
        klines = _load_backbone_klines(self.symbol, self.interval, window.start_ms, end_ms_inclusive)
        kline_times = [(k.open_time, k.close_time) for k in klines]
        if self.tick_access == "range":
            tick_map = build_tick_slice_accessor(ticks, kline_times)
        else:
            tick_map = build_bar_map(ticks, kline_times)
        return DatasetSlice(
            name=name,
            start=window.start,
            end=window.end,
            start_ms=window.start_ms,
            end_ms_exclusive=window.end_ms_exclusive,
            tick_count=int(len(ticks)),
            klines=klines,
            tick_map=tick_map,
        )

    def run(self, params: dict[str, Any], dataset: DatasetSlice) -> dict[str, Any]:
        strategy = WickReversalV4Strategy()
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


def _combine_params(long_result: dict[str, Any], short_result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    baseline = _default_side_params("long")
    baseline["enable_long"] = True
    baseline["enable_short"] = True
    optimized = deepcopy(baseline)
    for key, value in long_result["best"]["params"].items():
        if key.startswith("long_") or key.startswith("lower_"):
            optimized[key] = value
    for key, value in short_result["best"]["params"].items():
        if key.startswith("short_") or key.startswith("upper_"):
            optimized[key] = value
    return baseline, optimized


def run_experiment(
    spec: ExperimentSpec,
    interval: str,
    passes: int,
    topn: int,
    tick_access: str,
) -> dict[str, Any]:
    runner = SegmentExperimentRunner(
        symbol=spec.dataset.symbol,
        interval=interval,
        train=spec.train,
        test=spec.test,
        tick_access=tick_access,
    )
    long_baseline = _default_side_params("long")
    short_baseline = _default_side_params("short")
    long_optimizer = CoordinateOptimizer(runner, "long")
    short_optimizer = CoordinateOptimizer(runner, "short")
    long_result = long_optimizer.search(
        long_baseline,
        _grid_for_side("long"),
        passes=passes,
        top_n_validation=topn,
    )
    short_result = short_optimizer.search(
        short_baseline,
        _grid_for_side("short"),
        passes=passes,
        top_n_validation=topn,
    )
    combined_baseline, combined_optimized = _combine_params(long_result, short_result)
    combined_summary = {
        "train_baseline": runner.run(combined_baseline, runner.train),
        "train_optimized": runner.run(combined_optimized, runner.train),
        "validation_baseline": runner.run(combined_baseline, runner.validation),
        "validation_optimized": runner.run(combined_optimized, runner.validation),
        "full_baseline": runner.run(combined_baseline, runner.full),
        "full_optimized": runner.run(combined_optimized, runner.full),
    }
    return {
        "dataset": {
            "name": spec.dataset.name,
            "label": spec.dataset.label,
            "symbol": spec.dataset.symbol,
        },
        "plan": {
            "name": spec.plan.name,
            "description": spec.plan.description,
        },
        "windows": {
            "train": {
                "start": spec.train.start,
                "end_exclusive": spec.train.end,
                "end_inclusive": _inclusive_end_date(spec.train.end),
                "bars": len(runner.train.klines),
                "ticks": runner.train.tick_count,
            },
            "test": {
                "start": spec.test.start,
                "end_exclusive": spec.test.end,
                "end_inclusive": _inclusive_end_date(spec.test.end),
                "bars": len(runner.validation.klines),
                "ticks": runner.validation.tick_count,
            },
            "full": {
                "start": runner.full.start,
                "end_exclusive": runner.full.end,
                "end_inclusive": _inclusive_end_date(runner.full.end),
                "bars": len(runner.full.klines),
                "ticks": runner.full.tick_count,
            },
        },
        "baseline": {
            "long_train": _brief(long_optimizer.evaluate(long_baseline, runner.train)),
            "long_test": _brief(long_optimizer.evaluate(long_baseline, runner.validation)),
            "short_train": _brief(short_optimizer.evaluate(short_baseline, runner.train)),
            "short_test": _brief(short_optimizer.evaluate(short_baseline, runner.validation)),
        },
        "long": {
            "best_params": long_result["best"]["params"],
            "train": _brief(long_result["best"]["train"]),
            "test": _brief(long_result["best"]["validation"]),
            "full": _brief(long_result["best"]["full"]),
        },
        "short": {
            "best_params": short_result["best"]["params"],
            "train": _brief(short_result["best"]["train"]),
            "test": _brief(short_result["best"]["validation"]),
            "full": _brief(short_result["best"]["full"]),
        },
        "combined": {
            "optimized_params": combined_optimized,
            "train_baseline": _brief(combined_summary["train_baseline"]),
            "train_optimized": _brief(combined_summary["train_optimized"]),
            "test_baseline": _brief(combined_summary["validation_baseline"]),
            "test_optimized": _brief(combined_summary["validation_optimized"]),
            "full_baseline": _brief(combined_summary["full_baseline"]),
            "full_optimized": _brief(combined_summary["full_optimized"]),
        },
    }


def _leaderboard(results: list[dict[str, Any]], side: str, metric: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in results:
        stats = item[side]["test"] if side in {"long", "short"} else item["combined"]["test_optimized"]
        rows.append(
            {
                "dataset": item["dataset"]["name"],
                "plan": item["plan"]["name"],
                "metric": _to_builtin(stats[metric]),
                "trades": _to_builtin(stats["trades"]),
                "profit_factor": _to_builtin(stats["profit_factor"]),
                "total_net_pnl": _to_builtin(stats["total_net_pnl"]),
            }
        )
    return sorted(rows, key=lambda row: row["metric"], reverse=True)


def _print_dry_run(experiments: list[ExperimentSpec]) -> None:
    for spec in experiments:
        print(
            f"{spec.dataset.name}:{spec.plan.name} | "
            f"train {spec.train.start}~{_inclusive_end_date(spec.train.end)} | "
            f"test {spec.test.start}~{_inclusive_end_date(spec.test.end)}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Run multi-segment tick-level optimization/backtests for Wick Reversal v4."
    )
    ap.add_argument("--config", default=str(CONFIG_DEFAULT))
    ap.add_argument("--datasets", default="all", help="dataset names, comma separated, or all")
    ap.add_argument("--plans", default="default", help="plan names/groups, comma separated")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--topn", type=int, default=8)
    ap.add_argument("--tick-access", choices=["map", "range"], default="range")
    ap.add_argument("--out", default="docs/reports/wick_reversal_v4_segment_experiments.json")
    ap.add_argument("--list-datasets", action="store_true")
    ap.add_argument("--list-plans", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    config_path = Path(args.config)
    datasets, plans, groups = load_experiment_config(config_path)

    if args.list_datasets:
        for row in datasets.values():
            print(f"{row.name}: {row.label} [{row.symbol}]")
        return

    if args.list_plans:
        for row in plans.values():
            print(f"{row.name}: {row.description}")
        print("groups:")
        for name, values in groups.items():
            print(f"  {name}: {', '.join(values)}")
        return

    experiments = resolve_experiments(
        datasets,
        plans,
        groups,
        _normalize_csv_arg(args.datasets, allow_all=True),
        _normalize_csv_arg(args.plans, allow_all=True),
    )

    if args.dry_run:
        _print_dry_run(experiments)
        return

    results: list[dict[str, Any]] = []
    for idx, spec in enumerate(experiments, start=1):
        print(
            f"[{idx}/{len(experiments)}] "
            f"{spec.dataset.name}:{spec.plan.name} "
            f"train={spec.train.start}->{spec.train.end} "
            f"test={spec.test.start}->{spec.test.end}"
        )
        result = run_experiment(
            spec=spec,
            interval=args.interval,
            passes=args.passes,
            topn=args.topn,
            tick_access=args.tick_access,
        )
        results.append(result)
        test_stats = result["combined"]["test_optimized"]
        print(
            "  combined_test "
            f"trades={test_stats['trades']} "
            f"pf={test_stats['profit_factor']:.4f} "
            f"ret={test_stats['total_return_pct']:.4f} "
            f"dd={test_stats['max_drawdown_pct']:.4f} "
            f"score={test_stats['score']:.4f}"
        )

    report = {
        "meta": {
            "config": str(config_path),
            "bar_source": "exchange",
            "backbone_symbol": _backbone_symbol(results[0]["dataset"]["symbol"]) if results else "",
            "interval": args.interval,
            "passes": args.passes,
            "topn": args.topn,
            "tick_access": args.tick_access,
            "experiment_count": len(results),
        },
        "results": results,
        "leaderboards": {
            "combined_test_score": _leaderboard(results, "combined", "score"),
            "long_test_score": _leaderboard(results, "long", "score"),
            "short_test_score": _leaderboard(results, "short", "score"),
        },
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(_to_builtin(report), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"saved={out_path}")


if __name__ == "__main__":
    main()
