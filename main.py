"""
Amazon Catalog Verification Web App — FastAPI entrypoint.

Serves the static single-page UI from /static and exposes the REST API under /api.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from routers import barcode, export, library, pairs, settings, verify
from services import database  # side-effect: ensures DB tables exist

# Load environment variables early so routers/services can read OPENAI_API_KEY.
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="Amazon Catalog Verification",
    description="Automated vetting of CPG catalog products against Amazon listings.",
    version="1.0.0",
)

# Permissive CORS — this app is intended to run locally for now.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routers.
app.include_router(verify.router,   prefix="/api", tags=["verify"])
app.include_router(barcode.router,  prefix="/api", tags=["barcode"])
app.include_router(export.router,   prefix="/api", tags=["export"])
app.include_router(pairs.router,    prefix="/api", tags=["pairs"])
app.include_router(library.router,  prefix="/api", tags=["library"])
app.include_router(settings.router, prefix="/api", tags=["settings"])


@app.on_event("startup")
def _startup() -> None:
    """Ensure DB is initialised on startup even if services.database wasn't
    imported through a side-effect path."""
    database.init_db()


@app.get("/api/health")
def health() -> dict:
    """Simple health probe — also reports whether GPT-4o can be reached."""
    return {
        "status": "ok",
        "ai_available": bool(os.getenv("OPENAI_API_KEY")),
    }


# Serve static assets (CSS, JS, JSON, sample data) under /static.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def root() -> FileResponse:
    """Serve the SPA shell."""
    return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
