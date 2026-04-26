"""交易時段篩選模組。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from strategies.modules.base_module import BaseModule, ModuleConfig


@dataclass
class SessionConfig(ModuleConfig):
    # 允許交易的 UTC 時段列表，每項為 (start_hour_inclusive, end_hour_exclusive)
    # 空列表 = 不限制
    allowed_hours: list[tuple[int, int]] = field(default_factory=list)


class SessionModule(BaseModule):
    """依據 UTC 小時判斷目前是否在允許交易的時段內。"""

    def __init__(self, cfg: SessionConfig | None = None) -> None:
        self.cfg = cfg or SessionConfig()

    def is_active(self, open_time_ms: int) -> bool:
        """
        open_time_ms：K 棒開盤時間（毫秒 Unix 時間戳）。
        回傳 True 代表允許此時段進場。
        """
        if not self.cfg.allowed_hours:
            return True  # 未設定時段限制 → 全時段允許

        hour = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).hour
        return any(start <= hour < end for start, end in self.cfg.allowed_hours)
