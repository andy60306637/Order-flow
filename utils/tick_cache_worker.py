"""Background tick cache worker.

掃描指定 zip 目錄，將尚未解析的 aggTrades zip 檔案
增量合併進 NPZ 快取（data/ticks/{SYMBOL}_ticks.npz），
讓 UI / 回測等消費者可直接讀取秒級載入的快取。

工作流程
─────────
1. 讀取 manifest（data/ticks/{SYMBOL}_manifest.json）
   → 記錄哪些 zip 已被解析（filename + mtime + 筆數）
2. 掃描 tick_dir，找出新增 / 修改過的 zip
3. 逐一解析（pandas 優先，fallback 純 Python），增量 merge 進 NPZ 快取
4. 更新 manifest
5. --watch 模式：睡眠 --interval 秒後重複；--once 模式：完成即退出

用法
────
# 處理所有待解析 zip，完成後退出
python utils/tick_cache_worker.py --symbol BTCUSDT \\
    --tick-dir "tick_data/binance/futures/um/daily/aggTrades/BTCUSDT"

# 持續背景監看（每 60 秒掃一次）
python utils/tick_cache_worker.py --symbol BTCUSDT \\
    --tick-dir "tick_data/binance/futures/um/daily/aggTrades/BTCUSDT" \\
    --watch --interval 60

# 多 symbol（使用 JSON 設定檔）
python utils/tick_cache_worker.py --config worker_config.json --watch

# 強制全部重建（忽略 manifest，重解析所有 zip）
python utils/tick_cache_worker.py --symbol BTCUSDT \\
    --tick-dir "tick_data/binance/futures/um/daily/aggTrades/BTCUSDT" \\
    --full-rebuild

worker_config.json 格式：
[
  {"symbol": "BTCUSDT",
   "tick_dir": "tick_data/binance/futures/um/daily/aggTrades/BTCUSDT"},
  {"symbol": "ETHUSDT",
   "tick_dir": "tick_data/binance/futures/um/daily/aggTrades/ETHUSDT"}
]
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

# ── 專案根目錄 ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.tick_cache import (
    _parse_agg_trades_csv_lines,
    cache_path,
    load_raw,
    merge_and_save_array,
)

# ── pandas 可選依賴 ─────────────────────────────────────────────────────────
try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

# ── 常數 ────────────────────────────────────────────────────────────────────
_CACHE_DIR = PROJECT_ROOT / "data" / "ticks"
_LOG_DIR   = PROJECT_ROOT / "logs"

# ── 全域 shutdown 旗標 ───────────────────────────────────────────────────────
_shutdown = False


def _handle_signal(sig, frame):  # noqa: ANN001
    global _shutdown
    _shutdown = True
    logging.getLogger(__name__).info("shutdown signal received, finishing current batch…")


signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── logging ─────────────────────────────────────────────────────────────────

def _setup_logging(log_file: Optional[Path] = None) -> logging.Logger:
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    return logging.getLogger(__name__)


# ── Manifest ────────────────────────────────────────────────────────────────

def _manifest_path(symbol: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{symbol.upper()}_manifest.json"


def _load_manifest(symbol: str) -> dict:
    """讀取 manifest。若不存在或損毀則回傳空結構。"""
    p = _manifest_path(symbol)
    if not p.exists():
        return {"processed": {}, "last_scan": None}
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"processed": {}, "last_scan": None}


def _save_manifest(symbol: str, manifest: dict) -> None:
    manifest["last_scan"] = datetime.now(tz=timezone.utc).isoformat()
    p = _manifest_path(symbol)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)


# ── CSV/ZIP 解析 ─────────────────────────────────────────────────────────────

def _parse_zip_pandas(path: Path) -> np.ndarray:
    """Pandas 解析單個 zip → ndarray(N, 4)。失敗時 fallback 純 Python。
    欄位：[time_ms, price, qty, is_buyer_maker]
    """
    with zipfile.ZipFile(path) as zf:
        csv_names = sorted(n for n in zf.namelist() if n.lower().endswith(".csv"))
        if not csv_names:
            return np.empty((0, 4), dtype=np.float64)
        with zf.open(csv_names[0]) as fh:
            if _HAS_PANDAS:
                try:
                    df = pd.read_csv(
                        fh,
                        header=None,
                        usecols=[1, 2, 5, 6],
                        names=["price", "qty", "time", "is_bm"],
                        dtype={"price": np.float64, "qty": np.float64,
                               "time": np.float64, "is_bm": str},
                        on_bad_lines="skip",
                    )
                    mask = pd.to_numeric(df["price"], errors="coerce").notna()
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
            # fallback
            return _parse_agg_trades_csv_lines(fh)


# ── 核心：掃描並增量更新 ─────────────────────────────────────────────────────

def _find_pending(
    tick_dir: Path,
    symbol: str,
    manifest: dict,
    full_rebuild: bool,
) -> list[Path]:
    """回傳需要（重新）解析的 zip 列表，依日期升序排列。"""
    paths = sorted(tick_dir.glob(f"{symbol.upper()}*.zip"))
    if full_rebuild:
        return paths

    processed: dict[str, dict] = manifest.get("processed", {})
    pending: list[Path] = []
    for p in paths:
        key = p.name
        entry = processed.get(key)
        if entry is None:
            pending.append(p)
            continue
        # 若檔案被修改（mtime 不同），也重新解析
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if abs(mtime - entry.get("mtime", 0)) > 1.0:
            pending.append(p)
    return pending


def _process_symbol(
    symbol: str,
    tick_dir: Path,
    full_rebuild: bool,
    zip_workers: int,
    log: logging.Logger,
) -> int:
    """解析待處理 zip，增量合併進快取。回傳本次新增 tick 數。"""
    manifest = _load_manifest(symbol)
    if full_rebuild:
        manifest = {"processed": {}, "last_scan": None}
        log.info("[%s] full-rebuild: clearing manifest", symbol)

    pending = _find_pending(tick_dir, symbol, manifest, full_rebuild)
    if not pending:
        log.info("[%s] no new zip files", symbol)
        return 0

    log.info(
        "[%s] %d zip(s) pending, pandas=%s, workers=%d",
        symbol, len(pending), "yes" if _HAS_PANDAS else "no", zip_workers,
    )

    # 全部重建時先清除舊快取，讓 merge 從空開始
    if full_rebuild:
        npz = cache_path(symbol)
        if npz.exists():
            npz.unlink()
            log.info("[%s] removed old cache %s", symbol, npz.name)

    total_new = 0
    processed = manifest.setdefault("processed", {})

    # 分批（每批 zip_workers 個），避免一次佔用過多記憶體
    batch_size = max(zip_workers, 4)
    for batch_start in range(0, len(pending), batch_size):
        if _shutdown:
            log.info("[%s] shutdown requested, stopping mid-batch", symbol)
            break

        batch = pending[batch_start: batch_start + batch_size]
        parts: list[tuple[Path, np.ndarray]] = []

        with ThreadPoolExecutor(max_workers=zip_workers) as ex:
            futures = {ex.submit(_parse_zip_pandas, p): p for p in batch}
            for fut in as_completed(futures):
                path = futures[fut]
                try:
                    arr = fut.result()
                except Exception as exc:
                    log.warning("[%s] parse error %s: %s", symbol, path.name, exc)
                    arr = np.empty((0, 4), dtype=np.float64)
                parts.append((path, arr))

        # 將本批 parts 依日期排序後合併進快取（確保時間連續性）
        parts.sort(key=lambda t: t[0].name)
        for path, arr in parts:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0

            if len(arr) == 0:
                log.warning("[%s] %s parsed 0 rows, skipping", symbol, path.name)
                processed[path.name] = {"mtime": mtime, "rows": 0}
                continue

            start_ms = int(arr[:, 0].min())
            end_ms   = int(arr[:, 0].max())
            n = merge_and_save_array(symbol, arr, start_ms, end_ms)
            total_new += len(arr)
            processed[path.name] = {"mtime": mtime, "rows": len(arr)}
            log.info(
                "[%s] merged %s → +%d rows (cache total %d)",
                symbol, path.name, len(arr), n,
            )

        _save_manifest(symbol, manifest)
        log.info(
            "[%s] batch %d–%d done",
            symbol,
            batch_start + 1,
            min(batch_start + batch_size, len(pending)),
        )

    return total_new


# ── 主迴圈 ─────────────────────────────────────────────────────────────────

def _run_once(jobs: list[dict], full_rebuild: bool, zip_workers: int, log: logging.Logger) -> None:
    t0 = time.perf_counter()
    for job in jobs:
        if _shutdown:
            break
        symbol   = job["symbol"].upper()
        tick_dir = Path(job["tick_dir"])
        if not tick_dir.is_dir():
            log.error("[%s] tick_dir not found: %s", symbol, tick_dir)
            continue
        try:
            n = _process_symbol(symbol, tick_dir, full_rebuild, zip_workers, log)
            if n:
                log.info("[%s] total new ticks this run: %d", symbol, n)
        except Exception as exc:
            log.exception("[%s] unexpected error: %s", symbol, exc)
    log.info("run complete in %.1fs", time.perf_counter() - t0)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Background worker: parse aggTrades zips → NPZ cache (incremental)."
    )
    # single-symbol shorthand
    ap.add_argument("--symbol",   default=None, help="e.g. BTCUSDT")
    ap.add_argument("--tick-dir", default=None, help="directory containing *.zip files")
    # multi-symbol config file
    ap.add_argument(
        "--config", default=None,
        help="JSON config file: [{\"symbol\":\"BTCUSDT\",\"tick_dir\":\"...\"}]",
    )
    # behaviour
    ap.add_argument(
        "--watch", action="store_true",
        help="keep running, re-scan every --interval seconds",
    )
    ap.add_argument(
        "--interval", type=int, default=60,
        help="watch poll interval in seconds (default: 60)",
    )
    ap.add_argument(
        "--full-rebuild", action="store_true",
        help="ignore manifest, re-parse all zips and rebuild cache from scratch",
    )
    ap.add_argument(
        "--zip-workers", type=int, default=4,
        help="parallel workers for zip parsing (default: 4)",
    )
    ap.add_argument(
        "--log-file", default=None,
        help="write logs to this file in addition to stdout",
    )
    args = ap.parse_args()

    log_file = Path(args.log_file) if args.log_file else None
    log = _setup_logging(log_file)

    # ── 建立 job 列表 ────────────────────────────────────────────────────
    jobs: list[dict] = []
    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.is_file():
            ap.error(f"config file not found: {cfg_path}")
        with open(cfg_path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, list):
            ap.error("config JSON must be a list of {symbol, tick_dir}")
        jobs = raw
    elif args.symbol and args.tick_dir:
        jobs = [{"symbol": args.symbol, "tick_dir": args.tick_dir}]
    else:
        ap.error("provide either --config or both --symbol and --tick-dir")

    log.info(
        "tick_cache_worker starting | jobs=%d watch=%s interval=%ds pandas=%s",
        len(jobs), args.watch, args.interval, "yes" if _HAS_PANDAS else "no",
    )

    if args.watch:
        cycle = 0
        while not _shutdown:
            cycle += 1
            log.info("── cycle %d ──", cycle)
            _run_once(jobs, args.full_rebuild and cycle == 1, args.zip_workers, log)
            if _shutdown:
                break
            log.info("sleeping %ds (Ctrl+C to stop)…", args.interval)
            for _ in range(args.interval):
                if _shutdown:
                    break
                time.sleep(1)
    else:
        _run_once(jobs, args.full_rebuild, args.zip_workers, log)

    log.info("tick_cache_worker exited")


if __name__ == "__main__":
    main()
