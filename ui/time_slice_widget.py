"""Shard month selector and walk-forward slice builder."""
from __future__ import annotations

import re
from pathlib import Path

from PyQt6.QtCore import pyqtSignal
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

from backtest.time_slice import TimeSlice, WalkForwardConfig
from core import tick_cache


SourceSegment = tuple[int, int, str]


def discover_tick_sources(symbol: str) -> list[str]:
    """Return the base symbol plus all date-ranged shard aliases."""
    symbol = symbol.upper()
    datasets: list[str] = []
    if tick_cache.load_meta(symbol) is not None:
        datasets.append(symbol)

    tick_dir = Path(__file__).resolve().parent.parent / "data" / "ticks"
    alias_re = re.compile(rf"^{re.escape(symbol)}_\d{{8}}_\d{{8}}$")
    for path in sorted(tick_dir.glob(f"{symbol}_*_shards.json")):
        alias = path.name.removesuffix("_shards.json")
        if alias_re.match(alias) and tick_cache.load_meta(alias) is not None:
            datasets.append(alias)
    return datasets or [symbol]


class ShardCalendarWidget(QWidget):
    """Month-level shard calendar aggregated across all tick sources."""

    selection_changed = pyqtSignal(list)  # list[str] month_keys

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        self._buttons: dict[str, QPushButton] = {}
        self._month_segments: dict[str, list[SourceSegment]] = {}

    def load_symbol(self, symbol: str) -> None:
        self.load_sources(discover_tick_sources(symbol))

    def load_sources(self, sources: list[str]) -> None:
        self._clear()
        if not sources:
            return

        from backtest.time_slice import TimeSliceManager

        for source in sources:
            mgr = TimeSliceManager(source)
            for shard in mgr.available_shards():
                if not shard.available:
                    continue
                self._month_segments.setdefault(shard.month_key, []).append(
                    (shard.start_ms, shard.end_ms, source)
                )

        for segments in self._month_segments.values():
            segments.sort(key=lambda item: item[0])

        if not self._month_segments:
            lbl = QLabel("No shard data found")
            lbl.setStyleSheet("color: #787b86;")
            self._layout.addWidget(lbl)
            return

        years: dict[str, list[str]] = {}
        for month_key in sorted(self._month_segments):
            years.setdefault(month_key[:4], []).append(month_key)

        month_names = [
            "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        ]

        for year in sorted(years):
            year_box = QGroupBox(year)
            year_box.setStyleSheet(
                "QGroupBox { color: #787b86; font-size: 11px; border: none; margin-top: 8px; }"
                "QGroupBox::title { subcontrol-origin: margin; left: 4px; }"
            )
            grid = QGridLayout(year_box)
            grid.setSpacing(3)
            grid.setContentsMargins(4, 12, 4, 4)

            for month_key in years[year]:
                month_idx = int(month_key[4:]) - 1
                btn = QPushButton(month_names[month_idx])
                btn.setCheckable(True)
                btn.setFixedSize(42, 28)
                btn.setProperty("month_key", month_key)
                btn.setToolTip(self._tooltip_for_month(month_key))
                btn.setStyleSheet(
                    "QPushButton { background-color: #1a3a2a; color: #26a69a; "
                    "border: 1px solid #26a69a44; border-radius: 3px; font-size: 11px; }"
                    "QPushButton:checked { background-color: #2962ff; color: white; "
                    "border: 1px solid #2962ff; }"
                    "QPushButton:hover { border: 1px solid #26a69a; }"
                )
                btn.toggled.connect(self._on_toggled)
                grid.addWidget(btn, 0, month_idx % 12)
                self._buttons[month_key] = btn

            self._layout.addWidget(year_box)

        self._layout.addStretch()

    def selected_months(self) -> list[str]:
        return [mk for mk, btn in self._buttons.items() if btn.isChecked()]

    def month_segments(self, month_key: str) -> list[SourceSegment]:
        return list(self._month_segments.get(month_key, []))

    def selected_segments(self) -> list[SourceSegment]:
        segments: list[SourceSegment] = []
        for month_key in sorted(self.selected_months()):
            segments.extend(self.month_segments(month_key))
        return sorted(segments, key=lambda item: item[0])

    def clipped_segments(self, start_ms: int, end_ms: int) -> list[SourceSegment]:
        clipped: list[SourceSegment] = []
        for seg_start, seg_end, source in self.selected_segments():
            start = max(seg_start, start_ms)
            end = min(seg_end, end_ms)
            if start < end:
                clipped.append((start, end, source))
        return clipped

    def _tooltip_for_month(self, month_key: str) -> str:
        sources = sorted({source for _, _, source in self.month_segments(month_key)})
        return f"{month_key}: " + ", ".join(sources)

    def _on_toggled(self) -> None:
        self.selection_changed.emit(self.selected_months())

    def _clear(self) -> None:
        self._buttons.clear()
        self._month_segments.clear()
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def select_range(self, start_mk: str, end_mk: str) -> None:
        for mk, btn in self._buttons.items():
            btn.setChecked(start_mk <= mk <= end_mk)


class TimeSliceWidget(QWidget):
    """ShardCalendarWidget plus multi-select/walk-forward slice modes."""

    slices_confirmed = pyqtSignal(list)  # list[TimeSlice | tuple(TimeSlice, TimeSlice)]
    selection_changed = pyqtSignal(list)  # list[str] month keys

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        mode_box = QGroupBox("Slice Mode")
        mode_layout = QHBoxLayout(mode_box)
        self._mode_group = QButtonGroup()

        self._radio_multi = QRadioButton("Multi-select")
        self._radio_wf = QRadioButton("Walk-Forward")
        self._radio_multi.setChecked(True)

        for i, radio in enumerate([self._radio_multi, self._radio_wf]):
            self._mode_group.addButton(radio, i)
            mode_layout.addWidget(radio)

        self._radio_multi.toggled.connect(self._on_mode_changed)
        layout.addWidget(mode_box)

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

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._calendar = ShardCalendarWidget()
        scroll.setWidget(self._calendar)
        layout.addWidget(scroll, stretch=1)

        action_row = QHBoxLayout()
        self._select_all_btn = QPushButton("Select All")
        self._clear_btn = QPushButton("Clear")
        self._select_all_btn.clicked.connect(self.select_all)
        self._clear_btn.clicked.connect(self.clear_selection)
        action_row.addWidget(self._select_all_btn)
        action_row.addWidget(self._clear_btn)
        layout.addLayout(action_row)

        self._info_label = QLabel("0 months selected")
        self._info_label.setStyleSheet("color: #787b86; font-size: 11px;")
        layout.addWidget(self._info_label)
        self._calendar.selection_changed.connect(self._on_selection_changed)

        self._symbol = ""

    def load_symbol(self, symbol: str) -> None:
        self._symbol = symbol
        self._calendar.load_symbol(symbol)

    def get_slices(self) -> list:
        selected = self._calendar.selected_months()
        if not selected or not self._symbol:
            return []

        if self._radio_multi.isChecked():
            return self._build_multi_select_slice()
        return self._build_walk_forward_slices()

    def selected_months(self) -> list[str]:
        return self._calendar.selected_months()

    def set_selected_months(self, months: list[str]) -> None:
        month_set = set(months)
        for month_key, btn in self._calendar._buttons.items():
            btn.setChecked(month_key in month_set)

    def select_all(self) -> None:
        for btn in self._calendar._buttons.values():
            btn.setChecked(True)

    def clear_selection(self) -> None:
        for btn in self._calendar._buttons.values():
            btn.setChecked(False)

    def _build_multi_select_slice(self) -> list[TimeSlice]:
        source_segments = self._calendar.selected_segments()
        if not source_segments:
            return []
        return [self._make_slice("Custom", source_segments)]

    def _build_walk_forward_slices(self) -> list[tuple[TimeSlice, TimeSlice]]:
        source_segments = self._calendar.selected_segments()
        if not source_segments:
            return []

        start_ms = min(seg[0] for seg in source_segments)
        end_ms = max(seg[1] for seg in source_segments)
        total_ms = end_ms - start_ms
        cfg = WalkForwardConfig(
            n_segments=self._wf_segments.value(),
            oos_fraction=self._wf_oos.value(),
            anchored=False,
        )
        if cfg.n_segments <= 0 or total_ms <= 0:
            return []

        is_fraction = 1.0 - cfg.oos_fraction
        window_size = total_ms / (cfg.n_segments * is_fraction + cfg.oos_fraction)
        step = window_size * is_fraction
        result: list[tuple[TimeSlice, TimeSlice]] = []

        for i in range(cfg.n_segments):
            w_start = start_ms + int(step * i)
            is_end = w_start + int(window_size * is_fraction)
            oos_start = is_end
            oos_end = min(w_start + int(window_size), end_ms)
            if w_start >= end_ms or oos_start >= oos_end:
                continue
            is_segments = self._calendar.clipped_segments(w_start, is_end)
            oos_segments = self._calendar.clipped_segments(oos_start, oos_end)
            if not is_segments or not oos_segments:
                continue
            result.append((
                self._make_slice(f"IS_{i + 1}", is_segments),
                self._make_slice(f"OOS_{i + 1}", oos_segments),
            ))

        return result

    def _make_slice(self, label: str, source_segments: list[SourceSegment]) -> TimeSlice:
        segments = [(start, end) for start, end, _ in source_segments]
        symbols = [source for _, _, source in source_segments]
        return TimeSlice(label=label, segments=segments, segment_symbols=symbols)

    def _on_mode_changed(self) -> None:
        self._wf_box.setVisible(self._radio_wf.isChecked())

    def _on_selection_changed(self, months: list[str]) -> None:
        segment_count = len(self._calendar.selected_segments())
        self._info_label.setText(f"{len(months)} months selected | {segment_count} shard parts")
        self.selection_changed.emit(months)
