"""
時間切片管理器：基於 data/ticks/shards/ 月份分片，
提供自訂時間區間、非連續月份選取、Walk-forward 切分。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core import tick_cache
from core.tick_cache import _load_shard_manifest, shard_path


# ──────────────────────────────────────────────────────────────────────────────
# 資料類別
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ShardInfo:
    """單一月份分片的描述資訊。"""
    month_key: str        # e.g. "202301"
    start_ms:  int        # 該月第一毫秒（從 manifest 或推算）
    end_ms:    int        # 該月最後一毫秒
    count:     int        # tick 筆數（manifest 記錄的值，0 = 未知）
    path:      str        # 相對 npy 路徑（來自 manifest）
    available: bool       # .npy 實體檔案存在


@dataclass
class TimeSlice:
    """一個邏輯時段，可能由多個不連續區段組成。"""
    label:    str
    segments: list[tuple[int, int]] = field(default_factory=list)  # [(start_ms, end_ms), ...]
    segment_symbols: list[str] = field(default_factory=list)       # optional tick source per segment

    def total_ms(self) -> int:
        return sum(end - start for start, end in self.segments)


@dataclass
class WalkForwardConfig:
    """Walk-forward 參數。"""
    n_segments:    int   = 4       # 分幾段（IS + OOS 各 n 組）
    oos_fraction:  float = 0.3     # 每段中 OOS 佔比（0.3 = 後 30%）
    anchored:      bool  = False   # True=擴張視窗, False=滾動視窗


SourceSegment = tuple[int, int, str]  # start_ms, end_ms, tick source symbol


# ──────────────────────────────────────────────────────────────────────────────
# 工具函式
# ──────────────────────────────────────────────────────────────────────────────

def _month_start_ms(month_key: str) -> int:
    dt = datetime.strptime(month_key, "%Y%m").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _next_month_start_ms(month_key: str) -> int:
    dt = datetime.strptime(month_key, "%Y%m").replace(tzinfo=timezone.utc)
    if dt.month == 12:
        nxt = dt.replace(year=dt.year + 1, month=1)
    else:
        nxt = dt.replace(month=dt.month + 1)
    return int(nxt.timestamp() * 1000)


def _month_end_ms(month_key: str) -> int:
    return _next_month_start_ms(month_key) - 1


def _iter_month_keys(start_ms: int, end_ms: int) -> list[str]:
    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    keys: list[str] = []
    cur = start
    while cur <= end:
        keys.append(cur.strftime("%Y%m"))
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    return keys


def discover_tick_sources(symbol: str) -> list[str]:
    """Return the base symbol plus all date-ranged shard aliases in the data root."""
    symbol = symbol.upper()
    datasets: list[str] = []
    if tick_cache.load_meta(symbol) is not None:
        datasets.append(symbol)

    tick_dir = tick_cache.shard_manifest_path(symbol).parent
    alias_re = re.compile(rf"^{re.escape(symbol)}_\d{{8}}_\d{{8}}$")
    for path in sorted(Path(tick_dir).glob(f"{symbol}_*_shards.json")):
        alias = path.name.removesuffix("_shards.json")
        if alias_re.match(alias) and tick_cache.load_meta(alias) is not None:
            datasets.append(alias)

    return datasets or [symbol]


def tick_source_segments(
    symbol: str,
    *,
    month_keys: list[str] | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[SourceSegment]:
    """Return available shard-backed source segments for a base symbol.

    The same calendar month can be split across multiple yearly shard aliases.
    This mirrors the desktop shard calendar: the returned segment carries the
    exact tick source symbol needed to load that shard.
    """
    month_set = set(month_keys) if month_keys else None
    segments: list[SourceSegment] = []

    for source in discover_tick_sources(symbol):
        mgr = TimeSliceManager(source)
        for shard in mgr.available_shards():
            if not shard.available:
                continue
            if month_set is not None and shard.month_key not in month_set:
                continue

            seg_start = int(shard.start_ms)
            seg_end = int(shard.end_ms)
            if start_ms is not None:
                seg_start = max(seg_start, int(start_ms))
            if end_ms is not None:
                seg_end = min(seg_end, int(end_ms))
            if seg_start <= seg_end:
                segments.append((seg_start, seg_end, source))

    return sorted(set(segments), key=lambda item: (item[0], item[1], item[2]))


def make_source_slice(label: str, source_segments: list[SourceSegment]) -> TimeSlice:
    segments = [(start, end) for start, end, _ in source_segments]
    symbols = [source for _, _, source in source_segments]
    return TimeSlice(label=label, segments=segments, segment_symbols=symbols)


def build_tick_source_slice(symbol: str, month_keys: list[str], label: str = "Custom") -> TimeSlice:
    return make_source_slice(label, tick_source_segments(symbol, month_keys=month_keys))


def build_tick_source_range_slice(
    symbol: str,
    start_ms: int,
    end_ms: int,
    label: str = "Range",
) -> TimeSlice:
    return make_source_slice(
        label,
        tick_source_segments(symbol, start_ms=start_ms, end_ms=end_ms),
    )


def _clip_source_segments(
    source_segments: list[SourceSegment],
    start_ms: int,
    end_ms: int,
) -> list[SourceSegment]:
    clipped: list[SourceSegment] = []
    for seg_start, seg_end, source in source_segments:
        start = max(seg_start, start_ms)
        end = min(seg_end, end_ms)
        if start <= end:
            clipped.append((start, end, source))
    return clipped


def build_tick_source_walk_forward(
    source_segments: list[SourceSegment],
    cfg: WalkForwardConfig,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[tuple[TimeSlice, TimeSlice]]:
    """Build walk-forward slices from explicit tick source segments."""
    if not source_segments:
        return []

    range_start = int(start_ms) if start_ms is not None else min(seg[0] for seg in source_segments)
    range_end = int(end_ms) if end_ms is not None else max(seg[1] for seg in source_segments)
    total_ms = range_end - range_start
    n = cfg.n_segments
    oos_frac = cfg.oos_fraction
    is_frac = 1.0 - oos_frac

    if n <= 0 or total_ms <= 0:
        return []

    result: list[tuple[TimeSlice, TimeSlice]] = []

    if cfg.anchored:
        window_size = total_ms / n
        for i in range(n):
            is_end = range_start + int(window_size * (i + is_frac))
            oos_start = is_end
            oos_end = range_start + int(window_size * (i + 1))
            if oos_start >= oos_end:
                continue
            is_segments = _clip_source_segments(source_segments, range_start, is_end)
            oos_segments = _clip_source_segments(source_segments, oos_start, oos_end)
            if is_segments and oos_segments:
                result.append((
                    make_source_slice(f"IS_{i + 1}", is_segments),
                    make_source_slice(f"OOS_{i + 1}", oos_segments),
                ))
    else:
        window_size = total_ms / (n * is_frac + oos_frac)
        step = window_size * is_frac
        for i in range(n):
            w_start = range_start + int(step * i)
            is_end = w_start + int(window_size * is_frac)
            oos_start = is_end
            oos_end = min(w_start + int(window_size), range_end)
            if w_start >= range_end or oos_start >= oos_end:
                continue
            is_segments = _clip_source_segments(source_segments, w_start, is_end)
            oos_segments = _clip_source_segments(source_segments, oos_start, oos_end)
            if is_segments and oos_segments:
                result.append((
                    make_source_slice(f"IS_{i + 1}", is_segments),
                    make_source_slice(f"OOS_{i + 1}", oos_segments),
                ))

    return result


# ──────────────────────────────────────────────────────────────────────────────
# 主要管理器
# ──────────────────────────────────────────────────────────────────────────────

class TimeSliceManager:
    """管理指定交易對的時間切片，基於 shard manifest。"""

    def __init__(self, symbol: str) -> None:
        self._symbol   = symbol.upper()
        self._manifest = _load_shard_manifest(self._symbol)

    # ── 查詢 ──────────────────────────────────────────────────────────────────

    def available_shards(self) -> list[ShardInfo]:
        """回傳所有已知月份分片（包含資料不存在的月份）。"""
        if self._manifest is None:
            return []
        months: dict = self._manifest.get("months", {})
        result: list[ShardInfo] = []
        for mk, meta in sorted(months.items()):
            npy = shard_path(self._symbol, mk)
            start = meta.get("start_ms") or _month_start_ms(mk)
            end   = meta.get("end_ms")   or _month_end_ms(mk)
            result.append(ShardInfo(
                month_key = mk,
                start_ms  = int(start),
                end_ms    = int(end),
                count     = int(meta.get("count", 0)),
                path      = meta.get("path", ""),
                available = npy.exists(),
            ))
        return result

    def shards_in_range(self, start_ms: int, end_ms: int) -> list[ShardInfo]:
        """回傳與 [start_ms, end_ms] 重疊的分片。"""
        return [
            s for s in self.available_shards()
            if s.end_ms >= start_ms and s.start_ms <= end_ms
        ]

    def has_data(self) -> bool:
        """是否有任何可用分片。"""
        return any(s.available for s in self.available_shards())

    # ── 建立切片 ──────────────────────────────────────────────────────────────

    def build_slice(
        self,
        month_keys: list[str],
        label: str = "Custom",
    ) -> TimeSlice:
        """
        從指定月份清單建立 TimeSlice。
        連續月份合併為單一 segment，非連續保持各別 segment。
        """
        if not month_keys:
            return TimeSlice(label=label)

        shard_map: dict[str, ShardInfo] = {
            s.month_key: s for s in self.available_shards()
        }

        segments: list[tuple[int, int]] = []
        seg_start: Optional[int] = None
        seg_end:   Optional[int] = None
        prev_key:  Optional[str] = None

        for mk in sorted(month_keys):
            if mk in shard_map:
                s = shard_map[mk]
                cur_start = s.start_ms
                cur_end   = s.end_ms
            else:
                cur_start = _month_start_ms(mk)
                cur_end   = _month_end_ms(mk)

            if seg_start is None:
                seg_start = cur_start
                seg_end   = cur_end
            else:
                expected = _month_start_ms(mk) if prev_key and _is_consecutive(prev_key, mk) else None
                if expected is not None:
                    seg_end = cur_end
                else:
                    segments.append((seg_start, seg_end))
                    seg_start = cur_start
                    seg_end   = cur_end
            prev_key = mk

        if seg_start is not None and seg_end is not None:
            segments.append((seg_start, seg_end))

        return TimeSlice(label=label, segments=segments)

    def build_range_slice(
        self,
        start_ms: int,
        end_ms:   int,
        label:    str = "Range",
    ) -> TimeSlice:
        """從起迄時間直接建立單一連續 TimeSlice。"""
        return TimeSlice(label=label, segments=[(start_ms, end_ms)])

    def build_walk_forward(
        self,
        start_ms: int,
        end_ms:   int,
        cfg:      WalkForwardConfig,
    ) -> list[tuple[TimeSlice, TimeSlice]]:
        """
        將 [start_ms, end_ms] 切分為 (IS_slice, OOS_slice) 組合列表。

        rolling (anchored=False)：
          Window i 從 start + i*(window_size*(1-oos)) 開始，長 window_size
        anchored (anchored=True)：
          Window i 從 start 開始，長度隨 i 增加

        回傳 list[(IS TimeSlice, OOS TimeSlice)]
        """
        total_ms = end_ms - start_ms
        n = cfg.n_segments
        oos_frac = cfg.oos_fraction
        is_frac  = 1.0 - oos_frac

        if n <= 0 or total_ms <= 0:
            return []

        result: list[tuple[TimeSlice, TimeSlice]] = []

        if cfg.anchored:
            # 擴張視窗：每段 IS 從 start 開始，長度遞增
            window_size = total_ms / n
            for i in range(n):
                is_end   = start_ms + int(window_size * (i + is_frac))
                oos_start = is_end
                oos_end  = start_ms + int(window_size * (i + 1))
                if oos_start >= oos_end:
                    continue
                is_sl  = TimeSlice(label=f"IS_{i+1}",  segments=[(start_ms, is_end)])
                oos_sl = TimeSlice(label=f"OOS_{i+1}", segments=[(oos_start, oos_end)])
                result.append((is_sl, oos_sl))
        else:
            # 滾動視窗
            window_size = total_ms / (n * is_frac + oos_frac)
            step        = window_size * is_frac
            for i in range(n):
                w_start   = start_ms + int(step * i)
                is_end    = w_start + int(window_size * is_frac)
                oos_start = is_end
                oos_end   = min(w_start + int(window_size), end_ms)
                if w_start >= end_ms or oos_start >= oos_end:
                    continue
                is_sl  = TimeSlice(label=f"IS_{i+1}",  segments=[(w_start, is_end)])
                oos_sl = TimeSlice(label=f"OOS_{i+1}", segments=[(oos_start, oos_end)])
                result.append((is_sl, oos_sl))

        return result

    def slice_to_tick_range(
        self,
        slice_: TimeSlice,
    ) -> list[tuple[int, int]]:
        """
        回傳 slice 的 (start_ms, end_ms) 區段列表，
        用於傳入 load_range_sharded()。
        """
        return list(slice_.segments)


# ──────────────────────────────────────────────────────────────────────────────

def _is_consecutive(mk1: str, mk2: str) -> bool:
    """判斷 mk2 是否是 mk1 的下一個月份。"""
    return _next_month_start_ms(mk1) == _month_start_ms(mk2)
