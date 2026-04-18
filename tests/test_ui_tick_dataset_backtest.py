import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _install_module_stub(module_name: str, **attrs):
    module = types.ModuleType(module_name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[module_name] = module


class _StubObject:
    def __init__(self, *args, **kwargs):
        pass


_install_module_stub("core.ws_client", WsWorkerThread=_StubObject)
_install_module_stub("core.history_processor", HistoryProcessorThread=_StubObject)
_install_module_stub("ui.order_book_widget", OrderBookWidget=_StubObject)
_install_module_stub("ui.kline_chart", KlineChart=_StubObject)
_install_module_stub("ui.cvd_chart", CvdChart=_StubObject, StatsPanel=_StubObject)
_install_module_stub("ui.heatmap_widget", HeatmapWidget=_StubObject)
_install_module_stub("ui.footprint_widget", FootprintChart=_StubObject)
_install_module_stub("ui.capacity_tab", CapacityTab=_StubObject)

from core.data_types import Kline
from ui import main_window


class _FakeSettings:
    def __init__(self, initial=None):
        self.store = dict(initial or {})

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value, autosave=True):
        self.store[key] = value


class _ValueStub:
    def __init__(self, value):
        self._value = value

    def value(self):
        return self._value


class _TextStub:
    def __init__(self, value):
        self._value = value

    def currentText(self):
        return self._value


class _IndexStub:
    def __init__(self, value):
        self._value = value

    def currentIndex(self):
        return self._value


class _ChartStub:
    def __init__(self):
        self.markers = None

    def set_strategy_markers(self, markers):
        self.markers = markers


class _CapacityStub:
    def __init__(self):
        self.ready = None

    def set_ready(self, ready):
        self.ready = ready


class _StrategyEngineStub:
    def __init__(self):
        self.name = "Stub Strategy"
        self.received_klines = None
        self.received_tick_map = None
        self.allow_bar_fallback_in_tick_mode = True

    def on_history(self, klines, tick_map=None):
        self.received_klines = list(klines)
        self.received_tick_map = tick_map
        return []

    def compute_stats(self, signals):
        return {"signals": len(signals)}


class _DialogStub:
    instances = []

    def __init__(self, stats, klines=None, tick_map=None, signals=None, parent=None):
        self.stats = stats
        self.klines = klines
        self.tick_map = tick_map
        self.signals = signals
        self.parent = parent
        self.executed = False
        self.__class__.instances.append(self)

    def exec(self):
        self.executed = True


def _make_kline(symbol: str, open_time: int, open_: float) -> Kline:
    return Kline(
        symbol=symbol,
        interval="1m",
        open_time=open_time,
        close_time=open_time + 60_000 - 1,
        open=open_,
        high=open_ + 2.0,
        low=open_ - 2.0,
        close=open_ + 1.0,
        volume=10.0,
        taker_buy_volume=6.0,
        is_closed=True,
    )


class _ComboStub:
    def __init__(self):
        self._items = []
        self._index = -1
        self._blocked = False

    def addItem(self, text, data=None):
        self._items.append((text, text if data is None else data))
        if self._index < 0:
            self._index = 0

    def addItems(self, texts):
        for text in texts:
            self.addItem(text)

    def clear(self):
        self._items.clear()
        self._index = -1

    def count(self):
        return len(self._items)

    def itemText(self, index):
        return self._items[index][0]

    def currentData(self):
        if 0 <= self._index < len(self._items):
            return self._items[self._index][1]
        return None

    def currentText(self):
        if 0 <= self._index < len(self._items):
            return self._items[self._index][0]
        return ""

    def currentIndex(self):
        return self._index

    def setCurrentIndex(self, index):
        self._index = index

    def blockSignals(self, blocked):
        self._blocked = blocked


class _LabelStub:
    def __init__(self):
        self.value = ""

    def setText(self, value):
        self.value = value

    def text(self):
        return self.value


class TestUiTickDatasetBacktest(unittest.TestCase):
    def test_rebuild_tick_dataset_combo_restores_previous_selection(self):
        window = SimpleNamespace()
        window._symbol = "BTCUSDT"
        window._bt_dataset_combo = _ComboStub()
        window._list_tick_backtest_symbols = lambda: [
            "BTCUSDT",
            "BTCUSDT_20230414_20240413",
            "BTCUSDT_20240414_20250413",
        ]
        window._tick_dataset_label = lambda symbol: f"label:{symbol}"

        fake_settings = _FakeSettings(
            {"backtest_tick_symbol": "BTCUSDT_20240414_20250413"}
        )
        with patch.object(main_window, "ui_settings", fake_settings):
            main_window.MainWindow._rebuild_tick_dataset_combo(window)

        self.assertEqual(window._bt_dataset_combo.count(), 3)
        self.assertEqual(
            window._bt_dataset_combo.currentData(),
            "BTCUSDT_20240414_20250413",
        )
        self.assertEqual(
            window._bt_dataset_combo.itemText(1),
            "label:BTCUSDT_20230414_20240413",
        )

    def test_rebuild_range_combo_uses_selected_tick_dataset(self):
        window = SimpleNamespace()
        window._symbol = "BTCUSDT"
        window._bt_range_combo = _ComboStub()
        window._bt_mode_combo = _ComboStub()
        window._bt_mode_combo.addItems(["Bar", "Tick"])
        window._bt_mode_combo.setCurrentIndex(1)
        window._current_tick_backtest_symbol = lambda: "BTCUSDT_20230414_20240413"

        def info_side_effect(symbol):
            if symbol == "BTCUSDT_20230414_20240413":
                return {
                    "start_ms": 1_681_430_400_000,
                    "end_ms": 1_712_966_399_999,
                }
            if symbol == "BTCUSDT":
                return {
                    "start_ms": 1_744_588_800_000,
                    "end_ms": 1_776_124_799_999,
                }
            return None

        with patch.object(main_window.tick_cache, "info", side_effect=info_side_effect):
            main_window.MainWindow._rebuild_range_combo(window)

        self.assertEqual(window._bt_range_combo.count(), 2)
        self.assertIn("04/14", window._bt_range_combo.itemText(0))
        self.assertNotIn("05/14", window._bt_range_combo.itemText(0))

    def test_on_run_strategy_uses_selected_dataset_coverage(self):
        calls = []
        window = SimpleNamespace()
        window._strategy_engine = object()
        window._loaded_klines = [object()]
        window._status_lbl = _LabelStub()
        window._bt_mode_combo = _ComboStub()
        window._bt_mode_combo.addItems(["Bar", "Tick"])
        window._bt_mode_combo.setCurrentIndex(1)
        window._bt_range_combo = _ComboStub()
        window._bt_range_combo.addItem("全部")
        window._current_tick_backtest_symbol = lambda: "BTCUSDT_20230414_20240413"
        window._execute_backtest = lambda **kwargs: calls.append(kwargs)

        def info_side_effect(symbol):
            if symbol == "BTCUSDT_20230414_20240413":
                return {"start_ms": 111, "end_ms": 222}
            if symbol == "BTCUSDT":
                return {"start_ms": 333, "end_ms": 444}
            return None

        with patch.object(main_window.tick_cache, "info", side_effect=info_side_effect):
            main_window.MainWindow._on_run_strategy(window)

        self.assertEqual(calls, [{"tick_start_ms": 111, "tick_end_ms": 222}])

    def test_execute_backtest_tick_mode_uses_exchange_klines_backbone(self):
        ticks = np.array(
            [
                [1_681_430_400_000, 100.0, 1.0, 0.0],
                [1_681_430_460_000, 101.0, 2.0, 1.0],
            ],
            dtype=np.float64,
        )
        built_klines = [
            _make_kline("BTCUSDT_20230414_20240413", 1_681_430_400_000, 100.0),
            _make_kline("BTCUSDT_20230414_20240413", 1_681_430_460_000, 101.0),
        ]
        engine = _StrategyEngineStub()
        dialog_cls = _DialogStub
        dialog_cls.instances = []

        window = SimpleNamespace()
        window._symbol = "BTCUSDT"
        window._loaded_klines = []
        window._interval = "1m"
        window._strategy_engine = engine
        window._strategy_signals = []
        window._status_lbl = _LabelStub()
        window._bt_mode_combo = _ComboStub()
        window._bt_mode_combo.addItems(["Bar", "Tick"])
        window._bt_mode_combo.setCurrentIndex(1)
        window._current_tick_backtest_symbol = lambda: "BTCUSDT_20230414_20240413"
        window._kline_chart = _ChartStub()
        window._capacity_tab = _CapacityStub()
        window._show_strategy_stats_label = lambda stats: None
        window._bt_config_dlg = SimpleNamespace(
            capital_spin=_ValueStub(10_000.0),
            loss_spin=_ValueStub(2.0),
            leverage_spin=_ValueStub(10),
            fee_combo=_TextStub("Taker"),
            custom_fee_spin=_ValueStub(0.05),
            slippage_spin=_ValueStub(0.0),
            funding_spin=_ValueStub(0.0),
            maint_spin=_ValueStub(0.005),
            compound_combo=_IndexStub(0),
        )

        with patch.object(main_window.tick_cache, "load_range", return_value=ticks) as load_range_mock, \
             patch.object(main_window.tick_cache, "build_bar_map", return_value={
                 built_klines[0].open_time: ticks[:1],
                 built_klines[1].open_time: ticks[1:],
             }) as build_bar_map_mock, \
             patch.object(main_window.kline_cache, "load_range_as_klines", return_value=built_klines) as load_klines_mock, \
             patch.object(main_window, "BacktestResultDialog", dialog_cls), \
             patch("backtest.engine.simulate_trades", return_value={
                 "trade_list": [],
                 "final_equity": 10_000.0,
                 "total_return_pct": 0.0,
             }):
            main_window.MainWindow._execute_backtest(
                window,
                tick_start_ms=1_681_430_400_000,
                tick_end_ms=1_681_430_520_000,
            )

        load_range_mock.assert_called_once_with(
            "BTCUSDT_20230414_20240413",
            1_681_430_400_000,
            1_681_430_520_000,
        )
        load_klines_mock.assert_called_once_with(
            "BTCUSDT",
            "1m",
            1_681_430_400_000,
            1_681_430_520_000,
        )
        self.assertEqual(engine.received_klines, built_klines)
        self.assertEqual(len(engine.received_tick_map), 2)
        self.assertTrue(engine.allow_bar_fallback_in_tick_mode)
        self.assertEqual(len(dialog_cls.instances), 1)
        self.assertTrue(dialog_cls.instances[0].executed)
        self.assertAlmostEqual(
            dialog_cls.instances[0].stats["tick_coverage_pct"],
            100.0,
        )
        self.assertEqual(dialog_cls.instances[0].stats["backtest_start_ms"], built_klines[0].open_time)
        self.assertEqual(dialog_cls.instances[0].stats["backtest_end_ms"], built_klines[-1].open_time)
        build_bar_map_mock.assert_called_once()
        self.assertTrue(window._capacity_tab.ready is False)


if __name__ == "__main__":
    unittest.main()
