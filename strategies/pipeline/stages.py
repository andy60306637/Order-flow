"""
Pipeline Stage 定義。

每個 Stage 實作 process(ctx) -> Optional[PipelineContext]：
  - 回傳 ctx（已寫入本 Stage 結果）：繼續往下
  - 回傳 None：阻斷 Pipeline，不產生訊號

計算密集的部分透過 SharedComponent + SharedContext 快取，
多個 Pipeline 共用同一個 SharedContext 實例，避免重複計算。

內建 Stages：
  RegimeStage    市場狀態過濾
  AlphaStage     一個或多個 SignalModule 組合（AND / SCORE）
  RRStage        TP 計算 + 最低 RR 確認 + 倉位大小
  FeeStage       手續費覆蓋確認（Pipeline 最後一道關卡）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Optional

from strategies.modules.capital_management import CapitalConfig, CapitalModule
from strategies.modules.exit_management import ExitConfig, ExitModule
from strategies.modules.signal_trigger import SignalModule
from strategies.pipeline.component import (
    ATRComponent,
    RegimeClassifier,
    SharedComponent,
)
from strategies.pipeline.context import PipelineContext


class PipelineStage(ABC):
    """Base class for all pipeline stages."""

    name: str = "UnnamedStage"

    @abstractmethod
    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        """Return ctx to continue or None to stop the pipeline."""
        ...


class RegimeStage(PipelineStage):
    """
    Regime filter stage.

    One pipeline should have one RegimeStage. Trend, session, volatility, or
    other RegimeClassifier components are modeled as dimensions inside it.
    """

    name = "RegimeStage"

    def __init__(
        self,
        component: RegimeClassifier | Sequence[RegimeClassifier] | None = None,
        allowed_regimes: Sequence[str] | Mapping[str, Sequence[str]] | None = None,
        *,
        components: Sequence[RegimeClassifier] | None = None,
        allowed: Sequence[str] | Mapping[str, Sequence[str]] | None = None,
        session_component: RegimeClassifier | None = None,
        allowed_sessions: Sequence[str] | None = None,
    ) -> None:
        selected = components if components is not None else component
        if selected is None:
            raise ValueError("RegimeStage requires at least one RegimeClassifier")

        if isinstance(selected, RegimeClassifier):
            self.components = [selected]
        else:
            self.components = list(selected)

        if session_component is not None:
            self.components.append(session_component)
        if not self.components:
            raise ValueError("RegimeStage requires at least one RegimeClassifier")

        allowed_spec = allowed if allowed is not None else allowed_regimes
        self.allowed_by_dimension = self._normalize_allowed(allowed_spec)
        if allowed_sessions is not None:
            self.allowed_by_dimension["session"] = set(allowed_sessions)

    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        ctx.regime_meta.setdefault("regime_dimensions", {})

        for component in self.components:
            result = ctx.shared.get_or_compute(
                component.component_id,
                lambda component=component: component.compute(ctx.klines, ctx.idx, ctx.tick_map),
            )
            dimension = self._dimension(component)
            label = self._label(result)

            ctx.regime_meta["regime_dimensions"][dimension] = label
            ctx.regime_meta[dimension] = dict(result)
            self._write_legacy_meta(ctx, dimension, result, label)

            allowed = self.allowed_by_dimension.get(dimension)
            if allowed is not None and label not in allowed:
                return None

        return ctx

    def _normalize_allowed(
        self,
        allowed: Sequence[str] | Mapping[str, Sequence[str]] | None,
    ) -> dict[str, set[str]]:
        if allowed is None:
            return {}
        if isinstance(allowed, Mapping):
            return {dimension: set(labels) for dimension, labels in allowed.items()}
        return {self._dimension(self.components[0]): set(allowed)}

    @staticmethod
    def _dimension(component: RegimeClassifier) -> str:
        return getattr(component, "dimension", component.component_id)

    @staticmethod
    def _label(result: dict) -> str:
        label = result.get("label", result.get("regime", result.get("session")))
        if label is None:
            raise ValueError("RegimeClassifier.compute() must return label/regime/session")
        return str(label)

    @staticmethod
    def _write_legacy_meta(
        ctx: PipelineContext,
        dimension: str,
        result: dict,
        label: str,
    ) -> None:
        if dimension == "trend":
            ctx.regime = label
            ctx.regime_meta.update(result)
            ctx.regime_meta["regime"] = label
            return

        if dimension == "session":
            ctx.regime_meta.update(result)





# ── AlphaStage ────────────────────────────────────────────────────────────────

class AlphaStage(PipelineStage):
    """
    執行一個或多個 SignalModule，組合邏輯：

    AND 模式（預設）：
      所有模組都同意（相同方向）才通過。
      entry_price / stop_price 取第一個模組的值。

    SCORE 模式：
      各模組依 weights 加權投票，dominant 方向得票比例 >= min_score 才通過。
      entry_price 取同方向模組的加權平均成交價。
      stop_price  取同方向第一個模組的停損價。

    填入 ctx：direction, entry_price, stop_price, alpha_score, alpha_meta
    """

    name = "AlphaStage"

    def __init__(
        self,
        modules:   list[SignalModule],
        mode:      str                = "AND",
        min_score: float              = 0.5,
        weights:   Optional[list[float]] = None,
    ) -> None:
        if not modules:
            raise ValueError("AlphaStage 需要至少一個 SignalModule")
        self.modules   = modules
        self.mode      = mode.upper()
        self.min_score = min_score
        self.weights   = weights or [1.0] * len(modules)
        if len(self.weights) != len(self.modules):
            raise ValueError("weights 長度必須與 modules 相同")

    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        return self._and_mode(ctx) if self.mode == "AND" else self._score_mode(ctx)

    # ── AND ──────────────────────────────────────────────────────────────────

    def _and_mode(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        klines, idx = ctx.klines, ctx.idx
        direction = entry_price = stop_price = None
        meta_list: list[dict] = []

        for mod in self.modules:
            if not mod.can_trade(klines, idx):
                return None
            k0_meta = mod.detect_k0(klines, idx)
            if k0_meta is None:
                return None
            sig = mod.entry_conditions(klines, idx, k0_meta, ctx.tick_map)
            if sig is None:
                return None

            d = k0_meta["direction"]
            if direction is None:
                direction   = d
                entry_price = sig.fill_price or sig.price
                stop_price  = sig.stop_price
            elif direction != d:
                return None  # 方向衝突，AND 失敗
            meta_list.append({"module": mod.name, "k0_meta": k0_meta})

        if direction is None:
            return None

        ctx.direction   = direction
        ctx.entry_price = entry_price
        ctx.stop_price  = stop_price
        ctx.alpha_score = 1.0
        ctx.alpha_meta  = {"modules": meta_list, "mode": "AND"}
        return ctx

    # ── SCORE ─────────────────────────────────────────────────────────────────

    def _score_mode(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        klines, idx = ctx.klines, ctx.idx
        votes: list[tuple[str, float, Optional[float], float]] = []
        total_w = sum(self.weights)

        for mod, w in zip(self.modules, self.weights):
            if not mod.can_trade(klines, idx):
                continue
            k0_meta = mod.detect_k0(klines, idx)
            if k0_meta is None:
                continue
            sig = mod.entry_conditions(klines, idx, k0_meta, ctx.tick_map)
            if sig is None:
                continue
            votes.append((
                k0_meta["direction"],
                sig.fill_price or sig.price,
                sig.stop_price,
                w,
            ))

        if not votes:
            return None

        long_w  = sum(w for d, _, _, w in votes if d == "long")
        short_w = sum(w for d, _, _, w in votes if d == "short")
        score   = max(long_w, short_w) / total_w

        if score < self.min_score:
            return None

        direction  = "long" if long_w >= short_w else "short"
        side_votes = [(ep, sp, w) for d, ep, sp, w in votes if d == direction]
        w_sum      = sum(w for _, _, w in side_votes)
        entry_price = sum(ep * w for ep, _, w in side_votes) / w_sum
        stop_price  = next((sp for ep, sp, w in side_votes if sp is not None), None)

        ctx.direction   = direction
        ctx.entry_price = entry_price
        ctx.stop_price  = stop_price
        ctx.alpha_score = score
        ctx.alpha_meta  = {"mode": "SCORE", "long_w": long_w, "short_w": short_w}
        return ctx


# ── RRStage ───────────────────────────────────────────────────────────────────

class RRStage(PipelineStage):
    """
    根據 entry/stop 計算 TP 與倉位大小，確認 expected_rr >= min_rr。

    若提供 atr_component，會從 SharedContext 讀取 ATR，
    並取 config RR 與 ATR 推算目標的較大值（不強制，可透過 use_atr_tp=False 關閉）。

    填入 ctx：tp_price, expected_rr, qty, risk_amount
    """

    name = "RRStage"

    def __init__(
        self,
        exit_cfg:      Optional[ExitConfig]    = None,
        capital_cfg:   Optional[CapitalConfig] = None,
        min_rr:        float                   = 1.5,
        atr_component: Optional[ATRComponent]  = None,
        use_atr_tp:    bool                    = False,
    ) -> None:
        self._exit      = ExitModule(exit_cfg or ExitConfig())
        self._capital   = CapitalModule(capital_cfg or CapitalConfig())
        self.min_rr     = min_rr
        self._atr       = atr_component
        self._use_atr_tp = use_atr_tp

    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        if ctx.entry_price is None or ctx.stop_price is None or ctx.direction is None:
            return None

        entry = ctx.entry_price
        stop  = ctx.stop_price
        risk  = abs(entry - stop)
        if risk < 1e-10:
            return None

        # 決定 TP RR 倍數
        rr_ratio = self._exit.cfg.tp_rr_ratio
        if self._use_atr_tp and self._atr is not None:
            atr_result = ctx.shared.get_or_compute(
                self._atr.component_id,
                lambda: self._atr.compute(ctx.klines, ctx.idx, ctx.tick_map),
            )
            atr = atr_result["atr"]
            atr_rr = (atr * 2) / risk if risk > 0 else rr_ratio
            rr_ratio = max(rr_ratio, atr_rr)

        tp_price    = entry + risk * rr_ratio if ctx.direction == "long" else entry - risk * rr_ratio
        expected_rr = abs(tp_price - entry) / risk

        if expected_rr < self.min_rr:
            return None

        qty = self._capital.position_size(
            equity      = ctx.equity,
            entry_price = entry,
            stop_price  = stop,
            direction   = ctx.direction,
        )
        if qty is None or qty <= 0:
            return None

        ctx.tp_price    = tp_price
        ctx.expected_rr = expected_rr
        ctx.qty         = qty
        ctx.risk_amount = risk * qty
        return ctx


# ── FeeStage ──────────────────────────────────────────────────────────────────

class FeeStage(PipelineStage):
    """
    Pipeline 最後一道關卡：估算雙邊手續費 + 滑點，確認淨收益達標。

    費用計算（保守，雙邊 taker + 雙邊滑點）：
      total_fee = (entry_notional + exit_notional) × (taker_rate + slippage_rate)

    net_rr = (expected_reward - total_fee) / risk_amount

    填入 ctx：expected_fee, net_reward, fee_approved
    """

    name = "FeeStage"

    def __init__(
        self,
        taker_rate:    float = 0.0005,
        slippage_rate: float = 0.0002,
        min_net_rr:    float = 1.2,
    ) -> None:
        self.taker_rate    = taker_rate
        self.slippage_rate = slippage_rate
        self.min_net_rr    = min_net_rr

    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        if None in (ctx.qty, ctx.entry_price, ctx.tp_price, ctx.stop_price):
            return None

        entry = ctx.entry_price
        tp    = ctx.tp_price
        stop  = ctx.stop_price
        qty   = ctx.qty
        risk  = abs(entry - stop)

        rate           = self.taker_rate + self.slippage_rate
        total_fee      = (entry * qty + tp * qty) * rate
        expected_reward = abs(tp - entry) * qty
        net_reward      = expected_reward - total_fee
        net_rr          = net_reward / (risk * qty) if risk * qty > 0 else 0.0

        if net_rr < self.min_net_rr:
            return None

        ctx.expected_fee = total_fee
        ctx.net_reward   = net_reward
        ctx.fee_approved = True
        return ctx


# ── TickFactorStage ───────────────────────────────────────────────────────────

class TickFactorStage(PipelineStage):
    """
    通用 tick-based 因子計算 Stage。

    將任何 SharedComponent 的計算結果存入 SharedContext 並附加到
    ctx.alpha_meta["tick_factors"] dict，供後續 Stage（AlphaStage、自定義 Stage）讀取。

    本 Stage 不做阻斷（永遠回傳 ctx），僅負責確保 tick 因子在快取中就位。
    需要阻斷邏輯請繼承此 Stage 並 override process()，或使用自定義 Stage。

    典型用法（放在 AlphaStage 之前）：
        TradingPipeline([
            RegimeStage(...),
            TickFactorStage(component=TickDeltaComponent()),
            TickFactorStage(component=TickVWAPComponent()),
            AlphaStage(modules=[MyTickSignal(delta_key="tick_delta")]),
            RRStage(...),
            FeeStage(...),
        ])
    """

    name = "TickFactorStage"

    def __init__(self, component: SharedComponent) -> None:
        self.component = component
        self.name      = f"TickFactorStage[{component.component_id}]"

    def process(self, ctx: PipelineContext) -> Optional[PipelineContext]:
        result = ctx.shared.get_or_compute(
            self.component.component_id,
            lambda: self.component.compute(ctx.klines, ctx.idx, ctx.tick_map),
        )
        if "tick_factors" not in ctx.alpha_meta:
            ctx.alpha_meta["tick_factors"] = {}
        ctx.alpha_meta["tick_factors"][self.component.component_id] = result
        return ctx
