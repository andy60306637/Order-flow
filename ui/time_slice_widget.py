"""時間切片選擇器：Shard 月份日曆 + 切片模式控制。"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
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

from backtest.time_slice import TimeSlice, TimeSliceManager, WalkForwardConfig


class ShardCalendarWidget(QWidget):
    """
    月份按鈕網格（依年份排列）。
    綠色 = 有資料，灰色 = 無資料，藍色邊框 = 已選取。
    """

    selection_changed = pyqtSignal(list)   # list[str] month_keys

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        self._buttons: dict[str, QPushButton] = {}
        self._symbol = ""

    def load_symbol(self, symbol: str) -> None:
        self._symbol = symbol
        self._clear()
        if not symbol:
            return

        mgr = TimeSliceManager(symbol)
        shards = mgr.available_shards()
        if not shards:
            lbl = QLabel("No shard data found")
            lbl.setStyleSheet("color: #787b86;")
            self._layout.addWidget(lbl)
            return

        # 依年份分組
        years: dict[str, list] = {}
        for s in shards:
            year = s.month_key[:4]
            years.setdefault(year, []).append(s)

        for year in sorted(years):
            year_box = QGroupBox(year)
            year_box.setStyleSheet(
                "QGroupBox { color: #787b86; font-size: 11px; border: none; margin-top: 8px; }"
                "QGroupBox::title { subcontrol-origin: margin; left: 4px; }"
            )
            grid = QGridLayout(year_box)
            grid.setSpacing(3)
            grid.setContentsMargins(4, 12, 4, 4)

            month_names = ["Jan","Feb","Mar","Apr","May","Jun",
                           "Jul","Aug","Sep","Oct","Nov","Dec"]

            for s in years[year]:
                month_idx = int(s.month_key[4:]) - 1
                btn = QPushButton(month_names[month_idx])
                btn.setCheckable(True)
                btn.setFixedSize(42, 28)
                btn.setProperty("month_key", s.month_key)

                if s.available:
                    btn.setStyleSheet(
                        "QPushButton { background-color: #1a3a2a; color: #26a69a; "
                        "border: 1px solid #26a69a44; border-radius: 3px; font-size: 11px; }"
                        "QPushButton:checked { background-color: #2962ff; color: white; "
                        "border: 1px solid #2962ff; }"
                        "QPushButton:hover { border: 1px solid #26a69a; }"
                    )
                else:
                    btn.setEnabled(False)
                    btn.setStyleSheet(
                        "QPushButton { background-color: #1a1a1a; color: #444; "
                        "border: 1px solid #333; border-radius: 3px; font-size: 11px; }"
                    )

                btn.toggled.connect(self._on_toggled)
                grid.addWidget(btn, 0, month_idx % 12)
                self._buttons[s.month_key] = btn

            self._layout.addWidget(year_box)

        self._layout.addStretch()

    def selected_months(self) -> list[str]:
        return [mk for mk, btn in self._buttons.items() if btn.isChecked()]

    def _on_toggled(self) -> None:
        self.selection_changed.emit(self.selected_months())

    def _clear(self) -> None:
        self._buttons.clear()
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def select_range(self, start_mk: str, end_mk: str) -> None:
        """批次選取指定月份範圍。"""
        for mk, btn in self._buttons.items():
            if btn.isEnabled():
                btn.setChecked(start_mk <= mk <= end_mk)


class TimeSliceWidget(QWidget):
    """
    整合 ShardCalendarWidget + 切片模式選擇。
    模式：Multi-select / Walk-Forward
    """

    slices_confirmed = pyqtSignal(list)   # list[TimeSlice | tuple(TimeSlice,TimeSlice)]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # ── 模式選擇 ─────────────────────────────────────────────────────────
        mode_box = QGroupBox("Slice Mode")
        mode_layout = QHBoxLayout(mode_box)
        self._mode_group = QButtonGroup()

        self._radio_multi = QRadioButton("Multi-select")
        self._radio_wf    = QRadioButton("Walk-Forward")
        self._radio_multi.setChecked(True)

        for i, r in enumerate([self._radio_multi, self._radio_wf]):
            self._mode_group.addButton(r, i)
            mode_layout.addWidget(r)

        self._radio_multi.toggled.connect(self._on_mode_changed)
        layout.addWidget(mode_box)

        # ── Walk-Forward 參數 ────────────────────────────────────────────────
        self._wf_box = QGroupBox("Walk-Forward Config")
        wf_layout = QGridLayout(self._wf_box)

        wf_layout.addWidget(QLabel("Segments:"), 0, 0)
        self._wf_segments = QSpinBox()
        self._wf_segments.setRange(2, 20)
        self._wf_segments.setValue(4)
        wf_layout.addWidget(self._wf_segments, 0, 1)

        wf_layout.addWidget(QLabel("OOS Fraction:"), 1, 0)
        self._wf_oos = QDoubleSpinBox()
        self._wf_oos.setRange(0.1, 0.5)
        self._wf_oos.setSingleStep(0.05)
        self._wf_oos.setValue(0.3)
        wf_layout.addWidget(self._wf_oos, 1, 1)

        self._wf_box.setVisible(False)
        layout.addWidget(self._wf_box)

        # ── 月份日曆 ─────────────────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._calendar = ShardCalendarWidget()
        scroll.setWidget(self._calendar)
        layout.addWidget(scroll, stretch=1)

        # ── 選取資訊 ─────────────────────────────────────────────────────────
        self._info_label = QLabel("0 months selected")
        self._info_label.setStyleSheet("color: #787b86; font-size: 11px;")
        layout.addWidget(self._info_label)
        self._calendar.selection_changed.connect(self._on_selection_changed)

        self._symbol = ""

    # ── 公開 API ──────────────────────────────────────────────────────────────

    def load_symbol(self, symbol: str) -> None:
        self._symbol = symbol
        self._calendar.load_symbol(symbol)

    def get_slices(self) -> list:
        """
        依目前模式回傳切片列表。
        Multi-select → [TimeSlice]
        Walk-Forward → [(IS_TimeSlice, OOS_TimeSlice), ...]
        """
        selected = self._calendar.selected_months()
        if not selected or not self._symbol:
            return []

        mgr = TimeSliceManager(self._symbol)

        if self._radio_multi.isChecked():
            sl = mgr.build_slice(selected, label="Custom")
            return [sl] if sl.segments else []
        else:
            # Walk-forward：以所選月份的起迄建範圍
            all_shards = {s.month_key: s for s in mgr.available_shards()}
            start_mk = min(selected)
            end_mk   = max(selected)
            start_ms = all_shards[start_mk].start_ms if start_mk in all_shards else 0
            end_ms   = all_shards[end_mk].end_ms     if end_mk   in all_shards else 0
            if not start_ms or not end_ms:
                return []
            cfg = WalkForwardConfig(
                n_segments=self._wf_segments.value(),
                oos_fraction=self._wf_oos.value(),
                anchored=False,
            )
            return mgr.build_walk_forward(start_ms, end_ms, cfg)

    # ── 私有 ──────────────────────────────────────────────────────────────────

    def _on_mode_changed(self) -> None:
        self._wf_box.setVisible(self._radio_wf.isChecked())

    def _on_selection_changed(self, months: list[str]) -> None:
        self._info_label.setText(f"{len(months)} months selected")
