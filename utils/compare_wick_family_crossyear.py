from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestConfig, _resolve_fee_rate, simulate_trades
from core import kline_cache, tick_cache
from strategies import STRATEGY_REGISTRY

OUT_PATH = PROJECT_ROOT / "docs" / "reports" / "wick_family_crossyear_robustness_2025_2026.json"
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

SYMBOL = "BTCUSDT"
INTERVAL = "15m"

STRATEGIES = [
    "Wick Reversal 1m v4",
    "Wick Reversal 1m v4 Dyn",
    "Wick Reversal 1m v4 Ratio",
    "Wick Reversal 1m v4 band files",
    "Wick Reversal 1m v5",
    "Wick Reversal 15m v6",
]

CFG = BacktestConfig(
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


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _safe(v: Any) -> float:
    out = float(v)
    if math.isnan(out):
        return 0.0
    if math.isinf(out):
        return 999.0 if out > 0 else -999.0
    return out


def _brief(stats: dict[str, Any], runtime_sec: float, coverage_pct: float, signals: int) -> dict[str, Any]:
    return {
        "trades": int(stats.get("trades", 0)),
        "signals": int(signals),
        "win_rate": _safe(stats.get("win_rate", 0.0)),
        "profit_factor": _safe(stats.get("profit_factor", 0.0)),
        "total_net_pnl": _safe(stats.get("total_net_pnl", 0.0)),
        "total_return_pct": _safe(stats.get("total_return_pct", 0.0)),
        "max_drawdown_pct": _safe(stats.get("max_drawdown_pct", 0.0)),
        "sl_count": int(stats.get("sl_count", 0)),
        "tp_count": int(stats.get("tp_count", 0)),
        "ts_count": int(stats.get("ts_count", 0)),
        "td_count": int(stats.get("td_count", 0)),
        "runtime_sec": float(runtime_sec),
        "tick_coverage_pct": float(coverage_pct),
    }


def _robustness(y2025: dict[str, Any], y2026: dict[str, Any]) -> dict[str, Any]:
    ret_25 = _safe(y2025["total_return_pct"])
    ret_26 = _safe(y2026["total_return_pct"])
    pf_25 = _safe(y2025["profit_factor"])
    pf_26 = _safe(y2026["profit_factor"])
    tr_25 = int(y2025["trades"])
    tr_26 = int(y2026["trades"])
    wr_25 = _safe(y2025["win_rate"])
    wr_26 = _safe(y2026["win_rate"])

    return_gap = abs(ret_25 - ret_26)
    pf_gap = abs(pf_25 - pf_26)
    win_gap = abs(wr_25 - wr_26)
    trade_balance = min(tr_25, tr_26) / max(tr_25, tr_26) if max(tr_25, tr_26) > 0 else 0.0
    same_sign = (ret_25 >= 0 and ret_26 >= 0) or (ret_25 <= 0 and ret_26 <= 0)

    # Higher is better. Focus on cross-year stability first.
    stability_score = (
        (10.0 if same_sign else 0.0)
        - return_gap * 0.8
        - pf_gap * 12.0
        - win_gap * 0.2
        + trade_balance * 10.0
    )
    return {
        "same_sign_return": same_sign,
        "return_gap_pct": return_gap,
        "pf_gap": pf_gap,
        "win_rate_gap_pct": win_gap,
        "trade_balance": trade_balance,
        "stability_score": stability_score,
    }


def _run_segment(strategy_name: str, seg_name: str, start_ms: int, end_ms: int) -> dict[str, Any]:
    cls = STRATEGY_REGISTRY[strategy_name]
    bar_ms = 15 * 60 * 1000
    start_bar = (start_ms // bar_ms) * bar_ms
    end_bar = (end_ms // bar_ms) * bar_ms
    klines = kline_cache.load_range_as_klines(SYMBOL, INTERVAL, start_bar, end_bar)
    if not klines:
        raise RuntimeError(f"no klines for {seg_name}")

    tick_map = tick_cache.build_lazy_bar_map(
        [SYMBOL],
        [(k.open_time, k.close_time) for k in klines],
    )
    strategy = cls()
    strategy.allow_bar_fallback_in_tick_mode = False
    if hasattr(strategy, "configure_backtest_costs"):
        strategy.configure_backtest_costs(_resolve_fee_rate(CFG), CFG.slippage_bps)

    t0 = time.perf_counter()
    signals = strategy.on_history(klines, tick_map=tick_map)
    stats = simulate_trades(signals, CFG)
    dt = time.perf_counter() - t0

    covered, total = tick_map.observed_coverage()
    coverage = (covered / total * 100.0) if total else 0.0
    out = _brief(stats, runtime_sec=dt, coverage_pct=coverage, signals=len(signals))
    out["segment"] = seg_name
    out["bars"] = len(klines)
    out["range_utc"] = {
        "start": datetime.fromtimestamp(klines[0].open_time / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "end": datetime.fromtimestamp(klines[-1].close_time / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    return out


def main() -> None:
    info = tick_cache.info(SYMBOL)
    if info is None:
        raise RuntimeError("tick dataset missing")

    eff_start = int(info["start_ms"])
    eff_end = int(info["end_ms"])
    y2025_start = max(eff_start, _ms(datetime(2025, 1, 1, tzinfo=timezone.utc)))
    y2025_end = min(eff_end, _ms(datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc)))
    y2026_start = max(eff_start, _ms(datetime(2026, 1, 1, tzinfo=timezone.utc)))
    y2026_end = min(eff_end, _ms(datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)))

    if y2025_start >= y2025_end or y2026_start >= y2026_end:
        raise RuntimeError("effective cross-year range is empty")

    report: dict[str, Any] = {
        "meta": {
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "strategy_names": STRATEGIES,
            "backtest_config": asdict(CFG),
            "effective_tick_range_utc": {
                "start": datetime.fromtimestamp(eff_start / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "end": datetime.fromtimestamp(eff_end / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            },
            "segments": {
                "2025": {
                    "start": datetime.fromtimestamp(y2025_start / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "end": datetime.fromtimestamp(y2025_end / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                },
                "2026": {
                    "start": datetime.fromtimestamp(y2026_start / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "end": datetime.fromtimestamp(y2026_end / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                },
            },
        },
        "results": [],
    }

    for name in STRATEGIES:
        print(f"[run] {name} (2025)")
        y25 = _run_segment(name, "2025", y2025_start, y2025_end)
        print(f"[run] {name} (2026)")
        y26 = _run_segment(name, "2026", y2026_start, y2026_end)
        rb = _robustness(y25, y26)
        row = {
            "strategy": name,
            "y2025": y25,
            "y2026": y26,
            "robustness": rb,
        }
        report["results"].append(row)
        print(
            f"[done] {name} stability={rb['stability_score']:.2f} "
            f"ret_gap={rb['return_gap_pct']:.2f} pf_gap={rb['pf_gap']:.3f}"
        )

    report["ranking_by_stability"] = [
        {
            "strategy": row["strategy"],
            "stability_score": row["robustness"]["stability_score"],
            "return_gap_pct": row["robustness"]["return_gap_pct"],
            "pf_gap": row["robustness"]["pf_gap"],
            "same_sign_return": row["robustness"]["same_sign_return"],
        }
        for row in sorted(report["results"], key=lambda x: x["robustness"]["stability_score"], reverse=True)
    ]

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"[saved] {OUT_PATH}")


if __name__ == "__main__":
    main()
