"""
多策略 Pipeline 執行器。

核心職責：
  1. 為每根 K 棒重置共享快取（SharedContext.invalidate）
  2. 將 SharedContext by reference 注入所有 PipelineContext
  3. 依序執行各 PipelineDef，收集通過的結果
  4. 依 conflict 模式解決多策略訊號衝突
  5. 全域風控最後過濾（shared_risk.allow_entry）
"""
from __future__ import annotations

from typing import Optional

from core.data_types import Kline
from strategies.base import StrategySignal, TickBarMap
from strategies.modules.risk_management import RiskConfig, RiskModule
from strategies.pipeline.context import PipelineContext, SharedContext
from strategies.pipeline.definition import PipelineDef
from strategies.pipeline.result import PipelineResult


_CONFLICT_MODES = ("all", "priority", "vote")


class MultiPipelineRunner:
    """
    多策略 Pipeline 執行器。

    conflict 模式：
      "all"      所有 Pipeline 獨立輸出，全部有效訊號都執行。
      "priority" 依 defs 順序，每個方向只取第一個過關的 Pipeline。
      "vote"     多數 Pipeline 同意同一方向才執行（取第一個）。

    shared_risk 是全域風控，在所有 Pipeline 通過後做最終過濾。
    各 Pipeline 內部的風控邏輯（若有）在 Stage 內自行處理。
    """

    def __init__(
        self,
        defs:        list[PipelineDef],
        shared_risk: Optional[RiskModule] = None,
        conflict:    str                  = "all",
    ) -> None:
        if conflict not in _CONFLICT_MODES:
            raise ValueError(f"conflict 必須是 {_CONFLICT_MODES} 之一，收到 {conflict!r}")
        self._defs        = defs
        self._shared_risk = shared_risk or RiskModule(RiskConfig())
        self._conflict    = conflict
        self._shared_ctx  = SharedContext()

    # ── 公開介面 ──────────────────────────────────────────────────────────────

    def run_all(
        self,
        klines:   list[Kline],
        idx:      int,
        equity:   float,
        tick_map: Optional[TickBarMap] = None,
    ) -> list[PipelineResult]:
        """
        對 klines[idx] 執行所有啟用的 Pipeline。
        回傳通過所有 Stage 與風控的 PipelineResult 列表。
        """
        self._shared_ctx.invalidate(klines, idx)

        raw: list[PipelineResult] = []

        for defn in self._defs:
            if not defn.enabled:
                continue

            ctx = PipelineContext(
                klines          = klines,
                idx             = idx,
                equity          = equity * defn.allocation_weight,
                tick_map        = tick_map,
                pipeline_name   = defn.name,
                pipeline_weight = defn.allocation_weight,
                shared          = self._shared_ctx,
            )

            result_ctx = defn.pipeline.run(ctx)
            if result_ctx is None:
                continue

            if defn.direction_filter and result_ctx.direction != defn.direction_filter:
                continue

            entry_sig = self._build_entry_signal(result_ctx)
            raw.append(PipelineResult(
                pipeline_name = defn.name,
                ctx           = result_ctx,
                entry_signal  = entry_sig,
                tags          = list(defn.tags),
            ))

        candidates = self._resolve_conflict(raw)

        if not self._shared_risk.allow_entry(equity):
            return []

        return candidates

    def update_risk(self, trade_pnl: float) -> None:
        """交易結束後呼叫，更新全域風控狀態。"""
        self._shared_risk.update(trade_pnl)

    def reset_daily_risk(self) -> None:
        """每日收盤後呼叫，重置每日損益計數。"""
        self._shared_risk.reset_daily()

    @property
    def pipeline_names(self) -> list[str]:
        return [d.name for d in self._defs]

    # ── 內部 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_entry_signal(ctx: PipelineContext) -> StrategySignal:
        sig_type = "long_entry" if ctx.direction == "long" else "short_entry"
        source_signal = ctx.alpha_meta.get("entry_signal")
        source_meta = getattr(source_signal, "meta", {}) or {}
        fill_price = (
            source_signal.fill_price
            if source_signal is not None and source_signal.fill_price is not None
            else ctx.entry_price
        )
        fill_time = source_signal.fill_time if source_signal is not None else None
        meta = {
            **source_meta,
            "pipeline":    ctx.pipeline_name,
            "regime":      ctx.regime,
            "session":     ctx.regime_meta.get("session"),
            "alpha_score": ctx.alpha_score,
            "expected_rr": ctx.expected_rr,
            "qty":         ctx.qty,
            "tp_price":    ctx.tp_price,
            "expected_fee":ctx.expected_fee,
            "net_reward":  ctx.net_reward,
        }
        return StrategySignal(
            open_time   = ctx.klines[ctx.idx].open_time,
            price       = ctx.entry_price,
            signal_type = sig_type,
            label       = ctx.pipeline_name,
            stop_price  = ctx.stop_price,
            fill_price  = fill_price,
            fill_time   = fill_time,
            meta        = meta,
        )

    def _resolve_conflict(self, results: list[PipelineResult]) -> list[PipelineResult]:
        if self._conflict == "all":
            return results

        if self._conflict == "priority":
            seen: set[str] = set()
            out: list[PipelineResult] = []
            for r in results:
                d = r.ctx.direction
                if d and d not in seen:
                    seen.add(d)
                    out.append(r)
            return out

        if self._conflict == "vote":
            n         = len([d for d in self._defs if d.enabled])
            threshold = n / 2
            dirs      = [r.ctx.direction for r in results]
            long_cnt  = dirs.count("long")
            short_cnt = dirs.count("short")
            if long_cnt > threshold:
                return [r for r in results if r.ctx.direction == "long"][:1]
            if short_cnt > threshold:
                return [r for r in results if r.ctx.direction == "short"][:1]
            return []

        return results
