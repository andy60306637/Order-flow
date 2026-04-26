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
from strategies import STRATEGY_REGISTRY
from ui.time_slice_widget import TimeSliceWidget


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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── 交易對 & 時間框架 ─────────────────────────────────────────────────
        symbol_box = QGroupBox("Symbol / Interval")
        sym_form = QFormLayout(symbol_box)

        self._symbol_combo = QComboBox()
        self._symbol_combo.addItems(cfg_base.SYMBOLS)
        self._symbol_combo.currentTextChanged.connect(self._on_symbol_changed)
        sym_form.addRow("Symbol:", self._symbol_combo)

        self._interval_combo = QComboBox()
        self._interval_combo.addItems(cfg_base.INTERVALS)
        self._interval_combo.setCurrentText("1m")
        sym_form.addRow("Interval:", self._interval_combo)

        layout.addWidget(symbol_box)

        # ── 策略選取 ──────────────────────────────────────────────────────────
        strategy_box = QGroupBox("Strategy")
        strat_form = QFormLayout(strategy_box)

        self._strategy_combo = QComboBox()
        self._strategy_combo.addItems(sorted(STRATEGY_REGISTRY.keys()))
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

        self._capital_spin = QDoubleSpinBox()
        self._capital_spin.setRange(100, 10_000_000)
        self._capital_spin.setValue(10_000)
        self._capital_spin.setSingleStep(1000)
        bt_form.addRow("Capital (USDT):", self._capital_spin)

        self._leverage_spin = QSpinBox()
        self._leverage_spin.setRange(1, 125)
        self._leverage_spin.setValue(20)
        bt_form.addRow("Leverage:", self._leverage_spin)

        self._risk_spin = QDoubleSpinBox()
        self._risk_spin.setRange(0.1, 100)
        self._risk_spin.setValue(2.0)
        self._risk_spin.setSuffix("%")
        bt_form.addRow("Max Risk/Trade:", self._risk_spin)

        self._fee_combo = QComboBox()
        self._fee_combo.addItems(["Taker", "Maker", "100% Maker", "70M/30T", "50M/50T", "自訂"])
        bt_form.addRow("Fee Mode:", self._fee_combo)

        self._slip_spin = QDoubleSpinBox()
        self._slip_spin.setRange(0, 100)
        self._slip_spin.setValue(0.0)
        self._slip_spin.setSuffix(" bps")
        bt_form.addRow("Slippage:", self._slip_spin)

        self._compound_check = QCheckBox("Compound Equity")
        self._compound_check.setChecked(True)
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

    # ── 公開 API ──────────────────────────────────────────────────────────────

    def set_running(self, running: bool) -> None:
        self._run_btn.setEnabled(not running)
        self._cancel_btn.setVisible(running)

    def symbol(self) -> str:
        return self._symbol_combo.currentText()

    def interval(self) -> str:
        return self._interval_combo.currentText()

    def build_bt_config(self) -> BacktestConfig:
        return BacktestConfig(
            initial_capital = self._capital_spin.value(),
            max_loss_pct    = self._risk_spin.value() / 100.0,
            leverage        = self._leverage_spin.value(),
            fee_mode        = self._fee_combo.currentText(),
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
        self._time_slice.load_symbol(symbol)

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
        self.run_requested.emit(strategy, bt_cfg, slices)

    def _on_cancel(self) -> None:
        self.cancel_requested.emit()
