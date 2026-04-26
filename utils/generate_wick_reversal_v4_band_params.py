from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from strategies.wick_reversal_v4 import WickReversalV4Strategy
from strategies.wick_reversal_v4_band_files import WickReversalV4BandFilesStrategy


def _default_params() -> dict[str, object]:
    strategy = WickReversalV4Strategy()
    return {
        name: getattr(strategy, name)
        for name in WickReversalV4BandFilesStrategy.PARAM_FIELDS
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate per-band v4 parameter JSON files.")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=15000)
    ap.add_argument("--step", type=int, default=1000)
    ap.add_argument("--out-dir", default="config/wick_reversal_v4_band_files")
    args = ap.parse_args()

    if args.step <= 0:
        raise SystemExit("--step must be > 0")
    if args.end <= args.start:
        raise SystemExit("--end must be > --start")

    out_dir = Path(args.out_dir) / args.symbol.upper()
    out_dir.mkdir(parents=True, exist_ok=True)
    params = _default_params()

    for low in range(args.start, args.end, args.step):
        high = low + args.step
        payload = {
            "meta": {
                "symbol": args.symbol.upper(),
                "price_low": low,
                "price_high": high,
                "base_strategy": "Wick Reversal 1m v4",
            },
            "params": params,
        }
        out_path = out_dir / f"{low:05d}_{high:05d}.json"
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
