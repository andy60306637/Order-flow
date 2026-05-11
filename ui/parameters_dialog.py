"""ui/parameters_dialog.py — Research Parameters 設定 Dialog。"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)


class ParametersDialog(QDialog):
    """
    Research Parameters 設定 Dialog（從 ResearchLab 左側面板分離出來）。

    Usage:
        dlg = ParametersDialog(parent, horizons, quantiles, entry_lag, train_ratio)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            horizons = dlg.get_horizons()       # list[int]
            quantiles = dlg.get_quantiles()     # int
            ...
    """

    def __init__(
        self,
        parent=None,
        horizons: str = "1,3,6,12",
        quantiles: int = 5,
        entry_lag: int = 1,
        train_ratio: float = 0.5,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Research Parameters")
        self.setFixedWidth(400)
        self.setModal(True)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._horizon_edit = QComboBox()
        self._horizon_edit.setEditable(True)
        self._horizon_edit.addItems([
            "1,3,6,12",
            "1,2,3,5",
            "3,6,12,24",
            "1,3,6,12,24,48",
        ])
        self._horizon_edit.setCurrentText(horizons)
        form.addRow("Forward Horizons (bars):", self._horizon_edit)

        self._quantile_spin = QSpinBox()
        self._quantile_spin.setRange(2, 10)
        self._quantile_spin.setValue(quantiles)
        form.addRow("Quantiles:", self._quantile_spin)

        self._entry_lag_spin = QSpinBox()
        self._entry_lag_spin.setRange(0, 10)
        self._entry_lag_spin.setValue(entry_lag)
        form.addRow("Entry Lag (bars):", self._entry_lag_spin)

        self._train_ratio_spin = QDoubleSpinBox()
        self._train_ratio_spin.setRange(0.1, 0.9)
        self._train_ratio_spin.setSingleStep(0.05)
        self._train_ratio_spin.setDecimals(2)
        self._train_ratio_spin.setValue(train_ratio)
        form.addRow("Train Ratio:", self._train_ratio_spin)

        layout.addLayout(form)

        btn_row = QHBoxLayout()
        apply_btn = QPushButton("Apply")
        cancel_btn = QPushButton("Cancel")
        apply_btn.setDefault(True)
        btn_row.addStretch()
        btn_row.addWidget(apply_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        apply_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)

    # ── Public API ────────────────────────────────────────────────────────────

    def get_horizons_str(self) -> str:
        return self._horizon_edit.currentText()

    def get_horizons(self) -> list[int]:
        values: list[int] = []
        for raw in self.get_horizons_str().split(","):
            raw = raw.strip()
            if raw.isdigit():
                values.append(max(1, int(raw)))
        return sorted(set(values))

    def get_quantiles(self) -> int:
        return self._quantile_spin.value()

    def get_entry_lag(self) -> int:
        return self._entry_lag_spin.value()

    def get_train_ratio(self) -> float:
        return self._train_ratio_spin.value()
