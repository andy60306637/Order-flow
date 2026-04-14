import json
import os
from typing import Any, Dict

class UiSettingsManager:
    """管理 UI 設定的持久化儲存 (JSON)。"""

    def __init__(self, file_path: str = ".ui_settings.json") -> None:
        self.file_path = file_path
        self.settings: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        """從檔案載入設定，若檔案不存在則回傳空字典。"""
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[UiSettingsManager] Load failed: {e}")
        return {}

    def save(self) -> None:
        """將目前設定儲存至檔案。"""
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"[UiSettingsManager] Save failed: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        """獲取特定設定值。"""
        return self.settings.get(key, default)

    def set(self, key: str, value: Any, autosave: bool = True) -> None:
        """更新設定值。"""
        self.settings[key] = value
        if autosave:
            self.save()

    def update_dict(self, data: Dict[str, Any], autosave: bool = True) -> None:
        """批量更新設定。"""
        self.settings.update(data)
        if autosave:
            self.save()

# 全域單例，方便各處存取
ui_settings = UiSettingsManager()
