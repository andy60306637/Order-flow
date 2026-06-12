"""
線性回歸（第二輪）— 落後對數報酬 + Tick 微結構特徵，預測未來對數報酬。

與第一輪基準（ml/train_linear_logret.py）的唯一差別：
  特徵 = N 個落後報酬  +  當前 K 棒的 tick 微結構特徵（ml/tick_features.py 的 6 個比值）
模型、週期、切分、評估全部沿用，以便乾淨歸因「加 tick/CVD 特徵是否有用」。

無洩漏：tick 特徵描述「已收盤的當前 K 棒」（決策時點已知），用來預測「下一根」報酬。

用法：
    source .venv/bin/activate
    python -m ml.train_linear_tickcvd                 # 12h / 6 落後 + 6 tick 特徵
    python -m ml.train_linear_tickcvd --n-lags 12
    python -m ml.train_linear_tickcvd --no-lags       # 只用 tick 特徵
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import torch

from ml import tick_features
from ml.train_linear_logret import (
    ARTIFACT_DIR, Config, evaluate, load_resampled_closes,
    print_report, temporal_split_and_scale, train_linear,
)


def _shift(arr: np.ndarray, k: int) -> np.ndarray:
    """正 k：取 arr[j-k]（前面補 NaN）；負 k：取 arr[j-k]（後面補 NaN）。"""
    out = np.full_like(arr, np.nan, dtype=np.float64)
    if k > 0:
        out[k:] = arr[:-k]
    elif k < 0:
        out[:k] = arr[-k:]
    else:
        out[:] = arr
    return out


def build_combined_dataset(cfg: Config, use_lags: bool, use_tick: bool):
    """以 bar_time 對齊 kline 報酬與 tick 特徵，回傳 (X, y, feat_names)。"""
    # kline 12h 收盤
    bar_time_k, close_k = load_resampled_closes(cfg)
    # tick 特徵（逐月聚合 + 快取）
    bar_time_t, feat_t = tick_features.build_or_load(cfg.symbol, cfg.horizon_hours)

    # 以 bar_time 取交集對齊（同一套 floor(time/bucket) 網格）
    common, idx_k, idx_t = np.intersect1d(bar_time_k, bar_time_t, return_indices=True)
    close = close_k[idx_k]
    feat = feat_t[idx_t]
    M = len(common)

    # 每根 K 棒「期間內」的對數報酬：ret_of_bucket[j] = ln(close[j]/close[j-1])
    ret = np.full(M, np.nan)
    ret[1:] = np.diff(np.log(close))

    cols, names = [], []
    if use_lags:
        for i in range(cfg.n_lags):                  # lag0 = 當前剛收盤的報酬（決策時已知）
            cols.append(_shift(ret, i))
            names.append(f"ret_lag{i}")
    if use_tick:
        for j, nm in enumerate(tick_features.FEATURE_NAMES):
            cols.append(feat[:, j])
            names.append(nm)

    X = np.column_stack(cols)
    y = _shift(ret, -1)                              # 目標 = 下一根報酬

    valid = ~np.isnan(X).any(axis=1) & ~np.isnan(y)
    return X[valid], y[valid], names


def _baseline_line() -> str:
    """印出第一輪純落後報酬基準的對照（若存在）。"""
    p = ARTIFACT_DIR / "BTCUSDT_12h_lin_lag12.json"
    if not p.exists():
        return ""
    m = json.load(open(p))["metrics"]
    nt = m["pnl_net_after_fee"]
    return (f"  [對照·第一輪純落後報酬] hit={m['directional_hit_rate']*100:.2f}% "
            f"IC={m['ic_pearson']:+.4f}  Net ×{nt['equity_multiple']:.3f} "
            f"Sharpe {nt['sharpe_annual']:+.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=Config.symbol)
    ap.add_argument("--source-interval", default=Config.source_interval)
    ap.add_argument("--horizon-hours", type=int, default=Config.horizon_hours)
    ap.add_argument("--n-lags", type=int, default=6)
    ap.add_argument("--train-frac", type=float, default=Config.train_frac)
    ap.add_argument("--epochs", type=int, default=Config.epochs)
    ap.add_argument("--lr", type=float, default=Config.lr)
    ap.add_argument("--no-lags", action="store_true", help="只用 tick 特徵")
    ap.add_argument("--no-tick", action="store_true", help="只用落後報酬（=第一輪）")
    args = ap.parse_args()

    cfg = Config(symbol=args.symbol, source_interval=args.source_interval,
                 horizon_hours=args.horizon_hours, n_lags=args.n_lags,
                 train_frac=args.train_frac, epochs=args.epochs, lr=args.lr)
    use_lags = not args.no_lags
    use_tick = not args.no_tick

    print(f"建立資料集（{cfg.horizon_hours}h；lags={use_lags}(N={cfg.n_lags}) tick={use_tick}）…")
    X, y, names = build_combined_dataset(cfg, use_lags, use_tick)
    (X_tr, y_tr), (X_te, y_te), (mu, sd), cut = temporal_split_and_scale(X, y, cfg.train_frac)
    print(f"有效樣本 {len(X)}（tick 覆蓋 2021-04+）；訓練 {len(X_tr)} / 測試 {len(X_te)}")
    print(f"特徵 ({len(names)}): {names}")

    print("訓練線性回歸 …")
    model = train_linear(X_tr, y_tr, cfg)
    metrics = evaluate(model, X_te, y_te, cfg)
    print_report(cfg, metrics, len(X), cut)
    bl = _baseline_line()
    if bl:
        print(bl)

    # 特徵權重（標準化後 → 可直接比較重要性）
    w = model.weight.detach().numpy().ravel()
    print("\n  [標準化後權重 / 特徵重要性]")
    for nm, wi in sorted(zip(names, w), key=lambda x: -abs(x[1])):
        print(f"    {nm:16s} {wi:+.4f}")

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    parts = ("lag" if use_lags else "") + ("tick" if use_tick else "")
    tag = f"{cfg.symbol}_{cfg.horizon_hours}h_lin_{parts}_n{cfg.n_lags}"
    torch.save(model.state_dict(), ARTIFACT_DIR / f"{tag}.pt")
    artifact = {
        "config": asdict(cfg), "use_lags": use_lags, "use_tick": use_tick,
        "feature_names": names,
        "scaler_mean": mu.tolist(), "scaler_std": sd.tolist(),
        "weights": w.tolist(), "bias": float(model.bias.detach()),
        "metrics": metrics,
    }
    with open(ARTIFACT_DIR / f"{tag}.json", "w") as f:
        json.dump(artifact, f, indent=2)
    print(f"\n已保存：{ARTIFACT_DIR / (tag + '.pt')}\n        {ARTIFACT_DIR / (tag + '.json')}")


if __name__ == "__main__":
    main()
