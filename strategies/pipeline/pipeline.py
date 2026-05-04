"""單一策略 Pipeline 主體。"""
from __future__ import annotations

from typing import Optional

from strategies.pipeline.context import PipelineContext
from strategies.pipeline.stages import PipelineStage


class TradingPipeline:
    """
    單一交易策略 Pipeline。

    依序執行所有 Stage，任一 Stage 回傳 None 即停止並回傳 None。
    最終成功回傳的 PipelineContext 已填入所有 Stage 的結果，
    可直接由 MultiPipelineRunner 轉換為 PipelineResult。
    """

    def __init__(self, stages: list[PipelineStage]) -> None:
        if not stages:
            raise ValueError("TradingPipeline 需要至少一個 Stage")
        self.stages = stages

    def run(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        for stage in self.stages:
            ctx = stage.process(ctx)
            if ctx is None:
                return None
        return ctx

    @property
    def stage_names(self) -> list[str]:
        return [s.name for s in self.stages]
