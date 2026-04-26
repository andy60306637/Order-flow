"""
v6.1 Baseline Diagnostic — Section 3 of v6.1_analysis.md

Usage:
  python utils/v61_diagnostic.py

Runs WickReversalV6_1Strategy on BTCUSDT 15m for 2023-04-14~2024-04-13
and prints the full diagnostic breakdown required by the analysis plan.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestConfig, simulate_trades, _group_stats, _calc_r
from core import kline_cache, tick_cache
from strategies.wick_reversal_v6_1 import WickReversalV6_1Strategy

# ── 回測參數（與 analysis doc 一致）───────────────────────────────────────────
CFG = BacktestConfig(
    initial_capital=10_000.0,
    leverage=20,
    fee_mode="自訂",
    custom_fee_rate=0.00032,
    slippage_bps=0.2,
    compound=False,
    maint_margin=0.004,
    max_loss_pct=0.02,
)

SYMBOL      = "BTCUSDT"
INTERVAL    = "15m"
TICK_SYMBOL = "BTCUSDT_20230414_20240413"


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _pf(v: float) -> str:
    return "inf" if v == float("inf") else f"{v:.3f}"


def _row(label: str, gs: dict) -> str:
    return (
        f"  {label:<28}  n={gs['trades']:>3}  wr={gs['win_rate']:>5.1f}%"
        f"  PF={_pf(gs['pf']):>6}  net={gs['net_pnl']:>8.1f}"
        f"  avgR={gs['avg_R']:>6.3f}  mae_r={gs['avg_mae_r']:>5.3f}"
        f"  mfe_r={gs['avg_mfe_r']:>5.3f}"
    )


def run() -> None:
    # ── 1. 載入 15m klines ───────────────────────────────────────────────
    ti = tick_cache.info(TICK_SYMBOL)
    if ti is None:
        print(f"[ERR] tick dataset not found: {TICK_SYMBOL}")
        return
    start_ms = ti["start_ms"]
    end_ms   = ti["end_ms"]
    print(f"Tick range : {_fmt(start_ms)} ~ {_fmt(end_ms)}")

    BAR_MS  = 15 * 60 * 1000
    bar_start = (start_ms // BAR_MS) * BAR_MS
    bar_end   = (end_ms   // BAR_MS) * BAR_MS
    klines = kline_cache.load_range_as_klines(SYMBOL, INTERVAL, bar_start, bar_end)
    if not klines:
        print("[ERR] no 15m kline data")
        return
    print(f"Klines     : {len(klines):,}  ({_fmt(klines[0].open_time)} ~ {_fmt(klines[-1].open_time)})")

    # ── 2. 載入 ticks ────────────────────────────────────────────────────
    ticks = tick_cache.load_range(TICK_SYMBOL, start_ms, end_ms)
    print(f"Ticks      : {len(ticks):,}")

    kline_times = [(k.open_time, k.close_time) for k in klines]
    tick_map = tick_cache.build_bar_map(ticks, kline_times)
    coverage = len(tick_map) / len(klines) * 100
    print(f"Coverage   : {coverage:.1f}%  ({len(tick_map):,}/{len(klines):,} bars)\n")

    # ── 3. 執行策略 ──────────────────────────────────────────────────────
    strat = WickReversalV6_1Strategy()
    strat.allow_bar_fallback_in_tick_mode = False
    signals = strat.on_history(klines, tick_map=tick_map)
    print(f"Signals    : {len(signals):,}")

    # ── 4. 回測 ──────────────────────────────────────────────────────────
    res = simulate_trades(signals, CFG)
    active = [t for t in res["trade_list"] if not t.get("skipped")]
    n = res["trades"]

    # ═══════════════════════════════════════════════════════════════════════
    print("\n" + "═"*70)
    print("SECTION 3 — v6.1 Baseline Diagnostic (2023-04-14 ~ 2024-04-13)")
    print("═"*70)

    # ── 整體 ──────────────────────────────────────────────────────────────
    print(f"\n[Overall]")
    print(f"  Trades      : {n}")
    print(f"  Win rate    : {res['win_rate']:.1f}%")
    print(f"  PF          : {_pf(res['profit_factor'])}")
    print(f"  Net PnL     : {res['total_net_pnl']:.2f} USDT")
    print(f"  Gross PnL   : {sum(t.get('gross_pnl',0) for t in active):.2f} USDT")
    print(f"  Total fees  : {res['total_fees']:.2f} USDT")
    print(f"  Return      : {res['total_return_pct']:.2f}%")
    print(f"  Max DD      : {res['max_drawdown_pct']:.2f}%")
    print(f"  SL/TP/TS/TD/TDD : {res['sl_count']} / {res['tp_count']} / {res['ts_count']} / {res['td_count']} / {res['tdd_count']}")

    # ── Exit label 統計 ──────────────────────────────────────────────────
    print(f"\n[Exit label breakdown]")
    for lbl, gs in res["exit_stats"].items():
        if gs["trades"] > 0:
            print(_row(lbl, gs))

    # ── 多空分離 ─────────────────────────────────────────────────────────
    print(f"\n[Side]")
    for side, gs in res["side_stats"].items():
        print(_row(side, gs))

    # ── Side + Regime ────────────────────────────────────────────────────
    print(f"\n[Side + Regime]")
    for key, gs in res["regime_side_stats"].items():
        if gs["trades"] > 0:
            print(_row(key, gs))

    # ── Wick type ────────────────────────────────────────────────────────
    print(f"\n[Wick type]")
    for wt, gs in res["wick_type_stats"].items():
        print(_row(wt, gs))

    print(f"\n[Side + Wick type]")
    for key, gs in res["side_wick_type_stats"].items():
        if gs["trades"] > 0:
            print(_row(key, gs))

    # ── Session hour ─────────────────────────────────────────────────────
    print(f"\n[Session hour (UTC)]")
    for hour in sorted(res["session_hour_stats"]):
        gs = res["session_hour_stats"][hour]
        print(_row(f"UTC {hour:02d}h", gs))

    # ── Entry delay bars ─────────────────────────────────────────────────
    print(f"\n[Entry delay bars]")
    for d, gs in res["entry_delay_bars_stats"].items():
        print(_row(f"delay={d}", gs))

    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n{'─'*70}")
    print("Q1. 虧損來源")
    long_gs  = res["side_stats"]["long"]
    short_gs = res["side_stats"]["short"]
    print(f"  Long  net={long_gs['net_pnl']:.1f}  short net={short_gs['net_pnl']:.1f}")

    worst_regime = max(res["regime_side_stats"].items(),
                       key=lambda kv: abs(kv[1]["net_pnl"]) if kv[1]["trades"] > 0 and kv[1]["net_pnl"] < 0 else 0)
    print(f"  Worst side+regime: {worst_regime[0]}  net={worst_regime[1]['net_pnl']:.1f}")

    if res["session_hour_stats"]:
        worst_h = min(res["session_hour_stats"].items(), key=lambda kv: kv[1]["net_pnl"])
        print(f"  Worst hour: UTC {worst_h[0]:02d}h  net={worst_h[1]['net_pnl']:.1f}")

    worst_exit = min(res["exit_stats"].items(), key=lambda kv: kv[1]["net_pnl"] if kv[1]["trades"]>0 else 0)
    print(f"  Worst exit: {worst_exit[0]}  net={worst_exit[1]['net_pnl']:.1f}")

    print(f"\nQ2. Gross edge vs net")
    gross = sum(t.get("gross_pnl", 0) for t in active)
    fees  = res["total_fees"]
    print(f"  Gross PnL : {gross:.2f}")
    print(f"  Net PnL   : {res['total_net_pnl']:.2f}")
    print(f"  Fee drag  : {fees:.2f}  ({fees/(abs(gross)+1e-9)*100:.1f}% of |gross|)")

    print(f"\nQ3. Long side")
    print(f"  {_row('long', long_gs)}")

    print(f"\nQ4. Short side")
    print(f"  {_row('short', short_gs)}")
    short_neutral = res["regime_side_stats"].get("short_neutral", {})
    print(f"  short+neutral: n={short_neutral.get('trades',0)}  net={short_neutral.get('net_pnl',0):.1f}")

    print(f"\nQ5. TDD value")
    tdd_gs = res["exit_stats"].get("TDD", {})
    if tdd_gs.get("trades", 0) > 0:
        print(f"  TDD: {_row('TDD', tdd_gs)}")
    else:
        print("  TDD: 0 trades")

    print(f"\nQ6. MFE_R distribution (RR=2.5 check)")
    thresholds = [0.5, 1.0, 1.5, 2.0, 2.5]
    mfe_r_vals = [t.get("mfe_r") for t in active if isinstance(t.get("mfe_r"), (int, float))]
    if mfe_r_vals:
        for thr in thresholds:
            pct = sum(1 for v in mfe_r_vals if v >= thr) / len(mfe_r_vals) * 100
            print(f"  >= {thr}R : {pct:.1f}%  ({sum(1 for v in mfe_r_vals if v >= thr)}/{len(mfe_r_vals)})")
    else:
        print("  (no MFE_R data — run in tick mode with v6.1)")

    # ── JSON 存檔 ────────────────────────────────────────────────────────
    out_path = PROJECT_ROOT / "docs" / "reports" / "v61_diagnostic.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "overall": {k: v for k, v in res.items() if k != "trade_list"},
        "exit_stats": res["exit_stats"],
        "side_stats": res["side_stats"],
        "regime_side_stats": res["regime_side_stats"],
        "wick_type_stats": res["wick_type_stats"],
        "side_wick_type_stats": res["side_wick_type_stats"],
        "session_hour_stats": {str(k): v for k, v in res["session_hour_stats"].items()},
        "entry_delay_bars_stats": {str(k): v for k, v in res["entry_delay_bars_stats"].items()},
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[Saved] {out_path}")


if __name__ == "__main__":
    run()
