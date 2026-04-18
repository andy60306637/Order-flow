"""本機 K 線快取。以 NumPy .npy 二進位格式儲存歷史 K 線，路徑：
    <project_root>/data/klines/{SYMBOL}_{interval}.npy

每個 .npy 檔為 shape (N, 12) 的 float64 陣列，欄位順序與
Binance REST /fapi/v1/klines 回傳完全相同：
  0:open_time  1:open  2:high  3:low  4:close  5:volume
  6:close_time 7:quote_volume  8:count  9:taker_buy_volume
  10:taker_buy_quote_volume  11:ignore
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np

from core.data_types import Kline

logger = logging.getLogger(__name__)

# 快取目錄（project root / data / klines）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CACHE_DIR    = _PROJECT_ROOT / "data" / "klines"
_NCOLS        = 12  # Binance kline 欄位數


# ─────────────────────────────────────────────────────────────────────────────

def cache_path(symbol: str, interval: str) -> Path:
    """回傳指定交易對 + interval 的快取檔路徑（確保目錄存在）。"""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{symbol.upper()}_{interval}.npy"


def load(symbol: str, interval: str) -> list[list]:
    """從快取讀取所有 K 線，回傳 list[list]（與 Binance REST 格式相同）。
    找不到快取或讀取失敗時回傳空 list。
    """
    path = cache_path(symbol, interval)
    if not path.exists():
        return []
    try:
        arr = np.load(str(path))       # shape (N, 12), dtype float64
        return arr.tolist()
    except Exception as exc:
        logger.error("kline_cache load error [%s]: %s", path.name, exc)
        return []


def save(symbol: str, interval: str, rows: list[list]) -> bool:
    """將 rows 全量寫入快取（覆蓋既有檔案）。
    rows 必須為升序排列（open_time 遞增）。
    成功回傳 True，失敗回傳 False。
    """
    if not rows:
        return False
    path = cache_path(symbol, interval)
    try:
        arr = np.array(rows, dtype=np.float64)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        # 若欄位不足 12 欄，補 0（相容較舊資料或不完整頁）
        if arr.shape[1] < _NCOLS:
            pad = np.zeros((arr.shape[0], _NCOLS - arr.shape[1]), dtype=np.float64)
            arr = np.concatenate([arr, pad], axis=1)
        np.save(str(path), arr)
        size_mb = path.stat().st_size / 1_048_576
        logger.info(
            "kline_cache saved %d rows → %s (%.1f MB)",
            len(rows), path.name, size_mb,
        )
        return True
    except Exception as exc:
        logger.error("kline_cache save error [%s]: %s", path.name, exc)
        return False


def merge_and_save(symbol: str, interval: str, new_rows: list[list]) -> list[list]:
    """合併 new_rows 與現有快取（去重、升序排序），儲存後回傳合併結果。"""
    existing = load(symbol, interval)
    merged   = _merge(existing, new_rows)
    save(symbol, interval, merged)
    return merged


def info(symbol: str, interval: str) -> Optional[dict]:
    """回傳快取資訊字典，快取不存在時回傳 None。

    Returns:
        {
            "count"   : int,    # 總 K 棒數
            "start_ms": int,    # 最早 open_time (ms)
            "end_ms"  : int,    # 最近 open_time (ms)
            "size_mb" : float,  # 檔案大小
            "path"    : str,    # 完整路徑
        }
    """
    path = cache_path(symbol, interval)
    if not path.exists():
        return None
    try:
        arr      = np.load(str(path), mmap_mode="r")  # 僅讀 metadata
        count    = int(arr.shape[0])
        start_ms = int(arr[0, 0])
        end_ms   = int(arr[-1, 0])
        return {
            "count"   : count,
            "start_ms": start_ms,
            "end_ms"  : end_ms,
            "size_mb" : path.stat().st_size / 1_048_576,
            "path"    : str(path),
        }
    except Exception as exc:
        logger.error("kline_cache info error [%s]: %s", path.name, exc)
        return None


def load_range(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
    """讀取 open_time 落在 [start_ms, end_ms] 的 K 線列。"""
    path = cache_path(symbol, interval)
    if not path.exists():
        return []
    try:
        arr = np.load(str(path), mmap_mode="r")
        open_times = arr[:, 0].astype(np.int64)
        lo = int(np.searchsorted(open_times, start_ms, side="left"))
        hi = int(np.searchsorted(open_times, end_ms, side="right"))
        return arr[lo:hi].tolist()
    except Exception as exc:
        logger.error("kline_cache load_range error [%s]: %s", path.name, exc)
        return []


def load_range_as_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Kline]:
    """讀取 open_time 落在 [start_ms, end_ms] 的 K 線並轉成 Kline 物件。"""
    rows = load_range(symbol, interval, start_ms, end_ms)
    return [Kline.from_rest(symbol.upper(), interval, row) for row in rows]


# ─────────────────────────────────────────────────────────────────────────────

def _merge(a: list[list], b: list[list]) -> list[list]:
    """合併兩段 K 線列表，以 open_time 為 key 去重，升序排列。"""
    seen: dict[int, list] = {}
    for row in a:
        seen[int(row[0])] = row
    for row in b:
        seen[int(row[0])] = row
    return [seen[k] for k in sorted(seen)]
