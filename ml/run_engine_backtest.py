"""
把訓練好的純 Tick 線性模型接成專案策略 (StrategyBase)，用真正的回測引擎
backtest.engine.simulate_trades 重跑——取代 ml/ 內部的簡化 PnL。

差別（相對 ml 簡化版）：
  - 真實手續費：名目 × 費率，開倉 + 平倉各一次
  - 成交價：訊號在 K 棒收盤產生，用「下一根開盤價」成交（消 look-ahead）
  - 倉位 / 複利 / 滑價 / 維持保證金：全由 backtest.engine 處理
  - leverage=1 + 無 stop → 名目=權益（≈研究版 ±1 曝險），可比

訊號來源：沿用走前驗證（逐折重訓、防洩漏）的 OOS 預測 + hold 門檻，縫接成連續訊號。

用法：
    source .venv/bin/activate
    python -m ml.run_engine_backtest                 # 預設掃幾個操作點
    python -m ml.run_engine_backtest --q 0.7 --slippage-bps 1
"""
from __future__ import annotations

import argparse
import contextlib
import io
from typing import List, Optional

import numpy as np
import torch

from backtest.engine import BacktestConfig, simulate_trades
from core.data_types import Kline
from core import kline_cache
from ml.tick_features import MS_PER_HOUR
from ml.train_linear_logret import Config, train_linear
from ml.walkforward import build_dataset_with_time

from strategies.base import StrategyBase, StrategySignal, TickBarMap


# ──────────────────────────────────────────────────────────────────────────────
# 12h K 棒（給引擎用的價格序列）
# ──────────────────────────────────────────────────────────────────────────────
def resample_12h_klines(cfg: Config) -> list[Kline]:
    arr = np.load(kline_cache.cache_path(cfg.symbol, cfg.source_interval))
    arr = arr[np.argsort(arr[:, 0])]
    bucket_ms = cfg.horizon_hours * MS_PER_HOUR
    bids = (arr[:, 0] // bucket_ms).astype(np.int64)
    uniq, first = np.unique(bids, return_index=True)
    last = np.append(first[1:] - 1, len(arr) - 1)
    o = arr[first, 1]; h = np.maximum.reduceat(arr[:, 2], first)
    lo = np.minimum.reduceat(arr[:, 3], first); c = arr[last, 4]
    v = np.add.reduceat(arr[:, 5], first)
    t = (uniq * bucket_ms).astype(np.int64)
    out = []
    for i in range(len(uniq)):
        out.append(Kline(symbol=cfg.symbol, interval=f"{cfg.horizon_hours}h",
                         open_time=int(t[i]), close_time=int(t[i] + bucket_ms - 1),
                         open=float(o[i]), high=float(h[i]), low=float(lo[i]),
                         close=float(c[i]), volume=float(v[i]),
                         taker_buy_volume=0.0, is_closed=True))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# 走前驗證 OOS 預測 + hold 門檻 → 每根決策 K 棒的目標倉位
# ──────────────────────────────────────────────────────────────────────────────
def walkforward_positions(cfg: Config, train_size: int, test_size: int, q: float) -> dict[int, int]:
    X, y, bar_time, names = build_dataset_with_time(cfg, use_lags=False, use_tick=True)
    n = len(X)
    bt_oos, pred_oos, cut_oos = [], [], []
    start = 0
    while start + train_size + test_size <= n:
        tr = slice(start, start + train_size)
        te = slice(start + train_size, start + train_size + test_size)
        mu = X[tr].mean(0); sd = X[tr].std(0); sd[sd == 0] = 1.0
        Xtr, Xte = (X[tr] - mu) / sd, (X[te] - mu) / sd
        torch.manual_seed(cfg.seed)
        with contextlib.redirect_stdout(io.StringIO()):
            model = train_linear(Xtr, y[tr], cfg)
        with torch.no_grad():
            ptr = model(torch.tensor(Xtr, dtype=torch.float32)).squeeze(1).numpy()
            pte = model(torch.tensor(Xte, dtype=torch.float32)).squeeze(1).numpy()
        cutoff = 0.0 if q == 0.0 else float(np.quantile(np.abs(ptr), q))
        bt_oos.append(bar_time[te]); pred_oos.append(pte)
        cut_oos.append(np.full(len(pte), cutoff))
        start += test_size

    bt = np.concatenate(bt_oos); pred = np.concatenate(pred_oos); cut = np.concatenate(cut_oos)
    # hold 邏輯：低信心續抱前一倉位
    pos = np.zeros(len(pred)); last = 0.0
    for i in range(len(pred)):
        if abs(pred[i]) >= cut[i]:
            last = np.sign(pred[i])
        pos[i] = last
    return {int(bt[i]): int(pos[i]) for i in range(len(bt))}, names, len(bt)


# ──────────────────────────────────────────────────────────────────────────────
# ML 策略：目標倉位 → StrategySignal（下一根開盤成交）
# ──────────────────────────────────────────────────────────────────────────────
class MLTickStrategy(StrategyBase):
    name = "ML_Tick_Linear"

    def __init__(self, positions: dict[int, int]) -> None:
        self.positions = positions   # bar_open_time → 目標倉位 (-1/0/+1)，持有「下一根」

    def on_history(self, klines: List[Kline],
                   tick_map: Optional[TickBarMap] = None) -> List[StrategySignal]:
        sigs: List[StrategySignal] = []
        cur = 0
        for i, k in enumerate(klines):
            if k.open_time not in self.positions or i + 1 >= len(klines):
                continue
            target = self.positions[k.open_time]
            if target == cur:
                continue
            nb = klines[i + 1]   # 下一根開盤成交（消 look-ahead）
            if target == 1:
                sigs.append(StrategySignal(k.open_time, k.close, "long_entry",
                                           label="ML long", fill_price=nb.open, fill_time=nb.open_time))
            elif target == -1:
                sigs.append(StrategySignal(k.open_time, k.close, "short_entry",
                                           label="ML short", fill_price=nb.open, fill_time=nb.open_time))
            else:  # target == 0 → 平掉現有倉位
                st = "long_exit" if cur == 1 else "short_exit"
                sigs.append(StrategySignal(k.open_time, k.close, st,
                                           label="ML flat", fill_price=nb.open, fill_time=nb.open_time))
            cur = target
        # 收尾：最後一根平倉，使 PnL 全數實現
        if cur != 0 and klines:
            kl = klines[-1]
            st = "long_exit" if cur == 1 else "short_exit"
            sigs.append(StrategySignal(kl.open_time, kl.close, st,
                                       label="ML close", fill_price=kl.close, fill_time=kl.open_time))
        return sigs


def run_one(klines, positions, leverage, slippage_bps):
    strat = MLTickStrategy(positions)
    signals = strat.on_history(klines)
    cfg_bt = BacktestConfig(initial_capital=10_000.0, leverage=leverage,
                            fee_mode="Taker", slippage_bps=slippage_bps,
                            max_loss_pct=0.02, compound=True)
    return simulate_trades(signals, cfg_bt), len(signals)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=Config.symbol)
    ap.add_argument("--horizon-hours", type=int, default=Config.horizon_hours)
    ap.add_argument("--train", type=int, default=730)
    ap.add_argument("--test", type=int, default=182)
    ap.add_argument("--leverage", type=int, default=1)
    ap.add_argument("--epochs", type=int, default=Config.epochs)
    ap.add_argument("--q", type=float, default=None, help="只跑單一門檻 q")
    ap.add_argument("--slippage-bps", type=float, default=None)
    args = ap.parse_args()
    cfg = Config(symbol=args.symbol, horizon_hours=args.horizon_hours, epochs=args.epochs)

    print("重採樣 12h K 棒 …")
    klines = resample_12h_klines(cfg)

    # 操作點：(q, slippage_bps)
    if args.q is not None:
        points = [(args.q, args.slippage_bps if args.slippage_bps is not None else 0.0)]
    else:
        points = [(0.0, 0.0), (0.7, 0.0), (0.7, 1.0)]

    print("=" * 100)
    print(f"  真實引擎回測 · 純 Tick 線性模型 · 走前 OOS · leverage={args.leverage} · Taker 0.05%")
    print("=" * 100)
    print(f"  {'門檻q':>6} {'滑價bps':>7} {'交易':>6} {'勝率%':>7} {'獲利因子':>8} "
          f"{'總報酬%':>9} {'最大回撤%':>9} {'Sharpe':>7} {'手續費$':>10} {'多/空':>10}")
    print("-" * 100)
    saved = {}
    for q, slip in points:
        positions, names, n_oos = walkforward_positions(cfg, args.train, args.test, q)
        stats, n_sig = run_one(klines, positions, args.leverage, slip)
        saved[(q, slip)] = stats
        print(f"  {q:>6.2f} {slip:>7.1f} {stats['trades']:>6} {stats['win_rate']:>6.1f} "
              f"{stats['profit_factor']:>8.2f} {stats['total_return_pct']:>+9.1f} "
              f"{stats['max_drawdown_pct']:>9.1f} {stats['sharpe_ratio']:>+7.2f} "
              f"{stats['total_fees']:>10.0f} {stats['long_trades']:>4}/{stats['short_trades']:<4}")
    print("-" * 100)
    print(f"  OOS 決策 K 棒數 {n_oos} · 特徵 {names}")
    print("=" * 100)


if __name__ == "__main__":
    main()
