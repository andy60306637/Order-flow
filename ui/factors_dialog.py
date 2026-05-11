"""ui/factors_dialog.py — Factor 選擇 Dialog。"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from research.base import FACTOR_GROUPS, FACTOR_SIDE_LABELS, FACTOR_SIDES, factor_sides_label
from research.registry import ensure_builtin_factors, get_factor, list_factors


class FactorsDialog(QDialog):
    """
    Factor 選擇 Dialog（從 ResearchLab 左側面板分離出來）。

    Usage:
        dlg = FactorsDialog(parent, selected_factors, side_filter, group_filter)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            factors = dlg.get_selected_factors()
    """

    def __init__(
        self,
        parent=None,
        selected_factors: list[str] | None = None,
        side_filter: str = "",
        group_filter: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Factor 選擇")
        self.setMinimumSize(500, 540)
        self.setModal(True)
        ensure_builtin_factors()

        layout = QVBoxLayout(self)

        # ── Side + Group filter ───────────────────────────────────────────────
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Side:"))
        self._side_combo = QComboBox()
        self._side_combo.addItem("All Directions", "")
        for s in FACTOR_SIDES:
            self._side_combo.addItem(FACTOR_SIDE_LABELS[s], s)
        si = self._side_combo.findData(side_filter)
        self._side_combo.setCurrentIndex(max(0, si))
        filter_row.addWidget(self._side_combo)
        filter_row.addSpacing(8)
        filter_row.addWidget(QLabel("Group:"))
        self._group_combo = QComboBox()
        self._group_combo.addItem("All Groups", "")
        for g in FACTOR_GROUPS:
            self._group_combo.addItem(g, g)
        gi = self._group_combo.findData(group_filter)
        self._group_combo.setCurrentIndex(max(0, gi))
        filter_row.addWidget(self._group_combo, stretch=1)
        layout.addLayout(filter_row)

        # ── Check / Clear visible ─────────────────────────────────────────────
        btn_row = QHBoxLayout()
        check_btn = QPushButton("Check Visible")
        clear_btn = QPushButton("Clear Visible")
        check_btn.clicked.connect(lambda: self._set_visible(Qt.CheckState.Checked))
        clear_btn.clicked.connect(lambda: self._set_visible(Qt.CheckState.Unchecked))
        btn_row.addWidget(check_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # ── Factor list ───────────────────────────────────────────────────────
        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        layout.addWidget(self._list)

        # ── Apply / Cancel ────────────────────────────────────────────────────
        ok_row = QHBoxLayout()
        apply_btn = QPushButton("Apply")
        cancel_btn = QPushButton("Cancel")
        apply_btn.setDefault(True)
        ok_row.addStretch()
        ok_row.addWidget(apply_btn)
        ok_row.addWidget(cancel_btn)
        layout.addLayout(ok_row)

        apply_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        self._side_combo.currentIndexChanged.connect(self._apply_filters)
        self._group_combo.currentIndexChanged.connect(self._apply_filters)

        self._init_selected: set[str] = set(
            selected_factors if selected_factors is not None
            else list_factors(include_tick=True)
        )
        self._load_list()
        self._apply_filters()

    # ── Private ───────────────────────────────────────────────────────────────

    def _load_list(self) -> None:
        group_order = {g: i for i, g in enumerate(FACTOR_GROUPS)}

        def sort_key(name: str) -> tuple[int, str]:
            f = get_factor(name)
            return (group_order.get(f.group if f else "", len(group_order)), name)

        for name in sorted(list_factors(include_tick=True), key=sort_key):
            f = get_factor(name)
            suffix = " [tick]" if f and f.requires_ticks else ""
            side  = factor_sides_label(f.sides) if f else ""
            group = f.group if f else ""
            item = QListWidgetItem(f"{name}{suffix}\n{side} | {group}")
            item.setToolTip(f"{side} | {group}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if name in self._init_selected
                else Qt.CheckState.Unchecked
            )
            self._list.addItem(item)

    def _apply_filters(self) -> None:
        side  = self._side_combo.currentData()  or ""
        group = self._group_combo.currentData() or ""
        for i in range(self._list.count()):
            item = self._list.item(i)
            f = get_factor(str(item.data(Qt.ItemDataRole.UserRole)))
            item.setHidden(
                f is None
                or (side  and side  not in f.sides)
                or (group and f.group != group)
            )

    def _set_visible(self, state: Qt.CheckState) -> None:
        for i in range(self._list.count()):
            item = self._list.item(i)
            if not item.isHidden():
                item.setCheckState(state)

    # ── Public API ────────────────────────────────────────────────────────────

    def get_selected_factors(self) -> list[str]:
        return [
            str(self._list.item(i).data(Qt.ItemDataRole.UserRole))
            for i in range(self._list.count())
            if self._list.item(i).checkState() == Qt.CheckState.Checked
        ]

    def get_side_filter(self) -> str:
        return self._side_combo.currentData() or ""

    def get_group_filter(self) -> str:
        return self._group_combo.currentData() or ""
