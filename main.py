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

from routers import analytics, barcode, brand_analytics, eligibility, export, generic_check, health_check, library, mpn_check, pairs, scans, seller_analytics, settings, storage_fees, verify
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
    _orphans = database.reset_orphaned_running_states()
    # Auto-resume anything that was genuinely mid-work the moment the process
    # died/restarted, instead of leaving it sitting at "Stopped" until someone
    # notices and clicks Resume by hand -- a server restart happens constantly
    # during active development, so "stopped but resumable" silently became
    # "stopped and forgotten" for anything nobody was actively watching.
    try:
        from services.analytics import runner as _analytics_runner
        for _rid in _orphans.get("analytics_run_ids") or []:
            try:
                _analytics_runner.resume_run(_rid)
            except Exception:
                pass
    except Exception:
        pass
    try:
        import json as _json
        from services.analytics import brand_runner as _brand_runner
        for _rid in _orphans.get("brand_run_ids") or []:
            try:
                with database._connect() as _c:
                    _run = _c.execute(
                        "SELECT * FROM brand_analytics_runs WHERE id=?", (_rid,)
                    ).fetchone()
                if _run:
                    _run = dict(_run)
                    try:
                        _terms = _json.loads(_run.get("search_terms") or "[]")
                    except (ValueError, TypeError):
                        _terms = []
                    if _terms:
                        _brand_runner.start_brand_run(
                            _rid, _terms,
                            _run.get("min_rank") or 0, _run.get("max_rank") or 0,
                            _run.get("pages_per_brand") or 10,
                            vetting_mode=_run.get("vetting_mode") or "cpg",
                        )
            except Exception:
                pass
    except Exception:
        pass
    # Every in-memory background job (Offer Analytics export, PO Analytics
    # export, Eligibility/Storage/Generic ASIN checks) that was still running
    # when the process went down gets relaunched fresh here -- see
    # services/job_persistence.py. Must run after every router module has
    # been imported (it has, by the time this coroutine starts) so every
    # relauncher is already registered.
    try:
        from services import job_persistence
        job_persistence.resume_orphaned_jobs()
    except Exception:
        pass
    # Live SellerCloud (Azure) catalog: hourly background refresh (2026-10-07,
    # replaces a one-shot startup-only warm) so PO Analytics/Create SKUs/
    # sc_reference almost always find an already-warm cache (<1h old) instead
    # of paying the ~165s pull inline, and so a change made in SellerCloud
    # shows up here within the hour even with nobody actively using the app.
    # The scheduler's own first loop iteration runs immediately (not after the
    # first hour), so this still warms the cache right at startup exactly like
    # the one-shot thread it replaces. Best-effort; every feature that reads
    # this data already falls back to the local snapshot if Azure is
    # unconfigured/unreachable.
    try:
        from services import azure_sql
        if azure_sql.is_configured():
            azure_sql.start_scheduler()
    except Exception:
        pass
    # Weekly catalog health check (Eligibility + DOG over the Pair Library),
    # scheduled in-process for Friday 19:00 America/New_York.
    try:
        from services import health_check
        health_check.start_scheduler()
    except Exception:
        pass
    # MPN Check: local mirror of fbidb products/manufacturers (hourly,
    # incremental after the first full pull) so a check never hits Azure live.
    try:
        from services import mpn_check_sync
        if azure_sql.is_configured():
            mpn_check_sync.start_scheduler()
    except Exception:
        pass
    yield
    try:
        from services import health_check
        health_check.stop_scheduler()
    except Exception:
        pass
    try:
        from services import mpn_check_sync
        mpn_check_sync.stop_scheduler()
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
app.include_router(storage_fees.router,   prefix="/api", tags=["storage-fees"])
app.include_router(health_check.router,   prefix="/api", tags=["health-check"])
app.include_router(mpn_check.router,      prefix="/api", tags=["mpn-check"])
app.include_router(seller_analytics.router, prefix="/api", tags=["seller-analytics"])


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

    # 0.0.0.0 (not 127.0.0.1) so other devices on the same LAN/wifi can
    # reach this server at http://<this machine's IP>:8000.
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
