# Wick Reversal v6 Deployment Plan

## 1) Scope and fixed assumptions

This deployment plan implements `Wick Reversal v6` under the following fixed assumptions:

1. Trailing `cum_delta` is bar-level:
   resets each bar; does not accumulate across bars from entry tick.
2. `StrategySignal` supports tick timestamp:
   add `fill_time` and use it in trade pairing and holding-time calculations.
3. v6 fee/slippage defaults are aligned with UI backtest settings:
   strategy-side fee cover checks must use the same resolved fee/slippage as backtest config.

Out of scope for this phase:

- Refactoring legacy v4/v5 behavior.
- Rewriting backtest engine architecture.
- Multi-position support (still one position at a time).

## 2) Target files

- `strategies/wick_reversal_v6.py` (new)
- `strategies/base.py` (extend `StrategySignal`)
- `backtest/engine.py` (pairing time source update)
- `ui/main_window.py` (build cfg before `on_history`, inject costs into strategy)
- `tests/test_wick_reversal_v6.py` (new)
- `tests/test_backtest.py` (extend for `fill_time` behavior)

## 3) Implementation phases

### Phase A: data contract upgrade (`StrategySignal.fill_time`)

Tasks:

1. Add `fill_time: Optional[int] = None` to `StrategySignal`.
2. Keep backward compatibility:
   existing strategies with only `open_time` still work.
3. Update backtest pairing logic:
   entry/exit timestamp uses `fill_time or open_time`.

Acceptance criteria:

- Existing strategies produce unchanged PnL when `fill_time` is absent.
- Holding-time dependent fields (e.g. funding windows) use tick-time when `fill_time` exists.

### Phase B: UI-to-strategy cost synchronization

Tasks:

1. In `MainWindow._execute_backtest`, create `BacktestConfig` before `strategy.on_history`.
2. Resolve effective fee rate using current `fee_mode/custom_fee_rate`.
3. Add optional strategy hook, for example:
   `configure_backtest_costs(fee_rate: float, slippage_bps: float) -> None`.
4. Call hook if strategy provides it, then run `on_history`, then `simulate_trades` with the same config.

Acceptance criteria:

- Strategy-side `_risk_covers_cost` uses the same fee/slippage basis as final backtest.
- Changing fee mode in UI changes both entry filtering and final PnL path consistently.

### Phase C: new v6 strategy core

Tasks:

1. Create `WickReversalV6Strategy` as independent strategy.
2. Enforce/guard `15m` usage (reject signals or mark unsupported interval).
3. Implement k0 detection with v6 shape rules:
   ATR-based range gate, wick/body ratios, opposite wick cap.
4. Implement dynamic zoom window:
   `N = round(Base_N * ATR(14)/SMA_ATR(100))`, clamped to `[Min_N, Max_N]`.
5. Implement session filter:
   Asia/London/NewYork entry eligibility.
6. Implement tick-level entry and guard invalidation:
   body guard break kills setup, and enforce max/min allowed entry price.
7. Implement stop/target:
   stop by k0 range and `b`, target by `RR`.
8. Implement fee cover:
   `risk * rr >= round_trip_cost * fee_cover_ratio`.

Acceptance criteria:

- Strategy runs in current tick backtest flow without engine changes beyond Phase A/B.
- `fill_price` and `fill_time` are populated on tick entries/exits.
- Setup kill logic and max-entry constraints behave deterministically in unit tests.

### Phase D: bar-level trailing delta

Tasks:

1. Use bar-level `cum_delta` accumulator:
   `bar_cum_delta`, `bar_cum_buy_vol` — reset to 0 at the start of each bar.
2. At TP touch (within a bar):
   choose direct TP vs trailing by bar-level `cum_delta` sign at that tick.
3. In trailing mode:
   set stop to `target_price` (TP level), then exit on TS/TD based on v6 rule.
4. TD exit requires `td_consec_bars >= 2` consecutive bars with non-positive bar delta.
5. Reset bar accumulators when a new bar begins.

Acceptance criteria:

- `cum_delta` resets at bar boundaries (bar-level, not trade-level).
- Trailing stop is set to `target_price`, not breakeven (`entry + round_trip_fee`).
- Long/short mirror behavior is verified with controlled tick sequences.
- TS/TD labels are emitted correctly for backtest stats.

### Phase E: regression and rollout

Tasks:

1. Add tests for:
   `fill_time` fallback behavior, dynamic N bounds, session filter, fee cover, trailing state machine.
2. Run full relevant test set (`backtest`, `wick_reversal_v4`, `wick_reversal_v5`, `v6`).
3. Perform side-by-side smoke run (v4/v5/v6) on same symbol/date range.
4. Document parameter defaults and known constraints.

Acceptance criteria:

- No regression in existing strategy tests.
- v6 outputs stable trade list and labels under repeated runs.
- Tick coverage and fallback behavior are explicit in run results.

## 4) Parameter baseline (v6 initial defaults)

- `required_interval = "15m"`
- `atr_period = 14`
- `sma_atr_period = 100`
- `base_n = 24`
- `min_n = 12`
- `max_n = 48`
- `entry_extension_a = 0.25`
- `stop_extension_b = 0.10`
- `rr = 2.0`
- `fee_cover_ratio = 1.2`
- `allow_bar_fallback_in_tick_mode = False` in tick backtest

## 5) Risks and controls

1. Risk: document ambiguity around short-side max/min entry bound.
   Control: lock mirror formula in code comments and tests.
2. Risk: fee-cover mismatch between strategy and engine.
   Control: Phase B shared config source and explicit resolved fee-rate injection.
3. Risk: funding-time drift without tick timestamp.
   Control: Phase A `fill_time` in pairing logic.
4. Risk: behavior drift from v4 expectations.
   Control: keep v6 isolated strategy class; do not mutate v4/v5 logic.

## 6) Rollout sequence

1. Merge Phase A and Phase B first.
2. Merge Phase C (v6 core without trailing delta), validate baseline.
3. Merge Phase D (trade-level trailing), validate state machine.
4. Finalize Phase E tests and docs, then expose v6 in strategy selector.

## 7) Definition of done

Deployment is complete when:

1. v6 can run end-to-end in current UI tick backtest flow.
2. `fill_time` is produced and consumed correctly.
3. strategy fee/slippage assumptions are synchronized with UI config.
4. bar-level trailing `cum_delta` behavior matches the fixed assumption; trailing stop is set to `target_price`.
5. tests cover core paths and no regression is introduced to existing strategies.

