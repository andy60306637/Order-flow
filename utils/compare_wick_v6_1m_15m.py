from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestConfig, _resolve_fee_rate, simulate_trades
from core import kline_cache, tick_cache
from strategies.wick_reversal_v6 import (
    WickReversalV6Strategy,
    WickReversalV6_1mStrategy,
)

SYMBOL = "BTCUSDT"
TICK_SYMBOL = "BTCUSDT"

REQ_START = datetime(2025, 1, 1, tzinfo=timezone.utc)
REQ_END = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

OUT_PATH = PROJECT_ROOT / "docs" / "reports" / "wick_v6_1m_vs_15m_2025_2026_tick.json"
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

CFG = BacktestConfig(
    initial_capital=10_000.0,
    max_loss_pct=0.02,
    leverage=20,
    fee_mode="Taker",
    slippage_bps=0.0,
    funding_rate=0.0,
    maint_margin=0.005,
    compound=True,
)

INTERVAL_MS = {
    "1m": 60_000,
    "15m": 900_000,
}


def _fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _run_one(interval: str, strategy_cls) -> dict:
    tick_info = tick_cache.info(TICK_SYMBOL)
    if tick_info is None:
        raise RuntimeError(f"tick dataset not found: {TICK_SYMBOL}")

    req_start_ms = int(REQ_START.timestamp() * 1000)
    req_end_ms = int(REQ_END.timestamp() * 1000)
    start_ms = max(req_start_ms, int(tick_info["start_ms"]))
    end_ms = min(req_end_ms, int(tick_info["end_ms"]))
    if start_ms >= end_ms:
        raise RuntimeError("effective range is empty after clipping to tick coverage")

    bar_ms = INTERVAL_MS[interval]
    range_start_ms = (start_ms // bar_ms) * bar_ms
    range_end_ms = (end_ms // bar_ms) * bar_ms
    klines = kline_cache.load_range_as_klines(SYMBOL, interval, range_start_ms, range_end_ms)
    if not klines:
        raise RuntimeError(f"no klines for {interval} in effective range")

    kline_times = [(k.open_time, k.close_time) for k in klines]
    tick_map = tick_cache.build_lazy_bar_map([TICK_SYMBOL], kline_times)

    strategy = strategy_cls()
    strategy.allow_bar_fallback_in_tick_mode = False
    if hasattr(strategy, "configure_backtest_costs"):
        strategy.configure_backtest_costs(_resolve_fee_rate(CFG), CFG.slippage_bps)

    signals = strategy.on_history(klines, tick_map=tick_map)
    sim = simulate_trades(signals, CFG)

    covered, total = tick_map.observed_coverage()
    coverage = (covered / total * 100.0) if total else 0.0

    return {
        "interval": interval,
        "strategy_name": strategy.name,
        "requested_range_utc": {
            "start": REQ_START.strftime("%Y-%m-%d %H:%M:%S"),
            "end": REQ_END.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "effective_tick_range_utc": {
            "start": _fmt_ms(start_ms),
            "end": _fmt_ms(end_ms),
        },
        "bars": len(klines),
        "signals": len(signals),
        "tick_coverage_pct": coverage,
        "covered_bars": covered,
        "total_bars": total,
        "stats": {
            "trades": sim.get("trades", 0),
            "win_rate": sim.get("win_rate", 0.0),
            "profit_factor": sim.get("profit_factor", 0.0),
            "total_net_pnl": sim.get("total_net_pnl", 0.0),
            "total_return_pct": sim.get("total_return_pct", 0.0),
            "max_drawdown_pct": sim.get("max_drawdown_pct", 0.0),
            "sl_count": sim.get("sl_count", 0),
            "tp_count": sim.get("tp_count", 0),
            "ts_count": sim.get("ts_count", 0),
            "td_count": sim.get("td_count", 0),
        },
    }


def main() -> None:
    out = {
        "symbol": SYMBOL,
        "tick_symbol": TICK_SYMBOL,
        "config": {
            "initial_capital": CFG.initial_capital,
            "max_loss_pct": CFG.max_loss_pct,
            "leverage": CFG.leverage,
            "fee_mode": CFG.fee_mode,
            "slippage_bps": CFG.slippage_bps,
            "funding_rate": CFG.funding_rate,
            "maint_margin": CFG.maint_margin,
            "compound": CFG.compound,
        },
        "results": [],
    }

    runs = [
        ("15m", WickReversalV6Strategy),
        ("1m", WickReversalV6_1mStrategy),
    ]
    for interval, cls in runs:
        print(f"[run] interval={interval} strategy={cls.__name__}")
        out["results"].append(_run_one(interval, cls))
        print(f"[done] interval={interval}")

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)

    print(f"[saved] {OUT_PATH}")
    for item in out["results"]:
        s = item["stats"]
        print(
            f"{item['interval']}: trades={s['trades']} win_rate={s['win_rate']:.2f}% "
            f"net_pnl={s['total_net_pnl']:.2f} return={s['total_return_pct']:.2f}% "
            f"pf={s['profit_factor']:.3f}"
        )


if __name__ == "__main__":
    main()
