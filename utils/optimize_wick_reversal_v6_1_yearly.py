from __future__ import annotations

import argparse
import json
import math
import sys
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestConfig, _resolve_fee_rate, simulate_trades
from core import kline_cache, tick_cache
from strategies.wick_reversal_v6_1 import WickReversalV6_1Strategy

INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}


def _safe_float(v: Any) -> float:
    out = float(v)
    if math.isinf(out):
        return 999.0 if out > 0 else -999.0
    if math.isnan(out):
        return 0.0
    return out


def _to_builtin(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _to_builtin(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_builtin(v) for v in obj]
    if isinstance(obj, tuple):
        return [_to_builtin(v) for v in obj]
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:
            return obj
    return obj


def _ms_to_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _score_stats(stats: dict[str, Any]) -> float:
    trades = int(stats.get("trades", 0))
    if trades <= 0:
        return -1e9
    ret = _safe_float(stats.get("total_return_pct", 0.0))
    dd = _safe_float(stats.get("max_drawdown_pct", 0.0))
    pf = min(_safe_float(stats.get("profit_factor", 0.0)), 3.0)
    win = _safe_float(stats.get("win_rate", 0.0))
    trade_penalty = 0.0 if trades >= 20 else (20 - trades) * 1.25
    return ret - dd * 1.15 + (pf - 1.0) * 18.0 + win * 0.06 - trade_penalty


def _brief(stats: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "trades",
        "win_rate",
        "profit_factor",
        "total_net_pnl",
        "final_equity",
        "total_return_pct",
        "max_drawdown_pct",
        "sl_count",
        "tp_count",
        "ts_count",
        "td_count",
        "score",
        "signals",
        "runtime_sec",
        "tick_coverage_pct",
    ]
    return {k: _to_builtin(stats.get(k)) for k in keys}


def _default_tuned_params() -> dict[str, Any]:
    s = WickReversalV6_1Strategy()
    return {
        "rr": s.rr,
        "zoom_entry_delta_eff_threshold": s.zoom_entry_delta_eff_threshold,
        "trade_delta_drawdown_pct": s.trade_delta_drawdown_pct,
        "trailing_stop_mode": s.trailing_stop_mode,
    }


def _grid_for_profile(profile: str) -> dict[str, list[Any]]:
    if profile == "quick":
        return {
            "rr": [2.0, 2.5, 3.0],
            "zoom_entry_delta_eff_threshold": [0.25, 0.30, 0.35],
            "trade_delta_drawdown_pct": [0.25, 0.30, 0.35],
            "trailing_stop_mode": ["lock_tp", "breakeven_cost"],
        }
    if profile == "standard":
        return {
            "rr": [2.0, 2.5, 3.0],
            "zoom_entry_delta_eff_threshold": [0.20, 0.30, 0.40],
            "trade_delta_drawdown_pct": [0.20, 0.30, 0.40],
            "trailing_stop_mode": ["lock_tp", "breakeven_cost"],
            "entry_atr_cap": [0.25, 0.35, 0.45],
            "stop_atr_mult": [0.15, 0.25, 0.35],
        }
    raise ValueError(f"unsupported profile: {profile}")


@dataclass
class Segment:
    name: str
    start_ms: int
    end_ms: int
    bars: int
    klines: list
    tick_map: Any


def _year_segments(start_ms: int, end_ms: int) -> list[tuple[str, int, int]]:
    start_dt = datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ms / 1000.0, tz=timezone.utc)
    out: list[tuple[str, int, int]] = []
    for year in range(start_dt.year, end_dt.year + 1):
        y0 = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        y1 = int(datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)
        seg_start = max(start_ms, y0)
        seg_end = min(end_ms, y1)
        if seg_start <= seg_end:
            out.append((str(year), seg_start, seg_end))
    return out


def _load_segment(symbol: str, interval: str, seg_name: str, start_ms: int, end_ms: int) -> Segment:
    bar_ms = INTERVAL_MS[interval]
    start_bar = (start_ms // bar_ms) * bar_ms
    end_bar = (end_ms // bar_ms) * bar_ms
    klines = kline_cache.load_range_as_klines(symbol, interval, start_bar, end_bar)
    if not klines:
        raise RuntimeError(f"no klines for segment {seg_name}: {interval} {start_bar}-{end_bar}")
    tick_map = tick_cache.build_lazy_bar_map(
        [symbol],
        [(k.open_time, k.close_time) for k in klines],
    )
    return Segment(
        name=seg_name,
        start_ms=int(klines[0].open_time),
        end_ms=int(klines[-1].close_time),
        bars=len(klines),
        klines=klines,
        tick_map=tick_map,
    )


def _eval_params(
    segment: Segment,
    cfg: BacktestConfig,
    params: dict[str, Any],
) -> dict[str, Any]:
    strategy = WickReversalV6_1Strategy()
    strategy.allow_bar_fallback_in_tick_mode = False
    strategy.configure_backtest_costs(_resolve_fee_rate(cfg), cfg.slippage_bps)
    for k, v in params.items():
        setattr(strategy, k, v)

    t0 = time.perf_counter()
    signals = strategy.on_history(segment.klines, tick_map=segment.tick_map)
    stats = simulate_trades(signals, deepcopy(cfg))
    dt = time.perf_counter() - t0
    covered, total = segment.tick_map.observed_coverage()
    coverage = (covered / total * 100.0) if total else 0.0

    out = dict(stats)
    out["signals"] = len(signals)
    out["runtime_sec"] = dt
    out["tick_coverage_pct"] = coverage
    out["covered_bars"] = covered
    out["total_bars"] = total
    out["params"] = deepcopy(params)
    out["score"] = _score_stats(out)
    return _to_builtin(out)


def _optimize_one_segment(
    segment: Segment,
    cfg: BacktestConfig,
    baseline: dict[str, Any],
    grid: dict[str, list[Any]],
    passes: int,
    topn: int,
) -> dict[str, Any]:
    cache: dict[tuple[tuple[str, Any], ...], dict[str, Any]] = {}

    def evaluate(p: dict[str, Any]) -> dict[str, Any]:
        key = tuple(sorted(p.items()))
        if key in cache:
            return cache[key]
        result = _eval_params(segment, cfg, p)
        cache[key] = result
        return result

    current = deepcopy(baseline)
    best = evaluate(current)

    for _ in range(max(1, passes)):
        changed = False
        for name, values in grid.items():
            local_best_params = deepcopy(current)
            local_best_stats = best
            for candidate in values:
                trial = deepcopy(current)
                trial[name] = candidate
                trial_stats = evaluate(trial)
                if trial_stats["score"] > local_best_stats["score"]:
                    local_best_params = trial
                    local_best_stats = trial_stats
            if local_best_params != current:
                current = local_best_params
                best = local_best_stats
                changed = True
        if not changed:
            break

    ranked = sorted(cache.values(), key=lambda x: x["score"], reverse=True)[: max(1, topn)]
    return {
        "segment": segment.name,
        "range_utc": {
            "start": _ms_to_utc(segment.start_ms),
            "end": _ms_to_utc(segment.end_ms),
        },
        "bars": segment.bars,
        "best_params": best["params"],
        "best_stats": _brief(best),
        "top_candidates": [
            {
                "params": row["params"],
                "stats": _brief(row),
            }
            for row in ranked
        ],
        "evaluations": len(cache),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Year-by-year tick-level optimizer for Wick Reversal v6.1.")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="15m", choices=list(INTERVAL_MS))
    ap.add_argument("--profile", default="quick", choices=["quick", "standard"])
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--topn", type=int, default=5)
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--out", default="docs/reports/wick_reversal_v6_1_yearly_optimization.json")
    args = ap.parse_args()

    symbol = args.symbol.upper()
    info = tick_cache.info(symbol)
    if info is None:
        raise RuntimeError(f"tick dataset not found for {symbol}")

    eff_start_ms = int(info["start_ms"])
    eff_end_ms = int(info["end_ms"])
    if args.start:
        req_start = int(datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc).timestamp() * 1000)
        eff_start_ms = max(eff_start_ms, req_start)
    if args.end:
        req_end = int(datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc).timestamp() * 1000)
        eff_end_ms = min(eff_end_ms, req_end)
    if eff_start_ms >= eff_end_ms:
        raise RuntimeError("effective range is empty after clipping")

    year_ranges = _year_segments(eff_start_ms, eff_end_ms)
    if not year_ranges:
        raise RuntimeError("no yearly segments in effective range")

    cfg = BacktestConfig(
        initial_capital=10_000.0,
        max_loss_pct=0.02,
        leverage=20,
        fee_mode="Taker",
        custom_fee_rate=0.00032,
        slippage_bps=0.0,
        funding_rate=0.0,
        maint_margin=0.005,
        compound=True,
    )

    baseline = _default_tuned_params()
    grid = _grid_for_profile(args.profile)

    print(f"[info] symbol={symbol} interval={args.interval} profile={args.profile}")
    print(f"[info] effective_range={_ms_to_utc(eff_start_ms)} -> {_ms_to_utc(eff_end_ms)}")
    print(f"[info] segments={[name for name, _, _ in year_ranges]}")

    results = []
    for idx, (name, seg_start, seg_end) in enumerate(year_ranges, start=1):
        print(f"[segment {idx}/{len(year_ranges)}] loading {name} ...")
        segment = _load_segment(symbol, args.interval, name, seg_start, seg_end)
        print(
            f"[segment {idx}/{len(year_ranges)}] optimizing {name} "
            f"bars={segment.bars} range={_ms_to_utc(segment.start_ms)}->{_ms_to_utc(segment.end_ms)}"
        )
        seg_result = _optimize_one_segment(
            segment=segment,
            cfg=cfg,
            baseline=baseline,
            grid=grid,
            passes=args.passes,
            topn=args.topn,
        )
        results.append(seg_result)
        print(
            f"[segment {idx}/{len(year_ranges)}] done {name} "
            f"score={seg_result['best_stats']['score']:.2f} trades={seg_result['best_stats']['trades']}"
        )

    out = {
        "meta": {
            "strategy": WickReversalV6_1Strategy.name,
            "symbol": symbol,
            "interval": args.interval,
            "profile": args.profile,
            "passes": args.passes,
            "topn": args.topn,
            "effective_range_utc": {
                "start": _ms_to_utc(eff_start_ms),
                "end": _ms_to_utc(eff_end_ms),
            },
            "baseline_params": baseline,
            "grid": grid,
            "backtest_config": asdict(cfg),
        },
        "yearly_results": results,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(_to_builtin(out), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[saved] {out_path}")


if __name__ == "__main__":
    main()
