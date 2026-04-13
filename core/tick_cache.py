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

import logging
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CACHE_DIR    = _PROJECT_ROOT / "data" / "ticks"
_NCOLS        = 4  # trade_time, price, qty, is_buyer_maker


def cache_path(symbol: str) -> Path:
    """回傳快取路徑。Tick 資料與 K 棒 interval 無關，僅以 symbol 命名。"""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{symbol.upper()}_ticks.npz"


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


def save_raw(symbol: str, data: np.ndarray,
             start_ms: int, end_ms: int) -> bool:
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
    data, meta = load_raw(symbol)
    if data is None or len(data) == 0:
        return np.empty((0, _NCOLS), dtype=np.float64)
    mask = (data[:, 0] >= start_ms) & (data[:, 0] <= end_ms)
    return data[mask]


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
    if len(ticks) == 0:
        return {}
    result: dict[int, np.ndarray] = {}
    times = ticks[:, 0]
    for ot, ct in kline_times:
        lo = int(np.searchsorted(times, ot,     side="left"))
        hi = int(np.searchsorted(times, ct + 1, side="left"))
        if lo < hi:
            result[ot] = ticks[lo:hi]
    return result


def info(symbol: str) -> Optional[dict]:
    """回傳快取資訊。"""
    path = cache_path(symbol)
    if not path.exists():
        return None
    try:
        data, meta = load_raw(symbol)
        if data is None:
            return None
        return {
            "count":    len(data),
            "start_ms": meta["start_ms"],
            "end_ms":   meta["end_ms"],
            "size_mb":  path.stat().st_size / 1_048_576,
            "path":     str(path),
        }
    except Exception as exc:
        logger.error("tick_cache info error: %s", exc)
        return None
