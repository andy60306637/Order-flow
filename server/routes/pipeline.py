"""Pipeline Studio REST API."""
from __future__ import annotations

import inspect
from typing import Any

from fastapi import APIRouter, HTTPException

from strategies import STRATEGY_REGISTRY
from strategies.pipeline.strategy import MultiPipelineStrategy

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


_GATE_CLS = {"PositionGateStage", "CooldownStage"}

_KIND_BADGE: dict[str, str] = {
    "RegimeStage": "STAGE 1 - Regime",
    "AlphaStage": "STAGE 2 - Alpha",
    "EntryManagementStage": "STAGE 3 - Entry",
    "VolumeAreaStage": "STAGE 3b - Volume",
    "RRStage": "STAGE 4 - RR",
    "FeeCoverRatioStage": "STAGE 4b - FeeCover",
    "FeeStage": "STAGE 4b - Fee",
    "TickFactorStage": "STAGE - TickFactor",
}


def _safe_scalar(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, set):
        return sorted(str(v) for v in value)
    if isinstance(value, tuple):
        return [_safe_scalar(v) for v in value]
    if isinstance(value, list):
        return [_safe_scalar(v) for v in value[:50]]
    if isinstance(value, dict):
        return {str(k): _safe_scalar(v) for k, v in value.items()}
    name = getattr(value, "name", None)
    return name or type(value).__name__


def _stage_params(stage: Any) -> dict:
    out: dict[str, Any] = {}
    for key, value in vars(stage).items():
        if key.startswith("_"):
            continue
        out[key] = _safe_scalar(value)
    return out


def _stage_summary(stage: Any) -> list[str]:
    cls = type(stage).__name__
    if cls == "RegimeStage":
        lines = [f"{type(c).__name__}" for c in (getattr(stage, "components", None) or [])]
        allowed = getattr(stage, "allowed_by_dimension", {}) or {}
        for dim, vals in allowed.items():
            lines.append(f"{dim}: {', '.join(sorted(vals))}")
        return lines[:8]

    if cls == "AlphaStage":
        lines = [f"mode: {getattr(stage, 'mode', '?')}"]
        for mod in (getattr(stage, "modules", None) or []):
            lines.append(getattr(mod, "name", type(mod).__name__))
        return lines[:8]

    lines = []
    for key, value in vars(stage).items():
        if key.startswith("_") or key == "name":
            continue
        if isinstance(value, (str, int, float, bool)):
            lines.append(f"{key}: {value}")
    return lines[:8]


def _stages(pipeline: Any) -> list:
    val = getattr(pipeline, "stages", None)
    if isinstance(val, (list, tuple)):
        return list(val)
    try:
        return list(pipeline)
    except TypeError:
        return []


def _serialise_strategy(name: str) -> dict:
    cls = STRATEGY_REGISTRY.get(name)
    if cls is None or not inspect.isclass(cls) or not issubclass(cls, MultiPipelineStrategy):
        raise HTTPException(404, "Pipeline strategy not found")

    inst = cls()
    runner = getattr(inst, "_runner", None) or getattr(inst, "runner", None)
    defs = getattr(runner, "_defs", None) or getattr(runner, "defs", []) if runner else []
    pipelines = []

    for pdef in defs:
        gate_no = 0
        stages = []
        for idx, stage in enumerate(_stages(pdef.pipeline)):
            cls_name = type(stage).__name__
            if cls_name in _GATE_CLS:
                gate_no += 1
                badge = f"GATE {gate_no}"
            else:
                badge = _KIND_BADGE.get(cls_name, "STAGE")
            stages.append({
                "index": idx,
                "class_name": cls_name,
                "name": getattr(stage, "name", cls_name),
                "badge": badge,
                "doc": (type(stage).__doc__ or "").strip().split("\n\n")[0],
                "summary": _stage_summary(stage),
                "params": _stage_params(stage),
            })

        pipelines.append({
            "name": getattr(pdef, "name", "pipeline"),
            "allocation_weight": getattr(pdef, "allocation_weight", 1.0),
            "stages": stages,
        })

    return {
        "name": name,
        "class_name": cls.__name__,
        "pipelines": pipelines,
    }


@router.get("/strategies")
def list_pipeline_strategies() -> dict:
    names = [
        name for name, cls in STRATEGY_REGISTRY.items()
        if inspect.isclass(cls) and issubclass(cls, MultiPipelineStrategy)
    ]
    return {"strategies": names}


@router.get("/strategies/{name}")
def get_pipeline_strategy(name: str) -> dict:
    return _serialise_strategy(name)
