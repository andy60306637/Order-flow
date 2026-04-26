"""風險管理模組：每日虧損上限與最大回撤保護。"""
from __future__ import annotations

from dataclasses import dataclass

from strategies.modules.base_module import BaseModule, ModuleConfig


@dataclass
class RiskConfig(ModuleConfig):
    max_daily_loss_pct: float = 5.0   # 每日最大虧損佔帳戶比例
    max_drawdown_pct:   float = 15.0  # 最大回撤比例（達到後停止交易）


class RiskModule(BaseModule):
    """追蹤每日損益與帳戶高水位，超限時拒絕新進場。"""

    def __init__(self, cfg: RiskConfig | None = None) -> None:
        self.cfg = cfg or RiskConfig()
        self._daily_pnl:  float = 0.0
        self._peak_equity: float = 0.0

    # ── 查詢 ──────────────────────────────────────────────────────────────────

    def allow_entry(
        self,
        equity:      float,
        peak_equity: float | None = None,
        daily_pnl:   float | None = None,
    ) -> bool:
        """
        判斷是否允許新進場。
        - equity：目前帳戶資金
        - peak_equity：帳戶歷史高點（None 則使用內部追蹤值）
        - daily_pnl：今日累計損益（None 則使用內部追蹤值）
        """
        peak = peak_equity if peak_equity is not None else self._peak_equity
        dpnl = daily_pnl   if daily_pnl   is not None else self._daily_pnl

        # 更新高水位
        if equity > self._peak_equity:
            self._peak_equity = equity

        if peak > 0:
            dd_pct = (peak - equity) / peak * 100
            if dd_pct >= self.cfg.max_drawdown_pct:
                return False

        if equity > 0 and dpnl < 0:
            daily_loss_pct = abs(dpnl) / equity * 100
            if daily_loss_pct >= self.cfg.max_daily_loss_pct:
                return False

        return True

    # ── 更新狀態 ──────────────────────────────────────────────────────────────

    def update(self, trade_pnl: float) -> None:
        """每筆交易結束後呼叫，累計今日損益。"""
        self._daily_pnl += trade_pnl

    def reset_daily(self) -> None:
        """每日開始時重置當日損益計數器。"""
        self._daily_pnl = 0.0

    def reset(self) -> None:
        """完整重置（新回測開始時）。"""
        self._daily_pnl   = 0.0
        self._peak_equity = 0.0
