"""
策略模組。

用法：
  在各策略檔案最底部加上 @register，即可自動掛載至 STRATEGY_REGISTRY。
  MainWindow 啟動時從 STRATEGY_REGISTRY 取 key 列表填入 ComboBox。

新增策略：
  1. 在 strategies/ 目錄下建立 my_strategy.py
  2. 繼承 StrategyBase，實作 on_history()
  3. 類別定義後加 @register
  4. 在本檔案末尾 import 該模組（觸發 @register 執行）
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Type

if TYPE_CHECKING:
    from strategies.base import StrategyBase

STRATEGY_REGISTRY: Dict[str, Type["StrategyBase"]] = {}


def register(cls: Type["StrategyBase"]) -> Type["StrategyBase"]:
    """Class decorator：將策略類別登錄至 STRATEGY_REGISTRY。"""
    STRATEGY_REGISTRY[cls.name] = cls
    return cls


# ── 自動匯入所有策略（觸發 @register）────────────────────────────────────────
from strategies import sma_cross as _sma_cross  # noqa: E402, F401
from strategies import wick_reversal as _wick_reversal  # noqa: E402, F401
from strategies import wick_reversal_v4 as _wick_reversal_v4  # noqa: E402, F40
from strategies import wick_reversal_v4_test as _wick_reversal_v4_test
from strategies import wick_reversal_v4_band_files as _wick_reversal_v4_band_files
from strategies import wick_reversal_v4_ratio as _wick_reversal_v4_ratio  # noqa: E402, F401, F811
from strategies import wick_reversal_v5 as _wick_reversal_v5  # noqa: E402, F401
from strategies import auction_value_sweep as _auction_value_sweep  # noqa: E402, F401
from strategies import wick_reversal_v6 as _wick_reversal_v6  # noqa: E402, F401
from strategies import wick_reversal_v4_dyn as _wick_reversal_v4_dyn  # noqa: E402, F401
from strategies import wick_reversal_v6_1 as _wick_reversal_v6_1  # noqa: E402, F401
