# OrderFlow Data Layout

This file defines the expected data root layout for OrderFlow. Future agents should read this file before loading, moving, rebuilding, or deleting market data.

## Data Root

The data root can be the repository-local `data/` directory or an external path selected by configuration.

Resolution order:

1. Explicit CLI/UI path for the current operation.
2. `ORDERFLOW_DATA_ROOT` environment variable.
3. Persisted application setting.
4. Repository-local `data/`.

## Hierarchy

```text
{DATA_ROOT}/
  DATA_LAYOUT.md
  manifests/
    data_root.json
  futures_um/
    ticks/
      aggTrades/
        cache/
        raw/
    klines/
    metrics/
    fundingRate/
    premiumIndexKlines/
    liquidationSnapshot/
```

`futures_um` means Binance USDT-M Futures. Future contract categories should be added as siblings, for example `futures_cm` or `spot`.

## Tick Data

Canonical cache location:

```text
{DATA_ROOT}/futures_um/ticks/aggTrades/cache/
  {SYMBOL}_ticks.npz
  {SYMBOL}_manifest.json
  {SYMBOL}_shards.json
  shards/
    {SYMBOL}/
      {SYMBOL}_YYYYMM.npy
```

Legacy cache location, still supported during migration:

```text
{DATA_ROOT}/ticks/
  {SYMBOL}_ticks.npz
  {SYMBOL}_manifest.json
  {SYMBOL}_shards.json
  shards/{SYMBOL}/{SYMBOL}_YYYYMM.npy
```

Tick cache columns:

```text
0 trade_time_ms
1 price
2 qty
3 is_buyer_maker
```

## Kline Data

Canonical cache location:

```text
{DATA_ROOT}/futures_um/klines/{SYMBOL}/{INTERVAL}/{SYMBOL}_{INTERVAL}.npy
```

Legacy cache location, still supported during migration:

```text
{DATA_ROOT}/klines/{SYMBOL}_{INTERVAL}.npy
```

Kline cache columns follow Binance `/fapi/v1/klines` order:

```text
0 open_time
1 open
2 high
3 low
4 close
5 volume
6 close_time
7 quote_volume
8 count
9 taker_buy_volume
10 taker_buy_quote_volume
11 ignore
```

## Extended Market Data

Recommended locations:

```text
{DATA_ROOT}/futures_um/metrics/{SYMBOL}/raw/
{DATA_ROOT}/futures_um/metrics/{SYMBOL}/cache/

{DATA_ROOT}/futures_um/fundingRate/{SYMBOL}/raw/
{DATA_ROOT}/futures_um/fundingRate/{SYMBOL}/cache/

{DATA_ROOT}/futures_um/premiumIndexKlines/{SYMBOL}/{INTERVAL}/raw/
{DATA_ROOT}/futures_um/premiumIndexKlines/{SYMBOL}/{INTERVAL}/cache/

{DATA_ROOT}/futures_um/liquidationSnapshot/{SYMBOL}/raw/
{DATA_ROOT}/futures_um/liquidationSnapshot/{SYMBOL}/cache/
```

Raw files should preserve Binance Vision filenames. Cache files should have a manifest describing source files, date range, schema, row count, and format version.

## Agent Rules

- Do not assume repository-local `data/` is the active data root.
- Resolve the active data root first.
- Prefer manifests over directory guessing.
- Treat tick shard files and their `{SYMBOL}_shards.json` manifest as one movable unit.
- Do not delete large data files unless explicitly requested.
- Keep this file tracked; do not commit large market data caches unless explicitly requested.

