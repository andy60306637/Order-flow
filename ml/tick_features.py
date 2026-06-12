"""
逐筆 (tick) 微結構特徵聚合 — 以 12h（或指定）K 棒為單位。

目的：提供 kline 給不了的 order-flow 微結構特徵。
      （CVD / delta / imbalance 本身可由 kline 的 taker_buy_volume 推導，
        所以這裡只放「真正需要逐筆資料」且設計成平穩比值的特徵。）

特徵（每根 K 棒一列，皆為平穩比值，避免成交量逐年成長造成非平穩）：
  0 vol_imbalance    成交量不平衡 = delta / total_vol            ∈[-1,1]
  1 count_imbalance  買賣「筆數」不平衡 = (buy_cnt-sell_cnt)/cnt  ∈[-1,1]
  2 large_imbalance  大單(≥1 BTC)淨量比 = large_delta/large_total ∈[-1,1]
  3 cvd_close_pos    棒內 CVD 收尾位置（在 [min,max] 的相對位置）∈[-1,1]
  4 cvd_reversal     棒內 CVD 自高點回落幅度 = (max-end)/(max-min) ∈[0,1]
  5 price_vs_vwap    收盤對 tick VWAP 乖離 = (close-vwap)/vwap

Tick 欄位（見 data/DATA_LAYOUT.md）：0 time_ms, 1 price, 2 qty, 3 is_buyer_maker
  is_buyer_maker=True → 賣方主動 (sell aggressor)；False → 買方主動 (buy aggressor)

一次性逐月聚合並快取到 ml/artifacts/{symbol}_{horizon}h_tickfeat.npz。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np

from core import tick_cache

MS_PER_HOUR = 3_600_000
LARGE_TRADE_BTC = 1.0  # 大單門檻（BTC）
EPS = 1e-12
ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"
FEATURE_NAMES = [
    "vol_imbalance", "count_imbalance", "large_imbalance",
    "cvd_close_pos", "cvd_reversal", "price_vs_vwap",
]


def _month_starts(start: dt.datetime, end: dt.datetime):
    """產生 [start, end] 之間每個月的 (month_start, month_end) UTC ms 區間。"""
    y, m = start.year, start.month
    out = []
    while (y, m) <= (end.year, end.month):
        ms0 = int(dt.datetime(y, m, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        ms1 = int(dt.datetime(ny, nm, 1, tzinfo=dt.timezone.utc).timestamp() * 1000) - 1
        out.append((f"{y}-{m:02d}", ms0, ms1))
        y, m = ny, nm
    return out


def _aggregate_month(ticks: np.ndarray, bucket_ms: int):
    """對單月 ticks（已按時間排序）以 bucket_ms 分桶，回傳 (bar_time, feat[M,6])。

    12h 桶不跨月邊界（00:00 對齊），故逐月聚合即完整。全程向量化（reduceat）。
    """
    if len(ticks) == 0:
        return np.empty(0, np.int64), np.empty((0, len(FEATURE_NAMES)), np.float64)

    t = ticks[:, 0]
    price = ticks[:, 1]
    qty = ticks[:, 2]
    is_bm = ticks[:, 3].astype(bool)          # True = sell aggressor
    sign = np.where(is_bm, -1.0, 1.0)
    signed_qty = sign * qty

    bucket_ids = (t // bucket_ms).astype(np.int64)
    uniq, first_idx = np.unique(bucket_ids, return_index=True)
    last_idx = np.append(first_idx[1:] - 1, len(ticks) - 1)

    # 總量 / delta / 筆數
    total_vol = np.add.reduceat(qty, first_idx)
    delta = np.add.reduceat(signed_qty, first_idx)
    buy_cnt = np.add.reduceat((~is_bm).astype(np.float64), first_idx)
    sell_cnt = np.add.reduceat(is_bm.astype(np.float64), first_idx)
    cnt = buy_cnt + sell_cnt

    # 大單
    large = qty >= LARGE_TRADE_BTC
    large_signed = np.where(large, signed_qty, 0.0)
    large_abs = np.where(large, qty, 0.0)
    large_delta = np.add.reduceat(large_signed, first_idx)
    large_total = np.add.reduceat(large_abs, first_idx)

    # VWAP / close
    vwap = np.add.reduceat(price * qty, first_idx) / (total_vol + EPS)
    close = price[last_idx]

    # 棒內 CVD 軌跡（intrabar = 全域 cumsum 扣除該桶起點基線）
    gcum = np.cumsum(signed_qty)
    baseline = gcum[first_idx] - signed_qty[first_idx]          # 該桶第一筆「之前」的 cum
    sizes = np.diff(np.append(first_idx, len(ticks)))
    baseline_exp = np.repeat(baseline, sizes)
    intrabar = gcum - baseline_exp
    cvd_max = np.maximum.reduceat(intrabar, first_idx)
    cvd_min = np.minimum.reduceat(intrabar, first_idx)
    cvd_end = delta                                            # 桶內最後一筆 = 總 delta
    rng = (cvd_max - cvd_min) + EPS

    feat = np.column_stack([
        delta / (total_vol + EPS),                             # vol_imbalance
        (buy_cnt - sell_cnt) / (cnt + EPS),                    # count_imbalance
        large_delta / (large_total + EPS),                     # large_imbalance
        2.0 * (cvd_end - cvd_min) / rng - 1.0,                 # cvd_close_pos ∈[-1,1]
        (cvd_max - cvd_end) / rng,                             # cvd_reversal ∈[0,1]
        (close - vwap) / (vwap + EPS),                         # price_vs_vwap
    ])
    return (uniq * bucket_ms).astype(np.int64), feat


def build_or_load(symbol: str, horizon_hours: int, rebuild: bool = False):
    """回傳 (bar_time[M], feat[M,6])，逐月聚合並快取。"""
    bucket_ms = horizon_hours * MS_PER_HOUR
    cache = ARTIFACT_DIR / f"{symbol}_{horizon_hours}h_tickfeat.npz"
    if cache.exists() and not rebuild:
        d = np.load(cache)
        print(f"  載入快取 tick 特徵：{cache.name}  ({len(d['bar_time'])} 根)")
        return d["bar_time"], d["feat"]

    print(f"  逐月聚合 tick 微結構特徵（{symbol} {horizon_hours}h）…")
    start = dt.datetime(2021, 4, 1, tzinfo=dt.timezone.utc)
    end = dt.datetime.now(dt.timezone.utc)
    bts, fts = [], []
    for label, ms0, ms1 in _month_starts(start, end):
        ticks = tick_cache.load_range(symbol, ms0, ms1)
        if len(ticks) == 0:
            continue
        ticks = ticks[np.argsort(ticks[:, 0])]
        bt, ft = _aggregate_month(ticks, bucket_ms)
        bts.append(bt)
        fts.append(ft)
        print(f"    {label}: {len(ticks):>9,} ticks → {len(bt)} 根 K 棒")
        del ticks
    bar_time = np.concatenate(bts)
    feat = np.vstack(fts)
    order = np.argsort(bar_time)
    bar_time, feat = bar_time[order], feat[order]

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache, bar_time=bar_time, feat=feat)
    print(f"  已快取：{cache}  ({len(bar_time)} 根)")
    return bar_time, feat


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--horizon-hours", type=int, default=12)
    ap.add_argument("--rebuild", action="store_true")
    a = ap.parse_args()
    bt, ft = build_or_load(a.symbol, a.horizon_hours, a.rebuild)
    print(f"\nbar_time {bt.shape}  feat {ft.shape}")
    print("特徵均值 :", dict(zip(FEATURE_NAMES, np.round(np.nanmean(ft, 0), 4))))
    print("特徵標準差:", dict(zip(FEATURE_NAMES, np.round(np.nanstd(ft, 0), 4))))
