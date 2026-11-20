"""Data access layer.

`BlacklistRepository` is the contract the API depends on. `InMemoryRepository`
backs it with data imported from the Excel blacklist for now; an Azure SQL
implementation of the same interface replaces it at launch (see README
"Moving to Azure SQL").

Semantics deliberately match SQL Server so nothing changes at the swap:
  * identifier comparisons are case-insensitive (default *_CI_AS collation),
  * (identifier, identifier_type_code, channel_id) is unique -> DuplicateError,
  * unknown channel / reason / identifier type -> InvalidReferenceError (FK violation).
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Iterable

from .models import (
    Channel,
    IdentifierType,
    Listing,
    ListingCreate,
    ListingOut,
    ListingUpdate,
    Lookups,
    ReasonCode,
)


class RepositoryError(Exception):
    pass


class NotFoundError(RepositoryError):
    pass


class DuplicateError(RepositoryError):
    def __init__(self, existing: ListingOut):
        super().__init__(
            f"'{existing.identifier}' ({existing.identifier_type_name}) is already "
            f"blacklisted on {existing.channel_name}"
        )
        self.existing = existing


class InvalidReferenceError(RepositoryError):
    pass


class BlacklistRepository(ABC):
    @abstractmethod
    def lookups(self) -> Lookups: ...

    @abstractmethod
    def search(
        self,
        q: str | None = None,
        channel_id: int | None = None,
        blacklist_reason_code_id: int | None = None,
        identifier_type_code: int | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[ListingOut], int]: ...

    @abstractmethod
    def get(self, blacklist_id: int) -> ListingOut: ...

    @abstractmethod
    def create(self, data: ListingCreate) -> ListingOut: ...

    @abstractmethod
    def update(self, blacklist_id: int, data: ListingUpdate) -> ListingOut: ...

    @abstractmethod
    def delete(self, blacklist_id: int) -> None: ...

    @abstractmethod
    def find_by_identifiers(
        self,
        identifiers: Iterable[str],
        channel_id: int | None = None,
        identifier_type_code: int | None = None,
    ) -> list[ListingOut]:
        """Exact (case-insensitive) matches for any of the given identifiers."""

    @abstractmethod
    def find_by_asins(self, asins: Iterable[str], channel_id: int | None = None) -> list[ListingOut]:
        """Rows whose `asin` column is one of the given ASINs."""

    @abstractmethod
    def list_by_type(self, identifier_type_code: int, channel_id: int | None = None) -> list[ListingOut]:
        """All rows of one identifier type (used to load the small set of brand-prefix rules)."""


_NO_DATE = datetime.min.replace(tzinfo=timezone.utc)


def _key(identifier: str) -> str:
    return identifier.casefold()


class InMemoryRepository(BlacklistRepository):
    def __init__(
        self,
        channels: list[Channel],
        reason_codes: list[ReasonCode],
        identifier_types: list[IdentifierType],
        listings: Iterable[ListingCreate | dict] = (),
    ):
        """`listings` may be ListingCreate objects, or dicts that also carry created_at/updated_at (imports)."""
        self._lock = threading.RLock()
        self._channels = {c.channel_id: c for c in channels}
        self._reasons = {r.blacklist_reason_code_id: r for r in reason_codes}
        self._types = {t.identifier_type_code: t for t in identifier_types}
        self._rows: dict[int, Listing] = {}
        self._next_id = 1
        for item in listings:
            if isinstance(item, dict):
                self._insert(
                    ListingCreate.model_validate(item),
                    created_at=item.get("created_at"),
                    updated_at=item.get("updated_at"),
                    imported=True,
                )
            else:
                self._insert(item)

    # -- helpers -----------------------------------------------------------

    def _enrich(self, row: Listing) -> ListingOut:
        return ListingOut(
            **row.model_dump(),
            identifier_type_name=self._types[row.identifier_type_code].identifier_type_name,
            channel_name=self._channels[row.channel_id].channel_name,
            reason_name=self._reasons[row.blacklist_reason_code_id].reason_name,
        )

    def _check_refs(self, channel_id: int, reason_id: int, type_code: int) -> None:
        if channel_id not in self._channels:
            raise InvalidReferenceError(f"Unknown channel_id {channel_id}")
        if reason_id not in self._reasons:
            raise InvalidReferenceError(f"Unknown blacklist_reason_code_id {reason_id}")
        if type_code not in self._types:
            raise InvalidReferenceError(f"Unknown identifier_type_code {type_code}")

    def _find_duplicate(
        self, identifier: str, type_code: int, channel_id: int, exclude_id: int | None = None
    ) -> Listing | None:
        k = _key(identifier)
        for row in self._rows.values():
            if (
                row.blacklist_id != exclude_id
                and _key(row.identifier) == k
                and row.identifier_type_code == type_code
                and row.channel_id == channel_id
            ):
                return row
        return None

    def _insert(self, data: ListingCreate, created_at=None, updated_at=None, imported=False) -> ListingOut:
        """Imported rows keep their dates as-is, including missing ones; new rows are stamped now."""
        with self._lock:
            self._check_refs(data.channel_id, data.blacklist_reason_code_id, data.identifier_type_code)
            dup = self._find_duplicate(data.identifier, data.identifier_type_code, data.channel_id)
            if dup:
                raise DuplicateError(self._enrich(dup))
            now = datetime.now(timezone.utc)
            row = Listing(
                blacklist_id=self._next_id,
                created_at=created_at if imported else now,
                updated_at=(updated_at or created_at) if imported else now,
                **data.model_dump(),
            )
            self._rows[row.blacklist_id] = row
            self._next_id += 1
            return self._enrich(row)

    # -- interface ---------------------------------------------------------

    def lookups(self) -> Lookups:
        return Lookups(
            channels=sorted(self._channels.values(), key=lambda c: c.channel_id),
            reason_codes=sorted(self._reasons.values(), key=lambda r: r.reason_name),
            identifier_types=sorted(self._types.values(), key=lambda t: t.identifier_type_code),
        )

    def search(
        self,
        q=None,
        channel_id=None,
        blacklist_reason_code_id=None,
        identifier_type_code=None,
        offset=0,
        limit=50,
    ):
        needle = q.strip().casefold() if q and q.strip() else None
        with self._lock:
            rows = [
                r
                for r in self._rows.values()
                if (channel_id is None or r.channel_id == channel_id)
                and (blacklist_reason_code_id is None or r.blacklist_reason_code_id == blacklist_reason_code_id)
                and (identifier_type_code is None or r.identifier_type_code == identifier_type_code)
                and (
                    needle is None
                    or needle in r.identifier.casefold()
                    or needle in r.blacklist_notes.casefold()
                    or needle in (r.asin or "").casefold()
                    or needle in (r.created_by or "").casefold()
                )
            ]
            rows.sort(key=lambda r: (r.updated_at or _NO_DATE, r.blacklist_id), reverse=True)
            return [self._enrich(r) for r in rows[offset : offset + limit]], len(rows)

    def get(self, blacklist_id):
        with self._lock:
            row = self._rows.get(blacklist_id)
            if row is None:
                raise NotFoundError(f"Blacklist entry {blacklist_id} not found")
            return self._enrich(row)

    def create(self, data):
        return self._insert(data)

    def update(self, blacklist_id, data):
        with self._lock:
            row = self._rows.get(blacklist_id)
            if row is None:
                raise NotFoundError(f"Blacklist entry {blacklist_id} not found")
            changes = data.model_dump(exclude_none=True)
            for nullable in ("asin", "created_by"):
                if changes.get(nullable) == "":
                    changes[nullable] = None
            merged = row.model_copy(update=changes)
            self._check_refs(merged.channel_id, merged.blacklist_reason_code_id, merged.identifier_type_code)
            dup = self._find_duplicate(
                merged.identifier, merged.identifier_type_code, merged.channel_id, exclude_id=blacklist_id
            )
            if dup:
                raise DuplicateError(self._enrich(dup))
            merged.updated_at = datetime.now(timezone.utc)
            self._rows[blacklist_id] = merged
            return self._enrich(merged)

    def delete(self, blacklist_id):
        with self._lock:
            if self._rows.pop(blacklist_id, None) is None:
                raise NotFoundError(f"Blacklist entry {blacklist_id} not found")

    def find_by_identifiers(self, identifiers, channel_id=None, identifier_type_code=None):
        wanted = {_key(i) for i in identifiers}
        with self._lock:
            return [
                self._enrich(r)
                for r in self._rows.values()
                if _key(r.identifier) in wanted
                and (channel_id is None or r.channel_id == channel_id)
                and (identifier_type_code is None or r.identifier_type_code == identifier_type_code)
            ]

    def find_by_asins(self, asins, channel_id=None):
        wanted = {a.upper() for a in asins}
        with self._lock:
            return [
                self._enrich(r)
                for r in self._rows.values()
                if r.asin and r.asin in wanted and (channel_id is None or r.channel_id == channel_id)
            ]

    def list_by_type(self, identifier_type_code, channel_id=None):
        with self._lock:
            return [
                self._enrich(r)
                for r in self._rows.values()
                if r.identifier_type_code == identifier_type_code
                and (channel_id is None or r.channel_id == channel_id)
            ]
