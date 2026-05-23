"""
OrderFlow FastAPI server application.

啟動方式:
    python server_main.py
    # 或
    uvicorn server.app:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# 確保 project root 在 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from config.server import HOST, PORT, CORS_ORIGINS
from server.routes import backtest, research, market, pipeline, settings as settings_route

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(
    title="OrderFlow API",
    description="Binance Futures Order Flow — Web API",
    version="1.0.0",
)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API routers ───────────────────────────────────────────────────────────────
app.include_router(backtest.router)
app.include_router(research.router)
app.include_router(market.router)
app.include_router(pipeline.router)
app.include_router(settings_route.router)


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


# ── Serve Vue 3 SPA (production build) ───────────────────────────────────────
_WEB_DIST = _PROJECT_ROOT / "web" / "dist"

if _WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(_WEB_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        index = _WEB_DIST / "index.html"
        return FileResponse(str(index))
