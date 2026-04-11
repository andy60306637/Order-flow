"""
主視窗：整合所有元件與資料流。

布局（深色 QSplitter）：
  ┌─── Toolbar ─────────────────────────────────────────────────┐
  │ [Symbol ▼] [Interval ▼] | [View: Kline|Footprint] [Mode ▼] │
  ├──────────────┬──────────────────────────────────────────────┤
  │              │  QTabWidget: [Kline] [Footprint]             │
  │  Order Book  │  ─────────────────────────────────────────   │
  │  (Level 2)   │  CVD Chart                                   │
  ├──────────────┤                                              │
  │  OB Heatmap  │                                              │
  └──────────────┴──────────────────────────────────────────────┘
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional, List

from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QSplitter, QTabWidget,
    QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLabel,
    QToolBar, QFrame, QSizePolicy, QPushButton,
    QDialog, QDialogButtonBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QSpinBox, QDoubleSpinBox, QFileDialog, QMessageBox,
)
from PyQt6.QtGui import QAction, QColor
from PyQt6 import QtGui

import config
from core.data_types import Trade, Kline
from core.order_book import OrderBook
from core.cvd_calculator import CvdCalculator
from core.footprint_builder import FootprintBuilder
from core.ws_client import WsWorkerThread
from core.history_processor import HistoryProcessorThread
from core import kline_cache, tick_cache
from strategies import STRATEGY_REGISTRY
from strategies.base import StrategyBase, StrategySignal
from ui.order_book_widget import OrderBookWidget
from ui.kline_chart import KlineChart
from ui.cvd_chart import CvdChart, StatsPanel
from ui.heatmap_widget import HeatmapWidget
from ui.footprint_widget import FootprintChart

logger = logging.getLogger(__name__)

_INTERVAL_MS_MAP = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
    "3d": 259_200_000, "1w": 604_800_000,
}


def _interval_ms(interval: str) -> int:
    return _INTERVAL_MS_MAP.get(interval, 60_000)


# ─────────────────────────────────────────────────────────────────────────────
# Tick 匯入執行緒（背景解析 CSV/ZIP 並寫入快取）
# ─────────────────────────────────────────────────────────────────────────────
class TickImportThread(QThread):
    """將 data.binance.vision aggTrades CSV/ZIP 檔案匯入到本機 tick 快取。"""
    progress_signal = pyqtSignal(str)       # 狀態訊息
    done_signal     = pyqtSignal(int, str)  # (total_count, error_message)

    def __init__(self, symbol: str, interval: str,
                 paths: list, parent=None) -> None:
        super().__init__(parent)
        self._symbol   = symbol
        self._interval = interval
        self._paths    = paths

    def run(self) -> None:
        from core import tick_cache as _tc
        total = 0
        for idx, raw_path in enumerate(self._paths, 1):
            p = Path(raw_path)
            self.progress_signal.emit(
                f"匯入 {p.name}… ({idx}/{len(self._paths)})"
            )
            try:
                if p.suffix.lower() == ".zip":
                    arr = _tc.from_zip_file(p)
                else:
                    arr = _tc.from_csv_file(p)
                if len(arr) == 0:
                    continue
                st = int(arr[:, 0].min())
                et = int(arr[:, 0].max())
                total = _tc.merge_and_save_array(
                    self._symbol, self._interval, arr, st, et
                )
            except Exception as exc:
                self.done_signal.emit(0, f"{p.name}: {exc}")
                return
        self.done_signal.emit(total, "")


# ═══════════════════════════════════════════════════════════════════
# 回測結果對話框
# ═══════════════════════════════════════════════════════════════════

class BacktestResultDialog(QDialog):
    """顯示策略回測統計摘要與逐筆交易明細（含市場時區/月份篩選）。"""

    # UTC 小時區間（h_start <= hour < h_end）
    SESSIONS = {
        "全時間": None,
        "亞洲盤": (0, 8),
        "倫敦盤": (8, 16),
        "紐約盤": (13, 21),
    }

    @staticmethod
    def _fmt_pf(v: float) -> str:
        return "∞" if v == float("inf") else f"{v:.2f}"

    @staticmethod
    def _trade_month(t: dict) -> str:
        from datetime import datetime
        ts = t.get("entry_time", 0)
        if not ts:
            return ""
        dt = datetime.fromtimestamp(ts / 1000, tz=config.DISPLAY_TZ)
        return f"{dt.year}-{dt.month:02d}"

    @staticmethod
    def _in_session(t: dict, h_range) -> bool:
        from datetime import datetime, timezone
        ts = t.get("entry_time", 0)
        if not ts:
            return True
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        h = dt.hour
        h_start, h_end = h_range
        return h_start <= h < h_end if h_start < h_end else (h >= h_start or h < h_end)

    # ─────────────────────────────────────────────────────────────────
    def __init__(self, stats: dict, parent=None) -> None:
        super().__init__(parent)
        self._stats = stats
        self._full_trade_list = stats.get("trade_list", [])
        self.setWindowTitle("回測結果")
        self.setMinimumSize(1020, 620)
        self.setStyleSheet(
            f"QDialog {{ background: {config.COLOR_BG}; color: {config.COLOR_FG}; }}"
            f"QTableWidget {{ background: #1e222d; color: {config.COLOR_FG};"
            f" gridline-color: #2a2e39; font-size: 12px; }}"
            f"QHeaderView::section {{ background: #1e222d; color: {config.COLOR_FG};"
            f" border: 1px solid #2a2e39; padding: 4px; font-weight: bold; }}"
        )

        layout = QVBoxLayout(self)

        # ── 回測參數回顯 ─────────────────────────────────────────────
        cap  = stats.get("initial_capital", 0)
        lev  = stats.get("leverage", 0)
        fm   = stats.get("fee_mode", "")
        fr   = stats.get("fee_rate", 0)
        mlp  = stats.get("max_loss_pct", 0)
        slip = stats.get("slippage_bps", 0.0)
        fund = stats.get("funding_rate", 0.0)
        feq  = stats.get("final_equity", 0.0)
        ret  = stats.get("total_return_pct", 0.0)
        ret_c = config.COLOR_UP if ret >= 0 else config.COLOR_DOWN
        ret_s = f"+{ret:.2f}" if ret >= 0 else f"{ret:.2f}"
        # 費率展示：模式 + 實際費率％
        fee_display = f"{fm} ({fr*100:.4f}%)" if fm not in ("Maker", "Taker") else f"{fm} ({fr*100:.2f}%)"
        cfg_lbl = QLabel(
            f"<b>資金:</b> {cap:,.0f} U &nbsp;|&nbsp; "
            f"<b>槓桿:</b> {lev}x &nbsp;|&nbsp; "
            f"<b>費率:</b> {fee_display} &nbsp;|&nbsp; "
            f"<b>損失上限:</b> {mlp*100:.1f}% &nbsp;|&nbsp; "
            f"<b>滑價:</b> {slip:.1f} bps &nbsp;|&nbsp; "
            f"<b>資金費率:</b> {fund:.4f}/8h &nbsp;|&nbsp; "
            f"<b>最終餘額:</b> {feq:,.2f} U "
            f"<span style='color:{ret_c}'>({ret_s}%)</span>"
        )
        cfg_lbl.setStyleSheet("font-size: 12px; padding: 4px 8px; color: #aaa;")
        layout.addWidget(cfg_lbl)

        # ── 過濾列：市場時區 + 月份 ──────────────────────────────────
        _filter_style = (
            "QComboBox { background: #1e222d; color: #d1d4dc; border: 1px solid #363a45;"
            " border-radius: 3px; padding: 2px 6px; min-width: 90px; }"
            "QLabel { color: #aaa; font-size: 12px; }"
        )
        filter_w = QWidget()
        filter_w.setStyleSheet(_filter_style)
        filter_row = QHBoxLayout(filter_w)
        filter_row.setContentsMargins(4, 2, 4, 2)
        filter_row.addWidget(QLabel("市場時區:"))
        self._session_combo = QComboBox()
        for s in self.SESSIONS:
            self._session_combo.addItem(s)
        filter_row.addWidget(self._session_combo)
        filter_row.addSpacing(16)
        filter_row.addWidget(QLabel("月份:"))
        self._month_combo = QComboBox()
        self._populate_month_combo()
        filter_row.addWidget(self._month_combo)
        filter_row.addStretch()
        layout.addWidget(filter_w)

        # ── 摘要統計表（16 欄）────────────────────────────────────────
        self._sum_headers = [
            "策略", "交易數", "勝率", "PF", "淨利(USDT)", "手續費",
            "最大回撤", "最大連虧", "平均獲利", "平均虧損",
            "多單 PF", "空單 PF", "SL", "TP", "TS", "TD",
        ]
        self._sum_table = QTableWidget(1, len(self._sum_headers))
        self._sum_table.setHorizontalHeaderLabels(self._sum_headers)
        self._sum_table.verticalHeader().setVisible(False)
        self._sum_table.setMaximumHeight(58)
        self._sum_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._sum_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self._sum_table)

        self._warn_lbl = QLabel()
        self._warn_lbl.setStyleSheet("color:#f0c040; font-size:12px; padding:2px 8px;")
        self._warn_lbl.setVisible(False)
        layout.addWidget(self._warn_lbl)

        # ── 匯出按鈕 ─────────────────────────────────────────────────
        _exp_btn = QPushButton("⬇ 匯出 Excel")
        _exp_btn.setStyleSheet(
            "QPushButton { background:#1e3a1e; color:#26a69a; border:1px solid #26a69a;"
            " border-radius:3px; padding:3px 10px; }"
            "QPushButton:hover { background:#1a4a2a; }"
        )
        _exp_btn.clicked.connect(self._export_excel)
        _btn_row = QHBoxLayout()
        _btn_row.addStretch()
        _btn_row.addWidget(_exp_btn)
        layout.addLayout(_btn_row)

        # ── 交易明細表 ────────────────────────────────────────────────
        self._trade_cols = [
            "#", "方向", "入場時間", "入場價", "出場類型",
            "出場價", "數量", "手續費", "資金費", "淨利(USDT)", "餘額",
        ]
        self._trade_table = QTableWidget(0, len(self._trade_cols))
        self._trade_table.setHorizontalHeaderLabels(self._trade_cols)
        self._trade_table.verticalHeader().setVisible(False)
        self._trade_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._trade_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._trade_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self._trade_table)

        # ── 連接篩選信號 + 初次填充 ──────────────────────────────────
        self._session_combo.currentIndexChanged.connect(self._apply_filter)
        self._month_combo.currentIndexChanged.connect(self._apply_filter)
        self._apply_filter()

    # ── 過濾邏輯 ─────────────────────────────────────────────────────
    def _populate_month_combo(self) -> None:
        months = sorted(set(
            self._trade_month(t)
            for t in self._full_trade_list
            if not t.get("skipped") and self._trade_month(t)
        ))
        self._month_combo.addItem("全部月份")
        for m in months:
            self._month_combo.addItem(m)

    def _filtered_trades(self) -> list:
        trades = self._full_trade_list
        sess_range = self.SESSIONS.get(self._session_combo.currentText())
        if sess_range:
            trades = [t for t in trades if self._in_session(t, sess_range)]
        month = self._month_combo.currentText()
        if month != "全部月份":
            trades = [t for t in trades if self._trade_month(t) == month]
        return trades

    def _apply_filter(self) -> None:
        trades = self._filtered_trades()
        from backtest.engine import compute_subset_stats
        sub = compute_subset_stats(trades)
        sub["strategy_name"] = self._stats.get("strategy_name", "─")
        opens = self._stats.get("open_count", 0)
        self._refresh_summary(sub, opens)
        self._refresh_trades(trades)

    # ── 摘要表重新整理 ───────────────────────────────────────────────
    def _refresh_summary(self, s: dict, opens: int = 0) -> None:
        n    = s.get("trades", 0)
        net  = s.get("total_net_pnl", 0.0)
        net_s = f"+{net:,.2f}" if net >= 0 else f"{net:,.2f}"
        row_data = [
            s.get("strategy_name", "─"),
            str(n),
            f"{s.get('win_rate', 0.0):.1f}%",
            self._fmt_pf(s.get("profit_factor", 0.0)),
            net_s,
            f"{s.get('total_fees', 0.0):,.2f}",
            f"{s.get('max_drawdown_pct', 0.0):.2f}%",
            str(s.get("max_consec_loss", 0)),
            f"+{s.get('avg_win', 0.0):,.2f}" if s.get("avg_win", 0) > 0 else "─",
            f"-{s.get('avg_loss', 0.0):,.2f}" if s.get("avg_loss", 0) > 0 else "─",
            self._fmt_pf(s.get("long_profit_factor", 0.0)),
            self._fmt_pf(s.get("short_profit_factor", 0.0)),
            str(s.get("sl_count", 0)),
            str(s.get("tp_count", 0)),
            str(s.get("ts_count", 0)),
            str(s.get("td_count", 0)),
        ]
        for col, val in enumerate(row_data):
            item = QTableWidgetItem(val)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if col == 4:
                item.setForeground(QtGui.QColor(
                    config.COLOR_UP if net >= 0 else config.COLOR_DOWN
                ))
            self._sum_table.setItem(0, col, item)

        if opens:
            self._warn_lbl.setText(f"  ⚠ 未平倉: {opens} 筆")
            self._warn_lbl.setVisible(True)
        else:
            self._warn_lbl.setVisible(False)

    # ── 明細表重新整理 ───────────────────────────────────────────────
    def _refresh_trades(self, trades: list) -> None:
        from datetime import datetime
        active = [t for t in trades if not t.get("skipped")]
        self._trade_table.setRowCount(len(active))
        if not active:
            return

        _label_colors = {
            "SL": "#ef5350", "TP": "#26a69a",
            "TS": "#ff9800", "TD": "#9c27b0",
        }
        for i, t in enumerate(active):
            self._trade_table.setItem(i, 0, QTableWidgetItem(str(i + 1)))

            dir_txt = "做多" if t["dir"] == "long" else "做空"
            dir_item = QTableWidgetItem(dir_txt)
            dir_item.setForeground(
                QtGui.QColor(config.COLOR_UP if t["dir"] == "long" else config.COLOR_DOWN)
            )
            self._trade_table.setItem(i, 1, dir_item)

            # 入場時間（DISPLAY_TZ）
            ets = t.get("entry_time", 0)
            if ets:
                dt = datetime.fromtimestamp(ets / 1000, tz=config.DISPLAY_TZ)
                time_str = dt.strftime("%m-%d %H:%M")
            else:
                time_str = "─"
            self._trade_table.setItem(i, 2, QTableWidgetItem(time_str))

            self._trade_table.setItem(i, 3, QTableWidgetItem(f"{t['entry']:,.4f}"))

            exit_lbl = t.get("exit_label", "")
            lbl_item = QTableWidgetItem(exit_lbl)
            lbl_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if exit_lbl in _label_colors:
                lbl_item.setForeground(QtGui.QColor(_label_colors[exit_lbl]))
            self._trade_table.setItem(i, 4, lbl_item)

            self._trade_table.setItem(i, 5, QTableWidgetItem(f"{t['exit']:,.4f}"))
            self._trade_table.setItem(i, 6, QTableWidgetItem(f"{t.get('qty', 0):,.6f}"))
            self._trade_table.setItem(i, 7, QTableWidgetItem(f"{t.get('total_fee', 0):,.2f}"))
            self._trade_table.setItem(i, 8, QTableWidgetItem(f"{t.get('funding_cost', 0):,.2f}"))

            pv = t.get("net_pnl", 0.0)
            pnl_txt = f"+{pv:,.2f}" if pv >= 0 else f"{pv:,.2f}"
            pnl_item = QTableWidgetItem(pnl_txt)
            pnl_item.setForeground(
                QtGui.QColor(config.COLOR_UP if pv >= 0 else config.COLOR_DOWN)
            )
            self._trade_table.setItem(i, 9, pnl_item)
            self._trade_table.setItem(i, 10, QTableWidgetItem(f"{t.get('equity_after', 0):,.2f}"))

    # ── Excel 匯出 ───────────────────────────────────────────────────
    def _export_excel(self) -> None:
        """將回測摘要與交易明細匯出為 Excel 檔案。"""
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment
            from openpyxl.utils import get_column_letter
        except ImportError:
            QMessageBox.warning(self, "缺少套件",
                                "請先安裝 openpyxl：\npip install openpyxl")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "匯出 Excel", "backtest_result.xlsx",
            "Excel 檔案 (*.xlsx)"
        )
        if not path:
            return

        s = self._stats
        h_font   = Font(bold=True, color="FFFFFF")
        h_fill   = PatternFill("solid", fgColor="2962FF")
        s_fill   = PatternFill("solid", fgColor="1e222d")
        center   = Alignment(horizontal="center", vertical="center")

        wb = openpyxl.Workbook()

        # ── Sheet 1: 摘要 ──────────────────────────────────────────
        ws1 = wb.active
        ws1.title = "摘要"

        param_heads = ["資金(U)", "槓桿", "費率模式", "費率%",
                       "損失上限%", "滑價(bps)", "資金費率/8h",
                       "最終餘額", "報酬率%"]
        for col, h in enumerate(param_heads, 1):
            c = ws1.cell(row=1, column=col, value=h)
            c.font, c.fill, c.alignment = h_font, h_fill, center

        param_vals = [
            s.get("initial_capital", 0),
            s.get("leverage", 0),
            s.get("fee_mode", ""),
            round(s.get("fee_rate", 0) * 100, 4),
            round(s.get("max_loss_pct", 0) * 100, 1),
            s.get("slippage_bps", 0.0),
            s.get("funding_rate", 0.0),
            round(s.get("final_equity", 0.0), 2),
            round(s.get("total_return_pct", 0.0), 2),
        ]
        for col, val in enumerate(param_vals, 1):
            ws1.cell(row=2, column=col, value=val).alignment = center

        def _pf(v):
            return "∞" if v == float("inf") else round(v, 2)

        sum_heads = ["策略", "交易數", "勝率%", "PF",
                     "淨利(USDT)", "手續費",
                     "最大回撤%", "最大連虧",
                     "平均獲利", "平均虧損",
                     "多單PF", "空單PF",
                     "SL", "TP", "TS", "TD"]
        for col, h in enumerate(sum_heads, 1):
            c = ws1.cell(row=4, column=col, value=h)
            c.font, c.fill, c.alignment = h_font, s_fill, center

        sum_vals = [
            s.get("strategy_name", ""),
            s.get("trades", 0),
            round(s.get("win_rate", 0.0), 1),
            _pf(s.get("profit_factor", 0.0)),
            round(s.get("total_net_pnl", 0.0), 2),
            round(s.get("total_fees", 0.0), 2),
            round(s.get("max_drawdown_pct", 0.0), 2),
            s.get("max_consec_loss", 0),
            round(s.get("avg_win", 0.0), 2),
            round(s.get("avg_loss", 0.0), 2),
            _pf(s.get("long_profit_factor", 0.0)),
            _pf(s.get("short_profit_factor", 0.0)),
            s.get("sl_count", 0),
            s.get("tp_count", 0),
            s.get("ts_count", 0),
            s.get("td_count", 0),
        ]
        for col, val in enumerate(sum_vals, 1):
            cell = ws1.cell(row=5, column=col, value=val)
            cell.alignment = center
            if col == 5:
                net = s.get("total_net_pnl", 0.0)
                cell.font = Font(color="26A69A" if net >= 0 else "EF5350")

        for col in range(1, max(len(param_heads), len(sum_heads)) + 1):
            ws1.column_dimensions[get_column_letter(col)].width = 15

        # ── Sheet 2: 交易明細（當前篩選）─────────────────────────────
        ws2 = wb.create_sheet("交易明細")
        trade_heads = ["#", "方向", "入場時間", "入場價", "出場類型",
                       "出場價", "數量", "手續費",
                       "資金費", "淨利(USDT)", "餘額"]
        for col, h in enumerate(trade_heads, 1):
            c = ws2.cell(row=1, column=col, value=h)
            c.font, c.fill, c.alignment = h_font, h_fill, center

        from datetime import datetime as _dt
        filtered = self._filtered_trades()
        active = [t for t in filtered if not t.get("skipped")]
        for i, t in enumerate(active, 1):
            dir_txt = "做多" if t["dir"] == "long" else "做空"
            pv = t.get("net_pnl", 0.0)
            ets = t.get("entry_time", 0)
            time_str = _dt.fromtimestamp(ets / 1000, tz=config.DISPLAY_TZ).strftime(
                "%Y-%m-%d %H:%M") if ets else "─"
            row_vals = [
                i, dir_txt, time_str,
                round(t.get("entry", 0), 4),
                t.get("exit_label", ""),
                round(t.get("exit", 0), 4),
                round(t.get("qty", 0), 6),
                round(t.get("total_fee", 0), 2),
                round(t.get("funding_cost", 0), 2),
                round(pv, 2),
                round(t.get("equity_after", 0), 2),
            ]
            for col, val in enumerate(row_vals, 1):
                cell = ws2.cell(row=i + 1, column=col, value=val)
                cell.alignment = center
                if col == 10:
                    cell.font = Font(color="26A69A" if pv >= 0 else "EF5350")

        for col in range(1, len(trade_heads) + 1):
            ws2.column_dimensions[get_column_letter(col)].width = 14

        wb.save(path)
        QMessageBox.information(self, "匯出成功", f"已儲存至:\n{path}")


# ═══════════════════════════════════════════════════════════════════
# 回測參數設定對話框
# ═══════════════════════════════════════════════════════════════════

class BacktestConfigDialog(QDialog):
    """回測參數設定子頁面（可重複呼叫，非強制 modal）。"""

    _SPIN_STYLE = (
        "QDoubleSpinBox, QSpinBox, QComboBox {"
        " background:#1e222d; color:#d1d4dc;"
        " border:1px solid #2a2e39; border-radius:3px;"
        " padding:2px 6px; min-width:120px; }"
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("回測參數設定")
        self.setMinimumWidth(340)
        self.setStyleSheet(
            f"QDialog {{ background: {config.COLOR_BG}; color: {config.COLOR_FG}; }}"
            "QLabel { font-size: 13px; }"
        )

        form = QFormLayout(self)
        form.setContentsMargins(20, 16, 20, 16)
        form.setVerticalSpacing(10)

        self.capital_spin = QDoubleSpinBox()
        self.capital_spin.setRange(100, 10_000_000)
        self.capital_spin.setValue(10_000)
        self.capital_spin.setDecimals(0)
        self.capital_spin.setSuffix(" USDT")
        self.capital_spin.setStyleSheet(self._SPIN_STYLE)
        form.addRow("初始資金:", self.capital_spin)

        self.loss_spin = QDoubleSpinBox()
        self.loss_spin.setRange(0.1, 50.0)
        self.loss_spin.setValue(2.0)
        self.loss_spin.setDecimals(1)
        self.loss_spin.setSuffix(" %")
        self.loss_spin.setStyleSheet(self._SPIN_STYLE)
        form.addRow("每筆最高損失:", self.loss_spin)

        self.leverage_spin = QSpinBox()
        self.leverage_spin.setRange(1, 125)
        self.leverage_spin.setValue(20)
        self.leverage_spin.setSuffix(" x")
        self.leverage_spin.setStyleSheet(self._SPIN_STYLE)
        form.addRow("槓桿:", self.leverage_spin)

        self.fee_combo = QComboBox()
        self.fee_combo.addItems(["Taker", "Maker", "100% Maker", "70M/30T", "50M/50T", "自訂"])
        self.fee_combo.setToolTip(
            "Taker=0.05%  Maker=0.02%\n"
            "成本情境測試:\n"
            "  100% Maker = 0.02%\n"
            "  70M/30T = 0.029%\n"
            "  50M/50T = 0.035%\n"
            "自訂：直接輸入費率（%）"
        )
        self.fee_combo.setStyleSheet(self._SPIN_STYLE)
        form.addRow("費率模式:", self.fee_combo)

        self.custom_fee_spin = QDoubleSpinBox()
        self.custom_fee_spin.setRange(0.0, 1.0)
        self.custom_fee_spin.setValue(0.05)
        self.custom_fee_spin.setDecimals(4)
        self.custom_fee_spin.setSingleStep(0.001)
        self.custom_fee_spin.setSuffix(" %")
        self.custom_fee_spin.setToolTip("自訂手續費率（例：0.05 = 0.05%）")
        self.custom_fee_spin.setStyleSheet(self._SPIN_STYLE)
        self._custom_fee_row_label = QLabel("自訂費率:")
        form.addRow(self._custom_fee_row_label, self.custom_fee_spin)
        self._custom_fee_row_label.setVisible(False)
        self.custom_fee_spin.setVisible(False)

        self.fee_combo.currentTextChanged.connect(self._on_fee_mode_changed)

        self.slippage_spin = QDoubleSpinBox()
        self.slippage_spin.setRange(0.0, 50.0)
        self.slippage_spin.setValue(0.0)
        self.slippage_spin.setDecimals(1)
        self.slippage_spin.setSuffix(" bps")
        self.slippage_spin.setToolTip("滞dge (1 bps = 0.01%)")
        self.slippage_spin.setStyleSheet(self._SPIN_STYLE)
        form.addRow("滑價:", self.slippage_spin)

        self.funding_spin = QDoubleSpinBox()
        self.funding_spin.setRange(0.0, 1.0)
        self.funding_spin.setValue(0.0)
        self.funding_spin.setDecimals(4)
        self.funding_spin.setSingleStep(0.0001)
        self.funding_spin.setSuffix(" /8h")
        self.funding_spin.setToolTip("資金費率 (0.01% = 0.0001)；正値多付空收")
        self.funding_spin.setStyleSheet(self._SPIN_STYLE)
        form.addRow("資金費率:", self.funding_spin)

        self.maint_spin = QDoubleSpinBox()
        self.maint_spin.setRange(0.001, 0.5)
        self.maint_spin.setValue(0.005)
        self.maint_spin.setDecimals(3)
        self.maint_spin.setSingleStep(0.001)
        self.maint_spin.setToolTip("維持保證金率 (0.5% = 0.005)")
        self.maint_spin.setStyleSheet(self._SPIN_STYLE)
        form.addRow("維持保證金率:", self.maint_spin)

        self.compound_combo = QComboBox()
        self.compound_combo.addItems(["複利（動態資金）", "固定（初始資金）"])
        self.compound_combo.setToolTip(
            "複利：每筆依當前資產計算倉位 (equity) — PnL 複利\n"
            "固定：每筆依初始資金計算倉位，不受盈虧影響"
        )
        self.compound_combo.setStyleSheet(self._SPIN_STYLE)
        form.addRow("倉位模式:", self.compound_combo)

        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn_box.rejected.connect(self.hide)
        form.addRow(btn_box)

    def _on_fee_mode_changed(self, mode: str) -> None:
        visible = (mode == "自訂")
        self._custom_fee_row_label.setVisible(visible)
        self.custom_fee_spin.setVisible(visible)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("OrderFlow — Binance Futures")
        self.resize(1600, 900)

        # ── 狀態 ─────────────────────────────────────────────────────────────
        self._symbol   = config.DEFAULT_SYMBOL
        self._interval = config.DEFAULT_INTERVAL
        self._current_kline_open_time: int = 0
        self._last_price: float = 0.0
        self._loaded_klines: list = []   # 最近一次歷史 K 線，供 agg_history 使用
        self._kline_timestamps: List[int] = []  # kline open_time 序列，供 footprint x 軸對齊

        # ── 資料層 ────────────────────────────────────────────────────────────
        self._order_book = OrderBook()
        self._cvd_calc   = CvdCalculator()
        self._fp_builder = FootprintBuilder()

        self._ws_thread: Optional[WsWorkerThread] = None
        self._history_proc: Optional[HistoryProcessorThread] = None

        # ── 節流更新旗標 ──────────────────────────────────────────────────────
        self._dirty_cvd: bool = False
        self._dirty_fp: bool  = False
        self._last_trade_time: float = 0.0  # monotonic time of last aggTrade
        self._data_stale: bool = False       # 是否處於數據過期狀態
        # ── 策略測試狀態 ────────────────────────────────────────────
        self._strategy_engine: Optional[StrategyBase] = None
        self._strategy_realtime: bool = False
        self._strategy_signals: List[StrategySignal] = []
        self._tick_import_thread: Optional[TickImportThread] = None
        # ── UI ────────────────────────────────────────────────────────────────
        self._build_ui()
        self._build_toolbar()
        self._refresh_tick_label()
        self._bt_config_dlg = BacktestConfigDialog(self)

        # ── Heatmap timer ─────────────────────────────────────────────────────
        self._heatmap_timer = QTimer(self)
        self._heatmap_timer.setInterval(config.HEATMAP_UPDATE_MS)
        self._heatmap_timer.timeout.connect(self._snapshot_heatmap)

        # ── 節流刷新 timer（150ms，避免每筆 trade 都重繪導致閃爍）─────────────
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(150)
        self._flush_timer.timeout.connect(self._flush_updates)

        # ── 數據過期偵測 timer（每秒檢查一次，超過 5 秒無成交顯示警告）────────
        self._stale_timer = QTimer(self)
        self._stale_timer.setInterval(1000)
        self._stale_timer.timeout.connect(self._check_data_stale)

        # ── 啟動 ─────────────────────────────────────────────────────────────
        # K 線圖左滾觸發歷史載入（只連接一次，不隨 _start_stream 重建）
        self._kline_chart.need_more_history.connect(self._on_need_more_history)

        # ── 十字線同步 ────────────────────────────────────────────────────────
        self._crosshair_charts = [
            self._kline_chart, self._fp_chart, self._cvd_chart,
        ]
        for chart in self._crosshair_charts:
            chart.crosshair_moved.connect(self._sync_crosshair)
            chart.crosshair_left.connect(self._hide_all_crosshairs)

        self._start_stream()
        self._refresh_cache_label()

    # ══════════════════════════════════════════════════════════════
    # UI 建構
    # ══════════════════════════════════════════════════════════════

    def _build_toolbar(self) -> None:
        tb = QToolBar("主工具列", self)
        tb.setMovable(False)
        tb.setStyleSheet("QToolBar { spacing: 8px; padding: 4px; }")
        self.addToolBar(tb)

        # Symbol
        tb.addWidget(QLabel("交易對 "))
        self._sym_combo = QComboBox()
        self._sym_combo.addItems(config.SYMBOLS)
        self._sym_combo.setCurrentText(self._symbol)
        self._sym_combo.currentTextChanged.connect(self._on_symbol_changed)
        tb.addWidget(self._sym_combo)

        tb.addSeparator()

        # Interval
        tb.addWidget(QLabel("週期 "))
        self._iv_combo = QComboBox()
        self._iv_combo.addItems(config.INTERVALS)
        self._iv_combo.setCurrentText(self._interval)
        self._iv_combo.currentTextChanged.connect(self._on_interval_changed)
        tb.addWidget(self._iv_combo)

        tb.addSeparator()

        # Footprint mode
        tb.addWidget(QLabel("Footprint "))
        self._fp_combo = QComboBox()
        self._fp_combo.addItems(config.FOOTPRINT_MODES)
        self._fp_combo.currentTextChanged.connect(self._on_fp_mode_changed)
        tb.addWidget(self._fp_combo)

        tb.addSeparator()

        # Tick 聚合倍數
        tb.addWidget(QLabel("Tick "))
        self._tick_combo = QComboBox()
        for m in config.TICK_MULTIPLIERS:
            self._tick_combo.addItem(f"{m}x")
        self._tick_combo.setCurrentIndex(
            config.TICK_MULTIPLIERS.index(config.DEFAULT_TICK_MULTIPLIER)
        )
        self._tick_combo.currentIndexChanged.connect(self._on_tick_multiplier_changed)
        tb.addWidget(self._tick_combo)

        tb.addSeparator()

        # Log scale 切換按鈕
        self._log_btn = QPushButton("Log")
        self._log_btn.setCheckable(True)
        self._log_btn.setFixedWidth(46)
        self._log_btn.setToolTip("切換 K 線 Y 軸：線性 ↔ 對數")
        self._log_btn.setStyleSheet(
            "QPushButton { background:#1e222d; color:#d1d4dc; border:1px solid #2a2e39;"
            " border-radius:3px; padding:2px 6px; }"
            "QPushButton:checked { background:#2962ff; color:#fff; }"
        )
        self._log_btn.toggled.connect(self._on_log_toggled)
        tb.addWidget(self._log_btn)

        tb.addSeparator()

        # ── 策略測試區域 ────────────────────────────────────────────────────
        tb.addWidget(QLabel("策略 "))
        self._strategy_combo = QComboBox()
        self._strategy_combo.addItem("── 無 ──")
        self._strategy_combo.addItems(list(STRATEGY_REGISTRY.keys()))
        self._strategy_combo.currentTextChanged.connect(self._on_strategy_changed)
        tb.addWidget(self._strategy_combo)

        _btn_style = (
            "QPushButton { background:#1e222d; color:#d1d4dc; border:1px solid #2a2e39;"
            " border-radius:3px; padding:2px 7px; }"
            "QPushButton:checked { background:#2962ff; color:#fff; }"
            "QPushButton:hover   { background:#2a2e39; }"
        )

        self._run_btn = QPushButton("▶ 執行")
        self._run_btn.setToolTip("對目前歷史 K 棒執行回測")
        self._run_btn.setStyleSheet(_btn_style)
        self._run_btn.clicked.connect(self._on_run_strategy)
        tb.addWidget(self._run_btn)

        # 回測範圍選擇
        self._bt_range_combo = QComboBox()
        for label in config.BACKTEST_RANGE_OPTIONS:
            self._bt_range_combo.addItem(label)
        self._bt_range_combo.setToolTip("回測資料範圍（點 ▶ 時自動載入不足的歷史）")
        tb.addWidget(self._bt_range_combo)

        self._download_btn = QPushButton("💾 預載")
        self._download_btn.setToolTip("下載並儲存所選範圍的 K 線至本機，下次回測可直接讀取")
        self._download_btn.setStyleSheet(_btn_style)
        self._download_btn.clicked.connect(self._on_download_cache)
        tb.addWidget(self._download_btn)

        tb.addSeparator()

        # ── 回測資料模式 ────────────────────────────────────────────────────
        self._bt_mode_combo = QComboBox()
        self._bt_mode_combo.addItems(["📊 Bar 模式", "🎯 Tick 模式"])
        self._bt_mode_combo.setToolTip(
            "Bar 模式：用 K 棒收盤值判斷（快速，適合長週期）\n"
            "Tick 模式：用匯入的 aggTrades 逐 tick 模擬（無 look-ahead，適合精確驗證）"
        )
        self._bt_mode_combo.currentIndexChanged.connect(self._on_bt_mode_changed)
        tb.addWidget(self._bt_mode_combo)

        self._import_tick_btn = QPushButton("📂 匯入 Tick")
        self._import_tick_btn.setToolTip(
            "匯入從 data.binance.vision 下載的 aggTrades CSV/ZIP\n"
            "可一次選取多個月份檔案，自動合併至本機快取"
        )
        self._import_tick_btn.setStyleSheet(_btn_style)
        self._import_tick_btn.clicked.connect(self._on_import_ticks)
        tb.addWidget(self._import_tick_btn)

        self._tick_lbl = QLabel()
        self._tick_lbl.setStyleSheet("color:#80cbc4; font-size:10px; padding-left:4px;")
        tb.addWidget(self._tick_lbl)

        self._cache_lbl = QLabel()
        self._cache_lbl.setStyleSheet("color:#4fc3f7; font-size:10px; padding-left:4px;")
        tb.addWidget(self._cache_lbl)

        tb.addSeparator()

        self._bt_config_btn = QPushButton("⚙ 設定")
        self._bt_config_btn.setToolTip("開啟回測參數設定")
        self._bt_config_btn.setStyleSheet(_btn_style)
        self._bt_config_btn.clicked.connect(self._on_open_bt_config)
        tb.addWidget(self._bt_config_btn)

        self._rt_btn = QPushButton("⚡ 即時")
        self._rt_btn.setCheckable(True)
        self._rt_btn.setToolTip("開啟後，每根 K 棒收盤自動標註")
        self._rt_btn.setStyleSheet(_btn_style)
        self._rt_btn.toggled.connect(self._on_realtime_toggled)
        tb.addWidget(self._rt_btn)

        self._clear_btn = QPushButton("✕ 清除")
        self._clear_btn.setToolTip("清除標記與統計")
        self._clear_btn.setStyleSheet(_btn_style)
        self._clear_btn.clicked.connect(self._on_clear_strategy)
        tb.addWidget(self._clear_btn)

        self._strategy_stats_lbl = QLabel()
        self._strategy_stats_lbl.setStyleSheet(
            "color:#f0c040; font-size:11px; padding-left:8px;"
        )
        self._strategy_stats_lbl.setVisible(False)
        tb.addWidget(self._strategy_stats_lbl)

        tb.addSeparator()
        self._status_lbl = QLabel("初始化中 …")
        self._status_lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        tb.addWidget(self._status_lbl)

        # 最新價
        self._price_lbl = QLabel("─")
        self._price_lbl.setStyleSheet(
            "color: #d1d4dc; font-size: 14px; font-weight: bold; padding-left: 16px;"
        )
        tb.addWidget(self._price_lbl)

    def _build_ui(self) -> None:
        # ── 左欄（OB + Heatmap）─────────────────────────────────────────────
        self._ob_widget  = OrderBookWidget()
        self._heatmap    = HeatmapWidget()

        left_widget = QWidget()
        left_lay = QVBoxLayout(left_widget)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(2)

        ob_label = QLabel("Order Book")
        ob_label.setStyleSheet("color:#aaa; font-size:10px; padding:2px 4px;")
        left_lay.addWidget(ob_label)
        left_lay.addWidget(self._ob_widget, 3)   # 佔 3/5

        hm_label = QLabel("OB Heatmap")
        hm_label.setStyleSheet("color:#aaa; font-size:10px; padding:2px 4px;")
        left_lay.addWidget(hm_label)
        left_lay.addWidget(self._heatmap, 2)      # 佔 2/5

        left_widget.setMinimumWidth(200)
        left_widget.setMaximumWidth(280)

        # ── 右欄（K線/Footprint tab + CVD）────────────────────────────────
        self._kline_chart = KlineChart()
        self._fp_chart    = FootprintChart()
        self._cvd_chart   = CvdChart()
        self._stats_panel = StatsPanel()

        # K線 / Footprint tab
        self._chart_tabs = QTabWidget()
        self._chart_tabs.setTabPosition(QTabWidget.TabPosition.North)
        self._chart_tabs.setStyleSheet(
            "QTabBar::tab { padding: 4px 14px; }"
        )
        self._chart_tabs.addTab(self._kline_chart, "K 線")
        self._chart_tabs.addTab(self._fp_chart,    "Footprint")

        # 連結 CVD / Stats 的 x 軸到 K 線
        self._cvd_chart.link_x(self._kline_chart.get_plot_item())
        self._stats_panel.link_x(self._kline_chart.get_plot_item())

        # Footprint 也與 K 線共用 x 軸座標空間
        self._fp_chart.get_plot_item().setXLink(self._kline_chart.get_plot_item())

        # StatsPanel 固定高度
        self._stats_panel.setMaximumHeight(82)
        self._stats_panel.setMinimumHeight(60)

        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.addWidget(self._chart_tabs)
        right_splitter.addWidget(self._cvd_chart)
        right_splitter.addWidget(self._stats_panel)
        right_splitter.setSizes([600, 160, 82])

        # ── 主橫向分割 ────────────────────────────────────────────────────────
        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.addWidget(left_widget)
        main_splitter.addWidget(right_splitter)
        main_splitter.setSizes([230, 1370])

        self.setCentralWidget(main_splitter)

    # ══════════════════════════════════════════════════════════════
    # WebSocket 管理
    # ══════════════════════════════════════════════════════════════

    def _start_stream(self) -> None:
        # 停止舊執行緒
        if self._history_proc and self._history_proc.isRunning():
            self._history_proc.quit()
            self._history_proc.wait(2000)
            self._history_proc = None
        if self._ws_thread:
            self._ws_thread.stop()
            self._ws_thread.wait(4000)
            self._ws_thread = None

        # 重置資料層
        self._order_book.reset()
        self._cvd_calc.reset()
        self._fp_builder.reset(
            tick_size=config.TICK_SIZES.get(self._symbol, 1.0)
        )
        self._fp_chart.set_tick_size(
            config.TICK_SIZES.get(self._symbol, 1.0)
        )
        self._current_kline_open_time = 0
        self._last_price = 0.0
        self._last_trade_time = 0.0
        self._data_stale = False
        self._kline_timestamps = []

        # 重置 UI
        self._kline_chart.set_history([])
        self._heatmap.reset()
        self._cvd_chart.update_cvd([])
        self._stats_panel.update_data([], [])
        self._fp_chart.reset_auto_range()

        # 重置策略狀態（清標記、統計；保留選取的策略 engine）
        self._on_clear_strategy()
        self._strategy_realtime = False
        self._rt_btn.setChecked(False)

        # 建立新執行緒
        self._ws_thread = WsWorkerThread(self._symbol, self._interval)
        self._ws_thread.trade_signal.connect(self._on_trade)
        self._ws_thread.kline_signal.connect(self._on_kline)
        self._ws_thread.depth_signal.connect(self._on_depth)
        self._ws_thread.ob_snapshot_signal.connect(self._on_ob_snapshot)
        self._ws_thread.history_signal.connect(self._on_history)
        self._ws_thread.agg_history_signal.connect(self._on_agg_history)
        self._ws_thread.more_history_signal.connect(self._on_more_history)
        self._ws_thread.more_agg_history_signal.connect(self._on_more_agg_history)
        self._ws_thread.exchange_info_signal.connect(self._on_exchange_info)
        self._ws_thread.status_signal.connect(self._on_status)
        self._ws_thread.backtest_history_signal.connect(self._on_backtest_history)
        self._ws_thread.cache_ready_signal.connect(self._on_cache_ready)
        self._ws_thread.start()

        self._heatmap_timer.start()
        self._flush_timer.start()
        self._stale_timer.start()

    # ══════════════════════════════════════════════════════════════
    # Signal handlers（主執行緒）
    # ══════════════════════════════════════════════════════════════

    def _on_trade(self, data: dict) -> None:
        trade = Trade(
            symbol=data["s"],
            price=float(data["p"]),
            qty=float(data["q"]),
            is_buyer_maker=bool(data["m"]),
            trade_time=int(data["T"]),
        )
        self._last_price = trade.price
        self._last_trade_time = time.monotonic()

        # 若之前處於斷線/延遲狀態，恢復正常
        if self._data_stale:
            self._data_stale = False
            self._status_lbl.setStyleSheet("color: #aaa; font-size: 11px;")
            self._status_lbl.setText(f"已連線：{self._symbol} {self._interval}")

        # CVD — 只累加數值，不立即重繪
        self._cvd_calc.update(trade)
        self._dirty_cvd = True

        # Footprint — 只更新資料結構，不立即重繪
        self._fp_builder.update_trade(trade, open_time=self._current_kline_open_time)
        self._dirty_fp = True

        # Heatmap 成交點（低成本，不節流）
        self._heatmap.add_trade(
            trade.price, trade.qty, not trade.is_buyer_maker
        )

        # 更新價格標籤（低成本）
        color = "#26a69a" if not trade.is_buyer_maker else "#ef5350"
        self._price_lbl.setStyleSheet(
            f"color:{color}; font-size:14px; font-weight:bold; padding-left:16px;"
        )
        self._price_lbl.setText(f"{self._symbol}  {trade.price:,.4f}")

    def _on_kline(self, data: dict) -> None:
        k_raw = data["k"]
        kline = Kline.from_ws(k_raw)

        # ── 同步 _loaded_klines（即時策略的前提條件）──────────────────────
        if self._loaded_klines:
            if self._loaded_klines[-1].open_time == kline.open_time:
                self._loaded_klines[-1] = kline
            else:
                self._loaded_klines.append(kline)
                # 內存管理：限制最大長度
                if len(self._loaded_klines) > config.KLINE_MAX_LOADED:
                    self._loaded_klines = self._loaded_klines[-config.KLINE_MAX_LOADED:]

        # 通知 CVD 新 K 棒開始
        if kline.open_time != self._current_kline_open_time:
            self._cvd_calc.on_new_candle(kline.open_time)
            self._current_kline_open_time = kline.open_time
            self._fp_builder.set_current_open_time(kline.open_time)
            # 更新 kline timestamps 供 footprint 對齊（並上限 KLINE_MAX_LOADED 防長時間記憶體增長）
            if not self._kline_timestamps or self._kline_timestamps[-1] != kline.open_time:
                self._kline_timestamps.append(kline.open_time)
                if len(self._kline_timestamps) > config.KLINE_MAX_LOADED:
                    self._kline_timestamps = self._kline_timestamps[-config.KLINE_MAX_LOADED:]
                self._fp_chart.set_kline_timestamps(self._kline_timestamps)
                self._fp_builder.set_kline_open_times(self._kline_timestamps)

        # 更新 Footprint OHLCV
        self._fp_builder.update_kline(kline)

        # 更新 K 線圖
        self._kline_chart.update_candle(kline)

        # ── 即時策略標註（K 棒收盤後觸發）──────────────────────────
        if (
            kline.is_closed
            and self._strategy_realtime
            and self._strategy_engine is not None
            and self._loaded_klines
        ):
            sig = self._strategy_engine.on_kline(kline, self._loaded_klines)
            if sig is not None:
                self._strategy_signals.append(sig)
                # 上限 2000 筆，防止長時間燒機時 set_strategy_markers O(N) 惡化
                if len(self._strategy_signals) > 2000:
                    self._strategy_signals = self._strategy_signals[-2000:]
                self._kline_chart.set_strategy_markers(self._strategy_signals)
                self._update_strategy_stats()

    def _on_depth(self, data: dict) -> None:
        needs_resync = self._order_book.apply_diff(data)
        if needs_resync and self._ws_thread:
            self._ws_thread.request_resync()
            return

        bids = self._order_book.get_bids(config.OB_DISPLAY_LEVELS)
        asks = self._order_book.get_asks(config.OB_DISPLAY_LEVELS)
        self._ob_widget.update_ob(bids, asks, self._last_price)

    def _on_ob_snapshot(self, data: dict) -> None:
        if not data:
            return
        self._order_book.init_snapshot(data)
        logger.debug("OB snapshot received")

    def _on_exchange_info(self, tick_map: dict) -> None:
        """從 exchangeInfo 動態更新交易所原始 tick，並為未知幣種推算顯示用 tick。"""
        config.EXCHANGE_TICK_SIZES.update(tick_map)

        # 僅對 TICK_SIZES 中尚未定義的幣種，自動推算合理的顯示用 tick
        for symbol, raw_tick in tick_map.items():
            if symbol not in config.TICK_SIZES:
                # 啟發式：取交易所 tick 的 100 倍作為初始顯示 tick
                # （多數幣種的原始 tick 遠小於可讀的分桶大小）
                config.TICK_SIZES[symbol] = raw_tick * 100
                logger.info(
                    "Auto-computed display tick for %s: exchange=%.10g → display=%.10g",
                    symbol, raw_tick, raw_tick * 100,
                )

        # 用目前選中的聚合倍數重新計算實際 tick size
        base_tick = config.TICK_SIZES.get(self._symbol, 1.0)
        multiplier = config.TICK_MULTIPLIERS[self._tick_combo.currentIndex()]
        effective_tick = base_tick * multiplier
        self._fp_builder.reset(tick_size=effective_tick)
        self._fp_chart.set_tick_size(effective_tick)
        logger.info(
            "tickSize for %s: display_base=%.10g, multiplier=%dx, effective=%.10g",
            self._symbol, base_tick, multiplier, effective_tick,
        )

    def _on_history(self, rows: list) -> None:
        """從 REST 取得的歷史 K 線（list of list）。"""
        from core.data_types import Kline as _Kline
        klines = [
            _Kline.from_rest(self._symbol, self._interval, row)
            for row in rows
        ]
        self._loaded_klines = klines
        self._kline_chart.set_history(klines)

        if klines:
            # 傳遞 kline timestamps 給 footprint chart 供 x 軸對齊
            self._kline_timestamps = [k.open_time for k in klines]
            self._fp_chart.set_kline_timestamps(self._kline_timestamps)
            self._fp_builder.set_kline_open_times(self._kline_timestamps)

            # ── CVD: 從歷史 K 線的 taker_buy_volume 計算真正 CVD ──
            self._cvd_calc.seed_history(klines[:-1])
            self._cvd_calc.on_new_candle(klines[-1].open_time)
            self._current_kline_open_time = klines[-1].open_time
            self._fp_builder.set_current_open_time(klines[-1].open_time)

            # 立即將歷史 CVD 曲線渲染到畫面上
            ot_map = self._kline_chart.get_open_time_index_map()
            self._cvd_chart.update_cvd(self._cvd_calc.get_series(), ot_map)

    def _on_agg_history(self, payload_list: list) -> None:
        """
        收到歷史 aggTrades → 啟動背景執行緒處理，釋放主執行緒。
        payload_list[0] = {
            'trades': [...aggTrade dicts...],
            'klines': [(open_time_ms, close_time_ms), ...]
        }
        """
        if not payload_list:
            return
        payload = payload_list[0]
        if not payload.get("trades"):
            return

        # 若前一次處理尚未完成，先等它結束再取代
        if self._history_proc and self._history_proc.isRunning():
            self._history_proc.quit()
            self._history_proc.wait(3000)

        tick_size = config.TICK_SIZES.get(self._symbol, 1.0)
        history_klines = self._loaded_klines[-(config.FOOTPRINT_HISTORY_CANDLES):]

        self._history_proc = HistoryProcessorThread(
            payload=payload,
            tick_size=tick_size,
            history_klines=history_klines,
            parent=self,
        )
        self._history_proc.result_signal.connect(self._on_history_processed)
        self._history_proc.status_signal.connect(self._on_status)
        self._history_proc.start()

    def _on_need_more_history(self, oldest_open_time_ms: int) -> None:
        """K 線圖滚到最左端，請求更早的歷史資料。"""
        if self._ws_thread:
            self._ws_thread.request_more_history(oldest_open_time_ms)

    def _on_more_history(self, rows: list) -> None:
        """WsWorkerThread 完成更早歷史 K 線載入後回調。"""
        self._kline_chart.set_loading_more(False)
        if not rows:
            return

        from core.data_types import Kline as _Kline
        new_klines = [
            _Kline.from_rest(self._symbol, self._interval, row)
            for row in rows
        ]
        # 去除與現有資料重疊的部分
        if self._loaded_klines:
            cutoff = self._loaded_klines[0].open_time
            new_klines = [k for k in new_klines if k.open_time < cutoff]
        if not new_klines:
            return

        # 將新 K 棒插入歷史最前端
        self._loaded_klines = new_klines + self._loaded_klines
        # 內存管理：限制最大長度
        if len(self._loaded_klines) > config.KLINE_MAX_LOADED:
            self._loaded_klines = self._loaded_klines[-config.KLINE_MAX_LOADED:]
        self._kline_timestamps = [k.open_time for k in self._loaded_klines]
        self._fp_chart.set_kline_timestamps(self._kline_timestamps)
        self._fp_builder.set_kline_open_times(self._kline_timestamps)

        # Footprint 小圖已連結 x 軸，更新 timestamps 後重繪現有資料
        fp_candles = self._fp_builder.get_candles()
        if fp_candles:
            self._fp_chart.update_candles(fp_candles)

        # K 線圖前插新 K 棒並平移視圖
        self._kline_chart.prepend_history(new_klines)

        # 如果目前有策略標記，前插後 x 索引已整體右移，需重新渲染
        if self._strategy_signals:
            self._kline_chart.set_strategy_markers(self._strategy_signals)

    def _on_more_agg_history(self, payload_list: list) -> None:
        """WsWorkerThread 完成更早 aggTrades 載入後回調，連同處理 Footprint。"""
        self._on_agg_history(payload_list)

    def _on_history_processed(self, candles: list) -> None:
        """背景執行緒完成後，將 Footprint K 棒合併進主資料層並更新 UI。"""
        if not candles:
            return

        # 將歷史 candles 寫入主 fp_builder（只覆蓋尚未有資料的 K 棒）
        for candle in candles:
            ot = candle.open_time
            if ot not in self._fp_builder._candles:
                self._fp_builder._candles[ot] = candle
            else:
                # 合併：保留實時累積的 levels，補齊 OHLCV
                live = self._fp_builder._candles[ot]
                if live.open == 0.0:
                    live.open   = candle.open
                    live.high   = candle.high
                    live.low    = candle.low
                    live.close  = candle.close
                    live.volume = candle.volume
                for price, lv in candle.levels.items():
                    if price not in live.levels:
                        live.levels[price] = lv

        fp_candles = self._fp_builder.get_candles()
        ot_map = self._kline_chart.get_open_time_index_map()
        self._fp_chart.update_candles(fp_candles, ot_map)
        self._stats_panel.update_data(fp_candles, self._cvd_calc.get_series(), ot_map)
        self._status_lbl.setText(f"已連線：{self._symbol} {self._interval}")

    def _flush_updates(self) -> None:
        """節流刷新：每 150ms 最多重繪一次 CVD / Footprint / Stats。"""
        if self._dirty_cvd or self._dirty_fp:
            cvd_series = self._cvd_calc.get_series()
            fp_candles = self._fp_builder.get_candles()
            # 統一使用 KlineChart 的当前索引映射，避免 kline 截斷後各圖表 x 坐標漂移
            ot_map = self._kline_chart.get_open_time_index_map()

            if self._dirty_cvd:
                self._cvd_chart.update_cvd(cvd_series, ot_map)
                self._dirty_cvd = False

            if self._dirty_fp:
                self._fp_chart.update_candles(fp_candles, ot_map)
                self._dirty_fp = False

            self._stats_panel.update_data(fp_candles, cvd_series, ot_map)

    def _on_status(self, msg: str) -> None:
        self._status_lbl.setText(msg)

    def _check_data_stale(self) -> None:
        """每秒檢查：若超過 5 秒未收到 aggTrade，標記數據過期。"""
        if self._last_trade_time <= 0:
            return
        elapsed = time.monotonic() - self._last_trade_time
        if elapsed > 5.0 and not self._data_stale:
            self._data_stale = True
            self._status_lbl.setStyleSheet(
                "color: #ff4444; font-size: 11px; font-weight: bold;"
            )
            self._status_lbl.setText(
                f"⚠ 數據延遲 ({elapsed:.0f}s 無成交) — 請留意行情可能不準確"
            )
        elif self._data_stale and elapsed > 5.0:
            # 持續更新延遲秒數
            self._status_lbl.setText(
                f"⚠ 數據延遲 ({elapsed:.0f}s 無成交) — 請留意行情可能不準確"
            )

    # ══════════════════════════════════════════════════════════════
    # Heatmap timer
    # ══════════════════════════════════════════════════════════════

    def _snapshot_heatmap(self) -> None:
        if not self._order_book.is_initialized:
            return
        mid = self._order_book.mid_price()
        if mid <= 0:
            mid = self._last_price
        if mid <= 0:
            return
        bids = self._order_book.get_bids(200)
        asks = self._order_book.get_asks(200)
        self._heatmap.add_snapshot(bids, asks, mid)

    # ══════════════════════════════════════════════════════════════
    # 工具列事件
    # ══════════════════════════════════════════════════════════════

    def _on_symbol_changed(self, sym: str) -> None:
        if sym == self._symbol:
            return
        self._symbol = sym
        self._refresh_cache_label()
        self._start_stream()

    def _on_interval_changed(self, iv: str) -> None:
        if iv == self._interval:
            return
        self._interval = iv
        self._refresh_cache_label()
        self._start_stream()

    def _on_fp_mode_changed(self, mode: str) -> None:
        self._fp_chart.set_mode(mode)

    def _on_tick_multiplier_changed(self, index: int) -> None:
        """切換價格聚合倍數，重新設定 tick size 並清空 Footprint。"""
        multiplier = config.TICK_MULTIPLIERS[index]
        base_tick = config.TICK_SIZES.get(self._symbol, 1.0)
        effective_tick = base_tick * multiplier
        self._fp_builder.reset(tick_size=effective_tick)
        self._fp_chart.set_tick_size(effective_tick)
        if self._kline_timestamps:
            self._fp_builder.set_kline_open_times(self._kline_timestamps)
        logger.info("Tick multiplier changed to %dx (tick=%.10g)", multiplier, effective_tick)

    def _on_log_toggled(self, checked: bool) -> None:
        """Log scale 按鈕切換。"""
        self._kline_chart.toggle_log_scale()
        lbl = "Log ✓" if checked else "Log"
        self._log_btn.setText(lbl)

    # ── 策略事件 ──────────────────────────────────────────────────────────────

    def _on_strategy_changed(self, name: str) -> None:
        """下拉選單切換策略 engine。"""
        if name == "── 無 ──":
            self._strategy_engine = None
        else:
            cls = STRATEGY_REGISTRY.get(name)
            self._strategy_engine = cls() if cls else None

    def _on_run_strategy(self) -> None:
        """對目前已載入的歷史 K 棒執行回測，繪製標記並顯示統計。"""
        if not self._strategy_engine or not self._loaded_klines:
            return

        # 取得使用者選取的回測範圍（K 棒數量）
        range_label = self._bt_range_combo.currentText()
        need = config.BACKTEST_RANGE_OPTIONS.get(range_label, 200)
        have = len(self._loaded_klines)

        if have < need:
            # 內存不足 → 先檢查本機快取
            cached = kline_cache.load(self._symbol, self._interval)
            if len(cached) >= need:
                self._status_lbl.setText("從本機快取載入資料…")
                self._load_from_cache(cached, need)
                return

            # 快取不足 → 批量網路載入
            self._run_btn.setEnabled(False)
            self._run_btn.setText("載入中…")
            if self._ws_thread:
                self._ws_thread.request_backtest_history(need)
            return

        self._execute_backtest(klines=self._loaded_klines[-need:])

    def _on_backtest_history(self, rows: list) -> None:
        """批量歷史載入完成後的回調。"""
        self._run_btn.setEnabled(True)
        self._run_btn.setText("▶ 執行")

        if not rows:
            self._status_lbl.setText("回測歷史載入失敗")
            return

        from core.data_types import Kline as _Kline
        klines = [
            _Kline.from_rest(self._symbol, self._interval, row)
            for row in rows
        ]

        # 合併：將現有即時 K 棒中較新的資料附加在歷史之後
        if self._loaded_klines:
            latest_hist_ot = klines[-1].open_time
            newer = [k for k in self._loaded_klines if k.open_time > latest_hist_ot]
            if newer:
                klines.extend(newer)

        self._loaded_klines = klines
        self._kline_timestamps = [k.open_time for k in klines]

        # 大回測（> 30d）不更新圖表，避免渲染大量資料
        large_backtest = len(klines) > config.BACKTEST_NO_CHART_BARS

        if not large_backtest:
            # 更新 KlineChart 以顯示完整歷史
            self._kline_chart.set_history(klines)
            self._fp_chart.set_kline_timestamps(self._kline_timestamps)
            self._fp_builder.set_kline_open_times(self._kline_timestamps)

            # 重新計算 CVD
            self._cvd_calc.seed_history(klines[:-1])
            self._cvd_calc.on_new_candle(klines[-1].open_time)
            self._current_kline_open_time = klines[-1].open_time
            ot_map = self._kline_chart.get_open_time_index_map()
            self._cvd_chart.update_cvd(self._cvd_calc.get_series(), ot_map)

        n = len(klines)
        self._status_lbl.setText(f"已載入 {n} 根 K 棒，開始回測…")

        # 自動接續回測
        if self._strategy_engine:
            self._execute_backtest()

    def _execute_backtest(self, klines: list | None = None) -> None:
        """執行回測並顯示結果。klines 為 None 時使用 self._loaded_klines。"""
        bt_klines = klines if klines is not None else self._loaded_klines

        # ── 依模式決定是否載入 tick_map ────────────────────────────────
        tick_map = None
        use_tick_mode = self._bt_mode_combo.currentIndex() == 1
        if use_tick_mode and bt_klines:
            start_ms = bt_klines[0].open_time
            end_ms   = bt_klines[-1].open_time + _interval_ms(self._interval)
            ticks = tick_cache.load_range(self._symbol, self._interval,
                                         start_ms, end_ms)
            if len(ticks) > 0:
                kline_times = [
                    (k.open_time, k.open_time + _interval_ms(self._interval) - 1)
                    for k in bt_klines
                ]
                tick_map = tick_cache.build_bar_map(ticks, kline_times)
                coverage = len(tick_map) / len(bt_klines) * 100
                self._status_lbl.setText(
                    f"🎯 Tick 模式：覆蓋 {len(tick_map):,}/{len(bt_klines):,} 根 "
                    f"({coverage:.0f}%)，開始回測…"
                )
            else:
                self._status_lbl.setText(
                    "⚠ Tick 模式：無快取資料，請先匹入 aggTrades 檔案（點「📂 匯入 Tick」）"
                )

        self._strategy_signals = self._strategy_engine.on_history(
            bt_klines, tick_map=tick_map,
        )

        # 大回測（> 30d）不繪製圖表標記
        large_backtest = len(bt_klines) > config.BACKTEST_NO_CHART_BARS
        if not large_backtest:
            self._kline_chart.set_strategy_markers(self._strategy_signals)

        # 工具列簡易統計（百分比）
        basic_stats = self._strategy_engine.compute_stats(self._strategy_signals)
        self._show_strategy_stats_label(basic_stats)

        # 完整模擬（資金/手續費/倉位）
        from backtest.engine import BacktestConfig, simulate_trades
        cfg = BacktestConfig(
            initial_capital=self._bt_config_dlg.capital_spin.value(),
            max_loss_pct=self._bt_config_dlg.loss_spin.value() / 100.0,
            leverage=self._bt_config_dlg.leverage_spin.value(),
            fee_mode=self._bt_config_dlg.fee_combo.currentText(),
            custom_fee_rate=self._bt_config_dlg.custom_fee_spin.value() / 100.0,
            slippage_bps=self._bt_config_dlg.slippage_spin.value(),
            funding_rate=self._bt_config_dlg.funding_spin.value(),
            maint_margin=self._bt_config_dlg.maint_spin.value(),
            compound=self._bt_config_dlg.compound_combo.currentIndex() == 0,
        )
        sim_stats = simulate_trades(self._strategy_signals, cfg)
        sim_stats["strategy_name"] = getattr(self._strategy_engine, "name", "策略")

        dlg = BacktestResultDialog(sim_stats, parent=self)
        dlg.exec()

    # ── 本機快取 ──────────────────────────────────────────────────────────────

    def _load_from_cache(self, cached_rows: list, need: int) -> None:
        """以快取列表直接建立 _loaded_klines 並執行回測。"""
        rows = cached_rows[-need:]  # 取最近 need 根
        from core.data_types import Kline as _Kline
        klines = [
            _Kline.from_rest(self._symbol, self._interval, row)
            for row in rows
        ]

        # 合併較新的即時 K 棒
        if self._loaded_klines:
            latest_hist_ot = klines[-1].open_time
            newer = [k for k in self._loaded_klines if k.open_time > latest_hist_ot]
            if newer:
                klines.extend(newer)

        self._loaded_klines = klines
        self._kline_timestamps = [k.open_time for k in klines]

        n = len(klines)
        self._status_lbl.setText(f"已從快取載入 {n:,} 根 K 棒，開始回測…")

        large_backtest = n > config.BACKTEST_NO_CHART_BARS
        if not large_backtest:
            self._kline_chart.set_history(klines)
            self._fp_chart.set_kline_timestamps(self._kline_timestamps)
            self._fp_builder.set_kline_open_times(self._kline_timestamps)
            self._cvd_calc.seed_history(klines[:-1])
            self._cvd_calc.on_new_candle(klines[-1].open_time)
            self._current_kline_open_time = klines[-1].open_time
            ot_map = self._kline_chart.get_open_time_index_map()
            self._cvd_chart.update_cvd(self._cvd_calc.get_series(), ot_map)

        if self._strategy_engine:
            self._execute_backtest()

    def _on_download_cache(self) -> None:
        """「💾 預載」按鈕：只下載並儲存到本機快取，不執行回測。"""
        range_label = self._bt_range_combo.currentText()
        need = config.BACKTEST_RANGE_OPTIONS.get(range_label, 200)
        if self._ws_thread:
            self._download_btn.setEnabled(False)
            self._download_btn.setText("下載中…")
            self._status_lbl.setText(f"正在下載並儲存 {range_label} 快取…")
            self._ws_thread.request_backtest_history(need, cache_only=True)

    def _on_cache_ready(self, count: int) -> None:
        """cache_only 下載完成後的回調。"""
        self._download_btn.setEnabled(True)
        self._download_btn.setText("💾 預載")
        if count:
            info = kline_cache.info(self._symbol, self._interval)
            if info:
                total = info["count"]
                size  = info["size_mb"]
                self._cache_lbl.setText(f"📁 {total:,}根 {size:.0f}MB")
                self._status_lbl.setText(
                    f"快取已儲存：{total:,} 根 K 棒 ({size:.1f} MB)"
                )
            else:
                self._status_lbl.setText(f"快取已儲存：{count:,} 根")
        else:
            self._status_lbl.setText("快取下載失敗，請稍後再試")

    def _on_bt_mode_changed(self, index: int) -> None:
        """切換 Bar/Tick 模式：更新 tick 快取標籤。"""
        self._refresh_tick_label()

    def _on_import_ticks(self) -> None:
        """「📂 匯入 Tick」按鈕：開啟檔案對話框，背景匯入 aggTrades CSV/ZIP。"""
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "選擇 aggTrades 檔案（data.binance.vision）",
            "",
            "Binance aggTrades (*.csv *.zip);;所有檔案 (*)",
        )
        if not paths:
            return

        # 若上一次匯入仍在執行，等待結束
        if self._tick_import_thread and self._tick_import_thread.isRunning():
            self._tick_import_thread.wait(500)

        self._import_tick_btn.setEnabled(False)
        self._import_tick_btn.setText("匯入中…")
        self._tick_lbl.setText("🔄 匯入中…")

        self._tick_import_thread = TickImportThread(
            self._symbol, self._interval, paths, parent=self
        )
        self._tick_import_thread.progress_signal.connect(self._on_tick_import_progress)
        self._tick_import_thread.done_signal.connect(self._on_tick_import_done)
        self._tick_import_thread.start()

    def _on_tick_import_progress(self, msg: str) -> None:
        self._status_lbl.setText(msg)

    def _on_tick_import_done(self, count: int, err: str) -> None:
        self._import_tick_btn.setEnabled(True)
        self._import_tick_btn.setText("📂 匯入 Tick")
        if err:
            self._status_lbl.setText(f"❌ 匯入失敗：{err}")
            self._tick_lbl.setText("❌ 匯入失敗")
        elif count:
            ti = tick_cache.info(self._symbol, self._interval)
            self._refresh_tick_label()
            self._status_lbl.setText(
                f"✓ 匯入完成，快取共 {count:,} 筆"
                + (f" ({ti['size_mb']:.1f} MB)" if ti else "")
            )
        else:
            self._status_lbl.setText("⚠ 匯入完成，但未解析到任何資料（請確認檔案格式）")
            self._tick_lbl.setText("⚠ 無資料")

    def _refresh_tick_label(self) -> None:
        """更新 tick 快取狀態標籤。"""
        ti = tick_cache.info(self._symbol, self._interval)
        if ti:
            from datetime import datetime, timezone
            s = datetime.fromtimestamp(ti["start_ms"] / 1000, tz=timezone.utc).strftime("%y-%m-%d")
            e = datetime.fromtimestamp(ti["end_ms"]   / 1000, tz=timezone.utc).strftime("%y-%m-%d")
            self._tick_lbl.setText(
                f"🎯 {ti['count']:,} 筆 | {s}~{e} | {ti['size_mb']:.0f}MB"
            )
        else:
            self._tick_lbl.setText("🎯 無 Tick 快取")

    def _refresh_cache_label(self) -> None:
        """更新工具列快取資訊 label（切換幣對/interval 時呼叫）。"""
        info = kline_cache.info(self._symbol, self._interval)
        if info:
            self._cache_lbl.setText(f"📁 {info['count']:,}根 {info['size_mb']:.0f}MB")
        else:
            self._cache_lbl.setText("")

    # ─────────────────────────────────────────────────────────────────────────

    def _on_realtime_toggled(self, checked: bool) -> None:
        """⚡ 即時按鈕 toggle：開啟後每根收盤 K 棒自動標注。"""
        self._strategy_realtime = checked

    def _on_clear_strategy(self) -> None:
        """清除所有策略標記、訊號與統計。"""
        self._kline_chart.clear_strategy_markers()
        self._strategy_signals = []
        self._strategy_stats_lbl.setVisible(False)
        self._strategy_stats_lbl.setText("")

    def _on_open_bt_config(self) -> None:
        """開啟回測參數設定子頁面。"""
        self._bt_config_dlg.show()
        self._bt_config_dlg.raise_()
        self._bt_config_dlg.activateWindow()

    def _update_strategy_stats(self) -> None:
        """根據目前 _strategy_signals 計算並顯示統計 label。"""
        if not self._strategy_engine or not self._strategy_signals:
            self._strategy_stats_lbl.setVisible(False)
            return
        stats = self._strategy_engine.compute_stats(self._strategy_signals)
        self._show_strategy_stats_label(stats)

    def _show_strategy_stats_label(self, stats: dict) -> None:
        """將回測統計顯示在工具列 label 上。"""
        n      = stats.get("trades", 0)
        wr     = stats.get("win_rate", 0.0)
        pnl    = stats.get("total_pnl", 0.0)
        opens  = stats.get("open_count", 0)
        pf     = stats.get("profit_factor", 0.0)
        mcl    = stats.get("max_consec_loss", 0)
        mdd    = stats.get("max_drawdown", 0.0)
        ln     = stats.get("long_trades", 0)
        sn     = stats.get("short_trades", 0)

        pnl_sign = "+" if pnl >= 0 else ""
        pf_s = f"{pf:.2f}" if pf != float("inf") else "∞"
        txt = (f"{n} 筆  勝率 {wr:.1f}%  PnL {pnl_sign}{pnl:.2f}%"
               f"  PF {pf_s}  連虧 {mcl}  回撤 {mdd:.2f}%"
               f"  L{ln}/S{sn}")
        if opens:
            txt += f"  (+{opens} 未平倉)"
        self._strategy_stats_lbl.setText(txt)
        self._strategy_stats_lbl.setVisible(True)

    # ══════════════════════════════════════════════════════════════
    # 十字線同步
    # ══════════════════════════════════════════════════════════════

    def _sync_crosshair(self, x_pos: float) -> None:
        """任一右側面板的十字線移動時，同步垂直線至其他面板。"""
        sender = self.sender()
        for chart in self._crosshair_charts:
            if chart is not sender:
                chart.set_crosshair_x(x_pos)
        self._stats_panel.set_crosshair_x(x_pos)

    def _hide_all_crosshairs(self) -> None:
        """滑鼠離開面板時，隱藏所有十字線。"""
        for chart in self._crosshair_charts:
            chart.hide_crosshair()
        self._stats_panel.hide_crosshair()

    # ══════════════════════════════════════════════════════════════
    # 視窗生命週期
    # ══════════════════════════════════════════════════════════════

    def closeEvent(self, event) -> None:  # noqa: N802
        self._heatmap_timer.stop()
        self._flush_timer.stop()
        self._stale_timer.stop()
        if self._history_proc and self._history_proc.isRunning():
            self._history_proc.quit()
            self._history_proc.wait(3000)
        if self._ws_thread:
            self._ws_thread.stop()
            if not self._ws_thread.wait(6000):   # 等最多 6 秒讓執行緒自行結束
                self._ws_thread.terminate()      # 後備：強制終止，避免 QThread destroyed 警告
                self._ws_thread.wait(1000)
        event.accept()
