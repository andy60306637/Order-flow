---
name: mean-reversion-tuning
description: Specialized agent for tuning and optimizing the MeanReversionPipelineStrategy for BTCUSDT.
---
# BTCUSDT Mean Reversion Strategy Tuning Guide (Agent Instructions)

This guide defines the specialized workflow for tuning the `MeanReversionPipelineStrategy`. Any AI assistant tasked with "optimizing" or "tuning" this strategy must follow these procedures.

---

## 1. Strategy Architecture Overview
The strategy follows a 4-stage Pipeline:
1.  **RegimeStage**: Filters for `MEAN_REVERSION` volatility, specific `VWAP_DEVIATION` zones, and session times.
2.  **AlphaStage (OR Mode)**:
    *   `LWDE`: Absorption factor (Wick x Delta Efficiency).
    *   `CVDD`: Bullish divergence between price troughs and cumulative delta.
    *   `RBU`: Price action reversal candle (Lower wick > Body).
3.  **EntryManagementStage**: ATR-based stop loss with a percentage cap.
4.  **RR & FeeStage**: 2:1 Reward-to-Risk baseline with cost coverage verification.

---

## 2. Tuning Priorities (The "Agent" Logic)

### Priority A: Regime Refinement (Reduce False Positives)
*   **Goal**: Ensure we only trade in "Overextended" conditions.
*   **Parameters**:
    *   `allowed_vwap_zones`: Default is `("extended_low", "overextended_low")`. If win rate is low, restrict to `("overextended_low", "extreme_low")`.
    *   `vwap_window`: Default 120. Adjust based on the mean-reversion cycle (usually 1h-4h for 1m charts).

### Priority B: Alpha Signal Sensitivity
*   **LWDE (Absorption)**:
    *   Increase `min_eff` (default 0.04) to filter for higher quality "V-shape" reversals.
    *   Adjust `min_imbalance` to ensure aggressive buyers are actually present.
*   **CVDD (Divergence)**:
    *   `cvd_window`: Default 20. Longer windows find structural bottoms; shorter windows find micro-bounces.
    *   `price_tolerance`: Default 0.002. Tighten if the strategy enters too late after the bottom.
*   **RBU (Shape)**:
    *   `min_lower_wick_ratio`: Default 0.5. Increase to 0.6+ for "Pin Bar" style strictness.
*   **Shared**: `min_micro_cvd`. This is the ultimate "Gatekeeper". Increase to ensure real-time taker flow supports the reversal.

### Priority C: Risk & Cost (Sustainability)
*   **ATR Stop**: `atr_k` (default 1.0). If stopped out too early before the bounce, increase to 1.5, but watch the `fee_cover_ratio`.
*   **Fee Cover**: `fee_cover_ratio` (default 1.2). Do not lower below 1.1 unless the win rate is exceptionally high (>65%).

---

## 3. Optimization Workflow

1.  **Baseline Run**: Run `utils/fast_backtest.py` on 3 months of BTCUSDT data to get the current metrics.
2.  **Sensitivity Analysis**:
    *   Iterate `vwap_oe_low` from 1.5 to 2.5 in steps of 0.1.
    *   Iterate `min_micro_cvd` from 0 to 5.0 (BTC units).
3.  **Correlation Check**: Ensure the three Alpha factors aren't triggering on the same bars. The "OR" mode is most effective when signals are complementary.
4.  **Validation**:
    *   Must use **Tick Mode** for final validation (`_mr_long_entry` relies on tick-level Micro-CVD).
    *   Check for "Entry Drift": Use `utils/diagnose_entry_drift.py` to ensure fill prices match reality.

---

## 4. Constraint Checklist
*   **Direction**: Long Only (as per current design).
*   **Symbol**: BTCUSDT (High liquidity required for Micro-CVD accuracy).
*   **Fees**: Always assume 0.032% Taker fee and 0.2 bps slippage.
