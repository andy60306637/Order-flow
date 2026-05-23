"""Settings REST API — read/write .ui_settings.json and active data root."""
from __future__ import annotations

import os

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Any

from core import data_paths
from utils.ui_settings import ui_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsPatch(BaseModel):
    data: dict[str, Any]


class DataRootPatch(BaseModel):
    path: str = ""


def _data_root_status(root: str | None = None) -> dict:
    if root is not None:
        resolved = data_paths.ensure_data_root_layout(root)
    else:
        resolved = data_paths.data_root()
    ok, message = data_paths.validate_data_root(resolved)
    return {
        "path": str(resolved),
        "env_var": data_paths.ENV_DATA_ROOT,
        "env_value": os.environ.get(data_paths.ENV_DATA_ROOT, ""),
        "valid": ok,
        "message": message,
        "tick_cache_dir": str(data_paths.tick_cache_dir()),
        "kline_cache_dir": str(data_paths.kline_cache_dir()),
    }


@router.get("")
def get_settings() -> dict:
    return dict(ui_settings.settings)


@router.put("")
def update_settings(patch: SettingsPatch) -> dict:
    ui_settings.update_dict(patch.data)
    data_root = patch.data.get("data_root")
    if isinstance(data_root, str):
        if data_root.strip():
            root = data_paths.ensure_data_root_layout(data_root)
            data_paths.set_data_root_override(root)
        else:
            data_paths.clear_data_root_override()
    return {"ok": True}


@router.get("/data-root")
def get_data_root() -> dict:
    return _data_root_status()


@router.put("/data-root")
def set_data_root(patch: DataRootPatch) -> dict:
    if patch.path.strip():
        root = data_paths.ensure_data_root_layout(patch.path)
        data_paths.set_data_root_override(root)
        ui_settings.set("data_root", str(root))
    else:
        data_paths.clear_data_root_override()
        ui_settings.set("data_root", "")
    return {"ok": True, "data_root": _data_root_status()}


@router.get("/{key}")
def get_setting(key: str) -> dict:
    return {"key": key, "value": ui_settings.get(key)}


@router.put("/{key}")
def set_setting(key: str, body: dict) -> dict:
    ui_settings.set(key, body.get("value"))
    return {"ok": True}
