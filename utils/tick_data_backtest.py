from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
import zipfile

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestConfig, simulate_trades
from core.data_types import Kline
from core.tick_cache import (
    _parse_agg_trades_csv_lines,
    build_bar_map,
    build_tick_slice_accessor,
    load_raw,
    save_raw,
)
from strategies import STRATEGY_REGISTRY

# ── pandas 快速 CSV 解析（可選，fallback 到純 Python）────────────────────────
try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False


def _parse_one_zip_pandas(path: Path) -> np.ndarray:
    """用 pandas 解析單個 zip 內的 aggTrades CSV，比純 Python 快 10~50x。
    欄位順序（Binance data.binance.vision）：
      0 agg_id  1 price  2 qty  3 first_id  4 last_id  5 time_ms  6 is_buyer_maker
    輸出 ndarray (N, 4)：[time_ms, price, qty, is_buyer_maker(0/1)]
    """
    with zipfile.ZipFile(path) as zf:
        csv_names = sorted(n for n in zf.namelist() if n.lower().endswith(".csv"))
        if not csv_names:
            return np.empty((0, 4), dtype=np.float64)
        with zf.open(csv_names[0]) as fh:
            if _HAS_PANDAS:
                try:
                    df = pd.read_csv(
                        fh,
                        header=None,
                        usecols=[1, 2, 5, 6],
                        names=["price", "qty", "time", "is_bm"],
                        dtype={"price": np.float64, "qty": np.float64,
                               "time": np.float64, "is_bm": str},
                        on_bad_lines="skip",
                    )
                    # 去掉非數字 header 行（若有）
                    mask = pd.to_numeric(df["price"], errors="coerce").notna()
                    df = df[mask]
                    if df.empty:
                        return np.empty((0, 4), dtype=np.float64)
                    arr = np.empty((len(df), 4), dtype=np.float64)
                    arr[:, 0] = df["time"].to_numpy(dtype=np.float64)
                    arr[:, 1] = df["price"].to_numpy(dtype=np.float64)
                    arr[:, 2] = df["qty"].to_numpy(dtype=np.float64)
                    arr[:, 3] = (
                        df["is_bm"].str.strip().str.lower() == "true"
                    ).to_numpy(dtype=np.float64)
                    return arr
                except Exception:
                    pass  # fallback 到純 Python
            # fallback：原始純 Python 解析
            return _parse_agg_trades_csv_lines(fh)


def _load_ticks_from_zip_dir(
    tick_dir: Path,
    symbol: str,
    max_workers: int = 4,
) -> np.ndarray:
    """從 zip 目錄載入 tick 資料。
    - 優先使用 pandas 解析（快 10~50x）
    - 平行開啟多個 zip（ThreadPoolExecutor，最多 max_workers 個）
    """
    paths = sorted(tick_dir.glob(f"{symbol.upper()}*.zip"))
    if not paths:
        raise FileNotFoundError(
            f"no tick zip files found for {symbol} in {tick_dir}"
        )

    print(f"[load] {len(paths)} zip files, pandas={'yes' if _HAS_PANDAS else 'no (pip install pandas)'}, workers={max_workers}")
    t0 = time.perf_counter()

    parts: list[np.ndarray] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_parse_one_zip_pandas, p): p for p in paths}
        done = 0
        for fut in as_completed(futures):
            done += 1
            arr = fut.result()
            if len(arr) > 0:
                parts.append(arr)
            if done % 10 == 0 or done == len(paths):
                elapsed = time.perf_counter() - t0
                print(f"  [{done}/{len(paths)}] {elapsed:.1f}s", end="\r", flush=True)

    print()  # newline after \r

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

    elapsed = time.perf_counter() - t0
    print(f"[load] done: {len(ticks):,} ticks in {elapsed:.1f}s")
    return ticks


def _load_ticks_cached(
    tick_dir: Path,
    symbol: str,
    rebuild: bool = False,
    max_workers: int = 4,
) -> np.ndarray:
    """NPZ 快取層：第一次解析後存成 .npz，後續直接載入（秒級）。
    - 快取路徑：data/ticks/{SYMBOL}_ticks.npz（同 tick_cache.py 約定）
    - --rebuild-cache 強制重建
    """
    if not rebuild:
        data, meta = load_raw(symbol)
        if data is not None and len(data) > 0:
            print(
                f"[cache] hit: {len(data):,} ticks "
                f"({data.nbytes / 1_048_576:.0f} MB in RAM)"
            )
            return data
        print("[cache] miss, parsing from zip files...")

    ticks = _load_ticks_from_zip_dir(tick_dir, symbol, max_workers=max_workers)

    if len(ticks) > 0:
        t0 = time.perf_counter()
        save_raw(symbol, ticks, int(ticks[0, 0]), int(ticks[-1, 0]))
        print(f"[cache] saved in {time.perf_counter() - t0:.1f}s")

    return ticks


# ── Kline 重建 ─────────────────────────────────────────────────────────────

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
        raise ValueError(
            f"unsupported interval '{interval}'. choose from: {list(_INTERVAL_MS)}"
        )

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


# ── 月度分析 ───────────────────────────────────────────────────────────────

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


# ── 主程式 ─────────────────────────────────────────────────────────────────

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
    # ── 快取控制 ──────────────────────────────────────────────────────────
    parser.add_argument(
        "--no-cache", action="store_true",
        help="skip NPZ cache, always parse from zip files (slow but fresh)",
    )
    parser.add_argument(
        "--rebuild-cache", action="store_true",
        help="force re-parse from zip files and overwrite existing NPZ cache",
    )
    parser.add_argument(
        "--zip-workers", type=int, default=4,
        help="parallel workers for zip loading (default: 4)",
    )
    parser.add_argument(
        "--tick-access", choices=["map", "range"], default="map",
        help="tick access mode: materialized dict map or lazy range accessor (default: map)",
    )
    args = parser.parse_args()

    strategy_cls = STRATEGY_REGISTRY.get(args.strategy)
    if strategy_cls is None:
        names = ", ".join(sorted(STRATEGY_REGISTRY))
        raise SystemExit(f"unknown strategy: {args.strategy}. available: {names}")

    tick_dir = Path(args.tick_dir)

    # ── 載入 tick（優先走快取）──────────────────────────────────────────
    t_load = time.perf_counter()
    if args.no_cache:
        ticks = _load_ticks_from_zip_dir(tick_dir, args.symbol, args.zip_workers)
    else:
        ticks = _load_ticks_cached(
            tick_dir, args.symbol,
            rebuild=args.rebuild_cache,
            max_workers=args.zip_workers,
        )

    klines = _build_klines_from_ticks(args.symbol, ticks, interval=args.interval)
    kline_times = [(k.open_time, k.close_time) for k in klines]
    if args.tick_access == "range":
        tick_map = build_tick_slice_accessor(ticks, kline_times)
    else:
        tick_map = build_bar_map(ticks, kline_times)
    print(
        f"[prep] bars={len(klines)} tick_access={args.tick_access} "
        f"coverage={len(tick_map)} in {time.perf_counter()-t_load:.1f}s"
    )

    # ── 執行策略 ────────────────────────────────────────────────────────
    t_strat = time.perf_counter()
    strategy = strategy_cls()
    signals = strategy.on_history(klines, tick_map=tick_map)
    print(f"[strategy] signals={len(signals)} in {time.perf_counter()-t_strat:.1f}s")

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
    last_bar  = datetime.fromtimestamp(klines[-1].open_time / 1000, tz=timezone.utc)

    print(f"strategy={args.strategy}")
    print(f"symbol={args.symbol.upper()}  interval={args.interval}")
    print(f"tick_source={tick_dir.resolve()}")
    print(f"tick_access={args.tick_access}")
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
