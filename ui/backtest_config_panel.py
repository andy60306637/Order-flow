"""左側回測設定面板：策略選取 + 時間切片 + 回測參數 + Tick 管理。"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
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


class TickImportThread(QThread):
    """將 data.binance.vision aggTrades CSV/ZIP 檔案匯入到本機 tick 快取。"""
    progress_signal = pyqtSignal(str)
    done_signal     = pyqtSignal(int, str)  # (total_count, error_message)

    def __init__(self, symbol: str, paths: list, parent=None) -> None:
        super().__init__(parent)
        self._symbol = symbol
        self._paths  = paths

    def run(self) -> None:
        from core import tick_cache as _tc
        total = 0
        for idx, raw_path in enumerate(self._paths, 1):
            p = Path(raw_path)
            self.progress_signal.emit(f"匯入 {p.name}… ({idx}/{len(self._paths)})")
            try:
                if p.suffix.lower() == ".zip":
                    arr = _tc.from_zip_file(p)
                else:
                    arr = _tc.from_csv_file(p)
                if len(arr) == 0:
                    continue
                st = int(arr[:, 0].min())
                et = int(arr[:, 0].max())
                total = _tc.merge_and_save_array(self._symbol, arr, st, et)
            except Exception as exc:
                self.done_signal.emit(0, f"{p.name}: {exc}")
                return
        self.done_signal.emit(total, "")


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

        # ── Tick 快取管理 ──────────────────────────────────────────────────────
        tick_box = QGroupBox("Tick Cache")
        tick_layout = QVBoxLayout(tick_box)
        tick_layout.setSpacing(4)

        self._tick_status_lbl = QLabel("─")
        self._tick_status_lbl.setStyleSheet("color:#80cbc4; font-size:11px;")
        self._tick_status_lbl.setWordWrap(True)
        tick_layout.addWidget(self._tick_status_lbl)

        _btn_style = (
            "QPushButton { background:#1e222d; color:#d1d4dc; border:1px solid #2a2e39;"
            " border-radius:3px; padding:2px 8px; }"
            "QPushButton:hover { background:#2a2e39; }"
            "QPushButton:disabled { color:#555; }"
        )
        btn_row_tick = QHBoxLayout()
        self._import_tick_btn = QPushButton("📂 匯入 Tick")
        self._import_tick_btn.setToolTip(
            "匯入從 data.binance.vision 下載的 aggTrades CSV/ZIP\n"
            "可 Ctrl+點選多個檔案同時匯入"
        )
        self._import_tick_btn.setStyleSheet(_btn_style)
        self._import_tick_btn.clicked.connect(self._on_import_ticks)
        btn_row_tick.addWidget(self._import_tick_btn)

        self._import_tick_dir_btn = QPushButton("📁 資料夾")
        self._import_tick_dir_btn.setToolTip("選取資料夾，自動匯入其中所有 aggTrades CSV/ZIP")
        self._import_tick_dir_btn.setStyleSheet(_btn_style)
        self._import_tick_dir_btn.clicked.connect(self._on_import_ticks_folder)
        btn_row_tick.addWidget(self._import_tick_dir_btn)

        self._clear_tick_btn = QPushButton("🗑 清除")
        self._clear_tick_btn.setToolTip("刪除本機 Tick 快取檔案以釋放磁碟空間")
        self._clear_tick_btn.setStyleSheet(_btn_style)
        self._clear_tick_btn.clicked.connect(self._on_clear_tick_cache)
        btn_row_tick.addWidget(self._clear_tick_btn)

        tick_layout.addLayout(btn_row_tick)
        layout.addWidget(tick_box)

        self._tick_import_thread: TickImportThread | None = None
        self._refresh_tick_status()

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
        self._refresh_tick_status()

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

    # ── Tick 快取管理 ─────────────────────────────────────────────────────────

    def _refresh_tick_status(self) -> None:
        symbol = self.symbol()
        sources = discover_tick_sources(symbol)
        if not sources:
            self._tick_status_lbl.setText("無 Tick 快取")
            return
        text = self._tick_coverage_text(sources)
        self._tick_status_lbl.setText(text)

    def _on_import_ticks(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "選擇 aggTrades CSV/ZIP", "",
            "Tick Data (*.csv *.zip);;All Files (*)"
        )
        if not paths:
            return
        self._start_tick_import(paths)

    def _on_import_ticks_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "選擇 Tick 資料夾")
        if not folder:
            return
        folder_path = Path(folder)
        paths = [str(p) for p in sorted(folder_path.glob("*.csv")) + sorted(folder_path.glob("*.zip"))]
        if not paths:
            QMessageBox.information(self, "無檔案", f"資料夾中未找到任何 CSV/ZIP 檔案")
            return
        self._start_tick_import(paths)

    def _start_tick_import(self, paths: list) -> None:
        if self._tick_import_thread and self._tick_import_thread.isRunning():
            self._tick_import_thread.wait(500)
        self._import_tick_btn.setEnabled(False)
        self._import_tick_dir_btn.setEnabled(False)
        self._tick_status_lbl.setText("🔄 匯入中…")
        self._tick_import_thread = TickImportThread(self.symbol(), paths, parent=self)
        self._tick_import_thread.progress_signal.connect(
            lambda msg: self._tick_status_lbl.setText(msg)
        )
        self._tick_import_thread.done_signal.connect(self._on_tick_import_done)
        self._tick_import_thread.start()

    def _on_tick_import_done(self, count: int, err: str) -> None:
        self._import_tick_btn.setEnabled(True)
        self._import_tick_dir_btn.setEnabled(True)
        if err:
            self._tick_status_lbl.setText(f"❌ {err}")
        elif count:
            self._refresh_tick_status()
            self._rebuild_tick_dataset_combo(self.symbol())
        else:
            self._tick_status_lbl.setText("⚠ 無資料")

    def _on_clear_tick_cache(self) -> None:
        symbol = self.symbol()
        from core.tick_cache import cache_path
        path = cache_path(symbol)
        if not path.exists():
            self._tick_status_lbl.setText("無快取可清除")
            return
        size_mb = path.stat().st_size / 1024 / 1024
        reply = QMessageBox.question(
            self, "確認刪除",
            f"確定刪除 {symbol} 的 Tick 快取？（{size_mb:.1f} MB）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            path.unlink(missing_ok=True)
            self._refresh_tick_status()
        except Exception as exc:
            self._tick_status_lbl.setText(f"❌ 刪除失敗：{exc}")
