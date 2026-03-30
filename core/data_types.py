"""共用資料結構 (dataclasses)"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class Trade:
    symbol: str
    price: float
    qty: float
    is_buyer_maker: bool  # True = 賣方主動 (ask hit), False = 買方主動 (bid hit)
    trade_time: int       # ms epoch


@dataclass
class Kline:
    symbol: str
    interval: str
    open_time: int        # ms epoch
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float          # base asset volume
    taker_buy_volume: float
    is_closed: bool

    @classmethod
    def from_ws(cls, s: dict) -> "Kline":
        """從 WebSocket kline payload 的 'k' 欄位建立。"""
        k = s
        return cls(
            symbol=k["s"],
            interval=k["i"],
            open_time=k["t"],
            close_time=k["T"],
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            taker_buy_volume=float(k["V"]),
            is_closed=k["x"],
        )

    @classmethod
    def from_rest(cls, symbol: str, interval: str, row: list) -> "Kline":
        """從 REST /fapi/v1/klines 的單列建立。"""
        return cls(
            symbol=symbol,
            interval=interval,
            open_time=int(row[0]),
            close_time=int(row[6]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            taker_buy_volume=float(row[9]),
            is_closed=True,
        )


@dataclass
class FootprintLevel:
    """單一 K 棒中某個價格分桶的成交資訊。"""
    price: float
    bid_vol: float = 0.0   # 買方主動（taker buy）
    ask_vol: float = 0.0   # 賣方主動（taker sell）

    @property
    def delta(self) -> float:
        return self.bid_vol - self.ask_vol

    @property
    def total(self) -> float:
        return self.bid_vol + self.ask_vol

    @property
    def imbalance(self) -> float:
        """0=全賣, 0.5=均衡, 1=全買"""
        if self.total == 0:
            return 0.5
        return self.bid_vol / self.total


@dataclass
class FootprintCandle:
    """一根 Footprint K 棒。"""
    open_time: int
    levels: Dict[float, FootprintLevel] = field(default_factory=dict)
    closed: bool = False
    # 以下欄位在收到 kline 更新後填入
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0

    @property
    def delta(self) -> float:
        return sum(lv.delta for lv in self.levels.values())

    @property
    def total_volume(self) -> float:
        return sum(lv.total for lv in self.levels.values())

    @property
    def max_level_volume(self) -> float:
        if not self.levels:
            return 0.0
        return max(lv.total for lv in self.levels.values())

    @property
    def poc_price(self) -> float:
        """POC（Point of Control）= 最大成交量的價位。"""
        if not self.levels:
            return self.close if self.close else self.open
        return max(self.levels, key=lambda p: self.levels[p].total)
