"""共用基礎設定（零框架依賴）。所有端（Desktop / Server / Worker）共用。"""
from datetime import timezone, timedelta

APP_NAME = "Quantitative Analysis"

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
KLINE_HISTORY_LIMIT = 1500      # Binance REST 單次最大回傳量
KLINE_MAX_LOADED    = 2_628_000  # _loaded_klines 最大長度（支援 5y×1440 根）

# 回測天數選項（格式：UI 標籤 → 1m K 棒數）
BACKTEST_RANGE_OPTIONS = {
    "200根":     200,
    "1 天":     1_440,
    "2 天":     2_880,
    "3 天":     4_320,
    "10 天":   14_400,
    "20 天":   28_800,
    "30 天":   43_200,
    "60 天":   86_400,
    "90 天":  129_600,
    "180 天": 259_200,
    "1 年":   525_600,
    "3 年":  1_576_800,
    "5 年":  2_628_000,
}

# 超過此 K 棒數的回測不更新圖表（避免大量資料渲染卡頓）
BACKTEST_NO_CHART_BARS = 43200   # > 30 天

# ── Footprint ──────────────────────────────────────────────────────────────────
FOOTPRINT_MODES = ["BidxAsk", "Delta", "Volume", "Imbalance"]
FOOTPRINT_MAX_CANDLES = 100  # 保留幾根 K 棒的 Footprint 資料

# ── CVD ────────────────────────────────────────────────────────────────────────
CVD_HISTORY = 5500   # 需 >= KLINE_MAX_LOADED + 緩衝，避免 deque 比 kline 早 drop

# ── Footprint 歷史 aggTrade 回填 ────────────────────────────────────────────────
FOOTPRINT_HISTORY_CANDLES = 20   # 啟動時回填最近 N 根 K 棒的成交

# 各 interval 對應的最大回填時間（毫秒）。
FOOTPRINT_MAX_BACKFILL_MS: dict[str, int] = {
    "1m":  20 * 60 * 1_000,
    "3m":  20 * 3 * 60 * 1_000,
    "5m":  20 * 5 * 60 * 1_000,
    "15m": 10 * 15 * 60 * 1_000,
    "30m": 5  * 30 * 60 * 1_000,
    "1h":  3  * 60 * 60 * 1_000,
    "4h":  2  * 4  * 60 * 60 * 1_000,
}
