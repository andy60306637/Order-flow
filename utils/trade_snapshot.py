"""
Tick-level backtest K-bar snapshot visualization.

For each trade, generates a candlestick chart showing:
  - k0 signal confirmation bar
  - entry point (with fill price if tick mode)
  - exit point
  - stop loss / take profit levels

Usage:
    python utils/trade_snapshot.py --symbol BTCUSDT --tick-dir tick_data --out-dir snapshots
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sys

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestConfig, simulate_trades
from core.data_types import Kline
from core.tick_cache import build_bar_map
from strategies import STRATEGY_REGISTRY
from strategies.base import StrategySignal
from utils.tick_data_backtest import _load_ticks_from_zip_dir, _build_klines_from_ticks


# ═══════════════════════════════════════════════════════════════════════════
# 繪圖
# ═══════════════════════════════════════════════════════════════════════════

def _ensure_matplotlib():
    """Lazy import matplotlib with Agg backend for headless rendering."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.patches import FancyBboxPatch
    return plt, mpatches


def _find_kline_idx_by_time(klines: list[Kline], open_time_ms: int) -> int | None:
    """Binary search for kline index by open_time."""
    lo, hi = 0, len(klines) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if klines[mid].open_time == open_time_ms:
            return mid
        elif klines[mid].open_time < open_time_ms:
            lo = mid + 1
        else:
            hi = mid - 1
    return None


def _collect_trade_contexts(
    signals: list[StrategySignal],
    trade_list: list[dict],
    klines: list[Kline],
    context_bars: int = 10,
) -> list[dict]:
    """
    Match each trade with its associated signals and kline context.

    Returns a list of dicts:
      {
        "trade": dict,          # from simulate_trades trade_list
        "trade_idx": int,       # 0-based trade number
        "k0_signal": signal,    # k0_long signal
        "entry_signal": signal, # long_entry signal
        "exit_signal": signal,  # long_exit signal
        "kline_start": int,     # kline index start for chart window
        "kline_end": int,       # kline index end for chart window
      }
    """
    # Index signals by open_time for fast lookup
    sig_by_time: dict[int, list[StrategySignal]] = defaultdict(list)
    for s in signals:
        sig_by_time[s.open_time].append(s)

    # Build ordered lists of k0 signals for each direction
    k0_long_signals  = [s for s in signals if s.signal_type == "k0_long"]
    k0_short_signals = [s for s in signals if s.signal_type == "k0_short"]

    results = []
    for ti, trade in enumerate(trade_list):
        if trade.get("skipped"):
            continue

        entry_time = trade.get("entry_time", 0)
        exit_time  = trade.get("exit_time", 0)
        direction  = trade.get("dir", "long")

        if direction == "short":
            entry_type, exit_type, k0_pool = "short_entry", "short_exit", k0_short_signals
        else:
            entry_type, exit_type, k0_pool = "long_entry",  "long_exit",  k0_long_signals

        # Find entry and exit signals
        entry_sig = None
        exit_sig  = None
        for s in sig_by_time.get(entry_time, []):
            if s.signal_type == entry_type:
                entry_sig = s
                break
        for s in sig_by_time.get(exit_time, []):
            if s.signal_type == exit_type:
                exit_sig = s
                break

        if entry_sig is None:
            continue

        # Find the most recent k0 before entry
        k0_sig = None
        for k0s in reversed(k0_pool):
            if k0s.open_time <= entry_time:
                k0_sig = k0s
                break

        # Find kline indices
        entry_ki = _find_kline_idx_by_time(klines, entry_time)
        exit_ki = _find_kline_idx_by_time(klines, exit_time) if exit_time else None
        k0_ki = _find_kline_idx_by_time(klines, k0_sig.open_time) if k0_sig else None

        # Determine chart window
        earliest = entry_ki or 0
        if k0_ki is not None:
            earliest = min(earliest, k0_ki)
        latest = exit_ki if exit_ki is not None else earliest

        start = max(0, earliest - context_bars)
        end = min(len(klines) - 1, latest + context_bars)

        results.append({
            "trade": trade,
            "trade_idx": ti,
            "k0_signal": k0_sig,
            "entry_signal": entry_sig,
            "exit_signal": exit_sig,
            "kline_start": start,
            "kline_end": end,
            "k0_ki": k0_ki,
            "entry_ki": entry_ki,
            "exit_ki": exit_ki,
        })

    return results


def render_trade_snapshot(
    ctx: dict,
    klines: list[Kline],
    tick_map: dict | None,
    out_path: Path,
) -> None:
    """Render a single trade snapshot to a PNG file."""
    plt, mpatches = _ensure_matplotlib()

    trade = ctx["trade"]
    start = ctx["kline_start"]
    end = ctx["kline_end"]
    window = klines[start : end + 1]
    n = len(window)
    if n == 0:
        return

    fig, (ax_candle, ax_vol) = plt.subplots(
        2, 1,
        figsize=(max(14, n * 0.5), 8),
        height_ratios=[3, 1],
        sharex=True,
        gridspec_kw={"hspace": 0.05},
    )

    # ── Candlestick chart ──────────────────────────────────────────────────
    for i, k in enumerate(window):
        x = i
        color = "#26a69a" if k.close >= k.open else "#ef5350"
        body_low = min(k.open, k.close)
        body_high = max(k.open, k.close)
        body_h = max(body_high - body_low, (k.high - k.low) * 0.005)

        # Wick
        ax_candle.plot([x, x], [k.low, k.high], color=color, linewidth=0.8)
        # Body
        ax_candle.bar(
            x, body_h, bottom=body_low, width=0.6,
            color=color, edgecolor=color, linewidth=0.5,
        )

    # ── Volume bars ────────────────────────────────────────────────────────
    for i, k in enumerate(window):
        color = "#26a69a" if k.close >= k.open else "#ef5350"
        ax_vol.bar(i, k.volume, width=0.6, color=color, alpha=0.6)

    # ── Highlight k0 bar ───────────────────────────────────────────────────
    k0_sig = ctx.get("k0_signal")
    k0_ki = ctx.get("k0_ki")
    if k0_ki is not None and start <= k0_ki <= end:
        xi = k0_ki - start
        k0_bar = klines[k0_ki]
        # Highlight background
        ax_candle.axvspan(xi - 0.4, xi + 0.4, alpha=0.15, color="#ff9800", zorder=0)
        # Marker
        ax_candle.annotate(
            "k0",
            xy=(xi, k0_bar.low),
            xytext=(xi, k0_bar.low - (klines[start].high - klines[start].low) * 0.3),
            fontsize=9, fontweight="bold", color="#ff9800",
            ha="center", va="top",
            arrowprops=dict(arrowstyle="->", color="#ff9800", lw=1.2),
        )

    # ── Entry marker ───────────────────────────────────────────────────────
    entry_sig = ctx.get("entry_signal")
    entry_ki = ctx.get("entry_ki")
    entry_price = trade["entry"]
    if entry_ki is not None and start <= entry_ki <= end:
        xi = entry_ki - start
        # Entry point triangle
        ax_candle.scatter(
            xi, entry_price, marker="^", s=120, c="#2196f3",
            edgecolors="white", linewidths=0.8, zorder=5,
        )
        ax_candle.annotate(
            f"Entry\n{entry_price:.1f}",
            xy=(xi, entry_price),
            xytext=(xi + 0.8, entry_price),
            fontsize=8, color="#2196f3", fontweight="bold",
            ha="left", va="center",
        )

        # Fill price (tick mode actual fill)
        fill_p = entry_sig.fill_price if entry_sig and entry_sig.fill_price else None
        if fill_p and abs(fill_p - entry_price) > 0.01:
            ax_candle.scatter(
                xi, fill_p, marker="D", s=50, c="#1565c0",
                edgecolors="white", linewidths=0.5, zorder=5,
            )
            ax_candle.annotate(
                f"Fill {fill_p:.1f}",
                xy=(xi, fill_p),
                xytext=(xi + 0.8, fill_p),
                fontsize=7, color="#1565c0",
                ha="left", va="center",
            )

    # ── Exit marker ────────────────────────────────────────────────────────
    exit_sig = ctx.get("exit_signal")
    exit_ki = ctx.get("exit_ki")
    exit_price = trade["exit"]
    exit_label = trade.get("exit_label", "")
    if exit_ki is not None and start <= exit_ki <= end:
        xi = exit_ki - start
        exit_color = "#4caf50" if trade["net_pnl"] > 0 else "#f44336"
        ax_candle.scatter(
            xi, exit_price, marker="v", s=120, c=exit_color,
            edgecolors="white", linewidths=0.8, zorder=5,
        )
        ax_candle.annotate(
            f"{exit_label}\n{exit_price:.1f}",
            xy=(xi, exit_price),
            xytext=(xi + 0.8, exit_price),
            fontsize=8, color=exit_color, fontweight="bold",
            ha="left", va="center",
        )

    # ── Stop loss line ─────────────────────────────────────────────────────
    stop_p = trade.get("stop")
    if stop_p:
        ax_candle.axhline(
            stop_p, color="#f44336", linestyle="--", linewidth=1.0, alpha=0.7,
        )
        ax_candle.text(
            n - 0.5, stop_p, f"SL {stop_p:.1f}",
            fontsize=7, color="#f44336", va="bottom", ha="right",
        )

    # ── Take profit line ───────────────────────────────────────────────────
    # Reconstruct TP from entry + risk * rr
    direction = trade.get("dir", "long")
    if stop_p and entry_price:
        risk = entry_price - stop_p  # 正數 = long；負數 = short
        if direction == "short" and risk < 0:
            tp_p = entry_price + risk   # short TP 低於 entry
            ax_candle.axhline(
                tp_p, color="#4caf50", linestyle="--", linewidth=1.0, alpha=0.7,
            )
            ax_candle.text(
                n - 0.5, tp_p, f"TP {tp_p:.1f}",
                fontsize=7, color="#4caf50", va="top", ha="right",
            )
        elif direction == "long" and risk > 0:
            tp_p = entry_price + risk   # long TP 高於 entry
            ax_candle.axhline(
                tp_p, color="#4caf50", linestyle="--", linewidth=1.0, alpha=0.7,
            )
            ax_candle.text(
                n - 0.5, tp_p, f"TP {tp_p:.1f}",
                fontsize=7, color="#4caf50", va="bottom", ha="right",
            )

    # ── Tick scatter on entry bar (if tick data available) ──────────────────
    if tick_map and entry_ki is not None:
        entry_bar = klines[entry_ki]
        ticks = tick_map.get(entry_bar.open_time)
        if ticks is not None and len(ticks) > 0:
            xi = entry_ki - start
            prices = ticks[:, 1]
            is_bm = ticks[:, 3] > 0.5
            buy_mask = ~is_bm.astype(bool)
            # Show small dots for ticks in the entry bar
            offsets = np.linspace(-0.25, 0.25, len(prices))
            buy_prices = prices[buy_mask]
            sell_prices = prices[~buy_mask]
            buy_offsets = offsets[:len(buy_prices)] if len(buy_prices) <= len(offsets) else offsets
            sell_offsets = offsets[:len(sell_prices)] if len(sell_prices) <= len(offsets) else offsets
            if len(buy_prices) > 0:
                ax_candle.scatter(
                    np.full(len(buy_prices), xi) + np.linspace(-0.25, -0.05, len(buy_prices)),
                    buy_prices, s=2, c="#26a69a", alpha=0.3, zorder=1,
                )
            if len(sell_prices) > 0:
                ax_candle.scatter(
                    np.full(len(sell_prices), xi) + np.linspace(0.05, 0.25, len(sell_prices)),
                    sell_prices, s=2, c="#ef5350", alpha=0.3, zorder=1,
                )

    # ── X-axis time labels ─────────────────────────────────────────────────
    step = max(1, n // 12)
    tick_positions = list(range(0, n, step))
    tick_labels = []
    for pos in tick_positions:
        dt = datetime.fromtimestamp(window[pos].open_time / 1000, tz=timezone.utc)
        tick_labels.append(dt.strftime("%m-%d\n%H:%M"))
    ax_vol.set_xticks(tick_positions)
    ax_vol.set_xticklabels(tick_labels, fontsize=7)

    # ── Formatting ─────────────────────────────────────────────────────────
    pnl = trade["net_pnl"]
    pnl_str = f"+{pnl:.2f}" if pnl > 0 else f"{pnl:.2f}"
    pnl_color = "#4caf50" if pnl > 0 else "#f44336"

    entry_dt = datetime.fromtimestamp(trade["entry_time"] / 1000, tz=timezone.utc)
    title = (
        f"Trade #{ctx['trade_idx'] + 1}  |  "
        f"{trade['dir'].upper()}  |  "
        f"Entry: {entry_price:.1f}  Exit: {exit_price:.1f}  |  "
        f"PnL: {pnl_str} USDT  |  "
        f"{exit_label}  |  "
        f"{entry_dt.strftime('%Y-%m-%d %H:%M')} UTC"
    )
    ax_candle.set_title(title, fontsize=10, fontweight="bold", color=pnl_color)
    ax_candle.set_ylabel("Price", fontsize=8)
    ax_vol.set_ylabel("Volume", fontsize=8)
    ax_candle.grid(True, alpha=0.2)
    ax_vol.grid(True, alpha=0.2)

    # Dark theme
    for ax in (ax_candle, ax_vol):
        ax.set_facecolor("#1e1e1e")
        ax.tick_params(colors="#aaa", labelsize=7)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["bottom"].set_color("#444")
        ax.spines["left"].set_color("#444")
    fig.patch.set_facecolor("#121212")

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=150, bbox_inches="tight")
    plt.close(fig)


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate tick-level K-bar snapshots for each trade from tick backtest."
    )
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--strategy", default="Wick Reversal 1m v4")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--tick-dir", default="tick_data")
    parser.add_argument("--out-dir", default="snapshots",
                        help="Output directory for snapshot PNGs")
    parser.add_argument("--context-bars", type=int, default=10,
                        help="Number of bars before/after trade to show")
    parser.add_argument("--initial-capital", type=float, default=1650.0)
    parser.add_argument("--max-loss-pct", type=float, default=0.02)
    parser.add_argument("--leverage", type=int, default=20)
    parser.add_argument("--fee-mode", default="Taker")
    parser.add_argument("--custom-fee-rate", type=float, default=0.00032)
    parser.add_argument("--slippage-bps", type=float, default=0.2)
    args = parser.parse_args()

    strategy_cls = STRATEGY_REGISTRY.get(args.strategy)
    if strategy_cls is None:
        names = ", ".join(sorted(STRATEGY_REGISTRY))
        raise SystemExit(f"unknown strategy: {args.strategy}. available: {names}")

    print(f"Loading ticks from {args.tick_dir}/ ...")
    ticks = _load_ticks_from_zip_dir(Path(args.tick_dir), args.symbol)
    print(f"  {len(ticks)} ticks loaded")

    klines = _build_klines_from_ticks(args.symbol, ticks, interval=args.interval)
    tick_map = build_bar_map(ticks, [(k.open_time, k.close_time) for k in klines])
    print(f"  {len(klines)} bars, tick coverage {len(tick_map)}/{len(klines)}")

    strategy = strategy_cls()
    signals = strategy.on_history(klines, tick_map=tick_map)

    cfg = BacktestConfig(
        initial_capital=args.initial_capital,
        max_loss_pct=args.max_loss_pct,
        leverage=args.leverage,
        fee_mode=args.fee_mode,
        custom_fee_rate=args.custom_fee_rate,
        slippage_bps=args.slippage_bps,
        compound=True,
    )
    stats = simulate_trades(signals, cfg)

    trade_list = stats["trade_list"]
    active_trades = [t for t in trade_list if not t.get("skipped")]
    print(f"  {len(active_trades)} trades found")

    if not active_trades:
        print("No trades to snapshot.")
        return

    contexts = _collect_trade_contexts(
        signals, trade_list, klines, context_bars=args.context_bars,
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Rendering {len(contexts)} snapshots to {out_dir}/ ...")
    for ctx in contexts:
        ti = ctx["trade_idx"]
        fname = f"trade_{ti + 1:03d}.png"
        render_trade_snapshot(ctx, klines, tick_map, out_dir / fname)
        pnl = ctx["trade"]["net_pnl"]
        label = ctx["trade"].get("exit_label", "")
        print(f"  [{ti + 1:3d}] {label:4s}  pnl={pnl:+.2f}  → {fname}")

    print(f"Done. {len(contexts)} snapshots saved to {out_dir}/")


if __name__ == "__main__":
    main()
