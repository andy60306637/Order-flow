"""Vectorized research environment for factor IC and quantile analysis."""

from research.base import FactorBase
from research.registry import get_factor, list_factors, register_factor
from research.runner import ResearchConfig, ResearchResult, run_research

__all__ = [
    "FactorBase",
    "ResearchConfig",
    "ResearchResult",
    "get_factor",
    "list_factors",
    "register_factor",
    "run_research",
]
