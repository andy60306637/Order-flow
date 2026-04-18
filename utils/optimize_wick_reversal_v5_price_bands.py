from __future__ import annotations

import argparse
import json
import math
import sys
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestConfig, compute_subset_stats, simulate_trades
from core.tick_cache import build_tick_slice_accessor, load_meta, load_range
from strategies.wick_reversal_v5 import WickReversalV5Strategy
from utils.optimize_wick_reversal_v4 import _dt_to_ms, _load_backbone_klines, _to_builtin

SHARDS = [
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

LONG_KEYS = [
    "long_sl_pct_floor",
    "long_sl_wick_mult",
    "long_sl_pct_cap",
    "long_k0_vol_gate",
    "long_rr_wick_a",
    "long_rr_wick_b",
    "long_rr_wick_c",
    "long_min_fee_cover_ratio",
]

SHORT_KEYS = [
    "short_sl_pct_floor",
    "short_sl_wick_mult",
    "short_sl_pct_cap",
    "short_k0_vol_gate",
    "short_rr_wick_a",
    "short_rr_wick_b",
    "short_rr_wick_c",
    "short_min_fee_cover_ratio",
]

LONG_SL_PROFILES = [
    {"long_sl_pct_floor": 0.0003, "long_sl_wick_mult": 0.20, "long_sl_pct_cap": 0.003},
    {"long_sl_pct_floor": 0.0005, "long_sl_wick_mult": 0.20, "long_sl_pct_cap": 0.003},
    {"long_sl_pct_floor": 0.0008, "long_sl_wick_mult": 0.20, "long_sl_pct_cap": 0.003},
    {"long_sl_pct_floor": 0.0010, "long_sl_wick_mult": 0.20, "long_sl_pct_cap": 0.002},
]

LONG_RR_PROFILES = [
    {"long_rr_wick_a": 2.5, "long_rr_wick_b": 2.5, "long_rr_wick_c": 2.0},
    {"long_rr_wick_a": 3.0, "long_rr_wick_b": 2.0, "long_rr_wick_c": 1.0},
    {"long_rr_wick_a": 3.0, "long_rr_wick_b": 1.5, "long_rr_wick_c": 1.0},
    {"long_rr_wick_a": 3.5, "long_rr_wick_b": 2.0, "long_rr_wick_c": 1.5},
]

SHORT_SL_PROFILES = [
    {"short_sl_pct_floor": 0.0005, "short_sl_wick_mult": 0.20, "short_sl_pct_cap": 0.003},
    {"short_sl_pct_floor": 0.0008, "short_sl_wick_mult": 0.15, "short_sl_pct_cap": 0.003},
    {"short_sl_pct_floor": 0.0010, "short_sl_wick_mult": 0.20, "short_sl_pct_cap": 0.003},
    {"short_sl_pct_floor": 0.0010, "short_sl_wick_mult": 0.20, "short_sl_pct_cap": 0.004},
]

SHORT_RR_PROFILES = [
    {"short_rr_wick_a": 4.5, "short_rr_wick_b": 1.0, "short_rr_wick_c": 2.0},
    {"short_rr_wick_a": 4.5, "short_rr_wick_b": 2.5, "short_rr_wick_c": 2.0},
    {"short_rr_wick_a": 3.0, "short_rr_wick_b": 2.0, "short_rr_wick_c": 1.0},
    {"short_rr_wick_a": 2.5, "short_rr_wick_b": 1.5, "short_rr_wick_c": 0.8},
]

BT_CFG = BacktestConfig(
    initial_capital=10_000.0,
    leverage=20,
    fee_mode="Taker",
    custom_fee_rate=0.00032,
    slippage_bps=0.2,
    compound=True,
)


def _safe_float(value: Any) -> float:
    out = float(value)
    if math.isnan(out):
        return 0.0
    if math.isinf(out):
        return 999.0 if out > 0 else -999.0
    return out


def _score_stats(stats: dict[str, Any]) -> float:
    trades = int(stats["trades"])
    if trades == 0:
        return -1e9
    ret = _safe_float(stats["total_net_pnl"])
    dd = _safe_float(stats["max_drawdown_pct"])
    pf = min(_safe_float(stats["profit_factor"]), 3.0)
    win = _safe_float(stats["win_rate"])
    trade_bonus = min(trades, 80) * 0.08
    trade_penalty = 0.0 if trades >= 12 else (12 - trades) * 4.0
    return ret - dd * 0.9 + (pf - 1.0) * 18.0 + win * 0.06 + trade_bonus - trade_penalty


def _brief(stats: dict[str, Any]) -> str:
    return (
        f"trades={int(stats['trades'])} "
        f"wr={stats['win_rate']:.1f}% "
        f"pf={stats['profit_factor']:.3f} "
        f"pnl={stats['total_net_pnl']:.2f} "
        f"dd={stats['max_drawdown_pct']:.2f}% "
        f"score={stats['score']:.2f}"
    )


def _band_index(price: float, band_floor: float, band_size: float) -> int:
    shifted = max(price - band_floor, 0.0)
    return int(shifted // band_size)


def _band_bounds(band_idx: int, band_floor: float, band_size: float) -> tuple[float, float]:
    low = band_floor + band_idx * band_size
    return low, low + band_size


def _split_trades(trades: list[dict], train_ratio: float) -> tuple[list[dict], list[dict]]:
    if len(trades) <= 1:
        return trades, []
    cut = int(len(trades) * train_ratio)
    cut = max(1, min(len(trades) - 1, cut))
    return trades[:cut], trades[cut:]


def _stats_from_trades(trades: list[dict]) -> dict[str, Any]:
    stats = compute_subset_stats(trades)
    stats["score"] = _score_stats(stats)
    return stats


@dataclass
class Dataset:
    label: str
    symbol: str
    ticks: Any
    klines: list
    tick_count: int
    min_price: float
    max_price: float


@dataclass
class Segment:
    dataset_label: str
    klines: list
    tick_map: Any


def _load_dataset(shard: dict[str, str]) -> Dataset:
    symbol = shard["symbol"]
    if load_meta(symbol) is None:
        raise RuntimeError(f"tick cache not found for {symbol}")
    start_ms = _dt_to_ms(shard["start"])
    end_ms = _dt_to_ms(shard["end"])
    ticks = load_range(symbol, start_ms, end_ms)
    klines = _load_backbone_klines(symbol, "1m", start_ms, end_ms)
    min_price = min(k.low for k in klines) if klines else 0.0
    max_price = max(k.high for k in klines) if klines else 0.0
    return Dataset(
        label=shard["label"],
        symbol=symbol,
        ticks=ticks,
        klines=klines,
        tick_count=int(len(ticks)),
        min_price=min_price,
        max_price=max_price,
    )


def _build_segments(
    dataset: Dataset,
    band_lo: float,
    band_hi: float,
    margin: float,
    pad_bars: int,
) -> list[Segment]:
    hit_ranges: list[tuple[int, int]] = []
    start = -1
    prev = -1
    for idx, kline in enumerate(dataset.klines):
        active = kline.high >= (band_lo - margin) and kline.low < (band_hi + margin)
        if not active:
            continue
        if start < 0:
            start = idx
            prev = idx
            continue
        if idx == prev + 1:
            prev = idx
            continue
        hit_ranges.append((start, prev))
        start = idx
        prev = idx
    if start >= 0:
        hit_ranges.append((start, prev))

    padded_ranges: list[tuple[int, int]] = []
    for lo, hi in hit_ranges:
        lo = max(0, lo - pad_bars)
        hi = min(len(dataset.klines) - 1, hi + pad_bars)
        if padded_ranges and lo <= padded_ranges[-1][1] + 1:
            padded_ranges[-1] = (padded_ranges[-1][0], max(padded_ranges[-1][1], hi))
        else:
            padded_ranges.append((lo, hi))

    segments: list[Segment] = []
    for lo, hi in padded_ranges:
        klines = dataset.klines[lo:hi + 1]
        tick_map = build_tick_slice_accessor(
            dataset.ticks,
            [(k.open_time, k.close_time) for k in klines],
        )
        segments.append(Segment(dataset_label=dataset.label, klines=klines, tick_map=tick_map))
    return segments


class PriceBandRunner:
    def __init__(self, datasets: list[Dataset], side: str, band_idx: int, band_floor: float, band_size: float):
        self.datasets = datasets
        self.side = side
        self.band_idx = band_idx
        self.band_floor = band_floor
        self.band_size = band_size
        self.eval_cache: dict[tuple[tuple[str, Any], ...], dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        band_lo, band_hi = _band_bounds(band_idx, band_floor, band_size)
        self.active_segments: list[Segment] = []
        for dataset in datasets:
            if dataset.max_price < band_lo or dataset.min_price >= band_hi:
                continue
            self.active_segments.extend(
                _build_segments(dataset, band_lo, band_hi, margin=0.0, pad_bars=120)
            )

    def _key(self, params: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
        return tuple(sorted(params.items()))

    def _collect_band_trades(self, params: dict[str, Any]) -> list[dict]:
        trades: list[dict] = []
        for segment in self.active_segments:
            strategy = WickReversalV5Strategy()
            strategy.allow_bar_fallback_in_tick_mode = False
            for key, value in params.items():
                setattr(strategy, key, value)
            signals = strategy.on_history(segment.klines, tick_map=segment.tick_map)
            stats = simulate_trades(signals, deepcopy(BT_CFG))
            for trade in stats["trade_list"]:
                if trade.get("skipped") or trade.get("dir") != self.side:
                    continue
                entry = float(trade.get("entry", 0.0) or 0.0)
                if entry <= 0:
                    continue
                if _band_index(entry, self.band_floor, self.band_size) != self.band_idx:
                    continue
                trade_copy = dict(trade)
                trade_copy["dataset_label"] = segment.dataset_label
                trades.append(trade_copy)
        trades.sort(key=lambda item: item.get("entry_time", 0))
        return trades

    def evaluate(self, params: dict[str, Any], train_ratio: float) -> dict[str, Any]:
        cache_key = self._key(params)
        if cache_key in self.eval_cache:
            return self.eval_cache[cache_key]

        full_trades = self._collect_band_trades(params)
        train_trades, val_trades = _split_trades(full_trades, train_ratio=train_ratio)
        result = {
            "params": deepcopy(params),
            "train": _stats_from_trades(train_trades),
            "validation": _stats_from_trades(val_trades),
            "full": _stats_from_trades(full_trades),
            "train_trades": len(train_trades),
            "validation_trades": len(val_trades),
            "full_trades": len(full_trades),
        }
        self.history.append(result)
        self.eval_cache[cache_key] = result
        return result


class CoordinateBandOptimizer:
    def __init__(self, runner: PriceBandRunner, side: str):
        self.runner = runner
        self.side = side

    def search(
        self,
        baseline_params: dict[str, Any],
        param_grid: dict[str, list[Any]],
        passes: int,
        topn: int,
        train_ratio: float,
    ) -> dict[str, Any]:
        current = deepcopy(baseline_params)
        best_train = self.runner.evaluate(current, train_ratio)

        for _ in range(passes):
            changed = False
            for name, values in param_grid.items():
                local_best_params = deepcopy(current)
                local_best_train = best_train
                for candidate in values:
                    trial = deepcopy(current)
                    if isinstance(candidate, dict):
                        trial.update(candidate)
                    else:
                        trial[name] = candidate
                    result = self.runner.evaluate(trial, train_ratio)
                    if result["train"]["score"] > local_best_train["train"]["score"]:
                        local_best_params = trial
                        local_best_train = result
                if local_best_params != current:
                    current = local_best_params
                    best_train = local_best_train
                    changed = True
            if not changed:
                break

        dedup: dict[tuple[tuple[str, Any], ...], dict[str, Any]] = {}
        for record in self.runner.history:
            dedup[self.runner._key(record["params"])] = record
        top_candidates = sorted(
            dedup.values(),
            key=lambda row: row["train"]["score"],
            reverse=True,
        )[:topn]

        best_final: dict[str, Any] | None = None
        for row in top_candidates:
            if best_final is None:
                best_final = row
                continue
            current_key = (
                row["validation"]["score"],
                row["validation"]["profit_factor"],
                row["validation"]["total_net_pnl"],
            )
            best_key = (
                best_final["validation"]["score"],
                best_final["validation"]["profit_factor"],
                best_final["validation"]["total_net_pnl"],
            )
            if current_key > best_key:
                best_final = row

        assert best_final is not None
        return {
            "best": best_final,
            "validation_table": top_candidates,
        }


def _make_baseline(side: str, band_idx: int, band_floor: float, band_size: float) -> dict[str, Any]:
    strategy = WickReversalV5Strategy()
    strategy.regime_band_size = 0.0
    band_lo, band_hi = _band_bounds(band_idx, band_floor, band_size)
    band_mid = (band_lo + band_hi) / 2.0
    keys = LONG_KEYS if side == "long" else SHORT_KEYS
    params = {
        "enable_long": side == "long",
        "enable_short": side == "short",
        "enable_regime_mode": True,
        "regime_band_size": band_size,
        "regime_band_floor": band_floor,
    }
    prefix = f"b{band_idx}_"
    for key in keys:
        params[prefix + key] = strategy._rp(key, band_mid)
    return params


def _make_grid(side: str, band_idx: int) -> dict[str, list[Any]]:
    prefix = f"b{band_idx}_"
    if side == "long":
        return {
            f"{prefix}sl_profile": [{prefix + key: value for key, value in profile.items()} for profile in LONG_SL_PROFILES],
            f"{prefix}rr_profile": [{prefix + key: value for key, value in profile.items()} for profile in LONG_RR_PROFILES],
            prefix + "long_k0_vol_gate": [300.0, 500.0, 800.0, 1200.0],
            prefix + "long_min_fee_cover_ratio": [1.2, 1.5, 2.0],
        }
    return {
        f"{prefix}sl_profile": [{prefix + key: value for key, value in profile.items()} for profile in SHORT_SL_PROFILES],
        f"{prefix}rr_profile": [{prefix + key: value for key, value in profile.items()} for profile in SHORT_RR_PROFILES],
        prefix + "short_k0_vol_gate": [300.0, 500.0, 800.0, 1200.0],
        prefix + "short_min_fee_cover_ratio": [1.2, 1.5, 2.0],
    }


def _build_combined_params(band_indices: list[int], band_floor: float, band_size: float) -> dict[str, Any]:
    combined = {
        "enable_regime_mode": True,
        "regime_band_floor": band_floor,
        "regime_band_size": band_size,
    }
    for band_idx in band_indices:
        for side in ("long", "short"):
            combined.update(_make_baseline(side, band_idx, band_floor, band_size))
    return combined


def _find_band_indices(datasets: list[Dataset], band_floor: float, band_size: float) -> list[int]:
    min_price = min(dataset.min_price for dataset in datasets)
    max_price = max(dataset.max_price for dataset in datasets)
    first = _band_index(min_price, band_floor, band_size)
    last = _band_index(max_price, band_floor, band_size)
    return list(range(first, last + 1))


def _parse_band_indices(raw: str | None, datasets: list[Dataset], band_floor: float, band_size: float) -> list[int]:
    if not raw:
        return _find_band_indices(datasets, band_floor, band_size)
    return sorted({int(part.strip()) for part in raw.split(",") if part.strip()})


def _parse_sides(raw: str | None) -> list[str]:
    if not raw:
        return ["long", "short"]
    parts = [part.strip().lower() for part in raw.split(",") if part.strip()]
    return [part for part in parts if part in {"long", "short"}]


def main() -> None:
    ap = argparse.ArgumentParser(description="Optimize Wick Reversal v5 with BTC price bands.")
    ap.add_argument("--band-size", type=float, default=10_000.0)
    ap.add_argument("--band-floor", type=float, default=0.0)
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--topn", type=int, default=6)
    ap.add_argument("--train-ratio", type=float, default=0.7)
    ap.add_argument("--min-trades", type=int, default=12)
    ap.add_argument("--band-indices", default="", help="optional comma-separated band indexes, e.g. 4,5,6")
    ap.add_argument("--sides", default="long,short")
    ap.add_argument("--out", default="docs/reports/wick_v5_price_bands_opt.json")
    args = ap.parse_args()

    datasets = [_load_dataset(shard) for shard in SHARDS]
    band_indices = _parse_band_indices(args.band_indices, datasets, args.band_floor, args.band_size)
    sides = _parse_sides(args.sides)
    combined_params = _build_combined_params(band_indices, args.band_floor, args.band_size)
    report_rows: list[dict[str, Any]] = []

    print(f"Loaded {len(datasets)} shards.")
    for dataset in datasets:
        print(
            f"  {dataset.label}: bars={len(dataset.klines)} ticks={dataset.tick_count:,} "
            f"range={dataset.min_price:.1f}-{dataset.max_price:.1f}"
        )

    for band_idx in band_indices:
        band_lo, band_hi = _band_bounds(band_idx, args.band_floor, args.band_size)
        print(f"\n{'=' * 72}")
        print(f"Band b{band_idx}: {band_lo:.0f} - {band_hi:.0f}")
        print(f"{'=' * 72}")
        band_row: dict[str, Any] = {
            "band_idx": band_idx,
            "band_low": band_lo,
            "band_high": band_hi,
        }
        for side in sides:
            print(f"\n[{side}]")
            runner = PriceBandRunner(datasets, side, band_idx, args.band_floor, args.band_size)
            baseline = _make_baseline(side, band_idx, args.band_floor, args.band_size)
            baseline_eval = runner.evaluate(baseline, args.train_ratio)
            print(f"baseline train: {_brief(baseline_eval['train'])}")
            print(f"baseline valid: {_brief(baseline_eval['validation'])}")
            print(f"baseline full : {_brief(baseline_eval['full'])}")

            if baseline_eval["full_trades"] < args.min_trades:
                print(f"skip optimize: only {baseline_eval['full_trades']} trades in this band")
                band_row[side] = {
                    "optimized": False,
                    "best": baseline_eval,
                }
                continue

            optimizer = CoordinateBandOptimizer(runner, side)
            result = optimizer.search(
                baseline_params=baseline,
                param_grid=_make_grid(side, band_idx),
                passes=args.passes,
                topn=args.topn,
                train_ratio=args.train_ratio,
            )
            best = result["best"]
            for key, value in best["params"].items():
                if key.startswith(f"b{band_idx}_"):
                    combined_params[key] = value
            print(f"best train: {_brief(best['train'])}")
            print(f"best valid: {_brief(best['validation'])}")
            print(f"best full : {_brief(best['full'])}")
            band_row[side] = {
                "optimized": True,
                "best": best,
                "validation_table": result["validation_table"],
            }
        report_rows.append(band_row)

    report = {
        "band_size": args.band_size,
        "band_floor": args.band_floor,
        "passes": args.passes,
        "topn": args.topn,
        "train_ratio": args.train_ratio,
        "min_trades": args.min_trades,
        "bands": _to_builtin(report_rows),
        "combined_params": _to_builtin(combined_params),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nsaved={out_path}")


if __name__ == "__main__":
    main()
