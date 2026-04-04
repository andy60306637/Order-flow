"""
CVD（Cumulative Volume Delta）圖表 — 優化版

模式：
  line ─ 折線 + 正負半透明填色（預設）
  bar  ─ 直方圖（每根 bar 對應 footprint candle；正青負紅）

視覺原則：
  - 輔助工具定位，不搶 footprint 主體焦點
  - 填色 alpha 低，保持乾淨
  - 零線低調虛線
  - bar 模式讓方向節奏和上方 footprint 一一對齊

StatsPanel：
  - 顯示於 CVD 下方，三行固定數值
  - 每欄對應一根 K 棒：Volume（總量）, Delta（淨量）, CVD（累計）
"""
from __future__ import annotations
from typing import List, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6.QtCore import Qt, pyqtSignal

import config


_COLOR_POS   = (38,  166, 154,  50)   # 正 CVD 填色（淡青，更低調）
_COLOR_NEG   = (239,  83,  80,  50)   # 負 CVD 填色（淡紅）
_PEN_CVD     = pg.mkPen("#7a8aa0", width=1.2)   # 折線（低調，不與 footprint 搶色）
_PEN_ZERO    = pg.mkPen("#252d3e", width=1, style=Qt.PenStyle.DashLine)

_BR_POS = pg.mkBrush(38,  166, 154, 155)   # bar 模式：正值青色
_BR_NEG = pg.mkBrush(239,  83,  80, 155)   # bar 模式：負值紅色


class CvdChart(pg.PlotWidget):
    """CVD chart，支援 line（折線+填色）和 bar（直方圖）兩種顯示模式。"""

    # 十字線同步訊號
    crosshair_moved = pyqtSignal(float)
    crosshair_left  = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent, background=config.COLOR_BG)

        pi = self.getPlotItem()
        pi.showGrid(x=True, y=True, alpha=0.10)
        pi.setLabel("left", "CVD")
        pi.hideButtons()
        pi.getAxis("bottom").setStyle(showValues=False)
        pi.enableAutoRange(axis='x', enable=False)

        # 零線（低調）
        self._zero_line = pg.InfiniteLine(
            angle=0, pos=0, pen=_PEN_ZERO, movable=False
        )
        pi.addItem(self._zero_line)

        # ── 十字線 ──────────────────────────────────────────────────────────
        _ch_pen = pg.mkPen('#888888', width=0.8, style=Qt.PenStyle.DashLine)
        self._ch_vline = pg.InfiniteLine(angle=90, pen=_ch_pen, movable=False)
        self._ch_hline = pg.InfiniteLine(angle=0,  pen=_ch_pen, movable=False)
        pi.addItem(self._ch_vline, ignoreBounds=True)
        pi.addItem(self._ch_hline, ignoreBounds=True)
        self._ch_vline.setVisible(False)
        self._ch_hline.setVisible(False)
        self._ch_active: bool = False
        self.scene().sigMouseMoved.connect(self._on_mouse_moved)

        # ── 折線模式元件 ──────────────────────────────────────────────────────
        self._curve = pi.plot(pen=_PEN_CVD)
        self._fill_pos = pg.FillBetweenItem(
            curve1=self._curve,
            curve2=pg.PlotDataItem([0], [0]),
            brush=pg.mkBrush(*_COLOR_POS),
        )
        self._fill_neg = pg.FillBetweenItem(
            curve1=self._curve,
            curve2=pg.PlotDataItem([0], [0]),
            brush=pg.mkBrush(*_COLOR_NEG),
        )
        pi.addItem(self._fill_pos)
        pi.addItem(self._fill_neg)

        # ── 直方圖模式元件（正/負各一，顏色分開）──────────────────────────────
        self._bars_pos = pg.BarGraphItem(x=[], height=[], width=0.72, brush=_BR_POS)
        self._bars_neg = pg.BarGraphItem(x=[], height=[], width=0.72, brush=_BR_NEG)
        pi.addItem(self._bars_pos)
        pi.addItem(self._bars_neg)
        self._bars_pos.hide()
        self._bars_neg.hide()

        self._mode = "line"   # "line" | "bar"
        self._x: List[float] = []
        self._y: List[float] = []

    # ─────────────────────────────────────────────────────────────────────────
    def set_cvd_mode(self, mode: str) -> None:
        """切換 'line'（折線+填色）或 'bar'（直方圖）模式。"""
        if mode not in ("line", "bar") or mode == self._mode:
            return
        self._mode = mode
        if mode == "line":
            self._curve.show()
            self._fill_pos.show()
            self._fill_neg.show()
            self._bars_pos.hide()
            self._bars_neg.hide()
        else:
            self._curve.hide()
            self._fill_pos.hide()
            self._fill_neg.hide()
            self._bars_pos.show()
            self._bars_neg.show()
        self._redraw()

    def update_cvd(self, series: List[Tuple[int, float]]) -> None:
        """series: [(open_time_ms, cvd), ...] 依 kline index 排列"""
        if not series:
            return
        self._x = list(range(len(series)))
        self._y = [v for _, v in series]
        self._redraw()

    def _redraw(self) -> None:
        if not self._x:
            return
        x     = self._x
        y     = self._y
        arr_y = np.array(y, dtype=float)
        arr_x = np.array(x, dtype=float)

        if self._mode == "line":
            self._curve.setData(x=x, y=y)
            zero  = np.zeros_like(arr_y)
            y_pos = np.where(arr_y >= 0, arr_y, 0.0)
            y_neg = np.where(arr_y <  0, arr_y, 0.0)
            self._fill_pos.setCurves(
                pg.PlotDataItem(x=x, y=y_pos.tolist()),
                pg.PlotDataItem(x=x, y=zero.tolist()),
            )
            self._fill_neg.setCurves(
                pg.PlotDataItem(x=x, y=y_neg.tolist()),
                pg.PlotDataItem(x=x, y=zero.tolist()),
            )
        else:
            # bar 模式：正負分開畫，使用不同顏色
            pos_mask = arr_y >= 0
            neg_mask = ~pos_mask
            if pos_mask.any():
                self._bars_pos.setOpts(
                    x=arr_x[pos_mask].tolist(),
                    height=arr_y[pos_mask].tolist(),
                    width=0.72,
                )
            else:
                self._bars_pos.setOpts(x=[], height=[], width=0.72)
            if neg_mask.any():
                self._bars_neg.setOpts(
                    x=arr_x[neg_mask].tolist(),
                    height=arr_y[neg_mask].tolist(),
                    width=0.72,
                )
            else:
                self._bars_neg.setOpts(x=[], height=[], width=0.72)

    def link_x(self, other_plot: pg.PlotItem) -> None:
        """將本圖的 x 軸連結到另一個 PlotItem（例如 KlineChart）。"""
        self.getPlotItem().setXLink(other_plot)

    # ── 十字線 ────────────────────────────────────────────────────────────────
    def _on_mouse_moved(self, scene_pos) -> None:
        vb = self.getPlotItem().vb
        if vb.sceneBoundingRect().contains(scene_pos):
            pt = vb.mapSceneToView(scene_pos)
            self._ch_vline.setPos(pt.x())
            self._ch_hline.setPos(pt.y())
            self._ch_vline.setVisible(True)
            self._ch_hline.setVisible(True)
            self._ch_active = True
            self.crosshair_moved.emit(pt.x())
        elif self._ch_active:
            self._ch_active = False
            self._ch_vline.setVisible(False)
            self._ch_hline.setVisible(False)
            self.crosshair_left.emit()

    def set_crosshair_x(self, x: float) -> None:
        self._ch_vline.setPos(x)
        self._ch_vline.setVisible(True)

    def hide_crosshair(self) -> None:
        self._ch_vline.setVisible(False)
        self._ch_hline.setVisible(False)
        self._ch_active = False


# ─────────────────────────────────────────────────────────────────────────────
# StatsItem — 每根 K 棒的 Volume / Delta / CVD 數值文字
# ─────────────────────────────────────────────────────────────────────────────

# 顏色常數（與 footprint 一致的語意）
_C_VOL   = QtGui.QColor(160, 165, 180)          # 成交量：中性灰
_C_POS   = QtGui.QColor( 60, 210, 195)          # 正值（青）
_C_NEG   = QtGui.QColor(235,  90,  88)          # 負值（紅）
_C_GRID  = QtGui.QColor( 45,  52,  68, 120)     # 列分隔線
_C_LABEL = QtGui.QColor(130, 135, 155)          # 列標籤文字


def _fk(v: float) -> str:
    """格式化為 K/M，無符號（用於 Volume）。"""
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if a >= 1_000:
        return f"{v / 1_000:.1f}K"
    return f"{v:.0f}"


def _fks(v: float) -> str:
    """格式化為帶正負號的 K/M（用於 Delta / CVD）。"""
    sign = "+" if v >= 0 else ""
    a = abs(v)
    if a >= 1_000_000:
        return f"{sign}{v / 1_000_000:.1f}M"
    if a >= 1_000:
        return f"{sign}{v / 1_000:.1f}K"
    return f"{sign}{v:.0f}"


class StatsItem(pg.GraphicsObject):
    """
    直接以 device 座標繪製三行統計數值（避免 QPicture + y-axis 倒置的 drawText 問題）。
    座標系：x = candle index，y ∈ [0, 3)
      行 2（上）：Volume
      行 1（中）：Delta
      行 0（下）：CVD
    """

    def __init__(self) -> None:
        pg.GraphicsObject.__init__(self)
        self._candles: list = []
        self._cvd_map: dict = {}   # open_time_ms → cvd value
        self._ot_to_idx: dict = {}  # open_time_ms → kline index

    # ── 公開 API ─────────────────────────────────────────────────────────────

    def set_data(self, candles: list, cvd_series: List[Tuple[int, float]]) -> None:
        prev_n = len(self._candles)
        self._candles = candles
        self._cvd_map = {ot: v for ot, v in cvd_series}
        self._ot_to_idx = {ot: i for i, (ot, _) in enumerate(cvd_series)}
        if len(candles) != prev_n:
            self.prepareGeometryChange()
            self.informViewBoundsChanged()
        self.update()

    # ── pyqtgraph 介面 ────────────────────────────────────────────────────────

    def boundingRect(self) -> QtCore.QRectF:
        if not self._candles or not self._ot_to_idx:
            return QtCore.QRectF(-0.5, -0.05, 1.5, 3.1)
        indices = [self._ot_to_idx[int(c.open_time)]
                   for c in self._candles if int(c.open_time) in self._ot_to_idx]
        if not indices:
            return QtCore.QRectF(-0.5, -0.05, 1.5, 3.1)
        lo, hi = min(indices), max(indices)
        return QtCore.QRectF(lo - 0.5, -0.05, hi - lo + 1.5, 3.1)

    def paint(self, p: QtGui.QPainter, *args) -> None:
        if not self._candles:
            return

        # 取得 data → device transform，之後全部在 device pixel 座標繪製
        t = p.transform()
        p.save()
        p.resetTransform()

        # 行高與欄寬（device pixels）
        row_h = abs(t.m22())          # 1 data unit 在 y 軸的像素數
        col_w = abs(t.m11())          # 1 data unit 在 x 軸的像素數

        # 字型大小：以行高為基準，最小 7px 最大 11px
        font_px = max(7, min(11, int(row_h * 0.50)))
        font = QtGui.QFont("Consolas")
        font.setPixelSize(font_px)
        p.setFont(font)

        # 逐格繪製文字（使用 kline index 對齊 x 軸）
        for candle in self._candles:
            idx = self._ot_to_idx.get(int(candle.open_time))
            if idx is None:
                continue
            vol   = candle.total_volume
            delta = candle.delta
            cvd   = self._cvd_map.get(int(candle.open_time), 0.0)

            rows = [
                (2, _fk(vol),    _C_VOL),
                (1, _fks(delta), _C_POS if delta >= 0 else _C_NEG),
                (0, _fks(cvd),   _C_POS if cvd   >= 0 else _C_NEG),
            ]

            for row_y, text, col in rows:
                # 將格中心從 data 座標映射到 device 像素
                cx = t.map(QtCore.QPointF(idx, row_y + 0.5)).x()
                cy = t.map(QtCore.QPointF(idx, row_y + 0.5)).y()
                rect = QtCore.QRectF(
                    cx - col_w * 0.48,
                    cy - row_h * 0.45,
                    col_w * 0.96,
                    row_h * 0.90,
                )
                p.setPen(pg.mkPen(col))
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

        # 水平分隔線（y=1, y=2 data → device）
        all_idx = [self._ot_to_idx[int(c.open_time)]
                   for c in self._candles if int(c.open_time) in self._ot_to_idx]
        if all_idx:
            lo, hi = min(all_idx), max(all_idx)
        else:
            lo, hi = 0, max(len(self._candles) - 1, 0)
        p.setPen(pg.mkPen(_C_GRID, width=0.5))
        for y_data in (1.0, 2.0):
            p0 = t.map(QtCore.QPointF(lo - 0.5,  y_data))
            p1 = t.map(QtCore.QPointF(hi + 0.5,  y_data))
            p.drawLine(p0, p1)

        p.restore()


# ─────────────────────────────────────────────────────────────────────────────
# StatsPanel — PlotWidget 包裝
# ─────────────────────────────────────────────────────────────────────────────

class StatsPanel(pg.PlotWidget):
    """
    固定三行的統計面板，顯示在 CVD chart 下方。
    外部 API：
      update_data(candles, cvd_series) ─ 傳入最新資料
      link_x(PlotItem)                 ─ 連結 x 軸
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent, background=config.COLOR_BG)

        pi = self.getPlotItem()
        pi.hideButtons()
        pi.showGrid(x=True, y=False, alpha=0.08)

        # y 軸：固定 3 行，顯示列標籤
        pi.setYRange(0, 3, padding=0)
        left_ax = pi.getAxis("left")
        left_ax.setTicks([[(2.5, "Vol"), (1.5, "Δ"), (0.5, "CVD")]])
        left_ax.setWidth(36)
        left_ax.setStyle(tickLength=0)

        # x 軸：不顯示（由 footprint/kline chart 的 x 軸代理）
        pi.getAxis("bottom").setStyle(showValues=False, tickLength=0)
        pi.getAxis("bottom").setHeight(0)

        # 鎖定 y 軸縮放
        pi.setMouseEnabled(x=True, y=False)
        pi.vb.setLimits(yMin=0, yMax=3)
        pi.enableAutoRange(axis='x', enable=False)

        self._item = StatsItem()
        pi.addItem(self._item)

        # ── 十字線（僅垂直線）───────────────────────────────────
        _ch_pen = pg.mkPen('#888888', width=0.8, style=Qt.PenStyle.DashLine)
        self._ch_vline = pg.InfiniteLine(angle=90, pen=_ch_pen, movable=False)
        pi.addItem(self._ch_vline, ignoreBounds=True)
        self._ch_vline.setVisible(False)

    def update_data(
        self,
        candles: list,
        cvd_series: List[Tuple[int, float]],
    ) -> None:
        self._item.set_data(candles, cvd_series)

    def link_x(self, other_plot: pg.PlotItem) -> None:
        self.getPlotItem().setXLink(other_plot)

    def set_crosshair_x(self, x: float) -> None:
        self._ch_vline.setPos(x)
        self._ch_vline.setVisible(True)

    def hide_crosshair(self) -> None:
        self._ch_vline.setVisible(False)
