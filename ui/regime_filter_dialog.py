"""
ui/regime_filter_dialog.py

Regime Filter 設定 Dialog。

四個維度各自有 Enable 開關 + Label 多選 + 關鍵參數：
  Session          — 無額外參數
  Market Vol       — lookback
  VWAP Zone        — window, lookback
  Vol Profile      — window, tick_size, value_area_pct（注意：較重）

兩種模式：
  Filter  — 各維度 AND 合併（維度內 OR），跑一次
  Matrix  — 每個 label 獨立跑，結果並排
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from research.regime_filter import (
    DIM_MARKET_VOL,
    DIM_SESSION,
    DIM_VOL_PROFILE,
    DIM_VWAP_ZONE,
    DIMENSION_DISPLAY,
    DIMENSION_LABELS,
    MARKET_VOL_LABELS,
    SESSION_LABELS,
    VOL_PROFILE_LABELS,
    VWAP_ZONE_LABELS,
    RegimeDimConfig,
    RegimeFilterConfig,
)


class _DimSection(QWidget):
    """單個維度的 UI 區塊（Enable 開關 + Labels + Params）。"""

    def __init__(self, dimension: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dimension = dimension
        labels = DIMENSION_LABELS[dimension]

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        box = QGroupBox(DIMENSION_DISPLAY[dimension])
        box_layout = QVBoxLayout(box)
        box_layout.setSpacing(4)

        # Enable toggle
        self._enable_chk = QCheckBox("啟用此維度")
        box_layout.addWidget(self._enable_chk)

        # Label checkboxes
        self._label_frame = QWidget()
        label_layout = QHBoxLayout(self._label_frame)
        label_layout.setContentsMargins(0, 0, 0, 0)
        label_layout.setSpacing(6)
        self._label_chks: dict[str, QCheckBox] = {}
        for lbl in labels:
            chk = QCheckBox(lbl)
            self._label_chks[lbl] = chk
            label_layout.addWidget(chk)
        label_layout.addStretch()
        box_layout.addWidget(self._label_frame)

        # Param widgets (dimension-specific)
        self._param_frame = QWidget()
        param_layout = QHBoxLayout(self._param_frame)
        param_layout.setContentsMargins(0, 0, 0, 0)
        param_layout.setSpacing(8)
        self._params: dict[str, QSpinBox | QDoubleSpinBox] = {}
        self._build_params(dimension, param_layout)
        if self._params:
            box_layout.addWidget(self._param_frame)
        else:
            self._param_frame.hide()

        root.addWidget(box)

        # Warning label for heavy components
        if dimension == DIM_VOL_PROFILE:
            warn = QLabel("⚠ Vol Profile 逐根重建，大資料集耗時較長")
            warn.setStyleSheet("color: #f59e0b; font-size: 11px;")
            root.addWidget(warn)

        self._enable_chk.toggled.connect(self._on_enable_toggled)
        self._on_enable_toggled(False)

    def _build_params(self, dim: str, layout: QHBoxLayout) -> None:
        if dim == DIM_MARKET_VOL:
            layout.addWidget(QLabel("Lookback:"))
            sp = QSpinBox()
            sp.setRange(50, 500)
            sp.setValue(100)
            sp.setFixedWidth(64)
            layout.addWidget(sp)
            self._params["lookback"] = sp
            layout.addStretch()

        elif dim == DIM_VWAP_ZONE:
            layout.addWidget(QLabel("Window:"))
            w = QSpinBox()
            w.setRange(5, 200)
            w.setValue(24)
            w.setFixedWidth(56)
            layout.addWidget(w)
            self._params["window"] = w

            layout.addWidget(QLabel("Lookback:"))
            lb = QSpinBox()
            lb.setRange(20, 500)
            lb.setValue(100)
            lb.setFixedWidth(64)
            layout.addWidget(lb)
            self._params["lookback"] = lb
            layout.addStretch()

        elif dim == DIM_VOL_PROFILE:
            layout.addWidget(QLabel("Window:"))
            w = QSpinBox()
            w.setRange(5, 200)
            w.setValue(24)
            w.setFixedWidth(56)
            layout.addWidget(w)
            self._params["window"] = w

            layout.addWidget(QLabel("TickSize:"))
            ts = QDoubleSpinBox()
            ts.setRange(0.01, 1000.0)
            ts.setValue(1.0)
            ts.setSingleStep(0.1)
            ts.setFixedWidth(72)
            layout.addWidget(ts)
            self._params["tick_size"] = ts

            layout.addWidget(QLabel("VA%:"))
            va = QDoubleSpinBox()
            va.setRange(0.5, 0.95)
            va.setValue(0.70)
            va.setSingleStep(0.05)
            va.setDecimals(2)
            va.setFixedWidth(60)
            layout.addWidget(va)
            self._params["value_area_pct"] = va
            layout.addStretch()

    def _on_enable_toggled(self, enabled: bool) -> None:
        self._label_frame.setEnabled(enabled)
        self._param_frame.setEnabled(enabled)

    # ── public API ────────────────────────────────────────────────────────────

    def get_config(self) -> RegimeDimConfig:
        enabled = self._enable_chk.isChecked()
        selected = [
            lbl for lbl, chk in self._label_chks.items()
            if chk.isChecked()
        ]
        params: dict[str, Any] = {}
        for key, spin in self._params.items():
            params[key] = spin.value()
        return RegimeDimConfig(
            dimension=self._dimension,
            enabled=enabled,
            selected_labels=selected,
            params=params,
        )

    def set_config(self, cfg: RegimeDimConfig) -> None:
        self._enable_chk.setChecked(cfg.enabled)
        for lbl, chk in self._label_chks.items():
            chk.setChecked(lbl in cfg.selected_labels)
        for key, spin in self._params.items():
            if key in cfg.params:
                val = cfg.params[key]
                if isinstance(spin, QDoubleSpinBox):
                    spin.setValue(float(val))
                else:
                    spin.setValue(int(val))

    def clear_labels(self) -> None:
        for chk in self._label_chks.values():
            chk.setChecked(False)


class RegimeFilterDialog(QDialog):
    """
    Regime Filter 設定對話框。

    Usage:
        dlg = RegimeFilterDialog(parent, current_config)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            config = dlg.get_config()
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        config: RegimeFilterConfig | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Regime Filter 設定")
        self.setMinimumWidth(700)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ── Mode ─────────────────────────────────────────────────────────────
        mode_box = QGroupBox("執行模式")
        mode_layout = QVBoxLayout(mode_box)
        self._filter_radio = QRadioButton(
            "Filter  — 維度間 AND 合併，跑一次（適合條件切片）"
        )
        self._matrix_radio = QRadioButton(
            "Matrix  — 每個 label 獨立跑一次，結果並排比較"
        )
        self._cross_matrix_radio = QRadioButton(
            "Cross Matrix  — 各維度 label 笛卡兒積，每個組合各跑一次（N×M×… 次）"
        )
        mode_layout.addWidget(self._filter_radio)
        mode_layout.addWidget(self._matrix_radio)
        mode_layout.addWidget(self._cross_matrix_radio)
        self._matrix_radio.setChecked(True)
        root.addWidget(mode_box)

        # ── Dimension sections ────────────────────────────────────────────────
        self._sections: dict[str, _DimSection] = {}
        for dim in [DIM_SESSION, DIM_MARKET_VOL, DIM_VWAP_ZONE, DIM_VOL_PROFILE]:
            sec = _DimSection(dim, self)
            self._sections[dim] = sec
            root.addWidget(sec)

        # Separator
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #363c4e;")
        root.addWidget(line)

        # ── Preview label ─────────────────────────────────────────────────────
        self._preview_label = QLabel("")
        self._preview_label.setStyleSheet("color: #787b86; font-size: 11px;")
        root.addWidget(self._preview_label)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._clear_btn = QPushButton("Clear All")
        self._apply_btn = QPushButton("Apply")
        self._cancel_btn = QPushButton("Cancel")
        self._apply_btn.setDefault(True)
        btn_row.addWidget(self._clear_btn)
        btn_row.addStretch()
        btn_row.addWidget(self._apply_btn)
        btn_row.addWidget(self._cancel_btn)
        root.addLayout(btn_row)

        # ── Connections ───────────────────────────────────────────────────────
        self._clear_btn.clicked.connect(self._on_clear)
        self._apply_btn.clicked.connect(self.accept)
        self._cancel_btn.clicked.connect(self.reject)
        self._matrix_radio.toggled.connect(self._update_preview)
        self._cross_matrix_radio.toggled.connect(self._update_preview)
        for sec in self._sections.values():
            for chk in sec._label_chks.values():
                chk.toggled.connect(self._update_preview)
            sec._enable_chk.toggled.connect(self._update_preview)

        # ── Restore initial config ────────────────────────────────────────────
        if config is not None:
            self.set_config(config)
        self._update_preview()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_config(self) -> RegimeFilterConfig:
        if self._cross_matrix_radio.isChecked():
            mode = "cross_matrix"
        elif self._filter_radio.isChecked():
            mode = "filter"
        else:
            mode = "matrix"
        dims = [sec.get_config() for sec in self._sections.values()]
        return RegimeFilterConfig(mode=mode, dimensions=dims)

    def set_config(self, cfg: RegimeFilterConfig) -> None:
        if cfg.mode == "filter":
            self._filter_radio.setChecked(True)
        elif cfg.mode == "cross_matrix":
            self._cross_matrix_radio.setChecked(True)
        else:
            self._matrix_radio.setChecked(True)
        dim_map = {d.dimension: d for d in cfg.dimensions}
        for dim, sec in self._sections.items():
            if dim in dim_map:
                sec.set_config(dim_map[dim])

    # ── Private ───────────────────────────────────────────────────────────────

    def _on_clear(self) -> None:
        for sec in self._sections.values():
            sec._enable_chk.setChecked(False)
            sec.clear_labels()
        self._update_preview()

    def _update_preview(self) -> None:
        cfg = self.get_config()
        n = cfg.active_label_count()
        if n == 0:
            self._preview_label.setText("未選擇任何 regime — 等同全量分析")
            return
        if cfg.mode == "matrix":
            self._preview_label.setText(
                f"Matrix 模式：{n} 個 regime 各跑一次 IC 分析（共 {n} 次）"
            )
        elif cfg.mode == "cross_matrix":
            combos = cfg.cross_combination_count()
            self._preview_label.setText(
                f"Cross Matrix 模式：{n} 個 labels 笛卡兒積 → {combos} 個組合，各跑一次（共 {combos} 次）"
            )
        else:
            self._preview_label.setText(
                f"Filter 模式：合併所有勾選條件跑一次 IC 分析（"
                f"預估保留 ~{max(1, 100 // max(n, 1))}% 樣本）"
            )
