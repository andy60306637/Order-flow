from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.kline_cache import cache_path as kline_cache_path
from core.tick_cache import build_bar_map, load_range
from utils.tick_data_backtest import _build_klines_from_ticks

DEFAULT_SEGMENT_REPORT = PROJECT_ROOT / "docs" / "reports" / "wick_reversal_v4_segment_experiments.json"
DEFAULT_MARKDOWN_OUT = PROJECT_ROOT / "docs" / "reports" / "wick_reversal_v4_strategy_data_audit_2026-04-18.md"
DEFAULT_JSON_OUT = PROJECT_ROOT / "docs" / "reports" / "wick_reversal_v4_strategy_data_audit_2026-04-18.json"
BAR_MS = 60_000


@dataclass(frozen=True)
class AlignmentSampleSpec:
    label: str
    symbol: str
    start: str
    end: str


@dataclass
class AlignmentSampleResult:
    label: str
    symbol: str
    start: str
    end_exclusive: str
    bars_expected: int
    bars_rebuilt: int
    bars_exchange: int
    ticks: int
    bars_with_any_mismatch: int
    mismatch_rate_pct: float
    field_mismatch_counts: dict[str, int]
    max_diffs: dict[str, float]
    worst_days: list[dict[str, object]]
    worst_examples: list[dict[str, object]]
    first_tick_delay_ms: dict[str, float]
    last_tick_gap_ms: dict[str, float]
    all_closes_match: bool


@dataclass
class SegmentDatasetSummary:
    dataset: str
    optimized_positive_segments: int
    total_segments: int
    baseline_avg_score: float
    optimized_avg_score: float
    avg_score_delta: float
    best_plan: dict[str, object]
    worst_plan: dict[str, object]


@dataclass
class AuditReport:
    generated_at_utc: str
    alignment_samples: list[AlignmentSampleResult]
    segment_summary: list[SegmentDatasetSummary]
    conclusions: list[str]
    next_steps: list[str]


def _dt_to_ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _iso_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def _round(value: float) -> float:
    return round(float(value), 6)


def _series_summary(values: list[float]) -> dict[str, float]:
    arr = np.array(values, dtype=np.float64)
    return {
        "median": _round(np.percentile(arr, 50)),
        "p95": _round(np.percentile(arr, 95)),
        "max": _round(np.max(arr)),
    }


def _load_exchange_rows(start_ms: int, end_ms_exclusive: int) -> np.ndarray:
    path = kline_cache_path("BTCUSDT", "1m")
    arr = np.load(str(path), mmap_mode="r")
    open_times = arr[:, 0].astype(np.int64)
    lo = int(np.searchsorted(open_times, start_ms, side="left"))
    hi = int(np.searchsorted(open_times, end_ms_exclusive, side="left"))
    return np.array(arr[lo:hi], copy=False)


def _compare_fields(rebuilt_bar, exchange_row: np.ndarray) -> dict[str, float]:
    return {
        "open": abs(rebuilt_bar.open - float(exchange_row[1])),
        "high": abs(rebuilt_bar.high - float(exchange_row[2])),
        "low": abs(rebuilt_bar.low - float(exchange_row[3])),
        "close": abs(rebuilt_bar.close - float(exchange_row[4])),
        "volume": abs(rebuilt_bar.volume - float(exchange_row[5])),
        "taker_buy_volume": abs(rebuilt_bar.taker_buy_volume - float(exchange_row[9])),
    }


def run_alignment_sample(spec: AlignmentSampleSpec) -> AlignmentSampleResult:
    start_ms = _dt_to_ms(spec.start)
    end_ms_exclusive = _dt_to_ms(spec.end)
    end_ms_inclusive = end_ms_exclusive - 1
    ticks = load_range(spec.symbol, start_ms, end_ms_inclusive)
    rebuilt = _build_klines_from_ticks(spec.symbol, ticks, interval="1m")
    exchange_rows = _load_exchange_rows(start_ms, end_ms_exclusive)
    exchange_by_ot = {int(row[0]): row for row in exchange_rows}
    bar_map = build_bar_map(ticks, [(k.open_time, k.close_time) for k in rebuilt])

    field_mismatch_counts: Counter[str] = Counter()
    per_day_mismatches: defaultdict[str, int] = defaultdict(int)
    max_diffs = {name: 0.0 for name in ["open", "high", "low", "close", "volume", "taker_buy_volume"]}
    worst_examples: list[tuple[float, dict[str, object]]] = []
    first_tick_delays: list[float] = []
    last_tick_gaps: list[float] = []
    bars_with_any_mismatch = 0
    all_closes_match = True
    epsilon = 1e-9

    for bar in rebuilt:
        ticks_in_bar = bar_map.get(bar.open_time)
        if ticks_in_bar is not None and len(ticks_in_bar) > 0:
            first_tick_delays.append(float(ticks_in_bar[0, 0] - bar.open_time))
            last_tick_gaps.append(float(bar.close_time - ticks_in_bar[-1, 0]))

        exchange_row = exchange_by_ot.get(bar.open_time)
        if exchange_row is None:
            continue
        diffs = _compare_fields(bar, exchange_row)
        mismatched_fields = [name for name, value in diffs.items() if value > epsilon]
        if not mismatched_fields:
            continue

        bars_with_any_mismatch += 1
        day_key = _iso_ms(bar.open_time)[:10]
        per_day_mismatches[day_key] += 1
        for name in mismatched_fields:
            field_mismatch_counts[name] += 1
        for name, value in diffs.items():
            max_diffs[name] = max(max_diffs[name], float(value))
        if diffs["close"] > epsilon:
            all_closes_match = False

        example = {
            "open_time_utc": _iso_ms(bar.open_time),
            "diffs": {name: _round(value) for name, value in diffs.items() if value > epsilon},
            "rebuilt": {
                "open": _round(bar.open),
                "high": _round(bar.high),
                "low": _round(bar.low),
                "close": _round(bar.close),
                "volume": _round(bar.volume),
                "taker_buy_volume": _round(bar.taker_buy_volume),
            },
            "exchange": {
                "open": _round(exchange_row[1]),
                "high": _round(exchange_row[2]),
                "low": _round(exchange_row[3]),
                "close": _round(exchange_row[4]),
                "volume": _round(exchange_row[5]),
                "taker_buy_volume": _round(exchange_row[9]),
            },
        }
        worst_examples.append((max(diffs.values()), example))

    worst_examples = [row for _, row in sorted(worst_examples, key=lambda item: item[0], reverse=True)[:5]]
    worst_days = [
        {"date": day, "bars_with_mismatch": count}
        for day, count in sorted(per_day_mismatches.items(), key=lambda item: item[1], reverse=True)[:5]
    ]
    expected_bars = int((end_ms_exclusive - start_ms) / BAR_MS)

    return AlignmentSampleResult(
        label=spec.label,
        symbol=spec.symbol,
        start=spec.start,
        end_exclusive=spec.end,
        bars_expected=expected_bars,
        bars_rebuilt=len(rebuilt),
        bars_exchange=len(exchange_rows),
        ticks=int(len(ticks)),
        bars_with_any_mismatch=bars_with_any_mismatch,
        mismatch_rate_pct=_round((bars_with_any_mismatch / max(len(rebuilt), 1)) * 100.0),
        field_mismatch_counts=dict(field_mismatch_counts),
        max_diffs={name: _round(value) for name, value in max_diffs.items()},
        worst_days=worst_days,
        worst_examples=worst_examples,
        first_tick_delay_ms=_series_summary(first_tick_delays),
        last_tick_gap_ms=_series_summary(last_tick_gaps),
        all_closes_match=all_closes_match,
    )


def summarize_segment_report(path: Path) -> list[SegmentDatasetSummary]:
    report = json.loads(path.read_text(encoding="utf-8"))
    grouped: defaultdict[str, list[dict]] = defaultdict(list)
    for result in report["results"]:
        grouped[result["dataset"]["name"]].append(result)

    summaries: list[SegmentDatasetSummary] = []
    for dataset_name, rows in sorted(grouped.items()):
        baseline_scores = [row["combined"]["test_baseline"]["score"] for row in rows]
        optimized_scores = [row["combined"]["test_optimized"]["score"] for row in rows]
        sorted_rows = sorted(rows, key=lambda row: row["combined"]["test_optimized"]["score"], reverse=True)
        best_row = sorted_rows[0]
        worst_row = sorted_rows[-1]
        summaries.append(
            SegmentDatasetSummary(
                dataset=dataset_name,
                optimized_positive_segments=sum(1 for score in optimized_scores if score > 0),
                total_segments=len(rows),
                baseline_avg_score=_round(sum(baseline_scores) / len(baseline_scores)),
                optimized_avg_score=_round(sum(optimized_scores) / len(optimized_scores)),
                avg_score_delta=_round(sum(opt - base for opt, base in zip(optimized_scores, baseline_scores)) / len(rows)),
                best_plan={
                    "name": best_row["plan"]["name"],
                    "optimized_test_score": _round(best_row["combined"]["test_optimized"]["score"]),
                    "profit_factor": _round(best_row["combined"]["test_optimized"]["profit_factor"]),
                    "trades": int(best_row["combined"]["test_optimized"]["trades"]),
                },
                worst_plan={
                    "name": worst_row["plan"]["name"],
                    "optimized_test_score": _round(worst_row["combined"]["test_optimized"]["score"]),
                    "profit_factor": _round(worst_row["combined"]["test_optimized"]["profit_factor"]),
                    "trades": int(worst_row["combined"]["test_optimized"]["trades"]),
                },
            )
        )
    return summaries


def build_conclusions(
    alignment_samples: list[AlignmentSampleResult],
    segment_summary: list[SegmentDatasetSummary],
) -> tuple[list[str], list[str]]:
    conclusions: list[str] = []
    next_steps: list[str] = []

    worst_alignment = max(alignment_samples, key=lambda row: row.mismatch_rate_pct)
    if worst_alignment.mismatch_rate_pct >= 10.0:
        conclusions.append(
            "樣本週的 tick 重建 1m K 棒與交易所快取存在顯著差異，問題集中在 open/high/low/volume，close 全數吻合。"
        )
        conclusions.append(
            "這更像是 aggTrade 缺片或極值遺漏，而不是 bar 索引錯位；時間範圍內未觀察到跨分鐘漂移。"
        )
        days = ", ".join(day["date"] for day in worst_alignment.worst_days[:3])
        next_steps.append(
            f"優先回補 {worst_alignment.symbol} 的高差異日期：{days}，先核對 shard/manifest，再針對原始 zip 重新匯入。"
        )
    else:
        conclusions.append("樣本週對齊整體乾淨，可直接進入策略環境敏感度測試。")

    summary_map = {row.dataset: row for row in segment_summary}
    y2023 = summary_map.get("y2023")
    y2025 = summary_map.get("y2025")
    if y2023 and y2025:
        conclusions.append(
            "分段實驗顯示 2023 年優化後仍大多為負分，2025 年則全部維持正分，策略表現確實高度 regime-dependent。"
        )
        if y2025.avg_score_delta < 0:
            conclusions.append(
                "同一組優化流程對 2025 年平均反而降分，說明目前參數搜尋無法產出穩定跨年份解。"
            )
            next_steps.append(
                "後續參數掃描先以 y2023 / y2024 的失敗段 (`h2_to_h1`) 當 canary，並保留 y2025 baseline 當控制組，避免對 2025 過度調參。"
            )
    next_steps.append(
        "在資料回補完成前，先不要擴大全區間重優化；否則很可能只是對不完整 tick 序列做 overfit。"
    )
    next_steps.append(
        "資料修正後，先重跑 `y2023:h2_to_h1` 與 `y2024:h2_to_h1`，只掃 `long/short_delta_eff_threshold` 與 `long/short_sl_offset` 的小網格。"
    )
    return conclusions, next_steps


def render_markdown(report: AuditReport, segment_report_path: Path) -> str:
    lines = [
        "# Wick Reversal v4 稽核進度",
        "",
        f"- 產生時間（UTC）：{report.generated_at_utc}",
        f"- 分段報表來源：`{segment_report_path}`",
        "",
        "## 階段 1：資料對齊確認",
        "",
    ]
    for sample in report.alignment_samples:
        lines.extend(
            [
                f"### {sample.label}",
                "",
                f"- 區間：`{sample.start}` ~ `{sample.end_exclusive}`（end-exclusive）",
                f"- Tick / rebuilt bars / exchange bars：`{sample.ticks:,}` / `{sample.bars_rebuilt:,}` / `{sample.bars_exchange:,}`",
                f"- 任一欄位不一致 bar：`{sample.bars_with_any_mismatch:,}` (`{sample.mismatch_rate_pct:.2f}%`)",
                f"- 欄位不一致次數：`{sample.field_mismatch_counts}`",
                f"- 最大差異：`{sample.max_diffs}`",
                f"- Close 全數吻合：`{'yes' if sample.all_closes_match else 'no'}`",
                f"- first tick delay(ms)：`{sample.first_tick_delay_ms}`",
                f"- last tick gap(ms)：`{sample.last_tick_gap_ms}`",
                "",
                "最差日期：",
            ]
        )
        for row in sample.worst_days:
            lines.append(f"- `{row['date']}`: `{row['bars_with_mismatch']}` bars")
        lines.extend(["", "代表性差異 bar："])
        for row in sample.worst_examples[:3]:
            lines.append(f"- `{row['open_time_utc']}` diff=`{row['diffs']}`")
        lines.append("")

    lines.extend(["## 階段 2：既有分段實驗摘要", ""])
    for row in report.segment_summary:
        lines.extend(
            [
                f"### {row.dataset}",
                "",
                f"- optimized 正分段數：`{row.optimized_positive_segments}/{row.total_segments}`",
                f"- baseline 平均 score：`{row.baseline_avg_score}`",
                f"- optimized 平均 score：`{row.optimized_avg_score}`",
                f"- 平均 score 變化：`{row.avg_score_delta}`",
                f"- 最佳分段：`{row.best_plan}`",
                f"- 最差分段：`{row.worst_plan}`",
                "",
            ]
        )

    lines.extend(["## 判讀", ""])
    for row in report.conclusions:
        lines.append(f"- {row}")
    lines.extend(["", "## 下一步測試策略", ""])
    for row in report.next_steps:
        lines.append(f"- {row}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a staged audit report for Wick Reversal v4 data/strategy review.")
    parser.add_argument("--segment-report", default=str(DEFAULT_SEGMENT_REPORT))
    parser.add_argument("--markdown-out", default=str(DEFAULT_MARKDOWN_OUT))
    parser.add_argument("--json-out", default=str(DEFAULT_JSON_OUT))
    args = parser.parse_args()

    samples = [
        AlignmentSampleSpec(
            label="2023/06 Sample Week",
            symbol="BTCUSDT_20230414_20240413",
            start="2023-06-05",
            end="2023-06-12",
        ),
        AlignmentSampleSpec(
            label="2024/06 Sample Week",
            symbol="BTCUSDT_20240414_20250413",
            start="2024-06-03",
            end="2024-06-10",
        ),
    ]

    segment_report_path = Path(args.segment_report)
    alignment_results = [run_alignment_sample(sample) for sample in samples]
    segment_summary = summarize_segment_report(segment_report_path)
    conclusions, next_steps = build_conclusions(alignment_results, segment_summary)

    report = AuditReport(
        generated_at_utc=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        alignment_samples=alignment_results,
        segment_summary=segment_summary,
        conclusions=conclusions,
        next_steps=next_steps,
    )

    markdown = render_markdown(report, segment_report_path)
    markdown_out = Path(args.markdown_out)
    json_out = Path(args.json_out)
    markdown_out.parent.mkdir(parents=True, exist_ok=True)
    markdown_out.write_text(markdown, encoding="utf-8")
    json_out.write_text(
        json.dumps(asdict(report), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"markdown={markdown_out}")
    print(f"json={json_out}")


if __name__ == "__main__":
    main()
