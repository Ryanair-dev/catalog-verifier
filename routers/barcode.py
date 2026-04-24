"""
Barcode lookup endpoints.

The frontend calls ``POST /barcode/lookup`` for each product after verification
completes. Each call is independent so they run concurrently from the client;
the server stays stateless.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, field_validator

from services.barcode_lookup import aligned_with, lookup_barcode

router = APIRouter()


class BarcodeRequest(BaseModel):
    upc: str
    brand: str | None = None
    vendor_title: str | None = None

    @field_validator("upc", mode="before")
    @classmethod
    def coerce_upc(cls, v):
        return str(v) if v is not None else v


@router.post("/barcode/lookup")
async def barcode_lookup(req: BarcodeRequest) -> dict:
    result = await lookup_barcode(req.upc)
    if result.get("found"):
        result["aligned"] = aligned_with(
            result,
            {"Brand": req.brand, "Vendor Title": req.vendor_title},
        )
    else:
        result["aligned"] = False
    return result
