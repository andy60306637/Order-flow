"""Central data-root path resolution for OrderFlow."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_DATA_ROOT = "ORDERFLOW_DATA_ROOT"

_UI_SETTINGS_PATH = PROJECT_ROOT / ".ui_settings.json"
_DATA_ROOT_OVERRIDE: Optional[Path] = None
DATA_ROOT_FORMAT = "orderflow_data_root_v1"


def project_root() -> Path:
    return PROJECT_ROOT


def _normalize_path(value: str | os.PathLike[str]) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(str(value))).strip()
    path = Path(expanded)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def set_data_root_override(path: str | os.PathLike[str] | None) -> None:
    """Set a process-local data root override, typically from a CLI argument."""
    global _DATA_ROOT_OVERRIDE
    _DATA_ROOT_OVERRIDE = _normalize_path(path) if path else None


def clear_data_root_override() -> None:
    set_data_root_override(None)


def _data_root_from_ui_settings() -> Path | None:
    try:
        with open(_UI_SETTINGS_PATH, encoding="utf-8") as fh:
            settings = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None

    value = settings.get("data_root") if isinstance(settings, dict) else None
    if isinstance(value, str) and value.strip():
        return _normalize_path(value)
    return None


def data_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the active data root."""
    if explicit:
        return _normalize_path(explicit)
    if _DATA_ROOT_OVERRIDE is not None:
        return _DATA_ROOT_OVERRIDE

    env_value = os.environ.get(ENV_DATA_ROOT)
    if env_value and env_value.strip():
        return _normalize_path(env_value)

    settings_root = _data_root_from_ui_settings()
    if settings_root is not None:
        return settings_root

    return (PROJECT_ROOT / "data").resolve()


def tick_cache_dir() -> Path:
    return data_root() / "ticks"


def kline_cache_dir() -> Path:
    return data_root() / "klines"


def market_data_dir(kind: str, market: str = "futures_um") -> Path:
    if not kind or Path(kind).is_absolute() or ".." in Path(kind).parts:
        raise ValueError(f"invalid market data kind: {kind!r}")
    if not market or Path(market).is_absolute() or ".." in Path(market).parts:
        raise ValueError(f"invalid market: {market!r}")
    return data_root() / market / kind


def raw_binance_dir(market: str = "futures_um") -> Path:
    if not market or Path(market).is_absolute() or ".." in Path(market).parts:
        raise ValueError(f"invalid market: {market!r}")
    return data_root() / market / "raw"


def data_layout_doc_path(root: str | os.PathLike[str] | None = None) -> Path:
    return data_root(root) / "DATA_LAYOUT.md"


def data_root_manifest_path(root: str | os.PathLike[str] | None = None) -> Path:
    return data_root(root) / "manifests" / "data_root.json"


def default_data_root_manifest() -> dict:
    return {
        "format": DATA_ROOT_FORMAT,
        "created_by": "OrderFlow",
        "layout_doc": "DATA_LAYOUT.md",
        "markets": ["futures_um"],
        "default_symbol": "BTCUSDT",
        "datasets": {
            "futures_um.ticks.aggTrades": {
                "cache_format": "tick_shards_v1",
                "columns": ["trade_time_ms", "price", "qty", "is_buyer_maker"],
            },
            "futures_um.klines": {
                "cache_format": "binance_kline_npy_v1",
                "columns": [
                    "open_time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "close_time",
                    "quote_volume",
                    "count",
                    "taker_buy_volume",
                    "taker_buy_quote_volume",
                    "ignore",
                ],
            },
            "futures_um.metrics": {
                "cache_format": "market_data_npz_v1",
                "columns": "dataset_manifest",
            },
            "futures_um.fundingRate": {
                "cache_format": "market_data_npz_v1",
                "columns": "dataset_manifest",
            },
            "futures_um.premiumIndexKlines": {
                "cache_format": "market_data_npz_v1",
                "columns": "dataset_manifest",
            },
            "futures_um.liquidationSnapshot": {
                "cache_format": "market_data_npz_v1",
                "columns": "dataset_manifest",
            },
        },
    }


def _fallback_layout_doc() -> str:
    return """# OrderFlow Data Layout

This data root stores OrderFlow market data caches, raw Binance files, and manifests.

Read `manifests/data_root.json` before changing data files. Keep raw files under
dataset `raw/` directories and normalized cache output under `cache/`.
"""


def ensure_data_root_layout(root: str | os.PathLike[str] | None = None) -> Path:
    """Create DATA_LAYOUT.md and manifests/data_root.json for the selected root."""
    resolved_root = data_root(root)
    resolved_root.mkdir(parents=True, exist_ok=True)

    layout_doc = resolved_root / "DATA_LAYOUT.md"
    if not layout_doc.exists():
        repo_layout = PROJECT_ROOT / "data" / "DATA_LAYOUT.md"
        if repo_layout.exists():
            shutil.copyfile(repo_layout, layout_doc)
        else:
            layout_doc.write_text(_fallback_layout_doc(), encoding="utf-8")

    manifest_path = resolved_root / "manifests" / "data_root.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if not manifest_path.exists():
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(default_data_root_manifest(), fh, indent=2, ensure_ascii=False)
    return resolved_root


def load_data_root_manifest(root: str | os.PathLike[str] | None = None) -> dict | None:
    path = data_root_manifest_path(root)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def validate_data_root(root: str | os.PathLike[str] | None = None) -> tuple[bool, str]:
    resolved_root = data_root(root)
    layout_doc = resolved_root / "DATA_LAYOUT.md"
    if not layout_doc.exists():
        return False, f"missing layout doc: {layout_doc}"

    manifest = load_data_root_manifest(resolved_root)
    if manifest is None:
        return False, f"missing or invalid manifest: {resolved_root / 'manifests' / 'data_root.json'}"
    if manifest.get("format") != DATA_ROOT_FORMAT:
        return False, f"unsupported data root format: {manifest.get('format')!r}"
    return True, "ok"
