"""
Settings endpoints.

Currently exposes the two adjustable confidence thresholds
(``verified`` boundary and ``review`` boundary). A third implicit boundary —
the 0% floor — is static.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services import database

router = APIRouter()


class Thresholds(BaseModel):
    verified: float
    review: float


@router.get("/settings/thresholds")
async def get_thresholds() -> dict:
    return database.get_thresholds()


@router.post("/settings/thresholds")
async def set_thresholds(body: Thresholds) -> dict:
    if not (0 <= body.review <= body.verified <= 100):
        raise HTTPException(
            status_code=400,
            detail="Thresholds must satisfy 0 ≤ review ≤ verified ≤ 100",
        )
    database.set_thresholds(body.verified, body.review)
    return database.get_thresholds()
