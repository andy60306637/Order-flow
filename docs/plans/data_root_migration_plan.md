# Data Root Migration Plan

## Goal

Move OrderFlow away from a project-local, hard-coded `data/` directory and toward a configurable data root that can live on another disk or storage volume.

The migration must support current BTCUSDT tick and kline workflows first, then leave a clear path for added Binance data such as `metrics`, `fundingRate`, `premiumIndexKlines`, and `liquidationSnapshot`.

## Current State

Current cache paths are hard-coded inside cache modules:

- Tick cache: `core/tick_cache.py` uses `<project_root>/data/ticks`.
- Kline cache: `core/kline_cache.py` uses `<project_root>/data/klines`.
- Tick shards: `data/ticks/{SYMBOL}_shards.json` points to monthly `.npy` files under `data/ticks/shards/{SYMBOL}/`.
- Many utilities assume the same project-local layout in docs, examples, and default paths.

Most strategy, research, and UI code already calls cache APIs such as `tick_cache.load_range()` and `kline_cache.load_range_as_klines()`. That means the migration can be concentrated in the cache/path layer instead of rewriting strategy logic.

## Target Configuration

Add a single data root concept:

```text
ORDERFLOW_DATA_ROOT=D:\OrderFlowData
```

Recommended resolution order:

1. Explicit CLI/UI value for the current operation.
2. `ORDERFLOW_DATA_ROOT` environment variable.
3. Persisted application setting, for example `.ui_settings.json`.
4. Fallback to `<project_root>/data` for backward compatibility.

The resolved data root should be exposed by one module, for example:

```text
core/data_paths.py
```

Expected API:

```python
data_root() -> Path
tick_cache_dir() -> Path
kline_cache_dir() -> Path
market_data_dir(kind: str) -> Path
raw_binance_dir(market: str = "futures_um") -> Path
```

All cache modules and tools should call this module rather than computing `Path(__file__).parent.parent / "data"` directly.

## Target Data Layout

The future data root should be self-describing. A format file should exist at:

```text
{DATA_ROOT}/DATA_LAYOUT.md
```

The file tells future agents and tools how to interpret the data tree. A draft is included in the repository at `data/DATA_LAYOUT.md` and should be copied to external data roots.

Recommended hierarchy:

```text
{DATA_ROOT}/
  DATA_LAYOUT.md
  manifests/
    data_root.json
  futures_um/
    ticks/
      aggTrades/
        cache/
          BTCUSDT_ticks.npz
          BTCUSDT_manifest.json
          BTCUSDT_shards.json
          shards/
            BTCUSDT/
              BTCUSDT_202604.npy
        raw/
          BTCUSDT/
            BTCUSDT-aggTrades-2026-04-29.zip
    klines/
      BTCUSDT/
        1m/
          BTCUSDT_1m.npy
    metrics/
      BTCUSDT/
        raw/
        cache/
    fundingRate/
      BTCUSDT/
        raw/
        cache/
    premiumIndexKlines/
      BTCUSDT/
        1m/
          raw/
          cache/
    liquidationSnapshot/
      BTCUSDT/
        raw/
        cache/
```

This uses `futures_um` as the contract category for Binance USDT-M futures. If coin-margined futures or spot data is later added, add sibling categories such as `futures_cm` or `spot`.

## Migration Phases

### Phase 1: Path Abstraction

Add `core/data_paths.py` and route current cache modules through it:

- `core/tick_cache.py`
- `core/kline_cache.py`
- `utils/tick_cache_worker.py`
- `utils/rebuild_tick_cache_once.py`
- `utils/rebuild_tick_shards_once.py`
- `utils/tick_data_backtest.py`
- `utils/benchmark_tick_backtest.py`

Acceptance criteria:

- With no env var, existing project-local `data/` continues to work.
- With `ORDERFLOW_DATA_ROOT` set, tick/kline cache reads and writes move to the external root.
- Existing tests still pass.

### Phase 2: UI and CLI Controls

Add a data root field to application settings and relevant tools:

- UI setting: `data_root`
- CLI flag: `--data-root`
- Environment fallback: `ORDERFLOW_DATA_ROOT`

CLI `--data-root` should override the env var for that process only.

Acceptance criteria:

- User can point the app at `D:\OrderFlowData` without moving the repository.
- UI can display the active data root and warn when expected tick/kline datasets are missing.

### Phase 3: Data Layout Manifest

Introduce a machine-readable manifest:

```text
{DATA_ROOT}/manifests/data_root.json
```

Suggested fields:

```json
{
  "format": "orderflow_data_root_v1",
  "created_by": "OrderFlow",
  "layout_doc": "DATA_LAYOUT.md",
  "markets": ["futures_um"],
  "default_symbol": "BTCUSDT",
  "datasets": {
    "futures_um.ticks.aggTrades": {
      "cache_format": "tick_shards_v1",
      "columns": ["trade_time_ms", "price", "qty", "is_buyer_maker"]
    },
    "futures_um.klines": {
      "cache_format": "binance_kline_npy_v1",
      "columns": [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "count", "taker_buy_volume",
        "taker_buy_quote_volume", "ignore"
      ]
    }
  }
}
```

Acceptance criteria:

- A future agent can inspect `DATA_LAYOUT.md` and `manifests/data_root.json` before touching data files.
- Cache tools can validate that the selected data root is compatible.

### Phase 4: Extended BTCUSDT Datasets

Add cache loaders for:

- `metrics`
- `fundingRate`
- `premiumIndexKlines`
- `liquidationSnapshot`

Recommended module:

```text
core/market_data_cache.py
```

Recommended behavior:

- Keep raw Binance Vision zip/csv files under `raw/`.
- Store normalized cache output under `cache/`.
- Keep one manifest per dataset and symbol.
- Preserve source file names and date ranges in manifests.

BTCUSDT should be the first supported symbol. Avoid building all-symbol workflows until the single-symbol format is stable.

### Phase 5: Deprecate Project-Local Data

After external roots are stable:

- Keep project-local `data/` as a fallback only.
- Document that large datasets should live outside the repository.
- Consider adding `.gitignore` rules for large cache files while keeping `data/DATA_LAYOUT.md` tracked.

## Compatibility Notes

Tick shard manifests currently store paths relative to the tick cache directory. This is good and should be preserved. If all files under `ticks/aggTrades/cache/` are moved together, manifests remain portable.

Legacy flat paths should remain readable during migration:

```text
data/ticks/BTCUSDT_ticks.npz
data/ticks/BTCUSDT_shards.json
data/ticks/shards/BTCUSDT/BTCUSDT_YYYYMM.npy
data/klines/BTCUSDT_1m.npy
```

New tools should write the v1 data root layout. Existing tools can be migrated incrementally.

## Risks

- Some utility scripts still compute project root paths manually.
- UI settings are user state and should not be the only source of truth.
- External disks can be missing or mounted at a different drive letter.
- Mixed old/new layouts can confuse agents unless the data root manifest is present.
- Large tick caches should not be committed to git.

## Recommended Implementation Order

1. Add `core/data_paths.py`.
2. Add `data/DATA_LAYOUT.md` and keep it tracked.
3. Make `tick_cache` and `kline_cache` use `data_paths`.
4. Add env var support and tests for path resolution.
5. Add CLI `--data-root` to data utilities.
6. Add UI display/setting for active data root.
7. Add extended market data cache modules.

