from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from backtest.time_slice import TimeSlice
from core.data_types import Kline
from research.base import klines_to_arrays
from research.registry import ensure_builtin_factors, get_factor
from strategies.base import TickBarMap


@dataclass
class ResearchConfig:
    symbol: str
    interval: str
    slices: list[TimeSlice]
    factor_names: list[str]
    horizons: list[int] = field(default_factory=lambda: [1, 3, 6, 12])
    quantiles: int = 5
    use_tick_features: bool = True


@dataclass
class ResearchResult:
    summary: list[dict[str, Any]]
    metrics: list[dict[str, Any]]
    quantiles: list[dict[str, Any]]
    stability_monthly: list[dict[str, Any]]
    stability_yearly: list[dict[str, Any]]
    unavailable: list[dict[str, str]]
    rows: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "metrics": self.metrics,
            "quantiles": self.quantiles,
            "stability_monthly": self.stability_monthly,
            "stability_yearly": self.stability_yearly,
            "unavailable": self.unavailable,
            "rows": self.rows,
        }


def run_research(
    config: ResearchConfig,
    klines: list[Kline] | None = None,
    tick_map: TickBarMap | None = None,
) -> ResearchResult:
    ensure_builtin_factors()
    if klines is None:
        klines, tick_map = load_research_data(config)
    tick_map = tick_map if config.use_tick_features else None
    return analyze_factors(
        klines=klines,
        tick_map=tick_map,
        factor_names=config.factor_names,
        horizons=config.horizons,
        quantiles=config.quantiles,
        use_tick_features=config.use_tick_features,
    )


def load_research_data(config: ResearchConfig) -> tuple[list[Kline], TickBarMap | None]:
    from core.kline_cache import load_range_as_klines
    from core.tick_cache import build_bar_map, load_range_sharded

    all_klines: list[Kline] = []
    tick_parts: list[np.ndarray] = []

    for sl in config.slices:
        segment_symbols = getattr(sl, "segment_symbols", []) or []
        for idx, (start_ms, end_ms) in enumerate(sl.segments):
            tick_symbol = segment_symbols[idx] if idx < len(segment_symbols) else config.symbol
            all_klines.extend(load_range_as_klines(config.symbol, config.interval, start_ms, end_ms))
            if config.use_tick_features:
                ticks = load_range_sharded(tick_symbol, start_ms, end_ms)
                if ticks is not None and len(ticks) > 0:
                    tick_parts.append(ticks)

    seen: dict[int, Kline] = {}
    for k in all_klines:
        seen[k.open_time] = k
    klines = [seen[t] for t in sorted(seen)]

    tick_map = None
    if config.use_tick_features and tick_parts and klines:
        ticks = np.concatenate(tick_parts, axis=0) if len(tick_parts) > 1 else tick_parts[0]
        ticks = ticks[ticks[:, 0].argsort()]
        tick_map = build_bar_map(ticks, [(k.open_time, k.close_time) for k in klines])
    return klines, tick_map


def analyze_factors(
    klines: list[Kline],
    tick_map: TickBarMap | None,
    factor_names: list[str],
    horizons: list[int],
    quantiles: int,
    use_tick_features: bool = True,
) -> ResearchResult:
    ensure_builtin_factors()
    arr = klines_to_arrays(klines) if klines else {}
    close = arr.get("close", np.array([], dtype=np.float64))
    open_times = arr.get("open_time", np.array([], dtype=np.int64))
    fwd = {h: _forward_return(close, h) for h in horizons}
    summary: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    qrows: list[dict[str, Any]] = []
    monthly: list[dict[str, Any]] = []
    yearly: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []

    for name in factor_names:
        factor = get_factor(name)
        if factor is None:
            unavailable.append({"factor": name, "reason": "not_registered"})
            continue
        if factor.requires_ticks and (not use_tick_features or tick_map is None):
            unavailable.append({"factor": name, "reason": "tick_data_unavailable"})
            continue

        values = factor.compute(klines, tick_map)
        factor_metrics: list[dict[str, Any]] = []
        for horizon, returns in fwd.items():
            metric = _metric_row(name, horizon, values, returns)
            metrics.append(metric)
            factor_metrics.append(metric)
            qrows.extend(_quantile_rows(name, horizon, values, returns, quantiles))
            monthly.extend(_stability_rows(name, horizon, values, returns, open_times, "month", quantiles))
            yearly.extend(_stability_rows(name, horizon, values, returns, open_times, "year", quantiles))
        summary.append(_summary_row(name, factor.requires_ticks, factor_metrics))

    summary.sort(key=lambda r: abs(r.get("best_rank_ic", 0.0)), reverse=True)
    return ResearchResult(
        summary=summary,
        metrics=metrics,
        quantiles=qrows,
        stability_monthly=monthly,
        stability_yearly=yearly,
        unavailable=unavailable,
        rows=len(klines),
    )


def _forward_return(close: np.ndarray, horizon: int) -> np.ndarray:
    out = np.full(close.shape, np.nan, dtype=np.float64)
    if horizon <= 0 or len(close) <= horizon:
        return out
    out[:-horizon] = close[horizon:] / close[:-horizon] - 1.0
    return out


def _metric_row(factor: str, horizon: int, values: np.ndarray, returns: np.ndarray) -> dict[str, Any]:
    mask = _valid_mask(values, returns)
    x = values[mask]
    y = returns[mask]
    return {
        "factor": factor,
        "horizon": horizon,
        "ic": _corr(x, y),
        "rank_ic": _corr(_rank(x), _rank(y)),
        "sample_count": int(mask.sum()),
    }


def _summary_row(factor: str, requires_ticks: bool, metrics: list[dict[str, Any]]) -> dict[str, Any]:
    if not metrics:
        return {
            "factor": factor,
            "requires_ticks": requires_ticks,
            "best_horizon": "",
            "best_ic": 0.0,
            "best_rank_ic": 0.0,
            "avg_abs_rank_ic": 0.0,
            "sample_count": 0,
        }
    best = max(metrics, key=lambda r: abs(float(r["rank_ic"])))
    return {
        "factor": factor,
        "requires_ticks": requires_ticks,
        "best_horizon": best["horizon"],
        "best_ic": best["ic"],
        "best_rank_ic": best["rank_ic"],
        "avg_abs_rank_ic": float(np.nanmean([abs(float(r["rank_ic"])) for r in metrics])),
        "sample_count": max(int(r["sample_count"]) for r in metrics),
    }


def _quantile_rows(
    factor: str,
    horizon: int,
    values: np.ndarray,
    returns: np.ndarray,
    quantiles: int,
) -> list[dict[str, Any]]:
    mask = _valid_mask(values, returns)
    if int(mask.sum()) < quantiles or quantiles < 2:
        return []
    x = values[mask]
    y = returns[mask]
    order = np.argsort(x, kind="stable")
    buckets = np.array_split(order, quantiles)
    rows: list[dict[str, Any]] = []
    means: list[float] = []
    for idx, bucket in enumerate(buckets, start=1):
        vals = y[bucket]
        mean = float(np.mean(vals)) if len(vals) else np.nan
        means.append(mean)
        rows.append({
            "factor": factor,
            "horizon": horizon,
            "quantile": idx,
            "mean_return": mean,
            "win_rate": float(np.mean(vals > 0) * 100.0) if len(vals) else np.nan,
            "sample_count": int(len(vals)),
            "spread_qhigh_qlow": "",
        })
    if len(means) >= 2:
        spread = means[-1] - means[0]
        for row in rows:
            row["spread_qhigh_qlow"] = spread
    return rows


def _stability_rows(
    factor: str,
    horizon: int,
    values: np.ndarray,
    returns: np.ndarray,
    open_times: np.ndarray,
    granularity: str,
    quantiles: int,
) -> list[dict[str, Any]]:
    if len(open_times) == 0:
        return []
    keys = np.array([_period_key(int(ts), granularity) for ts in open_times], dtype=object)
    rows: list[dict[str, Any]] = []
    for period in sorted(set(keys)):
        period_mask = keys == period
        mask = period_mask & _valid_mask(values, returns)
        x = values[mask]
        y = returns[mask]
        if len(x) < 3:
            continue
        qrows = _quantile_rows(factor, horizon, values[mask], returns[mask], quantiles)
        spread = qrows[0]["spread_qhigh_qlow"] if qrows else ""
        rows.append({
            "factor": factor,
            "horizon": horizon,
            "period": period,
            "ic": _corr(x, y),
            "rank_ic": _corr(_rank(x), _rank(y)),
            "spread_qhigh_qlow": spread,
            "sample_count": int(len(x)),
        })
    return rows


def _period_key(ts_ms: int, granularity: str) -> str:
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return f"{dt.year:04d}" if granularity == "year" else f"{dt.year:04d}-{dt.month:02d}"


def _valid_mask(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    return np.isfinite(x) & np.isfinite(y)


def _corr(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3 or len(y) < 3:
        return 0.0
    sx = float(np.std(x))
    sy = float(np.std(y))
    if sx == 0.0 or sy == 0.0:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def _rank(values: np.ndarray) -> np.ndarray:
    if len(values) == 0:
        return values.astype(np.float64)
    order = np.argsort(values, kind="stable")
    ranks = np.empty(len(values), dtype=np.float64)
    ranks[order] = np.arange(len(values), dtype=np.float64)
    sorted_values = values[order]
    start = 0
    while start < len(values):
        end = start + 1
        while end < len(values) and sorted_values[end] == sorted_values[start]:
            end += 1
        if end - start > 1:
            avg = float(start + end - 1) / 2.0
            ranks[order[start:end]] = avg
        start = end
    return ranks
