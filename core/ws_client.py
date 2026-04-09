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
from core import kline_cache

logger = logging.getLogger(__name__)


class WsWorkerThread(QThread):
    # ── Qt signals（在主執行緒消費）──────────────────────────────────────────
    trade_signal            = pyqtSignal(dict)   # aggTrade payload
    kline_signal            = pyqtSignal(dict)   # kline payload (含 'k' 子物件)
    depth_signal            = pyqtSignal(dict)   # depthUpdate payload
    ob_snapshot_signal      = pyqtSignal(dict)   # REST /fapi/v1/depth 回應
    history_signal          = pyqtSignal(list)   # 歷史 K 線列表
    agg_history_signal      = pyqtSignal(list)   # 歷史 aggTrades（Footprint 回填用）
    more_history_signal     = pyqtSignal(list)   # 往前翻頁的更早 K 線
    more_agg_history_signal = pyqtSignal(list)   # 往前翻頁附帶的 aggTrades
    exchange_info_signal    = pyqtSignal(dict)   # {symbol: tick_size} 動態 tick size
    status_signal           = pyqtSignal(str)    # 狀態文字
    backtest_history_signal = pyqtSignal(list)   # 回測專用批量 K 線
    cache_ready_signal      = pyqtSignal(int)    # 快取儲存完成（傳回列數）

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
        self._resync_requested  = False
        self._loading_more      = False   # 防止並發的 load-more 請求
        self._rate_limit_wait: float = 0  # 429 Retry-After 秒數（_fetch_history 設定）
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ──────────────────────────────────────────────────────────────────────────
    def request_resync(self) -> None:
        """外部（主執行緒）請求重新取 OB 快照。"""
        self._resync_requested = True

    def request_more_history(self, end_time_ms: int) -> None:
        """
        從主執行緒請求比 end_time_ms 更早的歷史 K 線。
        非同步排入 event loop，不阻塞 UI。
        """
        if self._loading_more:
            return
        if self._loop and not self._loop.is_closed():
            self._loading_more = True
            asyncio.run_coroutine_threadsafe(
                self._load_more_history(end_time_ms), self._loop
            )

    def request_backtest_history(self, total_candles: int, cache_only: bool = False) -> None:
        """
        從主執行緒請求批量歷史 K 線（回測專用）。
        自動分頁直到累計 total_candles 根。

        cache_only=False（預設）：下載後透過 backtest_history_signal 回傳，並寫入本機快取。
        cache_only=True         ：只下載并寫入快取，完成後透過 cache_ready_signal 回傳列數。
        """
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._fetch_backtest_history(total_candles, cache_only=cache_only),
                self._loop,
            )

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
                    # 0) 動態獲取交易對的 tick size
                    await self._fetch_exchange_info(session)

                    # 1) 歷史 K 線
                    history = await self._fetch_history(session)
                    if history:
                        self.history_signal.emit(history)

                        # 2) Footprint 歷史 aggTrades（最近 N 根 K 棒）
                        n = config.FOOTPRINT_HISTORY_CANDLES
                        start_row = history[-min(n, len(history))]
                        start_t   = int(start_row[0])           # kline open_time ms
                        end_t     = int(history[-1][6]) + 1     # kline close_time ms + 1

                        # ── 依 interval 限制最大回填時間範圍 ──────────────────
                        # 大 interval（1h/4h）的 N 根橫跨數十小時，aggTrades 量
                        # 可達百萬筆，每頁 0.8s 節流會拖慢數分鐘。
                        # 依設定表截短 start_t，只拉近期足夠的量。
                        max_ms = config.FOOTPRINT_MAX_BACKFILL_MS.get(
                            self._interval,
                            n * 60 * 1_000,  # 未列出的 interval 預設 N 分鐘
                        )
                        capped_start_t = max(start_t, end_t - max_ms)
                        if capped_start_t > start_t:
                            # 依截短後的 start_t 重新找對應的 kline slice
                            backfill_rows = [
                                r for r in history[-min(n, len(history)):]
                                if int(r[6]) >= capped_start_t   # close_time 仍在範圍內
                            ]
                            start_t = capped_start_t
                        else:
                            backfill_rows = history[-min(n, len(history)):]

                        actual_n = len(backfill_rows) if capped_start_t > int(start_row[0]) else n
                        self.status_signal.emit(
                            f"拉取 Footprint 歷史 ({actual_n} 根) …"
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
                                    for r in backfill_rows
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

    async def _fetch_exchange_info(self, session: aiohttp.ClientSession) -> None:
        """
        從 GET /fapi/v1/exchangeInfo 動態獲取交易對的 tickSize，
        發送 exchange_info_signal 給主執行緒更新 config.TICK_SIZES。
        """
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
            self.exchange_info_signal.emit(tick_map)
            logger.info("Fetched tickSize for %d symbols from exchangeInfo", len(tick_map))

    async def _fetch_history(
        self,
        session: aiohttp.ClientSession,
        end_time_ms: int = 0,
    ) -> list:
        """
        拉取最新（或 end_time_ms 之前）的歷史 K 線。
        end_time_ms > 0 時，Binance 回傳 open_time <= end_time_ms 的最後 limit 根。
        429 時：設定 self._rate_limit_wait（秒）並回傳 []。
        """
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
                    self._rate_limit_wait = max(retry_after, 5.0)  # 至少等 5s
                    logger.warning(
                        "History fetch HTTP 429 — rate limited, retry after %.0fs",
                        self._rate_limit_wait,
                    )
                else:
                    logger.warning("History fetch HTTP %d", r.status)
        except Exception as exc:
            logger.error("History fetch error: %s", exc)
        return []

    async def _load_more_history(self, end_time_ms: int) -> None:
        """
        拉取比 end_time_ms 更早的歷史 K 線，並附帶對應的 Footprint aggTrades。
        完成後重置 _loading_more，允許下一次觸發。
        """
        try:
            async with aiohttp.ClientSession() as session:
                # Binance endTime 是 inclusive open_time，所以傳 end_time_ms - 1
                rows = await self._fetch_history(session, end_time_ms=end_time_ms - 1)
                if not rows:
                    self.more_history_signal.emit([])
                    return
                self.more_history_signal.emit(rows)

                # ── 附帶 aggTrades 供 Footprint 回填 ─────────────────────────
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
                    self.more_agg_history_signal.emit([payload])
        except Exception as exc:
            logger.error("load_more_history error: %s", exc)
            self.more_history_signal.emit([])
        finally:
            self._loading_more = False

    async def _fetch_backtest_history(self, total_candles: int, cache_only: bool = False) -> None:
        """
        分頁拉取最近 total_candles 根 K 線，支援從本機快取續傳。

        啟動策略：
          1. 先讀取本機快取（若有），以快取最舊的 open_time 作為下載起點，
             僅下載快取中尚缺少的更早資料（避免重複下載）。
          2. 每成功頁面後等待 PAGE_DELAY 秒，減少 429 機率。
          3. 遇到 429：等待 Retry-After 秒（最少 60s），不消耗重試次數。
          4. 遇到空頁（非限流）：最多重試 MAX_RETRIES 次（指數退避）。

        完成後：
          - 自動合併寫入本機快取（data/klines/{SYMBOL}_{interval}.npy）
          - cache_only=False：透過 backtest_history_signal 回傳全部資料
          - cache_only=True ：僅寫快取，透過 cache_ready_signal 回傳列數
        """
        MAX_RETRIES  = 5
        RETRY_BASE   = 2.0   # 空頁重試：2s, 4s, 8s, 16s, 32s
        PAGE_DELAY   = 0.4   # 每頁成功後稍候，避免觸發限流
        RL_MIN_WAIT  = 60.0  # 429 最少等待秒數（即使 Retry-After < 此值）

        # ── 從快取續傳：直接以快取作為已取得部分 ──────────────────────────
        existing = kline_cache.load(self._symbol.upper(), self._interval)
        if existing:
            logger.info(
                "Resuming from cache: %d existing rows, oldest=%s",
                len(existing),
                existing[0][0],
            )

        all_rows: list = list(existing)   # 快取作為基底（可能為空）
        # 從快取最舊的 open_time 繼續往更早下載；0 = 從最新開始
        end_time: int = (int(existing[0][0]) - 1) if existing else 0
        remaining = total_candles - len(all_rows)

        # 快取已充足：直接走快取路徑，不發網路請求
        if remaining <= 0:
            all_rows = all_rows[-total_candles:]
            self.status_signal.emit(
                f"快取已有足夠資料（{len(all_rows):,} 根），直接使用"
            )
            if cache_only:
                self.cache_ready_signal.emit(len(all_rows))
            else:
                self.backtest_history_signal.emit(all_rows)
            return

        try:
            async with aiohttp.ClientSession() as session:
                error_count = 0   # 連續非限流空頁計數

                while remaining > 0 and self._running:
                    self.status_signal.emit(
                        f"載入回測資料… 已取得 {len(all_rows):,}/{total_candles:,}"
                    )

                    # ── 單頁請求（可能觸發 429 設定 _rate_limit_wait）────────
                    self._rate_limit_wait = 0
                    rows = await self._fetch_history(session, end_time_ms=end_time)

                    # ── 429 限流：等 Retry-After，不消耗重試次數 ────────────
                    if not rows and self._rate_limit_wait > 0:
                        wait = max(self._rate_limit_wait, RL_MIN_WAIT)
                        self._rate_limit_wait = 0
                        self.status_signal.emit(
                            f"⏳ 限流中，等待 {wait:.0f}s 後繼續"
                            f"（已取得 {len(all_rows):,}/{total_candles:,}）"
                        )
                        await asyncio.sleep(wait)
                        continue   # 重新請求同一頁，不計入 error_count

                    # ── 空頁（網路/服務器問題）：指數退避重試 ─────────────
                    if not rows:
                        error_count += 1
                        if error_count > MAX_RETRIES:
                            logger.error(
                                "Backtest history fetch failed after %d retries "
                                "(got %d/%d candles). Emitting partial data.",
                                MAX_RETRIES, len(all_rows), total_candles,
                            )
                            self.status_signal.emit(
                                f"⚠ 部分回測資料載入失敗，已取得 {len(all_rows):,} 根"
                            )
                            break
                        wait = RETRY_BASE * (2 ** (error_count - 1))
                        logger.warning(
                            "Backtest history page empty (attempt %d/%d), "
                            "retrying in %.1fs …",
                            error_count, MAX_RETRIES, wait,
                        )
                        self.status_signal.emit(
                            f"載入回測資料… 第 {error_count} 次重試 "
                            f"(已取得 {len(all_rows):,}/{total_candles:,})"
                        )
                        await asyncio.sleep(wait)
                        continue

                    # ── 成功取得一頁 ─────────────────────────────────────
                    error_count = 0  # 重置連續錯誤計數

                    if all_rows:
                        cutoff = all_rows[0][0]
                        rows   = [r for r in rows if r[0] < cutoff]
                    if not rows:
                        # 已到達最早可用資料
                        break

                    all_rows  = rows + all_rows
                    remaining = total_candles - len(all_rows)
                    end_time  = int(all_rows[0][0]) - 1

                    await asyncio.sleep(PAGE_DELAY)  # 限流預防

                # 只保留最近 total_candles 根
                if len(all_rows) > total_candles:
                    all_rows = all_rows[-total_candles:]

                # ── 寫入本機快取 ────────────────────────────────────────
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
                        self.status_signal.emit(
                            f"回測資料載入完成，共 {len(all_rows):,} 根 K 棒{size_str}"
                        )
                    except Exception as exc:
                        logger.warning("kline_cache merge_and_save failed: %s", exc)
                        self.status_signal.emit(
                            f"回測資料載入完成，共 {len(all_rows):,} 根 K 棒"
                        )
                else:
                    self.status_signal.emit(
                        f"回測資料載入完成，共 {len(all_rows):,} 根 K 棒"
                    )

                if cache_only:
                    self.cache_ready_signal.emit(len(all_rows))
                else:
                    self.backtest_history_signal.emit(all_rows)
        except Exception as exc:
            logger.error("fetch_backtest_history error: %s", exc)
            if cache_only:
                self.cache_ready_signal.emit(0)
            else:
                self.backtest_history_signal.emit([])

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
        根據 X-MBX-USED-WEIGHT-1M header 動態調整節流間隔。
        """
        all_trades: list = []
        from_t = start_time_ms
        url_base = (
            f"{config.REST_BASE}/fapi/v1/aggTrades"
            f"?symbol={self._symbol.upper()}&limit=1000"
        )

        retry_delay = 1.0   # 初始 429 退避秒數
        # Binance Futures IP 限制 2400 weight/min，aggTrades weight=20
        _WEIGHT_LIMIT = 2400
        _AGG_WEIGHT   = 20

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
                    retry_delay = 2.0   # 成功後重置

                    # ── 動態節流：根據已用權重決定等待時間 ──────────────────
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
                        break       # 防止無進展死循環
                    from_t = last_t + 1
                    if len(data) < 1000:
                        break       # 最後一頁

                    # 根據剩餘權重動態決定間隔
                    if used_weight > 0:
                        remaining = _WEIGHT_LIMIT - used_weight
                        if remaining < _AGG_WEIGHT * 3:
                            # 接近限制，長等待
                            delay = 5.0
                        elif remaining < _AGG_WEIGHT * 10:
                            # 餘量較少，保守等待
                            delay = 1.0
                        elif remaining < _WEIGHT_LIMIT * 0.5:
                            # 餘量中等
                            delay = 0.3
                        else:
                            # 充裕，快速拉取
                            delay = 0.1
                    else:
                        # 無 header 資訊，回退保守策略
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
