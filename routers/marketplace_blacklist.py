"""Marketplace Blacklist routes, under /api/marketplace-blacklist.

Ported from the standalone app's blacklist/app/main.py. Same behaviour, three changes:
  * prefix /api/marketplace-blacklist (this app's POST /api/blacklist is the Pair-Manager reject),
  * async def + asyncio.to_thread() (this repo's convention; the repository is blocking pytds),
  * always the Azure SQL repository (no in-memory seed_data mode; tests override get_repository).

WRITES to shared Azure SQL: analytics.blacklist_listings. See docs/features/blacklist.md.
"""
from __future__ import annotations

import asyncio
import csv
import io
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse

from services.marketplace_blacklist.matching import check, dedupe
from services.marketplace_blacklist.models import (
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
from services.marketplace_blacklist.repository import (
    BlacklistRepository,
    DuplicateError,
    InvalidReferenceError,
    NotFoundError,
    RepositoryError,
)

router = APIRouter(prefix="/marketplace-blacklist")

EXPORT_LIMIT = 100_000
NOT_AVAILABLE = "Not available"
# Legacy reason kept for imported rows. New entries can't use it, and an entry can't be
# changed to it; an entry that already has it may keep it. Matched by name, ignoring case.
LEGACY_REASON_NAME = "Do not send"
_LEGACY_MSG = f"“{LEGACY_REASON_NAME}” is only for imported entries. Choose a specific reason."
HTTP_422 = 422  # starlette 0.38 has no HTTP_422_UNPROCESSABLE_CONTENT


@lru_cache
def _sql_repository() -> BlacklistRepository:
    from services.marketplace_blacklist.sql_repository import SqlRepository

    return SqlRepository()


def get_repository() -> BlacklistRepository:
    """The Azure SQL repository (one pooled instance). Tests override this dependency."""
    from services.marketplace_blacklist.sql_repository import is_configured

    if not is_configured():
        raise HTTPException(503, "Database unavailable: Azure SQL is not configured (AZURE_* variables in .env)")
    return _sql_repository()


Repo = Depends(get_repository)


async def _run(fn, *args):
    """Run blocking repository work off the event loop and map repository errors to HTTP codes."""
    try:
        return await asyncio.to_thread(fn, *args)
    except NotFoundError as e:
        raise HTTPException(404, str(e))
    except DuplicateError as e:
        raise HTTPException(409, str(e))
    except InvalidReferenceError as e:
        raise HTTPException(HTTP_422, str(e))
    except RepositoryError as e:
        raise HTTPException(503, f"Database unavailable: {e}")


def _iso(value) -> str:
    return value.isoformat() if value else NOT_AVAILABLE


def _require_initials(value: str | None) -> None:
    if not (value or "").strip():
        raise HTTPException(HTTP_422, "Initials (Added by) are required")


def _legacy_reason_ids(repo: BlacklistRepository) -> set[int]:
    legacy = LEGACY_REASON_NAME.strip().casefold()
    return {r.blacklist_reason_code_id for r in repo.lookups().reason_codes
            if r.reason_name.strip().casefold() == legacy}


def _reject_legacy_reason(repo: BlacklistRepository, reason_id: int | None) -> None:
    """422 when reason_id is the legacy reason (LEGACY_REASON_NAME)."""
    if reason_id is not None and reason_id in _legacy_reason_ids(repo):
        raise HTTPException(HTTP_422, _LEGACY_MSG)


# ---- lookups -------------------------------------------------------------

@router.get("/lookups", response_model=Lookups)
async def lookups(repo: BlacklistRepository = Repo):
    return await _run(repo.lookups)


# ---- listings ------------------------------------------------------------

@router.get("", response_model=ListingPage)
async def search_listings(
    q: str | None = Query(None, description="Substring match on identifier, notes or initials"),
    channel_id: int | None = None,
    blacklist_reason_code_id: int | None = None,
    identifier_type_code: int | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    repo: BlacklistRepository = Repo,
):
    items, total = await _run(
        lambda: repo.search(q, channel_id, blacklist_reason_code_id, identifier_type_code,
                            offset=(page - 1) * page_size, limit=page_size)
    )
    return ListingPage(items=items, total=total, page=page, page_size=page_size)


@router.get("/export.csv")
async def export_listings(
    q: str | None = None,
    channel_id: int | None = None,
    blacklist_reason_code_id: int | None = None,
    identifier_type_code: int | None = None,
    repo: BlacklistRepository = Repo,
):
    def build() -> str:
        items, _ = repo.search(q, channel_id, blacklist_reason_code_id, identifier_type_code,
                               offset=0, limit=EXPORT_LIMIT)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["blacklist_id", "identifier", "identifier_type", "asin", "channel", "reason", "notes",
                         "created_by", "created_at", "updated_at"])
        for i in items:
            writer.writerow([
                i.blacklist_id, i.identifier, i.identifier_type_name, i.asin or "", i.channel_name, i.reason_name,
                i.blacklist_notes, i.created_by or NOT_AVAILABLE, _iso(i.created_at), _iso(i.updated_at),
            ])
        return buf.getvalue()

    body = await _run(build)
    return StreamingResponse(
        iter([body]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="blacklist_export.csv"'},
    )


@router.post("/check", response_model=BulkCheckResponse)
async def bulk_check(req: BulkCheckRequest, repo: BlacklistRepository = Repo):
    results = await _run(check, repo, req.identifiers, req.channel_id, req.identifier_type_code)
    return BulkCheckResponse(
        results=results,
        total_checked=len(results),
        total_blacklisted=sum(r.status == "blacklisted" for r in results),
        total_review=sum(r.status == "review" for r in results),
    )


@router.post("/bulk", response_model=BulkAddResponse)
async def bulk_add(req: BulkAddRequest, repo: BlacklistRepository = Repo):
    """Blacklist many identifiers across one or more channels. Existing rows are skipped, not overwritten."""
    _require_initials(req.created_by)

    def work() -> BulkAddResponse:
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

    return await _run(work)


@router.post("/bulk-rows", response_model=BulkRowsResponse)
async def bulk_add_rows(req: BulkRowsRequest, repo: BlacklistRepository = Repo):
    """Blacklist many rows, each with its own type, store, reason and notes (the Add many grid).

    Every row gets a result, in request order. A row that already exists (in the blacklist or
    earlier in the same request) is reported as existing, not overwritten; a bad row is an error
    and the rest still go in.
    """
    _require_initials(req.created_by)

    def work() -> BulkRowsResponse:
        legacy_ids = _legacy_reason_ids(repo)
        results = []
        for i, r in enumerate(req.rows):
            if r.blacklist_reason_code_id in legacy_ids:
                results.append(BulkRowResult(row=i, status="error", error=_LEGACY_MSG))
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

    return await _run(work)


# Registered after /lookups, /export.csv, /check, /bulk and /bulk-rows so those win over /{id}.
@router.get("/{blacklist_id}", response_model=ListingOut)
async def get_listing(blacklist_id: int, repo: BlacklistRepository = Repo):
    return await _run(repo.get, blacklist_id)


@router.post("", response_model=ListingOut, status_code=201)
async def create_listing(data: ListingCreate, repo: BlacklistRepository = Repo):
    _require_initials(data.created_by)

    def work() -> ListingOut:
        _reject_legacy_reason(repo, data.blacklist_reason_code_id)
        return repo.create(data)

    return await _run(work)


@router.patch("/{blacklist_id}", response_model=ListingOut)
async def update_listing(blacklist_id: int, data: ListingUpdate, repo: BlacklistRepository = Repo):
    def work() -> ListingOut:
        # Changing a reason TO the legacy one is rejected; keeping it (same id sent back) is fine.
        if data.blacklist_reason_code_id is not None \
                and data.blacklist_reason_code_id != repo.get(blacklist_id).blacklist_reason_code_id:
            _reject_legacy_reason(repo, data.blacklist_reason_code_id)
        return repo.update(blacklist_id, data)

    return await _run(work)


@router.delete("/{blacklist_id}", status_code=204)
async def delete_listing(blacklist_id: int, repo: BlacklistRepository = Repo):
    await _run(repo.delete, blacklist_id)
    return Response(status_code=204)
