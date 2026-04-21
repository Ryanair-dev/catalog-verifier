"""
Abbreviation library endpoints (categorised, SQLite-backed).

Shape returned by ``GET /library``::

    {
      "Colors":   [{"id": 1, "abbr": "white", "full": "white"}, ...],
      "Sizes":    [...],
      "UOMs":     [...],
      ...
    }
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services import database

router = APIRouter()


class LibraryEntry(BaseModel):
    category: str
    abbr: str
    full: str


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
    ok = database.add_library_entry(body.category, body.abbr, body.full)
    if not ok:
        raise HTTPException(status_code=400, detail="Duplicate or empty entry")
    return {"ok": True, "library": database.list_library()}


@router.post("/library/delete")
async def delete_entry(body: LibraryDelete) -> dict:
    deleted = database.delete_library_entry(body.id)
    return {"deleted": deleted > 0, "library": database.list_library()}
