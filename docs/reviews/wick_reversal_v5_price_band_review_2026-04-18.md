# Wick Reversal v5 Price-Band Review

Date: `2026-04-18`

## Scope

This round moved `Wick Reversal 1m v5` from the old 3-bucket regime assumption to a BTC price-band model:

- regime width: `10,000`
- band mode: `b{idx}_*` overrides
- fallback: bands without accepted overrides still use legacy `r0/r1/r2`

Optimization used the 3 tick shards already in the repo:

- `BTCUSDT_20230414_20240413`
- `BTCUSDT_20240414_20250413`
- `BTCUSDT`

Because trade counts are sparse in many bands, not every optimized candidate was accepted. The acceptance rule used in this review was conservative:

- keep the candidate only if validation behavior did not degrade materially
- otherwise keep legacy fallback

## Accepted Bands

Accepted overrides written into `strategies/wick_reversal_v5.py`:

- `b5_short`
  - `short_k0_vol_gate = 500`
  - `short_rr_wick_a/b/c = 2.5 / 1.5 / 0.8`
  - `short_min_fee_cover_ratio = 2.0`
- `b8_long`
  - `long_k0_vol_gate = 800`
  - `long_rr_wick_a/b/c = 3.0 / 2.0 / 1.0`
- `b11_long`
  - `long_sl_pct_floor = 0.001`
  - `long_sl_pct_cap = 0.002`
  - `long_k0_vol_gate = 800`
  - `long_rr_wick_a/b/c = 3.0 / 1.5 / 1.0`

Accepted parameter bundle:

- `docs/reports/wick_v5_price_bands_accepted.json`

## Rejected Bands

These bands were evaluated but not imported because validation did not improve enough or regressed:

- `b2_long`
- `b2_short`
- `b3_long`
- `b6_long`
- `b6_short`
- `b7_short`
- `b9_long`
- `b9_short`
- `b10_long`

Reason:

- train score improved in several cases
- validation often degraded, which is exactly the overfit pattern we want to avoid

## Final 3-Year Backtest

Backtest command:

```bash
python utils/backtest_dynamic_sl.py --strategy "Wick Reversal 1m v5"
```

Verified identical with:

```bash
python utils/backtest_dynamic_sl.py --strategy "Wick Reversal 1m v5" --regime-params docs/reports/wick_v5_price_bands_accepted.json
```

Results:

| Shard | Trades | WR | PF | Net PnL | Max DD |
|---|---:|---:|---:|---:|---:|
| Y1 `2023-04 ~ 2024-04` | 370 | 38.108% | 0.847 | -1004.2094 | 72.6157% |
| Y2 `2024-04 ~ 2025-04` | 210 | 41.905% | 1.148 | 1110.5265 | 25.2784% |
| Y3 `2025-04 ~ 2026-04` | 125 | 54.400% | 1.586 | 4300.7280 | 17.0591% |

## Artifacts

- optimizer: `utils/optimize_wick_reversal_v5_price_bands.py`
- backtest runner: `utils/backtest_dynamic_sl.py`
- accepted params: `docs/reports/wick_v5_price_bands_accepted.json`
- exploratory reports:
  - `docs/reports/wick_v5_price_bands_opt_partial.json`
  - `docs/reports/wick_v5_price_bands_opt_high_long.json`
  - `docs/reports/wick_v5_price_bands_opt_misc_short.json`

## Notes

- `10,000` was chosen over `5,000` for this round because the 3-year sample becomes too sparse once trades are split by side and by price band.
- The current implementation is intentionally hybrid:
  - accepted price-band overrides for bands with evidence
  - legacy 3-regime fallback everywhere else
- If the next round continues this direction, the next useful step is to accumulate more samples and rerun the same optimizer with stricter acceptance thresholds rather than widening the parameter grid.
