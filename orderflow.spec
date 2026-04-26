# -*- mode: python ; coding: utf-8 -*-
# Quantitative Analysis — PyInstaller 打包設定（相容 PyInstaller 6.x）
# 使用方式：python -m PyInstaller orderflow.spec
from PyInstaller.utils.hooks import collect_all, collect_submodules

# ── 自動收集動態模組 ──────────────────────────────────────────────────────────
aiohttp_datas,   aiohttp_binaries,   aiohttp_hidden   = collect_all("aiohttp")
pyqtgraph_datas, pyqtgraph_binaries, pyqtgraph_hidden = collect_all("pyqtgraph")
websockets_datas, websockets_binaries, websockets_hidden = collect_all("websockets")

all_datas    = aiohttp_datas    + pyqtgraph_datas    + websockets_datas
all_binaries = aiohttp_binaries + pyqtgraph_binaries + websockets_binaries
all_hidden   = (
    aiohttp_hidden
    + pyqtgraph_hidden
    + websockets_hidden
    + collect_submodules("numpy")
    + collect_submodules("strategies")
    + collect_submodules("strategies.modules")
    + [
        "PyQt6.sip",
        "PyQt6.QtCore",
        "PyQt6.QtGui",
        "PyQt6.QtWidgets",
        "PyQt6.QtOpenGL",
        "PyQt6.QtSvg",
        "PyQt6.QtPrintSupport",
        "aiohttp.cookiejar",
        "aiohttp.client_ws",
        "aiohttp._websocket",
        "aiohttp.connector",
        "aiohttp.resolver",
        "charset_normalizer",
        "asyncio.windows_events",
        "asyncio.windows_utils",
    ]
)

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=all_binaries,
    datas=all_datas,
    hiddenimports=all_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "scipy", "PIL",
        "IPython", "jupyter", "pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# ── 單一 exe ──────────────────────────────────────────────────────────────────
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="QuantitativeAnalysis",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

