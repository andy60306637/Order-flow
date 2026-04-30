from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from backtest.time_slice import TimeSlice
from research.base import FACTOR_SIDE_LONG
from ui.research_lab import ResearchLab


class ResearchLabUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_research_lab_constructs_with_factor_list(self) -> None:
        widget = ResearchLab()
        self.assertGreater(widget._factor_list.count(), 0)
        self.assertIsNotNone(widget._time_slice)

    def test_factor_side_filter_hides_non_matching_factors(self) -> None:
        widget = ResearchLab()
        widget._restore_done = False
        widget._factor_group_filter.setCurrentIndex(0)
        idx = widget._factor_side_filter.findData(FACTOR_SIDE_LONG)
        widget._factor_side_filter.setCurrentIndex(idx)

        hidden = {}
        for i in range(widget._factor_list.count()):
            item = widget._factor_list.item(i)
            hidden[str(item.data(Qt.ItemDataRole.UserRole))] = item.isHidden()

        self.assertFalse(hidden["lower_wick_to_body_ratio"])
        self.assertTrue(hidden["upper_wick_to_body_ratio"])

    def test_research_parameters_flow_into_config(self) -> None:
        widget = ResearchLab()
        widget._time_slice.get_slices = lambda: [TimeSlice(label="test", segments=[(0, 60_000)])]
        widget._entry_lag_spin.setValue(2)
        widget._train_ratio_spin.setValue(0.65)

        config = widget._build_config()

        assert config is not None
        self.assertEqual(config.entry_lag, 2)
        self.assertAlmostEqual(config.train_ratio, 0.65)


if __name__ == "__main__":
    unittest.main()
