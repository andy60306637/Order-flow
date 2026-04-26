from __future__ import annotations

from typing import Type

from research.base import FactorBase

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


def ensure_builtin_factors() -> None:
    import research.factors  # noqa: F401
