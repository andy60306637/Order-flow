"""Monte Carlo 模擬分佈直方圖（pyqtgraph）。"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from PyQt6.QtCore import QThread


class _MonteCarloWorker(QThread):
    done = pyqtSignal(list)

    def __init__(
        self,
        pnls: list[float],
        initial_equity: float,
        n_iterations: int,
        seed: Optional[int],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._pnls = pnls
        self._initial = initial_equity
        self._n = n_iterations
        self._seed = seed

    def run(self) -> None:
        from backtest.engine import run_monte_carlo
        results = run_monte_carlo(
            [{"net_pnl": p} for p in self._pnls],
            self._initial,
            self._n,
            self._seed,
        )
        self.done.emit(results)


class MonteCarloChart(QWidget):
    """Monte Carlo 重抽樣最終資金分佈直方圖。"""

    simulation_done = pyqtSignal(list)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground("#131722")
        layout.addWidget(self._glw)

        self._plot = self._glw.addPlot()
        self._plot.setTitle("Monte Carlo (1000 runs)", color="#d1d4dc")
        self._plot.setLabel("bottom", "Final Equity (USDT)", color="#d1d4dc")
        self._plot.setLabel("left",   "Frequency",           color="#d1d4dc")
        self._plot.getAxis("left").setTextPen("#d1d4dc")
        self._plot.getAxis("bottom").setTextPen("#787b86")
        self._plot.showGrid(x=True, y=True, alpha=0.15)
        self._plot.setMenuEnabled(False)

        self._bar_graph: Optional[pg.BarGraphItem] = None
        self._lines: list[pg.InfiniteLine] = []
        self._worker: Optional[_MonteCarloWorker] = None
        self._initial_equity: float = 10_000.0

    def run_simulation(
        self,
        trade_list: list[dict],
        n_iterations: int = 1000,
        seed: Optional[int] = None,
    ) -> None:
        active = [t for t in trade_list if not t.get("skipped")]
        if not active:
            return

        pnls = [t["net_pnl"] for t in active]
        self._initial_equity = active[0].get("equity_after", 10_000.0) - active[0].get("net_pnl", 0)

        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait()

        self._worker = _MonteCarloWorker(
            pnls, self._initial_equity, n_iterations, seed, parent=self
        )
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, results: list[float]) -> None:
        self.simulation_done.emit(results)
        self._draw(results)

    def _draw(self, results: list[float]) -> None:
        self._plot.clear()
        arr = np.array(results, dtype=float)

        counts, edges = np.histogram(arr, bins=50)
        widths = np.diff(edges)

        # 依正負著色
        colors = []
        for e in edges[:-1]:
            colors.append("#26a69a" if e >= self._initial_equity else "#ef5350")

        bar = pg.BarGraphItem(
            x=edges[:-1],
            height=counts,
            width=widths * 0.9,
            brushes=colors,
        )
        self._plot.addItem(bar)

        # 分位線
        for pct, label, color in [
            (5,  "P5",  "#ef5350"),
            (50, "P50", "#d1d4dc"),
            (95, "P95", "#26a69a"),
        ]:
            val = float(np.percentile(arr, pct))
            line = pg.InfiniteLine(
                pos=val, angle=90,
                pen=pg.mkPen(color, width=1, style=pg.QtCore.Qt.PenStyle.DashLine),
                label=f"{label}:{val:.0f}",
                labelOpts={"color": color, "position": 0.85},
            )
            self._plot.addItem(line)

    def clear(self) -> None:
        self._plot.clear()
