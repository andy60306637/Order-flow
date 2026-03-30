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
└── utils/
    └── time_utils.py        # Timezone-aware time formatting (UTC+8)
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
└── HistoryProcessorThread (QThread)
      └── Background aggTrade → Footprint bucketing (bisect O(N log M))
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

1. **Select symbol and interval** from the toolbar dropdowns
2. **Switch between K-Line and Footprint** using the tab bar
3. **Switch Footprint mode** (BidxAsk / Delta / Volume / Imbalance) from the toolbar
4. **Toggle Log scale** on the K-line Y-axis with the `Log` button
5. **Scroll / zoom** the X-axis freely — all charts (K-Line, Footprint, CVD, Stats) stay synchronized
6. The chart **auto-scrolls** only when you are viewing the latest candles; manually scrolled views stay in place

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
