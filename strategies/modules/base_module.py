"""策略模組基礎類別。"""
from __future__ import annotations

from abc import ABC
from dataclasses import dataclass


@dataclass
class ModuleConfig:
    """所有模組參數 dataclass 的基底 marker。"""
    pass


class BaseModule(ABC):
    """所有可組合策略模組的抽象基底。"""
    pass
