from __future__ import annotations

from typing import Type

from research.base import FactorBase, factor_sides_label

_FACTOR_REGISTRY: dict[str, Type[FactorBase]] = {}


def register_factor(cls: Type[FactorBase]) -> Type[FactorBase]:
    _FACTOR_REGISTRY[cls.name] = cls
    return cls


def get_factor(name: str) -> FactorBase | None:
    cls = _FACTOR_REGISTRY.get(name)
    return cls() if cls else None


def list_factors(include_tick: bool = True) -> list[str]:
    names: list[str] = []
    for name, cls in _FACTOR_REGISTRY.items():
        if include_tick or not cls.requires_ticks:
            names.append(name)
    return sorted(names)


def get_factor_info(name: str) -> dict[str, object] | None:
    factor = get_factor(name)
    if factor is None:
        return None
    return {
        "name": factor.name,
        "requires_ticks": factor.requires_ticks,
        "sides": factor.sides,
        "side": factor_sides_label(factor.sides),
        "group": factor.group,
    }


def list_factor_infos(include_tick: bool = True) -> list[dict[str, object]]:
    infos: list[dict[str, object]] = []
    for name in list_factors(include_tick=include_tick):
        info = get_factor_info(name)
        if info is not None:
            infos.append(info)
    return infos


def ensure_builtin_factors() -> None:
    import research.factors  # noqa: F401
    import research.mr_alpha_ic_factors  # noqa: F401
