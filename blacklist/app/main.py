import csv
import io
from functools import lru_cache
from pathlib import Path

import os

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .models import (
    BulkAddError,
    BulkAddRequest,
    BulkAddResponse,
    BulkCheckRequest,
    BulkCheckResponse,
    BulkRowResult,
    BulkRowsRequest,
    BulkRowsResponse,
    ListingCreate,
    ListingOut,
    ListingPage,
    ListingUpdate,
    Lookups,
    clean_identifier,
)
from .matching import check, dedupe
from .repository import (
    BlacklistRepository,
    DuplicateError,
    InvalidReferenceError,
    NotFoundError,
    RepositoryError,
)

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
load_dotenv(ROOT / ".env")
EXPORT_LIMIT = 100_000
NOT_AVAILABLE = "Not available"
# Legacy reason kept for imported rows. New entries can't use it, and an entry can't be
# changed to it; an entry that already has it may keep it. Matched by name, ignoring case.
LEGACY_REASON_NAME = "Do not send"


def _iso(value) -> str:
    return value.isoformat() if value else NOT_AVAILABLE

app = FastAPI(title="Marketplace Blacklist", version="0.1.0")


@app.middleware("http")
async def _revalidate_assets(request: Request, call_next):
    """Browsers must re-check the page and its files on every load, so updates show without Ctrl+F5."""
    response = await call_next(request)
    path = request.url.path
    if path.endswith("/") or "/static/" in path or path.endswith((".html", ".js", ".css", ".svg")):
        response.headers["Cache-Control"] = "no-cache"
    return response


@lru_cache
def get_repository() -> BlacklistRepository:
    """BLACKLIST_BACKEND=sql uses Azure SQL; anything else uses the local Excel import (dev only)."""
    if os.getenv("BLACKLIST_BACKEND", "memory").lower() == "sql":
        from .sql_repository import SqlRepository

        return SqlRepository()
    from .seed_data import load_repository

    return load_repository()


@app.exception_handler(RepositoryError)
def _db_error(_: Request, exc: RepositoryError):
    return JSONResponse(status_code=503, content={"detail": f"Database unavailable: {exc}"})


def _require_initials(value: str | None) -> None:
    if not (value or "").strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Initials (Added by) are required")


def _reject_legacy_reason(repo: BlacklistRepository, reason_id: int | None) -> None:
    """422 when reason_id is the legacy reason (LEGACY_REASON_NAME)."""
    if reason_id is None:
        return
    legacy = LEGACY_REASON_NAME.strip().casefold()
    if any(r.blacklist_reason_code_id == reason_id and r.reason_name.strip().casefold() == legacy
           for r in repo.lookups().reason_codes):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"“{LEGACY_REASON_NAME}” is only for imported entries. Choose a specific reason.",
        )


Repo = Depends(get_repository)


# ---- lookups -------------------------------------------------------------

@app.get("/api/lookups", response_model=Lookups)
def lookups(repo: BlacklistRepository = Repo):
    return repo.lookups()


# ---- listings ------------------------------------------------------------

@app.get("/api/blacklist", response_model=ListingPage)
def search_listings(
    q: str | None = Query(None, description="Substring match on identifier or notes"),
    channel_id: int | None = None,
    blacklist_reason_code_id: int | None = None,
    identifier_type_code: int | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    repo: BlacklistRepository = Repo,
):
    items, total = repo.search(
        q, channel_id, blacklist_reason_code_id, identifier_type_code,
        offset=(page - 1) * page_size, limit=page_size,
    )
    return ListingPage(items=items, total=total, page=page, page_size=page_size)


@app.get("/api/blacklist/export.csv")
def export_listings(
    q: str | None = None,
    channel_id: int | None = None,
    blacklist_reason_code_id: int | None = None,
    identifier_type_code: int | None = None,
    repo: BlacklistRepository = Repo,
):
    items, _ = repo.search(
        q, channel_id, blacklist_reason_code_id, identifier_type_code, offset=0, limit=EXPORT_LIMIT
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["blacklist_id", "identifier", "identifier_type", "asin", "channel", "reason", "notes",
         "created_by", "created_at", "updated_at"]
    )
    for i in items:
        writer.writerow([
            i.blacklist_id, i.identifier, i.identifier_type_name, i.asin or "", i.channel_name, i.reason_name,
            i.blacklist_notes, i.created_by or NOT_AVAILABLE, _iso(i.created_at), _iso(i.updated_at),
        ])
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="blacklist_export.csv"'},
    )


@app.get("/api/blacklist/{blacklist_id}", response_model=ListingOut)
def get_listing(blacklist_id: int, repo: BlacklistRepository = Repo):
    try:
        return repo.get(blacklist_id)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))


@app.post("/api/blacklist", response_model=ListingOut, status_code=status.HTTP_201_CREATED)
def create_listing(data: ListingCreate, repo: BlacklistRepository = Repo):
    _require_initials(data.created_by)
    _reject_legacy_reason(repo, data.blacklist_reason_code_id)
    try:
        return repo.create(data)
    except DuplicateError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    except InvalidReferenceError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))


@app.patch("/api/blacklist/{blacklist_id}", response_model=ListingOut)
def update_listing(blacklist_id: int, data: ListingUpdate, repo: BlacklistRepository = Repo):
    try:
        # Changing a reason TO the legacy one is rejected; keeping it (same id sent back) is fine.
        if data.blacklist_reason_code_id is not None \
                and data.blacklist_reason_code_id != repo.get(blacklist_id).blacklist_reason_code_id:
            _reject_legacy_reason(repo, data.blacklist_reason_code_id)
        return repo.update(blacklist_id, data)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    except DuplicateError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    except InvalidReferenceError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))


@app.delete("/api/blacklist/{blacklist_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_listing(blacklist_id: int, repo: BlacklistRepository = Repo):
    try:
        repo.delete(blacklist_id)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- bulk ----------------------------------------------------------------

@app.post("/api/blacklist/check", response_model=BulkCheckResponse)
def bulk_check(req: BulkCheckRequest, repo: BlacklistRepository = Repo):
    results = check(repo, req.identifiers, req.channel_id, req.identifier_type_code)
    return BulkCheckResponse(
        results=results,
        total_checked=len(results),
        total_blacklisted=sum(r.status == "blacklisted" for r in results),
        total_review=sum(r.status == "review" for r in results),
    )


@app.post("/api/blacklist/bulk", response_model=BulkAddResponse)
def bulk_add(req: BulkAddRequest, repo: BlacklistRepository = Repo):
    """Blacklist many identifiers across one or more channels. Existing rows are skipped, not overwritten."""
    _require_initials(req.created_by)
    _reject_legacy_reason(repo, req.blacklist_reason_code_id)  # before anything is created
    created, skipped, errors = [], [], []
    for raw in dedupe(req.identifiers):
        try:
            identifier = clean_identifier(raw)
        except ValueError as e:
            errors.append(BulkAddError(identifier=raw, error=str(e)))
            continue
        for channel_id in dict.fromkeys(req.channel_ids):
            try:
                created.append(repo.create(ListingCreate(
                    identifier=identifier,
                    identifier_type_code=req.identifier_type_code,
                    channel_id=channel_id,
                    blacklist_reason_code_id=req.blacklist_reason_code_id,
                    blacklist_notes=req.blacklist_notes,
                    created_by=req.created_by,
                )))
            except DuplicateError as e:
                skipped.append(e.existing)
            except InvalidReferenceError as e:
                errors.append(BulkAddError(identifier=identifier, channel_id=channel_id, error=str(e)))
    return BulkAddResponse(created=created, skipped_existing=skipped, errors=errors)


@app.post("/api/blacklist/bulk-rows", response_model=BulkRowsResponse)
def bulk_add_rows(req: BulkRowsRequest, repo: BlacklistRepository = Repo):
    """Blacklist many rows, each with its own type, store, reason and notes (the Add many grid).

    Every row gets a result, in request order. A row that already exists (in the blacklist or
    earlier in the same request) is reported as existing, not overwritten; a bad row is an error
    and the rest still go in.
    """
    _require_initials(req.created_by)
    legacy = LEGACY_REASON_NAME.strip().casefold()
    legacy_ids = {r.blacklist_reason_code_id for r in repo.lookups().reason_codes
                  if r.reason_name.strip().casefold() == legacy}
    results = []
    for i, r in enumerate(req.rows):
        if r.blacklist_reason_code_id in legacy_ids:
            results.append(BulkRowResult(row=i, status="error", error=(
                f"“{LEGACY_REASON_NAME}” is only for imported entries. Choose a specific reason.")))
            continue
        try:
            data = ListingCreate(
                identifier=r.identifier,
                identifier_type_code=r.identifier_type_code,
                channel_id=r.channel_id,
                blacklist_reason_code_id=r.blacklist_reason_code_id,
                blacklist_notes=r.blacklist_notes.strip(),
                created_by=req.created_by,
            )
        except ValueError as e:  # pydantic's ValidationError is a ValueError
            msg = e.errors()[0]["msg"].removeprefix("Value error, ") if hasattr(e, "errors") else str(e)
            results.append(BulkRowResult(row=i, status="error", error=msg))
            continue
        try:
            results.append(BulkRowResult(row=i, status="created", listing=repo.create(data)))
        except DuplicateError as e:
            results.append(BulkRowResult(row=i, status="existing", listing=e.existing))
        except InvalidReferenceError as e:
            results.append(BulkRowResult(row=i, status="error", error=str(e)))
    return BulkRowsResponse(results=results)


# ---- ops + frontend ------------------------------------------------------

@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok", "backend": os.getenv("BLACKLIST_BACKEND", "memory").lower()}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC_DIR / "blacklist-icon.svg", media_type="image/svg+xml")


@app.get("/", include_in_schema=False)
def index(request: Request):
    # The page uses relative URLs (static/…, api/…) so it works under any path prefix,
    # e.g. https://fps.fordmed.com/blacklist/ - which needs the trailing slash.
    if not request.url.path.endswith("/"):
        return RedirectResponse(request.url.replace(path=request.url.path + "/"), status_code=307)
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
