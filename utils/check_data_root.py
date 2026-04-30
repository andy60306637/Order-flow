"""Inspect the active OrderFlow data root."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import data_paths, market_data_cache


def _fmt_bytes(size: int | float | None) -> str:
    if size is None:
        return "-"
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def _fmt_utc(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _load_json(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _status(ok: bool, warning: bool = False) -> str:
    if ok:
        return "WARN" if warning else "OK"
    return "MISSING"


def inspect_tick_cache(root: Path, symbol: str) -> dict[str, Any]:
    symbol = symbol.upper()
    tick_dir = root / "ticks"
    shard_manifest = tick_dir / f"{symbol}_shards.json"
    npz_path = tick_dir / f"{symbol}_ticks.npz"
    legacy_manifest = tick_dir / f"{symbol}_manifest.json"

    result: dict[str, Any] = {
        "status": "MISSING",
        "path": str(tick_dir),
        "source": None,
        "count": None,
        "start_utc": None,
        "end_utc": None,
        "size": None,
        "files": {
            "shard_manifest": str(shard_manifest),
            "npz": str(npz_path),
            "legacy_manifest": str(legacy_manifest),
        },
    }

    manifest = _load_json(shard_manifest)
    if manifest:
        months = manifest.get("months", {})
        size = 0
        for entry in months.values():
            rel = entry.get("path")
            if rel:
                size += _file_size(tick_dir / rel)
        result.update({
            "status": "OK",
            "source": "shards",
            "count": sum(int(v.get("count", 0)) for v in months.values()),
            "start_utc": _fmt_utc(int(manifest["start_ms"])) if "start_ms" in manifest else None,
            "end_utc": _fmt_utc(int(manifest["end_ms"])) if "end_ms" in manifest else None,
            "size": _fmt_bytes(size),
        })
        return result

    if npz_path.exists():
        meta = None
        try:
            with np.load(str(npz_path), mmap_mode="r") as npz:
                if "meta" in npz:
                    raw = npz["meta"]
                    meta = {"start_ms": int(raw[0]), "end_ms": int(raw[1])}
                if "data" in npz:
                    result["count"] = int(npz["data"].shape[0])
        except Exception as exc:
            result["error"] = str(exc)
        result.update({
            "status": "WARN" if result.get("error") else "OK",
            "source": "legacy_npz",
            "start_utc": _fmt_utc(meta["start_ms"]) if meta else None,
            "end_utc": _fmt_utc(meta["end_ms"]) if meta else None,
            "size": _fmt_bytes(_file_size(npz_path)),
        })
    return result


def inspect_kline_cache(root: Path, symbol: str, interval: str) -> dict[str, Any]:
    symbol = symbol.upper()
    path = root / "klines" / f"{symbol}_{interval}.npy"
    result: dict[str, Any] = {
        "status": "MISSING",
        "path": str(path),
        "count": None,
        "start_utc": None,
        "end_utc": None,
        "size": None,
    }
    if not path.exists():
        return result

    try:
        arr = np.load(str(path), mmap_mode="r")
        result.update({
            "status": "OK",
            "count": int(arr.shape[0]),
            "start_utc": _fmt_utc(int(arr[0, 0])) if arr.shape[0] else None,
            "end_utc": _fmt_utc(int(arr[-1, 0])) if arr.shape[0] else None,
            "size": _fmt_bytes(_file_size(path)),
        })
    except Exception as exc:
        result.update({"status": "WARN", "error": str(exc), "size": _fmt_bytes(_file_size(path))})
    return result


def inspect_extended_dataset(root: Path, kind: str, symbol: str, interval: str, market: str) -> dict[str, Any]:
    effective_interval = interval if kind == "premiumIndexKlines" else None
    base = market_data_cache.dataset_dir(kind, symbol, interval=effective_interval, market=market)
    raw_dir = base / "raw"
    cache_dir = base / "cache"
    suffix = f"_{effective_interval}" if effective_interval else ""
    cache_file = cache_dir / f"{symbol.upper()}_{kind}{suffix}.npz"
    manifest_file = cache_dir / f"{symbol.upper()}_{kind}{suffix}_manifest.json"
    manifest = _load_json(manifest_file)
    raw_files = sorted(p for p in raw_dir.iterdir() if p.is_file()) if raw_dir.exists() else []

    return {
        "status": _status(cache_file.exists() and manifest is not None, warning=bool(raw_files) and not cache_file.exists()),
        "base": str(base),
        "raw_files": len(raw_files),
        "cache": str(cache_file),
        "cache_exists": cache_file.exists(),
        "manifest": str(manifest_file),
        "manifest_exists": manifest is not None,
        "row_count": manifest.get("row_count") if manifest else None,
        "updated_at": manifest.get("updated_at") if manifest else None,
    }


def inspect_data_root(args: argparse.Namespace) -> dict[str, Any]:
    if args.data_root:
        data_paths.set_data_root_override(args.data_root)
    if args.init:
        data_paths.ensure_data_root_layout()

    root = data_paths.data_root()
    valid, message = data_paths.validate_data_root()
    payload: dict[str, Any] = {
        "data_root": str(root),
        "valid": valid,
        "validation": message,
        "layout_doc": str(root / "DATA_LAYOUT.md"),
        "root_manifest": str(root / "manifests" / "data_root.json"),
        "symbol": args.symbol.upper(),
        "interval": args.interval,
        "tick": inspect_tick_cache(root, args.symbol),
        "kline": inspect_kline_cache(root, args.symbol, args.interval),
        "extended": {},
    }
    for kind in args.datasets:
        payload["extended"][kind] = inspect_extended_dataset(
            root,
            kind,
            args.symbol,
            args.interval,
            args.market,
        )
    return payload


def print_report(payload: dict[str, Any]) -> None:
    print(f"Data root : {payload['data_root']}")
    print(f"Layout    : {'OK' if payload['valid'] else 'WARN'} - {payload['validation']}")
    print(f"Symbol    : {payload['symbol']}")
    print(f"Interval  : {payload['interval']}")
    print()

    tick = payload["tick"]
    print(f"[{tick['status']}] Tick cache")
    print(f"  source : {tick['source'] or '-'}")
    print(f"  count  : {tick['count'] if tick['count'] is not None else '-'}")
    print(f"  range  : {tick['start_utc'] or '-'} -> {tick['end_utc'] or '-'}")
    print(f"  size   : {tick['size'] or '-'}")
    print(f"  path   : {tick['path']}")
    print()

    kline = payload["kline"]
    print(f"[{kline['status']}] Kline cache")
    print(f"  count  : {kline['count'] if kline['count'] is not None else '-'}")
    print(f"  range  : {kline['start_utc'] or '-'} -> {kline['end_utc'] or '-'}")
    print(f"  size   : {kline['size'] or '-'}")
    print(f"  path   : {kline['path']}")
    print()

    for kind, item in payload["extended"].items():
        print(f"[{item['status']}] {kind}")
        print(f"  raw files : {item['raw_files']}")
        print(f"  cache     : {'yes' if item['cache_exists'] else 'no'}")
        print(f"  manifest  : {'yes' if item['manifest_exists'] else 'no'}")
        print(f"  rows      : {item['row_count'] if item['row_count'] is not None else '-'}")
        print(f"  base      : {item['base']}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the active OrderFlow data root.")
    parser.add_argument("--data-root", default=None, help="override active data root for this check")
    parser.add_argument("--symbol", default="BTCUSDT", help="symbol to inspect")
    parser.add_argument("--interval", default="1m", help="kline interval to inspect")
    parser.add_argument("--market", default="futures_um", help="market namespace for extended datasets")
    parser.add_argument("--init", action="store_true", help="create DATA_LAYOUT.md and manifests/data_root.json if missing")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument(
        "--datasets",
        nargs="*",
        default=sorted(market_data_cache.SUPPORTED_DATASETS),
        choices=sorted(market_data_cache.SUPPORTED_DATASETS),
        help="extended datasets to inspect",
    )
    args = parser.parse_args()

    payload = inspect_data_root(args)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print_report(payload)


if __name__ == "__main__":
    main()
