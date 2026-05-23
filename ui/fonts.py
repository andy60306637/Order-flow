"""Cross-platform font helpers for the PyQt6 UI.

Windows fonts like Consolas and Segoe UI are not available on Linux.
Use these helpers instead of hardcoding font names.
"""
from __future__ import annotations

from PyQt6.QtGui import QFont, QFontDatabase


def _first_available(families: list[str]) -> str:
    db = QFontDatabase.families()
    for fam in families:
        if fam in db:
            return fam
    return families[-1]  # always returns the generic fallback


# ── Monospace ────────────────────────────────────────────────────────────────
_MONO_FAMILIES = [
    "Consolas",          # Windows
    "DejaVu Sans Mono",  # Linux (Ubuntu/Debian)
    "Liberation Mono",   # Linux (RHEL/Fedora)
    "Noto Mono",         # Linux (Noto fonts)
    "Courier New",       # Cross-platform fallback
    "monospace",         # Generic CSS fallback
]

# ── UI / Sans-serif ──────────────────────────────────────────────────────────
_UI_FAMILIES = [
    "Segoe UI",    # Windows
    "Ubuntu",      # Ubuntu Linux
    "Noto Sans",   # Cross-platform (if Noto installed)
    "Liberation Sans",
    "sans-serif",  # Generic
]

_cached_mono: str | None = None
_cached_ui:   str | None = None


def mono_family() -> str:
    global _cached_mono
    if _cached_mono is None:
        _cached_mono = _first_available(_MONO_FAMILIES)
    return _cached_mono


def ui_family() -> str:
    global _cached_ui
    if _cached_ui is None:
        _cached_ui = _first_available(_UI_FAMILIES)
    return _cached_ui


def mono(size: int, bold: bool = False) -> QFont:
    f = QFont(mono_family(), size)
    if bold:
        f.setWeight(QFont.Weight.Bold)
    return f


def ui(size: int, bold: bool = False) -> QFont:
    f = QFont(ui_family(), size)
    if bold:
        f.setWeight(QFont.Weight.Bold)
    return f
