"""
線性回歸 — 以對數報酬落後期預測未來對數報酬（第一個 ML 基準模型）。

設計依據（與使用者確認後鎖定）：
  - 特徵 X：純對數報酬落後期 r_{t-1} … r_{t-N}（kline-only，N=12）
  - 目標 Y：下一根 K 棒對數報酬 r_{t+1}（= 未來一個週期報酬）
  - 週期：12h K 棒（由 15m 重採樣，對齊 UTC 00:00 / 12:00）
  - 模型：PyTorch nn.Linear（梯度下降），刻意保持極簡、可解釋
  - 評估：R² / 方向命中率 / IC（Pearson+Spearman）/ MSE
          ＋ 依預測符號做多空、扣 taker 手續費的淨 PnL

嚴格遵守的防洩漏紀律：
  - 時序切分（前 75% 訓練 / 後 25% 測試），絕不 shuffle
  - StandardScaler 只 fit 在訓練集，再套用到測試集
  - 目標不標準化（保留原始對數報酬，方便還原 PnL）
  - 12h 重採樣後目標不重疊，無重疊窗口洩漏

用法：
    source .venv/bin/activate
    python -m ml.train_linear_logret               # 預設 12h / N=12
    python -m ml.train_linear_logret --horizon-hours 8 --n-lags 12
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import torch
from torch import nn

from core import kline_cache

# 與 backtest/engine.py FEE_RATES["Taker"] 一致（單邊 0.05%）
TAKER_FEE = 0.0005
MS_PER_HOUR = 3_600_000
ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"


# ──────────────────────────────────────────────────────────────────────────────
# 設定
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class Config:
    symbol: str = "BTCUSDT"
    source_interval: str = "15m"   # 重採樣來源（回溯到 2019）
    horizon_hours: int = 12        # 目標週期 = 重採樣後的 K 棒長度
    n_lags: int = 12               # 落後期特徵數
    train_frac: float = 0.75       # 時序切分：前 75% 訓練
    epochs: int = 5000
    lr: float = 0.01
    fee: float = TAKER_FEE
    seed: int = 42


# ──────────────────────────────────────────────────────────────────────────────
# ① 載入 + 重採樣成 horizon K 棒
# ──────────────────────────────────────────────────────────────────────────────
def load_resampled_closes(cfg: Config) -> tuple[np.ndarray, np.ndarray]:
    """載入來源 K 棒並重採樣成 horizon K 棒，回傳 (bar_open_time_ms, close)。"""
    path = kline_cache.cache_path(cfg.symbol, cfg.source_interval)
    if not path.exists():
        raise FileNotFoundError(f"找不到 kline 快取：{path}")
    arr = np.load(path)  # 欄位見 data/DATA_LAYOUT.md：0 open_time,1 open,2 high,3 low,4 close,5 volume
    arr = arr[np.argsort(arr[:, 0])]  # 確保時間遞增

    bucket_ms = cfg.horizon_hours * MS_PER_HOUR
    bucket_ids = (arr[:, 0] // bucket_ms).astype(np.int64)

    # 以每個 bucket 的「最後一筆 close」作為該 horizon K 棒的收盤
    uniq, first_idx = np.unique(bucket_ids, return_index=True)
    last_idx = np.append(first_idx[1:] - 1, len(arr) - 1)
    bar_time = uniq * bucket_ms
    close = arr[last_idx, 4].astype(np.float64)
    return bar_time, close


# ──────────────────────────────────────────────────────────────────────────────
# ② ③ ④ ⑤ 對數報酬 → 落後期特徵 + 目標 → drop NaN
# ──────────────────────────────────────────────────────────────────────────────
def build_dataset(close: np.ndarray, n_lags: int):
    """回傳 X[n, n_lags], y[n], 以及對齊的 target 報酬時間索引。"""
    log_ret = np.diff(np.log(close))  # r_t，長度 = len(close)-1，對齊 close[1:]
    n = len(log_ret)

    # 特徵：r_{t-1} … r_{t-n_lags}；目標：r_{t+1}
    # 對某個「決策時點 t」，X 用過去 n_lags 期報酬，y 用下一期報酬
    feats = []
    for lag in range(1, n_lags + 1):
        feats.append(np.concatenate([np.full(lag, np.nan), log_ret[:-lag]]))
    X = np.column_stack(feats)          # X[t] = [r_{t-1}, …, r_{t-n_lags}]
    y = np.concatenate([log_ret[1:], [np.nan]])  # y[t] = r_{t+1}

    # ⑤ Drop NaN（暖機落後期 + 末列無未來值）
    valid = ~np.isnan(X).any(axis=1) & ~np.isnan(y)
    return X[valid], y[valid], valid


# ──────────────────────────────────────────────────────────────────────────────
# ⑥ 時序切分（不 shuffle）+ ⑦ StandardScaler（只 fit 訓練集）
# ──────────────────────────────────────────────────────────────────────────────
def temporal_split_and_scale(X, y, train_frac):
    cut = int(len(X) * train_frac)
    X_tr, X_te = X[:cut], X[cut:]
    y_tr, y_te = y[:cut], y[cut:]

    mu = X_tr.mean(axis=0)
    sd = X_tr.std(axis=0)
    sd[sd == 0] = 1.0
    X_tr_s = (X_tr - mu) / sd
    X_te_s = (X_te - mu) / sd  # 用「訓練集」統計量轉換測試集，避免洩漏
    return (X_tr_s, y_tr), (X_te_s, y_te), (mu, sd), cut


# ──────────────────────────────────────────────────────────────────────────────
# ⑧ ⑨ 訓練線性回歸
# ──────────────────────────────────────────────────────────────────────────────
def train_linear(X_tr, y_tr, cfg: Config):
    torch.manual_seed(cfg.seed)
    Xt = torch.tensor(X_tr, dtype=torch.float32)
    yt = torch.tensor(y_tr, dtype=torch.float32).unsqueeze(1)  # (N,1) 欄向量

    model = nn.Linear(Xt.shape[1], 1)
    opt = torch.optim.Adam(model.parameters(), lr=cfg.lr)
    loss_fn = nn.MSELoss()

    for ep in range(1, cfg.epochs + 1):
        opt.zero_grad()
        loss = loss_fn(model(Xt), yt)
        loss.backward()
        opt.step()
        if ep % 1000 == 0 or ep == 1:
            print(f"  epoch {ep:5d}  train MSE = {loss.item():.3e}")
    return model


# ──────────────────────────────────────────────────────────────────────────────
# ⑩ 評估：統計 + 含手續費 PnL
# ──────────────────────────────────────────────────────────────────────────────
def _spearman(a, b):
    ra = np.argsort(np.argsort(a))
    rb = np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


def evaluate(model, X_te, y_te, cfg: Config) -> dict:
    with torch.no_grad():
        pred = model(torch.tensor(X_te, dtype=torch.float32)).squeeze(1).numpy()
    actual = y_te

    mse = float(np.mean((pred - actual) ** 2))
    ss_res = np.sum((actual - pred) ** 2)
    ss_tot = np.sum((actual - actual.mean()) ** 2)
    r2 = float(1 - ss_res / ss_tot) if ss_tot > 0 else float("nan")
    hit = float(np.mean(np.sign(pred) == np.sign(actual)))
    ic_p = float(np.corrcoef(pred, actual)[0, 1])
    ic_s = _spearman(pred, actual)

    # 含手續費多空 PnL：每根 K 棒持有 sign(pred) 倉位，賺取 actual（對數報酬）
    pos = np.sign(pred)
    prev = np.concatenate([[0.0], pos[:-1]])
    turnover = np.abs(pos - prev)                 # 倉位變動量（翻倉=2，即出+進）
    fee_cost = turnover * cfg.fee
    net_ret = pos * actual - fee_cost
    gross_ret = pos * actual

    bars_per_year = (365 * 24) / cfg.horizon_hours

    def _summ(rets):
        std = rets.std()
        return {
            "cum_log_return": float(rets.sum()),
            "equity_multiple": float(np.exp(rets.sum())),
            "sharpe_annual": float(rets.mean() / std * np.sqrt(bars_per_year)) if std > 0 else float("nan"),
        }

    buy_hold = float(actual.sum())  # 對數報酬可直接相加

    return {
        "test_samples": int(len(actual)),
        "mse": mse,
        "r2": r2,
        "directional_hit_rate": hit,
        "ic_pearson": ic_p,
        "ic_spearman": ic_s,
        "pnl_gross": _summ(gross_ret),
        "pnl_net_after_fee": _summ(net_ret),
        "fee_drag_log": float(fee_cost.sum()),
        "num_trades": int((turnover > 0).sum()),
        "buy_hold_cum_log_return": buy_hold,
        "buy_hold_equity_multiple": float(np.exp(buy_hold)),
    }


def print_report(cfg: Config, metrics: dict, n_total: int, cut: int):
    print("\n" + "=" * 62)
    print("  線性回歸 — 對數報酬預測  績效報告（測試集）")
    print("=" * 62)
    print(f"  週期 {cfg.horizon_hours}h | 落後期 N={cfg.n_lags} | "
          f"樣本 {n_total}（訓練 {cut} / 測試 {n_total - cut}）")
    print("-" * 62)
    print("  [預測統計]")
    print(f"    MSE                 = {metrics['mse']:.3e}")
    print(f"    R²                  = {metrics['r2']:+.4f}")
    print(f"    方向命中率           = {metrics['directional_hit_rate']*100:.2f} %")
    print(f"    IC (Pearson)        = {metrics['ic_pearson']:+.4f}")
    print(f"    IC (Spearman)       = {metrics['ic_spearman']:+.4f}")
    print("  [含手續費多空 PnL]")
    g, nt = metrics["pnl_gross"], metrics["pnl_net_after_fee"]
    print(f"    Gross  累積log報酬   = {g['cum_log_return']:+.4f}  (×{g['equity_multiple']:.3f})  Sharpe {g['sharpe_annual']:+.2f}")
    print(f"    Net    累積log報酬   = {nt['cum_log_return']:+.4f}  (×{nt['equity_multiple']:.3f})  Sharpe {nt['sharpe_annual']:+.2f}")
    print(f"    手續費侵蝕(log)      = {metrics['fee_drag_log']:.4f}   交易次數 {metrics['num_trades']}")
    print(f"    對照 Buy&Hold        = {metrics['buy_hold_cum_log_return']:+.4f}  (×{metrics['buy_hold_equity_multiple']:.3f})")
    print("=" * 62)


# ──────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Linear regression on log returns")
    ap.add_argument("--symbol", default=Config.symbol)
    ap.add_argument("--source-interval", default=Config.source_interval)
    ap.add_argument("--horizon-hours", type=int, default=Config.horizon_hours)
    ap.add_argument("--n-lags", type=int, default=Config.n_lags)
    ap.add_argument("--train-frac", type=float, default=Config.train_frac)
    ap.add_argument("--epochs", type=int, default=Config.epochs)
    ap.add_argument("--lr", type=float, default=Config.lr)
    args = ap.parse_args()
    cfg = Config(symbol=args.symbol, source_interval=args.source_interval,
                 horizon_hours=args.horizon_hours, n_lags=args.n_lags,
                 train_frac=args.train_frac, epochs=args.epochs, lr=args.lr)

    print(f"載入 {cfg.symbol} {cfg.source_interval} → 重採樣成 {cfg.horizon_hours}h K 棒 …")
    bar_time, close = load_resampled_closes(cfg)
    print(f"  {cfg.horizon_hours}h K 棒數 = {len(close)}  "
          f"({np.datetime64(int(bar_time[0]), 'ms')} → {np.datetime64(int(bar_time[-1]), 'ms')})")

    X, y, _ = build_dataset(close, cfg.n_lags)
    (X_tr, y_tr), (X_te, y_te), (mu, sd), cut = temporal_split_and_scale(X, y, cfg.train_frac)
    print(f"有效樣本 {len(X)}（drop NaN 後）；訓練 {len(X_tr)} / 測試 {len(X_te)}")

    print("訓練線性回歸 …")
    model = train_linear(X_tr, y_tr, cfg)
    metrics = evaluate(model, X_te, y_te, cfg)
    print_report(cfg, metrics, len(X), cut)

    # 保存產物：權重 + scaler + 規格 + 績效
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    tag = f"{cfg.symbol}_{cfg.horizon_hours}h_lin_lag{cfg.n_lags}"
    torch.save(model.state_dict(), ARTIFACT_DIR / f"{tag}.pt")
    artifact = {
        "config": asdict(cfg),
        "feature_spec": {
            "type": "log_return_lags",
            "lags": list(range(1, cfg.n_lags + 1)),
            "scaler_mean": mu.tolist(),
            "scaler_std": sd.tolist(),
        },
        "metrics": metrics,
    }
    with open(ARTIFACT_DIR / f"{tag}.json", "w") as f:
        json.dump(artifact, f, indent=2)
    print(f"\n已保存：{ARTIFACT_DIR / (tag + '.pt')}\n        {ARTIFACT_DIR / (tag + '.json')}")


if __name__ == "__main__":
    main()
