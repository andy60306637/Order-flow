"""
WebSocket 工作執行緒。

在 QThread 內跑獨立的 asyncio event loop，負責：
  1. 取得歷史 K 線（REST）
  2. 取得 Order Book 快照（REST）
  3. 連線 Binance Futures combined stream
  4. 自動回應 PING / 24h 重連
  5. 透過 PyQt6 signal 把資料推送給主執行緒

若外部呼叫 request_resync()，下次迴圈重新取 OB 快照。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import aiohttp
import websockets
from PyQt6.QtCore import QThread, pyqtSignal

import config

logger = logging.getLogger(__name__)


class WsWorkerThread(QThread):
    # ── Qt signals（在主執行緒消費）──────────────────────────────────────────
    trade_signal       = pyqtSignal(dict)   # aggTrade payload
    kline_signal       = pyqtSignal(dict)   # kline payload (含 'k' 子物件)
    depth_signal       = pyqtSignal(dict)   # depthUpdate payload
    ob_snapshot_signal = pyqtSignal(dict)   # REST /fapi/v1/depth 回應
    history_signal     = pyqtSignal(list)   # 歷史 K 線列表
    agg_history_signal = pyqtSignal(list)   # 歷史 aggTrades（Footprint 回填用）
    status_signal      = pyqtSignal(str)    # 狀態文字

    def __init__(
        self,
        symbol: str,
        interval: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._symbol   = symbol.lower()
        self._interval = interval
        self._running  = True
        self._resync_requested = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ──────────────────────────────────────────────────────────────────────────
    def request_resync(self) -> None:
        """外部（主執行緒）請求重新取 OB 快照。"""
        self._resync_requested = True

    def stop(self) -> None:
        """安全停止：cancel 所有 tasks，讓各 coroutine 自行清理後再關 loop。"""
        self._running = False
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._cancel_all)

    def _cancel_all(self) -> None:
        """在 event loop 內部取消所有 tasks（含 _main）。"""
        for task in asyncio.all_tasks(self._loop):
            task.cancel()

    # ──────────────────────────────────────────────────────────────────────────
    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except (RuntimeError, asyncio.CancelledError):
            pass
        finally:
            try:
                # 1. 等所有被 cancel 的 task 跑完清理
                pending = asyncio.all_tasks(self._loop)
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                # 2. 關閉 async generators（防止 GeneratorExit 警告）
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            self._loop.close()

    # ──────────────────────────────────────────────────────────────────────────
    async def _main(self) -> None:
        while self._running:
            try:
                self.status_signal.emit(
                    f"正在連線 {self._symbol.upper()} …"
                )
                async with aiohttp.ClientSession() as session:
                    # 1) 歷史 K 線
                    history = await self._fetch_history(session)
                    if history:
                        self.history_signal.emit(history)

                        # 2) Footprint 歷史 aggTrades（最近 N 根 K 棒）
                        n = config.FOOTPRINT_HISTORY_CANDLES
                        start_row = history[-min(n, len(history))]
                        start_t   = int(start_row[0])           # kline open_time ms
                        end_t     = int(history[-1][6]) + 1     # kline close_time ms + 1
                        self.status_signal.emit(
                            f"拉取 Footprint 歷史 ({n} 根) …"
                        )
                        agg_trades = await self._fetch_agg_history(
                            session, start_t, end_t
                        )
                        if agg_trades:
                            # 同時傳送 kline boundaries 供 MainWindow bucketing
                            payload = {
                                "trades":  agg_trades,
                                "klines": [
                                    (int(r[0]), int(r[6]))
                                    for r in history[-min(n, len(history)):]
                                ],
                            }
                            self.agg_history_signal.emit([payload])  # list wrapper for signal type

                    # 3) OB 快照
                    await self._do_ob_snapshot(session)

                    # 4) WebSocket stream
                    await self._connect(session)

            except asyncio.CancelledError:
                raise   # 往上傳遞，讓 run() 的 gather 收到結果
            except Exception as exc:
                logger.error("WS main loop error: %s", exc)
                self.status_signal.emit(f"連線錯誤：{exc}")
                if self._running:
                    await asyncio.sleep(5)

    # ──────────────────────────────────────────────────────────────────────────
    async def _do_ob_snapshot(self, session: aiohttp.ClientSession) -> None:
        snapshot = await self._fetch_ob_snapshot(session)
        if snapshot:
            self.ob_snapshot_signal.emit(snapshot)

    async def _fetch_history(self, session: aiohttp.ClientSession) -> list:
        url = (
            f"{config.REST_BASE}/fapi/v1/klines"
            f"?symbol={self._symbol.upper()}"
            f"&interval={self._interval}"
            f"&limit={config.KLINE_HISTORY_LIMIT}"
        )
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json()
        except Exception as exc:
            logger.error("History fetch error: %s", exc)
        return []

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
        """
        分頁拉取 [start_time_ms, end_time_ms) 範圍內的 Futures aggTrades。
        每次最多 1000 筆，自動翻頁直到覆蓋整個區間。
        """
        all_trades: list = []
        from_t = start_time_ms
        url_base = (
            f"{config.REST_BASE}/fapi/v1/aggTrades"
            f"?symbol={self._symbol.upper()}&limit=1000"
        )

        retry_delay = 1.0   # 初始 429 退避秒數
        while from_t < end_time_ms and self._running:
            url = f"{url_base}&startTime={from_t}&endTime={end_time_ms}"
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status == 429:
                        # Rate limit：優先使用 Retry-After header，否則指數退避
                        ra = r.headers.get("Retry-After")
                        wait = float(ra) if ra else retry_delay
                        logger.warning("aggTrades HTTP 429, retry in %.1fs", wait)
                        await asyncio.sleep(wait)
                        retry_delay = min(retry_delay * 2, 60.0)
                        continue
                    if r.status != 200:
                        logger.warning("aggTrades HTTP %d", r.status)
                        break
                    retry_delay = 2.0   # 成功後重置（保守值，避免連續請求後仍觸發）
                    data = await r.json()
                    if not data:
                        break
                    all_trades.extend(data)
                    last_t = int(data[-1]["T"])
                    if last_t <= from_t:
                        break       # 防止無進展死循環
                    from_t = last_t + 1
                    if len(data) < 1000:
                        break       # 最後一頁
                    # 節流：fapi aggTrades weight=20，IP 限制 2400/min → 最快 0.5s/頁
                    # 保留 50% 餘量，使用 0.8s 確保不觸發 rate limit
                    await asyncio.sleep(0.8)
            except Exception as exc:
                logger.error("aggTrades fetch error: %s", exc)
                break

        logger.info(
            "Fetched %d historical aggTrades for Footprint (%s to %s)",
            len(all_trades), start_time_ms, end_time_ms,
        )
        return all_trades

    # ──────────────────────────────────────────────────────────────────────────
    async def _connect(self, session: aiohttp.ClientSession) -> None:
        sym = self._symbol
        iv  = self._interval
        streams = (
            f"{sym}@aggTrade"
            f"/{sym}@depth@100ms"
            f"/{sym}@kline_{iv}"
        )
        url = f"{config.WS_BASE}/stream?streams={streams}"

        self.status_signal.emit(f"已連線：{sym.upper()} {iv}")
        last_ping = time.monotonic()

        try:
            async with websockets.connect(
                url,
                ping_interval=None,   # 自行管理 ping
                max_size=10 * 1024 * 1024,
                close_timeout=2,      # 限制關閉握手等待時間，避免 stop() 逾時
            ) as ws:
                while self._running:
                    # 若需要重新取 OB 快照
                    if self._resync_requested:
                        self._resync_requested = False
                        snapshot = await self._fetch_ob_snapshot(session)
                        if snapshot:
                            self.ob_snapshot_signal.emit(snapshot)

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=25.0)
                    except asyncio.TimeoutError:
                        # 超時時主動送 ping 維持連線
                        await ws.ping()
                        last_ping = time.monotonic()
                        continue
                    except websockets.ConnectionClosed:
                        logger.warning("WS connection closed, reconnecting…")
                        break

                    # 每 20 秒主動 pong
                    if time.monotonic() - last_ping > 20:
                        await ws.pong()
                        last_ping = time.monotonic()

                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    payload = msg.get("data", {})
                    event   = payload.get("e", "")

                    if event == "aggTrade":
                        self.trade_signal.emit(payload)
                    elif event == "kline":
                        self.kline_signal.emit(payload)
                    elif event == "depthUpdate":
                        self.depth_signal.emit(payload)

        except asyncio.CancelledError:
            raise   # 必須往上傳遞，讓 gather 知道 task 已取消
        except Exception as exc:
            logger.error("WS connect error: %s", exc)
            raise
