from pathlib import Path
import unittest

from utils.optimize_wick_reversal_v4_segments import (
    _expand_names,
    _inclusive_end_date,
    _resolve_window,
    load_experiment_config,
    resolve_experiments,
)
from utils.optimize_wick_reversal_v4 import _backbone_symbol


class TestOptimizeWickReversalV4Segments(unittest.TestCase):
    def setUp(self):
        self.config_path = Path("config/wick_reversal_v4_segment_experiments.json")
        self.datasets, self.plans, self.groups = load_experiment_config(self.config_path)

    def test_expand_names_supports_groups(self):
        names = _expand_names(["default"], self.plans, self.groups)
        self.assertEqual(names, ["h1_to_h2", "h2_to_h1", "m8_to_m4", "m4_to_m8"])

    def test_resolve_window_for_first_half(self):
        dataset = self.datasets["y2023"]
        window = _resolve_window(dataset, "train", self.plans["h1_to_h2"].train)
        self.assertEqual(window.start, "2023-04-14")
        self.assertEqual(window.end, "2023-10-14")
        self.assertEqual(_inclusive_end_date(window.end), "2023-10-13")

    def test_resolve_experiments_for_dataset_and_group(self):
        experiments = resolve_experiments(
            self.datasets,
            self.plans,
            self.groups,
            ["y2024"],
            ["quarter_walk"],
        )
        self.assertEqual(len(experiments), 3)
        self.assertEqual(experiments[0].dataset.symbol, "BTCUSDT_20240414_20250413")
        self.assertEqual(experiments[0].plan.name, "q1_to_q2")

    def test_backbone_symbol_strips_dataset_suffix(self):
        self.assertEqual(_backbone_symbol("BTCUSDT_20230414_20240413"), "BTCUSDT")
        self.assertEqual(_backbone_symbol("BTCUSDT"), "BTCUSDT")


if __name__ == "__main__":
    unittest.main()
