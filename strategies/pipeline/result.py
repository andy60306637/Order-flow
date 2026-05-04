"""Pipeline 執行結果容器。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from strategies.base import StrategySignal
from strategies.pipeline.context import PipelineContext


@dataclass
class PipelineResult:
    """
    一條 TradingPipeline 成功通過所有 Stage 後的輸出。

    entry_signal：可直接送入回測引擎或實盤下單。
    ctx：保留完整上下文，供後處理（風控、日誌、UI）讀取詳情。
    """
    pipeline_name: str
    ctx:           PipelineContext
    entry_signal:  StrategySignal
    exit_signal:   Optional[StrategySignal] = None
    tags:          list[str]                = field(default_factory=list)
