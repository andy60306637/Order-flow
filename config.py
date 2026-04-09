from datetime import timezone, timedelta

# ── 顯示時區（UTC+8，Binance 預設）──────────────────────────────────────────────
DISPLAY_TZ = timezone(timedelta(hours=8))

# ── 交易對與 Footprint tick size ───────────────────────────────────────────────
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT",
    "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
]

INTERVALS = ["1m", "3m", "5m", "15m", "30m", "1h", "4h"]

# Footprint 顯示用 tick size（已針對視覺效果調校的分桶大小）
# 這些值是「可讀性優先」的預設值，不會被 exchangeInfo 覆蓋
TICK_SIZES: dict[str, float] = {
    "BTCUSDT":  10.0,
    "ETHUSDT":   1.0,
    "BNBUSDT":   0.5,
    "SOLUSDT":   0.1,
    "XRPUSDT":   0.0001,
    "DOGEUSDT":  0.00001,
    "ADAUSDT":   0.0001,
    "AVAXUSDT":  0.01,
}

# 交易所原始 tickSize（啟動時從 exchangeInfo 動態填入）
EXCHANGE_TICK_SIZES: dict[str, float] = {}

# 價格聚合倍數選項（基於 TICK_SIZES 中的顯示用 tick）
TICK_MULTIPLIERS = [1, 2, 5, 10, 20, 50]
DEFAULT_TICK_MULTIPLIER = 1

DEFAULT_SYMBOL   = "BTCUSDT"
DEFAULT_INTERVAL = "1m"

# ── Binance USDT-M Futures 端點 ────────────────────────────────────────────────
WS_BASE   = "wss://fstream.binance.com"
REST_BASE = "https://fapi.binance.com"

# ── 歷史 K 線 ──────────────────────────────────────────────────────────────────
KLINE_HISTORY_LIMIT = 1500     # Binance REST 單次最大回傳量
KLINE_MAX_LOADED    = 129600   # _loaded_klines 最大長度（支援 90d×1440 根）

# 回測天數選項（格式：UI 標籤 → 1m K 棒數）
BACKTEST_RANGE_OPTIONS = {
    "200根":    200,
    "1 天":    1440,
    "2 天":    2880,
    "3 天":    4320,
    "10 天":  14400,
    "20 天":  28800,
    "30 天":  43200,
    "60 天":  86400,
    "90 天": 129600,
}

# 超過此 K 棒數的回測不更新圖表（避免大量資料渲染卡頓）
BACKTEST_NO_CHART_BARS = 43200   # > 30 天

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

# ── Footprint ──────────────────────────────────────────────────────────────────
FOOTPRINT_MODES = ["BidxAsk", "Delta", "Volume", "Imbalance"]
FOOTPRINT_MAX_CANDLES = 100  # 保留幾根 K 棒的 Footprint 資料

# ── CVD ────────────────────────────────────────────────────────────────────────
CVD_HISTORY = 5500   # 需 >= KLINE_MAX_LOADED + 緩衝，避免 deque 比 kline 早 drop

# ── Footprint 歷史 aggTrade 回填 ────────────────────────────────────────────────
FOOTPRINT_HISTORY_CANDLES = 20   # 啟動時回填最近 N 根 K 棒的成交

# 各 interval 對應的最大回填時間（毫秒）。
# aggTrades 量與時間成正比，大 interval 不限制會導致拉取數十萬筆，費時數分鐘。
# 以「每根 K 棒約 30 分鐘等效交易量」為上限，超出則截短 start_t。
FOOTPRINT_MAX_BACKFILL_MS: dict[str, int] = {
    "1m":  20 * 60 * 1_000,          # 20 分鐘 → 與 FOOTPRINT_HISTORY_CANDLES 同
    "3m":  20 * 3 * 60 * 1_000,      # 60 分鐘
    "5m":  20 * 5 * 60 * 1_000,      # 100 分鐘
    "15m": 10 * 15 * 60 * 1_000,     # 150 分鐘（縮減至 10 根）
    "30m": 5  * 30 * 60 * 1_000,     # 150 分鐘（縮減至 5 根）
    "1h":  3  * 60 * 60 * 1_000,     # 3 小時（縮減至 3 根）
    "4h":  2  * 4  * 60 * 60 * 1_000, # 8 小時（縮減至 2 根）
}

# ── 策略標記顏色 ───────────────────────────────────────────────────────────────
STRATEGY_LONG_COLOR  = "#26a69a"   # 做多進場（綠）
STRATEGY_SHORT_COLOR = "#ef5350"   # 做空進場（紅）
STRATEGY_INFO_COLOR  = "#f0c040"   # 資訊標記（黃）
