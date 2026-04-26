import sys
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

from core.data_types import Kline


_APP = QApplication.instance() or QApplication(sys.argv)
_MS = 60_000


def _kline(i: int) -> Kline:
    return Kline(
        symbol="BTCUSDT",
        interval="1m",
        open_time=i * _MS,
        close_time=(i + 1) * _MS - 1,
        open=100.0 + i,
        high=110.0 + i,
        low=90.0 + i,
        close=105.0 + i,
        volume=100.0,
        taker_buy_volume=50.0,
        is_closed=True,
    )


def _trade() -> dict:
    return {
        "dir": "long",
        "entry_time": 1_000,
        "exit_time": 61_000,
        "entry": 101.0,
        "exit": 106.0,
        "stop": 99.0,
        "net_pnl": 10.0,
        "exit_label": "TP",
        "total_fee": 0.0,
        "funding_cost": 0.0,
        "equity_after": 10_010.0,
        "qty": 1.0,
    }


class _FakeSnapshotDialog:
    instances: list = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        _FakeSnapshotDialog.instances.append(self)

    def show(self):
        pass

    def raise_(self):
        pass

    def activateWindow(self):
        pass

    def exec(self):
        return 0


class BacktestDashboardSnapshotTests(unittest.TestCase):
    def setUp(self):
        _FakeSnapshotDialog.instances = []
        self.klines = [_kline(0), _kline(1)]
        self.trade = _trade()
        self.stats = {
            "trade_list": [self.trade],
            "trades": 1,
            "win_rate": 100.0,
            "profit_factor": 1.0,
            "total_net_pnl": 10.0,
            "final_equity": 10_010.0,
            "_snapshot_klines": self.klines,
            "_snapshot_tick_map": None,
            "_snapshot_signals": [],
        }

    def test_new_dashboard_snapshot_button_uses_fallback_context(self):
        import ui.backtest_dashboard as dashboard

        with patch.object(dashboard, "TradeSnapshotDialog", _FakeSnapshotDialog):
            widget = dashboard.BacktestDashboard()
            widget._montecarlo.run_simulation = lambda *args, **kwargs: None

            widget._on_result_ready(self.stats)
            self.assertEqual(len(widget._snapshot_contexts), 1)
            self.assertTrue(widget._snapshot_btn.isEnabled())

            widget._on_trade_selected(self.trade)
            widget._open_selected_snapshot()

        self.assertEqual(len(_FakeSnapshotDialog.instances), 1)
        ctx = _FakeSnapshotDialog.instances[0].args[0][0]
        self.assertEqual(ctx["entry_ki"], 0)
        self.assertEqual(ctx["exit_ki"], 1)

    def test_result_dialog_snapshot_does_not_require_signals(self):
        import ui.trade_snapshot_dialog as snapshot_module
        from ui.main_window import BacktestResultDialog

        stats = dict(self.stats)
        stats.pop("_snapshot_klines", None)
        stats.pop("_snapshot_tick_map", None)
        stats.pop("_snapshot_signals", None)

        with patch.object(snapshot_module, "TradeSnapshotDialog", _FakeSnapshotDialog):
            dialog = BacktestResultDialog(
                stats,
                klines=self.klines,
                tick_map=None,
                signals=[],
            )
            self.assertTrue(dialog._snap_btn.isEnabled())
            dialog._open_snapshot_at(0)

        self.assertEqual(len(_FakeSnapshotDialog.instances), 1)
        ctx = _FakeSnapshotDialog.instances[0].args[0][0]
        self.assertEqual(ctx["entry_ki"], 0)
        self.assertEqual(ctx["exit_ki"], 1)


if __name__ == "__main__":
    unittest.main()
