from datetime import datetime, timezone

import config


def ms_to_dt(ms: int) -> datetime:
    """將毫秒時間戳轉為 UTC datetime。"""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def ms_to_local_dt(ms: int) -> datetime:
    """將毫秒時間戳轉為顯示時區 datetime。"""
    return datetime.fromtimestamp(ms / 1000, tz=config.DISPLAY_TZ)


def fmt_time(ms: int, fmt: str = "%H:%M") -> str:
    """將毫秒時間戳格式化為字串（顯示時區）。"""
    return datetime.fromtimestamp(ms / 1000, tz=config.DISPLAY_TZ).strftime(fmt)


def fmt_date_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=config.DISPLAY_TZ).strftime("%m/%d %H:%M")


def interval_to_ms(interval: str) -> int:
    """將 K 線 interval 字串轉為毫秒數，例如 '1m' → 60000。"""
    unit = interval[-1]
    val  = int(interval[:-1])
    table = {'s': 1_000, 'm': 60_000, 'h': 3_600_000,
             'd': 86_400_000, 'w': 604_800_000}
    return val * table.get(unit, 60_000)
