import json
import shutil
import unittest
from pathlib import Path

import numpy as np

from core import tick_cache


class TestTickCacheShards(unittest.TestCase):
    def setUp(self):
        self._cache_dir = Path(__file__).resolve().parent / "_tmp_tick_cache_shards"
        shutil.rmtree(self._cache_dir, ignore_errors=True)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._old_cache_dir = tick_cache._CACHE_DIR
        self._old_shard_root = tick_cache._SHARD_ROOT
        tick_cache._CACHE_DIR = self._cache_dir
        tick_cache._SHARD_ROOT = self._cache_dir / "shards"

    def tearDown(self):
        tick_cache._CACHE_DIR = self._old_cache_dir
        tick_cache._SHARD_ROOT = self._old_shard_root
        shutil.rmtree(self._cache_dir, ignore_errors=True)

    def _sample_ticks(self) -> np.ndarray:
        return np.array([
            [1735689600000, 100.0, 1.0, 0.0],  # 2025-01-01
            [1735776000000, 101.0, 1.0, 1.0],  # 2025-01-02
            [1738368000000, 102.0, 1.0, 0.0],  # 2025-02-01
            [1738454400000, 103.0, 1.0, 1.0],  # 2025-02-02
        ], dtype=np.float64)

    def test_save_shards_and_load_range_sharded(self):
        ticks = self._sample_ticks()
        manifest = tick_cache.save_shards("BTCUSDT", ticks)

        self.assertEqual(set(manifest["months"]), {"202501", "202502"})
        jan = tick_cache.load_range("BTCUSDT", 1735689600000, 1735862399999)
        feb = tick_cache.load_range("BTCUSDT", 1738368000000, 1738540799999)

        self.assertEqual(len(jan), 2)
        self.assertEqual(len(feb), 2)
        self.assertTrue(np.array_equal(jan[:, 1], np.array([100.0, 101.0])))
        self.assertTrue(np.array_equal(feb[:, 1], np.array([102.0, 103.0])))

    def test_load_range_falls_back_to_legacy_npz_when_shards_incomplete(self):
        ticks = self._sample_ticks()
        tick_cache.save_raw("BTCUSDT", ticks, int(ticks[0, 0]), int(ticks[-1, 0]))

        shard_manifest = {
            "symbol": "BTCUSDT",
            "format": "tick_shards_v1",
            "start_ms": int(ticks[0, 0]),
            "end_ms": int(ticks[-1, 0]),
            "months": {
                "202501": {
                    "path": "shards/BTCUSDT/BTCUSDT_202501.npy",
                    "count": 2,
                    "start_ms": int(ticks[0, 0]),
                    "end_ms": int(ticks[1, 0]),
                },
            },
        }
        shard_file = tick_cache.shard_path("BTCUSDT", "202501")
        np.save(str(shard_file), ticks[:2], allow_pickle=False)
        with open(tick_cache.shard_manifest_path("BTCUSDT"), "w", encoding="utf-8") as fh:
            json.dump(shard_manifest, fh)

        loaded = tick_cache.load_range("BTCUSDT", int(ticks[0, 0]), int(ticks[-1, 0]))
        self.assertEqual(len(loaded), 4)
        self.assertTrue(np.array_equal(loaded[:, 1], ticks[:, 1]))

    def test_load_meta_prefers_shard_manifest(self):
        ticks = self._sample_ticks()
        tick_cache.save_raw("BTCUSDT", ticks, int(ticks[0, 0]), int(ticks[-1, 0]))
        tick_cache.save_shards("BTCUSDT", ticks)

        meta = tick_cache.load_meta("BTCUSDT")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["source"], "shards")
        self.assertEqual(meta["start_ms"], int(ticks[0, 0]))


if __name__ == "__main__":
    unittest.main()
