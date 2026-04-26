"""2D 參數優化熱力圖（pyqtgraph ImageItem）。"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import QVBoxLayout, QWidget


class OptimizationHeatmap(QWidget):
    """
    呈現雙參數掃描的優化結果。

    資料格式：
      results = [
          {"x": float, "y": float, "value": float},
          ...
      ]
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground("#131722")
        layout.addWidget(self._glw)

        self._plot = self._glw.addPlot()
        self._plot.setMenuEnabled(False)
        self._plot.getAxis("left").setTextPen("#d1d4dc")
        self._plot.getAxis("bottom").setTextPen("#787b86")

        self._img = pg.ImageItem()
        self._plot.addItem(self._img)

        # 顏色映射：藍→綠→黃
        cmap = pg.colormap.get("CET-L4")
        self._bar = pg.ColorBarItem(
            values=(0, 1),
            colorMap=cmap,
            label="Metric",
            interactive=False,
        )
        self._bar.setImageItem(self._img, insert_in=self._plot)

        self._x_label = "Param X"
        self._y_label = "Param Y"

    def load_optimization(
        self,
        results: list[dict],
        x_label: str = "Param X",
        y_label: str = "Param Y",
        metric_label: str = "Metric",
    ) -> None:
        if not results:
            return

        self._x_label = x_label
        self._y_label = y_label
        self._plot.setLabel("bottom", x_label, color="#d1d4dc")
        self._plot.setLabel("left",   y_label, color="#d1d4dc")

        xs = sorted({r["x"] for r in results})
        ys = sorted({r["y"] for r in results})
        xi = {v: i for i, v in enumerate(xs)}
        yi = {v: i for i, v in enumerate(ys)}

        grid = np.full((len(xs), len(ys)), np.nan)
        for r in results:
            grid[xi[r["x"]], yi[r["y"]]] = r["value"]

        # NaN 填充最小值
        vmin = np.nanmin(grid)
        grid = np.where(np.isnan(grid), vmin, grid)

        self._img.setImage(grid)
        self._img.setRect(
            pg.QtCore.QRectF(0, 0, len(xs), len(ys))
        )

        vmax = np.nanmax(grid)
        self._bar.setLevels((vmin, vmax))
        self._bar.setLabel(metric_label)

    def clear(self) -> None:
        self._img.clear()
