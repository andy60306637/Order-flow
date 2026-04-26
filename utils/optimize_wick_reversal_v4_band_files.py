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
from core.tick_cache import build_tick_slice_accessor, info as tick_info, load_meta, load_range
from strategies.wick_reversal_v4 import WickReversalV4Strategy
from strategies.wick_reversal_v4_band_files import WickReversalV4BandFilesStrategy
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
    "long_sl_offset",
    "long_td_consec_bars",
    "long_k0_vol_gate",
    "long_delta_eff_threshold",
    "long_vol_sma_mult",
    "lower_wick_absorption_delta_eff_max",
    "lower_wick_absorption_min_vol_ratio",
    "long_min_fee_cover_ratio",
    "long_rr_wick_a",
    "long_rr_wick_b",
    "long_rr_wick_c",
]

SHORT_KEYS = [
    "short_sl_offset",
    "short_td_consec_bars",
    "short_k0_vol_gate",
    "short_delta_eff_threshold",
    "short_vol_sma_mult",
    "upper_wick_absorption_delta_eff_min",
    "upper_wick_absorption_min_vol_ratio",
    "short_min_fee_cover_ratio",
    "short_a_min_upper_wick_pct",
    "short_rr_wick_a",
    "short_rr_wick_b",
    "short_rr_wick_c",
]

LONG_RR_PROFILES = [
    {"long_rr_wick_a": 3.0, "long_rr_wick_b": 1.5, "long_rr_wick_c": 2.0},
    {"long_rr_wick_a": 2.5, "long_rr_wick_b": 1.5, "long_rr_wick_c": 1.5},
    {"long_rr_wick_a": 3.0, "long_rr_wick_b": 2.0, "long_rr_wick_c": 1.5},
    {"long_rr_wick_a": 3.5, "long_rr_wick_b": 2.0, "long_rr_wick_c": 1.5},
]

SHORT_RR_PROFILES = [
    {"short_rr_wick_a": 4.5, "short_rr_wick_b": 2.5, "short_rr_wick_c": 2.0},
    {"short_rr_wick_a": 3.0, "short_rr_wick_b": 2.0, "short_rr_wick_c": 1.5},
    {"short_rr_wick_a": 2.5, "short_rr_wick_b": 1.5, "short_rr_wick_c": 1.0},
    {"short_rr_wick_a": 4.5, "short_rr_wick_b": 1.5, "short_rr_wick_c": 2.0},
]

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
    pf = min(_safe_float(stats["profit_factor"]), 3.0)
    wr = _safe_float(stats["win_rate"])
    dd = _safe_float(stats["max_drawdown_pct"])
    pnl = _safe_float(stats["total_net_pnl"])
    trade_bonus = min(trades, 60) * 0.10
    trade_penalty = 0.0 if trades >= 10 else (10 - trades) * 5.0
    return pnl - dd * 0.8 + (pf - 1.0) * 24.0 + wr * 0.08 + trade_bonus - trade_penalty


def _brief(stats: dict[str, Any]) -> str:
    return (
        f"trades={int(stats['trades'])} "
        f"wr={stats['win_rate']:.1f}% "
        f"pf={stats['profit_factor']:.3f} "
        f"pnl={stats['total_net_pnl']:.2f} "
        f"dd={stats['max_drawdown_pct']:.2f}% "
        f"score={stats['score']:.2f}"
    )


def _band_bounds(low: int, band_size: int) -> tuple[int, int]:
    return low, low + band_size


def _band_index(price: float, band_floor: float, band_size: float) -> int:
    shifted = max(price - band_floor, 0.0)
    return int(shifted // band_size)


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
    klines: list
    tick_count: int
    min_price: float
    max_price: float


@dataclass
class Segment:
    dataset_label: str
    symbol: str
    start_ms: int
    end_ms: int
    klines: list


def _load_dataset(shard: dict[str, str]) -> Dataset:
    symbol = shard["symbol"]
    if load_meta(symbol) is None:
        raise RuntimeError(f"tick cache not found for {symbol}")
    start_ms = _dt_to_ms(shard["start"])
    end_ms = _dt_to_ms(shard["end"])
    klines = _load_backbone_klines(symbol, "1m", start_ms, end_ms)
    meta = tick_info(symbol) or {}
    min_price = min(k.low for k in klines) if klines else 0.0
    max_price = max(k.high for k in klines) if klines else 0.0
    return Dataset(
        label=shard["label"],
        symbol=symbol,
        klines=klines,
        tick_count=int(meta.get("count", 0) or 0),
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
        segments.append(
            Segment(
                dataset_label=dataset.label,
                symbol=dataset.symbol,
                start_ms=int(klines[0].open_time),
                end_ms=int(klines[-1].close_time),
                klines=klines,
            )
        )
    return segments


def _band_dir(symbol: str) -> Path:
    return PROJECT_ROOT / "config" / "wick_reversal_v4_band_files" / symbol.upper()


def _band_path(symbol: str, band_low: int, band_high: int) -> Path:
    return _band_dir(symbol) / f"{band_low:05d}_{band_high:05d}.json"


def _default_band_payload(symbol: str, band_low: int, band_high: int) -> dict[str, Any]:
    strategy = WickReversalV4Strategy()
    return {
        "meta": {
            "symbol": symbol.upper(),
            "price_low": band_low,
            "price_high": band_high,
            "base_strategy": "Wick Reversal 1m v4",
        },
        "params": {
            name: getattr(strategy, name)
            for name in WickReversalV4BandFilesStrategy.PARAM_FIELDS
        },
    }


def _load_band_payload(symbol: str, band_low: int, band_high: int) -> dict[str, Any]:
    path = _band_path(symbol, band_low, band_high)
    if not path.exists():
        return _default_band_payload(symbol, band_low, band_high)
    return json.loads(path.read_text(encoding="utf-8"))


def _write_band_payload(symbol: str, band_low: int, band_high: int, payload: dict[str, Any]) -> None:
    path = _band_path(symbol, band_low, band_high)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


class PriceBandRunner:
    def __init__(self, datasets: list[Dataset], side: str, band_low: int, band_size: int):
        self.datasets = datasets
        self.side = side
        self.band_low = band_low
        self.band_high = band_low + band_size
        self.band_floor = 0.0
        self.band_size = float(band_size)
        self.band_idx = _band_index(band_low, self.band_floor, self.band_size)
        self.eval_cache: dict[tuple[tuple[str, Any], ...], dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        self.active_segments: list[Segment] = []
        for dataset in datasets:
            if dataset.max_price < self.band_low or dataset.min_price >= self.band_high:
                continue
            self.active_segments.extend(
                _build_segments(dataset, self.band_low, self.band_high, margin=0.0, pad_bars=120)
            )

    def _key(self, params: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
        return tuple(sorted(params.items()))

    def _collect_band_trades(self, params: dict[str, Any]) -> list[dict]:
        trades: list[dict] = []
        for segment in self.active_segments:
            ticks = load_range(segment.symbol, segment.start_ms, segment.end_ms)
            tick_map = build_tick_slice_accessor(
                ticks,
                [(k.open_time, k.close_time) for k in segment.klines],
            )
            strategy = WickReversalV4Strategy()
            strategy.allow_bar_fallback_in_tick_mode = False
            for key, value in params.items():
                setattr(strategy, key, value)
            signals = strategy.on_history(segment.klines, tick_map=tick_map)
            stats = simulate_trades(signals, deepcopy(BT_CFG))
            for trade in stats["trade_list"]:
                if trade.get("skipped") or trade.get("dir") != self.side:
                    continue
                entry = float(trade.get("entry", 0.0) or 0.0)
                if entry <= 0:
                    continue
                if not (self.band_low <= entry < self.band_high):
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
    def __init__(self, runner: PriceBandRunner):
        self.runner = runner

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


def _make_baseline(side: str, band_payload: dict[str, Any]) -> dict[str, Any]:
    params = deepcopy(band_payload["params"])
    params["enable_long"] = side == "long"
    params["enable_short"] = side == "short"
    return params


def _make_grid(side: str) -> dict[str, list[Any]]:
    if side == "long":
        return {
            "long_sl_offset": [6.0, 10.0, 15.0, 20.0],
            "long_td_consec_bars": [1, 2, 3, 4],
            "long_k0_vol_gate": [300.0, 500.0, 800.0, 1200.0],
            "long_delta_eff_threshold": [0.6, 0.8, 1.0, 1.2],
            "long_vol_sma_mult": [0.8, 1.0, 1.2, 1.4],
            "lower_wick_absorption_delta_eff_max": [0.0, -0.05, -0.10],
            "lower_wick_absorption_min_vol_ratio": [0.10, 0.15, 0.20, 0.25],
            "long_min_fee_cover_ratio": [1.2, 1.5, 2.0],
            "long_rr_profile": LONG_RR_PROFILES,
        }
    return {
        "short_sl_offset": [6.0, 10.0, 15.0, 20.0],
        "short_td_consec_bars": [1, 2, 3, 4],
        "short_k0_vol_gate": [200.0, 300.0, 500.0, 800.0],
        "short_delta_eff_threshold": [0.6, 0.8, 1.0, 1.2],
        "short_vol_sma_mult": [1.0, 1.2, 1.4, 1.6],
        "upper_wick_absorption_delta_eff_min": [0.0, 0.05, 0.10],
        "upper_wick_absorption_min_vol_ratio": [0.10, 0.15, 0.20, 0.25],
        "short_min_fee_cover_ratio": [1.5, 2.0, 2.5],
        "short_a_min_upper_wick_pct": [0.0, 0.0008, 0.0011, 0.0014],
        "short_rr_profile": SHORT_RR_PROFILES,
    }


def _extract_side_params(params: dict[str, Any], side: str) -> dict[str, Any]:
    keys = LONG_KEYS if side == "long" else SHORT_KEYS
    return {key: params[key] for key in keys if key in params}


def main() -> None:
    ap = argparse.ArgumentParser(description="Optimize per-band JSON params for Wick Reversal v4 band files.")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--price-start", type=int, default=10_000)
    ap.add_argument("--price-end", type=int, default=150_000)
    ap.add_argument("--band-size", type=int, default=1_000)
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--topn", type=int, default=6)
    ap.add_argument("--train-ratio", type=float, default=0.7)
    ap.add_argument("--min-trades", type=int, default=12)
    ap.add_argument("--sides", default="long,short")
    ap.add_argument("--out", default="docs/reports/wick_reversal_v4_band_files_optimization.json")
    args = ap.parse_args()

    if args.band_size <= 0:
        raise SystemExit("--band-size must be > 0")
    if args.price_end <= args.price_start:
        raise SystemExit("--price-end must be > --price-start")

    sides = [part.strip().lower() for part in args.sides.split(",") if part.strip()]
    sides = [side for side in sides if side in {"long", "short"}]
    if not sides:
        raise SystemExit("--sides must include long and/or short")

    datasets = [_load_dataset(shard) for shard in SHARDS]
    band_lows = list(range(args.price_start, args.price_end, args.band_size))
    report_rows: list[dict[str, Any]] = []

    print(f"Loaded {len(datasets)} shards.")
    for dataset in datasets:
        print(
            f"  {dataset.label}: bars={len(dataset.klines)} ticks={dataset.tick_count:,} "
            f"range={dataset.min_price:.1f}-{dataset.max_price:.1f}"
        )

    for band_low in band_lows:
        band_high = band_low + args.band_size
        payload = _load_band_payload(args.symbol, band_low, band_high)
        band_row: dict[str, Any] = {
            "band_low": band_low,
            "band_high": band_high,
        }

        print(f"\n{'=' * 72}")
        print(f"Band {band_low} - {band_high}")
        print(f"{'=' * 72}")

        changed = False
        for side in sides:
            print(f"\n[{side}]")
            runner = PriceBandRunner(datasets, side, band_low, args.band_size)
            baseline = _make_baseline(side, payload)
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

            optimizer = CoordinateBandOptimizer(runner)
            result = optimizer.search(
                baseline_params=baseline,
                param_grid=_make_grid(side),
                passes=args.passes,
                topn=args.topn,
                train_ratio=args.train_ratio,
            )
            best = result["best"]
            best_side_params = _extract_side_params(best["params"], side)
            payload["params"].update(best_side_params)
            changed = True
            print(f"best train: {_brief(best['train'])}")
            print(f"best valid: {_brief(best['validation'])}")
            print(f"best full : {_brief(best['full'])}")
            band_row[side] = {
                "optimized": True,
                "best": best,
                "validation_table": result["validation_table"],
            }

        if changed:
            _write_band_payload(args.symbol, band_low, band_high, payload)
        report_rows.append(band_row)

    report = {
        "symbol": args.symbol.upper(),
        "price_start": args.price_start,
        "price_end": args.price_end,
        "band_size": args.band_size,
        "passes": args.passes,
        "topn": args.topn,
        "train_ratio": args.train_ratio,
        "min_trades": args.min_trades,
        "sides": sides,
        "bands": _to_builtin(report_rows),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(_to_builtin(report), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nsaved={out_path}")


if __name__ == "__main__":
    main()
