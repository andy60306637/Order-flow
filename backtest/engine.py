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
FEE_RATES = {
    "Maker":         0.0002,
    "Taker":         0.0005,
    "100% Maker":    0.0002,              # 成本情境：全 Maker
    "70M/30T":       0.0002*0.7 + 0.0005*0.3,   # 成本情境：70% Maker / 30% Taker
    "50M/50T":       0.0002*0.5 + 0.0005*0.5,   # 成本情境：50% Maker / 50% Taker
}


@dataclass
class BacktestConfig:
    """回測參數。"""
    initial_capital: float = 10_000.0   # USDT
    max_loss_pct:    float = 0.02       # 每筆最高損失比例（0.02 = 2%）
    leverage:        int   = 20
    fee_mode:        str   = "Taker"    # "Maker" | "Taker"
    slippage_bps:    float = 0.0        # 滑價 bps（1 bps = 0.01% = 0.0001）
    funding_rate:    float = 0.0        # 資金費率 (0.01% per 8h)；0 = 不計
    maint_margin:    float = 0.005       # 維持保證金率 (0.5%)
    compound:        bool  = True        # True=複利（動態 equity），False=固定初始資金


FUNDING_INTERVAL_MS = 8 * 3600 * 1000   # 8 小時 (ms)


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

    slip = cfg.slippage_bps * 1e-4   # bps → 小數

    for rt in raw_trades:
        entry_p = rt["entry"]
        exit_p  = rt["exit"]
        stop_p  = rt.get("stop")
        d       = rt["dir"]

        # ── 滑價 ──────────────────────────────────────────────────
        if slip:
            if d == "long":
                entry_p *= (1 + slip)   # 買入更貴
                exit_p  *= (1 - slip)   # 賣出更便宜
            else:
                entry_p *= (1 - slip)   # 做空進場更低
                exit_p  *= (1 + slip)   # 做空平倉更高

        qty = _calc_qty(
            equity if cfg.compound else cfg.initial_capital,
            entry_p, stop_p, d, cfg.max_loss_pct, cfg.leverage
        )
        if qty is None:
            trade_list.append({
                "dir": d, "entry": entry_p, "exit": exit_p,
                "entry_time": rt.get("entry_time", 0),
                "exit_time": rt.get("exit_time", 0),
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

        # ── 資金費 (funding fee) ────────────────────────────────
        entry_time = rt.get("entry_time", 0)
        exit_time  = rt.get("exit_time", 0)
        funding_cost = 0.0
        if cfg.funding_rate and entry_time is not None and exit_time is not None:
            hold_ms = exit_time - entry_time
            n_fundings = int(hold_ms // FUNDING_INTERVAL_MS)
            funding_cost = n_fundings * entry_notional * cfg.funding_rate

        # ── 資金費方向：多單支付（扣費），空單收取（回收）────────────
        if d == "short":
            funding_cost = -funding_cost

        net_pnl = gross_pnl - total_fee - funding_cost
        equity += net_pnl

        # ── 維持保證金檢查（簡化爆倉）────────────────────
        maint_req = entry_notional * cfg.maint_margin
        liquidated = equity < maint_req

        trade_list.append({
            "dir": d, "entry": entry_p, "exit": exit_p,
            "entry_time": rt.get("entry_time", 0),
            "exit_time": rt.get("exit_time", 0),
            "qty": qty, "total_fee": total_fee,
            "funding_cost": funding_cost,
            "gross_pnl": gross_pnl, "net_pnl": net_pnl,
            "equity_after": equity,
            "exit_label": rt.get("exit_label", ""),
            "skipped": False, "skip_reason": "",
            "liquidated": liquidated,
        })

        if equity <= 0 or liquidated:
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
    long_time: int = 0
    short_time: int = 0
    open_count = 0

    for sig in signals:
        fp = sig.fill_price or sig.price   # 優先使用實際成交價
        if sig.signal_type == "long_entry":
            if open_short is not None:
                trades.append({"dir": "short", "entry": open_short,
                               "exit": fp, "stop": short_stop,
                               "entry_time": short_time, "exit_time": sig.open_time})
                open_short = None
            if open_long is None:
                open_long = fp
                long_stop = sig.stop_price
                long_time = sig.open_time
        elif sig.signal_type == "long_exit":
            if open_long is not None:
                trades.append({"dir": "long", "entry": open_long,
                               "exit": fp, "stop": long_stop,
                               "entry_time": long_time, "exit_time": sig.open_time,
                               "exit_label": sig.label})
                open_long = None
        elif sig.signal_type == "short_entry":
            if open_long is not None:
                trades.append({"dir": "long", "entry": open_long,
                               "exit": fp, "stop": long_stop,
                               "entry_time": long_time, "exit_time": sig.open_time})
                open_long = None
            if open_short is None:
                open_short = fp
                short_stop = sig.stop_price
                short_time = sig.open_time
        elif sig.signal_type == "short_exit":
            if open_short is not None:
                trades.append({"dir": "short", "entry": open_short,
                               "exit": fp, "stop": short_stop,
                               "entry_time": short_time, "exit_time": sig.open_time,
                               "exit_label": sig.label})
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
        "total_funding": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0,
        "sl_count": 0, "tp_count": 0, "ts_count": 0, "td_count": 0,
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
    total_funding = sum(t.get("funding_cost", 0.0) for t in active)

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

    wins_pnl = [t["net_pnl"] for t in active if t["net_pnl"] > 0]
    loss_pnl = [abs(t["net_pnl"]) for t in active if t["net_pnl"] < 0]
    avg_win  = sum(wins_pnl) / len(wins_pnl) if wins_pnl else 0.0
    avg_loss = sum(loss_pnl) / len(loss_pnl) if loss_pnl else 0.0

    sl_count = sum(1 for t in active if t.get("exit_label") == "SL")
    tp_count = sum(1 for t in active if t.get("exit_label") == "TP")
    ts_count = sum(1 for t in active if t.get("exit_label") == "TS")
    td_count = sum(1 for t in active if t.get("exit_label") == "TD")

    return {
        "initial_capital": cfg.initial_capital,
        "final_equity": final_equity,
        "total_return_pct": (final_equity - cfg.initial_capital) / cfg.initial_capital * 100,
        "leverage": cfg.leverage,
        "fee_mode": cfg.fee_mode,
        "fee_rate": FEE_RATES[cfg.fee_mode],
        "max_loss_pct": cfg.max_loss_pct,
        "slippage_bps": cfg.slippage_bps,
        "funding_rate": cfg.funding_rate,
        "total_funding": total_funding,
        "trades": n,
        "win_rate": wins / n * 100,
        "total_net_pnl": total_net,
        "total_fees": total_fees,
        "profit_factor": pf,
        "max_consec_loss": max_cl,
        "max_drawdown_pct": max_dd,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "sl_count": sl_count, "tp_count": tp_count,
        "ts_count": ts_count, "td_count": td_count,
        "long_trades": ln, "long_win_rate": lwr, "long_profit_factor": lpf,
        "short_trades": sn, "short_win_rate": swr, "short_profit_factor": spf,
        "open_count": open_count,
        "trade_list": trade_list,
    }


def compute_subset_stats(trades: list) -> dict:
    """從交易子集計算顯示統計（不重新模擬權益曲線）。"""
    active = [t for t in trades if not t.get("skipped")]
    n = len(active)
    empty = {
        "trades": 0, "win_rate": 0.0,
        "total_net_pnl": 0.0, "total_fees": 0.0,
        "profit_factor": 0.0, "max_consec_loss": 0,
        "max_drawdown_pct": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0,
        "sl_count": 0, "tp_count": 0, "ts_count": 0, "td_count": 0,
        "long_trades": 0, "long_profit_factor": 0.0,
        "short_trades": 0, "short_profit_factor": 0.0,
    }
    if n == 0:
        return empty

    wins = sum(1 for t in active if t["net_pnl"] > 0)
    total_net = sum(t["net_pnl"] for t in active)
    total_fees = sum(t["total_fee"] for t in active)

    gp = sum(t["net_pnl"] for t in active if t["net_pnl"] > 0)
    gl = abs(sum(t["net_pnl"] for t in active if t["net_pnl"] < 0))
    pf = gp / gl if gl > 0 else float("inf")

    max_cl = cur_cl = 0
    for t in active:
        if t["net_pnl"] < 0:
            cur_cl += 1
            max_cl = max(max_cl, cur_cl)
        else:
            cur_cl = 0

    # ── 最大回撤 ──────────────────────────────────────────────────
    # 以第一筆交易的 equity_after - net_pnl 還原子集起始資金，
    # 和 _build_stats 保持一致的計算基準（全集時精確吻合）。
    base_eq = active[0]["equity_after"] - active[0]["net_pnl"]
    eq = base_eq
    peak = base_eq
    max_dd = 0.0
    for t in active:
        eq += t["net_pnl"]
        if eq > peak:
            peak = eq
        if peak > 0:
            dd_pct = (peak - eq) / peak * 100
            if dd_pct > max_dd:
                max_dd = dd_pct

    wins_pnl = [t["net_pnl"] for t in active if t["net_pnl"] > 0]
    loss_pnl = [abs(t["net_pnl"]) for t in active if t["net_pnl"] < 0]
    avg_win = sum(wins_pnl) / len(wins_pnl) if wins_pnl else 0.0
    avg_loss = sum(loss_pnl) / len(loss_pnl) if loss_pnl else 0.0

    def _side(side_trades):
        sn = len(side_trades)
        if sn == 0:
            return 0, 0.0
        sp = sum(t["net_pnl"] for t in side_trades if t["net_pnl"] > 0)
        sl = abs(sum(t["net_pnl"] for t in side_trades if t["net_pnl"] < 0))
        return sn, (sp / sl if sl > 0 else float("inf"))

    ln, lpf = _side([t for t in active if t["dir"] == "long"])
    sn, spf = _side([t for t in active if t["dir"] == "short"])

    return {
        "trades": n, "win_rate": wins / n * 100,
        "total_net_pnl": total_net, "total_fees": total_fees,
        "profit_factor": pf, "max_consec_loss": max_cl,
        "max_drawdown_pct": max_dd,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "sl_count": sum(1 for t in active if t.get("exit_label") == "SL"),
        "tp_count": sum(1 for t in active if t.get("exit_label") == "TP"),
        "ts_count": sum(1 for t in active if t.get("exit_label") == "TS"),
        "td_count": sum(1 for t in active if t.get("exit_label") == "TD"),
        "long_trades": ln, "long_profit_factor": lpf,
        "short_trades": sn, "short_profit_factor": spf,
    }
