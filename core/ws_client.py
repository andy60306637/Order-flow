"""
WebSocket 客戶端模組。

提供兩個類：
  WsClient          — 純 asyncio 實作，零框架依賴。透過 emit(event, data) callback
                       推送資料，可直接被 DataEngine / Server / Worker 使用。
  WsWorkerThread     — 輕量 QThread 包裝器，將 WsClient 事件橋接到 PyQt6 signal，
                       維持向後相容（Desktop UI 可直接使用）。

負責：
  1. 取得歷史 K 線（REST）
  2. 取得 Order Book 快照（REST）
  3. 連線 Binance Futures combined stream
  4. 自動回應 PING / 24h 重連
  5. 透過 emit callback 推送資料給消費者

若外部呼叫 request_resync()，下次迴圈重新取 OB 快照。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional

import aiohttp
import websockets

import config
from core import kline_cache

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# WsClient — 純 asyncio，零框架依賴
# ═════════════════════════════════════════════════════════════════════════════

class WsClient:
    """
    純 asyncio WebSocket 客戶端。

    所有資料透過 ``emit(event, data)`` 回調推送，無 PyQt6 依賴。
    可嵌入 DataEngine、FastAPI、CLI Worker 等任何 asyncio runtime。

    事件名稱（與原 WsWorkerThread signal 一一對應）：
      trade, kline, depth, ob_snapshot, history, agg_history,
      more_history, more_agg_history, exchange_info, status,
      backtest_history, cache_ready
    """

    def __init__(
        self,
        symbol: str,
        interval: str,
        emit: Optional[Callable[[str, Any], None]] = None,
        engine: Optional[Any] = None,
    ) -> None:
        self._symbol   = symbol.lower()
        self._interval = interval
        self._running  = True
        self._resync_requested  = False
        self._loading_more      = False
        self._rate_limit_wait: float = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._live_bar: Optional[dict[str, Any]] = None

        # emit 來源：優先使用明確傳入的 emit，否則用 engine.emit
        if emit is not None:
            self._emit = emit
        elif engine is not None:
            self._emit = engine.emit
        else:
            self._emit = lambda event, data: None

    # ── 公開屬性 ─────────────────────────────────────────────────────

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def interval(self) -> str:
        return self._interval

    @interval.setter
    def interval(self, value: str) -> None:
        self._interval = value

    # ── 外部控制 ─────────────────────────────────────────────────────

    def request_resync(self) -> None:
        self._resync_requested = True

    def request_more_history(self, end_time_ms: int) -> None:
        if self._loading_more:
            return
        loop = self._loop
        if loop and not loop.is_closed():
            self._loading_more = True
            asyncio.run_coroutine_threadsafe(
                self._load_more_history(end_time_ms), loop
            )

    def request_backtest_history(
        self, total_candles: int, cache_only: bool = False
    ) -> None:
        loop = self._loop
        if loop and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._fetch_backtest_history(total_candles, cache_only=cache_only),
                loop,
            )

    def stop(self) -> None:
        self._running = False
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._cancel_all)

    def _cancel_all(self) -> None:
        for task in asyncio.all_tasks(self._loop):
            task.cancel()

    # ── 主迴圈 ───────────────────────────────────────────────────────

    async def run(self) -> None:
        """啟動主迴圈。可由外部 event loop 直接 await，也可在 QThread 中使用。"""
        self._loop = asyncio.get_running_loop()
        while self._running:
            try:
                self._emit("status", f"正在連線 {self._symbol.upper()} …")
                async with aiohttp.ClientSession() as session:
                    await self._fetch_exchange_info(session)

                    history = await self._fetch_history(session)
                    if history:
                        self._emit("history", history)
                        self._seed_live_bar(history[-1])

                        n = config.FOOTPRINT_HISTORY_CANDLES
                        start_row = history[-min(n, len(history))]
                        start_t   = int(start_row[0])
                        end_t     = int(history[-1][6]) + 1

                        max_ms = config.FOOTPRINT_MAX_BACKFILL_MS.get(
                            self._interval, n * 60 * 1_000,
                        )
                        capped_start_t = max(start_t, end_t - max_ms)
                        if capped_start_t > start_t:
                            backfill_rows = [
                                r for r in history[-min(n, len(history)):]
                                if int(r[6]) >= capped_start_t
                            ]
                            start_t = capped_start_t
                        else:
                            backfill_rows = history[-min(n, len(history)):]

                        actual_n = len(backfill_rows) if capped_start_t > int(start_row[0]) else n
                        self._emit("status", f"拉取 Footprint 歷史 ({actual_n} 根) …")
                        agg_trades = await self._fetch_agg_history(
                            session, start_t, end_t
                        )
                        if agg_trades:
                            payload = {
                                "trades":  agg_trades,
                                "klines": [
                                    (int(r[0]), int(r[6]))
                                    for r in backfill_rows
                                ],
                            }
                            self._emit("agg_history", [payload])

                    await self._do_ob_snapshot(session)
                    await self._connect(session)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("WS main loop error: %s", exc)
                self._emit("status", f"連線錯誤：{exc}")
                if self._running:
                    await asyncio.sleep(5)

    # ── 內部方法 ─────────────────────────────────────────────────────

    async def _do_ob_snapshot(self, session: aiohttp.ClientSession) -> None:
        snapshot = await self._fetch_ob_snapshot(session)
        if snapshot:
            self._emit("ob_snapshot", snapshot)

    async def _fetch_exchange_info(self, session: aiohttp.ClientSession) -> None:
        url = f"{config.REST_BASE}/fapi/v1/exchangeInfo"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    logger.warning("exchangeInfo HTTP %d", r.status)
                    return
                data = await r.json()
        except Exception as exc:
            logger.error("exchangeInfo fetch error: %s", exc)
            return

        tick_map: dict[str, float] = {}
        for sym_info in data.get("symbols", []):
            symbol = sym_info.get("symbol", "")
            for f in sym_info.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    try:
                        tick_map[symbol] = float(f["tickSize"])
                    except (KeyError, ValueError):
                        pass
                    break
        if tick_map:
            self._emit("exchange_info", tick_map)
            logger.info("Fetched tickSize for %d symbols from exchangeInfo", len(tick_map))

    async def _fetch_history(
        self,
        session: aiohttp.ClientSession,
        end_time_ms: int = 0,
    ) -> list:
        url = (
            f"{config.REST_BASE}/fapi/v1/klines"
            f"?symbol={self._symbol.upper()}"
            f"&interval={self._interval}"
            f"&limit={config.KLINE_HISTORY_LIMIT}"
        )
        if end_time_ms > 0:
            url += f"&endTime={end_time_ms}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status == 200:
                    return await r.json()
                if r.status == 429:
                    retry_after = float(r.headers.get("Retry-After", 60))
                    self._rate_limit_wait = max(retry_after, 5.0)
                    logger.warning(
                        "History fetch HTTP 429 — rate limited, retry after %.0fs",
                        self._rate_limit_wait,
                    )
                else:
                    logger.warning("History fetch HTTP %d", r.status)
        except Exception as exc:
            logger.error("History fetch error: %s", exc)
        return []

    def _interval_ms(self) -> int:
        unit = self._interval[-1]
        try:
            value = int(self._interval[:-1])
        except ValueError:
            return 60_000
        if unit == "m":
            return value * 60_000
        if unit == "h":
            return value * 60 * 60_000
        if unit == "d":
            return value * 24 * 60 * 60_000
        return 60_000

    def _seed_live_bar(self, row: list) -> None:
        try:
            self._live_bar = {
                "t": int(row[0]),
                "T": int(row[6]),
                "o": str(row[1]),
                "h": str(row[2]),
                "l": str(row[3]),
                "c": str(row[4]),
                "v": str(row[5]),
                "x": False,
            }
        except (IndexError, TypeError, ValueError):
            self._live_bar = None

    def _synthetic_kline_from_trade(self, trade: dict) -> dict | None:
        try:
            price = float(trade.get("p", 0))
            qty = float(trade.get("q", 0))
            trade_time = int(trade.get("T") or trade.get("E") or time.time() * 1000)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None

        interval_ms = self._interval_ms()
        open_time = (trade_time // interval_ms) * interval_ms
        close_time = open_time + interval_ms - 1
        bar = self._live_bar

        if not bar or int(bar.get("t", 0)) != open_time:
            bar = {
                "t": open_time,
                "T": close_time,
                "o": f"{price:.12g}",
                "h": f"{price:.12g}",
                "l": f"{price:.12g}",
                "c": f"{price:.12g}",
                "v": f"{qty:.12g}",
                "x": False,
            }
            self._live_bar = bar
            return {"e": "kline", "E": trade_time, "s": self._symbol.upper(), "k": dict(bar)}

        high = max(float(bar.get("h", price)), price)
        low = min(float(bar.get("l", price)), price)
        volume = max(0.0, float(bar.get("v", 0))) + max(0.0, qty)
        bar.update({
            "h": f"{high:.12g}",
            "l": f"{low:.12g}",
            "c": f"{price:.12g}",
            "v": f"{volume:.12g}",
            "x": False,
        })
        return {"e": "kline", "E": trade_time, "s": self._symbol.upper(), "k": dict(bar)}

    async def _load_more_history(self, end_time_ms: int) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                rows = await self._fetch_history(session, end_time_ms=end_time_ms - 1)
                if not rows:
                    self._emit("more_history", [])
                    return
                self._emit("more_history", rows)

                n = config.FOOTPRINT_HISTORY_CANDLES
                slice_rows = rows[-min(n, len(rows)):]
                start_t = int(slice_rows[0][0])
                end_t   = int(rows[-1][6]) + 1
                max_ms  = config.FOOTPRINT_MAX_BACKFILL_MS.get(
                    self._interval, n * 60 * 1_000
                )
                capped_start_t = max(start_t, end_t - max_ms)
                backfill_rows  = [
                    r for r in slice_rows if int(r[6]) >= capped_start_t
                ] or slice_rows
                capped_start_t = int(backfill_rows[0][0])

                agg_trades = await self._fetch_agg_history(
                    session, capped_start_t, end_t
                )
                if agg_trades:
                    payload = {
                        "trades": agg_trades,
                        "klines": [(int(r[0]), int(r[6])) for r in backfill_rows],
                    }
                    self._emit("more_agg_history", [payload])
        except Exception as exc:
            logger.error("load_more_history error: %s", exc)
            self._emit("more_history", [])
        finally:
            self._loading_more = False

    async def _fetch_backtest_history(self, total_candles: int, cache_only: bool = False) -> None:
        MAX_RETRIES  = 5
        RETRY_BASE   = 2.0
        PAGE_DELAY   = 0.4
        RL_MIN_WAIT  = 60.0

        existing = kline_cache.load(self._symbol.upper(), self._interval)
        if existing:
            logger.info(
                "Resuming from cache: %d existing rows, oldest=%s",
                len(existing), existing[0][0],
            )

        all_rows: list = list(existing)
        end_time: int = (int(existing[0][0]) - 1) if existing else 0
        remaining = total_candles - len(all_rows)

        if remaining <= 0:
            all_rows = all_rows[-total_candles:]
            self._emit("status", f"快取已有足夠資料（{len(all_rows):,} 根），直接使用")
            if cache_only:
                self._emit("cache_ready", len(all_rows))
            else:
                self._emit("backtest_history", all_rows)
            return

        try:
            async with aiohttp.ClientSession() as session:
                error_count = 0

                while remaining > 0 and self._running:
                    self._emit(
                        "status",
                        f"載入回測資料… 已取得 {len(all_rows):,}/{total_candles:,}",
                    )

                    self._rate_limit_wait = 0
                    rows = await self._fetch_history(session, end_time_ms=end_time)

                    if not rows and self._rate_limit_wait > 0:
                        wait = max(self._rate_limit_wait, RL_MIN_WAIT)
                        self._rate_limit_wait = 0
                        self._emit(
                            "status",
                            f"⏳ 限流中，等待 {wait:.0f}s 後繼續"
                            f"（已取得 {len(all_rows):,}/{total_candles:,}）",
                        )
                        await asyncio.sleep(wait)
                        continue

                    if not rows:
                        error_count += 1
                        if error_count > MAX_RETRIES:
                            logger.error(
                                "Backtest history fetch failed after %d retries "
                                "(got %d/%d candles). Emitting partial data.",
                                MAX_RETRIES, len(all_rows), total_candles,
                            )
                            self._emit(
                                "status",
                                f"⚠ 部分回測資料載入失敗，已取得 {len(all_rows):,} 根",
                            )
                            break
                        wait = RETRY_BASE * (2 ** (error_count - 1))
                        logger.warning(
                            "Backtest history page empty (attempt %d/%d), "
                            "retrying in %.1fs …",
                            error_count, MAX_RETRIES, wait,
                        )
                        self._emit(
                            "status",
                            f"載入回測資料… 第 {error_count} 次重試 "
                            f"(已取得 {len(all_rows):,}/{total_candles:,})",
                        )
                        await asyncio.sleep(wait)
                        continue

                    error_count = 0

                    if all_rows:
                        cutoff = all_rows[0][0]
                        rows   = [r for r in rows if r[0] < cutoff]
                    if not rows:
                        break

                    all_rows  = rows + all_rows
                    remaining = total_candles - len(all_rows)
                    end_time  = int(all_rows[0][0]) - 1

                    await asyncio.sleep(PAGE_DELAY)

                if len(all_rows) > total_candles:
                    all_rows = all_rows[-total_candles:]

                if all_rows:
                    try:
                        kline_cache.merge_and_save(
                            self._symbol.upper(), self._interval, all_rows
                        )
                        cache_info = kline_cache.info(self._symbol.upper(), self._interval)
                        size_str = (
                            f"，快取 {cache_info['count']:,} 根"
                            f" ({cache_info['size_mb']:.1f} MB)"
                            if cache_info else ""
                        )
                        self._emit(
                            "status",
                            f"回測資料載入完成，共 {len(all_rows):,} 根 K 棒{size_str}",
                        )
                    except Exception as exc:
                        logger.warning("kline_cache merge_and_save failed: %s", exc)
                        self._emit(
                            "status",
                            f"回測資料載入完成，共 {len(all_rows):,} 根 K 棒",
                        )
                else:
                    self._emit(
                        "status",
                        f"回測資料載入完成，共 {len(all_rows):,} 根 K 棒",
                    )

                if cache_only:
                    self._emit("cache_ready", len(all_rows))
                else:
                    self._emit("backtest_history", all_rows)
        except Exception as exc:
            logger.error("fetch_backtest_history error: %s", exc)
            if cache_only:
                self._emit("cache_ready", 0)
            else:
                self._emit("backtest_history", [])

    async def _fetch_ob_snapshot(self, session: aiohttp.ClientSession) -> dict:
        url = (
            f"{config.REST_BASE}/fapi/v1/depth"
            f"?symbol={self._symbol.upper()}&limit=1000"
        )
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json()
        except Exception as exc:
            logger.error("OB snapshot fetch error: %s", exc)
        return {}

    async def _fetch_agg_history(
        self,
        session: aiohttp.ClientSession,
        start_time_ms: int,
        end_time_ms: int,
    ) -> list:
        all_trades: list = []
        from_t = start_time_ms
        url_base = (
            f"{config.REST_BASE}/fapi/v1/aggTrades"
            f"?symbol={self._symbol.upper()}&limit=1000"
        )

        retry_delay = 1.0
        _WEIGHT_LIMIT = 2400
        _AGG_WEIGHT   = 20

        while from_t < end_time_ms and self._running:
            url = f"{url_base}&startTime={from_t}&endTime={end_time_ms}"
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status == 429:
                        ra = r.headers.get("Retry-After")
                        wait = float(ra) if ra else retry_delay
                        logger.warning("aggTrades HTTP 429, retry in %.1fs", wait)
                        await asyncio.sleep(wait)
                        retry_delay = min(retry_delay * 2, 60.0)
                        continue
                    if r.status != 200:
                        logger.warning("aggTrades HTTP %d", r.status)
                        break
                    retry_delay = 2.0

                    used_weight_str = r.headers.get("X-MBX-USED-WEIGHT-1M", "")
                    try:
                        used_weight = int(used_weight_str) if used_weight_str else 0
                    except ValueError:
                        used_weight = 0

                    data = await r.json()
                    if not data:
                        break
                    all_trades.extend(data)
                    last_t = int(data[-1]["T"])
                    if last_t <= from_t:
                        break
                    from_t = last_t + 1
                    if len(data) < 1000:
                        break

                    if used_weight > 0:
                        remaining_w = _WEIGHT_LIMIT - used_weight
                        if remaining_w < _AGG_WEIGHT * 3:
                            delay = 5.0
                        elif remaining_w < _AGG_WEIGHT * 10:
                            delay = 1.0
                        elif remaining_w < _WEIGHT_LIMIT * 0.5:
                            delay = 0.3
                        else:
                            delay = 0.1
                    else:
                        delay = 0.8
                    await asyncio.sleep(delay)
            except Exception as exc:
                logger.error("aggTrades fetch error: %s", exc)
                break

        logger.info(
            "Fetched %d historical aggTrades for Footprint (%s to %s)",
            len(all_trades), start_time_ms, end_time_ms,
        )
        return all_trades

    async def _connect(self, session: aiohttp.ClientSession) -> None:
        sym = self._symbol
        iv  = self._interval
        streams = (
            f"{sym}@trade"
            f"/{sym}@depth@100ms"
            f"/{sym}@kline_{iv}"
        )
        url = f"{config.WS_BASE}/stream?streams={streams}"

        self._emit("status", f"已連線：{sym.upper()} {iv}")
        last_ping = time.monotonic()

        try:
            async with websockets.connect(
                url,
                ping_interval=None,
                max_size=10 * 1024 * 1024,
                close_timeout=2,
            ) as ws:
                while self._running:
                    if self._resync_requested:
                        self._resync_requested = False
                        snapshot = await self._fetch_ob_snapshot(session)
                        if snapshot:
                            self._emit("ob_snapshot", snapshot)

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=25.0)
                    except asyncio.TimeoutError:
                        await ws.ping()
                        last_ping = time.monotonic()
                        continue
                    except websockets.ConnectionClosed:
                        logger.warning("WS connection closed, reconnecting…")
                        break

                    if time.monotonic() - last_ping > 20:
                        await ws.pong()
                        last_ping = time.monotonic()

                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    payload = msg.get("data", {})
                    event   = payload.get("e", "")

                    if event in ("aggTrade", "trade"):
                        self._emit("trade", payload)
                        synthetic = self._synthetic_kline_from_trade(payload)
                        if synthetic:
                            self._emit("kline", synthetic)
                    elif event == "kline":
                        self._emit("kline", payload)
                    elif event == "depthUpdate":
                        self._emit("depth", payload)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("WS connect error: %s", exc)
            raise


# ═════════════════════════════════════════════════════════════════════════════
# WsWorkerThread — QThread 向後相容包裝器
# ═════════════════════════════════════════════════════════════════════════════

try:
    from PyQt6.QtCore import QThread, pyqtSignal

    class WsWorkerThread(QThread):
        """
        QThread 包裝器，將 WsClient 事件橋接到 PyQt6 signal。
        API 與舊版完全相容，MainWindow 無需修改。
        """
        # ── Qt signals ──────────────────────────────────────────────────────
        trade_signal            = pyqtSignal(dict)
        kline_signal            = pyqtSignal(dict)
        depth_signal            = pyqtSignal(dict)
        ob_snapshot_signal      = pyqtSignal(dict)
        history_signal          = pyqtSignal(list)
        agg_history_signal      = pyqtSignal(list)
        more_history_signal     = pyqtSignal(list)
        more_agg_history_signal = pyqtSignal(list)
        exchange_info_signal    = pyqtSignal(dict)
        status_signal           = pyqtSignal(str)
        backtest_history_signal = pyqtSignal(list)
        cache_ready_signal      = pyqtSignal(int)

        # 事件名 → Qt signal 的映射表
        _SIGNAL_MAP: dict[str, str] = {
            "trade":            "trade_signal",
            "kline":            "kline_signal",
            "depth":            "depth_signal",
            "ob_snapshot":      "ob_snapshot_signal",
            "history":          "history_signal",
            "agg_history":      "agg_history_signal",
            "more_history":     "more_history_signal",
            "more_agg_history": "more_agg_history_signal",
            "exchange_info":    "exchange_info_signal",
            "status":           "status_signal",
            "backtest_history": "backtest_history_signal",
            "cache_ready":      "cache_ready_signal",
        }

        def __init__(
            self,
            symbol: str,
            interval: str,
            parent=None,
        ) -> None:
            super().__init__(parent)
            self._client = WsClient(
                symbol=symbol,
                interval=interval,
                emit=self._bridge_emit,
            )

        def _bridge_emit(self, event: str, data: Any) -> None:
            """將 WsClient 事件轉發到對應的 Qt signal。"""
            sig_name = self._SIGNAL_MAP.get(event)
            if sig_name:
                getattr(self, sig_name).emit(data)

        # ── 委派給 WsClient ─────────────────────────────────────────────
        def request_resync(self) -> None:
            self._client.request_resync()

        def request_more_history(self, end_time_ms: int) -> None:
            self._client.request_more_history(end_time_ms)

        def request_backtest_history(
            self, total_candles: int, cache_only: bool = False
        ) -> None:
            self._client.request_backtest_history(total_candles, cache_only=cache_only)

        def stop(self) -> None:
            self._client.stop()

        # ── QThread.run() ─────────────────────────────────────────────────
        def run(self) -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._client.run())
            except (RuntimeError, asyncio.CancelledError):
                pass
            finally:
                try:
                    pending = asyncio.all_tasks(loop)
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                loop.close()

except ImportError:
    # 無 PyQt6 環境（Server / Worker）：不提供 WsWorkerThread
    pass
