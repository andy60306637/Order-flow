"""
MultiPipelineStrategy：將 MultiPipelineRunner 包裝為標準 StrategyBase。

可直接插入現有回測引擎（on_history 介面），
現有策略與 CompositeStrategy 完全不受影響。

持倉追蹤邏輯：
  每個 PipelineDef.name 對應一個獨立持倉槽。
  同一 Pipeline 只有在前一筆交易出場後才能再次進場。
  不同 Pipeline 互不影響（各自的 allocation_weight 決定倉位大小）。

平行回測（兩階段）：
  Phase 1 — 多進程預計算所有 bar 的信號候選（signal discovery），
            跳過 PositionGateStage / CooldownStage（狀態依賴，始終通過）。
  Phase 2 — 主進程依序模擬：出場→進場，以實際 equity 重算 qty。
  閾值：len(klines) - 1 >= PARALLEL_THRESHOLD 時啟用平行路徑。
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

from core.data_types import Kline
from strategies.base import StrategyBase, StrategySignal, TickBarMap
from strategies.modules.exit_management import ExitConfig, ExitModule
from strategies.pipeline.context import PipelineContext, SharedContext
from strategies.pipeline.runner import MultiPipelineRunner

logger = logging.getLogger(__name__)

# 啟用平行路徑的最小 bar 數（不含第 0 棒）
PARALLEL_THRESHOLD = 2_000


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
        n_bars = len(klines) - 1
        if n_bars >= PARALLEL_THRESHOLD:
            return self._on_history_parallel(klines, tick_map)
        return self._on_history_sequential(klines, tick_map)

    # ── 循序路徑（原始邏輯）──────────────────────────────────────────────────────

    def _on_history_sequential(
        self,
        klines:   List[Kline],
        tick_map: Optional[TickBarMap] = None,
    ) -> List[StrategySignal]:
        results: List[StrategySignal] = []
        n = len(klines)

        open_positions: dict[str, dict] = {}
        bars_held:      dict[str, int]  = {}

        for i in range(1, n):
            if i % 5000 == 0:
                print(f"  [Strategy] Progress: {i}/{n} bars...")
            k = klines[i]

            closed: list[str] = []
            for pname, pos in list(open_positions.items()):
                bars_held[pname] = bars_held.get(pname, 0) + 1
                exit_sig = self._exit.check_exit(k, pos, tick_map, bars_held[pname])
                if exit_sig is not None:
                    exit_sig.meta.setdefault("pipeline", pname)
                    results.append(exit_sig)
                    closed.append(pname)

            for pname in closed:
                open_positions.pop(pname, None)
                bars_held.pop(pname, None)

            pipeline_results = self._runner.run_all(
                klines   = klines,
                idx      = i,
                equity   = self._equity,
                tick_map = tick_map,
            )
            for pr in pipeline_results:
                pname = pr.pipeline_name
                if pname in open_positions:
                    continue

                k0_sig = self._build_k0_snapshot_signal(pr.ctx)
                if k0_sig is not None:
                    results.append(k0_sig)
                results.append(pr.entry_signal)

                ctx = pr.ctx
                pos = self._exit.init_position(
                    direction   = ctx.direction,
                    entry_price = ctx.entry_price,
                    stop_price  = ctx.stop_price,
                    open_time   = k.open_time,
                )
                if ctx.tp_price is not None:
                    pos["tp_price"] = ctx.tp_price

                open_positions[pname] = pos
                bars_held[pname]      = 0

        return results

    # ── 平行路徑（兩階段）────────────────────────────────────────────────────────

    def _on_history_parallel(
        self,
        klines:   List[Kline],
        tick_map: Optional[TickBarMap] = None,
    ) -> List[StrategySignal]:
        from strategies.pipeline.parallel_backtest import precompute_candidates

        n_workers = min(os.cpu_count() or 4, 8)
        logger.info(
            "[MultiPipelineStrategy] Parallel mode: %d bars, %d workers",
            len(klines) - 1,
            n_workers,
        )
        candidates = precompute_candidates(
            runner       = self._runner,
            klines       = klines,
            tick_map     = tick_map,
            dummy_equity = self._equity,
            n_workers    = n_workers,
        )
        return self._simulate_with_candidates(klines, tick_map, candidates)

    def _get_rr_stages(self) -> dict[str, object]:
        """Return {pipeline_name: RRStage} for all enabled pipelines."""
        from strategies.pipeline.stages import RRStage
        result: dict[str, object] = {}
        for defn in self._runner._defs:
            for stage in defn.pipeline.stages:
                if isinstance(stage, RRStage):
                    result[defn.name] = stage
                    break
        return result

    def _simulate_with_candidates(
        self,
        klines:     List[Kline],
        tick_map:   Optional[TickBarMap],
        candidates: dict[int, list[dict]],
    ) -> List[StrategySignal]:
        """
        Phase 2: sequential trade simulation using pre-computed signal candidates.

        For each bar:
          1. Exit check (identical to sequential path).
          2. Look up pre-computed candidates; skip if bar has none.
          3. Check shared_risk gate once per bar.
          4. For each candidate, check position gate, re-run RRStage with
             actual equity to get correct qty, then build signals and positions.
        """
        rr_stages  = self._get_rr_stages()
        shared_ctx = SharedContext()   # dummy; not used by RRStage

        results:        List[StrategySignal] = []
        n               = len(klines)
        open_positions: dict[str, dict] = {}
        bars_held:      dict[str, int]  = {}

        for i in range(1, n):
            if i % 5000 == 0:
                print(f"  [Strategy] Progress: {i}/{n} bars...")
            k = klines[i]

            # ── 出場檢查 ──────────────────────────────────────────────────────
            closed: list[str] = []
            for pname, pos in list(open_positions.items()):
                bars_held[pname] = bars_held.get(pname, 0) + 1
                exit_sig = self._exit.check_exit(k, pos, tick_map, bars_held[pname])
                if exit_sig is not None:
                    exit_sig.meta.setdefault("pipeline", pname)
                    results.append(exit_sig)
                    closed.append(pname)
            for pname in closed:
                open_positions.pop(pname, None)
                bars_held.pop(pname, None)

            # ── 進場（使用預計算候選）────────────────────────────────────────
            bar_cands = candidates.get(i)
            if not bar_cands:
                continue
            if not self._runner._shared_risk.allow_entry(self._equity):
                continue
            shared_ctx.invalidate(klines, i)  # 每 bar 清除快取，避免 ATR 跨棒污染

            for cand in bar_cands:
                pname = cand["pipeline_name"]
                if pname in open_positions:
                    continue

                rr = rr_stages.get(pname)
                if rr is None:
                    continue

                # 重建 PipelineContext，以實際 equity 重算 qty
                ctx = PipelineContext(
                    klines          = klines,
                    idx             = i,
                    equity          = self._equity * cand["allocation_weight"],
                    tick_map        = tick_map,
                    pipeline_name   = pname,
                    pipeline_weight = cand["allocation_weight"],
                    shared          = shared_ctx,
                )
                ctx.direction   = cand["direction"]
                ctx.entry_price = cand["entry_price"]
                ctx.stop_price  = cand["stop_price"]
                ctx.tp_price    = cand["tp_price"]
                ctx.expected_rr = cand["expected_rr"]
                ctx.alpha_meta  = cand["alpha_meta"]
                ctx.regime      = cand["regime"]
                ctx.regime_meta = cand["regime_meta"]

                result_ctx = rr.process(ctx)  # type: ignore[union-attr]
                if result_ctx is None:
                    continue

                entry_sig = self._runner._build_entry_signal(result_ctx)
                k0_sig    = self._build_k0_snapshot_signal(result_ctx)
                if k0_sig is not None:
                    results.append(k0_sig)
                results.append(entry_sig)

                pos = self._exit.init_position(
                    direction   = result_ctx.direction,
                    entry_price = result_ctx.entry_price,
                    stop_price  = result_ctx.stop_price,
                    open_time   = k.open_time,
                )
                if result_ctx.tp_price is not None:
                    pos["tp_price"] = result_ctx.tp_price
                open_positions[pname] = pos
                bars_held[pname]      = 0

        return results

    @staticmethod
    def _build_k0_snapshot_signal(ctx: PipelineContext) -> Optional[StrategySignal]:
        k0_meta = ctx.alpha_meta.get("k0_meta")
        if not isinstance(k0_meta, dict):
            modules = ctx.alpha_meta.get("modules", [])
            if isinstance(modules, list):
                k0_meta = next(
                    (
                        item.get("k0_meta")
                        for item in modules
                        if isinstance(item, dict) and isinstance(item.get("k0_meta"), dict)
                    ),
                    None,
                )
        if not isinstance(k0_meta, dict):
            return None
        k0_idx = k0_meta.get("k0_idx")
        if not isinstance(k0_idx, int) or k0_idx < 0 or k0_idx >= len(ctx.klines):
            return None
        direction = k0_meta.get("direction", ctx.direction)
        sig_type = "k0_short" if direction == "short" else "k0_long"
        k0_bar = ctx.klines[k0_idx]
        return StrategySignal(
            open_time=k0_bar.open_time,
            price=k0_bar.close,
            signal_type=sig_type,
            label=ctx.pipeline_name,
            meta={
                "pipeline": ctx.pipeline_name,
                "module": ctx.alpha_meta.get("module"),
                **k0_meta,
            },
        )
