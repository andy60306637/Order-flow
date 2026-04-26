"""資金曲線圖 + 水下回撤圖（pyqtgraph）。"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QVBoxLayout, QWidget


class EquityChart(QWidget):
    """
    上方：資金曲線（折線）
    下方：水下回撤面積圖（紅色填充，負值 y 軸）
    兩圖共用 x 軸。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground("#131722")
        layout.addWidget(self._glw)

        # ── 資金曲線 ─────────────────────────────────────────────────────────
        self._eq_plot = self._glw.addPlot(row=0, col=0)
        self._eq_plot.setLabel("left", "Equity (USDT)", color="#d1d4dc")
        self._eq_plot.getAxis("left").setTextPen("#d1d4dc")
        self._eq_plot.getAxis("bottom").setTextPen("#787b86")
        self._eq_plot.showGrid(x=True, y=True, alpha=0.15)
        self._eq_plot.setMenuEnabled(False)

        self._eq_line = self._eq_plot.plot(
            pen=pg.mkPen("#26a69a", width=1.5),
            name="Equity",
        )
        self._bench_line = self._eq_plot.plot(
            pen=pg.mkPen("#787b86", width=1, style=Qt.PenStyle.DashLine),
            name="Initial",
        )

        # ── 回撤圖 ────────────────────────────────────────────────────────────
        self._glw.nextRow()
        self._dd_plot = self._glw.addPlot(row=1, col=0)
        self._dd_plot.setLabel("left", "Drawdown %", color="#ef5350")
        self._dd_plot.getAxis("left").setTextPen("#ef5350")
        self._dd_plot.getAxis("bottom").setTextPen("#787b86")
        self._dd_plot.showGrid(x=True, y=True, alpha=0.15)
        self._dd_plot.setMenuEnabled(False)
        self._dd_plot.setXLink(self._eq_plot)

        self._dd_fill = pg.FillBetweenItem(
            pg.PlotDataItem(),
            pg.PlotDataItem(),
            brush=pg.mkBrush("#ef535055"),
        )
        self._dd_plot.addItem(self._dd_fill)
        self._dd_line = self._dd_plot.plot(
            pen=pg.mkPen("#ef5350", width=1),
        )

        # 設定高度比例
        self._glw.ci.layout.setRowStretchFactor(0, 3)
        self._glw.ci.layout.setRowStretchFactor(1, 1)

    # ── 資料載入 ──────────────────────────────────────────────────────────────

    def load_result(self, stats: dict) -> None:
        trade_list = stats.get("trade_list", [])
        active = [t for t in trade_list if not t.get("skipped")]
        if not active:
            self.clear()
            return

        equity_vals = np.array([t["equity_after"] for t in active], dtype=float)
        x = np.arange(len(equity_vals))

        # 資金曲線
        self._eq_line.setData(x, equity_vals)

        # 初始資金水平線
        initial = stats.get("initial_capital", equity_vals[0])
        self._bench_line.setData(
            [0, len(equity_vals) - 1],
            [initial, initial],
        )

        # 回撤序列（負值）
        dd = self._compute_drawdown(equity_vals)
        self._dd_line.setData(x, -dd)

        zero_line = pg.PlotDataItem(x, np.zeros_like(x))
        dd_line   = pg.PlotDataItem(x, -dd)
        self._dd_fill.setCurves(dd_line, zero_line)

    def clear(self) -> None:
        self._eq_line.setData([], [])
        self._bench_line.setData([], [])
        self._dd_line.setData([], [])

    # ── 工具函式 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_drawdown(equity: np.ndarray) -> np.ndarray:
        """計算逐筆回撤百分比（0~100）。"""
        peak = np.maximum.accumulate(equity)
        dd = np.where(peak > 0, (peak - equity) / peak * 100, 0.0)
        return dd
