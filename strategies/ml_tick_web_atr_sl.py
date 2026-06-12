"""
ML Tick 線性策略 — web UI 用 adapter（自洽、無參數、可註冊）— ATR 停損版。

複製自 ml_tick_web.py，唯一差異：

  原版完全沒有風控，倉位只在模型預測「目標倉位改變」時才出場/反手。
  本版在每次進場時，依進場當下 12h ATR 設定停損價：
    long  stop = entry_price - ATR * atr_sl_mult
    short stop = entry_price + ATR * atr_sl_mult
  進場後逐根掃描原始 K 棒的 tick 資料，價格觸碰停損即立即出場
  （tick-level 出場，價格=stop_price，時間=觸碰當下的 tick 時間）。
  觸發停損後倉位視為平倉，等待模型下一次目標倉位變化才會再進場。

⚠️ 使用前提（誠實性）：
  - 需開 tick 模式（此策略只吃 tick 特徵；無 tick_map → 不產生訊號）
  - 模型為固定單一模型，訓練截止見 artifact 的 train_end_ms；
    web 回測區間「須晚於該日」才是真 OOS，否則為 in-sample 假績效
  - 模型在 12h 週期訓練；UI interval 建議 ≤ 12h（如 15m / 1h），策略內部聚到 12h
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path
from typing import List, Optional

import numpy as np

from core.data_types import Kline
from strategies import register
from strategies.base import StrategyBase, StrategySignal, TickBarMap
from ml.tick_features import MS_PER_HOUR, _aggregate_month

_ARTIFACT = Path(__file__).resolve().parent.parent / "ml" / "artifacts" / "BTCUSDT_12h_tickweb.json"


@register
class MLTickStrategyWebAtrSl(StrategyBase):
    name = "ML Tick Linear 12h ATR-SL (OOS>2025-01)"

    # ── ATR 停損參數 ─────────────────────────────────────────────────────────
    atr_period: int = 14      # ATR SMA 週期（單位：12h K 棒）
    atr_sl_mult: float = 1.5  # SL = entry ± ATR * atr_sl_mult

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

        # 1. 重採樣 klines → 12h（open / high / low / close / bar_time）
        ot = np.fromiter((k.open_time for k in klines), dtype=np.int64, count=len(klines))
        opens = np.fromiter((k.open for k in klines), dtype=np.float64, count=len(klines))
        highs = np.fromiter((k.high for k in klines), dtype=np.float64, count=len(klines))
        lows = np.fromiter((k.low for k in klines), dtype=np.float64, count=len(klines))
        closes = np.fromiter((k.close for k in klines), dtype=np.float64, count=len(klines))
        order = np.argsort(ot)
        ot, opens, highs, lows, closes = ot[order], opens[order], highs[order], lows[order], closes[order]
        bids = ot // bucket_ms
        uniq, first = np.unique(bids, return_index=True)
        last = np.append(first[1:] - 1, len(ot) - 1)
        bar_time_k = (uniq * bucket_ms).astype(np.int64)
        open_map = {int(bar_time_k[i]): float(opens[first[i]]) for i in range(len(uniq))}
        close_map = {int(bar_time_k[i]): float(closes[last[i]]) for i in range(len(uniq))}
        high_map = {int(bar_time_k[i]): float(np.max(highs[first[i]:last[i] + 1])) for i in range(len(uniq))}
        low_map = {int(bar_time_k[i]): float(np.min(lows[first[i]:last[i] + 1])) for i in range(len(uniq))}

        # 2. tick_map → 12h 微結構特徵
        #    逐 12h 桶聚合，一次只實體化一個桶的 ticks。
        #    （tick_map 的值是 build_bar_map_streaming 的 mmap 視圖；
        #     若一次 vstack 全部會把整段 ticks 拉進 RAM → 長區間 OOM。）
        bucket_arrs: dict[int, list] = defaultdict(list)
        for k_ot, arr in tick_map.items():
            if arr is None or len(arr) == 0:
                continue
            bstart = (int(k_ot) // bucket_ms) * bucket_ms
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

        # 4b. 12h ATR（SMA over True Range，warm-up 期用累積平均）
        atr_map: dict[int, float] = {}
        trs: "deque[float]" = deque(maxlen=max(1, self.atr_period))
        prev_close: Optional[float] = None
        for t in sorted(bar_time_k.tolist()):
            h, l, c = high_map[t], low_map[t], close_map[t]
            tr = (h - l) if prev_close is None else max(h - l, abs(h - prev_close), abs(l - prev_close))
            trs.append(tr)
            atr_map[t] = float(np.mean(trs))
            prev_close = c

        # 5. 目標倉位 → StrategySignal（下一根 12h 開盤成交）+ ATR 停損監控
        sigs: List[StrategySignal] = []
        cur = 0
        entry_price = 0.0
        stop_price: Optional[float] = None
        scan_idx = 0   # 指向 ot（原始 K 棒 open_time，已排序）的掃描位置

        for i, t in enumerate(common):
            nb = t + bucket_ms                      # 下一根 12h 開盤時間
            if nb not in open_map:
                continue                            # 無下一根可成交 → 略過

            # 5a. 掃描 [scan_idx, nb) 區間內的原始 K 棒 ticks，偵測停損觸碰
            while scan_idx < len(ot) and int(ot[scan_idx]) < nb:
                ot_k = int(ot[scan_idx])
                if cur != 0 and stop_price is not None:
                    tick_arr = tick_map.get(ot_k)
                    if tick_arr is not None and len(tick_arr) > 0:
                        for row in tick_arr:
                            price = float(row[1])
                            if cur == 1 and price <= stop_price:
                                sigs.append(StrategySignal(
                                    ot_k, stop_price, "long_exit", label="ML SL",
                                    fill_price=stop_price, fill_time=int(row[0]),
                                    meta={"exit_reason": "atr_stop_loss"}))
                                cur = 0; entry_price = 0.0; stop_price = None
                                break
                            if cur == -1 and price >= stop_price:
                                sigs.append(StrategySignal(
                                    ot_k, stop_price, "short_exit", label="ML SL",
                                    fill_price=stop_price, fill_time=int(row[0]),
                                    meta={"exit_reason": "atr_stop_loss"}))
                                cur = 0; entry_price = 0.0; stop_price = None
                                break
                scan_idx += 1

            target = int(pos[i])
            if target == cur:
                continue
            price = close_map[t]
            fp, ft = open_map[nb], nb
            if target == 1:
                entry_price = fp
                stop_price = fp - atr_map[t] * self.atr_sl_mult
                sigs.append(StrategySignal(t, price, "long_entry", label="ML long",
                                           fill_price=fp, fill_time=ft,
                                           meta={"atr": atr_map[t], "stop": stop_price}))
            elif target == -1:
                entry_price = fp
                stop_price = fp + atr_map[t] * self.atr_sl_mult
                sigs.append(StrategySignal(t, price, "short_entry", label="ML short",
                                           fill_price=fp, fill_time=ft,
                                           meta={"atr": atr_map[t], "stop": stop_price}))
            else:
                st = "long_exit" if cur == 1 else "short_exit"
                sigs.append(StrategySignal(t, price, st, label="ML flat",
                                           fill_price=fp, fill_time=ft))
                entry_price = 0.0; stop_price = None
            cur = target

        # 5b. 最後一個 12h 桶之後也可能觸發停損，繼續掃描剩餘 ticks
        while cur != 0 and stop_price is not None and scan_idx < len(ot):
            ot_k = int(ot[scan_idx])
            tick_arr = tick_map.get(ot_k)
            if tick_arr is not None and len(tick_arr) > 0:
                for row in tick_arr:
                    price = float(row[1])
                    if cur == 1 and price <= stop_price:
                        sigs.append(StrategySignal(
                            ot_k, stop_price, "long_exit", label="ML SL",
                            fill_price=stop_price, fill_time=int(row[0]),
                            meta={"exit_reason": "atr_stop_loss"}))
                        cur = 0; entry_price = 0.0; stop_price = None
                        break
                    if cur == -1 and price >= stop_price:
                        sigs.append(StrategySignal(
                            ot_k, stop_price, "short_exit", label="ML SL",
                            fill_price=stop_price, fill_time=int(row[0]),
                            meta={"exit_reason": "atr_stop_loss"}))
                        cur = 0; entry_price = 0.0; stop_price = None
                        break
            scan_idx += 1

        # 收尾：最後一根平倉，使 PnL 全數實現
        if cur != 0 and common:
            tlast = common[-1]
            st = "long_exit" if cur == 1 else "short_exit"
            sigs.append(StrategySignal(tlast, close_map[tlast], st, label="ML close",
                                       fill_price=close_map[tlast], fill_time=tlast))
        return sigs
