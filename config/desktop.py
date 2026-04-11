"""Desktop 端專用設定（PyQt6 UI 相關）。"""

# ── Heatmap ────────────────────────────────────────────────────────────────────
HEATMAP_TIME_SLOTS   = 300    # 保留幾個 OB 快照（x 軸）
HEATMAP_PRICE_BUCKETS = 150   # 價格分桶數（y 軸）
HEATMAP_PRICE_RANGE  = 0.015  # 以當前價格 ±1.5% 為顯示範圍
HEATMAP_UPDATE_MS    = 1000   # 快照更新頻率（ms）

# ── Order Book 顯示層數 ────────────────────────────────────────────────────────
OB_DISPLAY_LEVELS = 20

# ── 顏色主題（深色）──────────────────────────────────────────────────────────
COLOR_UP     = "#26a69a"
COLOR_DOWN   = "#ef5350"
COLOR_BG     = "#131722"
COLOR_GRID   = "#1e222d"
COLOR_FG     = "#d1d4dc"
COLOR_PANEL  = "#1e222d"
COLOR_ACCENT = "#2962ff"

# ── 策略標記顏色 ───────────────────────────────────────────────────────────────
STRATEGY_LONG_COLOR  = "#26a69a"   # 做多進場（綠）
STRATEGY_SHORT_COLOR = "#ef5350"   # 做空進場（紅）
STRATEGY_INFO_COLOR  = "#f0c040"   # 資訊標記（黃）
