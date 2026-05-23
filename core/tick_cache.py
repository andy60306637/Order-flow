"""本機 aggTrades 快取。

路徑：<project_root>/data/ticks/{SYMBOL}_ticks.npz
（Tick 資料與 K 棒 interval 無關，快取以 symbol 命名，可跨 interval 共用）

儲存格式為 NumPy compressed .npz，內含:
  - "data": shape (N, 4) float64 陣列
      col 0: trade_time (ms)
      col 1: price
      col 2: qty
      col 3: is_buyer_maker (0.0 / 1.0)
  - "meta": shape (2,) — [start_time_ms, end_time_ms] 已下載的時間範圍

每筆 aggTrades 約 32 bytes，30天 ≈ 10-30M 筆 ≈ 300-900 MB（壓縮後約 100-300 MB）。
"""
from __future__ import annotations

import json
import logging
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from core import data_paths

logger = logging.getLogger(__name__)

_CACHE_DIR: Path | None = None
_SHARD_ROOT: Path | None = None
_NCOLS        = 4  # trade_time, price, qty, is_buyer_maker


def _cache_dir() -> Path:
    return _CACHE_DIR if _CACHE_DIR is not None else data_paths.tick_cache_dir()


def _shard_root() -> Path:
    return _SHARD_ROOT if _SHARD_ROOT is not None else _cache_dir() / "shards"


class TickSliceAccessor(Mapping[int, np.ndarray]):
    """以 index range 延後 materialize 每根 bar 的 tick slice。"""

    def __init__(self, ticks: np.ndarray, bar_ranges: dict[int, tuple[int, int]]):
        self._ticks = ticks
        self._bar_ranges = bar_ranges

    def __getitem__(self, open_time: int) -> np.ndarray:
        bar_range = self._bar_ranges.get(open_time)
        if bar_range is None:
            raise KeyError(open_time)
        lo, hi = bar_range
        return self._ticks[lo:hi]

    def __iter__(self) -> Iterator[int]:
        return iter(self._bar_ranges)

    def __len__(self) -> int:
        return len(self._bar_ranges)

    def __contains__(self, open_time: object) -> bool:
        return open_time in self._bar_ranges

    def get(self, open_time: int, default=None) -> np.ndarray | None:
        bar_range = self._bar_ranges.get(open_time)
        if bar_range is None:
            return default
        lo, hi = bar_range
        return self._ticks[lo:hi]

    def range_for(self, open_time: int) -> tuple[int, int] | None:
        return self._bar_ranges.get(open_time)

    @property
    def ticks(self) -> np.ndarray:
        return self._ticks

    @property
    def bar_ranges(self) -> dict[int, tuple[int, int]]:
        return self._bar_ranges


class LazyCombinedTickBarMap(Mapping[int, np.ndarray]):
    """Lazy bar->ticks map across multiple symbols.

    The map only materializes ticks for the requested bar window [open_time, close_time].
    This avoids loading and concatenating the full multi-month dataset in memory.
    """

    def __init__(
        self,
        symbols: list[str],
        kline_times: list[tuple[int, int]],
        *,
        bar_cache_size: int = 64,
        shard_cache_size_per_symbol: int = 2,
    ) -> None:
        uniq_symbols = list(dict.fromkeys(s.upper() for s in symbols if s))
        self._symbols = uniq_symbols
        self._open_to_close: dict[int, int] = {
            int(ot): int(ct) for ot, ct in kline_times
        }
        self._bar_cache_size = max(1, int(bar_cache_size))
        self._shard_cache_size_per_symbol = max(1, int(shard_cache_size_per_symbol))
        self._bar_cache: OrderedDict[int, np.ndarray] = OrderedDict()
        self._empty = np.empty((0, _NCOLS), dtype=np.float64)

        # coverage stats collected while strategy is reading bars
        self._queried_open_times: set[int] = set()
        self._non_empty_open_times: set[int] = set()

        self._symbol_states: dict[str, dict] = {}
        for sym in self._symbols:
            manifest = _load_shard_manifest(sym)
            months = manifest.get("months", {}) if manifest is not None else None
            self._symbol_states[sym] = {
                "months": months if isinstance(months, dict) else None,
                "shard_cache": OrderedDict(),
            }

    def __getitem__(self, open_time: int) -> np.ndarray:
        arr = self.get(open_time)
        if arr is None:
            raise KeyError(open_time)
        return arr

    def __iter__(self) -> Iterator[int]:
        return iter(self._open_to_close)

    def __len__(self) -> int:
        # Keep non-zero length so strategy tick-mode gates remain enabled.
        return len(self._open_to_close)

    def get(self, open_time: int, default=None) -> np.ndarray | None:
        ot = int(open_time)
        ct = self._open_to_close.get(ot)
        if ct is None:
            return default

        cached = self._bar_cache.get(ot)
        if cached is not None:
            self._bar_cache.move_to_end(ot)
            return cached

        self._queried_open_times.add(ot)
        parts: list[np.ndarray] = []
        for sym in self._symbols:
            seg = self._load_symbol_bar_ticks(sym, ot, ct)
            if len(seg) > 0:
                parts.append(seg)

        if not parts:
            out = self._empty
        elif len(parts) == 1:
            out = parts[0]
        else:
            out = np.concatenate(parts, axis=0)
            if len(out) > 1:
                order = np.argsort(out[:, 0], kind="stable")
                out = out[order]

        if len(out) > 0:
            self._non_empty_open_times.add(ot)

        self._bar_cache[ot] = out
        while len(self._bar_cache) > self._bar_cache_size:
            self._bar_cache.popitem(last=False)
        return out

    def observed_coverage(self) -> tuple[int, int]:
        """Returns (bars_with_ticks, total_bars)."""
        return len(self._non_empty_open_times), len(self._open_to_close)

    def observed_query_count(self) -> int:
        return len(self._queried_open_times)

    def _load_symbol_bar_ticks(self, symbol: str, start_ms: int, end_ms: int) -> np.ndarray:
        st = self._symbol_states[symbol]
        month_entries = st.get("months")
        if not month_entries:
            return load_range(symbol, start_ms, end_ms)

        parts: list[np.ndarray] = []
        for month_key in _iter_month_keys(start_ms, end_ms):
            entry = month_entries.get(month_key)
            if entry is None:
                return load_range(symbol, start_ms, end_ms)

            path = _shard_entry_path(entry)
            if not path.exists():
                return load_range(symbol, start_ms, end_ms)

            shard = self._load_shard_cached(symbol, month_key, path)
            times = shard[:, 0]
            lo = int(np.searchsorted(times, start_ms, side="left"))
            hi = int(np.searchsorted(times, end_ms, side="right"))
            if lo < hi:
                parts.append(np.array(shard[lo:hi], copy=True))

        if not parts:
            return self._empty
        if len(parts) == 1:
            return parts[0]
        return np.concatenate(parts, axis=0)

    def _load_shard_cached(self, symbol: str, month_key: str, path: Path) -> np.ndarray:
        st = self._symbol_states[symbol]
        cache: OrderedDict[str, np.ndarray] = st["shard_cache"]
        shard = cache.get(month_key)
        if shard is not None:
            cache.move_to_end(month_key)
            return shard

        shard = np.load(str(path), mmap_mode="r")
        cache[month_key] = shard
        while len(cache) > self._shard_cache_size_per_symbol:
            cache.popitem(last=False)
        return shard


def cache_path(symbol: str) -> Path:
    """回傳快取路徑。Tick 資料與 K 棒 interval 無關，僅以 symbol 命名。"""
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{symbol.upper()}_ticks.npz"


def shard_dir(symbol: str) -> Path:
    path = _shard_root() / symbol.upper()
    path.mkdir(parents=True, exist_ok=True)
    return path


def shard_manifest_path(symbol: str) -> Path:
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{symbol.upper()}_shards.json"


def shard_path(symbol: str, month_key: str) -> Path:
    return shard_dir(symbol) / f"{symbol.upper()}_{month_key}.npy"


def _month_key_from_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y%m")


def _month_start_ms(month_key: str) -> int:
    cur = datetime.strptime(month_key, "%Y%m").replace(tzinfo=timezone.utc)
    return int(cur.timestamp() * 1000)


def _iter_month_keys(start_ms: int, end_ms: int) -> list[str]:
    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    keys: list[str] = []
    cur = start
    while cur <= end:
        keys.append(cur.strftime("%Y%m"))
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    return keys


def _next_month_start_ms(month_key: str) -> int:
    cur = datetime.strptime(month_key, "%Y%m").replace(tzinfo=timezone.utc)
    if cur.month == 12:
        nxt = cur.replace(year=cur.year + 1, month=1)
    else:
        nxt = cur.replace(month=cur.month + 1)
    return int(nxt.timestamp() * 1000)


def _load_shard_manifest(symbol: str) -> dict | None:
    path = shard_manifest_path(symbol)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logger.error("tick shard manifest load error [%s]: %s", path.name, exc)
        return None


def _shard_entry_path(entry: dict) -> Path:
    """Convert a manifest entry's path to a platform-appropriate Path.

    Manifests written on Windows use backslash separators; normalise to
    forward slashes so they resolve correctly on Linux/macOS.
    """
    return _cache_dir() / Path(entry["path"].replace("\\", "/"))


def _load_npz_meta(symbol: str) -> dict | None:
    path = cache_path(symbol)
    if not path.exists():
        return None
    try:
        with np.load(str(path)) as npz:
            meta_arr = npz["meta"]
        return {"start_ms": int(meta_arr[0]), "end_ms": int(meta_arr[1]), "source": "legacy_npz"}
    except Exception as exc:
        logger.error("tick_cache meta load error [%s]: %s", path.name, exc)
        return None


def load_meta(symbol: str) -> dict | None:
    """讀取資料時間範圍 metadata；優先 shard manifest，再回退舊 NPZ。"""
    manifest = _load_shard_manifest(symbol)
    if manifest is not None:
        return {
            "start_ms": int(manifest["start_ms"]),
            "end_ms": int(manifest["end_ms"]),
            "source": "shards",
        }
    return _load_npz_meta(symbol)


def load_raw(symbol: str) -> tuple[np.ndarray | None, dict | None]:
    """讀取原始快取。回傳 (data_array, meta_dict) 或 (None, None)。"""
    path = cache_path(symbol)
    if not path.exists():
        return None, None
    try:
        npz = np.load(str(path))
        data = npz["data"]
        meta_arr = npz["meta"]
        meta = {"start_ms": int(meta_arr[0]), "end_ms": int(meta_arr[1])}
        return data, meta
    except Exception as exc:
        logger.error("tick_cache load error [%s]: %s", path.name, exc)
        return None, None


def save_shards(symbol: str, data: np.ndarray, overwrite: bool = False) -> dict:
    if _CACHE_DIR is None:
        data_paths.ensure_data_root_layout()
    """將完整 tick array 寫成按月份分片的 NPY shards。"""
    symbol = symbol.upper()
    if len(data) == 0:
        raise ValueError("cannot shard empty tick data")

    order = np.argsort(data[:, 0], kind="stable")
    sorted_data = data[order]
    times = sorted_data[:, 0]
    month_keys = _iter_month_keys(int(times[0]), int(times[-1]))
    months: dict[str, dict] = {}
    for month_key in month_keys:
        lo = int(np.searchsorted(times, _month_start_ms(month_key), side="left"))
        hi = int(np.searchsorted(times, _next_month_start_ms(month_key), side="left"))
        if lo >= hi:
            continue
        part = sorted_data[lo:hi]
        path = shard_path(symbol, str(month_key))
        if path.exists() and not overwrite:
            raise FileExistsError(f"shard already exists: {path}")
        np.save(str(path), part, allow_pickle=False)
        months[str(month_key)] = {
            "path": str(path.relative_to(_cache_dir())),
            "count": int(len(part)),
            "start_ms": int(part[0, 0]),
            "end_ms": int(part[-1, 0]),
        }

    manifest = {
        "symbol": symbol,
        "format": "tick_shards_v1",
        "start_ms": int(sorted_data[0, 0]),
        "end_ms": int(sorted_data[-1, 0]),
        "months": months,
    }
    with open(shard_manifest_path(symbol), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    return manifest


def save_raw(symbol: str, data: np.ndarray,
             start_ms: int, end_ms: int) -> bool:
    if _CACHE_DIR is None:
        data_paths.ensure_data_root_layout()
    """全量寫入快取（原子寫入：先寫 .tmp 再 rename，避免讀寫競爭造成損毀）。"""
    path = cache_path(symbol)
    # np.savez_compressed 只在路徑不含 .npz 時才補後綴，
    # 因此 tmp 必須已含 .npz，否則 rename 會找不到檔案。
    tmp  = path.with_name(path.stem + "_writing.npz")
    try:
        meta = np.array([start_ms, end_ms], dtype=np.float64)
        np.savez_compressed(str(tmp), data=data, meta=meta)
        tmp.replace(path)          # 原子 rename：讀端永遠看到完整檔案
        size_mb = path.stat().st_size / 1_048_576
        logger.info(
            "tick_cache saved %d ticks → %s (%.1f MB)",
            len(data), path.name, size_mb,
        )
        return True
    except Exception as exc:
        logger.error("tick_cache save error [%s]: %s", path.name, exc)
        tmp.unlink(missing_ok=True)
        return False


def from_api_list(trades: list[dict]) -> np.ndarray:
    """將 Binance aggTrades API JSON list 轉為 ndarray (N, 4)。"""
    if not trades:
        return np.empty((0, _NCOLS), dtype=np.float64)
    arr = np.empty((len(trades), _NCOLS), dtype=np.float64)
    for idx, t in enumerate(trades):
        arr[idx, 0] = float(t["T"])           # trade_time
        arr[idx, 1] = float(t["p"])           # price
        arr[idx, 2] = float(t["q"])           # qty
        arr[idx, 3] = 1.0 if t["m"] else 0.0  # is_buyer_maker
    return arr


# ─────────────────────────────────────────────────────────────────────────────
# 使用者自備 CSV / ZIP 匯入（data.binance.vision 格式）
# ─────────────────────────────────────────────────────────────────────────────

def _parse_agg_trades_csv_lines(lines) -> np.ndarray:
    """從 Binance aggTrades CSV 行（str 或 bytes）解析 ndarray(N, 4)。

    data.binance.vision 欄位順序（通常無 header，或 header 第 0 欄非數字會自動跳過）：
      0  agg_trade_id
      1  price
      2  quantity
      3  first_trade_id
      4  last_trade_id
      5  transact_time  (ms unix timestamp)
      6  is_buyer_maker (True / False)
    """
    rows = []
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 7:
            continue
        # 若第一欄非純數字，視為 header 跳過
        if not parts[0].strip().lstrip("-").isdigit():
            continue
        try:
            trade_time = float(parts[5])
            price      = float(parts[1])
            qty        = float(parts[2])
            is_bm      = 1.0 if parts[6].strip().lower() == "true" else 0.0
            rows.append((trade_time, price, qty, is_bm))
        except (IndexError, ValueError):
            continue
    if not rows:
        return np.empty((0, _NCOLS), dtype=np.float64)
    return np.array(rows, dtype=np.float64)


def from_csv_file(path) -> np.ndarray:
    """解析 data.binance.vision aggTrades CSV 檔案。回傳 ndarray(N, 4)。"""
    with open(Path(path), "r", encoding="utf-8", newline="") as fh:
        return _parse_agg_trades_csv_lines(fh)


def from_zip_file(path) -> np.ndarray:
    """解析 data.binance.vision aggTrades ZIP 檔案（內含單一 CSV）。回傳 ndarray(N, 4)。"""
    import zipfile
    with zipfile.ZipFile(Path(path)) as zf:
        csv_names = sorted(n for n in zf.namelist() if n.lower().endswith(".csv"))
        if not csv_names:
            return np.empty((0, _NCOLS), dtype=np.float64)
        with zf.open(csv_names[0]) as f:
            return _parse_agg_trades_csv_lines(f)


def merge_and_save_array(symbol: str,
                          new_arr: np.ndarray,
                          start_ms: int, end_ms: int) -> int:
    """將 ndarray(N, 4) 合併進既有快取並儲存，回傳合併後總筆數。"""
    if len(new_arr) == 0:
        existing, _ = load_raw(symbol)
        return len(existing) if existing is not None else 0

    existing, meta = load_raw(symbol)
    if existing is not None and len(existing) > 0:
        combined = np.concatenate([existing, new_arr], axis=0)
    else:
        combined = new_arr.copy()

    # 依 trade_time 排序
    order = np.argsort(combined[:, 0], kind="stable")
    combined = combined[order]

    # 去重：trade_time + price + qty 三欄相同即為重複
    if len(combined) > 1:
        diff = np.diff(combined[:, :3], axis=0)
        keep = np.ones(len(combined), dtype=bool)
        keep[1:] = np.any(diff != 0, axis=1)
        combined = combined[keep]

    s_ms = int(combined[:, 0].min())
    e_ms = int(combined[:, 0].max())
    if meta:
        s_ms = min(s_ms, meta["start_ms"])
        e_ms = max(e_ms, meta["end_ms"])

    save_raw(symbol, combined, s_ms, e_ms)
    return len(combined)


def merge_and_save(symbol: str,
                   new_trades: list[dict],
                   new_start_ms: int, new_end_ms: int) -> int:
    """合併新資料與既有快取，依 trade_time 排序去重，儲存並回傳總筆數。"""
    new_arr = from_api_list(new_trades)
    existing, meta = load_raw(symbol)

    if existing is not None and len(existing) > 0:
        combined = np.concatenate([existing, new_arr], axis=0)
        # 去重：以 trade_time 排序後，移除連續重複時間+價格+數量
        order = np.argsort(combined[:, 0])
        combined = combined[order]
        # 用相鄰行差異去重（trade_time + price + qty 三欄唯一）
        if len(combined) > 1:
            diff = np.diff(combined[:, :3], axis=0)
            keep = np.ones(len(combined), dtype=bool)
            keep[1:] = np.any(diff != 0, axis=1)
            combined = combined[keep]
        # 擴展時間範圍
        start_ms = min(meta["start_ms"], new_start_ms) if meta else new_start_ms
        end_ms   = max(meta["end_ms"], new_end_ms)     if meta else new_end_ms
    else:
        combined = new_arr
        if len(combined) > 0:
            order = np.argsort(combined[:, 0])
            combined = combined[order]
        start_ms = new_start_ms
        end_ms   = new_end_ms

    save_raw(symbol, combined, start_ms, end_ms)
    return len(combined)


def load_range(symbol: str,
               start_ms: int, end_ms: int) -> np.ndarray:
    """載入指定時間範圍內的 ticks（含兩端）。"""
    shard_data = load_range_sharded(symbol, start_ms, end_ms)
    if shard_data is not None:
        return shard_data

    data, meta = load_raw(symbol)
    if data is None or len(data) == 0:
        return np.empty((0, _NCOLS), dtype=np.float64)
    mask = (data[:, 0] >= start_ms) & (data[:, 0] <= end_ms)
    return data[mask]


def load_range_sharded(symbol: str, start_ms: int, end_ms: int) -> np.ndarray | None:
    """優先從月分片讀取指定時間範圍；若 shards 不完整則回傳 None。"""
    manifest = _load_shard_manifest(symbol)
    if manifest is None:
        return None

    month_entries: dict[str, dict] = manifest.get("months", {})
    month_keys = _iter_month_keys(start_ms, end_ms)
    if not month_keys:
        return np.empty((0, _NCOLS), dtype=np.float64)

    parts: list[np.ndarray] = []
    for month_key in month_keys:
        entry = month_entries.get(month_key)
        if entry is None:
            return None

        path = _shard_entry_path(entry)
        if not path.exists():
            return None

        shard = np.load(str(path), mmap_mode="r")
        times = shard[:, 0]
        lo = int(np.searchsorted(times, start_ms, side="left"))
        hi = int(np.searchsorted(times, end_ms, side="right"))
        if lo < hi:
            parts.append(np.array(shard[lo:hi], copy=True))

    if not parts:
        return np.empty((0, _NCOLS), dtype=np.float64)
    if len(parts) == 1:
        return parts[0]
    return np.concatenate(parts, axis=0)


def build_bar_map(ticks: np.ndarray,
                  kline_times: list[tuple[int, int]]) -> dict[int, np.ndarray]:
    """將 ticks 分群到對應的 K 棒區間。

    使用 np.searchsorted 達到 O(K log N) 而非舊版的 O(N×K)。
    ticks 必須已按 trade_time (col 0) 升序排列。

    Args:
        ticks:       shape (N, 4) 已排序的 tick 陣列
        kline_times: list of (open_time_ms, close_time_ms)

    Returns:
        dict: open_time_ms → ticks within [open_time, close_time]
    """
    bar_ranges = build_bar_ranges(ticks, kline_times)
    return {ot: ticks[lo:hi] for ot, (lo, hi) in bar_ranges.items()}


def build_bar_map_streaming(
    symbol: str,
    start_ms: int,
    end_ms: int,
    kline_times: list[tuple[int, int]],
) -> dict[int, np.ndarray] | None:
    """Memory-efficient bar map: processes monthly shards one at a time via mmap.

    Unlike load_range_sharded + build_bar_map, this never allocates a single
    contiguous array for the full time range. Each shard is mmap-opened (backed
    by disk), sliced into bar buckets, and the reference is kept alive only
    through the returned dict values. Peak RAM is bounded by the OS page-cache
    rather than total tick count (avoids OOM for 1-year datasets).

    Returns None if shards are incomplete or unavailable — caller should fall
    back to the legacy load_range_sharded path.
    """
    manifest = _load_shard_manifest(symbol)
    if manifest is None:
        return None

    month_entries: dict[str, dict] = manifest.get("months", {})
    month_keys = _iter_month_keys(start_ms, end_ms)
    if not month_keys:
        return {}

    merged: dict[int, list[np.ndarray]] = {}
    for month_key in month_keys:
        entry = month_entries.get(month_key)
        if entry is None:
            return None  # incomplete shards — signal caller to fall back
        path = _shard_entry_path(entry)
        if not path.exists():
            return None

        # mmap_mode="r": backed by disk, no RAM copy unless accessed.
        # The view shard[lo:hi] holds a reference to the mmap so the file
        # handle stays open as long as bar_map dict entries referencing it live.
        shard: np.ndarray = np.load(str(path), mmap_mode="r")
        times = shard[:, 0]
        lo = int(np.searchsorted(times, start_ms, side="left"))
        hi = int(np.searchsorted(times, end_ms,   side="right"))
        if lo >= hi:
            continue
        chunk = shard[lo:hi]  # mmap view — zero allocation
        for ot, arr in build_bar_map(chunk, kline_times).items():
            merged.setdefault(ot, []).append(arr)

    # Bars spanning a month boundary (≤1 per boundary) need their chunks merged.
    return {
        ot: vs[0] if len(vs) == 1 else np.concatenate(vs, axis=0)
        for ot, vs in merged.items()
    }


def build_bar_ranges(
    ticks: np.ndarray,
    kline_times: list[tuple[int, int]],
) -> dict[int, tuple[int, int]]:
    """將 ticks 對應到每根 K 棒的 index range。"""
    if len(ticks) == 0:
        return {}
    result: dict[int, tuple[int, int]] = {}
    times = ticks[:, 0]
    for ot, ct in kline_times:
        lo = int(np.searchsorted(times, ot,     side="left"))
        hi = int(np.searchsorted(times, ct + 1, side="left"))
        if lo < hi:
            result[ot] = (lo, hi)
    return result


def build_tick_slice_accessor(
    ticks: np.ndarray,
    kline_times: list[tuple[int, int]],
) -> TickSliceAccessor:
    """建立 dict-like lazy accessor，保留舊策略的 get/contains/len 用法。"""
    return TickSliceAccessor(ticks, build_bar_ranges(ticks, kline_times))


def build_lazy_bar_map(
    symbols: list[str],
    kline_times: list[tuple[int, int]],
    *,
    bar_cache_size: int = 64,
    shard_cache_size_per_symbol: int = 2,
) -> LazyCombinedTickBarMap:
    """Build a lazy bar map that loads tick slices on demand."""
    return LazyCombinedTickBarMap(
        symbols,
        kline_times,
        bar_cache_size=bar_cache_size,
        shard_cache_size_per_symbol=shard_cache_size_per_symbol,
    )


def info(symbol: str) -> Optional[dict]:
    """回傳快取資訊。"""
    path = cache_path(symbol)
    manifest = _load_shard_manifest(symbol)
    try:
        if manifest is not None:
            count = sum(int(v["count"]) for v in manifest.get("months", {}).values())
            size_mb = 0.0
            for month in manifest.get("months", {}).values():
                shard_file = _cache_dir() / month["path"]
                if shard_file.exists():
                    size_mb += shard_file.stat().st_size / 1_048_576
            return {
                "count": count,
                "start_ms": int(manifest["start_ms"]),
                "end_ms": int(manifest["end_ms"]),
                "size_mb": size_mb,
                "path": str(shard_manifest_path(symbol)),
                "source": "shards",
            }

        if not path.exists():
            return None

        data, meta = load_raw(symbol)
        if data is None:
            return None
        return {
            "count":    len(data),
            "start_ms": meta["start_ms"],
            "end_ms":   meta["end_ms"],
            "size_mb":  path.stat().st_size / 1_048_576,
            "path":     str(path),
            "source":   "legacy_npz",
        }
    except Exception as exc:
        logger.error("tick_cache info error: %s", exc)
        return None
