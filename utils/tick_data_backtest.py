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
from core.tick_cache import _parse_agg_trades_csv_lines, build_bar_map
from strategies import STRATEGY_REGISTRY


def _load_ticks_from_zip_dir(tick_dir: Path, symbol: str) -> np.ndarray:
    import zipfile

    paths = sorted(tick_dir.glob(f"{symbol.upper()}*.zip"))
    if not paths:
        raise FileNotFoundError(f"no tick zip files found for {symbol} in {tick_dir}")

    parts: list[np.ndarray] = []
    for path in paths:
        with zipfile.ZipFile(path) as zf:
            csv_names = sorted(n for n in zf.namelist() if n.lower().endswith(".csv"))
            if not csv_names:
                continue
            with zf.open(csv_names[0]) as fh:
                arr = _parse_agg_trades_csv_lines(fh)
            if len(arr) > 0:
                parts.append(arr)

    if not parts:
        return np.empty((0, 4), dtype=np.float64)

    ticks = np.concatenate(parts, axis=0)
    order = np.argsort(ticks[:, 0], kind="stable")
    ticks = ticks[order]
    if len(ticks) > 1:
        diff = np.diff(ticks[:, :3], axis=0)
        keep = np.ones(len(ticks), dtype=bool)
        keep[1:] = np.any(diff != 0, axis=1)
        ticks = ticks[keep]
    return ticks


_INTERVAL_MS: dict[str, int] = {
    "1m":  60_000,
    "3m":  3  * 60_000,
    "5m":  5  * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h":  60 * 60_000,
    "4h":  4  * 60 * 60_000,
}


def _build_klines_from_ticks(
    symbol: str,
    ticks: np.ndarray,
    interval: str = "1m",
) -> list[Kline]:
    """Aggregate raw tick data into OHLCV klines of any supported interval."""
    if len(ticks) == 0:
        return []
    bar_ms = _INTERVAL_MS.get(interval)
    if bar_ms is None:
        raise ValueError(f"unsupported interval '{interval}'. choose from: {list(_INTERVAL_MS)}")

    buckets = (ticks[:, 0].astype(np.int64) // bar_ms) * bar_ms
    open_times, starts = np.unique(buckets, return_index=True)

    klines: list[Kline] = []
    for idx, open_time in enumerate(open_times):
        lo = starts[idx]
        hi = starts[idx + 1] if idx + 1 < len(starts) else len(ticks)
        chunk = ticks[lo:hi]
        prices = chunk[:, 1]
        qty = chunk[:, 2]
        is_buyer_maker = chunk[:, 3] > 0.5
        klines.append(
            Kline(
                symbol=symbol.upper(),
                interval=interval,
                open_time=int(open_time),
                close_time=int(open_time + bar_ms - 1),
                open=float(prices[0]),
                high=float(np.max(prices)),
                low=float(np.min(prices)),
                close=float(prices[-1]),
                volume=float(np.sum(qty)),
                taker_buy_volume=float(np.sum(qty[~is_buyer_maker])),
                is_closed=True,
            )
        )
    return klines


def _build_1m_klines_from_ticks(symbol: str, ticks: np.ndarray) -> list[Kline]:
    """Backward-compatible wrapper for 1m kline building."""
    return _build_klines_from_ticks(symbol, ticks, interval="1m")


def _monthly_breakdown(trade_list: list[dict]) -> list[str]:
    monthly = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for trade in trade_list:
        month = datetime.fromtimestamp(
            trade["entry_time"] / 1000, tz=timezone.utc
        ).strftime("%Y-%m")
        monthly[month]["trades"] += 1
        monthly[month]["pnl"] += trade["net_pnl"]
        if trade["net_pnl"] > 0:
            monthly[month]["wins"] += 1

    lines: list[str] = []
    for month in sorted(monthly):
        row = monthly[month]
        win_rate = 0.0
        if row["trades"] > 0:
            win_rate = row["wins"] / row["trades"] * 100.0
        lines.append(
            f"{month}: trades={row['trades']} pnl={row['pnl']:.4f} win_rate={win_rate:.2f}%"
        )
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run project backtests on 1m bars reconstructed directly from tick_data zip files."
    )
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--strategy", default="Wick Reversal 1m v4")
    parser.add_argument("--interval", default="1m",
                        choices=list(_INTERVAL_MS),
                        help="bar interval for kline reconstruction (default: 1m)")
    parser.add_argument("--tick-dir", default="tick_data")
    parser.add_argument("--initial-capital", type=float, default=1650.0)
    parser.add_argument("--max-loss-pct", type=float, default=0.02)
    parser.add_argument("--leverage", type=int, default=20)
    parser.add_argument("--fee-mode", default="Taker")
    parser.add_argument("--custom-fee-rate", type=float, default=0.00032)
    parser.add_argument("--slippage-bps", type=float, default=0.2)
    parser.add_argument("--funding-rate", type=float, default=0.0)
    parser.add_argument("--maint-margin", type=float, default=0.005)
    args = parser.parse_args()

    strategy_cls = STRATEGY_REGISTRY.get(args.strategy)
    if strategy_cls is None:
        names = ", ".join(sorted(STRATEGY_REGISTRY))
        raise SystemExit(f"unknown strategy: {args.strategy}. available: {names}")

    ticks = _load_ticks_from_zip_dir(Path(args.tick_dir), args.symbol)
    klines = _build_klines_from_ticks(args.symbol, ticks, interval=args.interval)
    tick_map = build_bar_map(ticks, [(k.open_time, k.close_time) for k in klines])

    strategy = strategy_cls()
    signals = strategy.on_history(klines, tick_map=tick_map)
    cfg = BacktestConfig(
        initial_capital=args.initial_capital,
        max_loss_pct=args.max_loss_pct,
        leverage=args.leverage,
        fee_mode=args.fee_mode,
        custom_fee_rate=args.custom_fee_rate,
        slippage_bps=args.slippage_bps,
        funding_rate=args.funding_rate,
        maint_margin=args.maint_margin,
        compound=True,
    )
    stats = simulate_trades(signals, cfg)

    first_bar = datetime.fromtimestamp(klines[0].open_time / 1000, tz=timezone.utc)
    last_bar = datetime.fromtimestamp(klines[-1].open_time / 1000, tz=timezone.utc)

    print(f"strategy={args.strategy}")
    print(f"symbol={args.symbol.upper()}  interval={args.interval}")
    print(f"tick_source={Path(args.tick_dir).resolve()}")
    print(f"bars={len(klines)} tick_coverage={len(tick_map)}/{len(klines)}")
    print(f"range_utc={first_bar} -> {last_bar}")
    print(f"trades={stats['trades']}")
    print(f"win_rate={stats['win_rate']:.4f}")
    print(f"profit_factor={stats['profit_factor']:.4f}")
    print(f"total_net_pnl={stats['total_net_pnl']:.4f}")
    print(f"max_drawdown_pct={stats['max_drawdown_pct']:.4f}")
    print("monthly:")
    for line in _monthly_breakdown(stats["trade_list"]):
        print(f"  {line}")


if __name__ == "__main__":
    main()
