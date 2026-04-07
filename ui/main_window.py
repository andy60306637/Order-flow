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
from typing import Optional, List

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QSplitter, QTabWidget,
    QVBoxLayout, QHBoxLayout, QComboBox, QLabel,
    QToolBar, QFrame, QSizePolicy, QPushButton,
    QDialog, QTableWidget, QTableWidgetItem, QHeaderView,
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
from strategies import STRATEGY_REGISTRY
from strategies.base import StrategyBase, StrategySignal
from ui.order_book_widget import OrderBookWidget
from ui.kline_chart import KlineChart
from ui.cvd_chart import CvdChart, StatsPanel
from ui.heatmap_widget import HeatmapWidget
from ui.footprint_widget import FootprintChart

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 回測結果對話框
# ═══════════════════════════════════════════════════════════════════

class BacktestResultDialog(QDialog):
    """顯示策略回測統計摘要與逐筆交易明細。"""

    def __init__(self, stats: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("回測結果")
        self.setMinimumSize(560, 420)
        self.setStyleSheet(
            f"QDialog {{ background: {config.COLOR_BG}; color: {config.COLOR_FG}; }}"
            f"QTableWidget {{ background: #1e222d; color: {config.COLOR_FG};"
            f" gridline-color: #2a2e39; font-size: 12px; }}"
            f"QHeaderView::section {{ background: #1e222d; color: {config.COLOR_FG};"
            f" border: 1px solid #2a2e39; padding: 4px; font-weight: bold; }}"
        )

        layout = QVBoxLayout(self)

        n      = stats.get("trades", 0)
        wr     = stats.get("win_rate", 0.0)
        pnl    = stats.get("total_pnl", 0.0)
        opens  = stats.get("open_count", 0)
        pf     = stats.get("profit_factor", 0.0)
        mcl    = stats.get("max_consec_loss", 0)
        mdd    = stats.get("max_drawdown", 0.0)
        trades = stats.get("trade_list", [])

        pnl_c = config.COLOR_UP if pnl >= 0 else config.COLOR_DOWN
        pnl_s = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
        pf_s  = f"{pf:.2f}" if pf != float("inf") else "∞"

        summary = QLabel(
            f"<b>交易次數:</b> {n} &nbsp;|&nbsp; "
            f"<b>勝率:</b> {wr:.1f}% &nbsp;|&nbsp; "
            f"<b>總 PnL:</b> <span style='color:{pnl_c}'>{pnl_s}%</span> &nbsp;|&nbsp; "
            f"<b>Profit Factor:</b> {pf_s} &nbsp;|&nbsp; "
            f"<b>最大連續虧損:</b> {mcl} &nbsp;|&nbsp; "
            f"<b>最大回撤:</b> {mdd:.2f}% &nbsp;|&nbsp; "
            f"<b>未平倉:</b> {opens}"
        )
        summary.setStyleSheet("font-size: 13px; padding: 8px;")
        layout.addWidget(summary)

        if not trades:
            layout.addWidget(QLabel("（無已平倉交易）"))
            return

        table = QTableWidget(len(trades), 5)
        table.setHorizontalHeaderLabels(["#", "方向", "入場價", "出場價", "PnL%"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        for i, t in enumerate(trades):
            table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            dir_txt = "做多" if t["dir"] == "long" else "做空"
            dir_item = QTableWidgetItem(dir_txt)
            dir_item.setForeground(
                QtGui.QColor(config.COLOR_UP if t["dir"] == "long" else config.COLOR_DOWN)
            )
            table.setItem(i, 1, dir_item)
            table.setItem(i, 2, QTableWidgetItem(f"{t['entry']:,.4f}"))
            table.setItem(i, 3, QTableWidgetItem(f"{t['exit']:,.4f}"))

            pv = t["pnl_pct"]
            pnl_txt = f"+{pv:.2f}%" if pv >= 0 else f"{pv:.2f}%"
            pnl_item = QTableWidgetItem(pnl_txt)
            pnl_item.setForeground(
                QtGui.QColor(config.COLOR_UP if pv >= 0 else config.COLOR_DOWN)
            )
            table.setItem(i, 4, pnl_item)

        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table)


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
        # ── UI ────────────────────────────────────────────────────────────────
        self._build_ui()
        self._build_toolbar()

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
            # 更新 kline timestamps 供 footprint 對齊
            if not self._kline_timestamps or self._kline_timestamps[-1] != kline.open_time:
                self._kline_timestamps.append(kline.open_time)
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
            self._cvd_chart.update_cvd(self._cvd_calc.get_series())

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
        self._fp_chart.update_candles(fp_candles)
        self._stats_panel.update_data(fp_candles, self._cvd_calc.get_series())
        self._status_lbl.setText(f"已連線：{self._symbol} {self._interval}")

    def _flush_updates(self) -> None:
        """節流刷新：每 150ms 最多重繪一次 CVD / Footprint / Stats。"""
        if self._dirty_cvd or self._dirty_fp:
            cvd_series = self._cvd_calc.get_series()
            fp_candles = self._fp_builder.get_candles()

            if self._dirty_cvd:
                self._cvd_chart.update_cvd(cvd_series)
                self._dirty_cvd = False

            if self._dirty_fp:
                self._fp_chart.update_candles(fp_candles)
                self._dirty_fp = False

            self._stats_panel.update_data(fp_candles, cvd_series)

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
        self._start_stream()

    def _on_interval_changed(self, iv: str) -> None:
        if iv == self._interval:
            return
        self._interval = iv
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
        self._strategy_signals = self._strategy_engine.on_history(self._loaded_klines)
        self._kline_chart.set_strategy_markers(self._strategy_signals)
        stats = self._strategy_engine.compute_stats(self._strategy_signals)
        self._show_strategy_stats_label(stats)

        # 彈出回測結果對話框
        dlg = BacktestResultDialog(stats, parent=self)
        dlg.exec()

    def _on_realtime_toggled(self, checked: bool) -> None:
        """⚡ 即時按鈕 toggle：開啟後每根收盤 K 棒自動標注。"""
        self._strategy_realtime = checked

    def _on_clear_strategy(self) -> None:
        """清除所有策略標記、訊號與統計。"""
        self._kline_chart.clear_strategy_markers()
        self._strategy_signals = []
        self._strategy_stats_lbl.setVisible(False)
        self._strategy_stats_lbl.setText("")

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

        pnl_sign = "+" if pnl >= 0 else ""
        pf_s = f"{pf:.2f}" if pf != float("inf") else "∞"
        txt = (f"{n} 筆  勝率 {wr:.1f}%  PnL {pnl_sign}{pnl:.2f}%"
               f"  PF {pf_s}  連虧 {mcl}  回撤 {mdd:.2f}%")
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
