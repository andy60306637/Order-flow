"""Vectorized research environment for factor IC and quantile analysis."""

from research.base import FactorBase
from research.registry import get_factor, get_factor_info, list_factor_infos, list_factors, register_factor
from research.runner import ResearchConfig, ResearchResult, run_research

__all__ = [
    "FactorBase",
    "ResearchConfig",
    "ResearchResult",
    "get_factor",
    "get_factor_info",
    "list_factor_infos",
    "list_factors",
    "register_factor",
    "run_research",
]
