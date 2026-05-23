"""
Order Book 買賣盤小工具。

使用自訂 paintEvent 渲染，效能優於 QTableWidget。
布局（上→下）：
  - 賣側（N 檔，price ↑，紅色，量條靠右對齊）
  - 價差列
  - 買側（N 檔，price ↓，綠色，量條靠左對齊）
"""
from __future__ import annotations
from typing import List, Tuple

from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QColor, QPainter, QFont, QFontMetrics
from PyQt6.QtWidgets import QWidget, QSizePolicy

import config
from ui.fonts import mono


_COLOR_BID_BAR  = QColor(38,  166, 154, 55)   # 半透明綠
_COLOR_ASK_BAR  = QColor(239,  83,  80, 55)   # 半透明紅
_COLOR_BID_TEXT = QColor(38,  166, 154)
_COLOR_ASK_TEXT = QColor(239,  83,  80)
_COLOR_FG       = QColor(209, 212, 220)
_COLOR_BG       = QColor(19,  23,  34)
_COLOR_SEP      = QColor(50,  55,  70)
_COLOR_SPREAD   = QColor(180, 180, 140)


class OrderBookWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._bids: List[Tuple[float, float]] = []
        self._asks: List[Tuple[float, float]] = []
        self._max_qty: float = 1.0
        self._last_price: float = 0.0

        self.setMinimumWidth(180)
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self.setAutoFillBackground(False)

        self._font = mono(9)
        self._font_sm = mono(8)

    # ──────────────────────────────────────────────────────────────────────────
    def update_ob(
        self,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
        last_price: float = 0.0,
    ) -> None:
        self._bids = bids[: config.OB_DISPLAY_LEVELS]
        self._asks = asks[: config.OB_DISPLAY_LEVELS]
        self._last_price = last_price
        all_qty = [q for _, q in self._bids] + [q for _, q in self._asks]
        self._max_qty = max(all_qty, default=1.0) or 1.0
        self.update()

    # ──────────────────────────────────────────────────────────────────────────
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        W = self.width()
        H = self.height()

        # 背景
        p.fillRect(0, 0, W, H, _COLOR_BG)

        n_ask = len(self._asks)
        n_bid = len(self._bids)
        total_rows = n_ask + 1 + n_bid  # 1 = spread 列
        if total_rows == 0:
            p.end()
            return

        row_h = max(H / (total_rows + 1), 14.0)
        fm = QFontMetrics(self._font)

        # ── 賣側（由高到低：index 0 = 最差賣單，顯示在最上方）─────────────────
        asks_display = list(reversed(self._asks))  # 高→低
        for i, (price, qty) in enumerate(asks_display):
            y = i * row_h
            bar_w = (qty / self._max_qty) * W * 0.55
            # 量條（靠右）
            p.fillRect(
                QRectF(W - bar_w, y + 1, bar_w, row_h - 2), _COLOR_ASK_BAR
            )
            # 價格
            p.setFont(self._font)
            p.setPen(_COLOR_ASK_TEXT)
            p.drawText(
                QRectF(4, y, W * 0.52, row_h),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                _fmt_price(price),
            )
            # 數量
            p.setPen(_COLOR_FG)
            p.drawText(
                QRectF(W * 0.52, y, W * 0.46, row_h),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                _fmt_qty(qty),
            )

        # ── 價差列 ──────────────────────────────────────────────────────────
        sep_y = n_ask * row_h
        p.fillRect(QRectF(0, sep_y, W, row_h), _COLOR_SEP)
        if self._asks and self._bids:
            spread = self._asks[0][0] - self._bids[0][0]
            mid    = (self._asks[0][0] + self._bids[0][0]) / 2
            txt    = f"{_fmt_price(mid)}  (±{spread:.4g})"
        else:
            txt = "─"
        p.setFont(self._font_sm)
        p.setPen(_COLOR_SPREAD)
        p.drawText(
            QRectF(0, sep_y, W, row_h),
            Qt.AlignmentFlag.AlignCenter,
            txt,
        )

        # ── 買側（由高到低）──────────────────────────────────────────────────
        for i, (price, qty) in enumerate(self._bids):
            y = (n_ask + 1 + i) * row_h
            bar_w = (qty / self._max_qty) * W * 0.55
            # 量條（靠左）
            p.fillRect(QRectF(0, y + 1, bar_w, row_h - 2), _COLOR_BID_BAR)
            # 價格
            p.setFont(self._font)
            p.setPen(_COLOR_BID_TEXT)
            p.drawText(
                QRectF(4, y, W * 0.52, row_h),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                _fmt_price(price),
            )
            # 數量
            p.setPen(_COLOR_FG)
            p.drawText(
                QRectF(W * 0.52, y, W * 0.46, row_h),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                _fmt_qty(qty),
            )

        p.end()


# ── helpers ───────────────────────────────────────────────────────────────────
def _fmt_price(p: float) -> str:
    if p >= 1000:
        return f"{p:,.1f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.6f}"


def _fmt_qty(q: float) -> str:
    if q >= 1000:
        return f"{q:,.1f}"
    if q >= 1:
        return f"{q:.3f}"
    return f"{q:.5f}"
