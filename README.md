# OrderFlow — Binance Futures Order Flow Analyzer

A real-time order flow analysis desktop application for Binance USDT-M Futures, built with Python, PyQt6, and pyqtgraph.

![Python](https://img.shields.io/badge/Python-3.12%2B-blue)
![PyQt6](https://img.shields.io/badge/PyQt6-6.4%2B-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## Features

| Module | Description |
|---|---|
| **K-Line Chart** | Candlestick chart with volume bars, log/linear Y-axis toggle, smart auto-scroll |
| **Footprint Chart** | Price-level bid/ask volume breakdown per candle with 4 display modes |
| **CVD Chart** | Cumulative Volume Delta — line or bar mode with positive/negative fill |
| **Stats Panel** | Per-candle Volume / Delta / CVD numeric summary |
| **Order Book (Level 2)** | Real-time bid/ask ladder with price-weighted color depth |
| **OB Heatmap** | Time-series order book heatmap showing liquidity distribution |
| **Tick-Level Backtest** | Bar or tick mode strategy simulation from local aggTrades zip files |
| **Background Tick Cache Worker** | Pre-parses aggTrades zip files into NPZ cache so UI loads instantly |

### Footprint Modes
- **BidxAsk** — Left column = taker sell, right column = taker buy; dominance coloring
- **Delta** — Cell center shows `bid_vol − ask_vol` with directional color
- **Volume** — Cell center shows total volume with heat coloring (cold → warm)
- **Imbalance** — BidxAsk layout with 3:1 imbalance cell highlighting and stacked imbalance side bars

### Supported Symbols
`BTCUSDT`, `ETHUSDT`, `BNBUSDT`, `SOLUSDT`, `XRPUSDT`, `DOGEUSDT`, `ADAUSDT`, `AVAXUSDT`

### Supported Intervals
`1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `4h`

---

## Architecture

```
OrderFlow/
├── main.py                  # Entry point — QApplication setup
├── config.py                # Global configuration (symbols, colors, intervals)
├── requirements.txt
│
├── core/
│   ├── ws_client.py         # WsWorkerThread — asyncio WebSocket + REST in QThread
│   ├── history_processor.py # HistoryProcessorThread — background aggTrade bucketing
│   ├── cvd_calculator.py    # CVD accumulator with history seeding
│   ├── footprint_builder.py # Footprint candle builder (tick-size bucketing)
│   ├── order_book.py        # Level-2 order book with diff application
│   ├── tick_cache.py        # aggTrades NPZ cache (load/save/merge/build_bar_map)
│   └── data_types.py        # Dataclasses: Trade, Kline, FootprintCandle, etc.
│
├── ui/
│   ├── main_window.py       # MainWindow — layout, toolbar, signal routing
│   ├── kline_chart.py       # KlineChart widget (candlestick + volume)
│   ├── cvd_chart.py         # CvdChart + StatsPanel widgets
│   ├── footprint_widget.py  # FootprintChart widget
│   ├── order_book_widget.py # OrderBookWidget (Level 2 ladder)
│   └── heatmap_widget.py    # HeatmapWidget (OB time-series heatmap)
│
├── strategies/
│   ├── base.py              # StrategyBase + StrategySignal dataclass
│   ├── wick_reversal_v4.py  # Wick Reversal 1m v4 (long + short, tick-first)
│   └── __init__.py          # STRATEGY_REGISTRY
│
├── backtest/
│   └── engine.py            # simulate_trades — fee / slippage / drawdown accounting
│
├── utils/
│   ├── tick_data_backtest.py  # CLI: run strategy on local aggTrades zip files
│   ├── tick_cache_worker.py   # Background worker: zip → NPZ cache (incremental)
│   └── time_utils.py          # Timezone-aware time formatting (UTC+8)
│
└── data/
    └── ticks/               # Auto-created; stores {SYMBOL}_ticks.npz + manifest
```

### Threading Model

```
Main Thread (Qt)
│
├── WsWorkerThread (QThread)
│     └── asyncio event loop
│           ├── REST: fetch kline history + OB snapshot + aggTrade history
│           └── WebSocket: kline stream + aggTrade stream + depth stream
│
├── HistoryProcessorThread (QThread)
│     └── Background aggTrade → Footprint bucketing (bisect O(N log M))
│
└── TickImportThread (QThread)       ← triggered by toolbar "匯入 Tick" button
      └── merge_and_save_array → data/ticks/{SYMBOL}_ticks.npz
```

**Background tick cache worker** (separate process, optional):
```
tick_cache_worker.py  ──parse zips──►  data/ticks/{SYMBOL}_ticks.npz
                                                    │
                              UI opens / runs backtest in Tick mode
                                                    │
                                        tick_cache.load_range()
                                        build_bar_map()
                                        _build_klines_from_ticks()
```

All data flows from worker threads to the main thread exclusively via Qt signals, guaranteeing thread safety without explicit locking.

---

## Installation

### Requirements
- Python 3.12+
- Windows / macOS / Linux
- Active internet connection (Binance Futures public API — no API key required)

### Setup

```bash
git clone https://github.com/andy60306637/Order-flow.git
cd Order-flow
pip install -r requirements.txt
python main.py
```

### Dependencies

```
PyQt6>=6.4.0
pyqtgraph>=0.13.3
websockets>=11.0
aiohttp>=3.9.0
numpy>=1.24.0
pandas>=1.5.0   # optional but recommended — 10-50x faster zip parsing
```

---

## Running the Pre-built Executable (Windows)

Download `OrderFlow.exe` from the [Releases](../../releases) page and run it directly — no Python installation required.

---

## Building from Source (Windows)

```bash
pip install pyinstaller
python -m PyInstaller orderflow.spec --clean
# Output: dist\OrderFlow.exe
```

Or use the convenience script:

```bat
build.bat
```

---

## Configuration

Edit [`config.py`](config.py) to customize:

| Setting | Default | Description |
|---|---|---|
| `DEFAULT_SYMBOL` | `BTCUSDT` | Symbol shown on startup |
| `DEFAULT_INTERVAL` | `1m` | Interval shown on startup |
| `KLINE_HISTORY_LIMIT` | `200` | Number of historical candles to load |
| `FOOTPRINT_HISTORY_CANDLES` | `20` | Candles backfilled with aggTrade history |
| `FOOTPRINT_MAX_CANDLES` | `100` | Maximum footprint candles kept in memory |
| `HEATMAP_UPDATE_MS` | `1000` | Order book heatmap snapshot interval (ms) |
| `DISPLAY_TZ` | `UTC+8` | Timezone for all chart time axes |
| `OB_DISPLAY_LEVELS` | `20` | Order book depth levels shown |

---

## Usage

### Real-time UI

1. **Select symbol and interval** from the toolbar dropdowns
2. **Switch between K-Line and Footprint** using the tab bar
3. **Switch Footprint mode** (BidxAsk / Delta / Volume / Imbalance) from the toolbar
4. **Toggle Log scale** on the K-line Y-axis with the `Log` button
5. **Scroll / zoom** the X-axis freely — all charts (K-Line, Footprint, CVD, Stats) stay synchronized
6. The chart **auto-scrolls** only when you are viewing the latest candles; manually scrolled views stay in place

### Tick-Level Backtest (CLI)

Download daily aggTrades zip files from [data.binance.vision](https://data.binance.vision/?prefix=data/futures/um/daily/aggTrades/) and run:

```bash
# First run — parses zips and builds NPZ cache (~1-3 min for 90 days)
python utils/tick_data_backtest.py \
    --symbol BTCUSDT \
    --tick-dir "tick_data/binance/futures/um/daily/aggTrades/BTCUSDT"

# Subsequent runs — loads from NPZ cache (~5-30 sec)
python utils/tick_data_backtest.py \
    --symbol BTCUSDT \
    --tick-dir "tick_data/binance/futures/um/daily/aggTrades/BTCUSDT"

# Force rebuild cache
python utils/tick_data_backtest.py ... --rebuild-cache
```

### Background Tick Cache Worker

Pre-parse zip files in the background so the UI and backtest CLI load instantly:

```bash
# Parse all pending zips once and exit
python utils/tick_cache_worker.py \
    --symbol BTCUSDT \
    --tick-dir "tick_data/binance/futures/um/daily/aggTrades/BTCUSDT"

# Watch mode — re-scan every 60 s, picks up newly downloaded zips automatically
python utils/tick_cache_worker.py \
    --symbol BTCUSDT \
    --tick-dir "tick_data/binance/futures/um/daily/aggTrades/BTCUSDT" \
    --watch --interval 60

# Multiple symbols via config file
python utils/tick_cache_worker.py --config worker_config.json --watch
```

`worker_config.json` example:
```json
[
  {"symbol": "BTCUSDT", "tick_dir": "tick_data/binance/futures/um/daily/aggTrades/BTCUSDT"},
  {"symbol": "ETHUSDT", "tick_dir": "tick_data/binance/futures/um/daily/aggTrades/ETHUSDT"}
]
```

Once the worker has run, open the UI and select **Tick 模式** in the backtest panel — the cached data is loaded automatically with no further import step required. If the worker updated the cache while the UI was open, switch the symbol and switch back (or reopen) to pick up the latest data.

**Cache location:** `data/ticks/{SYMBOL}_ticks.npz`  
**Manifest:** `data/ticks/{SYMBOL}_manifest.json` — tracks which zips have been processed; only new or modified zips are re-parsed on subsequent runs.

---

## Data Source

All data is fetched from **Binance USDT-M Futures public endpoints** — no API key is needed:

| Data | Endpoint |
|---|---|
| Historical K-lines | `GET /fapi/v1/klines` |
| Order book snapshot | `GET /fapi/v1/depth` |
| Historical aggTrades | `GET /fapi/v1/aggTrades` |
| Real-time streams | `wss://fstream.binance.com` combined stream |

---

## License

MIT License — see [LICENSE](LICENSE) for details.
