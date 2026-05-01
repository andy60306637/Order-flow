# Wick Reversal v4 Optimization Report

## Scope

- Strategy: `strategies/wick_reversal_v4.py`
- Symbol: `BTCUSDT`
- Data: tick cache (`566,741,855` ticks)
- Full sample: `2025-04-14` to `2026-04-13` UTC
- Train: `2025-04-14` to `2026-01-31` UTC
- Validation: `2026-02-01` to `2026-04-13` UTC
- Backtest assumptions:
  - initial capital: `10,000 USDT`
  - leverage: `20x`
  - fee: `0.032%` per side
  - slippage: `0.2 bps`
  - mode: tick-level entry/exit with 1m bars reconstructed from ticks

Raw search artifact: `docs/reports/wick_reversal_v4_optimization.json`

## Final Decision

- `Long` side: adopt optimized parameters.
- `Short` side: keep baseline numeric structure, disable `S4C`, and add a minimum absolute upper-wick filter for `S4A`.

Reason:

- The long-side optimized set materially improved full-sample profitability and drawdown control.
- The short-side numeric optimization still showed overfit, but short wick subtype filtering plus an `S4A` strength gate produced a cleaner out-of-sample result.

## Adopted Parameters

### Long

- `long_td_consec_bars = 1`
- `long_k0_vol_gate = 500.0`
- `long_vol_sma_mult = 1.0`
- `long_min_fee_cover_ratio = 1.2`
- `long_rr_wick_a = 3.0`
- `long_rr_wick_b = 1.5`
- `long_rr_wick_c = 2.0` (unchanged)

### Short

- Keep baseline numeric values.
- `enable_short_wick_a = True`
- `enable_short_wick_b = True`
- `enable_short_wick_c = False`
- `short_a_min_upper_wick_pct = 0.0011`
- In practice this means: keep `S4A` and `S4B`, drop `S4C`, and only allow `S4A` when the upper wick itself is at least `0.11%` of price.

## Verified Results

### Long only

| Version | Period | Trades | Return | PF | Max DD |
|---|---:|---:|---:|---:|---:|
| Baseline | Train | 249 | -28.26% | 0.93 | 63.46% |
| Optimized | Train | 127 | 193.08% | 1.55 | 23.70% |
| Baseline | Validation | 61 | 46.45% | 1.39 | 18.68% |
| Optimized | Validation | 26 | 20.66% | 1.58 | 13.09% |
| Baseline | Full | 310 | 5.06% | 1.01 | 63.46% |
| Optimized | Full | 153 | 253.64% | 1.56 | 23.70% |

Interpretation:

- Long optimization clearly removed a large amount of low-quality trades.
- Validation return was lower than baseline, but PF and drawdown were better, so the new long setup is more selective and cleaner.

### Short only

| Version | Period | Trades | Return | PF | Max DD |
|---|---:|---:|---:|---:|---:|
| Baseline | Train | 215 | -18.45% | 0.94 | 41.12% |
| Filtered (`S4A+S4B`) | Train | 102 | 25.26% | 1.12 | 25.80% |
| Current short defaults | Train | 51 | 33.61% | 1.34 | 15.54% |
| Baseline | Validation | 91 | 22.08% | 1.13 | 23.03% |
| Filtered (`S4A+S4B`) | Validation | 41 | 23.09% | 1.28 | 19.09% |
| Current short defaults | Validation | 33 | 42.77% | 1.63 | 13.48% |
| Baseline | Full | 306 | -0.44% | 1.00 | 41.12% |
| Filtered (`S4A+S4B`) | Full | 143 | 54.18% | 1.18 | 25.80% |
| Current short defaults | Full | 84 | 90.77% | 1.48 | 15.54% |

Interpretation:

- The main short problem came from `S4C` and weak `S4A` bars with insufficient absolute wick size.
- Removing `S4C` already helped, but the larger improvement came from adding an `S4A` minimum upper-wick filter.
- This turned short-only validation from `+22.08% / PF 1.13 / DD 23.03%` into `+42.77% / PF 1.63 / DD 13.48%`.

### Combined strategy

Current defaults in `wick_reversal_v4.py` = `optimized long + filtered short (A+B only) + S4A wick-strength gate`.

| Version | Period | Trades | Return | PF | Max DD |
|---|---:|---:|---:|---:|---:|
| Old defaults | Train | 454 | -31.24% | 0.95 | 74.63% |
| Current defaults | Train | 176 | 312.17% | 1.53 | 21.67% |
| Old defaults | Validation | 148 | 86.41% | 1.24 | 20.23% |
| Current defaults | Validation | 58 | 76.43% | 1.62 | 13.48% |
| Old defaults | Full | 602 | 28.17% | 1.03 | 74.63% |
| Current defaults | Full | 234 | 627.19% | 1.57 | 21.67% |

Interpretation:

- The new defaults still trade less often than the old version, but they now recover most of the validation return while materially improving PF and drawdown.
- The short-side fix is structural: fewer but cleaner short signals, rather than curve-fit stop/RR tuning.

## Strengths

- Long-side noise was reduced significantly; the strategy now avoids many weak continuation attempts.
- Full-sample drawdown improved materially.
- Short-side noise was reduced by removing `S4C` and enforcing a minimum absolute wick size on `S4A`.
- Tick-level entry/exit logic remains intact, so the optimization is still based on actual intrabar behavior, not bar-close approximations.

## Weaknesses

- `S4A` is better now, but it is still more regime-sensitive than `S4B`.
- Validation return on the combined strategy is still lower than the old defaults, so the new version is not strictly dominant in every market phase.
- `long_rr_ratio` and `short_rr_ratio` exist in the class, but the strategy actually uses `*_rr_wick_a/b/c`; those two ratio fields are effectively dead parameters.

## Recommendations

- Keep the current code state: optimized long, filtered short (`S4A+S4B`), plus the `S4A` absolute wick filter.
- Next round should focus on whether `S4B` needs its own train-side cleanup, because it is still weaker than `S4A` in the train window while staying strong in validation.
- Priority changes for v5:
  - add walk-forward optimization by quarter instead of a single split
  - consider filtering or separately handling `S4A`, because short-side edge is uneven by wick subtype
  - remove or wire up `long_rr_ratio` / `short_rr_ratio` so parameter intent matches implementation
