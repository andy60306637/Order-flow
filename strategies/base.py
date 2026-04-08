"""
策略基礎類別與訊號資料型別。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from core.data_types import Kline


@dataclass
class StrategySignal:
    """單一策略訊號。"""
    open_time:   int    # K 棒 open_time（ms），用於映射至圖表 x 索引
    price:       float  # 訊號觸發價格（通常為收盤價）
    signal_type: str    # "long_entry" | "long_exit" | "short_entry" | "short_exit" | "info"
    label:       str = field(default="")  # 顯示在標記旁的文字
    stop_price:  Optional[float] = field(default=None)  # 進場時的停損價（用於倉位計算）
    fill_price:  Optional[float] = field(default=None)  # 實際成交價（next-bar open 等；None 時 fallback 到 price）


class StrategyBase(ABC):
    """所有策略的抽象基底類別。"""

    name: str = "Unnamed"  # 策略顯示名稱（子類別必須 override）

    # ──────────────────────────────────────────────────────────────────────────
    @abstractmethod
    def on_history(self, klines: List[Kline]) -> List[StrategySignal]:
        """
        對歷史 K 棒序列計算所有訊號。
        klines[0] 最舊，klines[-1] 最新（已收盤或進行中）。
        """

    # ──────────────────────────────────────────────────────────────────────────
    def on_kline(
        self,
        kline: Kline,
        history: List[Kline],
    ) -> Optional[StrategySignal]:
        """
        即時模式：K 棒收盤時呼叫。
        預設實作：對完整 history（含最新 kline）呼叫 on_history，
        取最後一個與 kline.open_time 吻合的訊號。
        子類別可 override 以提高效率（避免對全序列重算）。
        history 已包含收盤的 kline（由 MainWindow 更新後傳入）。
        """
        signals = self.on_history(history)
        for sig in reversed(signals):
            if sig.open_time == kline.open_time:
                return sig
        return None

    # ──────────────────────────────────────────────────────────────────────────
    def compute_stats(self, signals: List[StrategySignal]) -> dict:
        """
        計算回測統計。
        配對規則：
          - long_entry  → long_exit（或 short_entry 作為平多訊號）
          - short_entry → short_exit（或 long_entry 作為平空訊號）
          - 連續兩個同向 entry 只取第一個
          - 未平倉：不計入 P&L，另計入 open_count
        P&L：百分比 gross（不扣手續費）
        """
        trades: List[dict] = []   # {"dir", "entry_p", "exit_p", "pnl_pct"}
        open_long:  Optional[float] = None
        open_short: Optional[float] = None
        open_count = 0

        for sig in signals:
            if sig.signal_type == "long_entry":
                # 對向 entry 自動平掉空單
                if open_short is not None:
                    pnl = (open_short - sig.price) / open_short * 100
                    trades.append({"dir": "short", "entry": open_short, "exit": sig.price, "pnl_pct": pnl})
                    open_short = None
                if open_long is None:
                    open_long = sig.price
            elif sig.signal_type == "long_exit":
                if open_long is not None:
                    pnl = (sig.price - open_long) / open_long * 100
                    trades.append({"dir": "long", "entry": open_long, "exit": sig.price, "pnl_pct": pnl})
                    open_long = None
            elif sig.signal_type == "short_entry":
                # 對向 entry 自動平掉多單
                if open_long is not None:
                    pnl = (sig.price - open_long) / open_long * 100
                    trades.append({"dir": "long", "entry": open_long, "exit": sig.price, "pnl_pct": pnl})
                    open_long = None
                if open_short is None:
                    open_short = sig.price
            elif sig.signal_type == "short_exit":
                if open_short is not None:
                    pnl = (open_short - sig.price) / open_short * 100
                    trades.append({"dir": "short", "entry": open_short, "exit": sig.price, "pnl_pct": pnl})
                    open_short = None

        # 統計未平倉數量
        if open_long is not None:
            open_count += 1
        if open_short is not None:
            open_count += 1

        n = len(trades)
        if n == 0:
            return {"trades": 0, "win_rate": 0.0, "total_pnl": 0.0,
                    "profit_factor": 0.0, "max_consec_loss": 0,
                    "max_drawdown": 0.0,
                    "long_trades": 0, "long_win_rate": 0.0, "long_profit_factor": 0.0,
                    "short_trades": 0, "short_win_rate": 0.0, "short_profit_factor": 0.0,
                    "open_count": open_count, "trade_list": []}

        wins      = sum(1 for t in trades if t["pnl_pct"] > 0)
        total_pnl = sum(t["pnl_pct"] for t in trades)
        win_rate  = wins / n * 100

        # ── Profit Factor ────────────────────────────────────────
        gross_profit = sum(t["pnl_pct"] for t in trades if t["pnl_pct"] > 0)
        gross_loss   = abs(sum(t["pnl_pct"] for t in trades if t["pnl_pct"] < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # ── 最大連續虧損次數 ─────────────────────────────────────
        max_consec_loss = 0
        cur_consec = 0
        for t in trades:
            if t["pnl_pct"] < 0:
                cur_consec += 1
                if cur_consec > max_consec_loss:
                    max_consec_loss = cur_consec
            else:
                cur_consec = 0

        # ── 最大回撤 (%) ────────────────────────────────────────
        equity_peak = 0.0
        equity = 0.0
        max_drawdown = 0.0
        for t in trades:
            equity += t["pnl_pct"]
            if equity > equity_peak:
                equity_peak = equity
            dd = equity_peak - equity
            if dd > max_drawdown:
                max_drawdown = dd

        # ── 多空分離統計 ─────────────────────────────────────────
        def _side_pf(side_trades):
            sn = len(side_trades)
            if sn == 0:
                return 0, 0.0, 0.0
            sw = sum(1 for t in side_trades if t["pnl_pct"] > 0)
            sgp = sum(t["pnl_pct"] for t in side_trades if t["pnl_pct"] > 0)
            sgl = abs(sum(t["pnl_pct"] for t in side_trades if t["pnl_pct"] < 0))
            return sn, sw / sn * 100, (sgp / sgl if sgl > 0 else float("inf"))

        long_n, long_wr, long_pf = _side_pf([t for t in trades if t["dir"] == "long"])
        short_n, short_wr, short_pf = _side_pf([t for t in trades if t["dir"] == "short"])

        return {
            "trades":    n,
            "win_rate":  win_rate,
            "total_pnl": total_pnl,
            "profit_factor": profit_factor,
            "max_consec_loss": max_consec_loss,
            "max_drawdown": max_drawdown,
            "long_trades": long_n,
            "long_win_rate": long_wr,
            "long_profit_factor": long_pf,
            "short_trades": short_n,
            "short_win_rate": short_wr,
            "short_profit_factor": short_pf,
            "open_count": open_count,
            "trade_list": trades,
        }
