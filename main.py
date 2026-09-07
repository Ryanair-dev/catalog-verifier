"""
Amazon Catalog Verification Web App — FastAPI entrypoint.

Serves the static single-page UI from /static and exposes the REST API under /api.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from routers import analytics, barcode, brand_analytics, create_po, eligibility, export, generic_check, health_check, library, pairs, scans, settings, storage_fees, vendor_offers, verify
from services import database  # side-effect: ensures DB tables exist

# Load environment variables early so routers/services can read OPENAI_API_KEY.
# Load from this file's own directory so the cwd doesn't matter (e.g. when the
# server is launched via `uvicorn --app-dir` from a parent directory).
load_dotenv(Path(__file__).resolve().parent / ".env")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Ensure DB is initialised and orphaned run states are cleared on startup."""
    database.init_db()
    database.reset_orphaned_running_states()
    # Warm the live SellerCloud (Azure) catalog cache in the background so the first
    # Create SKUs run isn't blocked by the ~30s initial pull. Best-effort; the
    # feature falls back to the local snapshot if this fails.
    try:
        from services import azure_sql, sc_reference
        if azure_sql.is_configured():
            import threading

            def _warm():
                azure_sql.fetch_rows(force=True)
                # Rebuild the saved brand-prefix / manufacturer reference tables from
                # the fresh pull so Create SKUs reads them instantly (3-char prefixes,
                # company-scoped, Dove→DOV, Nestlé→Nestlé S.A. + purchaser/sourcer).
                try:
                    sc_reference.build_reference(force=False)
                except Exception:
                    pass

            threading.Thread(target=_warm, daemon=True).start()
    except Exception:
        pass
    # Weekly catalog health check (Eligibility + DOG over the Pair Library),
    # scheduled in-process for Friday 19:00 America/New_York.
    try:
        from services import health_check
        health_check.start_scheduler()
    except Exception:
        pass
    yield
    try:
        from services import health_check
        health_check.stop_scheduler()
    except Exception:
        pass


app = FastAPI(
    title="Amazon Catalog Verification",
    description="Automated vetting of CPG catalog products against Amazon listings.",
    version="1.0.0",
    lifespan=_lifespan,
)

# Compress JSON/text responses ≥ 1 KB — big win for large scan result payloads.
app.add_middleware(GZipMiddleware, minimum_size=1000)

def _cors_origins() -> list[str]:
    raw = os.getenv(
        "CORS_ORIGINS",
        "http://127.0.0.1:8000,http://localhost:8000",
    )
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


# Local-first CORS. Override CORS_ORIGINS for non-default frontends.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _optional_api_token(request: Request, call_next):
    """Require X-CV-Token only when CATALOG_VERIFIER_API_TOKEN is configured."""
    token = os.getenv("CATALOG_VERIFIER_API_TOKEN")
    if (
        token
        and request.method != "OPTIONS"
        and request.url.path.startswith("/api/")
        and request.url.path != "/api/health"
        and request.headers.get("x-cv-token") != token
    ):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)

# API routers.
app.include_router(scans.router,    prefix="/api", tags=["scans"])
app.include_router(verify.router,   prefix="/api", tags=["verify"])
app.include_router(barcode.router,  prefix="/api", tags=["barcode"])
app.include_router(export.router,   prefix="/api", tags=["export"])
app.include_router(pairs.router,    prefix="/api", tags=["pairs"])
app.include_router(library.router,  prefix="/api", tags=["library"])
app.include_router(settings.router, prefix="/api", tags=["settings"])
app.include_router(analytics.router,       prefix="/api", tags=["analytics"])
app.include_router(brand_analytics.router, prefix="/api", tags=["brand-analytics"])
app.include_router(eligibility.router,    prefix="/api", tags=["eligibility"])
app.include_router(generic_check.router,  prefix="/api", tags=["generic-check"])
app.include_router(vendor_offers.router,  prefix="/api", tags=["vendor-offers"])
app.include_router(storage_fees.router,   prefix="/api", tags=["storage-fees"])
app.include_router(create_po.router,      prefix="/api", tags=["create-po"])
app.include_router(health_check.router,   prefix="/api", tags=["health-check"])


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
    """Serve the SPA shell — no-cache so the browser always revalidates JS/CSS."""
    return FileResponse(
        str(STATIC_DIR / "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
