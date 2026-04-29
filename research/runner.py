from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from backtest.time_slice import TimeSlice
from core.data_types import Kline
from research.base import (
    FACTOR_SIDE_LONG,
    FACTOR_SIDE_SHORT,
    factor_sides_label,
    klines_to_arrays,
)
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
    # Bars between signal and entry. lag=1 means: signal at close[i] -> enter at close[i+1].
    # Set to 0 only if you can transact at the signal bar's close.
    entry_lag: int = 1
    # Minimum sample size required within a stability sub-period to report metrics.
    min_period_samples: int = 30
    # Fraction of valid samples used as the in-sample (train) split for OOS quantile evaluation.
    train_ratio: float = 0.5
    # Granularity used to derive rolling-IC-based IR / t-stat.
    ic_period_granularity: str = "month"


@dataclass
class ResearchResult:
    summary: list[dict[str, Any]]
    metrics: list[dict[str, Any]]
    quantiles: list[dict[str, Any]]
    stability_monthly: list[dict[str, Any]]
    stability_yearly: list[dict[str, Any]]
    factor_correlations: list[dict[str, Any]]
    unavailable: list[dict[str, str]]
    rows: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "metrics": self.metrics,
            "quantiles": self.quantiles,
            "stability_monthly": self.stability_monthly,
            "stability_yearly": self.stability_yearly,
            "factor_correlations": self.factor_correlations,
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
        entry_lag=config.entry_lag,
        min_period_samples=config.min_period_samples,
        train_ratio=config.train_ratio,
        ic_period_granularity=config.ic_period_granularity,
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
    entry_lag: int = 1,
    min_period_samples: int = 30,
    train_ratio: float = 0.5,
    ic_period_granularity: str = "month",
) -> ResearchResult:
    ensure_builtin_factors()
    arr = klines_to_arrays(klines) if klines else {}
    close = arr.get("close", np.array([], dtype=np.float64))
    open_times = arr.get("open_time", np.array([], dtype=np.int64))
    fwd = {h: _forward_return(close, h, entry_lag) for h in horizons}
    summary: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    qrows: list[dict[str, Any]] = []
    monthly: list[dict[str, Any]] = []
    yearly: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    factor_values: dict[str, np.ndarray] = {}
    factor_orientations: dict[str, int] = {}

    for name in factor_names:
        factor = get_factor(name)
        if factor is None:
            unavailable.append({"factor": name, "reason": "not_registered"})
            continue
        if factor.requires_ticks and (not use_tick_features or tick_map is None):
            unavailable.append({"factor": name, "reason": "tick_data_unavailable"})
            continue

        values = factor.compute(klines, tick_map)
        factor_values[name] = values
        orientation = _factor_orientation(factor.sides)
        factor_orientations[name] = orientation
        factor_metrics: list[dict[str, Any]] = []
        for horizon, returns in fwd.items():
            period_ic = _per_period_ic(
                values, returns, open_times,
                granularity=ic_period_granularity,
                min_samples=min_period_samples,
            )
            metric = _metric_row(name, horizon, values, returns, orientation, period_ic)
            metrics.append(metric)
            factor_metrics.append(metric)
            qrows.extend(_quantile_rows_in_sample(name, horizon, values, returns, quantiles, orientation))
            qrows.extend(_quantile_rows_out_of_sample(name, horizon, values, returns, quantiles, orientation, train_ratio))
            monthly.extend(_stability_rows(name, horizon, values, returns, open_times, "month", quantiles, min_period_samples))
            yearly.extend(_stability_rows(name, horizon, values, returns, open_times, "year", quantiles, min_period_samples))
        summary.append(_summary_row(
            name,
            factor.requires_ticks,
            factor.sides,
            factor.group,
            orientation,
            factor_metrics,
        ))

    correlations = _factor_correlations(factor_values)
    summary.sort(key=lambda r: float(r.get("oriented_rank_ic", 0.0)), reverse=True)
    return ResearchResult(
        summary=summary,
        metrics=metrics,
        quantiles=qrows,
        stability_monthly=monthly,
        stability_yearly=yearly,
        factor_correlations=correlations,
        unavailable=unavailable,
        rows=len(klines),
    )


def _factor_orientation(sides: tuple[str, ...]) -> int:
    """+1 = higher value -> long edge, -1 = higher value -> short edge, 0 = ambiguous."""
    has_long = FACTOR_SIDE_LONG in sides
    has_short = FACTOR_SIDE_SHORT in sides
    if has_long and not has_short:
        return 1
    if has_short and not has_long:
        return -1
    return 0


def _orient(value: float, orientation: int) -> float:
    if not np.isfinite(value):
        return float("nan")
    if orientation == 0:
        return abs(value)
    return value * orientation


def _forward_return(close: np.ndarray, horizon: int, entry_lag: int) -> np.ndarray:
    """Return at index i = close[i+lag+h]/close[i+lag] - 1.

    With entry_lag=1, the factor computed at bar i (only knowable at i's close)
    is paired with the return realized between bar i+1's close and bar i+1+h's close,
    matching execution at the next bar's close.
    """
    out = np.full(close.shape, np.nan, dtype=np.float64)
    if horizon <= 0 or entry_lag < 0:
        return out
    n = len(close)
    end = n - entry_lag - horizon
    if end <= 0:
        return out
    entry = close[entry_lag:entry_lag + end]
    exit_ = close[entry_lag + horizon:entry_lag + horizon + end]
    out[:end] = exit_ / entry - 1.0
    return out


def _per_period_ic(
    values: np.ndarray,
    returns: np.ndarray,
    open_times: np.ndarray,
    granularity: str,
    min_samples: int,
) -> list[float]:
    """Rank IC per sub-period. Used to compute IR and t-stat for the (factor, horizon) pair."""
    if len(open_times) == 0:
        return []
    keys = np.array([_period_key(int(ts), granularity) for ts in open_times], dtype=object)
    out: list[float] = []
    for period in sorted(set(keys)):
        mask = (keys == period) & _valid_mask(values, returns)
        if int(mask.sum()) < min_samples:
            continue
        ic = _corr(_rank(values[mask]), _rank(returns[mask]))
        if np.isfinite(ic):
            out.append(float(ic))
    return out


def _metric_row(
    factor: str,
    horizon: int,
    values: np.ndarray,
    returns: np.ndarray,
    orientation: int,
    period_ic: list[float],
) -> dict[str, Any]:
    mask = _valid_mask(values, returns)
    x = values[mask]
    y = returns[mask]
    ic = _corr(x, y)
    rank_ic = _corr(_rank(x), _rank(y))
    if len(period_ic) >= 3:
        arr = np.array(period_ic, dtype=np.float64)
        mean_p = float(np.mean(arr))
        std_p = float(np.std(arr, ddof=1))
        ir = mean_p / std_p if std_p > 0 else 0.0
        t_stat = ir * np.sqrt(len(arr))
    else:
        mean_p = 0.0
        std_p = 0.0
        ir = 0.0
        t_stat = 0.0
    return {
        "factor": factor,
        "horizon": horizon,
        "ic": ic,
        "rank_ic": rank_ic,
        "oriented_rank_ic": _orient(rank_ic, orientation),
        "ic_period_mean": mean_p,
        "ic_period_std": std_p,
        "ic_ir": float(ir),
        "ic_t_stat": float(t_stat),
        "ic_periods": len(period_ic),
        "sample_count": int(mask.sum()),
    }


def _summary_row(
    factor: str,
    requires_ticks: bool,
    sides: tuple[str, ...],
    group: str,
    orientation: int,
    metrics: list[dict[str, Any]],
) -> dict[str, Any]:
    base = {
        "factor": factor,
        "requires_ticks": requires_ticks,
        "side": factor_sides_label(sides),
        "group": group,
        "orientation": orientation,
    }
    if not metrics:
        base.update({
            "best_horizon": "",
            "best_rank_ic": 0.0,
            "oriented_rank_ic": 0.0,
            "ic_ir": 0.0,
            "ic_t_stat": 0.0,
            "avg_abs_rank_ic": 0.0,
            "sample_count": 0,
        })
        return base
    best = max(metrics, key=lambda r: float(r["oriented_rank_ic"]))
    base.update({
        "best_horizon": best["horizon"],
        "best_rank_ic": best["rank_ic"],
        "oriented_rank_ic": best["oriented_rank_ic"],
        "ic_ir": best["ic_ir"],
        "ic_t_stat": best["ic_t_stat"],
        "avg_abs_rank_ic": float(np.nanmean([abs(float(r["rank_ic"])) for r in metrics])),
        "sample_count": max(int(r["sample_count"]) for r in metrics),
    })
    return base


def _quantile_rows_from_buckets(
    factor: str,
    horizon: int,
    sample_label: str,
    bucket_indices: list[np.ndarray],
    returns: np.ndarray,
    orientation: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    means: list[float] = []
    for q, idx in enumerate(bucket_indices, start=1):
        vals = returns[idx]
        mean = float(np.mean(vals)) if len(vals) else float("nan")
        means.append(mean)
        rows.append({
            "factor": factor,
            "horizon": horizon,
            "sample": sample_label,
            "quantile": q,
            "mean_return": mean,
            "win_rate": float(np.mean(vals > 0) * 100.0) if len(vals) else float("nan"),
            "sample_count": int(len(vals)),
            "spread_qhigh_qlow": "",
            "oriented_spread": "",
        })
    finite = [m for m in means if np.isfinite(m)]
    if len(finite) >= 2:
        spread = means[-1] - means[0]
        oriented = _orient(spread, orientation)
        for row in rows:
            row["spread_qhigh_qlow"] = spread
            row["oriented_spread"] = oriented
    return rows


def _quantile_rows_in_sample(
    factor: str,
    horizon: int,
    values: np.ndarray,
    returns: np.ndarray,
    quantiles: int,
    orientation: int,
) -> list[dict[str, Any]]:
    mask = _valid_mask(values, returns)
    if int(mask.sum()) < quantiles or quantiles < 2:
        return []
    x = values[mask]
    y = returns[mask]
    order = np.argsort(x, kind="stable")
    buckets = [np.asarray(b, dtype=np.int64) for b in np.array_split(order, quantiles)]
    return _quantile_rows_from_buckets(factor, horizon, "in_sample", buckets, y, orientation)


def _quantile_rows_out_of_sample(
    factor: str,
    horizon: int,
    values: np.ndarray,
    returns: np.ndarray,
    quantiles: int,
    orientation: int,
    train_ratio: float,
) -> list[dict[str, Any]]:
    """Build buckets from the temporal first `train_ratio` slice, evaluate on the remainder."""
    if quantiles < 2:
        return []
    mask = _valid_mask(values, returns)
    valid_idx = np.where(mask)[0]
    if len(valid_idx) < quantiles * 4:
        return []
    train_ratio = max(0.1, min(0.9, float(train_ratio)))
    cut = int(len(valid_idx) * train_ratio)
    if cut < quantiles or len(valid_idx) - cut < quantiles:
        return []
    train_idx = valid_idx[:cut]
    test_idx = valid_idx[cut:]
    train_x = values[train_idx]
    edges = np.quantile(train_x, np.linspace(0.0, 1.0, quantiles + 1)[1:-1])
    test_x = values[test_idx]
    test_y = returns[test_idx]
    bucket_assignment = np.searchsorted(edges, test_x, side="right")
    buckets: list[np.ndarray] = []
    for q in range(quantiles):
        sel = np.where(bucket_assignment == q)[0]
        buckets.append(sel.astype(np.int64))
    return _quantile_rows_from_buckets(factor, horizon, "out_of_sample", buckets, test_y, orientation)


def _stability_rows(
    factor: str,
    horizon: int,
    values: np.ndarray,
    returns: np.ndarray,
    open_times: np.ndarray,
    granularity: str,
    quantiles: int,
    min_samples: int,
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
        if len(x) < min_samples:
            continue
        # Per-period quantile spread is intentionally global within the period —
        # it's a diagnostic, not an OOS prediction.
        order = np.argsort(x, kind="stable")
        means: list[float] = []
        if len(x) >= quantiles:
            for bucket in np.array_split(order, quantiles):
                vals = y[bucket]
                means.append(float(np.mean(vals)) if len(vals) else float("nan"))
        spread = (means[-1] - means[0]) if len(means) >= 2 else float("nan")
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


def _factor_correlations(factor_values: dict[str, np.ndarray]) -> list[dict[str, Any]]:
    """Pairwise Pearson + Spearman correlation across factor value series."""
    names = list(factor_values.keys())
    rows: list[dict[str, Any]] = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a = factor_values[names[i]]
            b = factor_values[names[j]]
            if len(a) != len(b):
                continue
            mask = _valid_mask(a, b)
            if int(mask.sum()) < 30:
                continue
            x = a[mask]
            y = b[mask]
            rows.append({
                "factor_a": names[i],
                "factor_b": names[j],
                "pearson": _corr(x, y),
                "spearman": _corr(_rank(x), _rank(y)),
                "sample_count": int(mask.sum()),
            })
    rows.sort(key=lambda r: abs(float(r.get("spearman", 0.0))), reverse=True)
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
