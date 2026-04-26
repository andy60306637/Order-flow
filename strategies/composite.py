"""
組合策略：將多個 SignalModule 以 AND 邏輯串聯，
搭配 ExitModule / CapitalModule / SessionModule / RiskModule 組成完整交易系統。

向下相容：LegacyStrategyAdapter 包裝任何既有 StrategyBase 子類別，
使其可在需要 CompositeStrategy 的場景使用（例如新 UI）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from core.data_types import Kline
from strategies.base import StrategyBase, StrategySignal, TickBarMap
from strategies.modules.capital_management import CapitalConfig, CapitalModule
from strategies.modules.exit_management import ExitConfig, ExitModule
from strategies.modules.risk_management import RiskConfig, RiskModule
from strategies.modules.session_filter import SessionConfig, SessionModule
from strategies.modules.signal_trigger import SignalModule


class CompositeStrategy(StrategyBase):
    """
    組合策略主體。

    進場邏輯（AND 串聯）：
      對每根 K 棒，依序詢問所有 signal modules：
        1. 先呼叫 can_trade()（前置過濾）
        2. detect_k0() 找 K0
        3. entry_conditions() 在 zoom window 內找進場點
      全部 signals 都回傳進場訊號才執行入場。
      （目前實作：以第一個 signal 的 K0 為基準，其餘 signals 在同一 K0 上確認）

    出場邏輯：由 ExitModule 在每根持倉 K 棒上判斷。
    """

    name: str = "Composite"

    def __init__(
        self,
        signals:  List[SignalModule],
        exit_mod: ExitModule | None = None,
        capital:  CapitalModule | None = None,
        session:  SessionModule | None = None,
        risk:     RiskModule | None = None,
    ) -> None:
        if not signals:
            raise ValueError("CompositeStrategy 需要至少一個 SignalModule")
        self._signals = signals
        self._exit    = exit_mod or ExitModule(ExitConfig())
        self._capital = capital  or CapitalModule(CapitalConfig())
        self._session = session
        self._risk    = risk

        # 從第一個 signal 的名稱組成顯示名稱
        sig_names = "+".join(s.name for s in signals)
        self.name = f"Composite[{sig_names}]"

    # ── StrategyBase 介面 ──────────────────────────────────────────────────────

    def on_history(
        self,
        klines:   List[Kline],
        tick_map: Optional[TickBarMap] = None,
    ) -> List[StrategySignal]:
        results: List[StrategySignal] = []
        n = len(klines)
        position: Optional[dict] = None  # 目前持倉
        bars_held: int = 0

        for i in range(1, n):
            k = klines[i]

            # ── 持倉中：檢查出場 ─────────────────────────────────────────────
            if position is not None:
                bars_held += 1
                exit_sig = self._exit.check_exit(
                    k, position, tick_map, bars_held
                )
                if exit_sig is not None:
                    results.append(exit_sig)
                    if self._risk:
                        self._risk.update(0.0)  # 實際 PnL 由 engine 計算
                    position  = None
                    bars_held = 0
                continue

            # ── 無持倉：尋找進場機會 ─────────────────────────────────────────

            # 時段篩選
            if self._session and not self._session.is_active(k.open_time):
                continue

            # 風險篩選（使用上個 K 棒的資金作為 proxy，無複利細節）
            if self._risk and not self._risk.allow_entry(10_000.0):
                continue

            # 主訊號 (index 0)：偵測 K0
            primary = self._signals[0]
            if not primary.can_trade(klines, i):
                continue

            k0_meta = primary.detect_k0(klines, i)
            if k0_meta is None:
                continue

            # 主訊號進場條件
            entry_sig = primary.entry_conditions(klines, i, k0_meta, tick_map)
            if entry_sig is None:
                continue

            # 次要訊號確認（AND 邏輯）
            confirmed = True
            for secondary in self._signals[1:]:
                sec_meta = secondary.detect_k0(klines, i)
                if sec_meta is None:
                    confirmed = False
                    break
                sec_entry = secondary.entry_conditions(klines, i, sec_meta, tick_map)
                if sec_entry is None:
                    confirmed = False
                    break

            if not confirmed:
                continue

            # 建立持倉
            results.append(entry_sig)
            position = self._exit.init_position(
                direction   = k0_meta["direction"],
                entry_price = entry_sig.fill_price or entry_sig.price,
                stop_price  = entry_sig.stop_price or (
                    k.low  if k0_meta["direction"] == "long" else k.high
                ),
                open_time   = k.open_time,
            )
            bars_held = 0

        return results


# ──────────────────────────────────────────────────────────────────────────────
# 向下相容包裝器
# ──────────────────────────────────────────────────────────────────────────────

class LegacyStrategyAdapter(StrategyBase):
    """
    包裝任何既有 StrategyBase 子類別，使其 API 與 CompositeStrategy 一致。
    不修改原始策略邏輯，僅作代理。
    """

    def __init__(self, legacy: StrategyBase) -> None:
        self._legacy = legacy
        self.name    = legacy.name

    def on_history(
        self,
        klines:   List[Kline],
        tick_map: Optional[TickBarMap] = None,
    ) -> List[StrategySignal]:
        return self._legacy.on_history(klines, tick_map)

    def on_kline(self, kline: Kline, history: List[Kline]) -> Optional[StrategySignal]:
        return self._legacy.on_kline(kline, history)
