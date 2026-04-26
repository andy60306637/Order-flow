from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.research_lab import ResearchLab


class ResearchLabUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_research_lab_constructs_with_factor_list(self) -> None:
        widget = ResearchLab()
        self.assertGreater(widget._factor_list.count(), 0)
        self.assertIsNotNone(widget._time_slice)


if __name__ == "__main__":
    unittest.main()
