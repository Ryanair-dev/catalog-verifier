"""
Abbreviation library endpoints (categorised, SQLite-backed).

Shape returned by ``GET /library``::

    {
      "categories": ["Colors", "Sizes", ...],
      "library": {
        "Colors":   [{"id":1,"abbr":"white","full":"white","added_by":"system"}, ...],
        "Sizes":    [...],
        ...
      }
    }
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from services import database

router = APIRouter()


class LibraryEntry(BaseModel):
    category: str
    abbr: str
    full: str
    added_by: str | None = "user"


class LibraryDelete(BaseModel):
    id: int


@router.get("/library")
async def get_library() -> dict:
    return {
        "categories": database.VALID_CATEGORIES,
        "library": database.list_library(),
    }


@router.post("/library")
async def add_entry(body: LibraryEntry) -> dict:
    created, entry = database.add_library_entry(
        body.category, body.abbr, body.full,
        added_by=body.added_by or "user",
    )
    return {
        "ok": created,
        "duplicate": not created and entry is not None,
        "entry": entry,
        "library": database.list_library(),
    }


@router.post("/library/delete")
async def delete_entry(body: LibraryDelete) -> dict:
    deleted = database.delete_library_entry(body.id)
    return {"deleted": deleted > 0, "library": database.list_library()}
