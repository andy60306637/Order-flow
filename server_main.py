"""
OrderFlow Web Server — 入口點。

等效於 main.py（PyQt6 GUI），但啟動 FastAPI + Vue 3 Web 介面。

Usage:
    python server_main.py [--host HOST] [--port PORT] [--reload]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 確保 project root 在 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")


def main() -> None:
    parser = argparse.ArgumentParser(description="OrderFlow Web Server")
    parser.add_argument("--host",   default="0.0.0.0",  help="Bind host")
    parser.add_argument("--port",   type=int, default=8000, help="Bind port")
    parser.add_argument("--reload", action="store_true",   help="Hot reload (dev mode)")
    args = parser.parse_args()

    import uvicorn
    uvicorn.run(
        "server.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
