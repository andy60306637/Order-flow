from __future__ import annotations

import json
import os
import shutil
import unittest
from pathlib import Path

from core import data_paths, kline_cache, tick_cache


class TestDataPaths(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.get(data_paths.ENV_DATA_ROOT)
        self._old_override = data_paths._DATA_ROOT_OVERRIDE
        self._old_ui_settings_path = data_paths._UI_SETTINGS_PATH
        self._old_tick_cache_dir = tick_cache._CACHE_DIR
        self._old_tick_shard_root = tick_cache._SHARD_ROOT
        self._old_kline_cache_dir = kline_cache._CACHE_DIR
        self._tmp_root = Path(__file__).resolve().parent / "_tmp_data_paths"
        shutil.rmtree(self._tmp_root, ignore_errors=True)
        self._tmp_root.mkdir(parents=True, exist_ok=True)
        data_paths._UI_SETTINGS_PATH = self._tmp_root / "missing_ui_settings.json"
        data_paths.clear_data_root_override()
        tick_cache._CACHE_DIR = None
        tick_cache._SHARD_ROOT = None
        kline_cache._CACHE_DIR = None

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop(data_paths.ENV_DATA_ROOT, None)
        else:
            os.environ[data_paths.ENV_DATA_ROOT] = self._old_env
        data_paths._DATA_ROOT_OVERRIDE = self._old_override
        data_paths._UI_SETTINGS_PATH = self._old_ui_settings_path
        tick_cache._CACHE_DIR = self._old_tick_cache_dir
        tick_cache._SHARD_ROOT = self._old_tick_shard_root
        kline_cache._CACHE_DIR = self._old_kline_cache_dir
        shutil.rmtree(self._tmp_root, ignore_errors=True)

    def test_default_data_root_is_project_data(self):
        os.environ.pop(data_paths.ENV_DATA_ROOT, None)

        self.assertEqual(data_paths.data_root(), data_paths.PROJECT_ROOT / "data")

    def test_env_data_root_controls_cache_dirs(self):
        root = (self._tmp_root / "env_root").resolve()
        os.environ[data_paths.ENV_DATA_ROOT] = str(root)

        self.assertEqual(data_paths.data_root(), root)
        self.assertEqual(tick_cache.cache_path("btcusdt"), root / "ticks" / "BTCUSDT_ticks.npz")
        self.assertEqual(kline_cache.cache_path("btcusdt", "1m"), root / "klines" / "BTCUSDT_1m.npy")
        self.assertTrue((root / "ticks").is_dir())
        self.assertTrue((root / "klines").is_dir())

    def test_cli_override_beats_env_data_root(self):
        env_root = (self._tmp_root / "env_root").resolve()
        cli_root = (self._tmp_root / "cli_root").resolve()
        os.environ[data_paths.ENV_DATA_ROOT] = str(env_root)
        data_paths.set_data_root_override(cli_root)

        self.assertEqual(data_paths.data_root(), cli_root)
        self.assertEqual(data_paths.tick_cache_dir(), cli_root / "ticks")

    def test_ui_setting_is_used_after_env(self):
        os.environ.pop(data_paths.ENV_DATA_ROOT, None)
        settings_root = (self._tmp_root / "settings_root").resolve()
        data_paths._UI_SETTINGS_PATH.write_text(
            json.dumps({"data_root": str(settings_root)}),
            encoding="utf-8",
        )

        self.assertEqual(data_paths.data_root(), settings_root)


if __name__ == "__main__":
    unittest.main()
