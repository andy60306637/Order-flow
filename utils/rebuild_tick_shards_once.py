from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.data_paths import set_data_root_override
from core.tick_cache import load_raw, save_shards, shard_dir, shard_manifest_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build monthly tick shard files from an existing legacy NPZ cache."
    )
    parser.add_argument("--symbol", required=True, help="e.g. BTCUSDT")
    parser.add_argument(
        "--overwrite", action="store_true",
        help="overwrite existing shard files for this symbol",
    )
    parser.add_argument(
        "--data-root",
        default=None,
        help="override ORDERFLOW_DATA_ROOT for cache reads/writes in this process",
    )
    args = parser.parse_args()
    set_data_root_override(args.data_root)

    symbol = args.symbol.upper()
    ticks, meta = load_raw(symbol)
    if ticks is None or len(ticks) == 0:
        raise SystemExit(f"legacy NPZ cache not found for {symbol}")

    manifest = save_shards(symbol, ticks, overwrite=args.overwrite)
    print(f"symbol={symbol}")
    print(f"legacy_span={meta['start_ms']}->{meta['end_ms']}")
    print(f"shard_dir={shard_dir(symbol)}")
    print(f"manifest={shard_manifest_path(symbol)}")
    print(f"months={len(manifest['months'])}")
    print(f"ticks={sum(int(v['count']) for v in manifest['months'].values()):,}")


if __name__ == "__main__":
    main()
