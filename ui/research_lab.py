from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt

from config import base as cfg_base
from research.base import FACTOR_GROUPS, FACTOR_SIDE_LABELS, FACTOR_SIDES, factor_sides_label
from research.registry import ensure_builtin_factors, get_factor, list_factors
from research.runner import ResearchConfig, run_research
from ui.time_slice_widget import TimeSliceWidget
from utils.ui_settings import ui_settings


import pyqtgraph as pg
import numpy as np
from datetime import datetime, timezone

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
        
        # Crosshair
        self._v_line = pg.InfiniteLine(angle=90, movable=False, pen="#787b86")
        self._h_line = pg.InfiniteLine(angle=0, movable=False, pen="#787b86")
        self._plot.addItem(self._v_line, ignoreBounds=True)
        self._plot.addItem(self._h_line, ignoreBounds=True)
        
        self._proxy = pg.SignalProxy(self._plot.scene().sigMouseMoved, rateLimit=60, slot=self._mouse_moved)
        self._lines = {}
        self._data = {} # {name: (x, y)}
        self._timestamps = []
        
        self._hover_label = pg.TextItem(anchor=(0, 0), color="#d1d4dc")
        self._plot.addItem(self._hover_label, ignoreBounds=True)

    def set_data(self, ts_data: dict) -> None:
        self.clear()
        if not ts_data:
            return
            
        self._timestamps = ts_data.get("timestamps", [])
        if not self._timestamps:
            return
            
        x = np.array(self._timestamps) / 1000.0 # Convert to seconds for Axis
        self._plot.setAxisItems({'bottom': pg.DateAxisItem()})
        
        colors = ["#26a69a", "#ef5350", "#2196f3", "#ff9800", "#9c27b0", "#e91e63", "#4caf50", "#ffeb3b"]
        
        factors = ts_data.get("factors", {})
        for i, (name, y_list) in enumerate(factors.items()):
            if not y_list:
                continue
            y = np.array(y_list)
            color = colors[i % len(colors)]
            line = self._plot.plot(x, y, pen=pg.mkPen(color, width=1.5), name=name)
            self._lines[name] = line
            self._data[name] = (x, y)

    def _mouse_moved(self, evt):
        pos = evt[0]
        if self._plot.sceneBoundingRect().contains(pos):
            mouse_point = self._plot.vb.mapSceneToView(pos)
            self._v_line.setPos(mouse_point.x())
            self._h_line.setPos(mouse_point.y())
            
            # Find nearest timestamp
            if not self._timestamps:
                return
            
            x_val = mouse_point.x()
            vx_arr = np.array(self._timestamps)/1000.0
            idx = np.searchsorted(vx_arr, x_val)
            if idx >= len(self._timestamps):
                idx = len(self._timestamps) - 1
            
            ts = self._timestamps[idx]
            dt_str = datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            
            text = f"Time: {dt_str}\n"
            for name, (vx, vy) in self._data.items():
                if idx < len(vy):
                    val = vy[idx]
                    text += f"{name}: {val:.4f}\n"
            
            self._hover_label.setHtml(f'<div style="background-color: #1e222d; padding: 5px; border: 1px solid #363c4e;">{text}</div>')
            self._hover_label.setPos(mouse_point.x(), mouse_point.y())

    def clear(self) -> None:
        for line in self._lines.values():
            self._plot.removeItem(line)
        self._lines = {}
        self._data = {}
        self._timestamps = []

class ResearchWorkerThread(QThread):
    result_ready = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, config: ResearchConfig, parent=None) -> None:
        super().__init__(parent)
        self._config = config

    def run(self) -> None:
        try:
            self.result_ready.emit(run_research(self._config).to_dict())
        except Exception as exc:
            self.error.emit(str(exc))


class ResearchLab(QWidget):
    """Vectorized factor IC and quantile research UI."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        ensure_builtin_factors()
        self._saved = ui_settings.get("research_lab_config", {})
        self._worker: Optional[ResearchWorkerThread] = None
        self._last_result: dict | None = None
        self._restore_done = False
        self._setup_ui()
        self._restore_done = True

    def _setup_ui(self) -> None:
        root = QSplitter(Qt.Orientation.Horizontal)
        root.setHandleWidth(4)

        controls = QWidget()
        controls.setFixedWidth(340)
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(8, 8, 8, 8)
        controls_layout.setSpacing(6)

        data_box = QGroupBox("Research Dataset")
        data_layout = QVBoxLayout(data_box)
        self._symbol_combo = QComboBox()
        self._symbol_combo.addItems(cfg_base.SYMBOLS)
        if self._saved.get("symbol") in cfg_base.SYMBOLS:
            self._symbol_combo.setCurrentText(self._saved["symbol"])
        self._interval_combo = QComboBox()
        self._interval_combo.addItems(cfg_base.INTERVALS)
        self._interval_combo.setCurrentText(self._saved.get("interval", "1m"))
        self._tick_check = QCheckBox("Use tick-derived factors when available")
        self._tick_check.setChecked(self._saved.get("use_tick_features", True))
        data_layout.addWidget(QLabel("Symbol"))
        data_layout.addWidget(self._symbol_combo)
        data_layout.addWidget(QLabel("Interval"))
        data_layout.addWidget(self._interval_combo)
        data_layout.addWidget(self._tick_check)
        controls_layout.addWidget(data_box)

        slice_box = QGroupBox("Time Slice")
        slice_layout = QVBoxLayout(slice_box)
        self._time_slice = TimeSliceWidget()
        slice_layout.addWidget(self._time_slice)
        controls_layout.addWidget(slice_box, stretch=1)

        factor_box = QGroupBox("Factors")
        factor_layout = QVBoxLayout(factor_box)
        self._factor_side_filter = QComboBox()
        self._factor_side_filter.addItem("All Directions", "")
        for side in FACTOR_SIDES:
            self._factor_side_filter.addItem(FACTOR_SIDE_LABELS[side], side)
        side_idx = self._factor_side_filter.findData(self._saved.get("factor_side_filter", ""))
        self._factor_side_filter.setCurrentIndex(max(0, side_idx))

        self._factor_group_filter = QComboBox()
        self._factor_group_filter.addItem("All Groups", "")
        for group in FACTOR_GROUPS:
            self._factor_group_filter.addItem(group, group)
        group_idx = self._factor_group_filter.findData(self._saved.get("factor_group_filter", ""))
        self._factor_group_filter.setCurrentIndex(max(0, group_idx))

        factor_layout.addWidget(QLabel("Side"))
        factor_layout.addWidget(self._factor_side_filter)
        factor_layout.addWidget(QLabel("Group"))
        factor_layout.addWidget(self._factor_group_filter)

        factor_button_row = QHBoxLayout()
        self._check_visible_btn = QPushButton("Check Visible")
        self._clear_visible_btn = QPushButton("Clear Visible")
        self._check_visible_btn.clicked.connect(lambda: self._set_visible_factor_checks(Qt.CheckState.Checked))
        self._clear_visible_btn.clicked.connect(lambda: self._set_visible_factor_checks(Qt.CheckState.Unchecked))
        factor_button_row.addWidget(self._check_visible_btn)
        factor_button_row.addWidget(self._clear_visible_btn)
        factor_layout.addLayout(factor_button_row)

        self._factor_list = QListWidget()
        self._factor_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._load_factor_list()
        factor_layout.addWidget(self._factor_list)
        self._apply_factor_filters()
        controls_layout.addWidget(factor_box, stretch=1)

        param_box = QGroupBox("Research Parameters")
        param_layout = QVBoxLayout(param_box)
        self._horizon_edit = QComboBox()
        self._horizon_edit.setEditable(True)
        self._horizon_edit.addItems(["1,3,6,12", "1,2,3,5", "3,6,12,24"])
        self._horizon_edit.setCurrentText(self._saved.get("horizons", "1,3,6,12"))
        self._quantile_spin = QSpinBox()
        self._quantile_spin.setRange(2, 10)
        self._quantile_spin.setValue(int(self._saved.get("quantiles", 5)))
        param_layout.addWidget(QLabel("Forward Horizons (bars)"))
        param_layout.addWidget(self._horizon_edit)
        param_layout.addWidget(QLabel("Quantiles"))
        param_layout.addWidget(self._quantile_spin)
        controls_layout.addWidget(param_box)

        button_row = QHBoxLayout()
        self._run_btn = QPushButton("Run Research")
        self._export_btn = QPushButton("Export Package")
        self._import_btn = QPushButton("Import")
        self._export_btn.setEnabled(False)
        self._run_btn.clicked.connect(self._on_run)
        self._export_btn.clicked.connect(self._on_export)
        self._import_btn.clicked.connect(self._on_import)
        button_row.addWidget(self._run_btn)
        button_row.addWidget(self._export_btn)
        button_row.addWidget(self._import_btn)
        controls_layout.addLayout(button_row)

        self._status = QLabel("")
        self._status.setStyleSheet("color: #787b86; font-size: 11px;")
        controls_layout.addWidget(self._status)

        root.addWidget(controls)

        self._tabs = QTabWidget()
        self._summary_table = QTableWidget()
        self._ortho_table = QTableWidget()
        self._ts_chart = FactorPerformanceChart()
        self._metrics_table = QTableWidget()
        self._quantile_table = QTableWidget()
        self._monthly_table = QTableWidget()
        self._yearly_table = QTableWidget()
        self._correlation_table = QTableWidget()
        self._unavailable_table = QTableWidget()
        self._tabs.addTab(self._summary_table, "Factor Ranking")
        self._tabs.addTab(self._ortho_table, "Orthogonal Ranking")
        self._tabs.addTab(self._ts_chart, "IC Time Series")
        self._tabs.addTab(self._metrics_table, "IC by Horizon")
        self._tabs.addTab(self._quantile_table, "Quantiles")
        self._tabs.addTab(self._monthly_table, "Monthly Stability")
        self._tabs.addTab(self._yearly_table, "Yearly Stability")
        self._tabs.addTab(self._correlation_table, "Factor Correlations")
        self._tabs.addTab(self._unavailable_table, "Unavailable")
        root.addWidget(self._tabs)
        root.setStretchFactor(0, 0)
        root.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(root)

        self._symbol_combo.currentTextChanged.connect(self._on_symbol_changed)
        self._time_slice.load_symbol(self._symbol_combo.currentText())
        self._time_slice.set_selected_months(self._saved.get("selected_months", []))
        self._connect_persistence()

    def _load_factor_list(self) -> None:
        selected = set(self._saved.get("factors", []))
        if not selected:
            selected = set(list_factors(include_tick=True))
        group_order = {group: idx for idx, group in enumerate(FACTOR_GROUPS)}

        def sort_key(name: str) -> tuple[int, str]:
            factor = get_factor(name)
            group_idx = group_order.get(factor.group if factor is not None else "", len(group_order))
            return group_idx, name

        for name in sorted(list_factors(include_tick=True), key=sort_key):
            factor = get_factor(name)
            suffix = " [tick]" if factor is not None and factor.requires_ticks else ""
            side = factor_sides_label(factor.sides) if factor is not None else ""
            group = factor.group if factor is not None else ""
            item = QListWidgetItem(f"{name}{suffix}\n{side} | {group}")
            item.setToolTip(f"{side} | {group}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in selected else Qt.CheckState.Unchecked)
            self._factor_list.addItem(item)

    def _on_factor_filter_changed(self) -> None:
        self._apply_factor_filters()
        self._save_config()

    def _apply_factor_filters(self) -> None:
        side_filter = self._factor_side_filter.currentData() or ""
        group_filter = self._factor_group_filter.currentData() or ""
        for i in range(self._factor_list.count()):
            item = self._factor_list.item(i)
            factor = get_factor(str(item.data(Qt.ItemDataRole.UserRole)))
            hidden = False
            if factor is None:
                hidden = True
            elif side_filter and side_filter not in factor.sides:
                hidden = True
            elif group_filter and factor.group != group_filter:
                hidden = True
            item.setHidden(hidden)

    def _set_visible_factor_checks(self, state: Qt.CheckState) -> None:
        for i in range(self._factor_list.count()):
            item = self._factor_list.item(i)
            if not item.isHidden():
                item.setCheckState(state)
        self._save_config()

    def _connect_persistence(self) -> None:
        save = lambda *_: self._save_config()
        self._symbol_combo.currentTextChanged.connect(save)
        self._interval_combo.currentTextChanged.connect(save)
        self._tick_check.toggled.connect(save)
        self._horizon_edit.currentTextChanged.connect(save)
        self._quantile_spin.valueChanged.connect(save)
        self._time_slice.selection_changed.connect(save)
        self._factor_list.itemChanged.connect(save)
        self._factor_side_filter.currentIndexChanged.connect(self._on_factor_filter_changed)
        self._factor_group_filter.currentIndexChanged.connect(self._on_factor_filter_changed)

    def _on_symbol_changed(self, symbol: str) -> None:
        self._time_slice.load_symbol(symbol)

    def _selected_factors(self) -> list[str]:
        names: list[str] = []
        for i in range(self._factor_list.count()):
            item = self._factor_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                names.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return names

    def _horizons(self) -> list[int]:
        values: list[int] = []
        for raw in self._horizon_edit.currentText().split(","):
            raw = raw.strip()
            if not raw:
                continue
            values.append(max(1, int(raw)))
        return sorted(set(values))

    def _build_config(self) -> ResearchConfig | None:
        slices = self._normalize_slices(self._time_slice.get_slices())
        if not slices:
            QMessageBox.warning(self, "No Time Slice", "Select at least one time slice for research.")
            return None
        factors = self._selected_factors()
        if not factors:
            QMessageBox.warning(self, "No Factors", "Select at least one research factor.")
            return None
        try:
            horizons = self._horizons()
        except ValueError:
            QMessageBox.warning(self, "Invalid Horizons", "Use comma-separated positive integers, e.g. 1,3,6,12.")
            return None
        if not horizons:
            QMessageBox.warning(self, "Invalid Horizons", "Use at least one positive horizon.")
            return None
        return ResearchConfig(
            symbol=self._symbol_combo.currentText(),
            interval=self._interval_combo.currentText(),
            slices=slices,
            factor_names=factors,
            horizons=horizons,
            quantiles=self._quantile_spin.value(),
            use_tick_features=self._tick_check.isChecked(),
        )

    def _normalize_slices(self, slices: list) -> list:
        normalized: list = []
        for item in slices:
            if isinstance(item, tuple):
                normalized.extend(item)
            else:
                normalized.append(item)
        return normalized

    def _on_run(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        config = self._build_config()
        if config is None:
            return
        self._save_config()
        self._run_btn.setEnabled(False)
        self._export_btn.setEnabled(False)
        self._status.setText("Running vectorized research...")
        self._worker = ResearchWorkerThread(config, self)
        self._worker.result_ready.connect(self._on_result_ready)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(lambda: self._run_btn.setEnabled(True))
        self._worker.start()

    def _on_result_ready(self, result: dict) -> None:
        self._last_result = result
        self._fill_table(self._summary_table, result.get("summary", []))
        self._fill_table(self._ortho_table, result.get("orthogonal_summary", []))
        self._ts_chart.set_data(result.get("timeseries_ic", {}))
        self._fill_table(self._metrics_table, result.get("metrics", []))
        self._fill_table(self._quantile_table, result.get("quantiles", []))
        self._fill_table(self._monthly_table, result.get("stability_monthly", []))
        self._fill_table(self._yearly_table, result.get("stability_yearly", []))
        self._fill_table(self._correlation_table, result.get("factor_correlations", []))
        self._fill_table(self._unavailable_table, result.get("unavailable", []))
        self._export_btn.setEnabled(True)
        self._status.setText(
            f"Done | rows={result.get('rows', 0)} | factors={len(result.get('summary', []))}"
        )

    def _on_error(self, msg: str) -> None:
        self._status.setText(f"Error: {msg}")
        QMessageBox.critical(self, "Research Error", msg)

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
                if isinstance(val, float):
                    text = f"{val:.6g}"
                else:
                    text = str(val)
                table.setItem(r, c, QTableWidgetItem(text))
        table.resizeColumnsToContents()

    def _on_export(self) -> None:
        if not self._last_result:
            return
        
        symbol = self._symbol_combo.currentText()
        interval = self._interval_combo.currentText()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_dir_name = f"{symbol}_{interval}_{timestamp}"
        
        base_report_dir = Path("docs/reports/factor_analysis")
        base_report_dir.mkdir(parents=True, exist_ok=True)
        
        suggested_path = base_report_dir / default_dir_name / "full_result.json"
        suggested_path.parent.mkdir(parents=True, exist_ok=True)
        
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Research Package (as full_result.json)",
            str(suggested_path),
            "JSON Files (full_result.json)",
        )
        if not path:
            return
            
        target_json = Path(path)
        target_dir = target_json.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        
        # Save JSON (contains all data including timeseries_ic and orthogonal_summary)
        target_json.write_text(json.dumps(self._last_result, indent=2), encoding="utf-8")
        
        # Save CSV Bundle in the same directory
        for key, rows in self._last_result.items():
            if key == "timeseries_ic":
                continue # Skip timeseries in CSV bundle, or format differently if needed
            if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
                continue
            csv_path = target_dir / f"{key}.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
        
        self._status.setText(f"Package saved to {target_dir}")
        QMessageBox.information(self, "Export Successful", f"Research package saved to:\n{target_dir}")

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Research Result",
            str(Path("docs/reports/factor_analysis")),
            "JSON Files (*.json)",
        )
        if not path:
            return
            
        try:
            with open(path, "r", encoding="utf-8") as f:
                result = json.load(f)
            
            # Simple validation
            if "summary" not in result or "metrics" not in result:
                raise ValueError("Invalid research result file (missing summary or metrics)")
                
            self._on_result_ready(result)
            self._status.setText(f"Imported: {Path(path).parent.name}")
            QMessageBox.information(self, "Import Successful", f"Successfully imported research results from:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Import Error", f"Failed to import results: {str(e)}")

    def _save_config(self) -> None:
        if not self._restore_done:
            return
        ui_settings.set("research_lab_config", {
            "symbol": self._symbol_combo.currentText(),
            "interval": self._interval_combo.currentText(),
            "use_tick_features": self._tick_check.isChecked(),
            "horizons": self._horizon_edit.currentText(),
            "quantiles": self._quantile_spin.value(),
            "factors": self._selected_factors(),
            "factor_side_filter": self._factor_side_filter.currentData() or "",
            "factor_group_filter": self._factor_group_filter.currentData() or "",
            "selected_months": self._time_slice.selected_months(),
        })
