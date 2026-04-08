"""
回測模擬引擎：倉位計算、手續費、權益追蹤、多空分離統計。

公式約定：
  - 名目價值 (notional) = qty × price
  - 手續費 = notional × fee_rate（開倉 + 平倉各算一次）
  - 倉位大小取 min(風險限制, 槓桿限制)
  - 淨損益 = 毛損益 − 手續費
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from strategies.base import StrategySignal

# ── 費率常數 ────────────────────────────────────────────────────────────────
FEE_RATES = {"Maker": 0.0002, "Taker": 0.0005}


@dataclass
class BacktestConfig:
    """回測參數。"""
    initial_capital: float = 10_000.0   # USDT
    max_loss_pct:    float = 0.02       # 每筆最高損失比例（0.02 = 2%）
    leverage:        int   = 20
    fee_mode:        str   = "Taker"    # "Maker" | "Taker"


# ═══════════════════════════════════════════════════════════════════════════
# 核心：模擬交易
# ═══════════════════════════════════════════════════════════════════════════

def simulate_trades(signals: List[StrategySignal], cfg: BacktestConfig) -> dict:
    """
    依序配對進出場訊號，以 cfg 模擬倉位 / 手續費 / 權益變動。

    回傳 dict 包含：
      - 回測參數回顯
      - 整體統計（trades, win_rate, profit_factor …）
      - 多空分離統計
      - trade_list（每筆交易明細）
    """
    fee_rate = FEE_RATES[cfg.fee_mode]
    equity = cfg.initial_capital

    raw_trades, open_count = _pair_signals(signals)

    trade_list: List[dict] = []

    for rt in raw_trades:
        entry_p = rt["entry"]
        exit_p  = rt["exit"]
        stop_p  = rt.get("stop")
        d       = rt["dir"]

        qty = _calc_qty(equity, entry_p, stop_p, d, cfg.max_loss_pct, cfg.leverage)
        if qty is None:
            trade_list.append({
                "dir": d, "entry": entry_p, "exit": exit_p,
                "qty": 0.0, "total_fee": 0.0,
                "gross_pnl": 0.0, "net_pnl": 0.0,
                "equity_after": equity,
                "skipped": True,
                "skip_reason": "資金不足或停損距離無效",
            })
            continue

        entry_notional = qty * entry_p
        exit_notional  = qty * exit_p
        open_fee  = entry_notional * fee_rate
        close_fee = exit_notional * fee_rate
        total_fee = open_fee + close_fee

        if d == "long":
            gross_pnl = (exit_p - entry_p) * qty
        else:
            gross_pnl = (entry_p - exit_p) * qty

        net_pnl = gross_pnl - total_fee
        equity += net_pnl

        trade_list.append({
            "dir": d, "entry": entry_p, "exit": exit_p,
            "qty": qty, "total_fee": total_fee,
            "gross_pnl": gross_pnl, "net_pnl": net_pnl,
            "equity_after": equity,
            "skipped": False, "skip_reason": "",
        })

        if equity <= 0:
            break  # 爆倉

    return _build_stats(trade_list, cfg, equity, open_count)


# ═══════════════════════════════════════════════════════════════════════════
# 內部工具
# ═══════════════════════════════════════════════════════════════════════════

def _pair_signals(signals: List[StrategySignal]):
    """從訊號序列配對出原始交易清單（不含倉位 / 手續費）。"""
    trades: List[dict] = []
    open_long: Optional[float] = None
    open_short: Optional[float] = None
    long_stop: Optional[float] = None
    short_stop: Optional[float] = None
    open_count = 0

    for sig in signals:
        if sig.signal_type == "long_entry":
            if open_short is not None:
                trades.append({"dir": "short", "entry": open_short,
                               "exit": sig.price, "stop": short_stop})
                open_short = None
            if open_long is None:
                open_long = sig.price
                long_stop = sig.stop_price
        elif sig.signal_type == "long_exit":
            if open_long is not None:
                trades.append({"dir": "long", "entry": open_long,
                               "exit": sig.price, "stop": long_stop})
                open_long = None
        elif sig.signal_type == "short_entry":
            if open_long is not None:
                trades.append({"dir": "long", "entry": open_long,
                               "exit": sig.price, "stop": long_stop})
                open_long = None
            if open_short is None:
                open_short = sig.price
                short_stop = sig.stop_price
        elif sig.signal_type == "short_exit":
            if open_short is not None:
                trades.append({"dir": "short", "entry": open_short,
                               "exit": sig.price, "stop": short_stop})
                open_short = None

    if open_long is not None:
        open_count += 1
    if open_short is not None:
        open_count += 1

    return trades, open_count


def _calc_qty(
    equity: float,
    entry_price: float,
    stop_price: Optional[float],
    direction: str,
    max_loss_pct: float,
    leverage: int,
) -> Optional[float]:
    """
    計算倉位數量，取 min(風險限制, 槓桿限制)。
    回傳 None 表示無法開倉。
    """
    if equity <= 0 or entry_price <= 0:
        return None

    # ── 風險限制 ──────────────────────────────────────────────────
    if stop_price is not None:
        stop_dist = (entry_price - stop_price) if direction == "long" else (stop_price - entry_price)
        if stop_dist <= 0:
            return None
        max_loss = equity * max_loss_pct
        risk_qty = max_loss / stop_dist
    else:
        risk_qty = float("inf")

    # ── 槓桿限制 ──────────────────────────────────────────────────
    max_notional = equity * leverage
    lev_qty = max_notional / entry_price

    qty = min(risk_qty, lev_qty)
    return qty if qty > 0 else None


def _build_stats(
    trade_list: List[dict],
    cfg: BacktestConfig,
    final_equity: float,
    open_count: int,
) -> dict:
    """從模擬結果計算統計指標。"""
    active = [t for t in trade_list if not t.get("skipped")]
    n = len(active)

    empty = {
        "initial_capital": cfg.initial_capital,
        "final_equity": final_equity,
        "total_return_pct": 0.0,
        "leverage": cfg.leverage,
        "fee_mode": cfg.fee_mode,
        "fee_rate": FEE_RATES[cfg.fee_mode],
        "max_loss_pct": cfg.max_loss_pct,
        "trades": 0, "win_rate": 0.0,
        "total_net_pnl": 0.0, "total_fees": 0.0,
        "profit_factor": 0.0,
        "max_consec_loss": 0, "max_drawdown_pct": 0.0,
        "long_trades": 0, "long_win_rate": 0.0, "long_profit_factor": 0.0,
        "short_trades": 0, "short_win_rate": 0.0, "short_profit_factor": 0.0,
        "open_count": open_count,
        "trade_list": trade_list,
    }
    if n == 0:
        return empty

    wins = sum(1 for t in active if t["net_pnl"] > 0)
    total_net = sum(t["net_pnl"] for t in active)
    total_fees = sum(t["total_fee"] for t in active)

    gp = sum(t["net_pnl"] for t in active if t["net_pnl"] > 0)
    gl = abs(sum(t["net_pnl"] for t in active if t["net_pnl"] < 0))
    pf = gp / gl if gl > 0 else float("inf")

    # ── 最大連續虧損 ──────────────────────────────────────────────
    max_cl = cur_cl = 0
    for t in active:
        if t["net_pnl"] < 0:
            cur_cl += 1
            max_cl = max(max_cl, cur_cl)
        else:
            cur_cl = 0

    # ── 最大回撤 (%) ─────────────────────────────────────────────
    eq = cfg.initial_capital
    peak = eq
    max_dd = 0.0
    for t in active:
        eq += t["net_pnl"]
        if eq > peak:
            peak = eq
        dd_pct = (peak - eq) / peak * 100 if peak > 0 else 0.0
        if dd_pct > max_dd:
            max_dd = dd_pct

    # ── 多空分離 ──────────────────────────────────────────────────
    def _side(side_trades):
        sn = len(side_trades)
        if sn == 0:
            return 0, 0.0, 0.0
        sw = sum(1 for t in side_trades if t["net_pnl"] > 0)
        sp = sum(t["net_pnl"] for t in side_trades if t["net_pnl"] > 0)
        sl = abs(sum(t["net_pnl"] for t in side_trades if t["net_pnl"] < 0))
        return sn, sw / sn * 100, (sp / sl if sl > 0 else float("inf"))

    ln, lwr, lpf = _side([t for t in active if t["dir"] == "long"])
    sn, swr, spf = _side([t for t in active if t["dir"] == "short"])

    return {
        "initial_capital": cfg.initial_capital,
        "final_equity": final_equity,
        "total_return_pct": (final_equity - cfg.initial_capital) / cfg.initial_capital * 100,
        "leverage": cfg.leverage,
        "fee_mode": cfg.fee_mode,
        "fee_rate": FEE_RATES[cfg.fee_mode],
        "max_loss_pct": cfg.max_loss_pct,
        "trades": n,
        "win_rate": wins / n * 100,
        "total_net_pnl": total_net,
        "total_fees": total_fees,
        "profit_factor": pf,
        "max_consec_loss": max_cl,
        "max_drawdown_pct": max_dd,
        "long_trades": ln, "long_win_rate": lwr, "long_profit_factor": lpf,
        "short_trades": sn, "short_win_rate": swr, "short_profit_factor": spf,
        "open_count": open_count,
        "trade_list": trade_list,
    }
