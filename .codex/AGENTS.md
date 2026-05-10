# Project OrderFlow — AGENTS.md

This document provides project-specific guidance for Codex and other AI agents. It mirrors the root `GEMINI.md` to maintain unified engineering standards.

---

## 1. Project Vision & Goals
*   Professional-grade Binance Futures Order Flow analysis and strategy research.
*   Key modules: Framework-agnostic `DataEngine`, PyQt6 UI, and Tick-level backtester.

## 2. Engineering Standards
*   **Python**: 3.12+ with vectorized NumPy operations.
*   **Conventions**: `snake_case` for functions/vars, `PascalCase` for classes.
*   **Concurrency**: AsyncIO for network, QThread for computation. Thread-safe UI updates via Signals.

## 3. Commit & Push Rules
*   **Tags**:
    *   `[A]` (Add)
    *   `[M]` (Modify)
    *   `[F]` (Fix)
*   **Workflow**: Summarize changes -> Propose `[Tag] Description` -> Commit upon approval.

## 4. Testing & Validation
*   **Validation**: 100% data consistency between UI and CLI.
*   **Tests**: Use `pytest`. Add regression tests for every fix.

## 5. Environment & Automation
*   **Package Manager**: `uv`.
*   **Automation**: Prioritize using existing scripts in `utils/` and `build.bat`.
