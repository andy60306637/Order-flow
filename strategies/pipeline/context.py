"""Pipeline 核心資料容器：SharedContext 與 PipelineContext。"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

from core.data_types import Kline

TickBarMap = Mapping[int, np.ndarray]


class SharedContext:
    """
    跨 Pipeline 共享的計算快取。

    生命週期：每根 K 棒一個快取週期。
    MultiPipelineRunner 在每次 run_all() 開頭呼叫 invalidate()，
    確保舊資料不會被下一根 K 棒誤用。

    所有 Pipeline 共用同一個 SharedContext 實例（by reference），
    第一個 Pipeline 計算好的結果，後續 Pipeline 直接讀取。
    """

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}
        self._cache_key: tuple = ()

    def invalidate(self, klines: list[Kline], idx: int) -> None:
        """新 K 棒開始時呼叫，清除舊快取。"""
        key = (id(klines), idx)
        if key != self._cache_key:
            self._cache.clear()
            self._cache_key = key

    def get_or_compute(self, component_id: str, fn: Callable[[], dict]) -> dict:
        """
        若 component_id 已在快取中，直接回傳；
        否則呼叫 fn() 計算並存入快取。
        """
        if component_id not in self._cache:
            self._cache[component_id] = fn()
        return self._cache[component_id]

    def has(self, component_id: str) -> bool:
        return component_id in self._cache

    def get(self, component_id: str, default: Any = None) -> Any:
        return self._cache.get(component_id, default)

    def set(self, component_id: str, value: dict) -> None:
        """手動寫入快取（測試或外部預計算用）。"""
        self._cache[component_id] = value


@dataclass
class PipelineContext:
    """
    Pipeline 執行時的流動狀態容器。

    各 Stage 讀取前面 Stage 寫入的欄位，並填入自己負責的欄位。
    任一 Stage 回傳 None 代表阻斷，不修改此物件。

    欄位按 Stage 職責分組，方便對照：
      - 輸入欄位：由 MultiPipelineRunner 注入
      - 系統欄位：Pipeline 識別與資源配置
      - RegimeStage：市場狀態
      - SessionStage：交易時段（寫入 regime_meta["session"]）
      - AlphaStage：方向與進出場價格
      - RRStage：目標 TP、倉位大小
      - FeeStage：費用核算
    """

    # ── 輸入（由 Runner 注入）──────────────────────────────────────────────────
    klines:   list[Kline]
    idx:      int
    equity:   float
    tick_map: Optional[TickBarMap] = None

    # ── 系統欄位 ──────────────────────────────────────────────────────────────
    pipeline_name:   str          = ""
    pipeline_weight: float        = 1.0
    shared:          SharedContext = field(default_factory=SharedContext)

    # ── RegimeStage ──────────────────────────────────────────────────────────
    regime:      Optional[str] = None   # "trending_bull"|"trending_bear"|"ranging"|"volatile"
    regime_meta: dict          = field(default_factory=dict)

    # ── AlphaStage ───────────────────────────────────────────────────────────
    direction:   Optional[str]   = None   # "long" | "short"
    entry_price: Optional[float] = None
    stop_price:  Optional[float] = None
    alpha_score: float           = 0.0    # 0~1，SCORE 模式下的投票加權分數
    alpha_meta:  dict            = field(default_factory=dict)

    # ── RRStage ──────────────────────────────────────────────────────────────
    tp_price:    Optional[float] = None
    expected_rr: Optional[float] = None
    qty:         Optional[float] = None
    risk_amount: Optional[float] = None   # stop_dist × qty（USD 風險金額）

    # ── FeeStage ─────────────────────────────────────────────────────────────
    expected_fee: Optional[float] = None
    net_reward:   Optional[float] = None  # 扣費後預期獲利（USD）
    fee_approved: bool            = False
