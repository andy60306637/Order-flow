"""
回測分析儀表板：整合所有分析 Widget 與後台回測執行緒。
作為 MainWindow 的 Tab 0 嵌入。
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QLabel,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt

from backtest.engine import BacktestConfig, simulate_trades
from backtest.time_slice import TimeSlice, WalkForwardConfig
from strategies.base import StrategyBase
from ui.backtest_config_panel import BacktestConfigPanel
from ui.equity_chart import EquityChart
from ui.metrics_panel import MetricsPanel
from ui.mfe_mae_chart import MfeMaeChart
from ui.montecarlo_chart import MonteCarloChart
from ui.optimization_heatmap import OptimizationHeatmap
from ui.trade_ledger import TradeLedger

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# 背景回測執行緒
# ──────────────────────────────────────────────────────────────────────────────

class BacktestWorkerThread(QThread):
    """
    在背景執行回測，支援：
    - Single slice / Multi-select：單段回測
    - Walk-forward：(IS, OOS) 對，每段重置 equity，合併 trade_list
    """

    result_ready = pyqtSignal(dict)
    progress     = pyqtSignal(str)
    error        = pyqtSignal(str)

    def __init__(
        self,
        strategy: StrategyBase,
        bt_cfg:   BacktestConfig,
        slices,                    # list[TimeSlice] 或 list[tuple[TimeSlice, TimeSlice]]
        symbol:   str,
        interval: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._strategy = strategy
        self._bt_cfg   = bt_cfg
        self._slices   = slices
        self._symbol   = symbol
        self._interval = interval
        self._abort    = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        try:
            result = self._run_backtest()
            if not self._abort:
                self.result_ready.emit(result)
        except Exception as exc:
            logger.exception("BacktestWorkerThread error: %s", exc)
            self.error.emit(str(exc))

    def _run_backtest(self) -> dict:
        from core.kline_cache import load_range_as_klines
        from core.tick_cache import build_bar_map, load_range_sharded

        is_walk_forward = (
            self._slices and
            isinstance(self._slices[0], tuple)
        )

        if is_walk_forward:
            return self._run_walk_forward(load_range_as_klines, load_range_sharded, build_bar_map)
        else:
            return self._run_single(self._slices, load_range_as_klines, load_range_sharded, build_bar_map)

    def _run_single(self, slices, load_klines_fn, load_ticks_fn, bar_map_fn) -> dict:
        """合併所有 segments 執行一次回測。"""
        all_klines = []
        all_tick_parts = []

        for sl in slices:
            if self._abort:
                return {}
            for start_ms, end_ms in sl.segments:
                self.progress.emit(f"Loading klines {sl.label}…")
                klines = load_klines_fn(self._symbol, self._interval, start_ms, end_ms)
                all_klines.extend(klines)

                self.progress.emit(f"Loading ticks {sl.label}…")
                ticks = load_ticks_fn(self._symbol, start_ms, end_ms)
                if ticks is not None and len(ticks) > 0:
                    all_tick_parts.append(ticks)

        if not all_klines:
            raise ValueError("No klines loaded for the selected time range.")

        # 去重並排序 klines（多 segment 合併時可能有重疊）
        seen: dict[int, object] = {}
        for k in all_klines:
            seen[k.open_time] = k
        all_klines = [seen[t] for t in sorted(seen)]

        # 合併 tick 資料
        tick_map = {}
        if all_tick_parts:
            merged = np.concatenate(all_tick_parts, axis=0)
            merged = merged[merged[:, 0].argsort()]  # 時間排序
            kline_times = [(k.open_time, k.close_time) for k in all_klines]
            tick_map = bar_map_fn(merged, kline_times)

        self.progress.emit("Generating signals…")
        signals = self._strategy.on_history(all_klines, tick_map or None)

        self.progress.emit("Simulating trades…")
        result = simulate_trades(signals, self._bt_cfg)
        result["mode"] = "single"
        return result

    def _run_walk_forward(self, load_klines_fn, load_ticks_fn, bar_map_fn) -> dict:
        """Walk-forward：每對 (IS, OOS) 以 OOS 為評估，合併全部 OOS trade_list。"""
        combined_trades = []
        equity = self._bt_cfg.initial_capital
        segment_results = []

        for i, (is_sl, oos_sl) in enumerate(self._slices):
            if self._abort:
                break

            self.progress.emit(f"WF segment {i+1}/{len(self._slices)}: optimizing IS…")
            # IS：僅執行策略（未來可加入參數優化）
            is_result = self._run_single([is_sl], load_klines_fn, load_ticks_fn, bar_map_fn)

            self.progress.emit(f"WF segment {i+1}/{len(self._slices)}: testing OOS…")
            # OOS：使用 IS 訓練結果的策略（目前直接使用相同策略）
            oos_cfg = BacktestConfig(
                initial_capital = equity,
                max_loss_pct    = self._bt_cfg.max_loss_pct,
                leverage        = self._bt_cfg.leverage,
                fee_mode        = self._bt_cfg.fee_mode,
                slippage_bps    = self._bt_cfg.slippage_bps,
                compound        = self._bt_cfg.compound,
            )
            oos_result = self._run_single([oos_sl], load_klines_fn, load_ticks_fn, bar_map_fn)

            # 更新滾動 equity
            if oos_result.get("trade_list"):
                equity = oos_result["final_equity"]

            combined_trades.extend(oos_result.get("trade_list", []))
            segment_results.append({
                "is_label":  is_sl.label,
                "oos_label": oos_sl.label,
                "is_result": is_result,
                "oos_result": oos_result,
            })

        # 重新合併統計
        if not combined_trades:
            return {"mode": "walk_forward", "trade_list": [], "trades": 0,
                    "segments": segment_results}

        final_result = simulate_trades(
            [],  # 空 signals，直接用合併 trade_list（hack：使用 empty simulate + 手動替換）
            self._bt_cfg,
        )
        # 以合併 trade_list 重算統計
        from backtest.engine import _build_stats
        merged = _build_stats(
            combined_trades,
            self._bt_cfg,
            combined_trades[-1].get("equity_after", equity) if combined_trades else equity,
            0,
        )
        merged["mode"] = "walk_forward"
        merged["segments"] = segment_results
        return merged


# ──────────────────────────────────────────────────────────────────────────────
# 主要儀表板 Widget
# ──────────────────────────────────────────────────────────────────────────────

class BacktestDashboard(QWidget):
    """
    回測分析主畫面。
    佈局：
      左側 340px：BacktestConfigPanel
      右側：
        頂部 70px：MetricsPanel
        中央：EquityChart（左 3/5）+ MfeMaeChart（右上）+ TradeLedger（右下）
        底部：OptimizationHeatmap（左）+ MonteCarloChart（右）
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._worker: Optional[BacktestWorkerThread] = None
        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self) -> None:
        root = QSplitter(Qt.Orientation.Horizontal)
        root.setHandleWidth(4)

        # ── 左側設定面板 ──────────────────────────────────────────────────────
        self._config = BacktestConfigPanel()
        root.addWidget(self._config)

        # ── 右側儀表板 ────────────────────────────────────────────────────────
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # 頂部指標列
        self._metrics = MetricsPanel()
        right_layout.addWidget(self._metrics)

        # 狀態標籤（回測進行中提示）
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(
            "color: #787b86; font-size: 11px; padding: 2px 8px;"
        )
        right_layout.addWidget(self._status_label)

        # 中間：資金曲線 + MFE/MAE + 交易明細
        center_splitter = QSplitter(Qt.Orientation.Horizontal)
        center_splitter.setHandleWidth(4)

        self._equity = EquityChart()
        center_splitter.addWidget(self._equity)

        right_detail = QSplitter(Qt.Orientation.Vertical)
        right_detail.setHandleWidth(4)

        self._mfe_mae = MfeMaeChart()
        right_detail.addWidget(self._mfe_mae)

        self._ledger = TradeLedger()
        right_detail.addWidget(self._ledger)

        right_detail.setStretchFactor(0, 1)
        right_detail.setStretchFactor(1, 2)

        center_splitter.addWidget(right_detail)
        center_splitter.setStretchFactor(0, 3)
        center_splitter.setStretchFactor(1, 2)

        right_layout.addWidget(center_splitter, stretch=3)

        # 底部：優化熱力圖 + Monte Carlo
        bottom_splitter = QSplitter(Qt.Orientation.Horizontal)
        bottom_splitter.setHandleWidth(4)

        self._heatmap = OptimizationHeatmap()
        bottom_splitter.addWidget(self._heatmap)

        self._montecarlo = MonteCarloChart()
        bottom_splitter.addWidget(self._montecarlo)

        bottom_splitter.setStretchFactor(0, 1)
        bottom_splitter.setStretchFactor(1, 1)

        right_layout.addWidget(bottom_splitter, stretch=1)

        root.addWidget(right_widget)
        root.setStretchFactor(0, 0)
        root.setStretchFactor(1, 1)

        # 主佈局
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(root)

    def _connect_signals(self) -> None:
        self._config.run_requested.connect(self._on_run_backtest)
        self._config.cancel_requested.connect(self._on_cancel)
        self._mfe_mae.trade_selected.connect(self._ledger.trade_selected)

    # ── 回測執行 ──────────────────────────────────────────────────────────────

    def _on_run_backtest(
        self,
        strategy: StrategyBase,
        bt_cfg:   BacktestConfig,
        slices:   list,
    ) -> None:
        if self._worker and self._worker.isRunning():
            return

        self._config.set_running(True)
        self._status_label.setText("Loading data…")

        self._worker = BacktestWorkerThread(
            strategy = strategy,
            bt_cfg   = bt_cfg,
            slices   = slices,
            symbol   = self._config.symbol(),
            interval = self._config.interval(),
            parent   = self,
        )
        self._worker.result_ready.connect(self._on_result_ready)
        self._worker.progress.connect(self._status_label.setText)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(lambda: self._config.set_running(False))
        self._worker.start()

    def _on_cancel(self) -> None:
        if self._worker:
            self._worker.abort()
            self._worker.quit()
        self._config.set_running(False)
        self._status_label.setText("Cancelled.")

    def _on_result_ready(self, stats: dict) -> None:
        self._status_label.setText(
            f"Done — {stats.get('trades', 0)} trades | "
            f"Win: {stats.get('win_rate', 0):.1f}% | "
            f"PF: {stats.get('profit_factor', 0):.2f}"
        )

        self._metrics.load_result(stats)
        self._equity.load_result(stats)
        self._mfe_mae.load_result(stats)
        self._ledger.load_result(stats)

        trade_list = stats.get("trade_list", [])
        if trade_list:
            self._montecarlo.run_simulation(
                trade_list,
                n_iterations=1000,
            )

    def _on_error(self, msg: str) -> None:
        self._status_label.setText(f"Error: {msg}")
        QMessageBox.critical(self, "Backtest Error", msg)
        self._config.set_running(False)

    # ── 熱力圖（外部調用）────────────────────────────────────────────────────

    def load_optimization(self, results: list[dict], x_label: str, y_label: str, metric: str) -> None:
        """由外部優化工具調用，載入熱力圖資料。"""
        self._heatmap.load_optimization(results, x_label, y_label, metric)
