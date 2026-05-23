# Pipeline Strategy Builder

Interactively guides the user through building a complete TradingPipeline strategy for the Order-flow project. Use when the user wants to create a new pipeline-based strategy from scratch or expand an existing one.

This skill walks the user through an end-to-end interactive session to create a fully working `TradingPipeline` strategy file (`strategies/pipeline/<strategy_name>.py`), following the same architecture as `mean_reversion.py` and `mean_reversion_reclaim.py`.

## Execution Flow

Work through each phase **in order**. At each phase, collect the missing information by asking the user focused questions. If the user already provided the answer earlier, skip that question and confirm the value instead.

---

### Phase 0 — Parse User Input

First, extract anything the user already stated:
- Strategy name (will become the file name and class prefix)
- Market direction: `long_only` / `short_only` / `both`
- Core pattern description (the market logic being exploited)

If any of the above are missing, ask for them before proceeding.

---

### Phase 1 — Regime Dimensions

Present the available `RegimeClassifier` dimensions (see the Pipeline System Reference at the bottom of this file). Ask the user which **dimensions** to include in `RegimeStage`.

For **each selected dimension**, ask which **labels** are allowed. Show the label options from the reference.

Common combinations for mean-reversion long strategies:
- `market_vol_regime`: `MEAN_REVERSION`, `NEUTRAL`
- `vwap_dev`: `extended_low`, `overextended_low`
- `vol_profile`: `price_in_val_band`, `below_POC`
- `session`: `asian`, `ny`, `overlap`

Ask: **"Do you want a cross-dimension combo whitelist (like `ValReclaimRegimeComboStage`), or independent per-dimension filters?"**
- **Combo whitelist** = more precise, requires defining explicit 4-tuples. Best when regime interactions matter.
- **Independent** = simpler, each dimension filtered separately. Best when dimensions are orthogonal.

If combo whitelist chosen: help the user enumerate the valid `(d1, d2, ...)` tuples. Build the `ALLOWED_REGIMES` list.

---

### Phase 2 — Alpha Signal(s)

Present the available `SignalModule` classes (see Pipeline System Reference below).

Ask:
1. Which signal module(s) to use?
2. If multiple: **AND** (all must fire, same direction) or **OR** (first to fire wins)?
3. Key parameters for each selected signal (show defaults, ask if user wants to override).

---

### Phase 3 — Entry Management

Ask (show defaults, let user accept or override):
- `atr_period` (default 14)
- `atr_k` multiplier (default 1.0)
- `max_sl_pct` maximum stop distance as fraction of entry (default 0.03 = 3%)
- `min_stop_pct` minimum stop distance (default 0.0015 = 0.15%)

---

### Phase 4 — Risk / Reward

Ask:
- `rr_ratio` baseline RR (default 2.0)
- Use **ValReclaimTPAdjustStage** (multi-target TP: min of POC / VWAP / baseline)? Only applicable when `vol_profile` and `vwap_dev` dimensions are in use.
  - If yes: `min_rr_adj` floor after TP compression (default 0.8)
- Capital config: `max_risk_pct` (default 1.0%) and `leverage` (default 20)

---

### Phase 5 — Fee Cover

Ask (show defaults):
- `taker_fee_rate` (default 0.00032 = 0.032%)
- `slippage_rate` (default 0.00002 = 0.2 bps)
- `fee_cover_ratio` (default 1.5)

---

### Phase 6 — Extra Gates

Ask:
- `max_positions` simultaneous (default 1)
- Enable `CooldownStage`? If yes: `cooldown_ms` (default 300_000 = 5 min)

---

### Phase 7 — Code Generation

Generate the complete Python strategy file. Follow this structure **exactly** (modelled on `mean_reversion_reclaim.py`):

```
strategies/pipeline/<strategy_name>.py
```

Sections in order:
1. **Module docstring** — summarise strategy logic, pipeline flow table, regime combo list (if applicable)
2. **Imports** — reuse from `mean_reversion.py` / `mean_reversion_reclaim.py` as much as possible
3. **`ALLOWED_REGIMES` list** — only if combo whitelist was chosen; include dimension union sets
4. **Custom `RegimeComboStage` subclass** — only if combo whitelist; follow `ValReclaimRegimeComboStage` pattern
5. **Custom `RegimeComponent` subclass** — only if a new VP / VWAP classifier wrapper is needed
6. **Signal class** (subclass of `SignalModule`) — implement `can_trade`, `detect_k0`, `entry_conditions`
7. **Custom `TPAdjustStage`** — only if multi-target TP was requested
8. **`build_<name>_pipeline()`** factory function with full `**kwargs` signature
9. **`build_<name>_pipeline_def()`** convenience wrapper returning `PipelineDef`
10. **`<Name>PipelineStrategy`** — `MultiPipelineStrategy` subclass for UI registration

### Code Style Rules

- `from __future__ import annotations` at the top
- All parameters have type hints and defaults
- No inline comments explaining *what* the code does; only *why* for non-obvious constraints
- Follow `mean_reversion_reclaim.py` naming conventions: `build_<name>_pipeline`, `<Name>PipelineStrategy`
- Reuse `EntryManagementStage`, `FeeCoverRatioStage`, `_mr_long_entry` from `mean_reversion.py` via import
- `allowed_combos` is the **single source of truth** — derive dimension unions from it programmatically

---

### Phase 8 — Confirmation

After generating the code:
1. Show a summary table of all chosen parameters
2. Ask: **"Shall I write this to `strategies/pipeline/<name>.py`?"**
3. On confirmation, write the file and also update `strategies/pipeline/__init__.py` if needed.

---

## Important Constraints

- Never implement signals that require unavailable data (e.g., L2 order book) without noting the dependency
- Preserve the fast-path: `RegimeStage` pre-filters with union of labels first; combo stage does final exact matching
- `SharedContext` caching is automatic via `ctx.shared.get_or_compute(component_id, lambda: ...)` — ensure the same component instance is passed to both `RegimeStage` and any `TPAdjustStage` that reads its result
- Do not add `CooldownStage` unless explicitly requested; it complicates backtest reproducibility

---

## Pipeline System Reference

Complete catalogue of built-in components, stages, and signal modules available in `strategies/pipeline/`.

### RegimeClassifiers

Each classifier is a `RegimeClassifier` (subclass of `SharedComponent`). Pass instances to `RegimeStage(components=[...])`.

| Class | Import from | `dimension` key | Labels |
|---|---|---|---|
| `MarketVolatilityRegimeComponent` | `component.py` | `market_vol_regime` | `MEAN_REVERSION`, `NEUTRAL`, `BREAKOUT_TREND`, `CHAOTIC_HIGH_VOL`, `COMPRESSION_WAIT` |
| `VWAPDeviationRegimeComponent` | `mean_reversion.py` | `vwap_dev` | `normal`, `extended_low`, `extended_high`, `overextended_low`, `overextended_high`, `extreme_low`, `extreme_high` |
| `VolumeProfileRegimeComponent` | `mean_reversion_reclaim.py` | `vol_profile` | `below_VAL`, `price_in_val_band`, `below_POC`, `in_value_area`, `above_POC`, `above_VAH` |
| `SessionComponent` | `component.py` | `session` | `asian`, `london`, `ny`, `overlap`, `off` |
| `RegimeComponent` | `component.py` | `trend` | `trending_bull`, `trending_bear`, `ranging`, `volatile` |

#### MarketVolatilityRegimeComponent — Label Definitions

| Label | Condition |
|---|---|
| `MEAN_REVERSION` | rv60_pct < 60 AND atr10/atr60 < 1.2 AND er30 < 0.30 AND adx14 < 25 |
| `BREAKOUT_TREND` | rv60_pct >= 60 AND atr10/atr60 > 1.3 AND er30 > 0.40 AND adx14 > 25 |
| `CHAOTIC_HIGH_VOL` | rv60_pct >= 85 AND atr10/atr60 > 1.5 AND er30 < 0.30 |
| `COMPRESSION_WAIT` | rv60_pct < 30 AND bb_width_pct < 20 |
| `NEUTRAL` | None of the above |

**Key params:** `rv_period=60`, `atr_short=10`, `atr_long=60`, `er_period=30`, `adx_period=14`, `lookback=100`

#### VWAPDeviationRegimeComponent — Label Definitions

VWAP z-score computed over `lookback` bars; `|z|` compared against thresholds:

| Label | z-score range | Side |
|---|---|---|
| `normal` | \|z\| < 1.0 | — |
| `extended_low` | 1.0 <= \|z\| < oe_low | close < VWAP |
| `extended_high` | 1.0 <= \|z\| < oe_low | close > VWAP |
| `overextended_low` | oe_low <= \|z\| <= oe_high | close < VWAP |
| `overextended_high` | oe_low <= \|z\| <= oe_high | close > VWAP |
| `extreme_low` | \|z\| > oe_high | close < VWAP |
| `extreme_high` | \|z\| > oe_high | close > VWAP |

**Key params:** `window=120`, `lookback=300`, `overextended_low=2.0`, `overextended_high=2.5`  
Mean-reversion long recommended: `extended_low`, `overextended_low`

#### VolumeProfileRegimeComponent — Label Definitions

| Label | Condition |
|---|---|
| `below_VAL` | close < val − touch_band |
| `price_in_val_band` | \|close − val\| <= touch_band |
| `below_POC` | val + touch_band < close < poc |
| `in_value_area` | poc <= close <= vah |
| `above_POC` | close > poc AND close <= vah |
| `above_VAH` | close > vah |

**Key params:** `interval="1h"`, `window=24`, `tick_size=1.0`, `value_area_pct=0.70`, `touch_band_pct=0.001`

---

### Built-in Pipeline Stages

| Stage Class | Import from | Purpose | Position |
|---|---|---|---|
| `PositionGateStage` | `stages.py` | Block new signals when `open_count >= max_positions` | Front of pipeline |
| `CooldownStage` | `stages.py` | Block signals for `cooldown_ms` after last exit | After PositionGate |
| `RegimeStage` | `stages.py` | Multi-dimension regime filter | Stage 1 |
| `AlphaStage` | `stages.py` | Combine SignalModules (AND / OR / SCORE) | Stage 2 |
| `EntryManagementStage` | `mean_reversion.py` | ATR(14) stop-loss + max/min stop cap | Stage 3 |
| `RRStage` | `stages.py` | TP = entry ± risk × rr_ratio; position sizing | Stage 4 |
| `ValReclaimTPAdjustStage` | `mean_reversion_reclaim.py` | Multi-target TP = min(POC, VWAP, 2R) | After RRStage |
| `FeeCoverRatioStage` | `mean_reversion.py` | Fee coverage check | Last stage |
| `ValReclaimRegimeComboStage` | `mean_reversion_reclaim.py` | Exact (d1,d2,d3,d4) combo whitelist | After RegimeStage |
| `VolumeAreaStage` | `mean_reversion.py` | Allow only when price inside Value Area | After RegimeStage |
| `TickFactorStage` | `stages.py` | Pre-compute & cache tick factors | Before AlphaStage |

#### EntryManagementStage Parameters

| Param | Default | Description |
|---|---|---|
| `atr_period` | 14 | ATR period for stop calculation |
| `atr_k` | 1.0 | ATR multiplier: stop = k0.low − ATR × k |
| `max_sl_pct` | 0.03 | Cap stop at entry × (1 − max_sl_pct) |
| `min_stop_pct` | 0.0015 | Reject if stop distance < entry × min_stop_pct |

#### FeeCoverRatioStage Parameters

| Param | Default | Description |
|---|---|---|
| `taker_fee_rate` | 0.00032 | Taker fee (0.032%) |
| `slippage_rate` | 0.00002 | Slippage estimate (0.2 bps) |
| `fee_cover_ratio` | 1.5 | gross_reward >= round_trip_cost × ratio |

#### RRStage Parameters

| Param | Default | Description |
|---|---|---|
| `exit_cfg` | `ExitConfig(tp_rr_ratio=2.0)` | Sets TP = entry ± risk × rr |
| `capital_cfg` | `CapitalConfig()` | Position sizing config |
| `min_rr` | 1.5 | Minimum expected RR; reject if below |

---

### Signal Modules (SignalModule subclasses)

All live in `strategies/pipeline/mean_reversion.py` or `mean_reversion_reclaim.py`.  
Every `SignalModule` implements: `can_trade(klines, idx)`, `detect_k0(klines, idx)`, `entry_conditions(klines, k0_idx, k0_meta, tick_map)`.

#### ReversalBarUpSignal

Bullish reversal bar pattern. Signal bar (klines[idx-1]) must satisfy:
- Range > SMA(sma_period) average range
- Lower wick ratio >= min_lower_wick_ratio
- Close position >= min_close_pos

| Param | Default |
|---|---|
| `sma_period` | 20 |
| `min_lower_wick_ratio` | 0.5 |
| `min_close_pos` | 0.6 |
| `sl_offset` | 0.0 |
| `min_micro_cvd` | 0.0 |

Entry trigger: signal bar HIGH. Stop: signal bar LOW − sl_offset (overridden by ATR in EntryManagementStage).

#### LowerWickRatioSignal

Simpler wick filter; signal bar lower wick >= threshold without the SMA range requirement.

| Param | Default |
|---|---|
| `min_wick_ratio` | 0.50 |
| `sl_offset` | 0.0 |
| `min_micro_cvd` | 0.0 |

#### CVDDivergenceSignal

Bullish CVD divergence: price at recent low but cumulative buy delta is higher than at previous trough (or lower, if `flipped=True`).

| Param | Default |
|---|---|
| `window` | 20 |
| `price_tolerance` | 0.002 |
| `min_cvd_divergence` | 0.0 |
| `sl_offset` | 0.0 |
| `min_micro_cvd` | 0.0 |
| `flipped` | False |

#### ValReclaimLongSignal

VAL Reclaim: signal bar (klines[idx-1]) broke below VAL but closed above it. Execution bar confirms with close_pos + one of (neg_delta_abs / lower_wick / small_body).

| Param | Default |
|---|---|
| `vp_interval` | "1h" |
| `vp_window` | 24 |
| `tick_size` | 1.0 |
| `value_area_pct` | 0.70 |
| `touch_band_pct` | 0.001 |
| `min_entry_wick_ratio` | 0.30 |
| `max_entry_body_ratio` | 0.60 |
| `min_close_pos` | 0.55 |
| `sl_offset` | 0.0 |
| `min_micro_cvd` | 0.0 |

---

### Combo Whitelist Pattern

When regime interactions matter, use an `ALLOWED_REGIMES` list with a custom `RegimeComboStage`:

```python
ALLOWED_REGIMES: list[tuple[str, str, str, str]] = [
    ("asian", "NEUTRAL",        "overextended_low", "price_in_val_band"),
    ("ny",    "MEAN_REVERSION", "extended_low",     "below_POC"),
    # ... add rows for each valid combination
]

# Derive fast pre-filter unions automatically:
_ALLOWED_SESSIONS    = frozenset(t[0] for t in ALLOWED_REGIMES)
_ALLOWED_MARKET_VOLS = frozenset(t[1] for t in ALLOWED_REGIMES)
_ALLOWED_VWAP_ZONES  = frozenset(t[2] for t in ALLOWED_REGIMES)
_ALLOWED_VP_LABELS   = frozenset(t[3] for t in ALLOWED_REGIMES)
_ALLOWED_COMBOS      = frozenset(ALLOWED_REGIMES)
```

`RegimeStage` uses the union sets for fast per-dimension pre-filtering.  
`ValReclaimRegimeComboStage` (or a new subclass) does exact 4-tuple matching.

Tuple field order convention: **(session, market_vol_regime, vwap_dev, vol_profile)**  
Adjust field order if using different dimensions; update the combo stage accordingly.

---

### Factory Function Template

```python
def build_<name>_pipeline(
    *,
    # Gate
    max_positions: int = 1,
    # Regime
    allowed_combos: frozenset[tuple] = _ALLOWED_COMBOS,
    # EntryManagement
    atr_period: int = 14, atr_k: float = 1.0,
    max_sl_pct: float = 0.03, min_stop_pct: float = 0.0015,
    # RR + TP
    rr_ratio: float = 2.0, min_rr_adj: float = 0.8,
    use_tp_adjust: bool = False,
    capital_cfg: Optional[CapitalConfig] = None,
    # Fee
    taker_fee_rate: float = 0.00032,
    slippage_rate: float = 0.00002,
    fee_cover_ratio: float = 1.5,
) -> TradingPipeline:
    ...
    return TradingPipeline([gate, regime_stage, combo_stage, alpha_stage,
                            entry_stage, rr_stage, *tp_stages, fee_stage])
```
