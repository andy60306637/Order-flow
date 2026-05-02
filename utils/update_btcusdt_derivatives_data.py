"""Download and cache BTCUSDT derivatives datasets from Binance Vision.

Raw zip archives are kept under project-local tick_data/binance/..., while
normalized numeric caches are written to the active OrderFlow data root.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import data_paths, market_data_cache, tick_cache

BASE_URL = "https://data.binance.vision/data/futures/um"
SYMBOL = "BTCUSDT"

FUNDING_COLUMNS = ["timestamp_ms", "funding_interval_hours", "last_funding_rate"]
METRICS_COLUMNS = [
    "timestamp_ms",
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
]
LIQUIDATION_COLUMNS = [
    "timestamp_ms",
    "long_liq_qty",
    "short_liq_qty",
    "long_liq_notional",
    "short_liq_notional",
    "event_count",
]


def _date_range(start_ms: int, end_ms: int) -> list[datetime]:
    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).date()
    end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).date()
    out = []
    cur = start
    while cur <= end:
        out.append(datetime(cur.year, cur.month, cur.day, tzinfo=timezone.utc))
        cur += timedelta(days=1)
    return out


def _month_range(start_ms: int, end_ms: int) -> list[datetime]:
    cur = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    out = []
    while cur <= end:
        out.append(cur)
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    return out


def _download(url: str, out_path: Path, *, force: bool = False) -> bool:
    if out_path.exists() and out_path.stat().st_size > 0 and not force:
        return True
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            tmp.write_bytes(response.read())
        tmp.replace(out_path)
        return True
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            print(f"WARN download failed {url}: HTTP {exc.code}")
        tmp.unlink(missing_ok=True)
        return False
    except Exception as exc:
        print(f"WARN download failed {url}: {exc}")
        tmp.unlink(missing_ok=True)
        return False


def _read_zip_csv_rows(path: Path) -> Iterable[dict[str, str]]:
    with zipfile.ZipFile(path) as zf:
        csv_names = [name for name in zf.namelist() if name.lower().endswith(".csv")]
        if not csv_names:
            return []
        with zf.open(csv_names[0]) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8", newline="")
            yield from csv.DictReader(text)


def _to_float(value: object, default: float = np.nan) -> float:
    try:
        text = str(value).strip()
        return float(text) if text else default
    except (TypeError, ValueError):
        return default


def _parse_utc_ms(text: str) -> int:
    dt = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _filter_time(rows: list[list[float]], start_ms: int, end_ms: int) -> list[list[float]]:
    return [row for row in rows if start_ms <= int(row[0]) <= end_ms]


def _load_json(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _tick_range_from_manifests(symbol: str) -> tuple[int, int]:
    tick_dir = data_paths.tick_cache_dir()
    starts: list[int] = []
    ends: list[int] = []
    for path in sorted(tick_dir.glob(f"{symbol}*_shards.json")):
        manifest = _load_json(path)
        if not manifest:
            continue
        start_ms = manifest.get("start_ms")
        end_ms = manifest.get("end_ms")
        if start_ms is not None and end_ms is not None:
            starts.append(int(start_ms))
            ends.append(int(end_ms))
    if starts and ends:
        return min(starts), max(ends)

    meta = tick_cache.load_meta(symbol)
    if meta is None:
        raise RuntimeError(f"missing tick cache metadata for {symbol}")
    return int(meta["start_ms"]), int(meta["end_ms"])


def _download_funding(symbol: str, start_ms: int, end_ms: int, raw_root: Path, force: bool) -> tuple[list[Path], list[str]]:
    raw_dir = raw_root / "monthly" / "fundingRate" / symbol
    paths: list[Path] = []
    missing: list[str] = []
    for month in _month_range(start_ms, end_ms):
        name = f"{symbol}-fundingRate-{month:%Y-%m}.zip"
        url = f"{BASE_URL}/monthly/fundingRate/{symbol}/{name}"
        out = raw_dir / name
        if _download(url, out, force=force):
            paths.append(out)
        else:
            missing.append(name)
    return paths, missing


def _download_daily(kind: str, symbol: str, start_ms: int, end_ms: int, raw_root: Path, force: bool) -> tuple[list[Path], list[str]]:
    raw_dir = raw_root / "daily" / kind / symbol
    paths: list[Path] = []
    missing: list[str] = []
    for day in _date_range(start_ms, end_ms):
        name = f"{symbol}-{kind}-{day:%Y-%m-%d}.zip"
        url = f"{BASE_URL}/daily/{kind}/{symbol}/{name}"
        out = raw_dir / name
        if _download(url, out, force=force):
            paths.append(out)
        else:
            missing.append(name)
    return paths, missing


def _build_funding_cache(paths: list[Path], start_ms: int, end_ms: int, missing: list[str]) -> dict:
    rows: list[list[float]] = []
    for path in paths:
        for row in _read_zip_csv_rows(path):
            rows.append([
                _to_float(row.get("calc_time")),
                _to_float(row.get("funding_interval_hours")),
                _to_float(row.get("last_funding_rate")),
            ])
    rows = _filter_time(rows, start_ms, end_ms)
    arr = np.array(rows, dtype=np.float64) if rows else np.empty((0, len(FUNDING_COLUMNS)), dtype=np.float64)
    return market_data_cache.save_cache(
        "fundingRate",
        SYMBOL,
        arr,
        columns=FUNDING_COLUMNS,
        source_files=paths,
        time_column="timestamp_ms",
        metadata={"source_root": "tick_data/binance/futures/um", "missing_source_files": missing},
    )


def _build_metrics_cache(paths: list[Path], start_ms: int, end_ms: int, missing: list[str]) -> dict:
    rows: list[list[float]] = []
    for path in paths:
        for row in _read_zip_csv_rows(path):
            try:
                ts = _parse_utc_ms(row.get("create_time", ""))
            except ValueError:
                continue
            rows.append([
                float(ts),
                _to_float(row.get("sum_open_interest")),
                _to_float(row.get("sum_open_interest_value")),
                _to_float(row.get("count_toptrader_long_short_ratio")),
                _to_float(row.get("sum_toptrader_long_short_ratio")),
                _to_float(row.get("count_long_short_ratio")),
                _to_float(row.get("sum_taker_long_short_vol_ratio")),
            ])
    rows = _filter_time(rows, start_ms, end_ms)
    arr = np.array(rows, dtype=np.float64) if rows else np.empty((0, len(METRICS_COLUMNS)), dtype=np.float64)
    return market_data_cache.save_cache(
        "metrics",
        SYMBOL,
        arr,
        columns=METRICS_COLUMNS,
        source_files=paths,
        time_column="timestamp_ms",
        metadata={"source_root": "tick_data/binance/futures/um", "missing_source_files": missing},
    )


def _first_present(row: dict[str, str], names: tuple[str, ...]) -> str | None:
    lower_map = {k.lower(): v for k, v in row.items()}
    for name in names:
        if name.lower() in lower_map:
            return lower_map[name.lower()]
    return None


def _build_liquidation_cache(paths: list[Path], start_ms: int, end_ms: int, missing: list[str]) -> dict:
    buckets: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0.0])
    for path in paths:
        for row in _read_zip_csv_rows(path):
            raw_ts = _first_present(row, ("time", "trade_time", "updated_time", "T", "transact_time"))
            raw_side = _first_present(row, ("side", "S"))
            raw_qty = _first_present(row, ("original_quantity", "orig_qty", "origQty", "last_fill_quantity", "executedQty", "q"))
            raw_price = _first_present(row, ("average_price", "avg_price", "price", "ap", "p"))
            if raw_ts is None or raw_side is None:
                continue
            ts = int(_to_float(raw_ts, -1))
            if ts < start_ms or ts > end_ms:
                continue
            qty = _to_float(raw_qty, 0.0)
            price = _to_float(raw_price, 0.0)
            minute = ts // 60_000 * 60_000
            bucket = buckets[minute]
            side = raw_side.strip().upper()
            if side == "SELL":
                bucket[0] += qty
                bucket[2] += qty * price
            elif side == "BUY":
                bucket[1] += qty
                bucket[3] += qty * price
            bucket[4] += 1.0

    rows = [[float(ts), *values] for ts, values in sorted(buckets.items())]
    arr = np.array(rows, dtype=np.float64) if rows else np.empty((0, len(LIQUIDATION_COLUMNS)), dtype=np.float64)
    return market_data_cache.save_cache(
        "liquidationSnapshot",
        SYMBOL,
        arr,
        columns=LIQUIDATION_COLUMNS,
        source_files=paths,
        time_column="timestamp_ms",
        metadata={
            "source_root": "tick_data/binance/futures/um",
            "missing_source_files": missing,
            "availability_note": (
                "Binance Vision USDT-M liquidationSnapshot is unavailable for this BTCUSDT range "
                "when no source files were downloaded."
            ) if not paths else "",
        },
    )


def update(symbol: str, force: bool = False, try_liquidation_download: bool = False) -> dict:
    symbol = symbol.upper()
    if symbol != SYMBOL:
        raise ValueError("this updater is intentionally scoped to BTCUSDT")
    data_paths.ensure_data_root_layout()
    start_ms, end_ms = _tick_range_from_manifests(symbol)
    raw_root = data_paths.project_root() / "tick_data" / "binance" / "futures" / "um"

    funding_paths, funding_missing = _download_funding(symbol, start_ms, end_ms, raw_root, force)
    metrics_paths, metrics_missing = _download_daily("metrics", symbol, start_ms, end_ms, raw_root, force)
    if try_liquidation_download:
        liquidation_paths, liquidation_missing = _download_daily(
            "liquidationSnapshot", symbol, start_ms, end_ms, raw_root, force
        )
    else:
        liquidation_paths = []
        liquidation_missing = ["data/futures/um/daily/liquidationSnapshot/ is not listed on Binance Vision"]

    payload = {
        "symbol": symbol,
        "tick_range": {"start_ms": start_ms, "end_ms": end_ms},
        "fundingRate": _build_funding_cache(funding_paths, start_ms, end_ms, funding_missing),
        "metrics": _build_metrics_cache(metrics_paths, start_ms, end_ms, metrics_missing),
        "liquidationSnapshot": _build_liquidation_cache(
            liquidation_paths, start_ms, end_ms, liquidation_missing
        ),
    }
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Update BTCUSDT funding, OI metrics, and liquidation caches.")
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--force", action="store_true", help="redownload existing zip files")
    parser.add_argument(
        "--try-liquidation-download",
        action="store_true",
        help="probe daily liquidationSnapshot files even though Binance Vision does not list this USDT-M dataset",
    )
    parser.add_argument("--json", action="store_true", help="print full manifest payload as JSON")
    args = parser.parse_args()

    payload = update(args.symbol, force=args.force, try_liquidation_download=args.try_liquidation_download)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    for kind in ("fundingRate", "metrics", "liquidationSnapshot"):
        item = payload[kind]
        print(
            f"{kind}: rows={item['row_count']} "
            f"range={item.get('start_ms')}->{item.get('end_ms')} "
            f"missing={len(item.get('missing_source_files', []))}"
        )


if __name__ == "__main__":
    main()
