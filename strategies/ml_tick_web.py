"""
ML Tick 線性策略 — web UI 用 adapter（自洽、無參數、可註冊）。

把 ml/ 訓練好的「純 Tick 線性模型」包成標準 StrategyBase：
on_history(klines, tick_map) 內部自行完成
  1. 把傳入 klines 重採樣成 12h K 棒
  2. 把 tick_map 聚合成 6 個 tick 微結構特徵（每 12h 一列；重用 ml.tick_features）
  3. 載入單一預訓練模型 + scaler + 門檻（ml/artifacts/*_tickweb.json，純 numpy 推論）
  4. 標準化 → 預測下一根 12h 對數報酬 → hold 門檻 → 產生進出場訊號
     （訊號在 K 棒收盤產生，用「下一根 12h 開盤價」成交，消 look-ahead）

⚠️ 使用前提（誠實性）：
  - 需開 tick 模式（此策略只吃 tick 特徵；無 tick_map → 不產生訊號）
  - 模型為固定單一模型，訓練截止見 artifact 的 train_end_ms；
    web 回測區間「須晚於該日」才是真 OOS，否則為 in-sample 假績效
  - 模型在 12h 週期訓練；UI interval 建議 ≤ 12h（如 15m / 1h），策略內部聚到 12h
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import List, Optional

import numpy as np

from core.data_types import Kline
from strategies import register
from strategies.base import StrategyBase, StrategySignal, TickBarMap
from ml.tick_features import MS_PER_HOUR, _aggregate_month

_ARTIFACT = Path(__file__).resolve().parent.parent / "ml" / "artifacts" / "BTCUSDT_12h_tickweb.json"


@register
class MLTickStrategyWeb(StrategyBase):
    name = "ML Tick Linear 12h (OOS>2025-01)"

    def __init__(self) -> None:
        self._m: Optional[dict] = None  # 延遲載入 artifact

    # ── 載入模型 artifact ──────────────────────────────────────────────────────
    def _model(self) -> Optional[dict]:
        if self._m is None and _ARTIFACT.exists():
            d = json.load(open(_ARTIFACT))
            d["_w"] = np.asarray(d["weights"], dtype=np.float64)
            d["_mu"] = np.asarray(d["scaler_mean"], dtype=np.float64)
            d["_sd"] = np.asarray(d["scaler_std"], dtype=np.float64)
            d["_bucket_ms"] = int(d["horizon_hours"]) * MS_PER_HOUR
            self._m = d
        return self._m

    # ── 主入口 ────────────────────────────────────────────────────────────────
    def on_history(self, klines: List[Kline],
                   tick_map: Optional[TickBarMap] = None) -> List[StrategySignal]:
        m = self._model()
        if m is None or not klines or not tick_map:
            return []                       # 無模型或無 tick → 不產生訊號
        bucket_ms = m["_bucket_ms"]

        # 1. 重採樣 klines → 12h（open / close / bar_time）
        ot = np.fromiter((k.open_time for k in klines), dtype=np.int64, count=len(klines))
        opens = np.fromiter((k.open for k in klines), dtype=np.float64, count=len(klines))
        closes = np.fromiter((k.close for k in klines), dtype=np.float64, count=len(klines))
        order = np.argsort(ot); ot, opens, closes = ot[order], opens[order], closes[order]
        bids = ot // bucket_ms
        uniq, first = np.unique(bids, return_index=True)
        last = np.append(first[1:] - 1, len(ot) - 1)
        bar_time_k = (uniq * bucket_ms).astype(np.int64)
        open_map = {int(bar_time_k[i]): float(opens[first[i]]) for i in range(len(uniq))}
        close_map = {int(bar_time_k[i]): float(closes[last[i]]) for i in range(len(uniq))}

        # 2. tick_map → 12h 微結構特徵
        #    逐 12h 桶聚合，一次只實體化一個桶的 ticks。
        #    （tick_map 的值是 build_bar_map_streaming 的 mmap 視圖；
        #     若一次 vstack 全部會把整段 ticks 拉進 RAM → 長區間 OOM。）
        bucket_arrs: dict[int, list] = defaultdict(list)
        for ot, arr in tick_map.items():
            if arr is None or len(arr) == 0:
                continue
            bstart = (int(ot) // bucket_ms) * bucket_ms
            bucket_arrs[bstart].append(arr)
        if not bucket_arrs:
            return []
        feat_map: dict[int, np.ndarray] = {}
        for bstart, arrs in bucket_arrs.items():
            bt = np.vstack(arrs)               # 僅一個 12h 桶（~1-2M 筆）
            bt = bt[np.argsort(bt[:, 0])]
            _, f = _aggregate_month(bt, bucket_ms)
            if len(f):
                feat_map[bstart] = f[0]
        if not feat_map:
            return []

        # 3. 對齊（同時有 kline 與 tick 特徵的 12h 桶）+ 預測
        common = sorted(set(map(int, bar_time_k)) & set(feat_map.keys()))
        if not common:
            return []
        X = np.array([feat_map[t] for t in common], dtype=np.float64)
        Xs = (X - m["_mu"]) / m["_sd"]
        pred = Xs @ m["_w"] + float(m["bias"])

        # 4. hold 門檻 → 目標倉位（低信心續抱前一倉位）
        cutoff = float(m["threshold_cutoff"])
        pos = np.zeros(len(pred)); last_p = 0.0
        for i in range(len(pred)):
            if abs(pred[i]) >= cutoff:
                last_p = np.sign(pred[i])
            pos[i] = last_p

        # 5. 目標倉位 → StrategySignal（下一根 12h 開盤成交）
        sigs: List[StrategySignal] = []
        cur = 0
        for i, t in enumerate(common):
            nb = t + bucket_ms                      # 下一根 12h 開盤時間
            if nb not in open_map:
                continue                            # 無下一根可成交 → 略過
            target = int(pos[i])
            if target == cur:
                continue
            price = close_map[t]
            fp, ft = open_map[nb], nb
            if target == 1:
                sigs.append(StrategySignal(t, price, "long_entry", label="ML long",
                                           fill_price=fp, fill_time=ft))
            elif target == -1:
                sigs.append(StrategySignal(t, price, "short_entry", label="ML short",
                                           fill_price=fp, fill_time=ft))
            else:
                st = "long_exit" if cur == 1 else "short_exit"
                sigs.append(StrategySignal(t, price, st, label="ML flat",
                                           fill_price=fp, fill_time=ft))
            cur = target

        # 收尾：最後一根平倉，使 PnL 全數實現
        if cur != 0 and common:
            tlast = common[-1]
            st = "long_exit" if cur == 1 else "short_exit"
            sigs.append(StrategySignal(tlast, close_map[tlast], st, label="ML close",
                                       fill_price=close_map[tlast], fill_time=tlast))
        return sigs
