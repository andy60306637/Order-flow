"""交易明細表：可排序、可篩選的 QTableWidget。"""
from __future__ import annotations

from datetime import datetime, timezone

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


_COLUMNS = [
    ("#",           "index"),
    ("Dir",         "dir"),
    ("Entry Time",  "entry_time"),
    ("Entry $",     "entry"),
    ("Exit",        "exit_label"),
    ("Exit $",      "exit"),
    ("Net PnL",     "net_pnl"),
    ("Equity",      "equity_after"),
    ("MAE",         "mae"),
    ("MFE",         "mfe"),
    ("Session",     "session_hour"),
    ("Regime",      "trend_regime"),
]

_GREEN = QColor("#26a69a")
_RED   = QColor("#ef5350")
_DIM   = QColor("#787b86")


def _fmt_time(ms: int | None) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")


class TradeLedger(QWidget):
    """回測交易明細表。"""

    trade_selected = pyqtSignal(dict)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # ── 篩選列 ────────────────────────────────────────────────────────────
        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)

        filter_row.addWidget(QLabel("Dir:"))
        self._dir_combo = QComboBox()
        self._dir_combo.addItems(["All", "Long", "Short"])
        self._dir_combo.currentTextChanged.connect(self._apply_filter)
        filter_row.addWidget(self._dir_combo)

        filter_row.addWidget(QLabel("Exit:"))
        self._exit_combo = QComboBox()
        self._exit_combo.addItems(["All", "SL", "TP", "TS", "TD"])
        self._exit_combo.currentTextChanged.connect(self._apply_filter)
        filter_row.addWidget(self._exit_combo)

        filter_row.addStretch()
        self._count_label = QLabel("0 trades")
        self._count_label.setStyleSheet("color: #787b86; font-size: 11px;")
        filter_row.addWidget(self._count_label)

        layout.addLayout(filter_row)

        # ── 表格 ──────────────────────────────────────────────────────────────
        self._table = QTableWidget()
        self._table.setColumnCount(len(_COLUMNS))
        self._table.setHorizontalHeaderLabels([c[0] for c in _COLUMNS])
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.setSortingEnabled(True)
        self._table.setStyleSheet(
            "QTableWidget { gridline-color: #2a2e39; }"
            "QHeaderView::section { background-color: #1e222d; color: #d1d4dc; }"
        )
        self._table.itemSelectionChanged.connect(self._on_selection)
        layout.addWidget(self._table)

        self._all_trades: list[dict] = []

    def load_result(self, stats: dict) -> None:
        trade_list = stats.get("trade_list", [])
        self._all_trades = [t for t in trade_list if not t.get("skipped")]
        self._apply_filter()

    def _apply_filter(self) -> None:
        dir_filter  = self._dir_combo.currentText().lower()
        exit_filter = self._exit_combo.currentText()

        filtered = self._all_trades
        if dir_filter != "all":
            filtered = [t for t in filtered if t.get("dir") == dir_filter]
        if exit_filter != "All":
            filtered = [t for t in filtered if t.get("exit_label") == exit_filter]

        self._populate(filtered)

    def _populate(self, trades: list[dict]) -> None:
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(trades))

        for row, t in enumerate(trades):
            pnl = t.get("net_pnl", 0.0)
            color = _GREEN if pnl > 0 else _RED

            vals = [
                str(row + 1),
                t.get("dir", "—"),
                _fmt_time(t.get("entry_time")),
                f"{t.get('entry', 0):.1f}",
                t.get("exit_label", "—"),
                f"{t.get('exit', 0):.1f}",
                f"{pnl:+.2f}",
                f"{t.get('equity_after', 0):.2f}",
                f"{abs(t.get('mae', t.get('MAE', 0)) or 0):.2f}",
                f"{abs(t.get('mfe', t.get('MFE', 0)) or 0):.2f}",
                str(t.get("session_hour", "—")),
                str(t.get("trend_regime", "—")),
            ]

            for col, val in enumerate(vals):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if col == 6:  # Net PnL 欄位染色
                    item.setForeground(color)
                elif col == 1:  # Dir
                    item.setForeground(_GREEN if t.get("dir") == "long" else _RED)
                self._table.setItem(row, col, item)
                item.setData(Qt.ItemDataRole.UserRole, t)

        self._table.setSortingEnabled(True)
        self._count_label.setText(f"{len(trades)} trades")

    def _on_selection(self) -> None:
        rows = self._table.selectedItems()
        if rows:
            trade = rows[0].data(Qt.ItemDataRole.UserRole)
            if isinstance(trade, dict):
                self.trade_selected.emit(trade)

    def clear(self) -> None:
        self._all_trades = []
        self._table.setRowCount(0)
        self._count_label.setText("0 trades")
