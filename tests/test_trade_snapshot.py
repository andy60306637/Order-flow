"""
trade_snapshot_dialog 的單元測試（純邏輯層，不啟動 Qt）。

涵蓋：
  1. _find_ki  — 二分搜尋 kline 索引
  2. _collect_contexts — 基本 context 建構
  3. _collect_contexts — k0 匹配（取 entry 之前最近一個 k0）
  4. _collect_contexts — 略過 skipped 交易
  5. _collect_contexts — entry 找不到時不產生 context
  6. _collect_contexts — 無 exit 時 window 仍正常計算
  7. _collect_contexts — context_bars 邊界箝制（不超出 klines 範圍）
  8. _collect_contexts — win_start / win_end 正確反映 k0 比 entry 更早的情況
  9. _collect_contexts — 多筆交易各自對應正確的 entry/exit bar 索引
 10. _collect_contexts — k0 在 entry 之後的 k0 不被選取
"""
import unittest
from typing import List, Optional

import numpy as np

from core.data_types import Kline
from strategies.base import StrategySignal

# 只匯入純邏輯函式，不觸發 Qt / pyqtgraph
from ui.trade_snapshot_dialog import _find_ki, _collect_contexts

_MS = 60_000   # 1 分鐘


# ─── 輔助 ─────────────────────────────────────────────────────────────────────

def _k(i: int, o=100.0, h=110.0, l=90.0, c=105.0,
       vol=100.0, tbv=50.0) -> Kline:
    ot = i * _MS
    return Kline(
        symbol="BTCUSDT", interval="1m",
        open_time=ot, close_time=ot + _MS - 1,
        open=o, high=h, low=l, close=c,
        volume=vol, taker_buy_volume=tbv,
        is_closed=True,
    )


def _sig(sig_type: str, bar_idx: int, price: float = 100.0,
         stop: float = None, fill: float = None,
         label: str = "") -> StrategySignal:
    return StrategySignal(
        open_time=bar_idx * _MS,
        price=price,
        signal_type=sig_type,
        label=label,
        stop_price=stop,
        fill_price=fill,
    )


def _trade(entry_idx: int, exit_idx: int,
           entry_p: float = 100.0, exit_p: float = 105.0,
           stop: float = 90.0, net_pnl: float = 5.0,
           exit_label: str = "TP", skipped: bool = False) -> dict:
    return {
        "entry_time":  entry_idx * _MS,
        "exit_time":   exit_idx  * _MS,
        "entry":       entry_p,
        "exit":        exit_p,
        "stop":        stop,
        "net_pnl":     net_pnl,
        "exit_label":  exit_label,
        "dir":         "long",
        "skipped":     skipped,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 1–2: _find_ki
# ═══════════════════════════════════════════════════════════════════════════════

class TestFindKi(unittest.TestCase):

    def setUp(self):
        self.klines = [_k(i) for i in range(10)]

    def test_finds_first(self):
        self.assertEqual(_find_ki(self.klines, 0), 0)

    def test_finds_last(self):
        self.assertEqual(_find_ki(self.klines, 9 * _MS), 9)

    def test_finds_middle(self):
        self.assertEqual(_find_ki(self.klines, 5 * _MS), 5)

    def test_missing_returns_none(self):
        self.assertIsNone(_find_ki(self.klines, 99 * _MS))

    def test_empty_klines_returns_none(self):
        self.assertIsNone(_find_ki([], 0))


# ═══════════════════════════════════════════════════════════════════════════════
# 3–10: _collect_contexts
# ═══════════════════════════════════════════════════════════════════════════════

class TestCollectContexts(unittest.TestCase):

    def _klines(self, n: int = 20) -> List[Kline]:
        return [_k(i) for i in range(n)]

    # ── test 3: 基本 context 建構 ──────────────────────────────────────────────
    def test_basic_context_fields(self):
        """正常一筆交易，context 應包含所有必要欄位且值正確。"""
        klines = self._klines(20)
        signals = [
            _sig("k0_long",    2),
            _sig("long_entry", 4, stop=85.0),
            _sig("long_exit",  7, label="TP"),
        ]
        trades = [_trade(entry_idx=4, exit_idx=7)]

        ctxs = _collect_contexts(signals, trades, klines, context_bars=2)

        self.assertEqual(len(ctxs), 1)
        ctx = ctxs[0]
        self.assertEqual(ctx["k0_ki"],    2)
        self.assertEqual(ctx["entry_ki"], 4)
        self.assertEqual(ctx["exit_ki"],  7)
        self.assertEqual(ctx["trade_idx"], 0)
        self.assertIsNotNone(ctx["k0_signal"])
        self.assertIsNotNone(ctx["entry_signal"])
        self.assertIsNotNone(ctx["exit_signal"])

    # ── test 4: k0 選取最近一個在 entry 之前的 ────────────────────────────────
    def test_k0_picks_latest_before_entry(self):
        """若有多個 k0，應選取最靠近 entry（且在 entry 之前）的那個。"""
        klines = self._klines(20)
        signals = [
            _sig("k0_long",    1),
            _sig("k0_long",    3),   # 這個更近
            _sig("long_entry", 5, stop=85.0),
            _sig("long_exit",  8, label="SL"),
        ]
        trades = [_trade(entry_idx=5, exit_idx=8)]

        ctxs = _collect_contexts(signals, trades, klines, context_bars=0)
        self.assertEqual(ctxs[0]["k0_ki"], 3)

    # ── test 5: k0 在 entry 之後不被選取 ────────────────────────────────────
    def test_k0_after_entry_not_used(self):
        """k0 在 entry 之後不應被匹配（時序錯誤）。"""
        klines = self._klines(20)
        signals = [
            _sig("long_entry", 3, stop=85.0),
            _sig("k0_long",    5),   # 在 entry 之後
            _sig("long_exit",  7, label="TP"),
        ]
        trades = [_trade(entry_idx=3, exit_idx=7)]

        ctxs = _collect_contexts(signals, trades, klines, context_bars=0)
        self.assertIsNone(ctxs[0]["k0_signal"])
        self.assertIsNone(ctxs[0]["k0_ki"])

    # ── test 6: skipped 交易被略過 ───────────────────────────────────────────
    def test_skipped_trade_excluded(self):
        klines = self._klines(20)
        signals = [
            _sig("long_entry", 4, stop=85.0),
            _sig("long_exit",  7, label="TP"),
        ]
        trades = [_trade(entry_idx=4, exit_idx=7, skipped=True)]

        ctxs = _collect_contexts(signals, trades, klines)
        self.assertEqual(len(ctxs), 0)

    # ── test 7: 無對應 entry signal 不產生 context ───────────────────────────
    def test_no_entry_signal_excluded(self):
        """trade_list 中有記錄但 signals 裡沒有對應的 long_entry，不應產生 context。"""
        klines = self._klines(20)
        signals = [
            _sig("long_exit", 7, label="TP"),
        ]
        trades = [_trade(entry_idx=4, exit_idx=7)]

        ctxs = _collect_contexts(signals, trades, klines)
        self.assertEqual(len(ctxs), 0)

    # ── test 8: 無 exit 時 window 計算不崩潰 ────────────────────────────────
    def test_no_exit_signal_still_builds_context(self):
        """exit_time=0 或找不到對應 exit kline 時，仍可建立 context。"""
        klines = self._klines(20)
        signals = [
            _sig("k0_long",    2),
            _sig("long_entry", 5, stop=85.0),
        ]
        trade = _trade(entry_idx=5, exit_idx=0)
        trade["exit_time"] = 0   # 無出場時間

        ctxs = _collect_contexts(signals, [trade], klines, context_bars=2)
        self.assertEqual(len(ctxs), 1)
        ctx = ctxs[0]
        self.assertIsNone(ctx["exit_ki"])
        self.assertIsNone(ctx["exit_signal"])
        # win_end 應基於 entry_ki（exit_ki 為 None 時）
        self.assertGreaterEqual(ctx["win_end"], ctx["entry_ki"])

    # ── test 9: context_bars 邊界不超出 klines ───────────────────────────────
    def test_context_bars_clamped_to_klines_bounds(self):
        """context_bars 很大時，win_start 不應小於 0，win_end 不應超過 len-1。"""
        klines = self._klines(10)
        signals = [
            _sig("k0_long",    1),
            _sig("long_entry", 2, stop=85.0),
            _sig("long_exit",  3, label="TP"),
        ]
        trades = [_trade(entry_idx=2, exit_idx=3)]

        ctxs = _collect_contexts(signals, trades, klines, context_bars=100)
        ctx = ctxs[0]
        self.assertGreaterEqual(ctx["win_start"], 0)
        self.assertLessEqual(ctx["win_end"], len(klines) - 1)

    # ── test 10: win_start 反映 k0 比 entry 更早 ────────────────────────────
    def test_win_start_uses_k0_when_earlier_than_entry(self):
        """k0 在 entry 前幾根時，win_start 應從 k0 往前推 context_bars。"""
        klines = self._klines(30)
        signals = [
            _sig("k0_long",    5),
            _sig("long_entry", 10, stop=85.0),
            _sig("long_exit",  12, label="TP"),
        ]
        trades = [_trade(entry_idx=10, exit_idx=12)]

        ctxs = _collect_contexts(signals, trades, klines, context_bars=2)
        ctx = ctxs[0]
        # win_start = max(0, min(entry_ki, k0_ki) - context_bars) = max(0, 5 - 2) = 3
        self.assertEqual(ctx["win_start"], 3)

    # ── test 11: 多筆交易各自對應正確的 bar 索引 ──────────────────────────────
    def test_multiple_trades_correct_indices(self):
        """兩筆交易，各自的 entry_ki / exit_ki 應互不干擾。"""
        klines = self._klines(30)
        signals = [
            _sig("k0_long",    1),
            _sig("long_entry", 3, stop=80.0),
            _sig("long_exit",  5, label="TP"),
            _sig("k0_long",    10),
            _sig("long_entry", 12, stop=80.0),
            _sig("long_exit",  15, label="SL"),
        ]
        trades = [
            _trade(entry_idx=3,  exit_idx=5,  net_pnl=10.0),
            _trade(entry_idx=12, exit_idx=15, net_pnl=-5.0),
        ]

        ctxs = _collect_contexts(signals, trades, klines, context_bars=1)
        self.assertEqual(len(ctxs), 2)
        self.assertEqual(ctxs[0]["entry_ki"], 3)
        self.assertEqual(ctxs[0]["exit_ki"],  5)
        self.assertEqual(ctxs[1]["entry_ki"], 12)
        self.assertEqual(ctxs[1]["exit_ki"],  15)

    # ── test 12: fill_price 保存在 entry_signal ──────────────────────────────
    def test_fill_price_preserved_in_entry_signal(self):
        """tick 模式下，entry signal 帶有 fill_price，ctx 中的 entry_signal 應保留它。"""
        klines = self._klines(20)
        signals = [
            _sig("k0_long",    2),
            StrategySignal(
                open_time=4 * _MS, price=100.0,
                signal_type="long_entry", label="L4",
                stop_price=85.0, fill_price=100.5,
            ),
            _sig("long_exit", 7, label="TP"),
        ]
        trades = [_trade(entry_idx=4, exit_idx=7)]

        ctxs = _collect_contexts(signals, trades, klines)
        self.assertEqual(ctxs[0]["entry_signal"].fill_price, 100.5)

    # ── test 13: 空 signals 不崩潰 ──────────────────────────────────────────
    def test_empty_signals_returns_empty(self):
        klines = self._klines(10)
        ctxs = _collect_contexts([], [], klines)
        self.assertEqual(ctxs, [])

    # ── test 14: entry_ki 恰好在 klines 邊界 ──────────────────────────────────
    def test_entry_at_last_kline(self):
        """entry 在最後一根 kline，win_end 應箝制在 len-1。"""
        klines = self._klines(10)
        last_i = 9
        signals = [
            _sig("long_entry", last_i, stop=85.0),
        ]
        trade = _trade(entry_idx=last_i, exit_idx=0)
        trade["exit_time"] = 0

        ctxs = _collect_contexts(signals, [trade], klines, context_bars=5)
        self.assertEqual(len(ctxs), 1)
        self.assertLessEqual(ctxs[0]["win_end"], len(klines) - 1)


if __name__ == "__main__":
    unittest.main()
