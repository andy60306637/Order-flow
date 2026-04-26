from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest import engine as bt_engine
from backtest.engine import BacktestConfig, simulate_trades
from core.tick_cache import build_tick_slice_accessor, load_meta, load_range
from strategies.wick_reversal_v4_test import WickReversalV4Strategy
from utils.optimize_wick_reversal_v4 import _dt_to_ms, _load_backbone_klines, _to_builtin


SYMBOL = "BTCUSDT_20220414_20230413"
START = "2022-04-14"
END_EXCLUSIVE = "2023-04-14"

LONG_GRID = {
    "long_sl_offset": list(range(2, 11)),
    "long_k0_vol_gate": list(range(300, 3001, 100)),
    "long_min_fee_cover_ratio": list(range(2, 11)),
}

SHORT_GRID = {
    "short_sl_offset": list(range(2, 11)),
    "short_k0_vol_gate": list(range(300, 3001, 100)),
    "short_min_fee_cover_ratio": list(range(2, 11)),
}


def _safe_float(value: Any) -> float:
    out = float(value)
    if math.isnan(out):
        return 0.0
    if math.isinf(out):
        return 999.0 if out > 0 else -999.0
    return out


def _score_tuple(stats: dict[str, Any]) -> tuple[float, float, float, float, int]:
    return (
        _safe_float(stats["total_net_pnl"]),
        _safe_float(stats["profit_factor"]),
        _safe_float(stats["win_rate"]),
        -_safe_float(stats["max_drawdown_pct"]),
        int(stats["trades"]),
    )


def _brief_stats(stats: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "trades",
        "win_rate",
        "profit_factor",
        "total_net_pnl",
        "total_return_pct",
        "max_drawdown_pct",
        "final_equity",
        "sl_count",
        "tp_count",
        "ts_count",
        "td_count",
        "long_trades",
        "long_win_rate",
        "long_profit_factor",
        "short_trades",
        "short_win_rate",
        "short_profit_factor",
    ]
    return {k: _to_builtin(stats[k]) for k in keys}


def _build_bt_cfg() -> BacktestConfig:
    return BacktestConfig(
        initial_capital=10_000.0,
        max_loss_pct=0.0002,  # 0.02%
        leverage=20,
        fee_mode="自訂",
        custom_fee_rate=0.00032,  # 0.032%
        slippage_bps=0.2,
        funding_rate=0.0,
        maint_margin=0.005,
        compound=False,  # fixed position mode
    )


def _default_params() -> dict[str, Any]:
    s = WickReversalV4Strategy()
    fields = [
        "enable_long",
        "enable_short",
        "long_sl_offset",
        "long_k0_vol_gate",
        "long_min_fee_cover_ratio",
        "short_sl_offset",
        "short_k0_vol_gate",
        "short_min_fee_cover_ratio",
    ]
    return {name: getattr(s, name) for name in fields}


def _run(
    params: dict[str, Any],
    klines: list,
    tick_map: Any,
    cfg: BacktestConfig,
    cache: dict[tuple[tuple[str, Any], ...], dict[str, Any]],
) -> dict[str, Any]:
    key = tuple(sorted(params.items()))
    if key in cache:
        return cache[key]
    strategy = WickReversalV4Strategy()
    strategy.allow_bar_fallback_in_tick_mode = False
    for k, v in params.items():
        setattr(strategy, k, v)
    signals = strategy.on_history(klines, tick_map=tick_map)
    stats = simulate_trades(signals, deepcopy(cfg))
    stats = _to_builtin(stats)
    cache[key] = stats
    return stats


def _coordinate_optimize(
    base_params: dict[str, Any],
    grid: dict[str, list[Any]],
    side: str,
    klines: list,
    tick_map: Any,
    cfg: BacktestConfig,
    passes: int = 2,
) -> dict[str, Any]:
    cache: dict[tuple[tuple[str, Any], ...], dict[str, Any]] = {}
    history: list[dict[str, Any]] = []
    current = deepcopy(base_params)
    best_stats = _run(current, klines, tick_map, cfg, cache)
    history.append({"params": deepcopy(current), "stats": _brief_stats(best_stats)})
    print(
        f"[{side}] baseline: trades={best_stats['trades']} "
        f"pnl={_safe_float(best_stats['total_net_pnl']):.4f} "
        f"pf={_safe_float(best_stats['profit_factor']):.4f}",
        flush=True,
    )

    for pass_idx in range(1, passes + 1):
        changed = False
        print(f"[{side}] pass {pass_idx}/{passes}", flush=True)
        for name, values in grid.items():
            local_best_params = deepcopy(current)
            local_best_stats = best_stats
            for v in values:
                trial = deepcopy(current)
                trial[name] = v
                stats = _run(trial, klines, tick_map, cfg, cache)
                if _score_tuple(stats) > _score_tuple(local_best_stats):
                    local_best_params = trial
                    local_best_stats = stats
            if local_best_params != current:
                current = local_best_params
                best_stats = local_best_stats
                changed = True
                history.append({"params": deepcopy(current), "stats": _brief_stats(best_stats)})
            print(
                f"[{side}]   {name}={current[name]} "
                f"trades={best_stats['trades']} "
                f"pnl={_safe_float(best_stats['total_net_pnl']):.4f} "
                f"pf={_safe_float(best_stats['profit_factor']):.4f}",
                flush=True,
            )
        if not changed:
            print(f"[{side}] converged on pass {pass_idx}", flush=True)
            break

    return {
        "best_params": current,
        "best_stats": best_stats,
        "history": history,
        "eval_count": len(cache),
    }


def main() -> None:
    if load_meta(SYMBOL) is None:
        raise RuntimeError(f"tick cache not found: {SYMBOL}")

    start_ms = _dt_to_ms(START)
    end_ms_inclusive = _dt_to_ms(END_EXCLUSIVE) - 1

    print(f"[load] symbol={SYMBOL} range={START} -> {END_EXCLUSIVE}", flush=True)
    ticks = load_range(SYMBOL, start_ms, end_ms_inclusive)
    klines = _load_backbone_klines(SYMBOL, "1m", start_ms, end_ms_inclusive)
    tick_map = build_tick_slice_accessor(ticks, [(k.open_time, k.close_time) for k in klines])
    print(
        f"[load] bars={len(klines)} ticks={len(ticks):,} covered_bars={len(tick_map)}",
        flush=True,
    )

    cfg = _build_bt_cfg()
    fee_rate = bt_engine._resolve_fee_rate(cfg)
    print(
        "[cfg] "
        f"capital={cfg.initial_capital} risk_pct={cfg.max_loss_pct} "
        f"fee_rate={fee_rate} slippage_bps={cfg.slippage_bps} compound={cfg.compound}",
        flush=True,
    )

    base = _default_params()
    long_base = deepcopy(base)
    long_base["enable_long"] = True
    long_base["enable_short"] = False
    short_base = deepcopy(base)
    short_base["enable_long"] = False
    short_base["enable_short"] = True

    long_result = _coordinate_optimize(
        long_base,
        LONG_GRID,
        "long",
        klines,
        tick_map,
        cfg,
        passes=2,
    )
    short_result = _coordinate_optimize(
        short_base,
        SHORT_GRID,
        "short",
        klines,
        tick_map,
        cfg,
        passes=2,
    )

    combined_base = deepcopy(base)
    combined_base["enable_long"] = True
    combined_base["enable_short"] = True
    combined_opt = deepcopy(combined_base)
    for k in LONG_GRID:
        combined_opt[k] = long_result["best_params"][k]
    for k in SHORT_GRID:
        combined_opt[k] = short_result["best_params"][k]

    combined_cache: dict[tuple[tuple[str, Any], ...], dict[str, Any]] = {}
    baseline_stats = _run(combined_base, klines, tick_map, cfg, combined_cache)
    optimized_stats = _run(combined_opt, klines, tick_map, cfg, combined_cache)

    report = {
        "meta": {
            "strategy_file": "strategies/wick_reversal_v4_test.py",
            "symbol": SYMBOL,
            "start_utc": START,
            "end_exclusive_utc": END_EXCLUSIVE,
            "bar_interval": "1m",
            "bars": len(klines),
            "ticks": len(ticks),
            "covered_bars": len(tick_map),
            "search_method": "coordinate_descent",
            "passes": 2,
            "bt_config": asdict(cfg),
            "resolved_fee_rate": fee_rate,
        },
        "search_space": {
            "long_sl_offset": [2, 10],
            "short_sl_offset": [2, 10],
            "long_k0_vol_gate": [300, 3000],
            "short_k0_vol_gate": [300, 3000],
            "long_min_fee_cover_ratio": [2, 10],
            "short_min_fee_cover_ratio": [2, 10],
            "long_granularity": {"sl_offset": 1, "k0_vol_gate": 100, "min_fee_cover_ratio": 1},
            "short_granularity": {"sl_offset": 1, "k0_vol_gate": 100, "min_fee_cover_ratio": 1},
        },
        "long_only": {
            "best_params": {k: long_result["best_params"][k] for k in LONG_GRID},
            "best_stats": _brief_stats(long_result["best_stats"]),
            "eval_count": long_result["eval_count"],
            "history": long_result["history"],
        },
        "short_only": {
            "best_params": {k: short_result["best_params"][k] for k in SHORT_GRID},
            "best_stats": _brief_stats(short_result["best_stats"]),
            "eval_count": short_result["eval_count"],
            "history": short_result["history"],
        },
        "combined": {
            "baseline_params": {
                k: combined_base[k]
                for k in list(LONG_GRID.keys()) + list(SHORT_GRID.keys())
            },
            "optimized_params": {
                k: combined_opt[k]
                for k in list(LONG_GRID.keys()) + list(SHORT_GRID.keys())
            },
            "baseline_stats": _brief_stats(baseline_stats),
            "optimized_stats": _brief_stats(optimized_stats),
        },
    }

    out_path = PROJECT_ROOT / "docs" / "reports" / "wick_reversal_v4_test_opt_202204_202304.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(_to_builtin(report), indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[done] saved={out_path}", flush=True)
    print(
        "[done] long_best="
        + json.dumps(report["long_only"]["best_params"], ensure_ascii=False),
        flush=True,
    )
    print(
        "[done] short_best="
        + json.dumps(report["short_only"]["best_params"], ensure_ascii=False),
        flush=True,
    )
    print(
        "[done] combined_optimized_stats="
        + json.dumps(report["combined"]["optimized_stats"], ensure_ascii=False),
        flush=True,
    )


if __name__ == "__main__":
    main()
