# Project OrderFlow — CLAUDE.md

This document provides project-specific guidance for Claude. It is synchronized with the root `GEMINI.md` to ensure consistent engineering standards and operational protocols.

---

## 1. Project Vision & Goals
*   **Mission**: High-performance Binance Futures Order Flow platform.
*   **Values**: NumPy-driven performance, precision reconstruction, and unified research/execution.

## 2. Engineering Standards
*   **Stack**: Python 3.12+, PyQt6, AsyncIO, NumPy.
*   **Style**: PEP 8 with mandatory Type Hints (`from __future__ import annotations`).
*   **Architecture**: Strict decoupling between `core/` (data logic) and `ui/` (PyQt6).

## 3. Commit & Push Rules
*   **Format**: `[Tag] Description`
    *   `[A]` (Add): New features, files, or deps.
    *   `[M]` (Modify): Refactor, perf, or config.
    *   `[F]` (Fix): Bug fixes or logic corrections.
*   **Protocol**: Propose changes and commit messages before execution. No auto-push.

## 4. Testing & Validation
*   **Framework**: `pytest`.
*   **Requirement**: Maintain 90%+ coverage on `core/`. Ensure UI/CLI data consistency.

## 5. Environment & Automation
*   **Tool**: `uv` recommended.
*   **Scripts**: Use `build.bat`, `tick_cache_worker.py`, and `fast_backtest.py` for automation.
