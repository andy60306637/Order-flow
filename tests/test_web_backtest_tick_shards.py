import shutil
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from core import tick_cache
from server.routes.backtest import BacktestRequest, _request_slices, _scan_tick_coverage


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _ticks(*dates: str) -> np.ndarray:
    rows = []
    for idx, date_text in enumerate(dates):
        rows.append([_ms(date_text), 100.0 + idx, 1.0, float(idx % 2)])
    return np.array(rows, dtype=np.float64)


class TestWebBacktestTickShards(unittest.TestCase):
    def setUp(self):
        self._cache_dir = Path(__file__).resolve().parent / "_tmp_web_backtest_tick_shards"
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

    def test_multi_select_month_uses_all_matching_yearly_shard_sources(self):
        tick_cache.save_shards(
            "BTCUSDT_20220414_20230413",
            _ticks("2023-04-01", "2023-04-13T23:59:59"),
        )
        tick_cache.save_shards(
            "BTCUSDT_20230414_20240413",
            _ticks("2023-04-14", "2023-04-30T23:59:59"),
        )

        req = BacktestRequest(
            symbol="BTCUSDT",
            start_ms=_ms("2023-04-01"),
            end_ms=_ms("2023-05-01") - 1,
            use_tick_mode=True,
            slice_mode="multi_select",
            selected_months=["202304"],
        )

        slices = _request_slices(req)
        self.assertEqual(len(slices), 1)
        sl = slices[0]
        self.assertEqual(sl.segment_symbols, [
            "BTCUSDT_20220414_20230413",
            "BTCUSDT_20230414_20240413",
        ])
        self.assertEqual(sl.segments[0][0], _ms("2023-04-01"))
        self.assertEqual(sl.segments[1][0], _ms("2023-04-14"))

    def test_tick_range_splits_across_available_shard_sources(self):
        tick_cache.save_shards(
            "BTCUSDT_20230414_20240413",
            _ticks("2024-04-01", "2024-04-13T23:59:59"),
        )
        tick_cache.save_shards(
            "BTCUSDT_20240414_20250413",
            _ticks("2024-04-14", "2024-04-30T23:59:59"),
        )

        req = BacktestRequest(
            symbol="BTCUSDT",
            start_ms=_ms("2024-04-01"),
            end_ms=_ms("2024-05-01") - 1,
            use_tick_mode=True,
            slice_mode="range",
        )

        sl = _request_slices(req)[0]
        self.assertEqual(sl.segment_symbols, [
            "BTCUSDT_20230414_20240413",
            "BTCUSDT_20240414_20250413",
        ])

    def test_available_data_reports_aggregate_tick_months(self):
        tick_cache.save_shards("BTCUSDT_20220414_20230413", _ticks("2023-03-01"))
        tick_cache.save_shards("BTCUSDT_20230414_20240413", _ticks("2023-04-14"))

        rows = _scan_tick_coverage()

        btc = next(row for row in rows if row["symbol"] == "BTCUSDT")
        self.assertEqual(btc["months"], ["202303", "202304"])
        self.assertEqual(btc["shard_sets"], 2)

    def test_tick_mode_fails_when_selected_month_has_no_shard(self):
        req = BacktestRequest(
            symbol="BTCUSDT",
            start_ms=_ms("2020-01-01"),
            end_ms=_ms("2020-02-01") - 1,
            use_tick_mode=True,
            slice_mode="multi_select",
            selected_months=["202001"],
        )

        with self.assertRaisesRegex(ValueError, "No tick shards"):
            _request_slices(req)


if __name__ == "__main__":
    unittest.main()
