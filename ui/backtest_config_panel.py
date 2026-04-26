"""左側回測設定面板：策略選取 + 時間切片 + 回測參數。"""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from backtest.engine import BacktestConfig
from backtest.time_slice import TimeSlice
from config import base as cfg_base
from core import tick_cache
from strategies import STRATEGY_REGISTRY
from ui.time_slice_widget import TimeSliceWidget, discover_tick_sources
from utils.ui_settings import ui_settings


class BacktestConfigPanel(QWidget):
    """
    內嵌式回測設定面板（非對話框）。
    run_requested：發射 (strategy_instance, BacktestConfig, list[slices])
    """

    run_requested    = pyqtSignal(object, object, list)  # (strategy, BacktestConfig, slices)
    cancel_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(340)
        self._saved = ui_settings.get("backtest_dashboard_config", {})
        self._restore_done = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── 交易對 & 時間框架 ─────────────────────────────────────────────────
        symbol_box = QGroupBox("Symbol / Interval")
        sym_form = QFormLayout(symbol_box)

        self._symbol_combo = QComboBox()
        self._symbol_combo.addItems(cfg_base.SYMBOLS)
        if self._saved.get("symbol") in cfg_base.SYMBOLS:
            self._symbol_combo.setCurrentText(self._saved["symbol"])
        self._symbol_combo.currentTextChanged.connect(self._on_symbol_changed)
        sym_form.addRow("Symbol:", self._symbol_combo)

        self._tick_coverage_label = QLabel("-")
        self._tick_coverage_label.setWordWrap(True)
        self._tick_coverage_label.setStyleSheet("color: #80cbc4; font-size: 11px;")
        sym_form.addRow("Tick Coverage:", self._tick_coverage_label)

        self._interval_combo = QComboBox()
        self._interval_combo.addItems(cfg_base.INTERVALS)
        self._interval_combo.setCurrentText(self._saved.get("interval", "1m"))
        sym_form.addRow("Interval:", self._interval_combo)

        layout.addWidget(symbol_box)

        # ── 策略選取 ──────────────────────────────────────────────────────────
        strategy_box = QGroupBox("Strategy")
        strat_form = QFormLayout(strategy_box)

        self._strategy_combo = QComboBox()
        self._strategy_combo.addItems(sorted(STRATEGY_REGISTRY.keys()))
        if self._saved.get("strategy") in STRATEGY_REGISTRY:
            self._strategy_combo.setCurrentText(self._saved["strategy"])
        strat_form.addRow("Strategy:", self._strategy_combo)

        layout.addWidget(strategy_box)

        # ── 時間切片 ──────────────────────────────────────────────────────────
        slice_box = QGroupBox("Time Slice")
        slice_layout = QVBoxLayout(slice_box)
        self._time_slice = TimeSliceWidget()
        slice_layout.addWidget(self._time_slice)
        layout.addWidget(slice_box, stretch=1)

        # ── 回測參數 ──────────────────────────────────────────────────────────
        bt_box = QGroupBox("Backtest Parameters")
        bt_form = QFormLayout(bt_box)
        bt_form.setSpacing(4)

        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["Tick", "Bar"])
        self._mode_combo.setCurrentText(self._saved.get("mode", "Tick"))
        self._mode_combo.setToolTip("Tick mode uses tick-level execution data; Bar mode uses kline-only strategy evaluation.")
        bt_form.addRow("Execution Mode:", self._mode_combo)

        self._capital_spin = QDoubleSpinBox()
        self._capital_spin.setRange(100, 10_000_000)
        self._capital_spin.setValue(self._saved.get("initial_capital", 10_000))
        self._capital_spin.setSingleStep(1000)
        bt_form.addRow("Capital (USDT):", self._capital_spin)

        self._leverage_spin = QSpinBox()
        self._leverage_spin.setRange(1, 125)
        self._leverage_spin.setValue(self._saved.get("leverage", 20))
        bt_form.addRow("Leverage:", self._leverage_spin)

        self._risk_spin = QDoubleSpinBox()
        self._risk_spin.setRange(0.1, 100)
        self._risk_spin.setValue(self._saved.get("max_risk_pct", 2.0))
        self._risk_spin.setSuffix("%")
        bt_form.addRow("Max Risk/Trade:", self._risk_spin)

        self._fee_combo = QComboBox()
        self._fee_combo.addItems(["Taker", "Maker", "100% Maker", "70M/30T", "50M/50T", "自訂"])
        self._fee_combo.setCurrentText(self._saved.get("fee_mode", "Taker"))
        self._fee_combo.currentTextChanged.connect(self._on_fee_mode_changed)
        bt_form.addRow("Fee Mode:", self._fee_combo)

        self._custom_fee_spin = QDoubleSpinBox()
        self._custom_fee_spin.setRange(0.0, 1.0)
        self._custom_fee_spin.setDecimals(4)
        self._custom_fee_spin.setSingleStep(0.005)
        self._custom_fee_spin.setValue(self._saved.get("custom_fee_pct", 0.05))
        self._custom_fee_spin.setSuffix("%")
        self._custom_fee_spin.setVisible(False)
        self._custom_fee_label = QLabel("Custom Fee:")
        self._custom_fee_label.setVisible(False)
        bt_form.addRow(self._custom_fee_label, self._custom_fee_spin)

        self._slip_spin = QDoubleSpinBox()
        self._slip_spin.setRange(0, 100)
        self._slip_spin.setValue(self._saved.get("slippage_bps", 0.0))
        self._slip_spin.setSuffix(" bps")
        bt_form.addRow("Slippage:", self._slip_spin)

        self._compound_check = QCheckBox("Compound Equity")
        self._compound_check.setChecked(self._saved.get("compound", True))
        bt_form.addRow("", self._compound_check)

        layout.addWidget(bt_box)

        # ── 執行按鈕 ──────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._run_btn = QPushButton("▶ Run Backtest")
        self._run_btn.setStyleSheet(
            "QPushButton { background-color: #2962ff; color: white; "
            "border-radius: 4px; padding: 6px 16px; font-weight: bold; }"
            "QPushButton:hover { background-color: #1e4fd8; }"
            "QPushButton:disabled { background-color: #333; color: #666; }"
        )
        self._cancel_btn = QPushButton("✕ Cancel")
        self._cancel_btn.setStyleSheet(
            "QPushButton { background-color: #1e222d; color: #d1d4dc; "
            "border: 1px solid #2a2e39; border-radius: 4px; padding: 6px 12px; }"
        )
        self._cancel_btn.setVisible(False)

        self._run_btn.clicked.connect(self._on_run)
        self._cancel_btn.clicked.connect(self._on_cancel)

        btn_row.addWidget(self._run_btn)
        btn_row.addWidget(self._cancel_btn)
        layout.addLayout(btn_row)

        # 初始化時間切片
        self._on_symbol_changed(self._symbol_combo.currentText())
        self._time_slice.set_selected_months(self._saved.get("selected_months", []))
        self._on_fee_mode_changed(self._fee_combo.currentText())
        self._connect_config_persistence()
        self._restore_done = True

    # ── 公開 API ──────────────────────────────────────────────────────────────

    def set_running(self, running: bool) -> None:
        self._run_btn.setEnabled(not running)
        self._cancel_btn.setVisible(running)

    def symbol(self) -> str:
        return self._symbol_combo.currentText()

    def tick_symbol(self) -> str:
        return self.symbol()

    def interval(self) -> str:
        return self._interval_combo.currentText()

    def use_tick_mode(self) -> bool:
        return self._mode_combo.currentText() == "Tick"

    def build_bt_config(self) -> BacktestConfig:
        return BacktestConfig(
            initial_capital = self._capital_spin.value(),
            max_loss_pct    = self._risk_spin.value() / 100.0,
            leverage        = self._leverage_spin.value(),
            fee_mode        = self._fee_combo.currentText(),
            custom_fee_rate = self._custom_fee_spin.value() / 100.0,
            slippage_bps    = self._slip_spin.value(),
            compound        = self._compound_check.isChecked(),
        )

    def build_strategy(self):
        """回傳已選取策略的實例。"""
        name = self._strategy_combo.currentText()
        cls = STRATEGY_REGISTRY.get(name)
        return cls() if cls else None

    # ── 私有 ──────────────────────────────────────────────────────────────────

    def _on_symbol_changed(self, symbol: str) -> None:
        self._rebuild_tick_dataset_combo(symbol)

    def _on_tick_dataset_changed(self, _label: str) -> None:
        # The month selector is aggregated by base symbol, not by this fallback tick dataset.
        return

    def _list_tick_datasets(self, symbol: str) -> list[str]:
        return discover_tick_sources(symbol)

    def _tick_dataset_label(self, symbol: str) -> str:
        meta = tick_cache.load_meta(symbol)
        if meta is None:
            return symbol
        from datetime import datetime, timezone
        start = datetime.fromtimestamp(meta["start_ms"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        end = datetime.fromtimestamp(meta["end_ms"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if symbol == self.symbol():
            return f"Current ({start} ~ {end})"
        return f"{start} ~ {end}"

    def _rebuild_tick_dataset_combo(self, symbol: str) -> None:
        datasets = self._list_tick_datasets(symbol)
        self._tick_coverage_label.setText(self._tick_coverage_text(datasets))
        self._time_slice.load_symbol(symbol)

    def _on_fee_mode_changed(self, mode: str) -> None:
        is_custom = mode == "自訂"
        self._custom_fee_label.setVisible(is_custom)
        self._custom_fee_spin.setVisible(is_custom)

    def _tick_coverage_text(self, datasets: list[str]) -> str:
        ranges: list[tuple[int, int]] = []
        for dataset in datasets:
            meta = tick_cache.load_meta(dataset)
            if meta is not None:
                ranges.append((int(meta["start_ms"]), int(meta["end_ms"])))
        if not ranges:
            return "No shard data"
        from datetime import datetime, timezone
        start_ms = min(start for start, _ in ranges)
        end_ms = max(end for _, end in ranges)
        start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        return f"{start} ~ {end} | {len(datasets)} shard sets"

    def _save_config(self) -> None:
        if not self._restore_done:
            return
        ui_settings.set("backtest_dashboard_config", {
            "symbol": self.symbol(),
            "interval": self.interval(),
            "strategy": self._strategy_combo.currentText(),
            "mode": self._mode_combo.currentText(),
            "initial_capital": self._capital_spin.value(),
            "leverage": self._leverage_spin.value(),
            "max_risk_pct": self._risk_spin.value(),
            "fee_mode": self._fee_combo.currentText(),
            "custom_fee_pct": self._custom_fee_spin.value(),
            "slippage_bps": self._slip_spin.value(),
            "compound": self._compound_check.isChecked(),
            "selected_months": self._time_slice.selected_months(),
        })

    def _connect_config_persistence(self) -> None:
        save = lambda *_: self._save_config()
        self._symbol_combo.currentTextChanged.connect(save)
        self._interval_combo.currentTextChanged.connect(save)
        self._strategy_combo.currentTextChanged.connect(save)
        self._mode_combo.currentTextChanged.connect(save)
        self._capital_spin.valueChanged.connect(save)
        self._leverage_spin.valueChanged.connect(save)
        self._risk_spin.valueChanged.connect(save)
        self._fee_combo.currentTextChanged.connect(save)
        self._custom_fee_spin.valueChanged.connect(save)
        self._slip_spin.valueChanged.connect(save)
        self._compound_check.toggled.connect(save)
        self._time_slice.selection_changed.connect(save)

    def _on_run(self) -> None:
        strategy = self.build_strategy()
        if strategy is None:
            return
        bt_cfg = self.build_bt_config()
        slices = self._time_slice.get_slices()
        if not slices:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "No Time Slice", "請先選取至少一個月份的分片資料。")
            return
        self._save_config()
        self.run_requested.emit(strategy, bt_cfg, slices)

    def _on_cancel(self) -> None:
        self.cancel_requested.emit()
