"""手續費覆蓋計算模組（獨立使用版）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from strategies.modules.base_module import BaseModule, ModuleConfig


@dataclass
class FeeConfig(ModuleConfig):
    taker_rate:    float = 0.0005  # Binance USDT Perp taker 費率 (0.05%)
    slippage_rate: float = 0.0002  # 單邊滑點保守估計 (0.02%)
    min_net_rr:    float = 1.2     # 扣費後最低 net RR 門檻


class FeeModule(BaseModule):
    """
    進場前估算雙邊手續費與滑點，判斷淨收益是否達標。

    費用計算公式（雙邊 taker + 滑點保守估計）：
      total_fee = (entry_notional + exit_notional) × (taker_rate + slippage_rate)

    適合作為 Pipeline 外獨立使用；Pipeline 內請使用 FeeStage。
    """

    def __init__(self, cfg: Optional[FeeConfig] = None) -> None:
        self.cfg = cfg or FeeConfig()

    def approve(
        self,
        entry_price: float,
        stop_price:  float,
        tp_price:    float,
        qty:         float,
    ) -> tuple[bool, dict]:
        """
        回傳 (approved: bool, detail: dict)。

        detail keys：
          expected_fee  總費用（USD）
          net_reward    扣費後預期獲利（USD）
          net_rr        扣費後 net RR
        """
        risk = abs(entry_price - stop_price)
        if risk < 1e-10 or qty <= 0:
            return False, {}

        rate           = self.cfg.taker_rate + self.cfg.slippage_rate
        entry_notional = entry_price * qty
        exit_notional  = tp_price    * qty
        total_fee      = (entry_notional + exit_notional) * rate

        expected_reward = abs(tp_price - entry_price) * qty
        net_reward      = expected_reward - total_fee
        net_rr          = net_reward / (risk * qty) if risk * qty > 0 else 0.0

        approved = net_rr >= self.cfg.min_net_rr
        return approved, {
            "expected_fee": total_fee,
            "net_reward":   net_reward,
            "net_rr":       net_rr,
        }
