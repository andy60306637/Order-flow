"""資金管理模組：倉位大小計算。"""
from __future__ import annotations

from dataclasses import dataclass

from strategies.modules.base_module import BaseModule, ModuleConfig


@dataclass
class CapitalConfig(ModuleConfig):
    max_risk_pct: float = 2.0   # 每筆最大損失佔帳戶比例（0.02 = 2%）
    leverage:     int   = 20
    compound:     bool  = True  # True=複利，False=固定初始資金


class CapitalModule(BaseModule):
    """根據風險比例與槓桿計算合適倉位大小（qty）。"""

    def __init__(self, cfg: CapitalConfig | None = None) -> None:
        self.cfg = cfg or CapitalConfig()

    def position_size(
        self,
        equity:      float,
        entry_price: float,
        stop_price:  float,
        direction:   str,   # "long" | "short"
    ) -> float | None:
        """
        回傳 qty（合約數量）。
        若 stop_price 未設或距離過小則回傳 None（跳過交易）。
        """
        if entry_price <= 0 or stop_price is None or stop_price <= 0:
            return None

        stop_dist = abs(entry_price - stop_price)
        if stop_dist < 1e-10:
            return None

        ref_equity = equity if self.cfg.compound else (equity / max(equity, 1.0))
        max_loss = ref_equity * self.cfg.max_risk_pct / 100.0

        # 風險限制倉位
        risk_qty = max_loss / stop_dist

        # 槓桿限制倉位
        leverage_qty = equity * self.cfg.leverage / entry_price

        qty = min(risk_qty, leverage_qty)
        return max(qty, 0.0) if qty > 0 else None
