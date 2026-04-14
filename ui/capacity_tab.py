"""
容量分析 UI 元件。

以 QThread 在背景執行多組資金掃描，結果以表格展示。
"""
from __future__ import annotations

import logging
from typing import List, Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QDoubleSpinBox, QSpinBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QProgressBar, QGroupBox, QTextEdit,
)
from PyQt6 import QtGui

import config
from strategies.base import StrategySignal
from backtest.engine import BacktestConfig
from backtest.capacity import CapacityAnalyzer, CapacityConfig, CapacityReport

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 背景掃描執行緒
# ─────────────────────────────────────────────────────────────────────────────

class CapacitySweepThread(QThread):
    """在背景執行容量掃描，完成後發送結果信號。"""
    finished = pyqtSignal(object)   # CapacityReport
    error    = pyqtSignal(str)

    def __init__(
        self,
        signals: List[StrategySignal],
        base_cfg: BacktestConfig,
        cap_cfg: CapacityConfig,
        symbol: str,
        interval: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        # Freeze the list for this run so MainWindow realtime appends do not
        # change the sweep input after the thread starts.
        self._signals  = list(signals)
        self._base_cfg = base_cfg
        self._cap_cfg  = cap_cfg
        self._symbol   = symbol
        self._interval = interval

    def run(self) -> None:
        try:
            analyzer = CapacityAnalyzer()
            report = analyzer.run_sweep(
                self._signals,
                self._base_cfg,
                self._cap_cfg,
                self._symbol,
                self._interval,
            )
            self.finished.emit(report)
        except Exception as exc:
            logger.exception("Capacity sweep failed")
            self.error.emit(str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# 容量分析 Widget（嵌入 Tab）
# ─────────────────────────────────────────────────────────────────────────────

class CapacityTab(QWidget):
    """容量分析頁面，嵌入 MainWindow 的 QTabWidget。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._sweep_thread: Optional[CapacitySweepThread] = None
        self._report: Optional[CapacityReport] = None
        self._build_ui()

    # ── UI 建構 ───────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.setStyleSheet(
            f"QWidget {{ background: {config.COLOR_BG}; color: {config.COLOR_FG}; }}"
            f"QGroupBox {{ border: 1px solid #363a45; border-radius: 4px;"
            f" margin-top: 8px; padding-top: 16px; color: #aaa; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 10px; }}"
            f"QTableWidget {{ background: #1e222d; color: {config.COLOR_FG};"
            f" gridline-color: #2a2e39; font-size: 12px; }}"
            f"QHeaderView::section {{ background: #1e222d; color: {config.COLOR_FG};"
            f" border: 1px solid #2a2e39; padding: 4px; font-weight: bold; }}"
        )

        _spin_style = (
            "QDoubleSpinBox, QSpinBox {"
            " background:#1e222d; color:#d1d4dc;"
            " border:1px solid #2a2e39; border-radius:3px;"
            " padding:2px 6px; min-width:100px; }"
        )

        # ── 參數區 ────────────────────────────────────────────────────
        param_group = QGroupBox("掃描參數")
        param_group.setStyleSheet(param_group.styleSheet() + _spin_style)
        param_layout = QHBoxLayout(param_group)

        form1 = QFormLayout()
        self._eta_spin = QDoubleSpinBox()
        self._eta_spin.setRange(0.01, 10.0)
        self._eta_spin.setValue(1.0)
        self._eta_spin.setSingleStep(0.1)
        self._eta_spin.setDecimals(2)
        self._eta_spin.setToolTip("市場衝擊係數 η（越大表示衝擊越嚴重）")
        form1.addRow("衝擊係數 η:", self._eta_spin)

        self._adv_spin = QSpinBox()
        self._adv_spin.setRange(1, 365)
        self._adv_spin.setValue(30)
        self._adv_spin.setToolTip("計算 ADV 的回看天數")
        form1.addRow("ADV 天數:", self._adv_spin)

        param_layout.addLayout(form1)

        form2 = QFormLayout()
        self._vpr_warn_spin = QDoubleSpinBox()
        self._vpr_warn_spin.setRange(0.001, 1.0)
        self._vpr_warn_spin.setValue(0.01)
        self._vpr_warn_spin.setSingleStep(0.005)
        self._vpr_warn_spin.setDecimals(3)
        self._vpr_warn_spin.setSuffix(" (1%)")
        self._vpr_warn_spin.setToolTip("VPR 警告門檻")
        form2.addRow("VPR 警告:", self._vpr_warn_spin)

        self._vpr_cap_spin = QDoubleSpinBox()
        self._vpr_cap_spin.setRange(0.01, 1.0)
        self._vpr_cap_spin.setValue(0.05)
        self._vpr_cap_spin.setSingleStep(0.01)
        self._vpr_cap_spin.setDecimals(3)
        self._vpr_cap_spin.setSuffix(" (5%)")
        self._vpr_cap_spin.setToolTip("VPR 上限門檻（超過視為容量瓶頸）")
        form2.addRow("VPR 上限:", self._vpr_cap_spin)

        param_layout.addLayout(form2)

        form3 = QFormLayout()
        self._drop_spin = QDoubleSpinBox()
        self._drop_spin.setRange(0.01, 1.0)
        self._drop_spin.setValue(0.20)
        self._drop_spin.setSingleStep(0.05)
        self._drop_spin.setDecimals(2)
        self._drop_spin.setSuffix(" (20%)")
        self._drop_spin.setToolTip("PF 衰退門檻：超過此比例視為容量上限")
        form3.addRow("PF 衰退門檻:", self._drop_spin)

        param_layout.addLayout(form3)
        layout.addWidget(param_group)

        # ── 按鈕列 ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()

        self._run_btn = QPushButton("▶ 開始掃描")
        self._run_btn.setStyleSheet(
            "QPushButton { background:#1e3a1e; color:#26a69a; border:1px solid #26a69a;"
            " border-radius:3px; padding:5px 16px; font-weight:bold; }"
            "QPushButton:hover { background:#1a4a2a; }"
            "QPushButton:disabled { color:#555; border-color:#333; }"
        )
        self._run_btn.clicked.connect(self._on_run_clicked)
        btn_row.addWidget(self._run_btn)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate
        self._progress.setVisible(False)
        self._progress.setMaximumHeight(18)
        self._progress.setStyleSheet(
            "QProgressBar { background:#1e222d; border:1px solid #363a45;"
            " border-radius:3px; text-align:center; color:#aaa; }"
            "QProgressBar::chunk { background:#26a69a; }"
        )
        btn_row.addWidget(self._progress)

        self._status_lbl = QLabel("就緒 — 請先執行回測以取得訊號")
        self._status_lbl.setStyleSheet("color:#aaa; font-size:11px; padding-left:8px;")
        btn_row.addWidget(self._status_lbl)
        btn_row.addStretch()

        layout.addLayout(btn_row)

        # ── 結論區 ────────────────────────────────────────────────────
        self._summary_lbl = QLabel()
        self._summary_lbl.setStyleSheet(
            "font-size:13px; padding:6px 8px; color:#f0c040;"
        )
        self._summary_lbl.setWordWrap(True)
        self._summary_lbl.setVisible(False)
        layout.addWidget(self._summary_lbl)

        # ── 結果表格 ──────────────────────────────────────────────────
        self._headers = [
            "資金 (USDT)", "PF", "勝率%", "最大回撤%",
            "淨利 (USDT)", "報酬率%", "交易數",
            "平均衝擊 (bps)", "最大 VPR", "平均 VPR", "警告數",
        ]
        self._table = QTableWidget(0, len(self._headers))
        self._table.setHorizontalHeaderLabels(self._headers)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self._table, stretch=1)

        # ── 備註區 ────────────────────────────────────────────────────
        self._notes_box = QTextEdit()
        self._notes_box.setReadOnly(True)
        self._notes_box.setMaximumHeight(80)
        self._notes_box.setStyleSheet(
            "QTextEdit { background:#1e222d; color:#80cbc4; border:1px solid #363a45;"
            " border-radius:3px; font-size:11px; padding:4px; }"
        )
        self._notes_box.setVisible(False)
        layout.addWidget(self._notes_box)

    # ── 外部介面 ──────────────────────────────────────────────────────────────

    def set_ready(self, has_signals: bool) -> None:
        """MainWindow 呼叫：通知是否有可用的策略訊號。"""
        if has_signals:
            self._status_lbl.setText("就緒 — 可開始容量掃描")
            self._run_btn.setEnabled(True)
        else:
            self._status_lbl.setText("就緒 — 請先執行回測以取得訊號")
            self._run_btn.setEnabled(False)

    def start_sweep(
        self,
        signals: List[StrategySignal],
        base_cfg: BacktestConfig,
        symbol: str,
        interval: str,
    ) -> None:
        """啟動背景掃描。"""
        if self._sweep_thread is not None and self._sweep_thread.isRunning():
            return

        cap_cfg = CapacityConfig(
            impact_eta=self._eta_spin.value(),
            adv_window_days=self._adv_spin.value(),
            vpr_warn_pct=self._vpr_warn_spin.value(),
            vpr_cap_pct=self._vpr_cap_spin.value(),
            limit_drop_pct=self._drop_spin.value(),
        )

        self._run_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._status_lbl.setText("掃描進行中…")

        self._sweep_thread = CapacitySweepThread(
            signals, base_cfg, cap_cfg, symbol, interval, parent=self,
        )
        self._sweep_thread.finished.connect(self._on_sweep_done)
        self._sweep_thread.error.connect(self._on_sweep_error)
        self._sweep_thread.start()

    # ── 內部 slot ─────────────────────────────────────────────────────────────

    def _on_run_clicked(self) -> None:
        """觸發掃描 — 由 MainWindow 提供實際資料。"""
        # 透過父層 MainWindow 取得 signals 與 config
        mw = self.window()
        if not hasattr(mw, "_strategy_signals") or not mw._strategy_signals:
            self._status_lbl.setText("⚠ 尚未執行回測，無策略訊號")
            return

        signals_snapshot = list(mw._strategy_signals)

        base_cfg = BacktestConfig(
            initial_capital=mw._bt_config_dlg.capital_spin.value(),
            max_loss_pct=mw._bt_config_dlg.loss_spin.value() / 100.0,
            leverage=mw._bt_config_dlg.leverage_spin.value(),
            fee_mode=mw._bt_config_dlg.fee_combo.currentText(),
            custom_fee_rate=mw._bt_config_dlg.custom_fee_spin.value() / 100.0,
            slippage_bps=mw._bt_config_dlg.slippage_spin.value(),
            funding_rate=mw._bt_config_dlg.funding_spin.value(),
            maint_margin=mw._bt_config_dlg.maint_spin.value(),
            compound=mw._bt_config_dlg.compound_combo.currentIndex() == 0,
        )

        if getattr(mw, "_strategy_realtime", False):
            self._status_lbl.setText(
                f"已鎖定 {len(signals_snapshot)} 筆 signal 快照；即時訊號不會影響本次容量掃描"
            )

        self.start_sweep(
            signals_snapshot,
            base_cfg,
            mw._symbol,
            mw._interval,
        )

    def _on_sweep_done(self, report: CapacityReport) -> None:
        """掃描完成。"""
        self._report = report
        self._progress.setVisible(False)
        self._run_btn.setEnabled(True)
        self._status_lbl.setText(
            f"掃描完成 — {len(report.points)} 個資金水位"
        )
        self._fill_table(report)
        self._show_summary(report)
        self._show_notes(report)

    def _on_sweep_error(self, msg: str) -> None:
        """掃描出錯。"""
        self._progress.setVisible(False)
        self._run_btn.setEnabled(True)
        self._status_lbl.setText(f"⚠ 掃描失敗: {msg}")

    # ── 表格填充 ──────────────────────────────────────────────────────────────

    def _fill_table(self, report: CapacityReport) -> None:
        pts = report.points
        self._table.setRowCount(len(pts))

        for i, pt in enumerate(pts):
            vals = [
                f"{pt.capital:,.0f}",
                f"{pt.profit_factor:.2f}" if pt.profit_factor != float("inf") else "∞",
                f"{pt.win_rate:.1f}",
                f"{pt.max_drawdown_pct:.2f}",
                f"{pt.total_net_pnl:,.2f}",
                f"{pt.total_return_pct:.2f}",
                str(pt.trades),
                f"{pt.avg_impact_bps:.2f}",
                f"{pt.max_vpr:.4f}",
                f"{pt.avg_vpr:.4f}",
                str(pt.warning_count),
            ]

            # 判斷此行是否超限
            is_over_cap = False
            if report.capacity_limit_usdt is not None:
                is_over_cap = pt.capital > report.capacity_limit_usdt

            for col, val in enumerate(vals):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

                # 標色
                if is_over_cap:
                    item.setForeground(QtGui.QColor("#ef5350"))
                elif pt.capital == report.recommended_capital:
                    item.setForeground(QtGui.QColor("#26a69a"))
                elif col == 4:  # 淨利
                    color = "#26a69a" if pt.total_net_pnl >= 0 else "#ef5350"
                    item.setForeground(QtGui.QColor(color))

                self._table.setItem(i, col, item)

    # ── 摘要結論 ──────────────────────────────────────────────────────────────

    def _show_summary(self, report: CapacityReport) -> None:
        parts = []
        parts.append(f"基準 PF: {report.baseline_profit_factor:.2f}"
                      if report.baseline_profit_factor != float("inf")
                      else "基準 PF: ∞")

        if report.capacity_limit_usdt is not None:
            parts.append(f"容量上限: {report.capacity_limit_usdt:,.0f} USDT")
        else:
            parts.append("容量上限: 未達衰退門檻（所有水位均可行 或 全部超限）")

        if report.recommended_capital is not None:
            parts.append(f"建議資金: {report.recommended_capital:,.0f} USDT")

        self._summary_lbl.setText("  |  ".join(parts))
        self._summary_lbl.setVisible(True)

    def _show_notes(self, report: CapacityReport) -> None:
        if report.notes:
            self._notes_box.setPlainText("\n".join(report.notes))
            self._notes_box.setVisible(True)
        else:
            self._notes_box.setVisible(False)
