"""
MultiPipelineStrategy：將 MultiPipelineRunner 包裝為標準 StrategyBase。

可直接插入現有回測引擎（on_history 介面），
現有策略與 CompositeStrategy 完全不受影響。

持倉追蹤邏輯：
  每個 PipelineDef.name 對應一個獨立持倉槽。
  同一 Pipeline 只有在前一筆交易出場後才能再次進場。
  不同 Pipeline 互不影響（各自的 allocation_weight 決定倉位大小）。
"""
from __future__ import annotations

from typing import List, Optional

from core.data_types import Kline
from strategies.base import StrategyBase, StrategySignal, TickBarMap
from strategies.modules.exit_management import ExitConfig, ExitModule
from strategies.pipeline.context import PipelineContext
from strategies.pipeline.runner import MultiPipelineRunner


class MultiPipelineStrategy(StrategyBase):
    """
    多策略 Pipeline 的回測包裝器。

    使用範例：
        strategy = MultiPipelineStrategy(
            runner=MultiPipelineRunner(defs=[...], shared_risk=...),
            exit_mod=ExitModule(ExitConfig(tp_rr_ratio=2.0)),
            initial_equity=10_000.0,
        )
        signals = strategy.on_history(klines)
    """

    name: str = "MultiPipeline"

    def __init__(
        self,
        runner:         MultiPipelineRunner,
        exit_mod:       Optional[ExitModule] = None,
        initial_equity: float                = 10_000.0,
    ) -> None:
        self._runner  = runner
        self._exit    = exit_mod or ExitModule(ExitConfig())
        self._equity  = initial_equity

        # 策略名稱：由各 Pipeline 名稱組成
        self.name = "MultiPipeline[" + ", ".join(runner.pipeline_names) + "]"

    # ── StrategyBase 介面 ──────────────────────────────────────────────────────

    def on_history(
        self,
        klines:   List[Kline],
        tick_map: Optional[TickBarMap] = None,
    ) -> List[StrategySignal]:
        results: List[StrategySignal] = []
        n = len(klines)

        open_positions: dict[str, dict] = {}   # pipeline_name → position dict
        bars_held:      dict[str, int]  = {}   # pipeline_name → bars held count

        for i in range(1, n):
            k = klines[i]

            # ── 持倉中：出場檢查 ──────────────────────────────────────────────
            closed: list[str] = []
            for pname, pos in list(open_positions.items()):
                bars_held[pname] = bars_held.get(pname, 0) + 1
                exit_sig = self._exit.check_exit(k, pos, tick_map, bars_held[pname])
                if exit_sig is not None:
                    exit_sig.label = pname
                    results.append(exit_sig)
                    closed.append(pname)

            for pname in closed:
                open_positions.pop(pname, None)
                bars_held.pop(pname, None)

            # ── 尋找新進場 ────────────────────────────────────────────────────
            pipeline_results = self._runner.run_all(
                klines   = klines,
                idx      = i,
                equity   = self._equity,
                tick_map = tick_map,
            )
            for pr in pipeline_results:
                pname = pr.pipeline_name
                if pname in open_positions:
                    continue  # 此 Pipeline 已有持倉，跳過

                results.append(pr.entry_signal)

                ctx = pr.ctx
                pos = self._exit.init_position(
                    direction   = ctx.direction,
                    entry_price = ctx.entry_price,
                    stop_price  = ctx.stop_price,
                    open_time   = k.open_time,
                )
                # Pipeline 計算的 TP 優先於 ExitModule config
                if ctx.tp_price is not None:
                    pos["tp_price"] = ctx.tp_price

                open_positions[pname] = pos
                bars_held[pname]      = 0

        return results
