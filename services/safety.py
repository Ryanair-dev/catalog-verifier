"""Shared input/output safety helpers."""
from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException, UploadFile


def _max_upload_bytes() -> int:
    raw = os.getenv("CATALOG_VERIFIER_MAX_UPLOAD_MB", "25")
    try:
        mb = int(raw)
    except (TypeError, ValueError):
        mb = 25
    return max(1, mb) * 1024 * 1024


async def read_upload_limited(file: UploadFile) -> bytes:
    """Read an upload with a hard server-side size cap."""
    limit = _max_upload_bytes()
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"Upload too large. Max size is {limit // (1024 * 1024)} MB.",
        )
    return data


def clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(minimum, min(maximum, n))


def safe_spreadsheet_value(value: Any) -> Any:
    """Prevent spreadsheet formula execution when exporting user-provided text."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def safe_spreadsheet_row(values: list[Any]) -> list[Any]:
    return [safe_spreadsheet_value(v) for v in values]
