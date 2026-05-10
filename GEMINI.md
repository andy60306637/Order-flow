# Project OrderFlow — GEMINI.md

This document serves as the foundational mandate for all AI assistants (Gemini, Claude, Codex, etc.) working on the OrderFlow project. It defines the project's vision, engineering standards, and operational protocols.

---

## 1. Project Vision & Goals

### 1.1 Core Mission
To build a professional-grade, high-performance Binance Futures Order Flow analysis and strategy research platform. The project provides deep market microstructure insights beyond traditional candlestick charts.

### 1.2 Core Values
*   **Performance First**: Utilize NumPy and multi-threading to ensure a fluid UI even when processing high-frequency tick data.
*   **Precision Reconstruction**: Accurately reconstruct market activity (Footprint, Heatmap) from `aggTrades` and `depth` data.
*   **Unified Research & Execution**: Use a single `StrategyBase` for both real-time monitoring and backtesting to ensure strategy executability.
*   **Modularity**: Maintain a framework-agnostic `DataEngine` to power both PyQt6 UI and headless optimization environments.

### 1.3 Key Objectives
*   Optimize rendering efficiency for Footprint and Order Book Heatmap.
*   Enhance backtest engine realism (fees, slippage, latency).
*   Expand the library of order-flow-based strategies (e.g., Wick Reversal, CVD Divergence).

---

## 2. Engineering Standards

### 2.1 Coding Style
*   **Language**: Python 3.12+.
*   **Naming**:
    *   Functions/Variables: `snake_case`.
    *   Classes: `PascalCase`.
    *   Constants: `UPPER_SNAKE_CASE`.
    *   Private Members: `_leading_underscore`.
*   **Typing**: Mandatory Type Hints using `from __future__ import annotations`. Use `dataclass` or `TypedDict` for complex structures.
*   **Logic**: Prefer composition over inheritance. Data logic (`core/`) must be strictly decoupled from UI logic (`ui/`).

### 2.2 Async & Concurrency
*   **AsyncIO**: Use for all network I/O (WebSocket/HTTP).
*   **Threading**:
    *   **UI Thread**: Rendering only. Never block the main thread.
    *   **Worker Threads**: Use `QThread` for heavy parsing or computation.
    *   **Communication**: Use Qt Signals or `DataEngine` callbacks. No direct cross-thread object manipulation.

### 2.3 Data Optimization
*   **NumPy**: Use vectorized operations for all tick-level calculations. Avoid native Python loops for large datasets.
*   **Memory**: Implement strict buffers (e.g., `FOOTPRINT_MAX_CANDLES`) and efficient caching (`tick_cache.py`).

---

## 3. Commit & Push Rules

### 3.1 Commit Message Format
Every commit title **must** start with one of the following tags:
*   **`[A]` (Add)**: New features, strategies, files, or dependencies.
*   **`[M]` (Modify)**: Modifications, refactoring, performance tuning, or configuration updates.
*   **`[F]` (Fix)**: Bug fixes, exception handling, or logic corrections.

**Example**: `[A] Add Footprint Imbalance mode`

### 3.2 Security & Privacy
*   **No Secrets**: Never commit `.env`, API keys, or private credentials.
*   **Gitignore**: Adhere strictly to `.gitignore` (ignore `data/ticks/`, `__pycache__`, etc.).

### 3.3 AI Agent Protocol
Applicable to all AI assistants (Gemini, Claude, Codex):
*   **Proposal First**: Summarize changes and propose a commit message in the `[Tag] Description` format before committing.
*   **No Auto-Push**: Never execute `git push` unless explicitly instructed.
*   **Atomic Commits**: Keep commits focused on a single logical change.

---

## 4. Testing & Validation

### 4.1 Data Accuracy
*   Ensure 100% consistency between UI calculations and CLI tool outputs.
*   Run benchmarks after modifying high-performance modules to prevent regressions.

### 4.2 Automated Testing
*   **Framework**: `pytest`.
*   **Coverage**: Aim for 90%+ on `core/` logic.
*   **Regression**: Always add a test case for every bug fix.

### 4.3 Strategy Validation
*   New strategies must pass tick-level backtesting via `backtest/engine.py`.
*   Explain parameter sensitivity (Win Rate, RR, MDD) when proposing optimizations.

---

## 5. Environment & Automation

### 5.1 Dependency Management
*   **Tool**: `uv` is recommended for fast environment and dependency management.
*   **Files**: Maintain `requirements.txt` and specific lists in `requirements/*.txt`.
*   **Sync**: Run `pip freeze` or equivalent after installing new packages.

### 5.2 Automation Scripts
*   **Build**: Use `build.bat` for PyInstaller packaging.
*   **Parsing**: Use `utils/tick_cache_worker.py` for background data processing.
*   **Backtest**: Use `utils/fast_backtest.py` for quick validation.
*   **Cache**: Use `utils/rebuild_tick_cache_once.py` after data structure changes.

### 5.3 Build Verification
*   Always verify the generated `dist/OrderFlow.exe` in a clean environment before release.
