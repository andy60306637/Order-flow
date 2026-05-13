from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import pyqtgraph as pg
import numpy as np
from datetime import datetime, timezone

from config import base as cfg_base
from research.regime_filter import RegimeFilterConfig, label_display_name
from research.registry import ensure_builtin_factors, list_factors
from research.runner import ResearchConfig, run_research, run_research_with_regimes
from ui.factors_dialog import FactorsDialog
from ui.parameters_dialog import ParametersDialog
from ui.regime_filter_dialog import RegimeFilterDialog
from ui.time_slice_dialog import TimeSliceDialog
from ui.time_slice_widget import TimeSliceWidget
from utils.ui_settings import ui_settings

# ── Style constants ───────────────────────────────────────────────────────────

_S_DIM  = "color: #787b86; font-size: 11px;"
_S_INFO = "color: #d1d4dc; font-size: 11px;"
_S_OK   = "color: #26a69a; font-size: 11px;"
_S_FIELD = (
    "color: #8f96a8; font-size: 10px; font-weight: 700;"
    " padding: 4px 2px 0 2px;"
)
_S_STATUS_DIM = (
    "QLabel { color: #8f96a8; background: #161c29; border: 1px solid #273145;"
    " border-radius: 6px; padding: 5px 8px; font-size: 11px; }"
)
_S_STATUS_INFO = (
    "QLabel { color: #dce3ee; background: #1b2636; border: 1px solid #35516d;"
    " border-radius: 6px; padding: 5px 8px; font-size: 11px; }"
)
_S_STATUS_OK = (
    "QLabel { color: #8fe7d8; background: #122724; border: 1px solid #24796e;"
    " border-radius: 6px; padding: 5px 8px; font-size: 11px; }"
)


def _field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(_S_FIELD)
    return label

# ── IC Time-Series Chart ──────────────────────────────────────────────────────

class FactorPerformanceChart(QWidget):
    """Time-series chart for factor Rank IC."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground("#131722")
        layout.addWidget(self._glw)

        self._plot = self._glw.addPlot()
        self._plot.setLabel("left", "Oriented Rank IC (Rolling)", color="#d1d4dc")
        self._plot.getAxis("left").setTextPen("#d1d4dc")
        self._plot.getAxis("bottom").setTextPen("#787b86")
        self._plot.showGrid(x=True, y=True, alpha=0.15)
        self._plot.addLegend(offset=(10, 10))

        self._v_line = pg.InfiniteLine(angle=90, movable=False, pen="#787b86")
        self._h_line = pg.InfiniteLine(angle=0,  movable=False, pen="#787b86")
        self._plot.addItem(self._v_line, ignoreBounds=True)
        self._plot.addItem(self._h_line, ignoreBounds=True)

        self._proxy = pg.SignalProxy(
            self._plot.scene().sigMouseMoved, rateLimit=60, slot=self._mouse_moved
        )
        self._lines: dict = {}
        self._data:  dict = {}
        self._timestamps: list = []
        self._cutoff_line: pg.InfiniteLine | None = None

        self._hover_label = pg.TextItem(anchor=(0, 0), color="#d1d4dc")
        self._plot.addItem(self._hover_label, ignoreBounds=True)

    def set_data(self, ts_data: dict) -> None:
        self.clear()
        if not ts_data:
            return

        self._timestamps = ts_data.get("timestamps", [])
        if not self._timestamps:
            return

        x = np.array(self._timestamps) / 1000.0
        self._plot.setAxisItems({"bottom": pg.DateAxisItem()})

        colors = ["#26a69a", "#ef5350", "#2196f3", "#ff9800",
                  "#9c27b0", "#e91e63", "#4caf50", "#ffeb3b"]
        for i, (name, y_list) in enumerate(ts_data.get("factors", {}).items()):
            if not y_list:
                continue
            y = np.array(y_list)
            line = self._plot.plot(x, y, pen=pg.mkPen(colors[i % len(colors)], width=1.5), name=name)
            self._lines[name] = line
            self._data[name] = (x, y)

        cut_ts = ts_data.get("train_cutoff_ts", 0)
        if cut_ts:
            self._cutoff_line = pg.InfiniteLine(
                pos=cut_ts / 1000.0, angle=90, movable=False,
                pen=pg.mkPen("#f59e0b", width=1, style=Qt.PenStyle.DashLine),
                label="IS | OOS", labelOpts={"color": "#f59e0b", "position": 0.95},
            )
            self._plot.addItem(self._cutoff_line, ignoreBounds=True)

    def _mouse_moved(self, evt) -> None:
        pos = evt[0]
        if not self._plot.sceneBoundingRect().contains(pos):
            return
        mp = self._plot.vb.mapSceneToView(pos)
        self._v_line.setPos(mp.x())
        self._h_line.setPos(mp.y())
        if not self._timestamps:
            return
        idx = int(np.searchsorted(np.array(self._timestamps) / 1000.0, mp.x()))
        idx = min(idx, len(self._timestamps) - 1)
        ts  = self._timestamps[idx]
        dt_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        text = f"Time: {dt_str}\n"
        for name, (vx, vy) in self._data.items():
            if idx < len(vy):
                text += f"{name}: {vy[idx]:.4f}\n"
        self._hover_label.setHtml(
            f'<div style="background-color:#1e222d;padding:5px;border:1px solid #363c4e;">{text}</div>'
        )
        self._hover_label.setPos(mp.x(), mp.y())

    def clear(self) -> None:
        for line in self._lines.values():
            self._plot.removeItem(line)
        if self._cutoff_line is not None:
            self._plot.removeItem(self._cutoff_line)
            self._cutoff_line = None
        self._lines = {}
        self._data  = {}
        self._timestamps = []


# ── Regime Matrix Widget ──────────────────────────────────────────────────────

_METRIC_KEYS = {
    "OOS Rank IC": "oos_oriented_rank_ic",
    "OOS IC IR":   "oos_oriented_ic_ir",
    "OOS t-stat":  "oos_oriented_ic_t_stat",
}


def _ic_bg_color(value: float, metric_key: str) -> str:
    if not np.isfinite(value):
        return "#1e222d"
    if metric_key == "oos_oriented_rank_ic":
        if value >= 0.05:  return "#163029"
        if value >= 0.02:  return "#152623"
        if value > 0:      return "#141f1d"
        if value <= -0.05: return "#2e1616"
        if value <= -0.02: return "#241515"
        if value < 0:      return "#1d1414"
    elif metric_key == "oos_oriented_ic_t_stat":
        a = abs(value)
        if a >= 2.0:  return "#163029" if value > 0 else "#2e1616"
        if a >= 1.65: return "#152623" if value > 0 else "#241515"
    return "#1e222d"


def _ic_text_color(value: float, metric_key: str) -> str:
    if not np.isfinite(value):
        return "#4a4e5a"
    if metric_key == "oos_oriented_ic_t_stat":
        a = abs(value)
        if a >= 2.0:  return "#d1d4dc"
        if a >= 1.65: return "#9598a1"
        return "#4a4e5a"
    return "#d1d4dc"


class RegimeMatrixWidget(QWidget):
    """Factor × Regime IC 矩陣表格。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.addWidget(QLabel("Metric:"))
        self._metric_combo = QComboBox()
        self._metric_combo.addItems(list(_METRIC_KEYS.keys()))
        self._metric_combo.currentIndexChanged.connect(self._refresh)
        top.addWidget(self._metric_combo)
        top.addStretch()
        self._info_label = QLabel("")
        self._info_label.setStyleSheet(_S_DIM)
        top.addWidget(self._info_label)
        layout.addLayout(top)

        self._table = QTableWidget()
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setDefaultSectionSize(28)
        layout.addWidget(self._table)

        self._results: dict[str, dict] = {}

    def set_data(self, results: dict[str, dict]) -> None:
        self._results = results
        self._refresh()

    def clear(self) -> None:
        self._results = {}
        self._table.clear()
        self._table.setRowCount(0)
        self._table.setColumnCount(0)
        self._info_label.setText("")

    def _refresh(self) -> None:
        if not self._results:
            self._table.clear()
            self._table.setRowCount(0)
            self._table.setColumnCount(0)
            return

        metric_label = self._metric_combo.currentText()
        metric_key   = _METRIC_KEYS.get(metric_label, "oos_oriented_rank_ic")

        regime_keys: list[str] = list(self._results.keys())
        factor_names: list[str] = []
        seen: set[str] = set()
        for res in self._results.values():
            for row in res.get("summary", []):
                name = row["factor"]
                if name not in seen:
                    factor_names.append(name)
                    seen.add(name)

        if not factor_names or not regime_keys:
            return

        # matrix[factor][col_idx] = (value, n_oos)
        matrix: dict[str, list[tuple[float, int]]] = {f: [] for f in factor_names}
        for rk in regime_keys:
            summary_map = {row["factor"]: row for row in self._results[rk].get("summary", [])}
            for f in factor_names:
                row = summary_map.get(f, {})
                val = row.get(metric_key, float("nan"))
                n   = row.get("oos_sample_count", 0)
                matrix[f].append((float(val) if val is not None else float("nan"), int(n)))

        self._table.setRowCount(len(factor_names) + 1)
        self._table.setColumnCount(len(regime_keys))
        self._table.setHorizontalHeaderLabels([label_display_name(k) for k in regime_keys])
        self._table.setVerticalHeaderLabels(list(factor_names) + ["n (OOS)"])

        for r, fname in enumerate(factor_names):
            for c, rk in enumerate(regime_keys):
                val, n = matrix[fname][c]
                text = f"{val:+.4f}" if np.isfinite(val) else "—"
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item.setToolTip(
                    f"Factor: {fname}\nRegime: {label_display_name(rk)}\n"
                    f"{metric_label}: {text}\nn(OOS): {n:,}"
                )
                item.setBackground(QColor(_ic_bg_color(val, metric_key)))
                item.setForeground(QColor(_ic_text_color(val, metric_key)))
                self._table.setItem(r, c, item)

        n_row = len(factor_names)
        for c, rk in enumerate(regime_keys):
            n_max = max((matrix[f][c][1] for f in factor_names), default=0)
            item = QTableWidgetItem(f"{n_max:,}")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setForeground(QColor("#787b86"))
            self._table.setItem(n_row, c, item)

        self._table.resizeColumnsToContents()
        self._info_label.setText(
            f"{len(regime_keys)} regimes × {len(factor_names)} factors = "
            f"{len(regime_keys) * len(factor_names)} cells"
        )


# ── IC Visualization helpers ──────────────────────────────────────────────────

def _monthly_ic_bg(val: float) -> str:
    if not np.isfinite(val): return "#1e222d"
    if val >= 0.06:  return "#0d3b2e"
    if val >= 0.04:  return "#163029"
    if val >= 0.02:  return "#152623"
    if val > 0:      return "#141f1d"
    if val <= -0.06: return "#3b0d0d"
    if val <= -0.04: return "#2e1616"
    if val <= -0.02: return "#241515"
    if val < 0:      return "#1d1414"
    return "#1e222d"


def _monthly_ic_fg(val: float) -> str:
    if not np.isfinite(val): return "#4a4e5a"
    a = abs(val)
    if a >= 0.04: return "#d1d4dc"
    if a >= 0.02: return "#9598a1"
    return "#6b6e78"


def _corr_bg(val: float, diagonal: bool = False) -> str:
    if diagonal: return "#2a2d3a"
    if not np.isfinite(val): return "#1e222d"
    a = abs(val)
    if val > 0:
        if a >= 0.7: return "#0d3b2e"
        if a >= 0.5: return "#163029"
        if a >= 0.3: return "#152623"
        if a >= 0.1: return "#141f1d"
    else:
        if a >= 0.7: return "#3b0d0d"
        if a >= 0.5: return "#2e1616"
        if a >= 0.3: return "#241515"
        if a >= 0.1: return "#1d1414"
    return "#1e222d"


def _corr_fg(val: float) -> str:
    if not np.isfinite(val): return "#4a4e5a"
    a = abs(val)
    if a >= 0.5: return "#d1d4dc"
    if a >= 0.3: return "#9598a1"
    return "#6b6e78"


# ── Quantile Returns Chart ─────────────────────────────────────────────────────

class QuantileReturnsChart(QWidget):
    """Bar chart: mean return by quantile bucket, IS/OOS selectable."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)
        self._factor_combo  = QComboBox()
        self._horizon_combo = QComboBox()
        self._sample_combo  = QComboBox()
        self._sample_combo.addItems(["out_of_sample", "in_sample"])
        for lbl, w in [("Factor:", self._factor_combo), ("Horizon:", self._horizon_combo), ("Sample:", self._sample_combo)]:
            ctrl.addWidget(QLabel(lbl))
            ctrl.addWidget(w)
        ctrl.addStretch()
        self._spread_lbl = QLabel("")
        self._spread_lbl.setStyleSheet(_S_DIM)
        ctrl.addWidget(self._spread_lbl)
        layout.addLayout(ctrl)

        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground("#131722")
        layout.addWidget(self._glw)

        self._plot = self._glw.addPlot()
        self._plot.getAxis("left").setTextPen("#d1d4dc")
        self._plot.getAxis("bottom").setTextPen("#787b86")
        self._plot.showGrid(x=False, y=True, alpha=0.15)
        self._plot.setLabel("left", "Mean Return (%)", color="#d1d4dc")
        self._plot.setLabel("bottom", "Quantile", color="#787b86")

        self._data: dict[tuple, list[dict]] = {}
        self._factor_combo.currentTextChanged.connect(self._refresh)
        self._horizon_combo.currentTextChanged.connect(self._refresh)
        self._sample_combo.currentTextChanged.connect(self._refresh)

    def set_data(self, quantile_rows: list[dict]) -> None:
        self._data = {}
        factors: list[str]  = []
        horizons: list[str] = []
        for row in quantile_rows:
            k = (row["factor"], int(row["horizon"]), row["sample"])
            self._data.setdefault(k, []).append(row)
            if row["factor"] not in factors:
                factors.append(row["factor"])
            h = str(row["horizon"])
            if h not in horizons:
                horizons.append(h)
        for combo, items in [(self._factor_combo, factors), (self._horizon_combo, sorted(horizons, key=int))]:
            prev = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(items)
            if prev in items:
                combo.setCurrentText(prev)
            combo.blockSignals(False)
        self._refresh()

    def _refresh(self) -> None:
        self._plot.clear()
        self._spread_lbl.setText("")
        factor      = self._factor_combo.currentText()
        horizon_str = self._horizon_combo.currentText()
        sample      = self._sample_combo.currentText()
        if not factor or not horizon_str:
            return
        rows = self._data.get((factor, int(horizon_str), sample), [])
        if not rows:
            return
        rows = sorted(rows, key=lambda r: r["quantile"])
        n_q = len(rows)
        x = np.arange(1, n_q + 1, dtype=float)
        y = np.array([r["mean_return"] * 100.0 for r in rows])  # percent

        for xi, yi in zip(x, y):
            brush = "#26a69a" if yi >= 0 else "#ef5350"
            self._plot.addItem(
                pg.BarGraphItem(x=[xi], height=[yi], width=0.65,
                                brush=brush, pen=pg.mkPen("#0d1117", width=1))
            )
        self._plot.addItem(
            pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen("#4a4e5a", width=1)),
            ignoreBounds=True,
        )
        self._plot.getAxis("bottom").setTicks([[(int(xi), f"Q{int(xi)}") for xi in x]])

        spread_pct = (y[-1] - y[0]) if n_q >= 2 else 0.0
        wr_parts = [
            f"Q{r['quantile']}:{r.get('win_rate', float('nan')):.0f}%"
            for r in rows if np.isfinite(r.get("win_rate", float("nan")))
        ]
        self._spread_lbl.setText(
            f"spread={spread_pct:+.3f}%  |  " + "  ".join(wr_parts)
        )


# ── Monthly IC Heatmap ─────────────────────────────────────────────────────────

class MonthlyICHeatmap(QWidget):
    """Factor × Month Rank IC heat table."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)
        self._horizon_combo = QComboBox()
        self._metric_combo  = QComboBox()
        self._metric_combo.addItems(["rank_ic", "ic"])
        ctrl.addWidget(QLabel("Horizon:")); ctrl.addWidget(self._horizon_combo)
        ctrl.addWidget(QLabel("Metric:"));  ctrl.addWidget(self._metric_combo)
        ctrl.addStretch()
        self._info_lbl = QLabel("")
        self._info_lbl.setStyleSheet(_S_DIM)
        ctrl.addWidget(self._info_lbl)
        layout.addLayout(ctrl)

        self._table = QTableWidget()
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setDefaultSectionSize(24)
        layout.addWidget(self._table)

        self._raw: list[dict] = []
        self._horizon_combo.currentTextChanged.connect(self._refresh)
        self._metric_combo.currentTextChanged.connect(self._refresh)

    def set_data(self, stability_monthly: list[dict]) -> None:
        self._raw = stability_monthly
        horizons: list[str] = []
        for row in stability_monthly:
            h = str(row["horizon"])
            if h not in horizons:
                horizons.append(h)
        prev_h = self._horizon_combo.currentText()
        self._horizon_combo.blockSignals(True)
        self._horizon_combo.clear()
        self._horizon_combo.addItems(sorted(horizons, key=int))
        if prev_h in horizons:
            self._horizon_combo.setCurrentText(prev_h)
        self._horizon_combo.blockSignals(False)
        self._refresh()

    def _refresh(self) -> None:
        self._table.clear()
        self._table.setRowCount(0)
        self._table.setColumnCount(0)
        self._info_lbl.setText("")
        if not self._raw:
            return
        horizon_str = self._horizon_combo.currentText()
        metric      = self._metric_combo.currentText()
        if not horizon_str:
            return
        rows = [r for r in self._raw if int(r["horizon"]) == int(horizon_str)]
        if not rows:
            return

        factors: list[str] = []
        periods: list[str] = []
        seen_f: set = set()
        seen_p: set = set()
        for r in rows:
            if r["factor"] not in seen_f:
                factors.append(r["factor"])
                seen_f.add(r["factor"])
            if r["period"] not in seen_p:
                periods.append(r["period"])
                seen_p.add(r["period"])
        periods = sorted(periods)
        data_map: dict[tuple, dict] = {(r["factor"], r["period"]): r for r in rows}

        self._table.setRowCount(len(factors))
        self._table.setColumnCount(len(periods))
        self._table.setHorizontalHeaderLabels(periods)
        self._table.setVerticalHeaderLabels(factors)

        for ri, f in enumerate(factors):
            for ci, p in enumerate(periods):
                r = data_map.get((f, p))
                if r is None:
                    item = QTableWidgetItem("—")
                    item.setForeground(QColor("#4a4e5a"))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self._table.setItem(ri, ci, item)
                    continue
                raw_val = r.get(metric, float("nan"))
                try:
                    val = float(raw_val) if raw_val is not None else float("nan")
                except (TypeError, ValueError):
                    val = float("nan")
                text = f"{val:+.3f}" if np.isfinite(val) else "—"
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item.setBackground(QColor(_monthly_ic_bg(val)))
                item.setForeground(QColor(_monthly_ic_fg(val)))
                item.setToolTip(
                    f"Factor: {f}\nPeriod: {p}\n{metric}: {text}\n"
                    f"split: {r.get('split', '')}\nn: {r.get('sample_count', 0)}"
                )
                self._table.setItem(ri, ci, item)

        self._table.resizeColumnsToContents()
        self._info_lbl.setText(f"{len(factors)} factors × {len(periods)} months")


# ── Correlation Heatmap ────────────────────────────────────────────────────────

class CorrelationHeatmap(QWidget):
    """Factor × Factor pairwise correlation heat table."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)
        self._metric_combo = QComboBox()
        self._metric_combo.addItems(["spearman", "pearson"])
        self._window_combo = QComboBox()
        self._window_combo.addItems(["full", "IS (train)", "OOS (test)"])
        ctrl.addWidget(QLabel("Metric:")); ctrl.addWidget(self._metric_combo)
        ctrl.addWidget(QLabel("Window:")); ctrl.addWidget(self._window_combo)
        ctrl.addStretch()
        layout.addLayout(ctrl)

        self._table = QTableWidget()
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setDefaultSectionSize(28)
        layout.addWidget(self._table)

        self._raw: list[dict] = []
        self._metric_combo.currentTextChanged.connect(self._refresh)
        self._window_combo.currentTextChanged.connect(self._refresh)

    def set_data(self, correlations: list[dict]) -> None:
        self._raw = correlations
        self._refresh()

    def _refresh(self) -> None:
        self._table.clear()
        self._table.setRowCount(0)
        self._table.setColumnCount(0)
        if not self._raw:
            return
        metric = self._metric_combo.currentText()
        window = self._window_combo.currentText()
        _KEY: dict[tuple, str] = {
            ("spearman", "full"):       "spearman",
            ("spearman", "IS (train)"): "spearman_is",
            ("spearman", "OOS (test)"): "spearman_oos",
            ("pearson",  "full"):       "pearson",
            ("pearson",  "IS (train)"): "pearson_is",
            ("pearson",  "OOS (test)"): "pearson_oos",
        }
        key = _KEY.get((metric, window), "spearman")

        names: list[str] = []
        seen: set = set()
        for r in self._raw:
            for f in (r["factor_a"], r["factor_b"]):
                if f not in seen:
                    names.append(f)
                    seen.add(f)
        n = len(names)
        idx = {f: i for i, f in enumerate(names)}
        mat = np.full((n, n), float("nan"))
        np.fill_diagonal(mat, 1.0)
        for r in self._raw:
            i, j = idx[r["factor_a"]], idx[r["factor_b"]]
            try:
                v = float(r.get(key, float("nan")))
            except (TypeError, ValueError):
                v = float("nan")
            mat[i, j] = mat[j, i] = v

        self._table.setRowCount(n)
        self._table.setColumnCount(n)
        self._table.setHorizontalHeaderLabels(names)
        self._table.setVerticalHeaderLabels(names)
        for ri in range(n):
            for ci in range(n):
                val = mat[ri, ci]
                text = f"{val:+.3f}" if np.isfinite(val) else "—"
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item.setBackground(QColor(_corr_bg(val, ri == ci)))
                item.setForeground(QColor(_corr_fg(val)))
                item.setToolTip(f"{names[ri]} vs {names[ci]}\n{metric} ({window}): {text}")
                self._table.setItem(ri, ci, item)
        self._table.resizeColumnsToContents()


# ── IC Visualization Panel ─────────────────────────────────────────────────────

class ICVisualizationPanel(QWidget):
    """Visualization panel: quantile chart + monthly IC heatmap + correlation matrix."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        inner = QTabWidget()
        self._quantile_chart  = QuantileReturnsChart()
        self._monthly_heatmap = MonthlyICHeatmap()
        self._corr_heatmap    = CorrelationHeatmap()
        inner.addTab(self._quantile_chart,  "Quantile Returns")
        inner.addTab(self._monthly_heatmap, "Monthly IC Heatmap")
        inner.addTab(self._corr_heatmap,    "Correlation Matrix")
        layout.addWidget(inner)

    def set_data(self, result: dict) -> None:
        self._quantile_chart.set_data(result.get("quantiles", []))
        self._monthly_heatmap.set_data(result.get("stability_monthly", []))
        self._corr_heatmap.set_data(result.get("factor_correlations", []))


# ── Worker Thread ─────────────────────────────────────────────────────────────

class ResearchWorkerThread(QThread):
    result_ready        = pyqtSignal(dict)
    matrix_result_ready = pyqtSignal(dict)
    error               = pyqtSignal(str)

    def __init__(self, config: ResearchConfig, parent=None) -> None:
        super().__init__(parent)
        self._config = config

    def run(self) -> None:
        try:
            rf = self._config.regime_filter
            if rf is not None and rf.is_active() and rf.mode in ("matrix", "cross_matrix"):
                results = run_research_with_regimes(self._config)
                self.matrix_result_ready.emit({k: v.to_dict() for k, v in results.items()})
            else:
                self.result_ready.emit(run_research(self._config).to_dict())
        except Exception as exc:
            self.error.emit(str(exc))


# ── Main Research Lab Widget ──────────────────────────────────────────────────

class ResearchLab(QWidget):
    """Vectorized factor IC and quantile research UI."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        ensure_builtin_factors()
        self._saved = ui_settings.get("research_lab_config", {})

        # ── State (persisted between dialog opens) ────────────────────────────
        self._selected_factors_list: list[str] = list(
            self._saved.get("factors") or list_factors(include_tick=True)
        )
        self._factor_side_filter_val: str  = self._saved.get("factor_side_filter", "")
        self._factor_group_filter_val: str = self._saved.get("factor_group_filter", "")
        self._horizons_str:   str   = self._saved.get("horizons",    "1,3,6,12")
        self._quantiles_val:  int   = int(self._saved.get("quantiles",  5))
        self._entry_lag_val:  int   = int(self._saved.get("entry_lag",  1))
        self._train_ratio_val: float = float(self._saved.get("train_ratio", 0.5))
        regime_data = self._saved.get("regime_filter")
        self._regime_config: RegimeFilterConfig | None = (
            RegimeFilterConfig.from_dict(regime_data) if regime_data else None
        )

        # ── Persistent hidden TimeSliceWidget (not in any layout) ─────────────
        self._time_slice = TimeSliceWidget()

        self._worker: Optional[ResearchWorkerThread] = None
        self._last_result: dict | None = None
        self._last_matrix_result: dict | None = None
        self._restore_done = False

        self._setup_ui()

        # Load symbol and restore months after symbol combo is created
        self._time_slice.load_symbol(self._symbol_combo.currentText())
        self._time_slice.set_selected_months(self._saved.get("selected_months", []))

        self._update_all_statuses()
        self._restore_done = True

    # ── UI Setup ──────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setObjectName("ResearchLab")
        self.setStyleSheet(
            """
            QWidget#ResearchLab {
                background: #101621;
                color: #d1d4dc;
            }
            QGroupBox {
                background: #151c2a;
                border: 1px solid #263245;
                border-radius: 8px;
                margin-top: 18px;
                padding: 10px 10px 8px 10px;
                color: #d1d4dc;
                font-size: 12px;
                font-weight: 700;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 10px;
                padding: 2px 8px;
                color: #8fe7d8;
                background: #101621;
                border: 1px solid #263245;
                border-radius: 5px;
            }
            QPushButton {
                background: #20283a;
                color: #dce3ee;
                border: 1px solid #334058;
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 12px;
            }
            QPushButton:hover {
                background: #27324a;
                border-color: #4a6484;
            }
            QPushButton:pressed {
                background: #162235;
            }
            QPushButton:disabled {
                color: #5f6878;
                background: #161c29;
                border-color: #252d3d;
            }
            QPushButton#primaryRunButton {
                background: #1f6f66;
                border-color: #26a69a;
                color: #f3fffd;
                font-weight: 700;
            }
            QPushButton#primaryRunButton:hover {
                background: #258477;
            }
            QComboBox {
                background: #101621;
                color: #dce3ee;
                border: 1px solid #334058;
                border-radius: 6px;
                padding: 5px 8px;
                min-height: 22px;
            }
            QTabWidget::pane {
                border: 1px solid #263245;
                background: #131a27;
            }
            QTabBar::tab {
                background: #151c2a;
                color: #8f96a8;
                border: 1px solid #263245;
                border-bottom: 0;
                padding: 7px 13px;
            }
            QTabBar::tab:selected {
                background: #20283a;
                color: #f2f5f9;
                border-top: 2px solid #26a69a;
            }
            QTableWidget {
                background: #101621;
                alternate-background-color: #141b29;
                color: #d1d4dc;
                gridline-color: #263245;
                border: 1px solid #263245;
                selection-background-color: #23423f;
                selection-color: #f2f5f9;
            }
            QHeaderView::section {
                background: #182132;
                color: #aab3c2;
                border: 1px solid #263245;
                padding: 5px;
                font-weight: 700;
            }
            """
        )
        root = QSplitter(Qt.Orientation.Horizontal)
        root.setHandleWidth(4)

        controls = QWidget()
        controls.setFixedWidth(340)
        cl = QVBoxLayout(controls)
        cl.setContentsMargins(8, 8, 8, 8)
        cl.setSpacing(6)

        # ── Research Dataset ──────────────────────────────────────────────────
        data_box = QGroupBox("Research Dataset")
        dl = QVBoxLayout(data_box)
        self._symbol_combo = QComboBox()
        self._symbol_combo.addItems(cfg_base.SYMBOLS)
        if self._saved.get("symbol") in cfg_base.SYMBOLS:
            self._symbol_combo.setCurrentText(self._saved["symbol"])
        self._interval_combo = QComboBox()
        self._interval_combo.addItems(cfg_base.INTERVALS)
        self._interval_combo.setCurrentText(self._saved.get("interval", "1m"))
        self._tick_check = QCheckBox("Use tick-derived factors when available")
        self._tick_check.setChecked(self._saved.get("use_tick_features", True))
        dl.addWidget(_field_label("Symbol"))
        dl.addWidget(self._symbol_combo)
        dl.addWidget(_field_label("Interval"))
        dl.addWidget(self._interval_combo)
        dl.addWidget(self._tick_check)
        cl.addWidget(data_box)

        # ── Analysis Configuration (4 button rows) ────────────────────────────
        cfg_box = QGroupBox("Analysis Configuration")
        cfg_l = QVBoxLayout(cfg_box)
        cfg_l.setSpacing(5)

        def _row(label: str) -> tuple[QPushButton, QLabel, QHBoxLayout]:
            row = QHBoxLayout()
            row.setSpacing(6)
            btn = QPushButton(label)
            btn.setFixedWidth(118)
            lbl = QLabel("—")
            lbl.setMinimumHeight(28)
            lbl.setStyleSheet(_S_STATUS_DIM)
            row.addWidget(btn)
            row.addWidget(lbl, stretch=1)
            return btn, lbl, row

        self._ts_btn,    self._ts_status,    ts_row    = _row("Time Slice...")
        self._fac_btn,   self._fac_status,   fac_row   = _row("Factors...")
        self._param_btn, self._param_status, param_row = _row("Parameters...")
        self._regime_btn, self._regime_status, reg_row = _row("Regime...")
        self._regime_btn.setToolTip("設定 Regime 條件過濾 / 矩陣分析")

        for row in (ts_row, fac_row, param_row, reg_row):
            cfg_l.addLayout(row)

        self._ts_btn.clicked.connect(self._on_ts_btn_clicked)
        self._fac_btn.clicked.connect(self._on_fac_btn_clicked)
        self._param_btn.clicked.connect(self._on_param_btn_clicked)
        self._regime_btn.clicked.connect(self._on_regime_btn_clicked)

        cl.addWidget(cfg_box, stretch=1)

        # ── Action buttons ────────────────────────────────────────────────────
        btn_row_layout = QHBoxLayout()
        self._run_btn    = QPushButton("Run Research")
        self._export_btn = QPushButton("Export")
        self._import_btn = QPushButton("Import")
        self._run_btn.setObjectName("primaryRunButton")
        self._export_btn.setEnabled(False)
        self._run_btn.clicked.connect(self._on_run)
        self._export_btn.clicked.connect(self._on_export)
        self._import_btn.clicked.connect(self._on_import)
        btn_row_layout.addWidget(self._run_btn)
        btn_row_layout.addWidget(self._export_btn)
        btn_row_layout.addWidget(self._import_btn)
        cl.addLayout(btn_row_layout)

        self._status = QLabel("")
        self._status.setStyleSheet(_S_DIM)
        cl.addWidget(self._status)

        root.addWidget(controls)

        # ── Result Tabs ───────────────────────────────────────────────────────
        self._tabs = QTabWidget()
        self._regime_matrix    = RegimeMatrixWidget()
        self._summary_table    = QTableWidget()
        self._ortho_table      = QTableWidget()
        self._ts_chart         = FactorPerformanceChart()
        self._viz_panel        = ICVisualizationPanel()
        self._metrics_table    = QTableWidget()
        self._quantile_table   = QTableWidget()
        self._monthly_table    = QTableWidget()
        self._yearly_table     = QTableWidget()
        self._correlation_table = QTableWidget()
        self._unavailable_table = QTableWidget()
        self._tabs.addTab(self._regime_matrix,     "Regime Matrix")
        self._tabs.addTab(self._summary_table,     "Factor Ranking")
        self._tabs.addTab(self._ortho_table,       "Orthogonal Ranking")
        self._tabs.addTab(self._ts_chart,          "IC Time Series")
        self._tabs.addTab(self._viz_panel,         "Visualization")
        self._tabs.addTab(self._metrics_table,     "IC by Horizon")
        self._tabs.addTab(self._quantile_table,    "Quantiles")
        self._tabs.addTab(self._monthly_table,     "Monthly Stability")
        self._tabs.addTab(self._yearly_table,      "Yearly Stability")
        self._tabs.addTab(self._correlation_table, "Factor Correlations")
        self._tabs.addTab(self._unavailable_table, "Unavailable")
        self._regime_matrix_tab_idx = 0

        root.addWidget(self._tabs)
        root.setStretchFactor(0, 0)
        root.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(root)

        self._symbol_combo.currentTextChanged.connect(self._on_symbol_changed)
        self._connect_persistence()

    # ── Config dialog handlers ────────────────────────────────────────────────

    def _on_ts_btn_clicked(self) -> None:
        dlg = TimeSliceDialog(
            self,
            symbol=self._symbol_combo.currentText(),
            selected_months=self._time_slice.selected_months(),
        )
        if dlg.exec() == TimeSliceDialog.DialogCode.Accepted:
            months = dlg.get_selected_months()
            self._time_slice.load_symbol(self._symbol_combo.currentText())
            self._time_slice.set_selected_months(months)
            self._update_ts_status()
            self._save_config()

    def _on_fac_btn_clicked(self) -> None:
        dlg = FactorsDialog(
            self,
            selected_factors=self._selected_factors_list,
            side_filter=self._factor_side_filter_val,
            group_filter=self._factor_group_filter_val,
        )
        if dlg.exec() == FactorsDialog.DialogCode.Accepted:
            self._selected_factors_list   = dlg.get_selected_factors()
            self._factor_side_filter_val  = dlg.get_side_filter()
            self._factor_group_filter_val = dlg.get_group_filter()
            self._update_fac_status()
            self._save_config()

    def _on_param_btn_clicked(self) -> None:
        dlg = ParametersDialog(
            self,
            horizons=self._horizons_str,
            quantiles=self._quantiles_val,
            entry_lag=self._entry_lag_val,
            train_ratio=self._train_ratio_val,
        )
        if dlg.exec() == ParametersDialog.DialogCode.Accepted:
            self._horizons_str    = dlg.get_horizons_str()
            self._quantiles_val   = dlg.get_quantiles()
            self._entry_lag_val   = dlg.get_entry_lag()
            self._train_ratio_val = dlg.get_train_ratio()
            self._update_param_status()
            self._save_config()

    def _on_regime_btn_clicked(self) -> None:
        dlg = RegimeFilterDialog(self, self._regime_config)
        if dlg.exec() == RegimeFilterDialog.DialogCode.Accepted:
            self._regime_config = dlg.get_config()
            self._update_regime_status()
            self._save_config()

    # ── Status label updaters ─────────────────────────────────────────────────

    def _update_ts_status(self) -> None:
        months = self._time_slice.selected_months()
        if not months:
            self._ts_status.setText("未設定")
            self._ts_status.setStyleSheet(_S_STATUS_DIM)
        else:
            n = len(months)
            preview = ", ".join(sorted(months)[:2])
            if n > 2:
                preview += f" …(+{n - 2})"
            self._ts_status.setText(f"{preview} ({n}個月)")
            self._ts_status.setStyleSheet(_S_STATUS_INFO)

    def _update_fac_status(self) -> None:
        n_sel   = len(self._selected_factors_list)
        n_total = len(list_factors(include_tick=True))
        self._fac_status.setText(f"{n_sel} / {n_total} 個因子")
        self._fac_status.setStyleSheet(_S_STATUS_INFO if n_sel > 0 else _S_STATUS_DIM)

    def _update_param_status(self) -> None:
        self._param_status.setText(
            f"H:{self._horizons_str} | Q:{self._quantiles_val} | lag:{self._entry_lag_val}"
        )
        self._param_status.setStyleSheet(_S_STATUS_INFO)

    def _update_regime_status(self) -> None:
        rc = self._regime_config
        if rc is None or not rc.is_active():
            self._regime_status.setText("Inactive")
            self._regime_status.setStyleSheet(_S_STATUS_DIM)
        else:
            if rc.mode == "matrix":
                detail = f"{rc.active_label_count()} labels"
            elif rc.mode == "cross_matrix":
                detail = f"{rc.cross_combination_count()} combos"
            else:
                detail = f"{rc.active_label_count()} labels"
            mode_str = {"matrix": "Matrix", "cross_matrix": "Cross×", "filter": "Filter"}.get(rc.mode, rc.mode)
            self._regime_status.setText(f"● {mode_str} ({detail})")
            self._regime_status.setStyleSheet(_S_STATUS_OK)

    def _update_all_statuses(self) -> None:
        self._update_ts_status()
        self._update_fac_status()
        self._update_param_status()
        self._update_regime_status()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _connect_persistence(self) -> None:
        save = lambda *_: self._save_config()
        self._symbol_combo.currentTextChanged.connect(save)
        self._interval_combo.currentTextChanged.connect(save)
        self._tick_check.toggled.connect(save)

    def _on_symbol_changed(self, symbol: str) -> None:
        self._time_slice.load_symbol(symbol)
        self._update_ts_status()

    def _parse_horizons(self, text: str) -> list[int]:
        values: list[int] = []
        for raw in text.split(","):
            raw = raw.strip()
            if raw.isdigit():
                values.append(max(1, int(raw)))
        return sorted(set(values))

    def _build_config(self) -> ResearchConfig | None:
        slices = self._normalize_slices(self._time_slice.get_slices())
        if not slices:
            QMessageBox.warning(self, "No Time Slice",
                                "Please open Time Slice and select at least one period.")
            return None
        if not self._selected_factors_list:
            QMessageBox.warning(self, "No Factors",
                                "Please open Factors and select at least one factor.")
            return None
        horizons = self._parse_horizons(self._horizons_str)
        if not horizons:
            QMessageBox.warning(self, "Invalid Horizons",
                                "Please open Parameters and set at least one horizon.")
            return None
        return ResearchConfig(
            symbol=self._symbol_combo.currentText(),
            interval=self._interval_combo.currentText(),
            slices=slices,
            factor_names=self._selected_factors_list,
            horizons=horizons,
            quantiles=self._quantiles_val,
            use_tick_features=self._tick_check.isChecked(),
            entry_lag=self._entry_lag_val,
            train_ratio=self._train_ratio_val,
            regime_filter=self._regime_config,
        )

    def _normalize_slices(self, slices: list) -> list:
        normalized: list = []
        for item in slices:
            if isinstance(item, tuple):
                normalized.extend(item)
            else:
                normalized.append(item)
        return normalized

    # ── Run / Result ──────────────────────────────────────────────────────────

    def _on_run(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        config = self._build_config()
        if config is None:
            return
        self._save_config()
        self._run_btn.setEnabled(False)
        self._export_btn.setEnabled(False)
        rc = self._regime_config
        if rc is not None and rc.is_active() and rc.mode == "matrix":
            self._status.setText(f"Running Matrix IC ({rc.active_label_count()} regimes)...")
        elif rc is not None and rc.is_active() and rc.mode == "cross_matrix":
            self._status.setText(f"Running Cross Matrix IC ({rc.cross_combination_count()} combos)...")
        else:
            self._status.setText("Running vectorized research...")
        self._worker = ResearchWorkerThread(config, self)
        self._worker.result_ready.connect(self._on_result_ready)
        self._worker.matrix_result_ready.connect(self._on_matrix_result_ready)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(lambda: self._run_btn.setEnabled(True))
        self._worker.start()

    def _on_result_ready(self, result: dict) -> None:
        self._last_result = result
        self._fill_table(self._summary_table,     result.get("summary", []))
        self._fill_table(self._ortho_table,       result.get("orthogonal_summary", []))
        self._ts_chart.set_data(result.get("timeseries_ic", {}))
        self._viz_panel.set_data(result)
        self._fill_table(self._metrics_table,     result.get("metrics", []))
        self._fill_table(self._quantile_table,    result.get("quantiles", []))
        self._fill_table(self._monthly_table,     result.get("stability_monthly", []))
        self._fill_table(self._yearly_table,      result.get("stability_yearly", []))
        self._fill_table(self._correlation_table, result.get("factor_correlations", []))
        self._fill_table(self._unavailable_table, result.get("unavailable", []))
        self._export_btn.setEnabled(True)
        self._status.setText(
            f"Done | rows={result.get('rows', 0)} | factors={len(result.get('summary', []))}"
        )

    def _on_matrix_result_ready(self, results: dict) -> None:
        self._last_matrix_result = results
        self._regime_matrix.set_data(results)
        self._tabs.setCurrentIndex(self._regime_matrix_tab_idx)
        if results:
            first = next(iter(results.values()))
            self._last_result = first
            self._fill_table(self._summary_table,     first.get("summary", []))
            self._fill_table(self._ortho_table,       first.get("orthogonal_summary", []))
            self._ts_chart.set_data(first.get("timeseries_ic", {}))
            self._viz_panel.set_data(first)
            self._fill_table(self._metrics_table,     first.get("metrics", []))
            self._fill_table(self._unavailable_table, first.get("unavailable", []))
        self._export_btn.setEnabled(bool(self._last_result))
        total_rows = sum(r.get("rows", 0) for r in results.values())
        self._status.setText(
            f"Matrix Done | {len(results)} regimes | total rows={total_rows:,}"
        )

    def _on_error(self, msg: str) -> None:
        self._status.setText(f"Error: {msg}")
        QMessageBox.critical(self, "Research Error", msg)

    # ── Table helper ──────────────────────────────────────────────────────────

    def _fill_table(self, table: QTableWidget, rows: list[dict]) -> None:
        table.clear()
        if not rows:
            table.setRowCount(0)
            table.setColumnCount(0)
            return
        headers = list(rows[0].keys())
        table.setColumnCount(len(headers))
        table.setRowCount(len(rows))
        table.setHorizontalHeaderLabels(headers)
        for r, row in enumerate(rows):
            for c, key in enumerate(headers):
                val = row.get(key, "")
                text = f"{val:.6g}" if isinstance(val, float) else str(val)
                table.setItem(r, c, QTableWidgetItem(text))
        table.resizeColumnsToContents()

    # ── Export / Import ───────────────────────────────────────────────────────

    def _on_export(self) -> None:
        is_matrix = bool(self._last_matrix_result)
        if not is_matrix and not self._last_result:
            return
        symbol    = self._symbol_combo.currentText()
        interval  = self._interval_combo.currentText()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_dir  = Path("docs/reports/factor_analysis")
        base_dir.mkdir(parents=True, exist_ok=True)
        json_name = "matrix_result.json" if is_matrix else "full_result.json"
        suggested = base_dir / f"{symbol}_{interval}_{timestamp}" / json_name
        suggested.parent.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Research Package",
            str(suggested), "JSON Files (*.json)",
        )
        if not path:
            return
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)

        if is_matrix:
            # Full matrix: {regime_label: result_dict, …}
            target.write_text(
                json.dumps(self._last_matrix_result, indent=2), encoding="utf-8"
            )
            # Merged summary CSV — one row per (regime, factor) for easy comparison
            merged: list[dict] = []
            for regime_key, result_dict in self._last_matrix_result.items():
                for row in result_dict.get("summary", []):
                    merged.append({"regime": regime_key, **row})
            if merged:
                with (target.parent / "matrix_summary.csv").open(
                    "w", newline="", encoding="utf-8"
                ) as fh:
                    writer = csv.DictWriter(fh, fieldnames=list(merged[0].keys()))
                    writer.writeheader()
                    writer.writerows(merged)
        else:
            target.write_text(json.dumps(self._last_result, indent=2), encoding="utf-8")
            for key, rows in self._last_result.items():
                if key == "timeseries_ic":
                    continue
                if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
                    continue
                with (target.parent / f"{key}.csv").open("w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                    writer.writeheader()
                    writer.writerows(rows)

        self._status.setText(f"Package saved to {target.parent}")
        QMessageBox.information(self, "Export Successful",
                                f"Research package saved to:\n{target.parent}")

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Research Result",
            str(Path("docs/reports/factor_analysis")), "JSON Files (*.json)",
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not data:
                raise ValueError("Invalid file: empty or not a JSON object")

            # Detect format: matrix result has regime-label keys whose values are
            # result dicts; regular result has "summary"/"metrics" at top level.
            _RESULT_FIELDS = {"summary", "metrics", "quantiles", "rows"}
            is_matrix = (
                not _RESULT_FIELDS.intersection(data.keys())
                and all(isinstance(v, dict) and "summary" in v for v in data.values())
            )

            if is_matrix:
                self._on_matrix_result_ready(data)
                self._status.setText(f"Imported matrix: {Path(path).parent.name}")
                QMessageBox.information(self, "Import Successful",
                                        f"Matrix results imported from:\n{path}")
            else:
                if "summary" not in data or "metrics" not in data:
                    raise ValueError("Invalid file (missing summary or metrics)")
                self._on_result_ready(data)
                self._status.setText(f"Imported: {Path(path).parent.name}")
                QMessageBox.information(self, "Import Successful",
                                        f"Results imported from:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Import Error", str(exc))

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_config(self) -> None:
        if not self._restore_done:
            return
        ui_settings.set("research_lab_config", {
            "symbol":              self._symbol_combo.currentText(),
            "interval":            self._interval_combo.currentText(),
            "use_tick_features":   self._tick_check.isChecked(),
            "horizons":            self._horizons_str,
            "quantiles":           self._quantiles_val,
            "entry_lag":           self._entry_lag_val,
            "train_ratio":         self._train_ratio_val,
            "factors":             self._selected_factors_list,
            "factor_side_filter":  self._factor_side_filter_val,
            "factor_group_filter": self._factor_group_filter_val,
            "selected_months":     self._time_slice.selected_months(),
            "regime_filter":       self._regime_config.to_dict() if self._regime_config else None,
        })
