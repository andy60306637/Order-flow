"""ui/time_slice_dialog.py — Time Slice 設定 Dialog。"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
)

from ui.time_slice_widget import TimeSliceWidget


class TimeSliceDialog(QDialog):
    """
    將 TimeSliceWidget 包裝為獨立 Dialog。

    Usage:
        dlg = TimeSliceDialog(parent, symbol, selected_months)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            months = dlg.get_selected_months()
            slices = dlg.get_slices()
    """

    def __init__(
        self,
        parent=None,
        symbol: str = "",
        selected_months: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Time Slice 設定")
        self.setMinimumSize(640, 380)
        self.setModal(True)

        layout = QVBoxLayout(self)

        self._widget = TimeSliceWidget()
        if symbol:
            self._widget.load_symbol(symbol)
        if selected_months:
            self._widget.set_selected_months(selected_months)
        layout.addWidget(self._widget)

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

    def get_slices(self) -> list:
        return self._widget.get_slices()

    def get_selected_months(self) -> list[str]:
        return self._widget.selected_months()
