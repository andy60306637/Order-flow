"""
換手門檻優化 (Turnover Threshold) — 把已驗證的 gross edge 轉成更高的 net 報酬。

問題：原策略每根 K 棒都依 sign(pred) 進場 → 換手高 → 手續費吃光 edge。
做法：只在「預測強度 |pred| 夠大（高信心）」時才持有方向，否則空手 (flat)。
     連續同向高信心 → 不重複交易（不付費）；轉弱/翻向才動作。

防洩漏：門檻不是在測試集挑的。沿用走前驗證框架，每折「用訓練段預測的
        |pred| 分位數」決定門檻 c，再套用到該折測試段。掃描分位 q 比較。
        所有折的測試段淨報酬「縫接」成一條連續 OOS 曲線評估。

用法：
    source .venv/bin/activate
    python -m ml.threshold_optimize                 # 純 tick，掃描門檻
    python -m ml.threshold_optimize --train 730 --test 182
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path

import numpy as np
import torch

from ml.train_linear_logret import ARTIFACT_DIR, Config, train_linear
from ml.walkforward import build_dataset_with_time

QUANTILES = [0.0, 0.3, 0.5, 0.7, 0.8, 0.9]  # 0.0 = 永遠交易（原策略）


def _positions(pred: np.ndarray, cutoff: float, mode: str = "flat") -> np.ndarray:
    """
    |pred| >= cutoff 視為高信心訊號。
      mode="flat"：低信心時空手 (0)
      mode="hold"：低信心時續抱前一倉位（只在高信心時更新方向）→ 真正降換手
    """
    strong = np.abs(pred) >= cutoff
    sgn = np.sign(pred)
    if mode == "flat":
        return np.where(strong, sgn, 0.0)
    pos = np.zeros(len(pred))
    last = 0.0
    for i in range(len(pred)):
        if strong[i]:
            last = sgn[i]
        pos[i] = last
    return pos


def _net_series(pos: np.ndarray, actual: np.ndarray, fee: float):
    prev = np.concatenate([[0.0], pos[:-1]])
    turnover = np.abs(pos - prev)
    net = pos * actual - turnover * fee
    gross = pos * actual
    n_trades = int((turnover > 0).sum())
    in_market = float((pos != 0).mean())
    return net, gross, n_trades, in_market


def _sharpe(rets: np.ndarray, bars_per_year: float) -> float:
    s = rets.std()
    return float(rets.mean() / s * np.sqrt(bars_per_year)) if s > 0 else float("nan")


def run(cfg: Config, train_size: int, test_size: int, fee: float):
    X, y, bar_time, names = build_dataset_with_time(cfg, use_lags=False, use_tick=True)
    n = len(X)
    bars_per_year = (365 * 24) / cfg.horizon_hours
    print(f"資料集 {n} 列 · 特徵 {len(names)} · 滾動視窗 訓練{train_size}/測試{test_size}\n")

    # 先跑一次走前迴圈，蒐集每折的 (train_pred, test_pred, test_actual)
    fold_data = []
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
        fold_data.append((ptr, pte, y[te]))
        start += test_size

    results = []
    for mode in ("flat", "hold"):
        print("=" * 86)
        print(f"  換手門檻掃描 · 純 Tick 模型 · {len(fold_data)} 折縫接 OOS · 低信心模式={mode}")
        print("=" * 86)
        print(f"  {'分位q':>6} {'門檻(均)':>10} {'在場%':>7} {'總交易':>7} "
              f"{'GrossSh':>8} {'NetSh':>8} {'Net淨值':>9} {'折Net>0':>8}")
        print("-" * 86)
        for q in QUANTILES:
            all_net, all_gross, total_trades, in_mkts, cutoffs = [], [], 0, [], []
            fold_net_pos = 0
            for ptr, pte, act in fold_data:
                cutoff = 0.0 if q == 0.0 else float(np.quantile(np.abs(ptr), q))
                cutoffs.append(cutoff)
                pos = _positions(pte, cutoff, mode)
                net, gross, ntr, inm = _net_series(pos, act, fee)
                all_net.append(net); all_gross.append(gross)
                total_trades += ntr; in_mkts.append(inm)
                if net.sum() > 0:
                    fold_net_pos += 1
            net = np.concatenate(all_net); gross = np.concatenate(all_gross)
            net_sh = _sharpe(net, bars_per_year); gr_sh = _sharpe(gross, bars_per_year)
            net_eq = float(np.exp(net.sum()))
            row = {
                "mode": mode, "q": q, "cutoff_mean": float(np.mean(cutoffs)),
                "in_market": float(np.mean(in_mkts)), "total_trades": total_trades,
                "gross_sharpe": gr_sh, "net_sharpe": net_sh, "net_equity": net_eq,
                "folds_net_pos_ratio": fold_net_pos / len(fold_data),
            }
            results.append(row)
            print(f"  {q:>6.2f} {row['cutoff_mean']:>10.5f} {row['in_market']*100:>6.1f} "
                  f"{total_trades:>7} {gr_sh:>+8.2f} {net_sh:>+8.2f} "
                  f"{net_eq:>9.3f} {row['folds_net_pos_ratio']*100:>7.0f}%")
        print("-" * 86)

    best = max(results, key=lambda r: (r["net_sharpe"] if not np.isnan(r["net_sharpe"]) else -9))
    base = results[0]
    print(f"\n  最佳 net Sharpe：mode={best['mode']} q={best['q']}  Net Sharpe {best['net_sharpe']:+.2f}  "
          f"Net淨值 ×{best['net_equity']:.3f}  (交易 {best['total_trades']}, 在場 {best['in_market']*100:.0f}%)")
    print(f"  對照 原策略(flat q=0)：Net Sharpe {base['net_sharpe']:+.2f}  Net淨值 ×{base['net_equity']:.3f}  (交易 {base['total_trades']})")
    print("=" * 86)
    return results, best, names


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=Config.symbol)
    ap.add_argument("--horizon-hours", type=int, default=Config.horizon_hours)
    ap.add_argument("--train", type=int, default=730)
    ap.add_argument("--test", type=int, default=182)
    ap.add_argument("--epochs", type=int, default=Config.epochs)
    ap.add_argument("--lr", type=float, default=Config.lr)
    args = ap.parse_args()
    cfg = Config(symbol=args.symbol, horizon_hours=args.horizon_hours,
                 epochs=args.epochs, lr=args.lr)
    results, best, names = run(cfg, args.train, args.test, cfg.fee)

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    out = ARTIFACT_DIR / f"{cfg.symbol}_{cfg.horizon_hours}h_threshold.json"
    with open(out, "w") as f:
        json.dump({"config": {"horizon_hours": cfg.horizon_hours, "fee": cfg.fee,
                              "train_size": args.train, "test_size": args.test},
                   "feature_names": names, "sweep": results, "best": best}, f, indent=2)
    print(f"已保存：{out}")


if __name__ == "__main__":
    main()
