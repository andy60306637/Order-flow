from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path

import numpy as np

from core import data_paths, market_data_cache


class TestMarketDataCache(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.get(data_paths.ENV_DATA_ROOT)
        self._old_override = data_paths._DATA_ROOT_OVERRIDE
        self._old_ui_settings_path = data_paths._UI_SETTINGS_PATH
        self._tmp_root = Path(__file__).resolve().parent / "_tmp_market_data_cache"
        shutil.rmtree(self._tmp_root, ignore_errors=True)
        self._tmp_root.mkdir(parents=True, exist_ok=True)
        data_paths._UI_SETTINGS_PATH = self._tmp_root / "missing_ui_settings.json"
        data_paths.set_data_root_override(self._tmp_root)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop(data_paths.ENV_DATA_ROOT, None)
        else:
            os.environ[data_paths.ENV_DATA_ROOT] = self._old_env
        data_paths._DATA_ROOT_OVERRIDE = self._old_override
        data_paths._UI_SETTINGS_PATH = self._old_ui_settings_path
        shutil.rmtree(self._tmp_root, ignore_errors=True)

    def test_ensure_data_root_layout_writes_manifest(self):
        data_paths.ensure_data_root_layout()

        ok, message = data_paths.validate_data_root()
        self.assertTrue(ok, message)
        self.assertTrue((self._tmp_root / "DATA_LAYOUT.md").exists())
        self.assertTrue((self._tmp_root / "manifests" / "data_root.json").exists())

    def test_save_and_load_extended_dataset_cache(self):
        manifest = market_data_cache.save_cache(
            "fundingRate",
            "btcusdt",
            [
                {"fundingTime": "1735689600000", "fundingRate": "0.0001"},
                {"fundingTime": "1735718400000", "fundingRate": "-0.0002"},
            ],
            columns=["fundingTime", "fundingRate"],
            source_files=["BTCUSDT-fundingRate-2025-01-01.csv"],
        )

        arr, loaded_manifest = market_data_cache.load_cache("fundingRate", "BTCUSDT")
        self.assertEqual(manifest["row_count"], 2)
        self.assertIsNotNone(loaded_manifest)
        self.assertTrue(np.array_equal(arr[:, 1], np.array([0.0001, -0.0002])))
        self.assertEqual(loaded_manifest["columns"], ["fundingTime", "fundingRate"])
        self.assertEqual(loaded_manifest["start_ms"], 1735689600000)
        self.assertEqual(loaded_manifest["end_ms"], 1735718400000)

    def test_align_cache_column_ffill_and_exact(self):
        market_data_cache.save_cache(
            "metrics",
            "BTCUSDT",
            np.array([
                [60_000, 100.0],
                [180_000, 130.0],
            ]),
            columns=["timestamp_ms", "sum_open_interest"],
            time_column="timestamp_ms",
        )
        open_times = np.array([0, 60_000, 120_000, 180_000, 240_000], dtype=np.int64)

        ffilled = market_data_cache.align_cache_column(
            "metrics", "BTCUSDT", open_times, "sum_open_interest"
        )
        exact = market_data_cache.align_cache_column(
            "metrics", "BTCUSDT", open_times, "sum_open_interest", mode="exact", default=0.0
        )

        np.testing.assert_allclose(ffilled, np.array([np.nan, 100.0, 100.0, 130.0, 130.0]), equal_nan=True)
        np.testing.assert_allclose(exact, np.array([0.0, 100.0, 0.0, 130.0, 0.0]))

    def test_premium_index_klines_requires_interval(self):
        with self.assertRaises(ValueError):
            market_data_cache.cache_path("premiumIndexKlines", "BTCUSDT")

        path = market_data_cache.cache_path("premiumIndexKlines", "BTCUSDT", interval="1m")
        self.assertIn("premiumIndexKlines", str(path))
        self.assertIn("1m", str(path))


if __name__ == "__main__":
    unittest.main()
