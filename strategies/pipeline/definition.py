"""Pipeline 定義容器：命名、配置一條完整的 TradingPipeline。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from strategies.pipeline.pipeline import TradingPipeline


@dataclass
class PipelineDef:
    """
    命名並設定一條 TradingPipeline 的所有運行參數。

    allocation_weight：此 Pipeline 占總資金的比例（0~1）。
      例：0.5 代表只用 50% 的帳戶資金計算倉位大小。
      Runner 會將 equity × allocation_weight 注入 PipelineContext.equity。

    max_concurrent：此 Pipeline 最多同時持有幾個方向的部位（預設 1）。
      實盤引擎應自行追蹤並在 run_all() 前傳入 can_open 旗標。

    direction_filter：限制訊號方向，"long"/"short"/None（不限）。

    tags：自由標籤，供 UI 篩選或日誌分類。
    """

    name:              str
    pipeline:          TradingPipeline
    allocation_weight: float         = 1.0
    max_concurrent:    int           = 1
    direction_filter:  Optional[str] = None
    enabled:           bool          = True
    tags:              list[str]     = field(default_factory=list)

    def __post_init__(self) -> None:
        if not (0.0 < self.allocation_weight <= 1.0):
            raise ValueError(f"allocation_weight 必須在 (0, 1] 之間，收到 {self.allocation_weight}")
        if self.direction_filter not in (None, "long", "short"):
            raise ValueError(f"direction_filter 必須是 None/'long'/'short'，收到 {self.direction_filter!r}")
