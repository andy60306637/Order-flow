"""
策略容量分析模組。

提供 Taker-only / Volume-based / Tier-2 容量估算：
  - 平方根市場衝擊模型 (impact_bps = eta * sigma * sqrt(Q / ADV) * 10000)
  - 成交量參與率 (VPR = position_qty / bar_volume_qty)
  - 多組資金掃描 (capital sweep)
"""
from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from backtest.engine import BacktestConfig, simulate_trades
from core import kline_cache, tick_cache
from strategies.base import StrategySignal

# ── raw kline 欄位索引（與 Binance REST 一致）────────────────────────────
_COL_OPEN_TIME   = 0
_COL_OPEN        = 1
_COL_HIGH        = 2
_COL_LOW         = 3
_COL_CLOSE       = 4
_COL_VOLUME      = 5   # base asset volume
_COL_CLOSE_TIME  = 6
_COL_QUOTE_VOL   = 7   # quote volume (USDT)
_COL_COUNT       = 8
_COL_TBV         = 9   # taker buy volume
_COL_TBQV        = 10  # taker buy quote volume

# ── 一天的毫秒數 ────────────────────────────────────────────────────────
_DAY_MS = 86_400_000


# ═══════════════════════════════════════════════════════════════════════════
# 資料結構
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CapacityConfig:
    impact_eta: float = 1.0
    adv_window_days: int = 30
    vpr_warn_pct: float = 0.01
    vpr_cap_pct: float = 0.05
    limit_drop_pct: float = 0.20
    capital_sweep: list[float] = field(default_factory=lambda: [
        1_000, 5_000, 10_000, 50_000, 100_000, 250_000, 500_000, 1_000_000
    ])


@dataclass
class CapacityPoint:
    capital: float
    profit_factor: float
    max_drawdown_pct: float
    total_net_pnl: float
    total_return_pct: float
    trades: int
    win_rate: float
    avg_impact_bps: float
    max_vpr: float
    avg_vpr: float
    warning_count: int


@dataclass
class CapacityReport:
    points: list[CapacityPoint]
    baseline_capital: float
    capacity_limit_usdt: float | None
    recommended_capital: float | None
    baseline_profit_factor: float
    notes: list[str]


# ═══════════════════════════════════════════════════════════════════════════
# CapacityAnalyzer
# ═══════════════════════════════════════════════════════════════════════════

class CapacityAnalyzer:
    """容量分析核心。所有市場資料直接從 raw cache 讀取。"""

    # ── 資料讀取 ───────────────────────────────────────────────────

    @staticmethod
    def load_raw_klines(symbol: str, interval: str) -> np.ndarray:
        """從 kline_cache 讀取 raw ndarray，shape (N, 12)。"""
        path = kline_cache.cache_path(symbol, interval)
        if not path.exists():
            return np.empty((0, 12), dtype=np.float64)
        return np.load(str(path))

    # ── 日線聚合 ──────────────────────────────────────────────────

    @staticmethod
    def _aggregate_to_daily(raw: np.ndarray) -> np.ndarray:
        """將任意 interval 的 raw klines 聚合為日線。

        回傳 shape (D, 6)：[day_open_time, open, high, low, close, quote_volume]
        """
        if len(raw) == 0:
            return np.empty((0, 6), dtype=np.float64)

        open_times = raw[:, _COL_OPEN_TIME]
        # 利用整除把同一天的 K 棒分組
        day_keys = (open_times // _DAY_MS).astype(np.int64)
        unique_days = np.unique(day_keys)

        daily = np.empty((len(unique_days), 6), dtype=np.float64)
        for i, dk in enumerate(unique_days):
            mask = day_keys == dk
            group = raw[mask]
            daily[i, 0] = group[0, _COL_OPEN_TIME]       # day open time
            daily[i, 1] = group[0, _COL_OPEN]             # open
            daily[i, 2] = group[:, _COL_HIGH].max()       # high
            daily[i, 3] = group[:, _COL_LOW].min()        # low
            daily[i, 4] = group[-1, _COL_CLOSE]           # close
            daily[i, 5] = group[:, _COL_QUOTE_VOL].sum()  # quote volume
        return daily

    # ── ADV（日均成交額）────────────────────────────────────────

    @staticmethod
    def calc_adv(raw_klines: np.ndarray, window_days: int = 30) -> float:
        """計算日均成交額 (USDT)。使用 raw kline 的 quote_volume。"""
        if len(raw_klines) == 0:
            return 0.0

        daily = CapacityAnalyzer._aggregate_to_daily(raw_klines)
        if len(daily) == 0:
            return 0.0

        # 取最近 window_days
        daily_qv = daily[-window_days:, 5]
        return float(daily_qv.mean()) if len(daily_qv) > 0 else 0.0

    # ── 日波動率 ──────────────────────────────────────────────────

    @staticmethod
    def calc_daily_volatility(raw_klines: np.ndarray) -> float:
        """計算日報酬率標準差（小數）。使用 daily close-to-close return。"""
        if len(raw_klines) == 0:
            return 0.0

        daily = CapacityAnalyzer._aggregate_to_daily(raw_klines)
        if len(daily) < 2:
            return 0.0

        closes = daily[:, 4]
        returns = np.diff(np.log(closes))
        return float(np.std(returns, ddof=1)) if len(returns) > 0 else 0.0

    # ── 市場衝擊 ──────────────────────────────────────────────────

    @staticmethod
    def calc_impact_bps(entry_notional: float, adv: float,
                        sigma_daily_frac: float, eta: float = 1.0) -> float:
        """平方根市場衝擊模型。

        impact_bps = eta * sigma_daily_frac * sqrt(Q / ADV) * 10000

        Args:
            entry_notional: 單筆名目值 (USDT)
            adv: 日均成交額 (USDT)
            sigma_daily_frac: 日報酬率標準差（小數）
            eta: 可調係數

        Returns:
            impact_bps (float): 衝擊 bps
        """
        if adv <= 0 or sigma_daily_frac <= 0:
            return 0.0
        return eta * sigma_daily_frac * math.sqrt(entry_notional / adv) * 10_000

    # ── VPR（成交量參與率）───────────────────────────────────────

    @staticmethod
    def calc_vpr_from_bars(trade_list: list[dict],
                           raw_klines: np.ndarray) -> list[dict]:
        """以 K 線 volume 計算每筆交易的 VPR。

        回傳每筆交易的 dict，額外含 'vpr' 欄位。
        """
        # 建立 open_time → volume 映射
        bar_vol_map: dict[int, float] = {}
        if len(raw_klines) > 0:
            for row in raw_klines:
                ot = int(row[_COL_OPEN_TIME])
                bar_vol_map[ot] = float(row[_COL_VOLUME])

        results = []
        for t in trade_list:
            if t.get("skipped"):
                results.append({**t, "vpr": 0.0})
                continue
            qty = t.get("qty", 0.0)
            entry_time = t.get("entry_time", 0)
            bar_vol = bar_vol_map.get(entry_time, 0.0)
            vpr = qty / bar_vol if bar_vol > 0 else 0.0
            results.append({**t, "vpr": vpr})
        return results

    # ── Tick-Level VPR（Phase 2）─────────────────────────────────

    @staticmethod
    def calc_vpr_from_ticks(trade_list: list[dict],
                            raw_klines: np.ndarray,
                            symbol: str) -> tuple[list[dict], int]:
        """以 tick 真實棒量計算 VPR，缺 tick 時 fallback 至 K 線 volume。

        Returns:
            (enriched_trade_list, fallback_count)
        """
        # 嘗試載入 tick 快取
        tick_data, _ = tick_cache.load_raw(symbol)
        has_ticks = tick_data is not None and len(tick_data) > 0

        # 建立 kline bar times 與 bar_map
        kline_times = []
        if len(raw_klines) > 0:
            for row in raw_klines:
                kline_times.append((int(row[_COL_OPEN_TIME]),
                                    int(row[_COL_CLOSE_TIME])))

        tick_bar_vol: dict[int, float] = {}
        if has_ticks:
            bar_map = tick_cache.build_bar_map(tick_data, kline_times)
            for ot, ticks in bar_map.items():
                tick_bar_vol[ot] = float(ticks[:, 2].sum())  # col 2 = qty

        # K 線 fallback map
        kline_vol_map: dict[int, float] = {}
        if len(raw_klines) > 0:
            for row in raw_klines:
                kline_vol_map[int(row[_COL_OPEN_TIME])] = float(row[_COL_VOLUME])

        fallback_count = 0
        results = []
        for t in trade_list:
            if t.get("skipped"):
                results.append({**t, "vpr": 0.0})
                continue
            qty = t.get("qty", 0.0)
            entry_time = t.get("entry_time", 0)

            if entry_time in tick_bar_vol:
                bar_vol = tick_bar_vol[entry_time]
            else:
                bar_vol = kline_vol_map.get(entry_time, 0.0)
                if has_ticks:
                    fallback_count += 1

            vpr = qty / bar_vol if bar_vol > 0 else 0.0
            results.append({**t, "vpr": vpr})
        return results, fallback_count

    # ── 掃描 ──────────────────────────────────────────────────────

    def run_sweep(
        self,
        signals: list[StrategySignal],
        base_cfg: BacktestConfig,
        cap_cfg: CapacityConfig,
        symbol: str,
        interval: str,
    ) -> CapacityReport:
        """對同一組 signals 以多組資金水位掃描，回傳 CapacityReport。"""

        raw_klines = self.load_raw_klines(symbol, interval)
        adv = self.calc_adv(raw_klines, cap_cfg.adv_window_days)
        sigma = self.calc_daily_volatility(raw_klines)

        notes: list[str] = []
        if adv <= 0:
            notes.append("ADV=0，無法計算市場衝擊；impact_bps 將全部為 0")
        if sigma <= 0:
            notes.append("日波動率=0，無法計算市場衝擊")

        # 建構 dynamic_slippage callback
        def make_dynamic_slip(eta: float):
            def _slip(provisional_notional: float, entry_time_ms: int) -> float:
                return CapacityAnalyzer.calc_impact_bps(
                    provisional_notional, adv, sigma, eta
                )
            return _slip

        points: list[CapacityPoint] = []

        for capital in cap_cfg.capital_sweep:
            cfg = deepcopy(base_cfg)
            cfg.initial_capital = capital
            cfg.dynamic_slippage = make_dynamic_slip(cap_cfg.impact_eta)

            result = simulate_trades(signals, cfg)
            tl = result.get("trade_list", [])

            # 計算 VPR（優先用 tick，若無則 fallback bar）
            enriched_tl, fb_count = self.calc_vpr_from_ticks(
                tl, raw_klines, symbol
            )

            # 統計
            active = [t for t in enriched_tl if not t.get("skipped")]
            vprs = [t["vpr"] for t in active]
            impact_bps_list = [t.get("impact_bps", 0.0) for t in active]
            warnings = sum(1 for v in vprs if v >= cap_cfg.vpr_warn_pct)

            pt = CapacityPoint(
                capital=capital,
                profit_factor=result.get("profit_factor", 0.0),
                max_drawdown_pct=result.get("max_drawdown_pct", 0.0),
                total_net_pnl=result.get("total_net_pnl", 0.0),
                total_return_pct=result.get("total_return_pct", 0.0),
                trades=result.get("trades", 0),
                win_rate=result.get("win_rate", 0.0),
                avg_impact_bps=float(np.mean(impact_bps_list)) if impact_bps_list else 0.0,
                max_vpr=max(vprs) if vprs else 0.0,
                avg_vpr=float(np.mean(vprs)) if vprs else 0.0,
                warning_count=warnings,
            )
            points.append(pt)

            if fb_count > 0:
                notes.append(
                    f"capital={capital}: {fb_count} 筆交易 VPR 使用 K 線 fallback"
                )

        # ── 基準與容量上限 ──────────────────────────────────────
        # 取 sweep 中最小資本作為基準
        baseline_pf = points[0].profit_factor if points else 0.0
        baseline_capital = cap_cfg.capital_sweep[0] if cap_cfg.capital_sweep else 0.0

        # 容量上限: 最大 capital 使得 PF >= baseline_PF * (1 - drop%)
        # 且 max_vpr < vpr_cap
        threshold_pf = baseline_pf * (1 - cap_cfg.limit_drop_pct)
        capacity_limit: float | None = None
        recommended: float | None = None
        best_pf = -1.0

        for pt in points:
            within_pf = pt.profit_factor >= threshold_pf
            within_vpr = pt.max_vpr < cap_cfg.vpr_cap_pct
            if within_pf and within_vpr:
                capacity_limit = pt.capital
                if pt.profit_factor > best_pf:
                    best_pf = pt.profit_factor
                    recommended = pt.capital

        return CapacityReport(
            points=points,
            baseline_capital=baseline_capital,
            capacity_limit_usdt=capacity_limit,
            recommended_capital=recommended,
            baseline_profit_factor=baseline_pf,
            notes=notes,
        )
