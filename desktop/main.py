"""
OrderFlow Desktop — Binance Futures Order Flow 分析工具
Desktop 端入口點（新架構）

等效於根目錄 main.py，但使用 desktop/ 結構。
遷移完成後將取代根目錄 main.py。
"""
import os
import sys
import logging

# 確保 project root 在 sys.path（讓 import config / core / strategies 正常運作）
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPalette, QColor
from PyQt6.QtCore import Qt

import pyqtgraph as pg

# 仍從原 ui/ 目錄載入 MainWindow（Phase 2.2 完成前不改動）
from ui.main_window import MainWindow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _apply_dark_palette(app: QApplication) -> None:
    dark   = QColor("#131722")
    panel  = QColor("#1e222d")
    text   = QColor("#d1d4dc")
    dim    = QColor("#787b86")
    accent = QColor("#2962ff")
    highlight = QColor("#2a2e39")

    palette = QPalette()
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
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("OrderFlow")
    app.setOrganizationName("OrderFlow")

    _apply_dark_palette(app)

    pg.setConfigOptions(
        antialias=False,
        useOpenGL=False,
        background=None,
        foreground="#d1d4dc",
    )

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
