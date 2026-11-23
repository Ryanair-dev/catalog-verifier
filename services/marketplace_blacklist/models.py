"""API schemas. Field names mirror analytics.blacklist_listings so the SQL swap is mechanical.

`created_by` maps to blacklist_added_by_initials (required initials, max 4 chars).
`asin` is only kept by the in-memory backend: the live table has no ASIN column.
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

IDENTIFIER_MAX = 64  # NVARCHAR(64)
NOTES_MAX = 1000  # NVARCHAR(1000)
ASIN_MAX = 10
CREATED_BY_MAX = 4  # blacklist_added_by_initials VARCHAR(4)
CREATED_BY_MAX_SQL = CREATED_BY_MAX
BULK_MAX = 5000  # per-request cap for bulk check / bulk add



def clean_identifier(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValueError("identifier cannot be blank")
    if len(value) > IDENTIFIER_MAX:
        raise ValueError(f"identifier exceeds {IDENTIFIER_MAX} characters")
    return value


def clean_asin(value: str | None) -> str | None:
    if value is None:
        return None
    value = "".join(value.split()).upper()
    if not value:
        return None
    if len(value) != ASIN_MAX or not value.isalnum() or not value.isascii():
        raise ValueError("ASIN must be 10 letters/digits")
    return value


def clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    return value or None


def clean_initials(value: str | None) -> str | None:
    value = clean_optional_text(value)
    return value.upper() if value else None


# ---- lookup tables -------------------------------------------------------

class Channel(BaseModel):
    channel_id: int
    channel_name: str


class ReasonCode(BaseModel):
    blacklist_reason_code_id: int
    reason_name: str


class IdentifierType(BaseModel):
    identifier_type_code: int
    identifier_type_name: str


class Lookups(BaseModel):
    channels: list[Channel]
    reason_codes: list[ReasonCode]
    identifier_types: list[IdentifierType]


# ---- listings ------------------------------------------------------------

class ListingCreate(BaseModel):
    identifier: str
    identifier_type_code: int
    channel_id: int
    blacklist_reason_code_id: int
    blacklist_notes: str = Field(default="", max_length=NOTES_MAX)
    asin: str | None = None
    created_by: str | None = Field(default=None, max_length=CREATED_BY_MAX)

    _clean_identifier = field_validator("identifier")(clean_identifier)
    _clean_asin = field_validator("asin")(clean_asin)
    _clean_created_by = field_validator("created_by")(clean_initials)


class ListingUpdate(BaseModel):
    identifier: str | None = None
    identifier_type_code: int | None = None
    channel_id: int | None = None
    blacklist_reason_code_id: int | None = None
    blacklist_notes: str | None = Field(default=None, max_length=NOTES_MAX)
    # "" clears these two; None leaves them unchanged.
    asin: str | None = None
    created_by: str | None = Field(default=None, max_length=CREATED_BY_MAX)

    @field_validator("identifier")
    @classmethod
    def _clean_identifier(cls, v: str | None) -> str | None:
        return None if v is None else clean_identifier(v)

    @field_validator("asin")
    @classmethod
    def _clean_asin(cls, v: str | None) -> str | None:
        return None if v is None else (clean_asin(v) or "")

    @field_validator("created_by")
    @classmethod
    def _clean_created_by(cls, v: str | None) -> str | None:
        return None if v is None else (clean_initials(v) or "")


class Listing(BaseModel):
    blacklist_id: int
    identifier: str
    identifier_type_code: int
    channel_id: int
    blacklist_reason_code_id: int
    blacklist_notes: str
    asin: str | None = None
    created_by: str | None = None
    # None only for rows imported from Excel without a DateAdded (shown as "Not available").
    created_at: datetime | None
    updated_at: datetime | None


class ListingOut(Listing):
    """Listing with lookup names joined in, for display."""
    identifier_type_name: str
    channel_name: str
    reason_name: str


class ListingPage(BaseModel):
    items: list[ListingOut]
    total: int
    page: int
    page_size: int


# ---- bulk ----------------------------------------------------------------

MatchedBy = Literal["identifier", "brand", "asin"]
CheckStatus = Literal["blacklisted", "review", "clear"]


class CheckMatch(ListingOut):
    # identifier: exact match on the blacklisted identifier
    # brand:      the input starts with a blacklisted brand prefix
    # asin:       the input is the ASIN of a blacklisted SKU (another SKU may still sell it)
    matched_by: MatchedBy


class BulkCheckRequest(BaseModel):
    identifiers: list[str] = Field(max_length=BULK_MAX)
    channel_id: int | None = None
    identifier_type_code: int | None = None


class BulkCheckResult(BaseModel):
    identifier: str
    status: CheckStatus
    blacklisted: bool
    matches: list[CheckMatch]


class BulkCheckResponse(BaseModel):
    results: list[BulkCheckResult]
    total_checked: int
    total_blacklisted: int
    total_review: int


class BulkAddRequest(BaseModel):
    identifiers: list[str] = Field(min_length=1, max_length=BULK_MAX)
    identifier_type_code: int
    channel_ids: list[int] = Field(min_length=1)
    blacklist_reason_code_id: int
    blacklist_notes: str = Field(default="", max_length=NOTES_MAX)
    created_by: str | None = Field(default=None, max_length=CREATED_BY_MAX)

    _clean_created_by = field_validator("created_by")(clean_initials)


class BulkAddError(BaseModel):
    identifier: str
    channel_id: int | None = None
    error: str


class BulkAddResponse(BaseModel):
    created: list[ListingOut]
    skipped_existing: list[ListingOut]
    errors: list[BulkAddError]


# ---- bulk rows (the Add many grid) ---------------------------------------

class BulkRow(BaseModel):
    """One grid row: its own type, store, reason and notes."""
    identifier: str
    identifier_type_code: int
    channel_id: int
    blacklist_reason_code_id: int
    blacklist_notes: str = Field(default="", max_length=NOTES_MAX)


class BulkRowsRequest(BaseModel):
    rows: list[BulkRow] = Field(min_length=1, max_length=BULK_MAX)
    created_by: str | None = Field(default=None, max_length=CREATED_BY_MAX)

    _clean_created_by = field_validator("created_by")(clean_initials)


RowStatus = Literal["created", "existing", "error"]


class BulkRowResult(BaseModel):
    row: int  # index into the request's rows, from 0
    status: RowStatus
    listing: ListingOut | None = None  # the new row (created) or the one already there (existing)
    error: str | None = None


class BulkRowsResponse(BaseModel):
    results: list[BulkRowResult]
