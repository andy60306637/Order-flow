"""
匯出「單一、自洽」的 web 推論用模型 artifact。

web UI 是 strategy_cls() 無參數實例化 + on_history(klines, tick_map)，
無法逐折重訓，因此需要一個固定模型 + scaler + 門檻，存成純數值 JSON
（推論時用 numpy 即可，不需 torch / 不需訓練資料）。

模型 = 純 Tick 線性回歸，訓練於 tick 覆蓋期前 75%（其餘留作 OOS）。
門檻 cutoff = 訓練段 |pred| 的 0.7 分位（hold 模式操作點，穩健區間）。
另存 train_end_ms：web 回測區間須在此之後才是真 OOS（防洩漏）。

用法：
    source .venv/bin/activate
    python -m ml.export_web_model
"""
from __future__ import annotations

import json

import numpy as np
import torch

from ml.train_linear_logret import ARTIFACT_DIR, Config, train_linear
from ml.walkforward import build_dataset_with_time

THRESHOLD_Q = 0.7
TRAIN_FRAC = 0.75


def main():
    cfg = Config()
    X, y, bar_time, names = build_dataset_with_time(cfg, use_lags=False, use_tick=True)
    cut = int(len(X) * TRAIN_FRAC)

    mu = X[:cut].mean(0); sd = X[:cut].std(0); sd[sd == 0] = 1.0
    Xtr = (X[:cut] - mu) / sd
    torch.manual_seed(cfg.seed)
    model = train_linear(Xtr, y[:cut], cfg)

    w = model.weight.detach().numpy().ravel()
    b = float(model.bias.detach())
    with torch.no_grad():
        ptr = (Xtr @ w + b)  # 訓練段預測
    cutoff = float(np.quantile(np.abs(ptr), THRESHOLD_Q))

    artifact = {
        "model": "linear_tick_only",
        "horizon_hours": cfg.horizon_hours,
        "feature_names": names,
        "scaler_mean": mu.tolist(),
        "scaler_std": sd.tolist(),
        "weights": w.tolist(),
        "bias": b,
        "threshold_cutoff": cutoff,
        "threshold_q": THRESHOLD_Q,
        "train_end_ms": int(bar_time[cut]),       # web 回測須晚於此（OOS）
        "train_rows": cut,
        "train_range_ms": [int(bar_time[0]), int(bar_time[cut - 1])],
    }
    out = ARTIFACT_DIR / f"{cfg.symbol}_{cfg.horizon_hours}h_tickweb.json"
    with open(out, "w") as f:
        json.dump(artifact, f, indent=2)

    print(f"已匯出 web 模型：{out}")
    print(f"  特徵 {names}")
    print(f"  cutoff(q={THRESHOLD_Q}) = {cutoff:.6f}")
    print(f"  訓練段 {np.datetime64(int(bar_time[0]),'ms').astype('datetime64[D]')} "
          f"→ {np.datetime64(int(bar_time[cut-1]),'ms').astype('datetime64[D]')} ({cut} 根)")
    print(f"  ⚠️ web 回測起點須晚於 {np.datetime64(int(bar_time[cut]),'ms').astype('datetime64[D]')} 才是真 OOS")


if __name__ == "__main__":
    main()
