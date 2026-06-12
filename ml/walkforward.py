"""
走前驗證 (Walk-Forward Validation) — 檢驗模型 edge 是否跨時間窗穩定。

做法：滾動視窗 (rolling-origin)，把時間軸切成多個「不重疊」的 train→test 折，
每折獨立重新訓練 + 重新 fit scaler（只用該折訓練段），在後續測試段評估。
重點不是單一窗的績效，而是 IC / Sharpe 是否「多數折為正且穩定」。

預設驗證純 Tick 模型（第二輪最佳），並可 --no-tick 跑落後報酬對照。

用法：
    source .venv/bin/activate
    python -m ml.walkforward                       # 純 tick，1y訓練/3mo測試
    python -m ml.walkforward --no-tick --n-lags 6  # 落後報酬對照
    python -m ml.walkforward --train 730 --test 182
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import torch

from ml import tick_features
from ml.train_linear_logret import ARTIFACT_DIR, Config, evaluate, load_resampled_closes, train_linear
from ml.train_linear_tickcvd import _shift


def build_dataset_with_time(cfg: Config, use_lags: bool, use_tick: bool):
    """同 train_linear_tickcvd 的對齊邏輯，但額外回傳每列的決策 bar 時間。"""
    bar_time_k, close_k = load_resampled_closes(cfg)
    bar_time_t, feat_t = tick_features.build_or_load(cfg.symbol, cfg.horizon_hours)
    common, idx_k, idx_t = np.intersect1d(bar_time_k, bar_time_t, return_indices=True)
    close = close_k[idx_k]
    feat = feat_t[idx_t]
    M = len(common)

    ret = np.full(M, np.nan)
    ret[1:] = np.diff(np.log(close))

    cols, names = [], []
    if use_lags:
        for i in range(cfg.n_lags):
            cols.append(_shift(ret, i)); names.append(f"ret_lag{i}")
    if use_tick:
        for j, nm in enumerate(tick_features.FEATURE_NAMES):
            cols.append(feat[:, j]); names.append(nm)

    X = np.column_stack(cols)
    y = _shift(ret, -1)
    valid = ~np.isnan(X).any(axis=1) & ~np.isnan(y)
    return X[valid], y[valid], common[valid], names


def _fmt_date(ms: int) -> str:
    return str(np.datetime64(int(ms), "ms").astype("datetime64[D]"))


def walk_forward(cfg: Config, use_lags: bool, use_tick: bool,
                 train_size: int, test_size: int):
    X, y, bar_time, names = build_dataset_with_time(cfg, use_lags, use_tick)
    n = len(X)
    print(f"資料集 {n} 列 · 特徵 {len(names)}: {names}")
    print(f"滾動視窗：訓練 {train_size} 根 / 測試 {test_size} 根\n")

    folds = []
    start = 0
    while start + train_size + test_size <= n:
        tr = slice(start, start + train_size)
        te = slice(start + train_size, start + train_size + test_size)

        # 只用該折訓練段 fit scaler（防洩漏）
        mu = X[tr].mean(0); sd = X[tr].std(0); sd[sd == 0] = 1.0
        Xtr = (X[tr] - mu) / sd
        Xte = (X[te] - mu) / sd

        torch.manual_seed(cfg.seed)
        with contextlib.redirect_stdout(io.StringIO()):   # 靜音逐折 epoch log
            model = train_linear(Xtr, y[tr], cfg)
        m = evaluate(model, Xte, y[te], cfg)
        folds.append({
            "fold": len(folds) + 1,
            "test_start": _fmt_date(bar_time[te][0]),
            "test_end": _fmt_date(bar_time[te][-1]),
            "n": int(m["test_samples"]),
            "ic": m["ic_pearson"],
            "hit": m["directional_hit_rate"],
            "gross_sharpe": m["pnl_gross"]["sharpe_annual"],
            "net_sharpe": m["pnl_net_after_fee"]["sharpe_annual"],
            "net_eq": m["pnl_net_after_fee"]["equity_multiple"],
            "bh_eq": m["buy_hold_equity_multiple"],
        })
        start += test_size
    return folds, names


def report(folds, names, label):
    print("\n" + "=" * 92)
    print(f"  走前驗證結果 · {label} · 共 {len(folds)} 折")
    print("=" * 92)
    print(f"  {'#':>2} {'測試區間':<24} {'樣本':>5} {'IC':>8} {'命中%':>7} "
          f"{'GrSh':>7} {'NetSh':>7} {'Net×':>7} {'B&H×':>7}")
    print("-" * 92)
    for f in folds:
        print(f"  {f['fold']:>2} {f['test_start']}~{f['test_end']:<12} {f['n']:>5} "
              f"{f['ic']:>+8.4f} {f['hit']*100:>6.1f} "
              f"{f['gross_sharpe']:>+7.2f} {f['net_sharpe']:>+7.2f} "
              f"{f['net_eq']:>7.3f} {f['bh_eq']:>7.3f}")
    print("-" * 92)

    ic = np.array([f["ic"] for f in folds])
    gsh = np.array([f["gross_sharpe"] for f in folds])
    nsh = np.array([f["net_sharpe"] for f in folds])
    hit = np.array([f["hit"] for f in folds])
    neq = np.array([f["net_eq"] for f in folds])

    def pct_pos(a): return f"{(a > 0).mean()*100:.0f}%"
    # IC 的 IR（穩定度）= mean/std
    ic_ir = ic.mean() / (ic.std() + 1e-12)
    print(f"  [彙總] IC: 均值 {ic.mean():+.4f}  中位 {np.median(ic):+.4f}  正比例 {pct_pos(ic)}  IR(穩定度) {ic_ir:+.2f}")
    print(f"         命中率 均值 {hit.mean()*100:.1f}%  > 50% 比例 {((hit>0.5).mean()*100):.0f}%")
    print(f"         Gross Sharpe 均值 {gsh.mean():+.2f}  正比例 {pct_pos(gsh)}")
    print(f"         Net   Sharpe 均值 {nsh.mean():+.2f}  正比例 {pct_pos(nsh)}  | Net淨值>1 比例 {((neq>1).mean()*100):.0f}%")
    print("=" * 92)
    return {
        "folds": folds, "n_folds": len(folds),
        "ic_mean": float(ic.mean()), "ic_median": float(np.median(ic)),
        "ic_pos_ratio": float((ic > 0).mean()), "ic_ir": float(ic_ir),
        "hit_mean": float(hit.mean()), "hit_gt50_ratio": float((hit > 0.5).mean()),
        "gross_sharpe_mean": float(gsh.mean()), "gross_sharpe_pos_ratio": float((gsh > 0).mean()),
        "net_sharpe_mean": float(nsh.mean()), "net_sharpe_pos_ratio": float((nsh > 0).mean()),
        "net_eq_gt1_ratio": float((neq > 1).mean()),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=Config.symbol)
    ap.add_argument("--horizon-hours", type=int, default=Config.horizon_hours)
    ap.add_argument("--n-lags", type=int, default=6)
    ap.add_argument("--train", type=int, default=730, help="訓練視窗根數（12h；730≈1年）")
    ap.add_argument("--test", type=int, default=182, help="測試視窗根數（182≈3月）")
    ap.add_argument("--epochs", type=int, default=Config.epochs)
    ap.add_argument("--lr", type=float, default=Config.lr)
    ap.add_argument("--no-lags", action="store_true")
    ap.add_argument("--no-tick", action="store_true")
    ap.add_argument("--quiet", action="store_true", default=True)
    args = ap.parse_args()

    cfg = Config(symbol=args.symbol, horizon_hours=args.horizon_hours,
                 n_lags=args.n_lags, epochs=args.epochs, lr=args.lr)
    use_lags = not args.no_lags
    use_tick = not args.no_tick
    label = ("落後+Tick" if (use_lags and use_tick) else
             "只Tick" if use_tick else "只落後報酬")

    folds, names = walk_forward(cfg, use_lags, use_tick, args.train, args.test)
    summary = report(folds, names, label)

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    parts = ("lag" if use_lags else "") + ("tick" if use_tick else "")
    out = ARTIFACT_DIR / f"{cfg.symbol}_{cfg.horizon_hours}h_walkforward_{parts}.json"
    with open(out, "w") as f:
        json.dump({"config": asdict(cfg), "use_lags": use_lags, "use_tick": use_tick,
                   "train_size": args.train, "test_size": args.test,
                   "feature_names": names, "summary": summary}, f, indent=2)
    print(f"已保存：{out}")


if __name__ == "__main__":
    main()
