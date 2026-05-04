"""
Pipeline 交易策略架構套件。

快速匯入路徑：
    from strategies.pipeline import (
        TradingPipeline, MultiPipelineRunner, MultiPipelineStrategy,
        PipelineDef, PipelineContext, PipelineResult, SharedContext,
        RegimeComponent, ATRComponent, SessionComponent, VolatilityComponent,
        RegimeStage, SessionStage, AlphaStage, RRStage, FeeStage,
    )
"""
from strategies.pipeline.context import PipelineContext, SharedContext
from strategies.pipeline.result import PipelineResult
from strategies.pipeline.component import (
    SharedComponent,
    ATRComponent,
    RegimeComponent,
    SessionComponent,
    VolatilityComponent,
    TickDeltaComponent,
    TickVWAPComponent,
)
from strategies.pipeline.stages import (
    PipelineStage,
    RegimeStage,
    SessionStage,
    AlphaStage,
    RRStage,
    FeeStage,
    TickFactorStage,
)
from strategies.pipeline.pipeline import TradingPipeline
from strategies.pipeline.definition import PipelineDef
from strategies.pipeline.runner import MultiPipelineRunner
from strategies.pipeline.strategy import MultiPipelineStrategy

__all__ = [
    # Context
    "SharedContext",
    "PipelineContext",
    "PipelineResult",
    # Components
    "SharedComponent",
    "ATRComponent",
    "RegimeComponent",
    "SessionComponent",
    "VolatilityComponent",
    "TickDeltaComponent",
    "TickVWAPComponent",
    # Stages
    "PipelineStage",
    "RegimeStage",
    "SessionStage",
    "AlphaStage",
    "RRStage",
    "FeeStage",
    "TickFactorStage",
    # Pipeline
    "TradingPipeline",
    "PipelineDef",
    # Runner & Strategy
    "MultiPipelineRunner",
    "MultiPipelineStrategy",
]
