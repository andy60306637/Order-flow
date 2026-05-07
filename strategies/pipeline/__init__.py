"""
Pipeline 交易策略架構套件。

快速匯入路徑：
    from strategies.pipeline import (
        TradingPipeline, MultiPipelineRunner, MultiPipelineStrategy,
        PipelineDef, PipelineContext, PipelineResult, SharedContext,
        RegimeComponent, ATRComponent, SessionComponent, VolatilityComponent,
        RegimeStage, AlphaStage, RRStage, FeeStage,
    )
"""
from strategies.pipeline.context import PipelineContext, SharedContext
from strategies.pipeline.result import PipelineResult
from strategies.pipeline.component import (
    SharedComponent,
    RegimeClassifier,
    ATRComponent,
    RegimeComponent,
    SessionComponent,
    VolatilityComponent,
    MarketVolatilityRegimeComponent,
    MicroVolatilityComponent,
    TickDeltaComponent,
    TickVWAPComponent,
    VWAPDeviationComponent,
    VolumeProfileComponent,
)
from strategies.pipeline.mean_reversion import (
    VolumeAreaStage,
    ReversalBarUpSignal,
    FeeCoverRatioStage,
    build_mean_reversion_pipeline,
    build_mean_reversion_pipeline_def,
)
from strategies.pipeline.stages import (
    PipelineStage,
    RegimeStage,
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
    "RegimeClassifier",
    "ATRComponent",
    "RegimeComponent",
    "SessionComponent",
    "VolatilityComponent",
    "MarketVolatilityRegimeComponent",
    "MicroVolatilityComponent",
    "TickDeltaComponent",
    "TickVWAPComponent",
    "VWAPDeviationComponent",
    "VolumeProfileComponent",
    # Stages
    "PipelineStage",
    "RegimeStage",
    "AlphaStage",
    "RRStage",
    "FeeStage",
    "TickFactorStage",
    # Mean Reversion Pipeline
    "VolumeAreaStage",
    "ReversalBarUpSignal",
    "FeeCoverRatioStage",
    "build_mean_reversion_pipeline",
    "build_mean_reversion_pipeline_def",
    # Pipeline
    "TradingPipeline",
    "PipelineDef",
    # Runner & Strategy
    "MultiPipelineRunner",
    "MultiPipelineStrategy",
]
