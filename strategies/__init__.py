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
    from strategies.modules.signal_trigger import SignalModule

STRATEGY_REGISTRY: Dict[str, Type["StrategyBase"]] = {}

# 訊號模組 Registry（與 STRATEGY_REGISTRY 並存，互不影響）
SIGNAL_MODULE_REGISTRY: Dict[str, Type["SignalModule"]] = {}


def register(cls: Type["StrategyBase"]) -> Type["StrategyBase"]:
    """Class decorator：將策略類別登錄至 STRATEGY_REGISTRY。"""
    STRATEGY_REGISTRY[cls.name] = cls
    return cls


def register_signal(cls: Type["SignalModule"]) -> Type["SignalModule"]:
    """Class decorator：將 SignalModule 子類別登錄至 SIGNAL_MODULE_REGISTRY。"""
    SIGNAL_MODULE_REGISTRY[cls.name] = cls
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

# ── 登錄 SignalModule 包裝器 ──────────────────────────────────────────────────
def _register_signal_modules():
    from strategies.modules.signal_trigger import _ensure_named_modules
    _ensure_named_modules()
    # 重新讀取（因為 _ensure_named_modules 可能更新了全域變數）
    from strategies.modules import signal_trigger as _st
    if _st.WickReversalV4Signal is not None:
        SIGNAL_MODULE_REGISTRY[_st.WickReversalV4Signal.name] = _st.WickReversalV4Signal
    if _st.WickReversalV6_1Signal is not None:
        SIGNAL_MODULE_REGISTRY[_st.WickReversalV6_1Signal.name] = _st.WickReversalV6_1Signal

_register_signal_modules()

# ── Pipeline 策略 ─────────────────────────────────────────────────────────────
from strategies.pipeline.mean_reversion import MeanReversionPipelineStrategy as _MRStrategy  # noqa: E402, F401
STRATEGY_REGISTRY[_MRStrategy.name] = _MRStrategy

from strategies.pipeline.mean_reversion_reclaim import ValReclaimPipelineStrategy as _VRStrategy  # noqa: E402, F401
STRATEGY_REGISTRY[_VRStrategy.name] = _VRStrategy

from strategies.pipeline.ny_cvd_divergence_mr import NYCVDDivergenceMRPipelineStrategy as _NYCVDStrategy  # noqa: E402, F401
STRATEGY_REGISTRY[_NYCVDStrategy.name] = _NYCVDStrategy

from strategies.pipeline.asian_overextended_cvd_reversal import AsianCVDOEPipelineStrategy as _AsianCVDOEStrategy  # noqa: E402, F401
STRATEGY_REGISTRY[_AsianCVDOEStrategy.name] = _AsianCVDOEStrategy

from strategies.pipeline.ny_wick_reversal import NYWickReversalPipelineStrategy as _NYWickRevStrategy  # noqa: E402, F401
STRATEGY_REGISTRY[_NYWickRevStrategy.name] = _NYWickRevStrategy

from strategies.pipeline.vp_below_poc_reversion import VPBelowPOCReversionPipelineStrategy as _VPBelowPOCStrategy  # noqa: E402, F401
STRATEGY_REGISTRY[_VPBelowPOCStrategy.name] = _VPBelowPOCStrategy

from strategies.pipeline.vp_reclaim import VPReclaimPipelineStrategy as _VPReclaimStrategy  # noqa: E402, F401
STRATEGY_REGISTRY[_VPReclaimStrategy.name] = _VPReclaimStrategy
