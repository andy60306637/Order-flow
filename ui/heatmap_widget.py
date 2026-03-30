"""
Order Book Heatmap。

原理：
  - 每隔 HEATMAP_UPDATE_MS 對 OrderBook 拍一次快照
  - 將快照轉為 numpy 1D 向量（price → qty），對數縮放
  - 滾動加入二維矩陣（time_slots × price_buckets）
  - 用 pyqtgraph ImageItem + viridis colormap 顯示

疊加：
  - aggTrade 落點（▲ buy / ▽ sell）透過 ScatterPlotItem 顯示
"""
from __future__ import annotations
import math
from collections import deque
from typing import List, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui

import config


class HeatmapWidget(pg.PlotWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent, background=config.COLOR_BG)

        pi = self.getPlotItem()
        pi.hideButtons()
        pi.showGrid(x=False, y=True, alpha=0.2)
        pi.setLabel("left", "Price")
        pi.getAxis("bottom").setStyle(showValues=False)

        # 二維矩陣：(time_slots, price_buckets)
        self._T = config.HEATMAP_TIME_SLOTS
        self._P = config.HEATMAP_PRICE_BUCKETS
        self._data = np.zeros((self._T, self._P), dtype=np.float32)

        # pyqtgraph ImageItem
        self._img = pg.ImageItem()
        pi.addItem(self._img)

        # viridis-like colormap
        lut = _make_lut()
        self._img.setLookupTable(lut)
        self._img.setLevels([0, 1])

        # 成交點
        self._trade_buf: deque = deque(maxlen=3000)  # (slot, price, qty, is_buy)
        self._scatter = pg.ScatterPlotItem(size=5, pxMode=True)
        pi.addItem(self._scatter)

        # 狀態
        self._current_price: float = 0.0
        self._price_min: float = 0.0
        self._price_max: float = 0.0
        self._slot_idx: int = 0          # 目前寫入的欄（迴圈覆蓋）

        # 是否累積過一輪（用於正確顯示）
        self._filled_once = False

    # ──────────────────────────────────────────────────────────────────────────
    def add_snapshot(
        self,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
        mid_price: float,
    ) -> None:
        """每次 Heatmap timer 觸發時呼叫。"""
        if mid_price <= 0:
            return
        self._current_price = mid_price
        half_range = mid_price * config.HEATMAP_PRICE_RANGE
        p_min = mid_price - half_range
        p_max = mid_price + half_range
        self._price_min = p_min
        self._price_max = p_max

        bucket_size = (p_max - p_min) / self._P
        if bucket_size <= 0:
            return

        col = np.zeros(self._P, dtype=np.float32)
        for price, qty in bids + asks:
            bi = int((price - p_min) / bucket_size)
            if 0 <= bi < self._P:
                col[bi] += qty

        # log1p 縮放，讓大單與小單都清晰可見
        col = np.log1p(col)

        # 寫入循環矩陣
        self._data[self._slot_idx % self._T] = col
        self._slot_idx += 1
        if self._slot_idx >= self._T:
            self._filled_once = True

        self._redraw()

    def add_trade(
        self, price: float, qty: float, is_buy: bool, slot: int = -1
    ) -> None:
        """記錄一筆成交，疊加在 Heatmap 上。"""
        if slot < 0:
            slot = self._slot_idx
        self._trade_buf.append((slot, price, qty, is_buy))

    def reset(self) -> None:
        self._data[:] = 0
        self._slot_idx = 0
        self._filled_once = False
        self._trade_buf.clear()
        self._scatter.setData([], [])
        self._current_price = 0.0

    # ──────────────────────────────────────────────────────────────────────────
    def _redraw(self) -> None:
        p_min = self._price_min
        p_max = self._price_max
        if p_max <= p_min:
            return

        # 把循環矩陣從「最老→最新」排列（最新在右）
        cur = self._slot_idx % self._T
        ordered = np.roll(self._data, -cur, axis=0)  # shape (T, P)

        # 全域正規化到 [0, 1]
        mx = ordered.max()
        if mx > 0:
            ordered = ordered / mx

        # setImage expects shape (cols, rows) = (T, P)
        self._img.setImage(ordered, autoLevels=False)

        # 設定 ImageItem 的座標：x=0..T, y=p_min..p_max
        tr = QtGui.QTransform()
        tr.translate(0, p_min)
        tr.scale(1.0, (p_max - p_min) / self._P)
        self._img.setTransform(tr)

        # 同步 y 軸
        self.getPlotItem().setYRange(p_min, p_max, padding=0)
        self.getPlotItem().setXRange(0, self._T, padding=0)

        # 更新成交點
        self._redraw_trades(p_min, p_max)

    def _redraw_trades(self, p_min: float, p_max: float) -> None:
        if not self._trade_buf:
            return
        cur = self._slot_idx

        spots = []
        for (slot, price, qty, is_buy) in self._trade_buf:
            if price < p_min or price > p_max:
                continue
            # x: 相對位置（在 T 個欄中的位置）
            age = cur - slot
            if age < 0 or age >= self._T:
                continue
            x = float(self._T - 1 - age % self._T)
            y = float(price)
            size = max(4, min(14, int(math.log1p(qty) * 2)))
            color = (
                pg.mkBrush(38, 166, 154, 200)
                if is_buy
                else pg.mkBrush(239, 83, 80, 200)
            )
            sym = "t1" if is_buy else "t"   # ▲ / ▽
            spots.append(
                dict(pos=(x, y), size=size, brush=color, symbol=sym, pen=None)
            )
        if spots:
            self._scatter.setData(spots)


# ── colormap（深色背景友善的 viridis 近似）─────────────────────────────────────
def _make_lut() -> np.ndarray:
    """產生 256 × 3 的 RGB LUT（viridis 色系）。"""
    colors = [
        [20,  20,  30],    # 0   深黑
        [30,  50,  100],   # 低量 深藍
        [10, 100,  120],   # 中低 青藍
        [30, 170,  100],   # 中   綠
        [200, 180,  30],   # 高   黃
        [255, 220,   0],   # 峰值 亮黃
    ]
    lut = np.zeros((256, 3), dtype=np.uint8)
    n = len(colors) - 1
    for i in range(256):
        t = i / 255.0 * n
        lo = int(t)
        hi = min(lo + 1, n)
        f  = t - lo
        for c in range(3):
            lut[i, c] = int(colors[lo][c] * (1 - f) + colors[hi][c] * f)
    return lut
