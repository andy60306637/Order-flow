"""
Config 分層配置包。

向後相容：所有原 config.py 中的屬性仍可透過 ``import config`` 存取。
  - config.base    — 共用設定（交易對、API 端點、快取限制…）
  - config.desktop — Desktop UI 設定（顏色、Heatmap、OB 層數…）
  - config.server  — Server API 設定（host/port、CORS…）
  - config.worker  — Worker CLI 設定（Broker、風控、日誌…）

根層級 re-export base + desktop，讓現有 ``config.COLOR_UP`` 等引用不中斷。
"""

# ── 共用基礎 ────────────────────────────────────────────────────────────────────
from config.base import *     # noqa: F401,F403

# ── Desktop 設定（向後相容：舊程式碼直接 import config 即可用）────────────────
from config.desktop import *  # noqa: F401,F403
