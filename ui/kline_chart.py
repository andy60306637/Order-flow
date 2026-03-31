"""
K 線圖（Candlestick + Volume）。

- 使用 pyqtgraph PlotWidget
- CandlestickItem：自訂 GraphicsObject，以 QPicture 預渲染
- 底部 volume bar（買=綠/賣=紅），獨立 ViewBox
- 自訂時間軸（index → HH:MM）
- 支援線性 / 對數 Y 軸切換（toggle_log_scale）
- 對外暴露 set_history() / update_candle() / toggle_log_scale()
"""
from __future__ import annotations
import math
from datetime import datetime
from typing import List, Optional

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6.QtCore import Qt, pyqtSignal

import config
from core.data_types import Kline


# ── 自訂時間軸 ────────────────────────────────────────────────────────────────
class _TimeAxis(pg.AxisItem):
    def __init__(self) -> None:
        super().__init__(orientation="bottom")
        self.timestamps: List[int] = []  # ms epoch，與 candle index 對齊

    def tickStrings(self, values, scale, spacing) -> List[str]:
        strs = []
        for v in values:
            idx = int(round(v))
            if 0 <= idx < len(self.timestamps):
                strs.append(
                    datetime.fromtimestamp(
                        self.timestamps[idx] / 1000,
                        tz=config.DISPLAY_TZ,
                    ).strftime("%m/%d\n%H:%M")
                )
            else:
                strs.append("")
        return strs


# ── 自訂價格軸（線性 / 對數雙模式）─────────────────────────────────────────────
class _PriceAxis(pg.AxisItem):
    def __init__(self) -> None:
        super().__init__(orientation="left")
        self.log_mode: bool = False

    def tickStrings(self, values, scale, spacing) -> List[str]:
        if self.log_mode:
            # values 是 log10(price)，還原顯示真實價格
            result = []
            for v in values:
                # 防止超大/超小 v 值導致 OverflowError（pyqtgraph 有時傳未裁切的 viewport 值）
                if not (-300 < v < 308):
                    result.append("")
                    continue
                try:
                    price = 10 ** v
                except OverflowError:
                    result.append("")
                    continue
                if price >= 1000:
                    result.append(f"{price:,.0f}")
                elif price >= 1:
                    result.append(f"{price:.4f}")
                else:
                    result.append(f"{price:.6f}")
            return result
        # 線性模式：直接格式化
        result = []
        for v in values:
            if v >= 1000:
                result.append(f"{v:,.1f}")
            elif v >= 1:
                result.append(f"{v:.4f}")
            else:
                result.append(f"{v:.6f}")
        return result


# ── Candlestick GraphicsObject ────────────────────────────────────────────────
class CandlestickItem(pg.GraphicsObject):
    """
    data: list of (index, open, close, low, high)
    """

    def __init__(self) -> None:
        pg.GraphicsObject.__init__(self)
        self._data: List[tuple] = []
        self._picture: Optional[QtGui.QPicture] = None

    def set_data(self, data: List[tuple]) -> None:
        self._data = data
        self._picture = None
        self.prepareGeometryChange()
        self.informViewBoundsChanged()
        self.update()

    def _build(self) -> None:
        self._picture = QtGui.QPicture()
        painter = QtGui.QPainter(self._picture)

        color_up   = QtGui.QColor(config.COLOR_UP)
        color_down = QtGui.QColor(config.COLOR_DOWN)
        w = 0.38  # 半寬

        for (idx, op, cl, lo, hi) in self._data:
            if cl >= op:
                painter.setPen(pg.mkPen(color_up,   width=1))
                painter.setBrush(pg.mkBrush(color_up))
            else:
                painter.setPen(pg.mkPen(color_down, width=1))
                painter.setBrush(pg.mkBrush(color_down))

            # 上下影線
            painter.drawLine(
                QtCore.QPointF(idx, lo), QtCore.QPointF(idx, hi)
            )
            # 實體
            body_h = abs(cl - op)
            if body_h < 1e-10:
                body_h = 1e-8
            painter.drawRect(
                QtCore.QRectF(idx - w, min(op, cl), 2 * w, body_h)
            )

        painter.end()

    def paint(self, p: QtGui.QPainter, *args) -> None:
        if not self._data:
            return
        if self._picture is None:
            self._build()
        self._picture.play(p)

    def boundingRect(self) -> QtCore.QRectF:
        if not self._data:
            return QtCore.QRectF()
        lo = min(d[3] for d in self._data)
        hi = max(d[4] for d in self._data)
        x0 = self._data[0][0] - 0.5
        x1 = self._data[-1][0] + 0.5
        return QtCore.QRectF(x0, lo, x1 - x0, hi - lo)


# ── KlineChart widget ─────────────────────────────────────────────────────────
class KlineChart(pg.PlotWidget):
    # 滾到最左端時發出，帶帶最舊一根 K 棒的 open_time_ms
    need_more_history = pyqtSignal(int)

    def __init__(self, parent=None) -> None:
        self._time_axis  = _TimeAxis()
        self._price_axis = _PriceAxis()
        super().__init__(
            parent,
            axisItems={"bottom": self._time_axis, "left": self._price_axis},
            background=config.COLOR_BG,
        )
        self._log_mode: bool = False

        pi = self.getPlotItem()
        pi.showGrid(x=True, y=True, alpha=0.25)
        pi.setLabel("left", "Price")
        pi.hideButtons()
        pi.enableAutoRange(axis='x', enable=False)

        # ── Candlestick ─────────────────────────────────────────────────────
        self._candle_item = CandlestickItem()
        pi.addItem(self._candle_item)

        # ── 最新價格橫線 ────────────────────────────────────────────────────
        self._price_line = pg.InfiniteLine(
            angle=0,
            pen=pg.mkPen(config.COLOR_FG, width=1, style=Qt.PenStyle.DashLine),
            movable=False,
        )
        pi.addItem(self._price_line)

        # ── Volume 獨立 ViewBox（底部 20%）─────────────────────────────────
        self._vol_vb = pg.ViewBox()
        pi.scene().addItem(self._vol_vb)
        self._vol_bar = pg.BarGraphItem(x=[], height=[], width=0.76, brushes=[])
        self._vol_vb.addItem(self._vol_bar)
        self._vol_vb.setXLink(pi.vb)

        pi.vb.sigResized.connect(self._update_vol_vb_geo)

        # 偵測使用者滚到最左端，觸發載入更早歷史
        self._loading_more: bool = False
        pi.vb.sigXRangeChanged.connect(self._on_x_range_changed)

        # 內部資料
        self._klines: List[Kline] = []
        self._candle_data: List[tuple] = []  # (idx, o, c, l, h)
        self._vol_x:      List[float] = []
        self._vol_h:      List[float] = []
        self._vol_colors: list = []
        self._need_auto_range: bool = False  # 僅在歷史載入時自動設定 x 範圍

    # ──────────────────────────────────────────────────────────────────────────
    def _update_vol_vb_geo(self) -> None:
        pi  = self.getPlotItem()
        vbr = pi.vb.sceneBoundingRect()
        # Volume ViewBox 佔下方 20%
        vol_h = vbr.height() * 0.20
        self._vol_vb.setGeometry(
            QtCore.QRectF(
                vbr.x(), vbr.y() + vbr.height() - vol_h, vbr.width(), vol_h
            )
        )
        self._vol_vb.linkedViewChanged(pi.vb, self._vol_vb.XAxis)

    # ──────────────────────────────────────────────────────────────────────────
    def toggle_log_scale(self) -> bool:
        """
        切換 Y 軸線性 / 對數顯示。
        回傳切換後的狀態（True = 對數）。
        """
        self._log_mode = not self._log_mode
        self._price_axis.log_mode = self._log_mode
        self._rebuild_all()
        return self._log_mode

    # ──────────────────────────────────────────────────────────────────────────
    def _p(self, price: float) -> float:
        """根據當前模式轉換單一價格值。"""
        if self._log_mode and price > 0:
            return math.log10(price)
        return price

    # ──────────────────────────────────────────────────────────────────────────
    def set_history(self, klines: List[Kline]) -> None:
        self._klines = list(klines)
        self._need_auto_range = True
        self._rebuild_all()

    def update_candle(self, kline: Kline) -> None:
        """新增或更新最後一根 K 棒。"""
        is_new = False
        if not self._klines:
            self._klines.append(kline)
            is_new = True
        elif self._klines[-1].open_time == kline.open_time:
            self._klines[-1] = kline
        else:
            self._klines.append(kline)
            is_new = True
            # 限制記憶體
            if len(self._klines) > config.KLINE_HISTORY_LIMIT + 50:
                self._klines = self._klines[-(config.KLINE_HISTORY_LIMIT):]

        self._rebuild_all(follow_new=is_new)

    # ──────────────────────────────────────────────────────────────────────────
    def _rebuild_all(self, follow_new: bool = False) -> None:
        candle_data = []
        vol_x, vol_h, vol_brsh = [], [], []
        timestamps = []

        color_up   = pg.mkBrush(config.COLOR_UP)
        color_down = pg.mkBrush(config.COLOR_DOWN)

        for i, k in enumerate(self._klines):
            # 價格轉換（線性或 log10）
            o = self._p(k.open)
            c = self._p(k.close)
            lo = self._p(k.low)
            hi = self._p(k.high)
            candle_data.append((i, o, c, lo, hi))
            vol_x.append(i)
            vol_h.append(k.volume)
            vol_brsh.append(color_up if k.close >= k.open else color_down)
            timestamps.append(k.open_time)

        self._candle_data = candle_data
        self._candle_item.set_data(candle_data)
        self._time_axis.timestamps = timestamps

        # Volume bar
        if vol_x:
            self._vol_bar.setOpts(
                x=vol_x, height=vol_h, width=0.76, brushes=vol_brsh
            )
            max_vol = max(vol_h) if vol_h else 1
            self._vol_vb.setYRange(0, max_vol * 5, padding=0)

        # 最新價格線（也需要 log 轉換）
        if self._klines:
            self._price_line.setValue(self._p(self._klines[-1].close))

        if not candle_data:
            return

        n = len(candle_data)
        pi = self.getPlotItem()

        if self._need_auto_range:
            # 歷史載入：自動顯示最後 80 根
            self._need_auto_range = False
            pi.setXRange(max(0, n - 80), n - 1, padding=0.05)
        elif follow_new:
            # 新蠟燭出現：若使用者正在看最新區域，自動右移一格
            vr = pi.viewRange()[0]  # [xMin, xMax]
            # 如果之前的最後一根在可見範圍內（± 2 根緩衝），自動跟隨
            if n - 2 <= vr[1] + 2:
                width = vr[1] - vr[0]
                pi.setXRange(n - 1 - width, n - 1, padding=0)

    # ── 往前捲動歷史載入 ──────────────────────────────────────────────────────
    def _on_x_range_changed(self, vb, x_range) -> None:
        """偵測使用者是否滾到最左邊，若是則觸發歷史資料請求。"""
        if self._loading_more or not self._klines:
            return
        # 可見左邊界在距最舊 K 棒 15 根以內時觸發
        if x_range[0] < 15:
            self._loading_more = True
            self.need_more_history.emit(self._klines[0].open_time)

    def set_loading_more(self, loading: bool) -> None:
        """由 MainWindow 在請求完成後呼叫，解除 loading 鎖定。"""
        self._loading_more = loading

    def prepend_history(self, klines: List[Kline]) -> None:
        """
        將更舊的 K 棒插入最前端，並平移視圖以保持當前畫面不跳動。
        klines 應已排序（最舊在前）且不包含與現有資料重疊的 K 棒。
        """
        if not klines:
            return
        m = len(klines)
        pi = self.getPlotItem()
        vr = pi.viewRange()[0]   # 儲存當前可見範圍

        self._klines = list(klines) + self._klines
        self._rebuild_all()

        # 將視圖右移 m 格，保持使用者正在看的位置不變
        pi.setXRange(vr[0] + m, vr[1] + m, padding=0)

    # ──────────────────────────────────────────────────────────────────────────
    def get_plot_item(self) -> pg.PlotItem:
        return self.getPlotItem()
