"""Settings REST API — read/write .ui_settings.json."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Any

from utils.ui_settings import ui_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsPatch(BaseModel):
    data: dict[str, Any]


@router.get("")
def get_settings() -> dict:
    return dict(ui_settings.settings)


@router.put("")
def update_settings(patch: SettingsPatch) -> dict:
    ui_settings.update_dict(patch.data)
    return {"ok": True}


@router.get("/{key}")
def get_setting(key: str) -> dict:
    return {"key": key, "value": ui_settings.get(key)}


@router.put("/{key}")
def set_setting(key: str, body: dict) -> dict:
    ui_settings.set(key, body.get("value"))
    return {"ok": True}
