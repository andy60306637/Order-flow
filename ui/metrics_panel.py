"""頂部效能指標列：Win Rate / Profit Factor / Max Drawdown / Sharpe / Total Return。"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget


_METRICS = [
    ("win_rate",         "Win Rate",       "{:.1f}%",  "#26a69a"),
    ("profit_factor",    "Profit Factor",  "{:.2f}",   "#26a69a"),
    ("max_drawdown_pct", "Max Drawdown",   "{:.1f}%",  "#ef5350"),
    ("sharpe_ratio",     "Sharpe Ratio",   "{:.2f}",   "#26a69a"),
    ("total_return_pct", "Total Return",   "{:.1f}%",  "#26a69a"),
    ("trades",           "Trades",         "{:d}",     "#d1d4dc"),
]


class _MetricCard(QWidget):
    def __init__(self, label: str, color: str, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(2)

        self._label = QLabel(label)
        self._label.setStyleSheet("color: #787b86; font-size: 10px;")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._value = QLabel("—")
        self._value.setStyleSheet(
            f"color: {color}; font-size: 18px; font-weight: bold;"
        )
        self._value.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(self._label)
        layout.addWidget(self._value)

    def set_value(self, text: str, color: str | None = None) -> None:
        self._value.setText(text)
        if color:
            self._value.setStyleSheet(
                f"color: {color}; font-size: 18px; font-weight: bold;"
            )


class MetricsPanel(QWidget):
    """水平排列的回測關鍵指標列。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(70)
        self.setStyleSheet("background-color: #1e222d; border-bottom: 1px solid #2a2e39;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(0)

        self._cards: dict[str, _MetricCard] = {}
        for key, label, fmt, color in _METRICS:
            card = _MetricCard(label, color)
            self._cards[key] = card
            layout.addWidget(card)

            if key != _METRICS[-1][0]:
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.VLine)
                sep.setStyleSheet("color: #2a2e39;")
                layout.addWidget(sep)

    def load_result(self, stats: dict) -> None:
        """從 simulate_trades() 回傳的 stats dict 更新所有指標。"""
        for key, _label, fmt, default_color in _METRICS:
            card = self._cards[key]
            val = stats.get(key)
            if val is None:
                card.set_value("—")
                continue

            try:
                if key == "trades":
                    text = fmt.format(int(val))
                else:
                    text = fmt.format(float(val))
            except (ValueError, TypeError):
                text = str(val)

            # 動態顏色
            color = default_color
            if key == "win_rate":
                color = "#26a69a" if float(val) >= 50 else "#ef5350"
            elif key == "max_drawdown_pct":
                color = "#ef5350" if float(val) > 10 else "#26a69a"
            elif key == "total_return_pct":
                color = "#26a69a" if float(val) >= 0 else "#ef5350"
            elif key == "sharpe_ratio":
                color = "#26a69a" if float(val) >= 0 else "#ef5350"

            card.set_value(text, color)

    def clear(self) -> None:
        for card in self._cards.values():
            card.set_value("—")
