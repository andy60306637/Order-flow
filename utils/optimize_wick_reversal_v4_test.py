from __future__ import annotations

import argparse
import json
import math
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestConfig, simulate_trades
from core.tick_cache import build_tick_slice_accessor, load_meta, load_range
from strategies.wick_reversal_v4_test import WickReversalV4TestStrategy
from utils.optimize_wick_reversal_v4 import _dt_to_ms, _load_backbone_klines, _to_builtin

SEARCH_WINDOWS = [
    {
        "label": "low_2023_06",
        "symbol": "BTCUSDT_20230414_20240413",
        "start": "2023-06-05",
        "end": "2023-06-19",
        "regime": "low",
    },
    {
        "label": "low_2023_10",
        "symbol": "BTCUSDT_20230414_20240413",
        "start": "2023-10-01",
        "end": "2023-10-15",
        "regime": "low",
    },
    {
        "label": "mid_2024_06",
        "symbol": "BTCUSDT_20240414_20250413",
        "start": "2024-06-03",
        "end": "2024-06-17",
        "regime": "mid",
    },
    {
        "label": "mid_2024_10",
        "symbol": "BTCUSDT_20240414_20250413",
        "start": "2024-10-01",
        "end": "2024-10-15",
        "regime": "mid",
    },
    {
        "label": "high_2025_05",
        "symbol": "BTCUSDT",
        "start": "2025-05-01",
        "end": "2025-05-15",
        "regime": "high",
    },
    {
        "label": "high_2025_11",
        "symbol": "BTCUSDT",
        "start": "2025-11-01",
        "end": "2025-11-15",
        "regime": "high",
    },
]

FULL_YEAR_WINDOWS = [
    {
        "label": "Y1 (2023-04 ~ 2024-04)",
        "symbol": "BTCUSDT_20230414_20240413",
        "start": "2023-04-14",
        "end": "2024-04-14",
    },
    {
        "label": "Y2 (2024-04 ~ 2025-04)",
        "symbol": "BTCUSDT_20240414_20250413",
        "start": "2024-04-14",
        "end": "2025-04-14",
    },
    {
        "label": "Y3 (2025-04 ~ 2026-04)",
        "symbol": "BTCUSDT",
        "start": "2025-04-14",
        "end": "2026-04-14",
    },
]

SEARCH_GRID = {
    "long_zoom_bars": [1, 2],
    "long_sl_pct_floor": [0.0005, 0.0008, 0.0010, 0.0012],
    "long_sl_wick_mult": [0.10, 0.15, 0.20, 0.30],
    "long_sl_pct_cap": [0.002, 0.003, 0.004],
    "long_td_consec_bars": [2, 3, 4],
    "long_k0_notional_usd": [54_000_000.0, 72_000_000.0, 90_000_000.0, 108_000_000.0],
    "long_delta_eff_threshold": [0.6, 0.8, 1.0, 1.2],
    "long_vol_sma_mult": [1.0, 1.2, 1.4, 1.6],
    "lower_wick_absorption_delta_eff_max": [0.0, -0.05, -0.10],
    "lower_wick_absorption_min_vol_ratio": [0.10, 0.15, 0.20, 0.25],
    "long_min_fee_cover_ratio": [1.2, 1.5, 2.0, 2.5],
    "long_rr_wick_a": [2.0, 2.5, 3.0],
    "long_rr_wick_b": [1.0, 1.5, 2.0],
    "long_rr_wick_c": [0.8, 1.0, 1.2, 1.5],
    "short_zoom_bars": [1, 2],
    "short_sl_pct_floor": [0.0008, 0.0010, 0.0012, 0.0015],
    "short_sl_wick_mult": [0.10, 0.15, 0.20, 0.25],
    "short_sl_pct_cap": [0.003, 0.004, 0.005],
    "short_td_consec_bars": [1, 2, 3],
    "short_k0_notional_usd": [18_000_000.0, 27_000_000.0, 36_000_000.0, 45_000_000.0],
    "short_delta_eff_threshold": [0.6, 0.8, 1.0, 1.2],
    "short_vol_sma_mult": [1.0, 1.2, 1.4, 1.6, 1.8],
    "upper_wick_absorption_delta_eff_min": [0.0, 0.05, 0.10],
    "upper_wick_absorption_min_vol_ratio": [0.10, 0.15, 0.20, 0.25],
    "short_min_fee_cover_ratio": [1.5, 2.0, 2.5, 3.0],
    "short_a_min_upper_wick_pct": [0.0, 0.0008, 0.0010, 0.0012],
    "short_rr_wick_a": [2.5, 3.0, 4.5],
    "short_rr_wick_b": [1.0, 1.5, 2.0],
    "short_rr_wick_c": [0.8, 1.0, 1.5],
}

BT_CFG = BacktestConfig(
    initial_capital=1650.0,
    max_loss_pct=0.02,
    leverage=20,
    fee_mode="Taker",
    custom_fee_rate=0.00032,
    slippage_bps=0.2,
    funding_rate=0.0,
    maint_margin=0.005,
    compound=True,
)


@dataclass
class Dataset:
    label: str
    symbol: str
    regime: str
    start: str
    end: str
    klines: list
    tick_map: Any
    tick_count: int


def _safe_float(value: Any) -> float:
    out = float(value)
    if math.isnan(out):
        return 0.0
    if math.isinf(out):
        return 999.0 if out > 0 else -999.0
    return out


def _default_params() -> dict[str, Any]:
    strategy = WickReversalV4TestStrategy()
    fields = [
        "enable_long",
        "enable_short",
        "long_zoom_bars",
        "long_sl_pct_floor",
        "long_sl_wick_mult",
        "long_sl_pct_cap",
        "long_td_consec_bars",
        "long_k0_notional_usd",
        "long_delta_eff_threshold",
        "long_vol_sma_period",
        "long_vol_sma_mult",
        "lower_wick_absorption_delta_eff_max",
        "lower_wick_absorption_min_vol_ratio",
        "long_min_fee_cover_ratio",
        "long_body_floor_pct",
        "long_wick_type_a_threshold",
        "long_wick_type_b_threshold",
        "long_rr_wick_a",
        "long_rr_wick_b",
        "long_rr_wick_c",
        "short_zoom_bars",
        "short_sl_pct_floor",
        "short_sl_wick_mult",
        "short_sl_pct_cap",
        "short_td_consec_bars",
        "short_k0_notional_usd",
        "short_delta_eff_threshold",
        "short_vol_sma_period",
        "short_vol_sma_mult",
        "upper_wick_absorption_delta_eff_min",
        "upper_wick_absorption_min_vol_ratio",
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
    ]
    return {name: getattr(strategy, name) for name in fields}


def _load_dataset(spec: dict[str, Any]) -> Dataset:
    symbol = spec["symbol"]
    if load_meta(symbol) is None:
        raise RuntimeError(f"tick cache not found for {symbol}")
    start_ms = _dt_to_ms(spec["start"])
    end_ms_inclusive = _dt_to_ms(spec["end"]) - 1
    ticks = load_range(symbol, start_ms, end_ms_inclusive)
    klines = _load_backbone_klines(symbol, "1m", start_ms, end_ms_inclusive)
    tick_map = build_tick_slice_accessor(ticks, [(k.open_time, k.close_time) for k in klines])
    return Dataset(
        label=spec["label"],
        symbol=symbol,
        regime=spec.get("regime", "full"),
        start=spec["start"],
        end=spec["end"],
        klines=klines,
        tick_map=tick_map,
        tick_count=int(len(ticks)),
    )


def _run_dataset(params: dict[str, Any], dataset: Dataset) -> dict[str, Any]:
    strategy = WickReversalV4TestStrategy()
    strategy.allow_bar_fallback_in_tick_mode = False
    for key, value in params.items():
        setattr(strategy, key, value)
    signals = strategy.on_history(dataset.klines, tick_map=dataset.tick_map)
    stats = simulate_trades(signals, deepcopy(BT_CFG))
    return {
        "trades": int(stats["trades"]),
        "win_rate": _safe_float(stats["win_rate"]),
        "profit_factor": _safe_float(stats["profit_factor"]),
        "total_net_pnl": _safe_float(stats["total_net_pnl"]),
        "total_return_pct": _safe_float(stats["total_return_pct"]),
        "max_drawdown_pct": _safe_float(stats["max_drawdown_pct"]),
    }


def _dataset_score(stats: dict[str, Any]) -> float:
    trades = int(stats["trades"])
    if trades == 0:
        return -1e9
    pf = min(_safe_float(stats["profit_factor"]), 3.0)
    wr = _safe_float(stats["win_rate"])
    dd = _safe_float(stats["max_drawdown_pct"])
    pnl = _safe_float(stats["total_net_pnl"])
    trade_bonus = min(trades, 18) * 0.25
    trade_penalty = 0.0 if trades >= 6 else (6 - trades) * 6.0
    return (
        (pf - 1.0) * 55.0
        + wr * 0.55
        - dd * 0.30
        + pnl * 0.05
        + trade_bonus
        - trade_penalty
    )


def _aggregate_score(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return -1e9
    pf_values = [min(_safe_float(row["profit_factor"]), 3.0) for row in rows]
    wr_values = [_safe_float(row["win_rate"]) for row in rows]
    dd_values = [_safe_float(row["max_drawdown_pct"]) for row in rows]
    pnl_values = [_safe_float(row["total_net_pnl"]) for row in rows]
    trade_values = [int(row["trades"]) for row in rows]
    regime_map: dict[str, list[dict[str, Any]]] = {"low": [], "mid": [], "high": []}
    for row in rows:
        regime_map.get(row["regime"], []).append(row)
    regime_scores = []
    for regime_rows in regime_map.values():
        if not regime_rows:
            continue
        regime_scores.append(np.mean([_dataset_score(row) for row in regime_rows]))
    worst_regime_score = min(regime_scores) if regime_scores else -1e9
    negative_windows = sum(1 for pf in pf_values if pf < 1.0)
    sparse_windows = sum(1 for trades in trade_values if trades < 6)
    return (
        np.mean([_dataset_score(row) for row in rows])
        + worst_regime_score * 0.9
        + np.mean(pf_values) * 28.0
        + min(pf_values) * 32.0
        + np.mean(wr_values) * 0.22
        - np.mean(dd_values) * 0.18
        + np.mean(np.clip(pnl_values, -300.0, 300.0)) * 0.04
        + np.mean(np.clip(trade_values, 0, 18)) * 0.20
        - negative_windows * 14.0
        - sparse_windows * 8.0
        - np.std(pf_values) * 10.0
    )


class WindowOptimizer:
    def __init__(self, datasets: list[Dataset]):
        self.datasets = datasets
        self.cache: dict[tuple[tuple[str, Any], ...], dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []

    @staticmethod
    def _key(params: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
        return tuple(sorted(params.items()))

    def evaluate(self, params: dict[str, Any]) -> dict[str, Any]:
        key = self._key(params)
        if key in self.cache:
            return self.cache[key]
        windows: list[dict[str, Any]] = []
        for dataset in self.datasets:
            stats = _run_dataset(params, dataset)
            row = {
                "label": dataset.label,
                "symbol": dataset.symbol,
                "regime": dataset.regime,
                **stats,
            }
            row["score"] = _dataset_score(row)
            windows.append(row)
        result = {
            "params": deepcopy(params),
            "windows": windows,
            "score": _aggregate_score(windows),
        }
        self.cache[key] = result
        self.history.append(result)
        return result

    def search(self, baseline_params: dict[str, Any], grid: dict[str, list[Any]], passes: int, topn: int) -> dict[str, Any]:
        current = deepcopy(baseline_params)
        best = self.evaluate(current)
        for pass_idx in range(passes):
            print(f"[search] pass {pass_idx + 1}/{passes} baseline_score={best['score']:.4f}", flush=True)
            changed = False
            for step_idx, (name, values) in enumerate(grid.items(), start=1):
                local_best = best
                local_best_params = deepcopy(current)
                for candidate in values:
                    trial = deepcopy(current)
                    trial[name] = candidate
                    result = self.evaluate(trial)
                    if result["score"] > local_best["score"]:
                        local_best = result
                        local_best_params = trial
                print(
                    f"[search] step {step_idx:02d}/{len(grid)} {name} -> {local_best_params[name]} "
                    f"score={local_best['score']:.4f}",
                    flush=True,
                )
                if local_best_params != current:
                    current = local_best_params
                    best = local_best
                    changed = True
            if not changed:
                break
        dedup = {self._key(row["params"]): row for row in self.history}
        top_candidates = sorted(dedup.values(), key=lambda row: row["score"], reverse=True)[:topn]
        return {
            "baseline": self.evaluate(baseline_params),
            "best": best,
            "top_candidates": top_candidates,
        }


def _full_year_score(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return -1e9
    pf_values = [min(_safe_float(row["profit_factor"]), 3.0) for row in rows]
    wr_values = [_safe_float(row["win_rate"]) for row in rows]
    dd_values = [_safe_float(row["max_drawdown_pct"]) for row in rows]
    pnl_values = [_safe_float(row["total_net_pnl"]) for row in rows]
    trades = [int(row["trades"]) for row in rows]
    negative_years = sum(1 for pf in pf_values if pf < 1.0)
    return (
        np.mean(pf_values) * 85.0
        + min(pf_values) * 95.0
        + np.mean(wr_values) * 0.65
        - np.mean(dd_values) * 0.30
        + np.mean(np.clip(pnl_values, -1200.0, 1200.0)) * 0.02
        + np.mean(np.clip(trades, 0, 240)) * 0.05
        - negative_years * 26.0
        - np.std(pf_values) * 14.0
    )


def _validate_full_years(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for idx, candidate in enumerate(candidates, start=1):
        print(f"[validate] candidate {idx}/{len(candidates)}", flush=True)
        params = candidate["params"]
        years: list[dict[str, Any]] = []
        for spec in FULL_YEAR_WINDOWS:
            print(f"[validate]   loading {spec['label']} {spec['start']}->{spec['end']}", flush=True)
            dataset = _load_dataset(spec)
            stats = _run_dataset(params, dataset)
            years.append(
                {
                    "label": dataset.label,
                    "symbol": dataset.symbol,
                    **stats,
                }
            )
        reports.append(
            {
                "rank": idx,
                "params": deepcopy(params),
                "search_score": candidate["score"],
                "years": years,
                "full_year_score": _full_year_score(years),
            }
        )
    reports.sort(key=lambda row: row["full_year_score"], reverse=True)
    return reports


def _brief_windows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "label": row["label"],
                "regime": row.get("regime", "full"),
                "trades": row["trades"],
                "win_rate": row["win_rate"],
                "profit_factor": row["profit_factor"],
                "total_net_pnl": row["total_net_pnl"],
                "max_drawdown_pct": row["max_drawdown_pct"],
                "score": row["score"],
            }
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Optimize Wick Reversal v4_test across price-regime windows, then validate on full years.")
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--topn", type=int, default=4)
    ap.add_argument("--out", default="docs/reports/wick_reversal_v4_test_optimization.json")
    args = ap.parse_args()

    datasets: list[Dataset] = []
    for spec in SEARCH_WINDOWS:
        print(f"[load] {spec['label']} {spec['start']}->{spec['end']}", flush=True)
        datasets.append(_load_dataset(spec))
    optimizer = WindowOptimizer(datasets)
    baseline = _default_params()
    search_result = optimizer.search(baseline, SEARCH_GRID, passes=args.passes, topn=args.topn)
    validations = _validate_full_years(search_result["top_candidates"])

    report = {
        "meta": {
            "strategy": "Wick Reversal 1m v4_test",
            "passes": args.passes,
            "topn": args.topn,
            "backtest_config": asdict(BT_CFG),
            "search_windows": [
                {
                    "label": dataset.label,
                    "symbol": dataset.symbol,
                    "regime": dataset.regime,
                    "start": dataset.start,
                    "end_exclusive": dataset.end,
                    "bars": len(dataset.klines),
                    "ticks": dataset.tick_count,
                }
                for dataset in datasets
            ],
        },
        "baseline": {
            "params": search_result["baseline"]["params"],
            "score": search_result["baseline"]["score"],
            "windows": _brief_windows(search_result["baseline"]["windows"]),
        },
        "best_search": {
            "params": search_result["best"]["params"],
            "score": search_result["best"]["score"],
            "windows": _brief_windows(search_result["best"]["windows"]),
        },
        "top_candidates": [
            {
                "params": row["params"],
                "score": row["score"],
                "windows": _brief_windows(row["windows"]),
            }
            for row in search_result["top_candidates"]
        ],
        "full_year_validation": validations,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(_to_builtin(report), indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"loaded_search_windows={len(datasets)}")
    for dataset in datasets:
        print(
            f"  {dataset.label}: bars={len(dataset.klines)} ticks={dataset.tick_count:,} "
            f"regime={dataset.regime} {dataset.start}->{dataset.end}"
        )
    print(f"baseline_score={search_result['baseline']['score']:.4f}")
    print(f"best_search_score={search_result['best']['score']:.4f}")
    print(f"saved={out_path}")
    if validations:
        best = validations[0]
        print(f"best_full_year_score={best['full_year_score']:.4f}")
        print("best_full_year_params", json.dumps(_to_builtin(best["params"]), ensure_ascii=False))


if __name__ == "__main__":
    main()
