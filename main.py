"""
OrderFlow — Binance Futures Order Flow 分析工具
入口點
"""
import os
import sys
import logging

# ── 確保 pyqtgraph 使用 PyQt6 ─────────────────────────────────────────────────
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPalette, QColor
from PyQt6.QtCore import Qt

import pyqtgraph as pg

from config.base import APP_NAME
from ui.main_window import MainWindow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _apply_dark_palette(app: QApplication) -> None:
    """套用全局深色主題。"""
    palette = QPalette()

    dark   = QColor("#131722")
    panel  = QColor("#1e222d")
    text   = QColor("#d1d4dc")
    dim    = QColor("#787b86")
    accent = QColor("#2962ff")
    highlight = QColor("#2a2e39")

    palette.setColor(QPalette.ColorRole.Window,          dark)
    palette.setColor(QPalette.ColorRole.WindowText,      text)
    palette.setColor(QPalette.ColorRole.Base,            panel)
    palette.setColor(QPalette.ColorRole.AlternateBase,   dark)
    palette.setColor(QPalette.ColorRole.ToolTipBase,     panel)
    palette.setColor(QPalette.ColorRole.ToolTipText,     text)
    palette.setColor(QPalette.ColorRole.Text,            text)
    palette.setColor(QPalette.ColorRole.Button,          panel)
    palette.setColor(QPalette.ColorRole.ButtonText,      text)
    palette.setColor(QPalette.ColorRole.BrightText,      QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Highlight,       accent)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Link,            accent)
    palette.setColor(QPalette.ColorRole.Midlight,        highlight)
    palette.setColor(QPalette.ColorRole.Mid,             dim)
    palette.setColor(QPalette.ColorRole.Shadow,          QColor("#000000"))

    app.setPalette(palette)
    app.setStyleSheet(
        """
        QComboBox, QToolBar, QLabel, QTabWidget, QTabBar::tab {
            background-color: #1e222d;
            color: #d1d4dc;
            border: 1px solid #2a2e39;
            border-radius: 3px;
            padding: 2px 6px;
        }
        QTabBar::tab:selected {
            background-color: #2962ff;
            color: #ffffff;
        }
        QSplitter::handle {
            background: #2a2e39;
        }
        QComboBox::drop-down {
            border: none;
        }
        QComboBox QAbstractItemView {
            background-color: #1e222d;
            color: #d1d4dc;
            selection-background-color: #2962ff;
        }
        """
    )


def main() -> None:
    # High-DPI 支援
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)

    _apply_dark_palette(app)

    # pyqtgraph 全局預設
    import platform
    pg.setConfigOptions(
        antialias=False,
        useOpenGL=(platform.system() != "Windows"),  # Linux/macOS 啟用 OpenGL
        background=None,
        foreground="#d1d4dc",
    )

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
