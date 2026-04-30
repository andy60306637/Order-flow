"""One-shot tick cache rebuild with date-range validation.

Compared with ``utils/tick_cache_worker.py --full-rebuild``, this script:

1. Validates the expected daily zip files exist for a target date range.
2. Scans zip metadata first to estimate the required output size.
3. Writes all parsed ticks into a disk-backed memmap once.
4. Saves the final NPZ cache and manifest only once.

This avoids rewriting the whole NPZ after every daily zip.
"""
from __future__ import annotations

import argparse
import gc
import json
import logging
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.data_paths import set_data_root_override, tick_cache_dir
from core.tick_cache import _parse_agg_trades_csv_lines, cache_path, save_raw

try:
    import pandas as pd

    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False


@dataclass
class ZipMeta:
    path: Path
    day: date
    rows: int
    start_ms: int
    end_ms: int
    mtime: float


def _setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger(__name__)


def _manifest_path(symbol: str) -> Path:
    path = tick_cache_dir() / f"{symbol.upper()}_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _save_manifest(symbol: str, processed: dict[str, dict]) -> None:
    payload = {
        "processed": processed,
        "last_scan": datetime.now(tz=timezone.utc).isoformat(),
    }
    with open(_manifest_path(symbol), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)


def _zip_day(path: Path, symbol: str) -> date:
    stem = path.name.removesuffix(".zip")
    prefix = f"{symbol.upper()}-aggTrades-"
    if not stem.startswith(prefix):
        raise ValueError(f"unexpected filename: {path.name}")
    return date.fromisoformat(stem[len(prefix):])


def _iter_expected_days(from_day: date, to_day: date) -> list[date]:
    days: list[date] = []
    cur = from_day
    while cur <= to_day:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def _open_first_csv(path: Path):
    zf = zipfile.ZipFile(path)
    csv_names = sorted(n for n in zf.namelist() if n.lower().endswith(".csv"))
    if not csv_names:
        zf.close()
        raise ValueError(f"zip contains no csv: {path.name}")
    return zf, csv_names[0]


def _scan_zip_meta(path: Path) -> ZipMeta:
    day = _zip_day(path, path.name.split("-aggTrades-")[0])
    zf, csv_name = _open_first_csv(path)
    try:
        with zf.open(csv_name) as fh:
            if _HAS_PANDAS:
                df = pd.read_csv(
                    fh,
                    header=None,
                    usecols=[5],
                    names=["time"],
                    dtype={"time": str},
                    on_bad_lines="skip",
                )
                times = pd.to_numeric(df["time"], errors="coerce").dropna()
                if times.empty:
                    raise ValueError(f"no valid rows in {path.name}")
                rows = int(len(times))
                start_ms = int(times.min())
                end_ms = int(times.max())
            else:
                rows = 0
                start_ms: Optional[int] = None
                end_ms: Optional[int] = None
                for line in fh:
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="replace")
                    parts = line.strip().split(",")
                    if len(parts) < 7 or not parts[0].strip().lstrip("-").isdigit():
                        continue
                    try:
                        t = int(float(parts[5]))
                    except ValueError:
                        continue
                    rows += 1
                    if start_ms is None or t < start_ms:
                        start_ms = t
                    if end_ms is None or t > end_ms:
                        end_ms = t
                if rows == 0 or start_ms is None or end_ms is None:
                    raise ValueError(f"no valid rows in {path.name}")
    finally:
        zf.close()

    return ZipMeta(
        path=path,
        day=day,
        rows=rows,
        start_ms=start_ms,
        end_ms=end_ms,
        mtime=path.stat().st_mtime,
    )


def _parse_zip_full(path: Path) -> np.ndarray:
    zf, csv_name = _open_first_csv(path)
    try:
        with zf.open(csv_name) as fh:
            if _HAS_PANDAS:
                try:
                    df = pd.read_csv(
                        fh,
                        header=None,
                        usecols=[1, 2, 5, 6],
                        names=["price", "qty", "time", "is_bm"],
                        dtype=str,
                        on_bad_lines="skip",
                    )
                    df["price"] = pd.to_numeric(df["price"], errors="coerce")
                    df["qty"] = pd.to_numeric(df["qty"], errors="coerce")
                    df["time"] = pd.to_numeric(df["time"], errors="coerce")
                    mask = df["price"].notna() & df["qty"].notna() & df["time"].notna()
                    df = df[mask]
                    if df.empty:
                        return np.empty((0, 4), dtype=np.float64)
                    arr = np.empty((len(df), 4), dtype=np.float64)
                    arr[:, 0] = df["time"].to_numpy(dtype=np.float64)
                    arr[:, 1] = df["price"].to_numpy(dtype=np.float64)
                    arr[:, 2] = df["qty"].to_numpy(dtype=np.float64)
                    arr[:, 3] = (
                        df["is_bm"].str.strip().str.lower() == "true"
                    ).to_numpy(dtype=np.float64)
                    return arr
                except Exception:
                    pass
            return _parse_agg_trades_csv_lines(fh)
    finally:
        zf.close()


def _dedupe_sorted(arr: np.ndarray) -> np.ndarray:
    if len(arr) <= 1:
        return arr
    diff = np.diff(arr[:, :3], axis=0)
    keep = np.ones(len(arr), dtype=bool)
    keep[1:] = np.any(diff != 0, axis=1)
    return arr[keep]


def _validate_date_coverage(paths: list[Path], symbol: str, from_day: date, to_day: date) -> list[Path]:
    files_by_day = {_zip_day(p, symbol): p for p in paths}
    expected_days = _iter_expected_days(from_day, to_day)
    missing = [d.isoformat() for d in expected_days if d not in files_by_day]

    if missing:
        raise ValueError(f"missing daily zip(s): {missing[:10]}{' ...' if len(missing) > 10 else ''}")

    return [files_by_day[d] for d in expected_days]


def _iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def rebuild(
    symbol: str,
    output_symbol: str,
    tick_dir: Path,
    from_day: date,
    to_day: date,
    scan_workers: int,
    log: logging.Logger,
) -> None:
    all_paths = sorted(tick_dir.glob(f"{symbol.upper()}-aggTrades-*.zip"))
    if not all_paths:
        raise FileNotFoundError(f"no zip files found in {tick_dir}")

    paths = _validate_date_coverage(all_paths, symbol, from_day, to_day)
    log.info("[%s] validating %d daily zip files", symbol, len(paths))

    metas: dict[str, ZipMeta] = {}
    with ThreadPoolExecutor(max_workers=scan_workers) as ex:
        futures = {ex.submit(_scan_zip_meta, p): p for p in paths}
        for idx, fut in enumerate(as_completed(futures), start=1):
            meta = fut.result()
            metas[meta.path.name] = meta
            if idx % 25 == 0 or idx == len(paths):
                log.info("[%s] metadata scan %d/%d", symbol, idx, len(paths))

    ordered_meta = [metas[p.name] for p in paths]
    total_rows_upper = sum(m.rows for m in ordered_meta)
    global_start = min(m.start_ms for m in ordered_meta)
    global_end = max(m.end_ms for m in ordered_meta)
    log.info(
        "[%s] metadata ready | files=%d rows_upper=%d first=%s last=%s",
        symbol,
        len(ordered_meta),
        total_rows_upper,
        ordered_meta[0].path.name,
        ordered_meta[-1].path.name,
    )
    log.info(
        "[%s] raw time span %s -> %s",
        symbol,
        _iso_utc(global_start),
        _iso_utc(global_end),
    )

    cache = cache_path(output_symbol)
    temp_memmap = cache.with_name(f"{cache.stem}_rebuild_tmp.npy")
    temp_memmap.unlink(missing_ok=True)
    writing_npz = cache.with_name(f"{cache.stem}_writing.npz")
    writing_npz.unlink(missing_ok=True)

    mm = np.lib.format.open_memmap(
        str(temp_memmap),
        mode="w+",
        dtype=np.float64,
        shape=(total_rows_upper, 4),
    )

    processed: dict[str, dict] = {}
    write_pos = 0
    last_row: Optional[np.ndarray] = None

    try:
        for idx, meta in enumerate(ordered_meta, start=1):
            arr = _parse_zip_full(meta.path)
            if len(arr) == 0:
                raise ValueError(f"parsed 0 rows from {meta.path.name}")

            order = np.argsort(arr[:, 0], kind="stable")
            arr = arr[order]
            arr = _dedupe_sorted(arr)

            if last_row is not None and len(arr) > 0 and np.array_equal(arr[0, :3], last_row[:3]):
                arr = arr[1:]

            if len(arr) == 0:
                raise ValueError(f"all rows deduped away for {meta.path.name}")

            if last_row is not None and arr[0, 0] < last_row[0]:
                raise ValueError(
                    f"non-monotonic tick time between files: {meta.path.name} starts at {arr[0,0]} < previous {last_row[0]}"
                )

            next_pos = write_pos + len(arr)
            mm[write_pos:next_pos] = arr
            write_pos = next_pos
            last_row = arr[-1].copy()

            processed[meta.path.name] = {
                "mtime": meta.mtime,
                "rows": meta.rows,
            }

            if idx % 10 == 0 or idx == len(ordered_meta):
                log.info(
                    "[%s] rebuild progress %d/%d | written=%d",
                    symbol,
                    idx,
                    len(ordered_meta),
                    write_pos,
                )

        final_view = mm[:write_pos]
        start_ms = int(final_view[0, 0])
        end_ms = int(final_view[-1, 0])
        if not save_raw(output_symbol, final_view, start_ms, end_ms):
            raise RuntimeError("save_raw failed")
        del final_view
    finally:
        del mm
        gc.collect()
        try:
            temp_memmap.unlink(missing_ok=True)
        except PermissionError:
            log.warning("[%s -> %s] temp memmap still locked, leaving %s", symbol, output_symbol, temp_memmap.name)

    _save_manifest(output_symbol, processed)
    log.info(
        "[%s -> %s] rebuild complete | ticks=%d span=%s -> %s",
        symbol,
        output_symbol,
        write_pos,
        _iso_utc(start_ms),
        _iso_utc(end_ms),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="One-shot aggTrades zip -> tick cache rebuild with validation.")
    ap.add_argument("--symbol", required=True, help="e.g. BTCUSDT")
    ap.add_argument("--output-symbol", default=None, help="cache/manifest symbol to write; defaults to --symbol")
    ap.add_argument("--tick-dir", required=True, help="directory containing daily aggTrades zip files")
    ap.add_argument("--from-date", required=True, help="inclusive start date, YYYY-MM-DD")
    ap.add_argument("--to-date", required=True, help="inclusive end date, YYYY-MM-DD")
    ap.add_argument("--scan-workers", type=int, default=4, help="parallel workers for metadata scan")
    ap.add_argument(
        "--data-root",
        default=None,
        help="override ORDERFLOW_DATA_ROOT for cache reads/writes in this process",
    )
    args = ap.parse_args()
    set_data_root_override(args.data_root)

    log = _setup_logging()
    rebuild(
        symbol=args.symbol.upper(),
        output_symbol=(args.output_symbol or args.symbol).upper(),
        tick_dir=Path(args.tick_dir),
        from_day=date.fromisoformat(args.from_date),
        to_day=date.fromisoformat(args.to_date),
        scan_workers=max(1, args.scan_workers),
        log=log,
    )


if __name__ == "__main__":
    main()
