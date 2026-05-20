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
from strategies.pipeline.ny_cvd_divergence_mr import (
    NY_CVD_ALLOWED_REGIMES,
    NYCVDDivergenceLongSignal,
    NYCVDDivergenceMRPipelineStrategy,
    build_ny_cvd_divergence_mr_pipeline,
    build_ny_cvd_divergence_mr_pipeline_def,
)
from strategies.pipeline.asian_overextended_cvd_reversal import (
    ASIAN_CVD_OE_ALLOWED_REGIMES,
    ExtendedVolumeProfileRegimeComponent,
    AsianCVDDivergenceLongSignal,
    AsianCVDOEPipelineStrategy,
    build_asian_cvd_oe_pipeline,
    build_asian_cvd_oe_pipeline_def,
)
from strategies.pipeline.ny_wick_reversal import (
    NY_WICK_REV_ALLOWED_REGIMES,
    POCBandVolumeProfileRegimeComponent,
    NYWickReversalLongSignal,
    NYWickReversalPipelineStrategy,
    build_ny_wick_reversal_pipeline,
    build_ny_wick_reversal_pipeline_def,
)
from strategies.pipeline.vp_reclaim import (
    VPReclaimVolumeProfileRegimeComponent,
    VPReclaimLongSignal,
    VPReclaimPipelineStrategy,
    build_vp_reclaim_pipeline,
    build_vp_reclaim_pipeline_def,
)
from strategies.pipeline.vp_below_poc_reversion import (
    VPBelowPOCLongSignal,
    VPBelowPOCTPAdjustStage,
    VPBelowPOCReversionPipelineStrategy,
    build_vp_below_poc_reversion_pipeline,
    build_vp_below_poc_reversion_pipeline_def,
)
from strategies.pipeline.stages import (
    PipelineStage,
    RegimeStage,
    AlphaStage,
    EnhancerModule,
    EnhancerStage,
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
    "EnhancerModule",
    "EnhancerStage",
    "RRStage",
    "FeeStage",
    "TickFactorStage",
    # Mean Reversion Pipeline
    "VolumeAreaStage",
    "ReversalBarUpSignal",
    "FeeCoverRatioStage",
    "build_mean_reversion_pipeline",
    "build_mean_reversion_pipeline_def",
    # NY CVD Divergence MR Pipeline
    "NY_CVD_ALLOWED_REGIMES",
    "NYCVDDivergenceLongSignal",
    "NYCVDDivergenceMRPipelineStrategy",
    "build_ny_cvd_divergence_mr_pipeline",
    "build_ny_cvd_divergence_mr_pipeline_def",
    # Asian Overextended CVD Reversal Pipeline
    "ASIAN_CVD_OE_ALLOWED_REGIMES",
    "ExtendedVolumeProfileRegimeComponent",
    "AsianCVDDivergenceLongSignal",
    "AsianCVDOEPipelineStrategy",
    "build_asian_cvd_oe_pipeline",
    "build_asian_cvd_oe_pipeline_def",
    # NY Wick Reversal Pipeline
    "NY_WICK_REV_ALLOWED_REGIMES",
    "POCBandVolumeProfileRegimeComponent",
    "NYWickReversalLongSignal",
    "NYWickReversalPipelineStrategy",
    "build_ny_wick_reversal_pipeline",
    "build_ny_wick_reversal_pipeline_def",
    # VP Reclaim Pipeline
    "VPReclaimVolumeProfileRegimeComponent",
    "VPReclaimLongSignal",
    "VPReclaimPipelineStrategy",
    "build_vp_reclaim_pipeline",
    "build_vp_reclaim_pipeline_def",
    # VP Below POC Reversion Pipeline
    "VPBelowPOCLongSignal",
    "VPBelowPOCTPAdjustStage",
    "VPBelowPOCReversionPipelineStrategy",
    "build_vp_below_poc_reversion_pipeline",
    "build_vp_below_poc_reversion_pipeline_def",
    # Pipeline
    "TradingPipeline",
    "PipelineDef",
    # Runner & Strategy
    "MultiPipelineRunner",
    "MultiPipelineStrategy",
]
