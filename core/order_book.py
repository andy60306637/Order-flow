"""
本地 Order Book 維護。

完整實作 Binance 官方文件「How to manage a local order book」演算法：
  1. 緩衝 depthUpdate 事件
  2. 收到 REST 快照後對齊 lastUpdateId
  3. 逐筆應用 diff，Q=0 則移除檔位
  4. 偵測到跳號時標記為未初始化（等待外部重新取快照）
"""
from __future__ import annotations
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)


class OrderBook:
    def __init__(self) -> None:
        self._bids: dict[float, float] = {}   # price → qty
        self._asks: dict[float, float] = {}
        self._last_update_id: int = 0
        self._initialized: bool = False
        self._buffer: list[dict] = []

    # ──────────────────────────────────────────────────────────────────────────
    def reset(self) -> None:
        self._bids.clear()
        self._asks.clear()
        self._last_update_id = 0
        self._initialized = False
        self._buffer.clear()

    # ──────────────────────────────────────────────────────────────────────────
    def init_snapshot(self, data: dict) -> None:
        """
        以 REST 快照初始化帳本。
        data 格式：{"lastUpdateId": int, "bids": [["price","qty"],...], "asks": [...]}
        """
        self._bids = {float(p): float(q) for p, q in data["bids"]}
        self._asks = {float(p): float(q) for p, q in data["asks"]}
        self._last_update_id = int(data["lastUpdateId"])
        self._initialized = True
        logger.debug("OB snapshot applied, lastUpdateId=%d", self._last_update_id)

        # 依照 Binance 文件：丟棄所有 u <= lastUpdateId 的緩衝事件
        pending = [e for e in self._buffer if e["u"] > self._last_update_id]
        self._buffer.clear()
        for event in pending:
            self._apply(event)

    # ──────────────────────────────────────────────────────────────────────────
    def apply_diff(self, data: dict) -> bool:
        """
        應用 WebSocket depthUpdate 事件。
        回傳 True 表示偵測到事件跳號，需要外部重新取快照。
        """
        if not self._initialized:
            self._buffer.append(data)
            return False
        return self._apply(data)

    def _apply(self, data: dict) -> bool:
        u = int(data["u"])
        U = int(data["U"])
        lid = self._last_update_id

        # 已過時的事件，直接丟棄
        if u <= lid:
            return False

        # 連續性檢查
        # Futures 串流提供 pu（前一個 final update ID），連續條件為 pu <= lid
        # Spot 串流沒有 pu，改用 U <= lid+1
        if "pu" in data:
            pu = int(data["pu"])
            if pu > lid:
                logger.warning(
                    "OB gap: expected pu<=%d, got pu=%d. Reinitializing.", lid, pu
                )
                self._initialized = False
                self._buffer = [data]
                return True
        else:
            if U > lid + 1:
                logger.warning(
                    "OB gap: expected U<=%d, got U=%d. Reinitializing.", lid + 1, U
                )
                self._initialized = False
                self._buffer = [data]
                return True

        for price_s, qty_s in data["b"]:
            p, q = float(price_s), float(qty_s)
            if q == 0:
                self._bids.pop(p, None)
            else:
                self._bids[p] = q

        for price_s, qty_s in data["a"]:
            p, q = float(price_s), float(qty_s)
            if q == 0:
                self._asks.pop(p, None)
            else:
                self._asks[p] = q

        self._last_update_id = u
        return False

    # ──────────────────────────────────────────────────────────────────────────
    def get_bids(self, n: int = 20) -> List[Tuple[float, float]]:
        """回傳前 n 檔買單，由高到低排序。"""
        return sorted(self._bids.items(), reverse=True)[:n]

    def get_asks(self, n: int = 20) -> List[Tuple[float, float]]:
        """回傳前 n 檔賣單，由低到高排序。"""
        return sorted(self._asks.items())[:n]

    def best_bid(self) -> float:
        return max(self._bids.keys()) if self._bids else 0.0

    def best_ask(self) -> float:
        return min(self._asks.keys()) if self._asks else 0.0

    def mid_price(self) -> float:
        bb, ba = self.best_bid(), self.best_ask()
        if bb and ba:
            return (bb + ba) / 2
        return bb or ba

    @property
    def is_initialized(self) -> bool:
        return self._initialized
