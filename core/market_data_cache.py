"""Cache helpers for non-tick Binance market datasets."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from core import data_paths

SUPPORTED_DATASETS = {
    "metrics",
    "fundingRate",
    "premiumIndexKlines",
    "liquidationSnapshot",
}


def _validate_dataset(kind: str) -> str:
    if kind not in SUPPORTED_DATASETS:
        raise ValueError(f"unsupported market data kind: {kind!r}")
    return kind


def dataset_dir(
    kind: str,
    symbol: str,
    *,
    interval: str | None = None,
    market: str = "futures_um",
) -> Path:
    kind = _validate_dataset(kind)
    symbol = symbol.upper()
    base = data_paths.market_data_dir(kind, market) / symbol
    if kind == "premiumIndexKlines":
        if not interval:
            raise ValueError("premiumIndexKlines requires interval")
        base = base / interval
    return base


def raw_dir(
    kind: str,
    symbol: str,
    *,
    interval: str | None = None,
    market: str = "futures_um",
) -> Path:
    return dataset_dir(kind, symbol, interval=interval, market=market) / "raw"


def cache_dir(
    kind: str,
    symbol: str,
    *,
    interval: str | None = None,
    market: str = "futures_um",
) -> Path:
    return dataset_dir(kind, symbol, interval=interval, market=market) / "cache"


def cache_path(
    kind: str,
    symbol: str,
    *,
    interval: str | None = None,
    market: str = "futures_um",
) -> Path:
    out_dir = cache_dir(kind, symbol, interval=interval, market=market)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_{interval}" if interval else ""
    return out_dir / f"{symbol.upper()}_{kind}{suffix}.npz"


def manifest_path(
    kind: str,
    symbol: str,
    *,
    interval: str | None = None,
    market: str = "futures_um",
) -> Path:
    out_dir = cache_dir(kind, symbol, interval=interval, market=market)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_{interval}" if interval else ""
    return out_dir / f"{symbol.upper()}_{kind}{suffix}_manifest.json"


def list_raw_files(
    kind: str,
    symbol: str,
    *,
    interval: str | None = None,
    market: str = "futures_um",
) -> list[Path]:
    path = raw_dir(kind, symbol, interval=interval, market=market)
    if not path.exists():
        return []
    return sorted(p for p in path.iterdir() if p.is_file())


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        has_header = csv.Sniffer().has_header(sample) if sample else False
        if has_header:
            return list(csv.DictReader(fh))
        reader = csv.reader(fh)
        return [{str(idx): value for idx, value in enumerate(row)} for row in reader]


def _coerce_value(value: object) -> float:
    if value is None:
        return np.nan
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return np.nan
    try:
        return float(text)
    except ValueError:
        return np.nan


def rows_to_array(
    rows: Sequence[Mapping[str, object]],
    *,
    columns: Sequence[str] | None = None,
) -> tuple[np.ndarray, list[str]]:
    if not rows:
        return np.empty((0, 0), dtype=np.float64), list(columns or [])
    resolved_columns = list(columns or rows[0].keys())
    arr = np.empty((len(rows), len(resolved_columns)), dtype=np.float64)
    for row_idx, row in enumerate(rows):
        for col_idx, col in enumerate(resolved_columns):
            arr[row_idx, col_idx] = _coerce_value(row.get(col))
    return arr, resolved_columns


def save_cache(
    kind: str,
    symbol: str,
    rows: Sequence[Mapping[str, object]] | np.ndarray,
    *,
    columns: Sequence[str] | None = None,
    interval: str | None = None,
    market: str = "futures_um",
    source_files: Iterable[str | Path] = (),
    time_column: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict:
    data_paths.ensure_data_root_layout()
    if isinstance(rows, np.ndarray):
        arr = np.asarray(rows, dtype=np.float64)
        resolved_columns = list(columns or [str(i) for i in range(arr.shape[1] if arr.ndim == 2 else 1)])
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
    else:
        arr, resolved_columns = rows_to_array(rows, columns=columns)

    np.savez_compressed(str(cache_path(kind, symbol, interval=interval, market=market)), data=arr)
    source_names = [Path(p).name for p in source_files]
    start_ms: int | None = None
    end_ms: int | None = None
    resolved_time_column = time_column
    if arr.size and arr.ndim == 2 and arr.shape[1] > 0:
        if resolved_time_column is None:
            resolved_time_column = "timestamp_ms" if "timestamp_ms" in resolved_columns else resolved_columns[0]
        if resolved_time_column in resolved_columns:
            ts = arr[:, resolved_columns.index(resolved_time_column)]
            ts = ts[np.isfinite(ts)]
            if len(ts):
                start_ms = int(np.nanmin(ts))
                end_ms = int(np.nanmax(ts))

    manifest = {
        "format": "market_data_npz_v1",
        "market": market,
        "kind": kind,
        "symbol": symbol.upper(),
        "interval": interval,
        "columns": resolved_columns,
        "row_count": int(arr.shape[0]),
        "source_files": source_names,
        "time_column": resolved_time_column,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    if metadata:
        manifest.update(dict(metadata))
    with open(manifest_path(kind, symbol, interval=interval, market=market), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    return manifest


def build_cache_from_csv_files(
    kind: str,
    symbol: str,
    paths: Sequence[str | Path],
    *,
    columns: Sequence[str] | None = None,
    interval: str | None = None,
    market: str = "futures_um",
) -> dict:
    rows: list[dict[str, str]] = []
    file_paths = [Path(p) for p in paths]
    for path in file_paths:
        rows.extend(read_csv_rows(path))
    return save_cache(
        kind,
        symbol,
        rows,
        columns=columns,
        interval=interval,
        market=market,
        source_files=file_paths,
    )


def load_cache(
    kind: str,
    symbol: str,
    *,
    interval: str | None = None,
    market: str = "futures_um",
) -> tuple[np.ndarray | None, dict | None]:
    path = cache_path(kind, symbol, interval=interval, market=market)
    manifest_file = manifest_path(kind, symbol, interval=interval, market=market)
    if not path.exists() or not manifest_file.exists():
        return None, None
    try:
        with np.load(str(path)) as npz:
            arr = npz["data"]
        with open(manifest_file, encoding="utf-8") as fh:
            manifest = json.load(fh)
        return arr, manifest
    except Exception:
        return None, None


def column_index(manifest: Mapping[str, Any] | None, column: str) -> int | None:
    if not manifest:
        return None
    columns = manifest.get("columns")
    if not isinstance(columns, list):
        return None
    try:
        return [str(c) for c in columns].index(column)
    except ValueError:
        return None


def align_cache_column(
    kind: str,
    symbol: str,
    open_times: np.ndarray,
    value_column: str,
    *,
    time_column: str = "timestamp_ms",
    interval: str | None = None,
    market: str = "futures_um",
    mode: str = "ffill",
    default: float = np.nan,
) -> np.ndarray:
    """Align a cached market-data column to kline open times.

    `ffill` uses the latest observation at or before each bar open, suitable for
    funding and open-interest snapshots. `exact` maps identical timestamps and
    fills missing bars with `default`, suitable for pre-aggregated bar data.
    """
    open_times = np.asarray(open_times, dtype=np.int64)
    out = np.full(len(open_times), default, dtype=np.float64)
    arr, manifest = load_cache(kind, symbol, interval=interval, market=market)
    if arr is None or manifest is None or len(open_times) == 0 or arr.size == 0:
        return out

    t_idx = column_index(manifest, time_column)
    v_idx = column_index(manifest, value_column)
    if t_idx is None or v_idx is None:
        return out

    timestamps = arr[:, t_idx].astype(np.int64, copy=False)
    values = arr[:, v_idx].astype(np.float64, copy=False)
    order = np.argsort(timestamps, kind="stable")
    timestamps = timestamps[order]
    values = values[order]

    if mode == "exact":
        loc = np.searchsorted(timestamps, open_times, side="left")
        valid = (loc < len(timestamps)) & (timestamps[np.minimum(loc, len(timestamps) - 1)] == open_times)
        out[valid] = values[loc[valid]]
        return out

    if mode != "ffill":
        raise ValueError(f"unsupported alignment mode: {mode!r}")
    loc = np.searchsorted(timestamps, open_times, side="right") - 1
    valid = loc >= 0
    out[valid] = values[loc[valid]]
    return out
