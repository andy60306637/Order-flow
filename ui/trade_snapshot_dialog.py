"""
Trade snapshot viewer — zero-dependency (pyqtgraph only).

Displays a candlestick chart centred on each trade, marking:
  • k0 confirmation bar  (orange background)
  • entry arrow          (blue ▲)
  • exit arrow           (green/red ▼)
  • stop-loss line       (dashed red)
  • take-profit line     (dashed green)
  • tick scatter         (tiny buy/sell dots on entry bar, tick mode only)

Navigation: ← / → buttons step through all trades.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QLineF
from PyQt6.QtGui import QColor, QPen, QBrush, QPicture, QPainter, QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QSizePolicy, QWidget,
)

import config
from core.data_types import Kline
from strategies.base import StrategySignal

# ─── 顏色常數 ─────────────────────────────────────────────────────────────────
_C_UP      = QColor("#26a69a")
_C_DOWN    = QColor("#ef5350")
_C_K0      = QColor("#ff9800")
_C_ENTRY   = QColor("#2196f3")
_C_FILL    = QColor("#1565c0")
_C_TP      = QColor("#26a69a")
_C_SL      = QColor("#ef5350")
_C_TS      = QColor("#ff9800")
_C_TD_EXIT = QColor("#ce93d8")
_C_BG      = QColor("#131722")
_C_GRID    = QColor("#2a2e39")


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _find_ki(klines: List[Kline], open_time_ms: int) -> Optional[int]:
    lo, hi = 0, len(klines) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if klines[mid].open_time == open_time_ms:
            return mid
        elif klines[mid].open_time < open_time_ms:
            lo = mid + 1
        else:
            hi = mid - 1
    return None


def _collect_contexts(
    signals: List[StrategySignal],
    trade_list: List[dict],
    klines: List[Kline],
    context_bars: int = 10,
) -> List[dict]:
    """Build one context dict per trade with k0/entry/exit bar indices."""
    sig_by_time: Dict[int, List[StrategySignal]] = defaultdict(list)
    for s in signals:
        sig_by_time[s.open_time].append(s)

    k0_long_signals  = [s for s in signals if s.signal_type == "k0_long"]
    k0_short_signals = [s for s in signals if s.signal_type == "k0_short"]

    result = []
    for ti, trade in enumerate(trade_list):
        if trade.get("skipped"):
            continue
        entry_time = trade.get("entry_time", 0)
        exit_time  = trade.get("exit_time",  0)
        direction  = trade.get("dir", "long")

        if direction == "short":
            entry_type, exit_type, k0_pool = "short_entry", "short_exit", k0_short_signals
        else:
            entry_type, exit_type, k0_pool = "long_entry",  "long_exit",  k0_long_signals

        entry_sig = next(
            (s for s in sig_by_time.get(entry_time, []) if s.signal_type == entry_type),
            None,
        )
        exit_sig = next(
            (s for s in sig_by_time.get(exit_time, []) if s.signal_type == exit_type),
            None,
        )
        if entry_sig is None:
            continue

        k0_sig = next(
            (k0 for k0 in reversed(k0_pool) if k0.open_time <= entry_time),
            None,
        )

        entry_ki = _find_ki(klines, entry_time)
        exit_ki  = _find_ki(klines, exit_time) if exit_time else None
        k0_ki    = _find_ki(klines, k0_sig.open_time) if k0_sig else None

        earliest = min(
            x for x in [entry_ki, k0_ki] if x is not None
        ) if entry_ki is not None else 0
        latest = exit_ki if exit_ki is not None else (entry_ki or 0)

        result.append({
            "trade":        trade,
            "trade_idx":    ti,
            "k0_signal":    k0_sig,
            "entry_signal": entry_sig,
            "exit_signal":  exit_sig,
            "k0_ki":        k0_ki,
            "entry_ki":     entry_ki,
            "exit_ki":      exit_ki,
            "win_start":    max(0, earliest - context_bars),
            "win_end":      min(len(klines) - 1, latest + context_bars),
        })
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Candlestick + Volume items (self-contained, no extra deps)
# ═══════════════════════════════════════════════════════════════════════════════

class _CandleItem(pg.GraphicsObject):
    """Draws an OHLC candlestick set from a window of Klines."""

    def __init__(self) -> None:
        super().__init__()
        self._picture = QPicture()
        self._bounds: Optional[tuple] = None
        self._klines: List[Kline] = []

    def set_klines(self, klines: List[Kline]) -> None:
        self._klines = klines
        self._render()
        self.prepareGeometryChange()
        self.update()

    def _render(self) -> None:
        p = QPainter(self._picture)
        p.setPen(pg.mkPen(None))
        self._bounds = None
        all_lows, all_highs = [], []
        for i, k in enumerate(self._klines):
            color = _C_UP if k.close >= k.open else _C_DOWN
            # cosmetic pen: width=0 → always 1px regardless of coordinate scale
            wick_pen = QPen(color, 0)
            brush = QBrush(color)
            # Body first (drawn beneath wick lines)
            body_lo = min(k.open, k.close)
            body_hi = max(k.open, k.close)
            body_h  = max(body_hi - body_lo, (k.high - k.low) * 0.002)
            p.setPen(wick_pen)
            p.setBrush(brush)
            p.drawRect(
                pg.QtCore.QRectF(i - 0.3, body_lo, 0.6, body_h)
            )
            # Wick on top (use QLineF for unambiguous PyQt6 dispatch)
            p.setBrush(pg.mkBrush(None))
            p.drawLine(QLineF(i, k.low,  i, body_lo))   # lower wick
            p.drawLine(QLineF(i, body_hi, i, k.high))   # upper wick
            all_lows.append(k.low)
            all_highs.append(k.high)
        if all_lows:
            self._bounds = (0, min(all_lows), len(self._klines) - 1, max(all_highs))
        p.end()

    def paint(self, p, *args):
        p.drawPicture(0, 0, self._picture)

    def boundingRect(self):
        if self._bounds:
            x0, y0, x1, y1 = self._bounds
            return pg.QtCore.QRectF(x0, y0, x1 - x0 + 1, y1 - y0)
        return pg.QtCore.QRectF(0, 0, 1, 1)


class _VolItem(pg.GraphicsObject):
    """Volume bars mirroring candlestick colour."""

    def __init__(self) -> None:
        super().__init__()
        self._picture = QPicture()
        self._bounds: Optional[tuple] = None

    def set_klines(self, klines: List[Kline]) -> None:
        p = QPainter(self._picture)
        max_v = max((k.volume for k in klines), default=1.0)
        for i, k in enumerate(klines):
            color = _C_UP if k.close >= k.open else _C_DOWN
            p.setPen(QPen(color, 0))
            alpha_color = QColor(color)
            alpha_color.setAlphaF(0.6)
            p.setBrush(QBrush(alpha_color))
            p.drawRect(pg.QtCore.QRectF(i - 0.3, 0, 0.6, k.volume))
        p.end()
        self._bounds = (0, 0, max(len(klines), 1), max_v)
        self.prepareGeometryChange()
        self.update()

    def paint(self, p, *args):
        p.drawPicture(0, 0, self._picture)

    def boundingRect(self):
        if self._bounds:
            x0, y0, x1, y1 = self._bounds
            return pg.QtCore.QRectF(x0, y0, x1, y1)
        return pg.QtCore.QRectF(0, 0, 1, 1)


# ═══════════════════════════════════════════════════════════════════════════════
# Snapshot Panel (single chart + vol)
# ═══════════════════════════════════════════════════════════════════════════════

class _SnapshotPanel(QWidget):
    """Pyqtgraph-based trade snapshot panel (no matplotlib)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # ── title ───────────────────────────────────────────────────────────
        self._title_lbl = QLabel()
        self._title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_lbl.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #d1d4dc; padding: 4px;"
        )
        layout.addWidget(self._title_lbl)

        # ── graphics layout ──────────────────────────────────────────────────
        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground(_C_BG)
        layout.addWidget(self._glw, stretch=1)

        # ── candle plot ──────────────────────────────────────────────────────
        self._time_axis = pg.AxisItem("bottom")
        self._time_axis.setStyle(tickLength=4)
        self._price_plot: pg.PlotItem = self._glw.addPlot(
            row=0, col=0, axisItems={"bottom": self._time_axis}
        )
        self._price_plot.showGrid(x=True, y=True, alpha=0.2)
        self._price_plot.getAxis("left").setStyle(tickLength=4)
        self._price_plot.getAxis("bottom").setStyle(tickLength=4)

        # ── vol plot ─────────────────────────────────────────────────────────
        self._glw.nextRow()
        self._vol_plot: pg.PlotItem = self._glw.addPlot(row=1, col=0)
        self._vol_plot.setXLink(self._price_plot)
        self._vol_plot.showGrid(x=True, y=True, alpha=0.15)
        self._vol_plot.setMaximumHeight(90)
        self._vol_plot.getAxis("left").setStyle(tickLength=3)
        self._vol_plot.getAxis("bottom").setVisible(False)

        # style axes
        for plot in (self._price_plot, self._vol_plot):
            for axis_name in ("left", "right", "bottom", "top"):
                ax = plot.getAxis(axis_name)
                ax.setPen(pg.mkPen(_C_GRID))
                ax.setTextPen(pg.mkPen("#787b86"))

        # ── items (replaced each render) ─────────────────────────────────────
        self._candle_item: Optional[_CandleItem] = None
        self._vol_item:    Optional[_VolItem]    = None
        self._overlays:   List = []   # InfiniteLines, ScatterPlots, TextItems etc.
        self._timestamps: List[int] = []

        # ── 十字線（常駐，不隨 render 清除）─────────────────────────────────
        _ch_pen = pg.mkPen('#888888', width=0.8, style=Qt.PenStyle.DashLine)
        self._ch_vline = pg.InfiniteLine(angle=90, pen=_ch_pen, movable=False)
        self._ch_hline = pg.InfiniteLine(angle=0,  pen=_ch_pen, movable=False)
        self._price_plot.addItem(self._ch_vline, ignoreBounds=True)
        self._price_plot.addItem(self._ch_hline, ignoreBounds=True)
        self._ch_vline.setVisible(False)
        self._ch_hline.setVisible(False)

        # 十字線讀數標籤
        self._ch_label = pg.TextItem(
            text="", color="#d1d4dc", anchor=(0.0, 1.0),
            fill=pg.mkBrush(QColor(30, 34, 45, 200)),
        )
        self._ch_label.setFont(QFont("monospace", 8))
        self._price_plot.addItem(self._ch_label)
        self._ch_label.setVisible(False)

        # 連接滑鼠移動訊號
        self._glw.scene().sigMouseMoved.connect(self._on_mouse_moved)

    # ── public ───────────────────────────────────────────────────────────────

    def render(
        self,
        ctx: dict,
        all_klines: List[Kline],
        tick_map: Optional[dict],
    ) -> None:
        self._clear_overlays()
        trade    = ctx["trade"]
        start    = ctx["win_start"]
        end      = ctx["win_end"]
        window   = all_klines[start : end + 1]
        n        = len(window)
        if n == 0:
            return

        self._timestamps = [k.open_time for k in window]
        self._update_time_axis(window)

        # ── candlestick ───────────────────────────────────────────────────────
        if self._candle_item:
            self._price_plot.removeItem(self._candle_item)
        self._candle_item = _CandleItem()
        self._candle_item.set_klines(window)
        self._price_plot.addItem(self._candle_item)

        # ── volume ────────────────────────────────────────────────────────────
        if self._vol_item:
            self._vol_plot.removeItem(self._vol_item)
        self._vol_item = _VolItem()
        self._vol_item.set_klines(window)
        self._vol_plot.addItem(self._vol_item)

        # ── 收集所有關鍵價格，統一計算 Y range ─────────────────────────────────
        candle_prices = [p for k in window for p in (k.high, k.low)]
        p_min_raw, p_max_raw = min(candle_prices), max(candle_prices)

        entry_price = trade["entry"]
        exit_price  = trade["exit"]
        stop_p      = trade.get("stop")
        entry_ki    = ctx.get("entry_ki")

        # TP 從 entry/stop 反推（rr=1.0 預設）
        direction = trade.get("dir", "long")
        tp_p: Optional[float] = None
        if stop_p and entry_price:
            risk = entry_price - stop_p  # 正數 = long（stop 在下方）；負數 = short（stop 在上方）
            if direction == "short" and risk < 0:
                tp_p = entry_price + risk  # short TP = entry - |risk|（低於 entry）
            elif direction == "long" and risk > 0:
                tp_p = entry_price + risk  # long  TP = entry + risk（高於 entry）

        key_prices = candle_prices.copy()
        for v in (entry_price, exit_price, stop_p, tp_p):
            if v is not None:
                key_prices.append(v)

        y_min = min(key_prices)
        y_max = max(key_prices)
        pad   = max((y_max - y_min) * 0.06, 0.5)   # 至少 0.5 USDT
        y_min -= pad
        y_max += pad

        # 關閉自動 range，避免 InfiniteLine 撐開視野
        self._price_plot.vb.disableAutoRange()

        # ── helper：水平線段（取代 InfiniteLine，bounding rect 可控）──────────
        def _hline(price: float, color, style=Qt.PenStyle.DashLine, width=1):
            item = pg.PlotDataItem(
                x=[-0.5, n - 0.5], y=[price, price],
                pen=pg.mkPen(color, width=width, style=style),
            )
            self._price_plot.addItem(item)
            self._overlays.append(item)

        # ── helper：inline label 緊貼在指定 bar 旁 ────────────────────────────
        def _label(xi: float, y: float, text: str, color,
                   anchor=(0.0, 0.5), font_size=7):
            lbl = pg.TextItem(text=text, color=color, anchor=anchor)
            lbl.setFont(QFont("monospace", font_size))
            lbl.setPos(xi, y)
            self._price_plot.addItem(lbl)
            self._overlays.append(lbl)

        # ── k0 highlight ──────────────────────────────────────────────────────
        k0_ki  = ctx.get("k0_ki")
        if k0_ki is not None and start <= k0_ki <= end:
            xi = k0_ki - start
            k0_bar = all_klines[k0_ki]
            span = pg.LinearRegionItem(
                values=[xi - 0.45, xi + 0.45],
                orientation="vertical",
                brush=QBrush(QColor(255, 152, 0, 30)),
                pen=QPen(QColor(0, 0, 0, 0)),
                movable=False,
            )
            self._price_plot.addItem(span)
            self._overlays.append(span)
            # label 緊貼 k0 bar 低點內側（不往下推離）
            _label(xi, k0_bar.low, "k0", _C_K0, anchor=(0.5, 0.0), font_size=8)

        # ── stop-loss 水平線 + entry bar 上的 SL 標示 ────────────────────────
        if stop_p:
            _hline(stop_p, _C_SL)
            # 右側標籤（緊貼右邊界內）
            _label(n - 1.0, stop_p, f"SL {stop_p:.1f}", _C_SL,
                   anchor=(1.0, 1.0))
            # entry bar 上的 SL 標示點（讓使用者一眼看到停損距離）
            if entry_ki is not None and start <= entry_ki <= end:
                xi_e = entry_ki - start
                sl_mark = pg.ScatterPlotItem(
                    x=[xi_e], y=[stop_p],
                    symbol="x", size=11,
                    pen=pg.mkPen(_C_SL, width=2),
                    brush=QBrush(QColor(0, 0, 0, 0)),
                )
                self._price_plot.addItem(sl_mark)
                self._overlays.append(sl_mark)

        # ── take-profit 水平線 ─────────────────────────────────────────────────
        if tp_p is not None:
            _hline(tp_p, _C_TP)
            _label(n - 1.0, tp_p, f"TP {tp_p:.1f}", _C_TP,
                   anchor=(1.0, 0.0))

        # ── tick scatter on entry bar ─────────────────────────────────────────
        if tick_map and entry_ki is not None and start <= entry_ki <= end:
            entry_bar = all_klines[entry_ki]
            ticks = tick_map.get(entry_bar.open_time)
            if ticks is not None and len(ticks) > 0:
                xi = entry_ki - start
                prices_t = ticks[:, 1]
                is_bm    = ticks[:, 3] > 0.5
                buy_px   = prices_t[~is_bm.astype(bool)]
                sell_px  = prices_t[ is_bm.astype(bool)]
                if len(buy_px) > 0:
                    buy_sp = pg.ScatterPlotItem(
                        x=np.full(len(buy_px), xi) + np.linspace(-0.22, -0.04, len(buy_px)),
                        y=buy_px, size=2.5, pen=None,
                        brush=QBrush(QColor(38, 166, 154, 100)),
                    )
                    self._price_plot.addItem(buy_sp)
                    self._overlays.append(buy_sp)
                if len(sell_px) > 0:
                    sell_sp = pg.ScatterPlotItem(
                        x=np.full(len(sell_px), xi) + np.linspace(0.04, 0.22, len(sell_px)),
                        y=sell_px, size=2.5, pen=None,
                        brush=QBrush(QColor(239, 83, 80, 100)),
                    )
                    self._price_plot.addItem(sell_sp)
                    self._overlays.append(sell_sp)

        # ── entry marker ──────────────────────────────────────────────────────
        entry_sig = ctx.get("entry_signal")
        if entry_ki is not None and start <= entry_ki <= end:
            xi = entry_ki - start
            entry_sp = pg.ScatterPlotItem(
                x=[xi], y=[entry_price],
                symbol="t1", size=14,
                pen=pg.mkPen(_C_ENTRY, width=1.5),
                brush=QBrush(_C_ENTRY),
            )
            self._price_plot.addItem(entry_sp)
            self._overlays.append(entry_sp)
            _label(xi + 0.55, entry_price, f"Entry\n{entry_price:.1f}",
                   _C_ENTRY, anchor=(0.0, 0.5), font_size=8)

            # fill price（tick 模式實際成交價）
            fill_p = entry_sig.fill_price if entry_sig else None
            if fill_p and abs(fill_p - entry_price) > 0.01:
                fill_sp = pg.ScatterPlotItem(
                    x=[xi], y=[fill_p], symbol="d", size=10,
                    pen=pg.mkPen(_C_FILL, width=1),
                    brush=QBrush(_C_FILL),
                )
                self._price_plot.addItem(fill_sp)
                self._overlays.append(fill_sp)
                _label(xi + 0.55, fill_p, f"Fill {fill_p:.1f}",
                       _C_FILL, anchor=(0.0, 0.5), font_size=7)

        # ── exit marker ───────────────────────────────────────────────────────
        exit_ki    = ctx.get("exit_ki")
        exit_label = trade.get("exit_label", "")
        if exit_ki is not None and start <= exit_ki <= end:
            xi = exit_ki - start
            is_win   = trade["net_pnl"] > 0
            ex_color = _C_UP if is_win else _C_DOWN
            if exit_label == "TS":
                ex_color = _C_TS
            elif exit_label == "TD":
                ex_color = _C_TD_EXIT

            exit_sp = pg.ScatterPlotItem(
                x=[xi], y=[exit_price], symbol="t", size=14,
                pen=pg.mkPen(ex_color, width=1.5),
                brush=QBrush(ex_color),
            )
            self._price_plot.addItem(exit_sp)
            self._overlays.append(exit_sp)
            _label(xi + 0.55, exit_price,
                   f"{exit_label}\n{exit_price:.1f}",
                   ex_color, anchor=(0.0, 0.5), font_size=8)

        # ── 明確設定 Y/X range（停用 auto-range 後必須手動設）──────────────────
        self._price_plot.setXRange(-0.5, n - 0.5, padding=0)
        self._price_plot.setYRange(y_min, y_max, padding=0)
        self._vol_plot.setXRange(-0.5, n - 0.5, padding=0)
        self._vol_plot.vb.enableAutoRange(axis=self._vol_plot.vb.YAxis, enable=True)
        self._vol_plot.vb.updateAutoRange()

        # ── title ─────────────────────────────────────────────────────────────
        pnl   = trade["net_pnl"]
        pnl_s = f"+{pnl:.2f}" if pnl > 0 else f"{pnl:.2f}"
        pnl_c = "#26a69a" if pnl > 0 else "#ef5350"
        ti    = ctx["trade_idx"]
        entry_dt = datetime.fromtimestamp(
            trade["entry_time"] / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M")
        self._title_lbl.setText(
            f"<span style='color:#aaa'>Trade #{ti + 1}</span>  "
            f"<b>{trade['dir'].upper()}</b>  |  "
            f"Entry: <b>{entry_price:.1f}</b>  "
            f"Exit: <b>{exit_price:.1f}</b>  |  "
            f"<span style='color:{pnl_c}'><b>PnL {pnl_s} USDT</b></span>  |  "
            f"<span style='color:#80cbc4'>{exit_label}</span>  |  "
            f"<span style='color:#787b86'>{entry_dt} UTC</span>"
        )

    # ── 十字線 ────────────────────────────────────────────────────────────────

    def _on_mouse_moved(self, scene_pos) -> None:
        vb = self._price_plot.vb
        if vb.sceneBoundingRect().contains(scene_pos):
            pt = vb.mapSceneToView(scene_pos)
            self._ch_vline.setPos(pt.x())
            self._ch_hline.setPos(pt.y())
            self._ch_vline.setVisible(True)
            self._ch_hline.setVisible(True)

            # 根據游標位置動態選擇標籤對齊方向（避免超出邊界）
            x_range = vb.viewRange()[0]
            x_mid   = (x_range[0] + x_range[1]) / 2
            y_range = vb.viewRange()[1]
            y_mid   = (y_range[0] + y_range[1]) / 2
            ax = (0.0, 1.0) if pt.x() < x_mid else (1.0, 1.0)
            ay = ax[0], (0.0 if pt.y() > y_mid else 1.0)
            self._ch_label.setAnchor(ay)

            # 時間標示（從 _timestamps 查 bar index）
            xi = int(round(pt.x()))
            if 0 <= xi < len(self._timestamps):
                dt = datetime.fromtimestamp(
                    self._timestamps[xi] / 1000, tz=timezone.utc
                )
                time_str = dt.strftime("%m/%d %H:%M")
            else:
                time_str = ""

            self._ch_label.setText(f"{pt.y():.2f}  {time_str}")
            self._ch_label.setPos(pt.x(), pt.y())
            self._ch_label.setVisible(True)
        else:
            self._ch_vline.setVisible(False)
            self._ch_hline.setVisible(False)
            self._ch_label.setVisible(False)

    # ── private ──────────────────────────────────────────────────────────────

    def _clear_overlays(self) -> None:
        for item in self._overlays:
            try:
                self._price_plot.removeItem(item)
            except Exception:
                pass
        self._overlays.clear()

    def _update_time_axis(self, window: List[Kline]) -> None:
        timestamps = [k.open_time for k in window]

        def tickStrings(values, scale, spacing):
            result = []
            for v in values:
                idx = int(round(v))
                if 0 <= idx < len(timestamps):
                    dt = datetime.fromtimestamp(
                        timestamps[idx] / 1000, tz=timezone.utc
                    )
                    result.append(dt.strftime("%m/%d\n%H:%M"))
                else:
                    result.append("")
            return result

        self._time_axis.tickStrings = tickStrings


# ═══════════════════════════════════════════════════════════════════════════════
# Trade Snapshot Dialog
# ═══════════════════════════════════════════════════════════════════════════════

class TradeSnapshotDialog(QDialog):
    """
    Opens a navigable trade snapshot viewer.

    Parameters
    ----------
    contexts   : list of context dicts (from _collect_contexts)
    all_klines : full kline list used in the backtest
    tick_map   : optional tick_map (bar open_time → ndarray)
    start_idx  : which trade to show first (0-based)
    """

    def __init__(
        self,
        contexts: List[dict],
        all_klines: List[Kline],
        tick_map: Optional[dict],
        start_idx: int = 0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._contexts   = contexts
        self._all_klines = all_klines
        self._tick_map   = tick_map
        self._idx        = max(0, min(start_idx, len(contexts) - 1))

        self.setWindowTitle("交易快照")
        self.setMinimumSize(1100, 620)
        self.setStyleSheet(
            f"QDialog {{ background: {config.COLOR_BG}; color: {config.COLOR_FG}; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # ── panel ─────────────────────────────────────────────────────────────
        self._panel = _SnapshotPanel()
        layout.addWidget(self._panel, stretch=1)

        # ── nav bar ───────────────────────────────────────────────────────────
        _btn_style = (
            "QPushButton { background:#1e222d; color:#d1d4dc;"
            " border:1px solid #2a2e39; border-radius:3px; padding:3px 12px; }"
            "QPushButton:hover   { background:#2a2e39; }"
            "QPushButton:disabled { color:#444; }"
        )
        nav_row = QHBoxLayout()

        self._prev_btn = QPushButton("◀ 上一筆")
        self._prev_btn.setStyleSheet(_btn_style)
        self._prev_btn.clicked.connect(self._on_prev)
        nav_row.addWidget(self._prev_btn)

        self._counter_lbl = QLabel()
        self._counter_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._counter_lbl.setStyleSheet("color:#aaa; font-size:12px;")
        nav_row.addWidget(self._counter_lbl, stretch=1)

        self._next_btn = QPushButton("下一筆 ▶")
        self._next_btn.setStyleSheet(_btn_style)
        self._next_btn.clicked.connect(self._on_next)
        nav_row.addWidget(self._next_btn)

        layout.addLayout(nav_row)

        self._render_current()

    # ── navigation ────────────────────────────────────────────────────────────

    def _render_current(self) -> None:
        n   = len(self._contexts)
        ctx = self._contexts[self._idx]
        self._panel.render(ctx, self._all_klines, self._tick_map)
        self._counter_lbl.setText(f"{self._idx + 1} / {n}")
        self._prev_btn.setEnabled(self._idx > 0)
        self._next_btn.setEnabled(self._idx < n - 1)

    def _on_prev(self) -> None:
        if self._idx > 0:
            self._idx -= 1
            self._render_current()

    def _on_next(self) -> None:
        if self._idx < len(self._contexts) - 1:
            self._idx += 1
            self._render_current()
