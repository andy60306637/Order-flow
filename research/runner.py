from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

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

ProgressCallback = Callable[[str, float | None], None]


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
    # Fraction of total bars used as the in-sample (train) split. Shared across all functions.
    train_ratio: float = 0.5
    # Granularity used to derive rolling-IC-based IR / t-stat.
    ic_period_granularity: str = "month"
    # Optional regime filter — None means no filtering.
    regime_filter: Any | None = None


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
    timeseries_ic: dict[str, Any] = field(default_factory=dict)
    orthogonal_summary: list[dict[str, Any]] = field(default_factory=list)

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
            "timeseries_ic": self.timeseries_ic,
            "orthogonal_summary": self.orthogonal_summary,
        }


def run_research(
    config: ResearchConfig,
    klines: list[Kline] | None = None,
    tick_map: TickBarMap | None = None,
    progress_callback: ProgressCallback | None = None,
) -> ResearchResult:
    ensure_builtin_factors()
    if klines is None:
        klines, tick_map = load_research_data(config, progress_callback=progress_callback)
    tick_map_f = tick_map if config.use_tick_features else None

    regime_mask: np.ndarray | None = None
    rf = config.regime_filter
    if rf is not None and rf.is_active() and rf.mode == "filter":
        from research.regime_filter import combine_for_filter, compute_regime_masks
        _emit_progress(progress_callback, "Computing regime masks...", 0.45)
        raw_masks = compute_regime_masks(klines, rf, tick_map_f)
        regime_mask = combine_for_filter(len(klines), raw_masks, rf)

    _emit_progress(progress_callback, "Computing IC analysis...", 0.55)
    return _analyze_with_config(config, klines, tick_map_f, regime_mask)


def run_research_with_regimes(
    config: ResearchConfig,
    klines: list[Kline] | None = None,
    tick_map: TickBarMap | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, ResearchResult]:
    """
    Matrix 模式：每個 regime label 獨立跑一次 IC 分析。

    回傳 {"dimension=label": ResearchResult, …}。
    若 regime_filter 未啟用，回傳 {"(all)": full_result}。
    Filter 模式時回傳 {"filtered": result}（合併遮罩）。

    優化：factor values / forward returns / period keys 在所有 regime 之間共用，
    每個 regime run 只重算與 mask 相關的 IC、quantile、stability 部分。
    """
    ensure_builtin_factors()
    if klines is None:
        klines, tick_map = load_research_data(config, progress_callback=progress_callback)
    tick_map_f = tick_map if config.use_tick_features else None

    ctx = _precompute_research_context(
        klines, tick_map_f, config.factor_names, config.horizons,
        config.use_tick_features, config.entry_lag, config.ic_period_granularity,
        progress_callback=progress_callback, progress_start=0.22, progress_end=0.58,
    )

    def _run(mask: np.ndarray | None) -> ResearchResult:
        return _analyze_with_precomputed(
            ctx, mask, config.quantiles, config.min_period_samples, config.train_ratio,
        )

    rf = config.regime_filter
    if rf is None or not rf.is_active():
        _emit_progress(progress_callback, "Computing IC analysis for all bars...", 0.72)
        return {"(all)": _run(None)}

    from research.regime_filter import combine_for_filter, compute_regime_masks
    _emit_progress(progress_callback, "Computing regime masks...", 0.60)
    raw_masks = compute_regime_masks(klines, rf, tick_map_f)

    if rf.mode == "filter":
        _emit_progress(progress_callback, "Computing filtered IC analysis...", 0.76)
        combined = combine_for_filter(len(klines), raw_masks, rf)
        return {"filtered": _run(combined)}

    if rf.mode == "cross_matrix":
        from research.regime_filter import cross_combination_key
        results: dict[str, ResearchResult] = {}
        combos = rf.get_cross_combinations()
        total = max(1, len(combos))
        done = 0
        for combo in combos:
            combined_mask: np.ndarray | None = None
            valid = True
            for dim, lbl in combo:
                mask = raw_masks.get(f"{dim}={lbl}")
                if mask is None:
                    valid = False
                    break
                combined_mask = mask if combined_mask is None else (combined_mask & mask)
            if not valid or combined_mask is None or combined_mask.sum() == 0:
                done += 1
                continue
            key = cross_combination_key(combo)
            _emit_progress(
                progress_callback,
                f"Computing cross-regime IC {done + 1}/{total}: {key}",
                0.62 + 0.33 * (done / total),
            )
            results[key] = _run(combined_mask)
            done += 1
        return results

    # Matrix: one run per label
    results: dict[str, ResearchResult] = {}
    items = list(raw_masks.items())
    total = max(1, len(items))
    for idx, (label, mask) in enumerate(items, start=1):
        _emit_progress(
            progress_callback,
            f"Computing regime IC {idx}/{total}: {label}",
            0.62 + 0.33 * ((idx - 1) / total),
        )
        results[label] = _run(mask)
    return results


def _analyze_with_config(
    config: ResearchConfig,
    klines: list[Kline],
    tick_map: TickBarMap | None,
    regime_mask: np.ndarray | None = None,
) -> ResearchResult:
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
        regime_mask=regime_mask,
    )


def load_research_data(
    config: ResearchConfig,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[Kline], TickBarMap | None]:
    from core.kline_cache import load_range_as_klines
    from core.tick_cache import build_bar_map, build_bar_map_streaming, load_range_sharded

    all_klines: list[Kline] = []
    # Each entry: (tick_symbol, start_ms, end_ms, kline_times_for_segment)
    segments_info: list[tuple[str, int, int, list[tuple[int, int]]]] = []

    total_segments = max(1, sum(len(sl.segments) for sl in config.slices))
    loaded_segments = 0
    _emit_progress(progress_callback, "Loading kline data...", 0.03)
    for sl in config.slices:
        segment_symbols = getattr(sl, "segment_symbols", []) or []
        for idx, (start_ms, end_ms) in enumerate(sl.segments):
            tick_symbol = segment_symbols[idx] if idx < len(segment_symbols) else config.symbol
            seg_klines = load_range_as_klines(config.symbol, config.interval, start_ms, end_ms)
            all_klines.extend(seg_klines)
            if config.use_tick_features:
                kline_times = [(k.open_time, k.close_time) for k in seg_klines]
                segments_info.append((tick_symbol, start_ms, end_ms, kline_times))
            loaded_segments += 1
            _emit_progress(
                progress_callback,
                f"Loaded kline segment {loaded_segments}/{total_segments}",
                0.03 + 0.09 * (loaded_segments / total_segments),
            )

    seen: dict[int, Kline] = {}
    for k in all_klines:
        seen[k.open_time] = k
    klines = [seen[t] for t in sorted(seen)]

    tick_map = None
    if not config.use_tick_features or not segments_info or not klines:
        _emit_progress(progress_callback, f"Loaded {len(klines):,} kline rows", 0.20)
        return klines, tick_map

    # Try streaming path first: processes one monthly shard at a time via mmap,
    # never allocating a contiguous array for the full range (avoids OOM).
    streaming_ok = True
    merged: dict[int, list[np.ndarray]] = {}
    total_tick_segments = max(1, len(segments_info))
    for idx, (tick_symbol, start_ms, end_ms, kline_times) in enumerate(segments_info, start=1):
        _emit_progress(
            progress_callback,
            f"Loading tick bars {idx}/{total_tick_segments}: {tick_symbol}",
            0.12 + 0.08 * ((idx - 1) / total_tick_segments),
        )
        seg_map = build_bar_map_streaming(tick_symbol, start_ms, end_ms, kline_times)
        if seg_map is None:
            streaming_ok = False
            break
        for ot, arr in seg_map.items():
            merged.setdefault(ot, []).append(arr)

    if streaming_ok:
        if merged:
            tick_map = {
                ot: vs[0] if len(vs) == 1 else np.concatenate(vs, axis=0)
                for ot, vs in merged.items()
            }
        _emit_progress(progress_callback, f"Loaded {len(klines):,} kline rows with tick bars", 0.20)
        return klines, tick_map

    # Legacy fallback: concatenates all ticks into one array — may OOM for large ranges.
    tick_parts: list[np.ndarray] = []
    for idx, (tick_symbol, start_ms, end_ms, _) in enumerate(segments_info, start=1):
        _emit_progress(
            progress_callback,
            f"Loading tick fallback {idx}/{total_tick_segments}: {tick_symbol}",
            0.12 + 0.08 * ((idx - 1) / total_tick_segments),
        )
        ticks = load_range_sharded(tick_symbol, start_ms, end_ms)
        if ticks is not None and len(ticks) > 0:
            tick_parts.append(ticks)
    if tick_parts:
        ticks = np.concatenate(tick_parts, axis=0) if len(tick_parts) > 1 else tick_parts[0]
        ticks = ticks[ticks[:, 0].argsort()]
        tick_map = build_bar_map(ticks, [(k.open_time, k.close_time) for k in klines])
    _emit_progress(progress_callback, f"Loaded {len(klines):,} kline rows with tick bars", 0.20)
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
    regime_mask: np.ndarray | None = None,
) -> ResearchResult:
    ctx = _precompute_research_context(
        klines, tick_map, factor_names, horizons,
        use_tick_features, entry_lag, ic_period_granularity,
    )
    return _analyze_with_precomputed(
        ctx, regime_mask, quantiles, min_period_samples, train_ratio,
    )


def _precompute_research_context(
    klines: list[Kline],
    tick_map: TickBarMap | None,
    factor_names: list[str],
    horizons: list[int],
    use_tick_features: bool,
    entry_lag: int,
    ic_period_granularity: str,
    progress_callback: ProgressCallback | None = None,
    progress_start: float = 0.0,
    progress_end: float = 1.0,
) -> dict[str, Any]:
    """Compute everything that does NOT depend on the regime mask, so it can be
    reused across multiple regime runs (matrix / cross_matrix modes).

    Heavy items: factor.compute() (often touches tick_map), forward returns,
    per-bar period ids for monthly / yearly stability.
    """
    ensure_builtin_factors()
    _emit_progress(progress_callback, "Preparing forward returns...", progress_start)
    arr = klines_to_arrays(klines) if klines else {}
    close = arr.get("close", np.array([], dtype=np.float64))
    open_times = arr.get("open_time", np.array([], dtype=np.int64))
    interval_ms = _kline_interval_ms(klines)
    fwd = {
        h: _forward_return(close, h, entry_lag, open_times=open_times, interval_ms=interval_ms)
        for h in horizons
    }

    factor_values: dict[str, np.ndarray] = {}
    factor_orientations: dict[str, int] = {}
    factor_meta: dict[str, dict[str, Any]] = {}
    unavailable: list[dict[str, str]] = []

    total_factors = max(1, len(factor_names))
    span = max(0.0, progress_end - progress_start)
    for idx, name in enumerate(factor_names, start=1):
        _emit_progress(
            progress_callback,
            f"Computing factor {idx}/{total_factors}: {name}",
            progress_start + span * (0.15 + 0.75 * ((idx - 1) / total_factors)),
        )
        factor = get_factor(name)
        if factor is None:
            unavailable.append({"factor": name, "reason": "not_registered"})
            continue
        if factor.requires_ticks and (not use_tick_features or tick_map is None):
            unavailable.append({"factor": name, "reason": "tick_data_unavailable"})
            continue
        values = factor.compute(klines, tick_map)
        factor_values[name] = values
        factor_orientations[name] = _factor_orientation(factor.sides)
        factor_meta[name] = {
            "requires_ticks": factor.requires_ticks,
            "sides": factor.sides,
            "group": factor.group,
        }

    _emit_progress(progress_callback, "Preparing period splits...", progress_start + span * 0.92)
    month_ids, month_labels = _period_ids(open_times, "month")
    year_ids, year_labels = _period_ids(open_times, "year")
    if ic_period_granularity == "year":
        ic_ids, ic_labels = year_ids, year_labels
    else:
        ic_ids, ic_labels = month_ids, month_labels

    return {
        "klines_count": len(klines),
        "open_times": open_times,
        "fwd": fwd,
        "factor_values": factor_values,
        "factor_orientations": factor_orientations,
        "factor_meta": factor_meta,
        "unavailable": unavailable,
        "ic_period_ids": ic_ids,
        "ic_period_labels": ic_labels,
        "month_ids": month_ids,
        "month_labels": month_labels,
        "year_ids": year_ids,
        "year_labels": year_labels,
    }


def _emit_progress(
    progress_callback: ProgressCallback | None,
    message: str,
    pct: float | None = None,
) -> None:
    if progress_callback is None:
        return
    if pct is not None:
        pct = max(0.0, min(1.0, float(pct)))
    progress_callback(message, pct)


def _analyze_with_precomputed(
    ctx: dict[str, Any],
    regime_mask: np.ndarray | None,
    quantiles: int,
    min_period_samples: int,
    train_ratio: float,
) -> ResearchResult:
    open_times: np.ndarray = ctx["open_times"]
    fwd: dict[int, np.ndarray] = ctx["fwd"]
    factor_values: dict[str, np.ndarray] = ctx["factor_values"]
    factor_orientations: dict[str, int] = ctx["factor_orientations"]
    factor_meta: dict[str, dict[str, Any]] = ctx["factor_meta"]
    unavailable: list[dict[str, str]] = list(ctx["unavailable"])
    ic_ids: np.ndarray = ctx["ic_period_ids"]
    month_ids: np.ndarray = ctx["month_ids"]
    month_labels: dict[int, str] = ctx["month_labels"]
    year_ids: np.ndarray = ctx["year_ids"]
    year_labels: dict[int, str] = ctx["year_labels"]

    # --- Global IS/OOS temporal split (shared across all factors) ---
    n_times = len(open_times)
    train_ratio_c = max(0.1, min(0.9, float(train_ratio)))
    time_cut_idx = int(n_times * train_ratio_c)
    is_time_mask = np.zeros(n_times, dtype=bool)
    is_time_mask[:time_cut_idx] = True
    oos_time_mask = ~is_time_mask
    cut_time = int(open_times[time_cut_idx]) if time_cut_idx < n_times else 0

    full_regime_mask: np.ndarray | None = None
    if regime_mask is not None and len(regime_mask) == n_times:
        is_time_mask = is_time_mask & regime_mask
        oos_time_mask = oos_time_mask & regime_mask
        full_regime_mask = regime_mask

    summary: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    qrows: list[dict[str, Any]] = []
    monthly: list[dict[str, Any]] = []
    yearly: list[dict[str, Any]] = []

    for name, values in factor_values.items():
        meta = factor_meta[name]
        orientation = factor_orientations[name]

        factor_metrics: list[dict[str, Any]] = []
        for horizon, returns in fwd.items():
            is_period_ic = _per_period_ic(
                values, returns, ic_ids,
                min_samples=min_period_samples,
                restrict_mask=is_time_mask,
            )
            oos_period_ic = _per_period_ic(
                values, returns, ic_ids,
                min_samples=min_period_samples,
                restrict_mask=oos_time_mask,
            )
            metric = _metric_row(
                name, horizon, values, returns, orientation,
                is_period_ic, oos_period_ic, is_time_mask, oos_time_mask,
            )
            metrics.append(metric)
            factor_metrics.append(metric)
            qrows.extend(_quantile_rows_in_sample(
                name, horizon, values, returns, quantiles, orientation, is_time_mask,
            ))
            qrows.extend(_quantile_rows_out_of_sample(
                name, horizon, values, returns, quantiles, orientation, is_time_mask, oos_time_mask,
            ))
            monthly.extend(_stability_rows(
                name, horizon, values, returns, open_times,
                month_ids, month_labels, quantiles, min_period_samples, cut_time,
                regime_mask=full_regime_mask, orientation=orientation,
            ))
            yearly.extend(_stability_rows(
                name, horizon, values, returns, open_times,
                year_ids, year_labels, quantiles, min_period_samples, cut_time,
                regime_mask=full_regime_mask, orientation=orientation,
            ))

        summary.append(_summary_row(
            name,
            meta["requires_ticks"],
            meta["sides"],
            meta["group"],
            orientation,
            factor_metrics,
        ))

    ts_ic = _calculate_timeseries_ic(
        factor_values, fwd, open_times, factor_orientations,
        cut_time=cut_time, regime_mask=full_regime_mask,
    )
    ortho_summary = _orthogonalize_factors(factor_values, fwd, factor_orientations, is_time_mask, oos_time_mask)
    correlations = _factor_correlations(factor_values, is_time_mask, oos_time_mask)
    summary.sort(key=lambda r: float(r.get("rank_score", 0.0)), reverse=True)
    return ResearchResult(
        summary=summary,
        metrics=metrics,
        quantiles=qrows,
        stability_monthly=monthly,
        stability_yearly=yearly,
        factor_correlations=correlations,
        unavailable=unavailable,
        rows=ctx["klines_count"],
        timeseries_ic=ts_ic,
        orthogonal_summary=ortho_summary,
    )


# ---------------------------------------------------------------------------
# Time-series IC
# ---------------------------------------------------------------------------

def _calculate_timeseries_ic(
    factor_values: dict[str, np.ndarray],
    fwd_returns: dict[int, np.ndarray],
    open_times: np.ndarray,
    orientations: dict[str, int],
    cut_time: int = 0,
    regime_mask: np.ndarray | None = None,
) -> dict[str, Any]:
    """Stepped rolling Rank IC. Includes train_cutoff_ts for IS/OOS boundary rendering."""
    if not factor_values or not fwd_returns:
        return {}

    best_horizon = min(fwd_returns.keys())
    returns = fwd_returns[best_horizon]

    n = len(open_times)
    step = max(1, n // 200)
    indices = np.arange(0, n, step)
    window = max(100, n // 10)

    ts_data: dict[str, Any] = {
        "timestamps": open_times[indices].tolist(),
        "factors": {},
        "horizon": best_horizon,
        "train_cutoff_ts": cut_time,
    }

    for name, values in factor_values.items():
        orientation = orientations.get(name, 0)
        ic_series = []
        for idx in indices:
            start = max(0, idx - window)
            end = idx + 1
            v_win = values[start:end]
            r_win = returns[start:end]
            mask = _valid_mask(v_win, r_win)
            if regime_mask is not None:
                mask = mask & regime_mask[start:end]
            if mask.sum() < 20:
                ic_series.append(0.0)
                continue
            ic = _corr(_rank(v_win[mask]), _rank(r_win[mask]))
            ic_series.append(_orient(ic, orientation))
        ts_data["factors"][name] = ic_series

    return ts_data


# ---------------------------------------------------------------------------
# Orthogonalization  (IS basis → OOS projection)
# ---------------------------------------------------------------------------

def _orthogonalize_factors(
    factor_values: dict[str, np.ndarray],
    fwd_returns: dict[int, np.ndarray],
    orientations: dict[str, int],
    is_mask: np.ndarray,
    oos_mask: np.ndarray,
) -> list[dict[str, Any]]:
    """QR orthogonalization on IS data; project OOS data onto same basis for evaluation."""
    names = list(factor_values.keys())
    if not names:
        return []

    best_h = min(fwd_returns.keys()) if fwd_returns else 1
    returns = fwd_returns[best_h]

    is_valid = is_mask & np.isfinite(returns)
    oos_valid = oos_mask & np.isfinite(returns)
    is_ret = returns[is_valid]
    oos_ret = returns[oos_valid]

    def _clean(v: np.ndarray, mask: np.ndarray) -> np.ndarray:
        sub = v[mask]
        mean = float(np.nanmean(sub)) if not np.all(np.isnan(sub)) else 0.0
        out = sub.copy()
        out[np.isnan(out)] = mean
        return out

    mat_is = [_clean(factor_values[n], is_valid) for n in names]
    mat_oos = [_clean(factor_values[n], oos_valid) for n in names]

    if not mat_is or len(mat_is[0]) < len(names):
        return []

    X_is = np.column_stack(mat_is)
    Q, R = np.linalg.qr(X_is)

    # Project OOS onto IS orthogonal basis: X_oos_ortho = X_oos @ R^{-1}
    X_oos_ortho: np.ndarray | None = None
    if len(mat_oos[0]) >= len(names):
        try:
            X_oos_ortho = np.column_stack(mat_oos) @ np.linalg.inv(R)
        except np.linalg.LinAlgError:
            pass

    ortho_summary = []
    for i, name in enumerate(names):
        v_is = Q[:, i]
        is_m = _valid_mask(v_is, is_ret)
        is_rank_ic = _corr(_rank(v_is[is_m]), _rank(is_ret[is_m])) if is_m.sum() >= 3 else 0.0

        oos_rank_ic: float = float("nan")
        oos_oriented: float = float("nan")
        if X_oos_ortho is not None:
            v_oos = X_oos_ortho[:, i]
            oos_m = _valid_mask(v_oos, oos_ret)
            if oos_m.sum() >= 3:
                oos_rank_ic = _corr(_rank(v_oos[oos_m]), _rank(oos_ret[oos_m]))
                oos_oriented = _orient(oos_rank_ic, orientations.get(name, 0))

        ortho_summary.append({
            "factor": name,
            "horizon": best_h,
            "rank_ic": is_rank_ic,
            "oriented_rank_ic": _orient(is_rank_ic, orientations.get(name, 0)),
            "oos_rank_ic": oos_rank_ic,
            "oos_oriented_rank_ic": oos_oriented,
        })

    return ortho_summary


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

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
        return value
    return value * orientation


def _fval(v: Any, default: float = 0.0) -> float:
    """Return float, substituting NaN/None with default."""
    try:
        f = float(v)
        return default if not np.isfinite(f) else f
    except (TypeError, ValueError):
        return default


def _forward_return(
    close: np.ndarray,
    horizon: int,
    entry_lag: int,
    open_times: np.ndarray | None = None,
    interval_ms: int | None = None,
) -> np.ndarray:
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
    returns = exit_ / entry - 1.0
    if open_times is not None and interval_ms is not None:
        continuity = _forward_continuity_mask(open_times, interval_ms, entry_lag + horizon)
        valid = continuity[:end]
        target = out[:end]
        target[valid] = returns[valid]
    else:
        out[:end] = returns
    return out


def _forward_continuity_mask(open_times: np.ndarray, interval_ms: int, steps: int) -> np.ndarray:
    out = np.zeros(len(open_times), dtype=bool)
    if interval_ms <= 0 or steps <= 0 or len(open_times) <= steps:
        return out
    pair_ok = np.diff(open_times) == interval_ms
    csum = np.cumsum(np.insert(pair_ok.astype(np.int64), 0, 0))
    end = len(open_times) - steps
    out[:end] = (csum[steps:steps + end] - csum[:end]) == steps
    return out


def _kline_interval_ms(klines: list[Kline]) -> int | None:
    for k in klines:
        parsed = _interval_to_ms(k.interval)
        if parsed is not None:
            return parsed
        span = int(k.close_time) - int(k.open_time) + 1
        if span > 0:
            return span
    return None


def _interval_to_ms(interval: str) -> int | None:
    if not interval:
        return None
    unit = interval[-1]
    raw = interval[:-1]
    if not raw.isdigit():
        return None
    value = int(raw)
    unit_ms = {
        "m": 60_000,
        "h": 3_600_000,
        "d": 86_400_000,
        "w": 604_800_000,
    }.get(unit)
    return value * unit_ms if unit_ms is not None else None


def _per_period_ic(
    values: np.ndarray,
    returns: np.ndarray,
    period_ids: np.ndarray,
    min_samples: int,
    restrict_mask: np.ndarray | None = None,
) -> list[float]:
    """Rank IC per sub-period, optionally restricted to IS or OOS bars.

    period_ids: int64 per-bar period id (e.g. months-since-epoch). np.unique
    sorts numerically which is also chronological.
    """
    if len(period_ids) == 0:
        return []
    base = _valid_mask(values, returns)
    if restrict_mask is not None:
        base = base & restrict_mask
    if not base.any():
        return []
    out: list[float] = []
    for period in np.unique(period_ids[base]):
        mask = base & (period_ids == period)
        if int(mask.sum()) < min_samples:
            continue
        ic = _corr(_rank(values[mask]), _rank(returns[mask]))
        if np.isfinite(ic):
            out.append(float(ic))
    return out


def _ir_tstat(period_ic: list[float]) -> tuple[float, float, float, float]:
    """Return (mean, std, IR, t-stat) from a list of per-period ICs."""
    if len(period_ic) >= 3:
        arr = np.array(period_ic, dtype=np.float64)
        mean_p = float(np.mean(arr))
        std_p = float(np.std(arr, ddof=1))
        ir = mean_p / std_p if std_p > 0 else 0.0
        t_stat = ir * np.sqrt(len(arr))
        return mean_p, std_p, ir, t_stat
    return 0.0, 0.0, 0.0, 0.0


def _metric_row(
    factor: str,
    horizon: int,
    values: np.ndarray,
    returns: np.ndarray,
    orientation: int,
    is_period_ic: list[float],
    oos_period_ic: list[float],
    is_mask: np.ndarray,
    oos_mask: np.ndarray,
) -> dict[str, Any]:
    # IS IC — regime-conditional (is_mask already includes regime_mask & IS split).
    is_valid = is_mask & _valid_mask(values, returns)
    x = values[is_valid]
    y = returns[is_valid]
    ic = _corr(x, y)
    rank_ic = _corr(_rank(x), _rank(y))

    mean_p, std_p, ir, t_stat = _ir_tstat(is_period_ic)
    oriented_is_period_ic = [_orient(v, orientation) for v in is_period_ic]
    oriented_mean_p, oriented_std_p, oriented_ir, oriented_t_stat = _ir_tstat(oriented_is_period_ic)
    oos_mean_p, oos_std_p, oos_ir, oos_t_stat = _ir_tstat(oos_period_ic)
    oriented_oos_period_ic = [_orient(v, orientation) for v in oos_period_ic]
    oriented_oos_mean_p, oriented_oos_std_p, oriented_oos_ir, oriented_oos_t_stat = _ir_tstat(
        oriented_oos_period_ic
    )

    oos_valid = oos_mask & _valid_mask(values, returns)
    x_oos = values[oos_valid]
    y_oos = returns[oos_valid]
    oos_ic = _corr(x_oos, y_oos)
    oos_rank_ic = _corr(_rank(x_oos), _rank(y_oos))

    return {
        "factor": factor,
        "horizon": horizon,
        "ic": ic,
        "rank_ic": rank_ic,
        "oriented_rank_ic": _orient(rank_ic, orientation),
        "abs_rank_ic": abs(rank_ic),
        "ic_period_mean": mean_p,
        "ic_period_std": std_p,
        "ic_ir": float(ir),
        "ic_t_stat": float(t_stat),
        "oriented_ic_period_mean": oriented_mean_p,
        "oriented_ic_period_std": oriented_std_p,
        "oriented_ic_ir": float(oriented_ir),
        "oriented_ic_t_stat": float(oriented_t_stat),
        "ic_periods": len(is_period_ic),
        "sample_count": int(is_valid.sum()),
        # OOS fields
        "oos_ic": oos_ic,
        "oos_rank_ic": oos_rank_ic,
        "oos_oriented_rank_ic": _orient(oos_rank_ic, orientation),
        "oos_abs_rank_ic": abs(oos_rank_ic),
        "oos_ic_period_mean": oos_mean_p,
        "oos_ic_ir": float(oos_ir),
        "oos_ic_t_stat": float(oos_t_stat),
        "oos_oriented_ic_period_mean": oriented_oos_mean_p,
        "oos_oriented_ic_period_std": oriented_oos_std_p,
        "oos_oriented_ic_ir": float(oriented_oos_ir),
        "oos_oriented_ic_t_stat": float(oriented_oos_t_stat),
        "oos_ic_periods": len(oos_period_ic),
        "oos_sample_count": int(oos_valid.sum()),
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
        "directional": orientation != 0,
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
            "oos_best_rank_ic": 0.0,
            "oos_oriented_rank_ic": 0.0,
            "oos_ic_ir": 0.0,
            "oos_ic_t_stat": 0.0,
            "oos_sample_count": 0,
            "rank_score": 0.0,
        })
        return base
    best = max(metrics, key=lambda r: _fval(r["oriented_rank_ic"]))
    oos_best = max(metrics, key=lambda r: _fval(r.get("oos_oriented_rank_ic")))
    rank_score = _fval(oos_best.get("oos_oriented_rank_ic")) if orientation != 0 else -1.0e9
    base.update({
        "best_horizon": best["horizon"],
        "best_rank_ic": best["rank_ic"],
        "oriented_rank_ic": best["oriented_rank_ic"],
        "abs_rank_ic": best.get("abs_rank_ic", abs(best["rank_ic"])),
        "ic_ir": best.get("oriented_ic_ir", best["ic_ir"]),
        "ic_t_stat": best.get("oriented_ic_t_stat", best["ic_t_stat"]),
        "raw_ic_ir": best["ic_ir"],
        "raw_ic_t_stat": best["ic_t_stat"],
        "oriented_ic_ir": best.get("oriented_ic_ir", best["ic_ir"]),
        "oriented_ic_t_stat": best.get("oriented_ic_t_stat", best["ic_t_stat"]),
        "avg_abs_rank_ic": float(np.nanmean([abs(float(r["rank_ic"])) for r in metrics])),
        "sample_count": max(int(r["sample_count"]) for r in metrics),
        "oos_best_horizon": oos_best.get("horizon", ""),
        "oos_best_rank_ic": oos_best.get("oos_rank_ic", 0.0),
        "oos_oriented_rank_ic": oos_best.get("oos_oriented_rank_ic", 0.0),
        "oos_abs_rank_ic": oos_best.get("oos_abs_rank_ic", abs(_fval(oos_best.get("oos_rank_ic")))),
        "oos_ic_ir": oos_best.get("oos_oriented_ic_ir", oos_best.get("oos_ic_ir", 0.0)),
        "oos_ic_t_stat": oos_best.get("oos_oriented_ic_t_stat", oos_best.get("oos_ic_t_stat", 0.0)),
        "oos_raw_ic_ir": oos_best.get("oos_ic_ir", 0.0),
        "oos_raw_ic_t_stat": oos_best.get("oos_ic_t_stat", 0.0),
        "oos_oriented_ic_ir": oos_best.get("oos_oriented_ic_ir", oos_best.get("oos_ic_ir", 0.0)),
        "oos_oriented_ic_t_stat": oos_best.get("oos_oriented_ic_t_stat", oos_best.get("oos_ic_t_stat", 0.0)),
        "oos_sample_count": max(int(r.get("oos_sample_count", 0)) for r in metrics),
        "rank_score": rank_score,
    })
    return base


# ---------------------------------------------------------------------------
# Quantile analysis
# ---------------------------------------------------------------------------

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
    is_mask: np.ndarray,
) -> list[dict[str, Any]]:
    """Bucket and evaluate entirely within IS bars."""
    mask = is_mask & _valid_mask(values, returns)
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
    is_mask: np.ndarray,
    oos_mask: np.ndarray,
) -> list[dict[str, Any]]:
    """Compute quantile edges from IS bars; evaluate on OOS bars."""
    if quantiles < 2:
        return []
    is_valid = is_mask & _valid_mask(values, returns)
    oos_valid = oos_mask & _valid_mask(values, returns)
    if int(is_valid.sum()) < quantiles or int(oos_valid.sum()) < quantiles:
        return []
    edges = np.quantile(values[is_valid], np.linspace(0.0, 1.0, quantiles + 1)[1:-1])
    test_x = values[oos_valid]
    test_y = returns[oos_valid]
    bucket_assignment = np.searchsorted(edges, test_x, side="right")
    buckets: list[np.ndarray] = [
        np.where(bucket_assignment == q)[0].astype(np.int64)
        for q in range(quantiles)
    ]
    return _quantile_rows_from_buckets(factor, horizon, "out_of_sample", buckets, test_y, orientation)


# ---------------------------------------------------------------------------
# Stability (per-period IC)
# ---------------------------------------------------------------------------

def _stability_rows(
    factor: str,
    horizon: int,
    values: np.ndarray,
    returns: np.ndarray,
    open_times: np.ndarray,
    period_ids: np.ndarray,
    period_labels: dict[int, str],
    quantiles: int,
    min_samples: int,
    cut_time: int,
    regime_mask: np.ndarray | None = None,
    orientation: int = 0,
) -> list[dict[str, Any]]:
    if len(period_ids) == 0:
        return []
    base_valid = _valid_mask(values, returns)
    if regime_mask is not None:
        base_valid = base_valid & regime_mask
    rows: list[dict[str, Any]] = []
    for period_id in np.unique(period_ids):
        period_mask = period_ids == period_id
        mask = period_mask & base_valid
        x = values[mask]
        y = returns[mask]
        if len(x) < min_samples:
            continue
        period_times = open_times[period_mask]
        if np.all(period_times < cut_time):
            split = "train"
        elif np.all(period_times >= cut_time):
            split = "test"
        else:
            split = "mixed"
        means: list[float] = []
        if len(x) >= quantiles:
            for bucket in np.array_split(np.argsort(x, kind="stable"), quantiles):
                vals = y[bucket]
                means.append(float(np.mean(vals)) if len(vals) else float("nan"))
        spread = (means[-1] - means[0]) if len(means) >= 2 else float("nan")
        rank_ic = _corr(_rank(x), _rank(y))
        rows.append({
            "factor": factor,
            "horizon": horizon,
            "period": period_labels.get(int(period_id), str(int(period_id))),
            "ic": _corr(x, y),
            "rank_ic": rank_ic,
            "oriented_rank_ic": _orient(rank_ic, orientation),
            "spread_qhigh_qlow": spread,
            "sample_count": int(len(x)),
            "split": split,
        })
    return rows


# ---------------------------------------------------------------------------
# Factor correlations
# ---------------------------------------------------------------------------

def _factor_correlations(
    factor_values: dict[str, np.ndarray],
    is_mask: np.ndarray,
    oos_mask: np.ndarray,
) -> list[dict[str, Any]]:
    """Pairwise Pearson + Spearman correlations for full / IS / OOS windows."""
    names = list(factor_values.keys())
    rows: list[dict[str, Any]] = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a = factor_values[names[i]]
            b = factor_values[names[j]]
            if len(a) != len(b):
                continue
            # Use IS∪OOS as "full" so all three windows (full/IS/OOS) are regime-conditional.
            full_m = (is_mask | oos_mask) & _valid_mask(a, b)
            if int(full_m.sum()) < 30:
                continue
            is_m = is_mask & full_m
            oos_m = oos_mask & full_m
            x_f, y_f = a[full_m], b[full_m]
            x_is, y_is = a[is_m], b[is_m]
            x_oos, y_oos = a[oos_m], b[oos_m]
            rows.append({
                "factor_a": names[i],
                "factor_b": names[j],
                "pearson": _corr(x_f, y_f),
                "spearman": _corr(_rank(x_f), _rank(y_f)),
                "pearson_is": _corr(x_is, y_is) if len(x_is) >= 30 else float("nan"),
                "spearman_is": _corr(_rank(x_is), _rank(y_is)) if len(x_is) >= 30 else float("nan"),
                "pearson_oos": _corr(x_oos, y_oos) if len(x_oos) >= 30 else float("nan"),
                "spearman_oos": _corr(_rank(x_oos), _rank(y_oos)) if len(x_oos) >= 30 else float("nan"),
                "sample_count": int(full_m.sum()),
                "is_sample_count": int(is_m.sum()),
                "oos_sample_count": int(oos_m.sum()),
            })
    rows.sort(key=lambda r: abs(float(r.get("spearman", 0.0))), reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Low-level utilities
# ---------------------------------------------------------------------------

def _period_key(ts_ms: int, granularity: str) -> str:
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return f"{dt.year:04d}" if granularity == "year" else f"{dt.year:04d}-{dt.month:02d}"


def _period_ids(
    open_times: np.ndarray,
    granularity: str,
) -> tuple[np.ndarray, dict[int, str]]:
    """Vectorized per-bar period id + id->label map.

    For "month": id = months-since-epoch (int64). For "year": id = year (e.g. 2026).
    Comparison by integer is much cheaper than the old string-based approach,
    and label formatting only happens once per unique period at output time.
    """
    if len(open_times) == 0:
        return np.array([], dtype=np.int64), {}
    dt = open_times.astype("datetime64[ms]")
    if granularity == "year":
        ids = (dt.astype("datetime64[Y]").astype(np.int64) + 1970).astype(np.int64)
        labels = {int(u): f"{int(u):04d}" for u in np.unique(ids)}
        return ids, labels
    # default: month
    ids = dt.astype("datetime64[M]").astype(np.int64)
    labels: dict[int, str] = {}
    for u in np.unique(ids):
        u_int = int(u)
        y = 1970 + u_int // 12
        m = 1 + u_int % 12
        labels[u_int] = f"{y:04d}-{m:02d}"
    return ids.astype(np.int64), labels


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
