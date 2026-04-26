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
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
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
from ui.trade_snapshot_dialog import TradeSnapshotDialog, _collect_contexts

logger = logging.getLogger(__name__)


def _find_bar_index(klines, ts_ms: int) -> Optional[int]:
    if not klines or not ts_ms:
        return None
    lo, hi = 0, len(klines) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        k = klines[mid]
        if k.open_time <= ts_ms <= k.close_time:
            return mid
        if ts_ms < k.open_time:
            hi = mid - 1
        else:
            lo = mid + 1
    return max(0, min(lo, len(klines) - 1))


def _fallback_snapshot_contexts(trade_list: list[dict], klines) -> list[dict]:
    contexts: list[dict] = []
    for idx, trade in enumerate([t for t in trade_list if not t.get("skipped")]):
        entry_ki = _find_bar_index(klines, int(trade.get("entry_time", 0) or 0))
        exit_ki = _find_bar_index(klines, int(trade.get("exit_time", 0) or 0))
        if entry_ki is None:
            continue
        latest = exit_ki if exit_ki is not None else entry_ki
        contexts.append({
            "trade": trade,
            "trade_idx": idx,
            "k0_signal": None,
            "entry_signal": None,
            "exit_signal": None,
            "k0_ki": None,
            "entry_ki": entry_ki,
            "exit_ki": exit_ki,
            "win_start": max(0, entry_ki - 10),
            "win_end": min(len(klines) - 1, latest + 10),
        })
    return contexts


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
        tick_symbol: str,
        interval: str,
        use_tick_mode: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._strategy = strategy
        self._bt_cfg   = bt_cfg
        self._slices   = slices
        self._symbol   = symbol
        self._tick_symbol = tick_symbol
        self._interval = interval
        self._use_tick_mode = use_tick_mode
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
            segment_symbols = getattr(sl, "segment_symbols", []) or []
            for idx, (start_ms, end_ms) in enumerate(sl.segments):
                tick_symbol = segment_symbols[idx] if idx < len(segment_symbols) else self._tick_symbol
                self.progress.emit(f"Loading klines {sl.label}…")
                klines = load_klines_fn(self._symbol, self._interval, start_ms, end_ms)
                all_klines.extend(klines)

                if self._use_tick_mode:
                    self.progress.emit(f"Loading ticks {sl.label} ({tick_symbol})…")
                    ticks = load_ticks_fn(tick_symbol, start_ms, end_ms)
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
        result["strategy_name"] = getattr(self._strategy, "name", self._strategy.__class__.__name__)
        result["backtest_start_ms"] = all_klines[0].open_time if all_klines else 0
        result["backtest_end_ms"] = all_klines[-1].open_time if all_klines else 0
        result["tick_coverage_pct"] = None
        result["fallback_bar_count"] = 0
        result["_snapshot_klines"] = all_klines
        result["_snapshot_tick_map"] = tick_map
        result["_snapshot_signals"] = signals
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
                custom_fee_rate = self._bt_cfg.custom_fee_rate,
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
        self._last_stats: dict | None = None
        self._selected_trade: dict | None = None
        self._snapshot_contexts: list[dict] = []
        self._snapshot_klines = []
        self._snapshot_tick_map = None
        self._snapshot_dialog: Optional[TradeSnapshotDialog] = None
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

        # 狀態與結果工具列
        status_row = QWidget()
        status_layout = QHBoxLayout(status_row)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(6)
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(
            "color: #787b86; font-size: 11px; padding: 2px 8px;"
        )
        status_layout.addWidget(self._status_label, stretch=1)

        self._result_btn = QPushButton("Result / Excel")
        self._result_btn.setEnabled(False)
        self._result_btn.setStyleSheet(
            "QPushButton { background:#1e222d; color:#d1d4dc; border:1px solid #2a2e39; "
            "border-radius:3px; padding:2px 8px; }"
            "QPushButton:hover { background:#2a2e39; }"
            "QPushButton:disabled { color:#555; }"
        )
        self._snapshot_btn = QPushButton("Snapshot")
        self._snapshot_btn.setEnabled(False)
        self._snapshot_btn.setStyleSheet(
            "QPushButton { background:#1e222d; color:#80cbc4; border:1px solid #26a69a; "
            "border-radius:3px; padding:2px 8px; }"
            "QPushButton:hover { background:#1a3a3a; }"
            "QPushButton:disabled { color:#555; border-color:#333; }"
        )
        status_layout.addWidget(self._snapshot_btn)
        status_layout.addWidget(self._result_btn)
        right_layout.addWidget(status_row)

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
        self._ledger.trade_selected.connect(self._on_trade_selected)
        self._ledger.trade_activated.connect(self._on_trade_snapshot_requested)
        self._snapshot_btn.clicked.connect(self._open_selected_snapshot)
        self._result_btn.clicked.connect(self._open_result_dialog)

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
        self._result_btn.setEnabled(False)
        self._snapshot_btn.setEnabled(False)
        self._selected_trade = None
        self._status_label.setText("Loading data…")

        self._worker = BacktestWorkerThread(
            strategy = strategy,
            bt_cfg   = bt_cfg,
            slices   = slices,
            symbol   = self._config.symbol(),
            tick_symbol = self._config.tick_symbol(),
            interval = self._config.interval(),
            use_tick_mode = self._config.use_tick_mode(),
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
        self._last_stats = stats
        self._result_btn.setEnabled(True)
        self._prepare_snapshot_contexts(stats)
        self._snapshot_btn.setEnabled(bool(self._snapshot_contexts))
        self._status_label.setText(
            f"Done — {stats.get('trades', 0)} trades | "
            f"Win: {stats.get('win_rate', 0):.1f}% | "
            f"PF: {stats.get('profit_factor', 0):.2f}"
        )

        self._metrics.load_result(stats)
        self._equity.load_result(stats)
        self._mfe_mae.load_result(stats)
        self._ledger.load_result(stats)
        self._heatmap.load_result(stats)

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
        self._snapshot_btn.setEnabled(False)

    def _prepare_snapshot_contexts(self, stats: dict) -> None:
        self._snapshot_contexts = []
        self._snapshot_klines = stats.get("_snapshot_klines", []) or []
        self._snapshot_tick_map = stats.get("_snapshot_tick_map")
        signals = stats.get("_snapshot_signals", []) or []
        trade_list = stats.get("trade_list", []) or []
        if self._snapshot_klines and signals and trade_list:
            self._snapshot_contexts = _collect_contexts(
                signals,
                trade_list,
                self._snapshot_klines,
            )
        if not self._snapshot_contexts and self._snapshot_klines and trade_list:
            self._snapshot_contexts = _fallback_snapshot_contexts(
                trade_list,
                self._snapshot_klines,
            )

    def _on_trade_snapshot_requested(self, trade: dict) -> None:
        if not self._snapshot_contexts and self._last_stats:
            self._prepare_snapshot_contexts(self._last_stats)
            self._snapshot_btn.setEnabled(bool(self._snapshot_contexts))
        if not self._snapshot_contexts:
            QMessageBox.information(self, "Snapshot", "No snapshot context is available for this result.")
            return
        start_idx = 0
        for idx, ctx in enumerate(self._snapshot_contexts):
            ctx_trade = ctx.get("trade", {})
            if ctx_trade is trade or (
                ctx_trade.get("entry_time") == trade.get("entry_time")
                and ctx_trade.get("exit_time") == trade.get("exit_time")
                and ctx_trade.get("dir") == trade.get("dir")
            ):
                start_idx = idx
                break
        self._snapshot_dialog = TradeSnapshotDialog(
            self._snapshot_contexts,
            self._snapshot_klines,
            self._snapshot_tick_map,
            start_idx=start_idx,
            parent=self,
        )
        self._snapshot_dialog.show()
        self._snapshot_dialog.raise_()
        self._snapshot_dialog.activateWindow()

    def _on_trade_selected(self, trade: dict) -> None:
        self._selected_trade = trade
        self._snapshot_btn.setEnabled(bool(self._snapshot_contexts))

    def _open_selected_snapshot(self) -> None:
        trade = self._selected_trade
        if trade is None and self._last_stats:
            trade = next(
                (t for t in self._last_stats.get("trade_list", []) if not t.get("skipped")),
                None,
            )
        if trade is not None:
            self._on_trade_snapshot_requested(trade)

    def _open_result_dialog(self) -> None:
        if not self._last_stats:
            return
        from ui.main_window import BacktestResultDialog
        dlg = BacktestResultDialog(
            self._last_stats,
            klines=self._snapshot_klines or None,
            tick_map=self._snapshot_tick_map,
            signals=self._last_stats.get("_snapshot_signals") or None,
            parent=self,
        )
        dlg.exec()

    # ── 熱力圖（外部調用）────────────────────────────────────────────────────

    def load_optimization(self, results: list[dict], x_label: str, y_label: str, metric: str) -> None:
        """由外部優化工具調用，載入熱力圖資料。"""
        self._heatmap.load_optimization(results, x_label, y_label, metric)
