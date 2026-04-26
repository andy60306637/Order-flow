"""
訊號觸發模組：K0 偵測與進場條件判斷的抽象介面。
包含：
  - SignalModule（抽象基底）
  - StrategySignalModule（包裝任何 StrategyBase 作為 SignalModule 使用）
  - WickReversalV4Signal, WickReversalV6_1Signal（具名包裝器，方便登錄）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from core.data_types import Kline
from strategies.base import StrategyBase, StrategySignal, TickBarMap
from strategies.modules.base_module import BaseModule


class SignalModule(BaseModule, ABC):
    """
    可組合訊號模組的抽象基底。
    CompositeStrategy 使用 AND 邏輯：所有 SignalModule 都同意才進場。

    子類別需實作：
      - detect_k0(klines, idx) → dict | None
      - entry_conditions(klines, k0_idx, k0_meta, tick_map) → StrategySignal | None
    """

    name: str = "Unnamed"

    @abstractmethod
    def detect_k0(
        self,
        klines: list[Kline],
        idx:    int,
    ) -> Optional[dict]:
        """
        檢查 klines[idx] 是否為有效的 K0（進場觸發蠟燭）。
        回傳含 K0 元資料的 dict（供 entry_conditions 使用），
        或 None（不符合條件）。

        必要 dict key：
          - "direction": "long" | "short"
          - "k0_idx": int
        其餘 key 由各子類別自行定義。
        """
        ...

    @abstractmethod
    def entry_conditions(
        self,
        klines:   list[Kline],
        k0_idx:   int,
        k0_meta:  dict,
        tick_map: Optional[TickBarMap] = None,
    ) -> Optional[StrategySignal]:
        """
        在 K0 之後的 zoom window 內，判斷是否觸發進場訊號。
        回傳 StrategySignal（long_entry / short_entry）或 None。
        """
        ...

    def can_trade(self, klines: list[Kline], idx: int) -> bool:
        """
        可選的前置過濾器（例如 volatility gate）。
        預設永遠回傳 True，子類別可 override。
        """
        return True


# ──────────────────────────────────────────────────────────────────────────────
# 通用包裝器：將任何 StrategyBase 作為 SignalModule 使用
# ──────────────────────────────────────────────────────────────────────────────

class StrategySignalModule(SignalModule):
    """
    將任何 StrategyBase 子類別包裝為 SignalModule。

    實作方式：
      - detect_k0()：對 klines[:idx+1] 執行一次 on_history，
        尋找 klines[idx] 處是否有進場訊號，並快取結果。
      - entry_conditions()：從快取中取出對應訊號回傳。

    由於每次 detect_k0 調用會執行完整 on_history，
    在 CompositeStrategy 中應先以此模組過濾，其餘輕量模組再確認。
    """

    def __init__(
        self,
        strategy: StrategyBase,
        *,
        direction_filter: Optional[str] = None,  # "long" | "short" | None（全部）
    ) -> None:
        self._strategy = strategy
        self._dir_filter = direction_filter
        self.name = strategy.name
        # 快取：(klines id, idx) → list[StrategySignal]
        self._cache_key: tuple = ()
        self._cache_signals: list[StrategySignal] = []

    def _get_signals(self, klines: list[Kline], tick_map: Optional[TickBarMap]) -> list[StrategySignal]:
        key = (id(klines), len(klines))
        if key != self._cache_key:
            self._cache_signals = self._strategy.on_history(klines, tick_map)
            self._cache_key = key
        return self._cache_signals

    def detect_k0(self, klines: list[Kline], idx: int) -> Optional[dict]:
        signals = self._get_signals(klines, None)
        target_time = klines[idx].open_time
        for sig in signals:
            if sig.open_time != target_time:
                continue
            if sig.signal_type not in ("long_entry", "short_entry"):
                continue
            direction = "long" if sig.signal_type == "long_entry" else "short"
            if self._dir_filter and direction != self._dir_filter:
                continue
            return {
                "direction": direction,
                "k0_idx":    idx,
                "signal":    sig,
            }
        return None

    def entry_conditions(
        self,
        klines:   list[Kline],
        k0_idx:   int,
        k0_meta:  dict,
        tick_map: Optional[TickBarMap] = None,
    ) -> Optional[StrategySignal]:
        sig = k0_meta.get("signal")
        if sig is None:
            signals = self._get_signals(klines, tick_map)
            target_time = klines[k0_idx].open_time
            for s in signals:
                if s.open_time == target_time and s.signal_type in ("long_entry", "short_entry"):
                    sig = s
                    break
        return sig


# ──────────────────────────────────────────────────────────────────────────────
# 具名包裝器（供 SIGNAL_MODULE_REGISTRY 登錄使用）
# ──────────────────────────────────────────────────────────────────────────────

def _make_strategy_signal_module(strategy_class, module_name: str, **default_params):
    """工廠函式：建立一個具名的 StrategySignalModule 子類別。"""
    def __init__(self, **kwargs):
        strategy = strategy_class()
        for k, v in {**default_params, **kwargs}.items():
            setattr(strategy, k, v)
        StrategySignalModule.__init__(self, strategy)

    cls = type(
        module_name.replace(" ", "").replace("/", "_"),
        (StrategySignalModule,),
        {
            "__init__": __init__,
            "name": module_name,
        }
    )
    return cls


def _build_named_modules():
    """延遲匯入策略以避免循環依賴，建立具名 SignalModule。"""
    try:
        from strategies.wick_reversal_v4 import WickReversalV4Strategy
        from strategies.wick_reversal_v6_1 import WickReversalV6_1Strategy

        WickReversalV4Signal = _make_strategy_signal_module(
            WickReversalV4Strategy,
            "WickReversal_v4",
        )
        WickReversalV6_1Signal = _make_strategy_signal_module(
            WickReversalV6_1Strategy,
            "WickReversal_v6_1",
        )
        return WickReversalV4Signal, WickReversalV6_1Signal
    except ImportError:
        return None, None


# 模組層級延遲初始化（避免循環 import）
WickReversalV4Signal: Optional[type] = None
WickReversalV6_1Signal: Optional[type] = None


def _ensure_named_modules():
    global WickReversalV4Signal, WickReversalV6_1Signal
    if WickReversalV4Signal is None:
        WickReversalV4Signal, WickReversalV6_1Signal = _build_named_modules()
