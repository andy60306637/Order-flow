"""MFE/MAE 散點圖（Maximum Favorable / Adverse Excursion）。"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QVBoxLayout, QWidget


class MfeMaeChart(QWidget):
    """
    x 軸 = MAE（最大不利偏移，越大越虧）
    y 軸 = MFE（最大有利偏移，越大越賺）
    綠點 = 獲利交易，紅點 = 虧損交易
    """

    trade_selected = pyqtSignal(dict)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground("#131722")
        layout.addWidget(self._glw)

        self._plot = self._glw.addPlot()
        self._plot.setLabel("bottom", "MAE (USDT)", color="#d1d4dc")
        self._plot.setLabel("left",   "MFE (USDT)", color="#d1d4dc")
        self._plot.setTitle("MFE / MAE", color="#d1d4dc")
        self._plot.getAxis("left").setTextPen("#d1d4dc")
        self._plot.getAxis("bottom").setTextPen("#787b86")
        self._plot.showGrid(x=True, y=True, alpha=0.15)
        self._plot.setMenuEnabled(False)

        self._win_scatter = pg.ScatterPlotItem(
            pen=None,
            brush=pg.mkBrush("#26a69a88"),
            symbol="o", size=6,
        )
        self._loss_scatter = pg.ScatterPlotItem(
            pen=None,
            brush=pg.mkBrush("#ef535088"),
            symbol="o", size=6,
        )
        self._plot.addItem(self._win_scatter)
        self._plot.addItem(self._loss_scatter)

        # 對角參考線（MAE = MFE → 損益兩平）
        self._diag = self._plot.plot(pen=pg.mkPen("#787b8655", width=1))

        self._trades: list[dict] = []
        self._win_scatter.sigClicked.connect(self._on_scatter_clicked)
        self._loss_scatter.sigClicked.connect(self._on_scatter_clicked)

    def load_result(self, stats: dict) -> None:
        trade_list = stats.get("trade_list", [])
        active = [t for t in trade_list if not t.get("skipped")]
        self._trades = active

        if not active:
            self.clear()
            return

        wins  = [t for t in active if t.get("net_pnl", 0) > 0]
        losses = [t for t in active if t.get("net_pnl", 0) <= 0]

        def _points(trades):
            pts = []
            for i, t in enumerate(trades):
                mae = abs(t.get("mae", t.get("MAE", 0.0)) or 0.0)
                mfe = abs(t.get("mfe", t.get("MFE", 0.0)) or 0.0)
                pts.append({"pos": (mae, mfe), "data": i})
            return pts

        self._win_scatter.setData(_points(wins))
        self._loss_scatter.setData(_points(losses))

        # 對角線範圍
        all_mae = [abs(t.get("mae", t.get("MAE", 0.0)) or 0.0) for t in active]
        all_mfe = [abs(t.get("mfe", t.get("MFE", 0.0)) or 0.0) for t in active]
        mx = max(max(all_mae, default=1), max(all_mfe, default=1))
        self._diag.setData([0, mx], [0, mx])

    def clear(self) -> None:
        self._win_scatter.setData([])
        self._loss_scatter.setData([])
        self._diag.setData([], [])

    def _on_scatter_clicked(self, scatter, points) -> None:
        if points and self._trades:
            idx = points[0].data()
            if isinstance(idx, int) and idx < len(self._trades):
                self.trade_selected.emit(self._trades[idx])
