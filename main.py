"""
Amazon Catalog Verification Web App — FastAPI entrypoint.

Serves the static single-page UI from /static and exposes the REST API under /api.
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from routers import analytics, barcode, brand_analytics, create_po, eligibility, export, generic_check, health_check, library, mpn_check, pairs, po_analytics, reports, scans, seller_analytics, settings, storage_fees, vendor_offers, verify, walmart
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
    # --- Startup scheduler stagger (2026-10-26) ---------------------------
    # All 6 of the schedulers below used to call their own start_scheduler()
    # directly here, and every one of them runs its FIRST loop iteration
    # immediately (not after its own interval) so the app is warm right at
    # startup. That's fine individually, but running all of them
    # concurrently turned into a real production incident: azure_sql's own
    # first pull alone is documented at "~165s", vendor_offers_mailbox_sync's
    # measured at ~100-116s against real data, and with mpn_check_sync/
    # sc_catalog_index/po_automation ALSO firing at the same moment, the
    # server became unresponsive for 10+ minutes straight on every restart
    # (confirmed independent of any other same-day code change -- reproduced
    # with a fully clean, single process running nothing but this stock
    # startup sequence). CPU climbed into the hundreds of seconds with no
    # recovery, rather than the brief, bounded warm-up this was designed for.
    #
    # Fix: stagger each scheduler's very first start by a fixed delay via
    # threading.Timer, so only one of them is doing its heavy first-run work
    # at any given moment. health_check is exempt -- it only fires at Friday
    # 19:00 America/New_York, never immediately at startup, so it was never
    # part of the pile-up.
    def _delayed_start(seconds: float, fn) -> None:
        t = threading.Timer(seconds, fn)
        t.daemon = True
        t.start()

    # Weekly catalog health check (Eligibility + DOG over the Pair Library),
    # scheduled in-process for Friday 19:00 America/New_York -- not part of
    # the startup pile-up, starts immediately as before.
    try:
        from services import health_check
        health_check.start_scheduler()
    except Exception:
        pass
    # Live SellerCloud (Azure) catalog: hourly background refresh (2026-10-07,
    # replaces a one-shot startup-only warm) so PO Analytics/Create SKUs/
    # sc_reference almost always find an already-warm cache (<1h old) instead
    # of paying the ~165s pull inline, and so a change made in SellerCloud
    # shows up here within the hour even with nobody actively using the app.
    # Best-effort; every feature that reads this data already falls back to
    # the local snapshot if Azure is unconfigured/unreachable. Starts first
    # (0s) of the staggered group -- its own ~165s is the single biggest
    # individual cost, so it gets the whole warm-up window to itself.
    try:
        from services import azure_sql
        if azure_sql.is_configured():
            _delayed_start(0, azure_sql.start_scheduler)
    except Exception:
        pass
    # Vendor Offer Analytics: 30-min sync of the 5 vendors straight from their
    # own catalog emails (replaces the Azure DB sync below for them, per user
    # request 2026-10-23 -- "FBI has awful data structure... take it directly
    # from the offers and inbox ourselves" -- see services/
    # vendor_offers_mailbox_sync.py's module docstring). The old DB-sync
    # module/endpoints are left intact, just not scheduled, as a safety net.
    # Starts at +210s -- after azure_sql's ~165s should be done.
    try:
        from services import vendor_offers_mailbox_sync as vo_mail_sync
        if vo_mail_sync.is_configured():
            _delayed_start(210, vo_mail_sync.start_scheduler)
    except Exception:
        pass
    # vendor_offers_db_sync's hourly Azure scheduler is DELIBERATELY not
    # started any more -- it reads the same FBI-populated tables the mailbox
    # sync above now bypasses, and the two could otherwise fight over
    # avail_qty for the same 5 vendors. Its manual endpoints
    # (/db-sync/status, /db-sync/run) still work if ever needed as a fallback.
    # MPN Check: local mirror of fbidb products/manufacturers (hourly,
    # incremental after the first full pull) so a check never hits Azure live.
    # Starts at +360s -- after the mailbox sync's own ~100-116s should be done.
    try:
        from services import mpn_check_sync
        if azure_sql.is_configured():
            _delayed_start(360, mpn_check_sync.start_scheduler)
    except Exception:
        pass
    # Local SellerCloud catalog snapshot (Create SKUs / PO Analytics' Main
    # SKU -> FBA/FBM children lookup): refresh from SC data.xlsx whenever it's
    # >24h stale. Found live 2026-10-07 sitting 56 days stale with nobody
    # noticing (ANH950685's real FBA child was invisible to PO Analytics as a
    # result) even though the source file itself had been refreshed that same
    # morning — this closes that gap with a periodic background check instead
    # of relying on someone remembering to run `--snapshot` by hand.
    # Starts at +450s.
    try:
        from services.sellercloud import catalog_index as sc_catalog_index
        _delayed_start(450, sc_catalog_index.start_scheduler)
    except Exception:
        pass
    # PO Analytics day-before-ETA automation: hourly check against Azure's own
    # Monday.com mirror (zero Monday API calls — services/monday_eta.py).
    # Dormant until a real recipient list is configured on the Settings page
    # (see services/po_automation.py — check_and_trigger() no-ops otherwise).
    # Starts at +540s.
    try:
        from services import po_automation
        _delayed_start(540, po_automation.start_scheduler)
    except Exception:
        pass
    # Price Desk eligibility warmer (2026-10-28): fills eligibility_cache for
    # every desk ASIN, only while nobody else is using the default store's
    # SP-API -- see services/eligibility_warmer.py. Starts at +630s.
    try:
        from services import eligibility_warmer
        _delayed_start(630, eligibility_warmer.start_scheduler)
    except Exception:
        pass
    # Reports > PO P&L: build the default dashboard's cache once (+720s, after
    # azure_sql's catalog index is warm), so the first visit isn't a cold build.
    try:
        from services import reports_po_pnl
        _delayed_start(720, reports_po_pnl.prewarm)
    except Exception:
        pass
    yield
    try:
        from services import health_check
        health_check.stop_scheduler()
    except Exception:
        pass
    try:
        from services import vendor_offers_db_sync as vo_sync
        vo_sync.stop_scheduler()
    except Exception:
        pass
    try:
        from services import vendor_offers_mailbox_sync as vo_mail_sync
        vo_mail_sync.stop_scheduler()
    except Exception:
        pass
    try:
        from services import mpn_check_sync
        mpn_check_sync.stop_scheduler()
    except Exception:
        pass
    try:
        from services import po_automation
        po_automation.stop_scheduler()
    except Exception:
        pass
    try:
        from services import eligibility_warmer
        eligibility_warmer.stop_scheduler()
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
app.include_router(mpn_check.router,      prefix="/api", tags=["mpn-check"])
app.include_router(walmart.router,        prefix="/api", tags=["walmart"])
app.include_router(po_analytics.router,   prefix="/api", tags=["po-analytics"])
app.include_router(seller_analytics.router, prefix="/api", tags=["seller-analytics"])
app.include_router(reports.router,          prefix="/api", tags=["reports"])


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
