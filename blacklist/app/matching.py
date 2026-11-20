"""Blacklist matching rules, shared by every repository implementation.

An input identifier is checked three ways:

1. identifier - exact, case-insensitive match on a blacklisted identifier of any type,
   whatever the type is called (so entering a brand code like "PDI" finds the rule itself).
2. brand      - the input starts with a blacklisted brand prefix for that channel. Only
               when an identifier type named BRAND_PREFIX_TYPE_NAME exists; otherwise
               skipped. Replaces the workbook formulas: R5 compared LEFT(sku, 3), which
               never matches 4-letter brands (POWE, SKIL); R4 used SEARCH(), which matches
               the code anywhere in the SKU ("REM" inside "...PREMIER...").
               Amazon-generated SKUs ("amzn.gr.<sku>-<random>-LN") are unwrapped first.
3. asin       - the input is the ASIN recorded on a blacklisted SKU. Only when ASIN data
               exists (the in-memory import; the live table has no ASIN column) and no
               type filter is set. Reported as "review", not "blacklisted": e.g.
               "duplicated ASIN" rows blacklist one SKU while another keeps selling it.
"""
from __future__ import annotations

import re

from .models import BulkCheckResult, CheckMatch
from .repository import BlacklistRepository

# The identifier type whose rows are brand-prefix rules. Matched by name, ignoring case and
# surrounding spaces: ids are IDENTITY values in the database.
BRAND_PREFIX_TYPE_NAME = "Brand prefix"

_AMZN_WRAPPER = re.compile(r'^amzn\.gr\.["\']?', re.I)
_ASIN_SHAPE = re.compile(r"^[A-Z0-9]{10}$", re.I)
_ORDER = {"identifier": 0, "brand": 1, "asin": 2}


def brand_type_id(repo: BlacklistRepository) -> int | None:
    """Id of the brand-prefix identifier type, or None when there isn't one."""
    wanted = BRAND_PREFIX_TYPE_NAME.strip().casefold()
    return next((t.identifier_type_code for t in repo.lookups().identifier_types
                 if t.identifier_type_name.strip().casefold() == wanted), None)


def brand_base(sku: str) -> str:
    """The part of a SKU that starts with the brand code."""
    return _AMZN_WRAPPER.sub("", sku.strip())


def dedupe(identifiers: list[str]) -> list[str]:
    """Trim, drop blanks, and dedupe case-insensitively while keeping input order."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in identifiers:
        value = raw.strip()
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            out.append(value)
    return out


def check(
    repo: BlacklistRepository,
    identifiers: list[str],
    channel_id: int | None = None,
    identifier_type_code: int | None = None,
) -> list[BulkCheckResult]:
    identifiers = dedupe(identifiers)

    exact: dict[str, list] = {}
    for row in repo.find_by_identifiers(identifiers, channel_id, identifier_type_code):
        exact.setdefault(row.identifier.casefold(), []).append(row)

    # Brand rules apply to any type filter except the brand type itself (an input of that
    # type is a brand code, and the exact match above already finds it).
    brand_type = brand_type_id(repo)
    use_brand = brand_type is not None and identifier_type_code != brand_type
    brand_rules = repo.list_by_type(brand_type, channel_id) if use_brand else []
    # Longest prefix first so "SKIL" is tested before a hypothetical "SKI".
    brand_rules.sort(key=lambda r: len(r.identifier), reverse=True)

    # ASIN review needs ASIN data: find_by_asins returns nothing on the SQL backend.
    use_asin = identifier_type_code is None
    asin_inputs = [i for i in identifiers if _ASIN_SHAPE.match(i)] if use_asin else []
    by_asin: dict[str, list] = {}
    for row in repo.find_by_asins(asin_inputs, channel_id) if asin_inputs else []:
        by_asin.setdefault(row.asin.upper(), []).append(row)

    results = []
    for ident in identifiers:
        matches: list[CheckMatch] = []
        seen_ids: set[int] = set()

        def add(rows, how):
            for r in rows:
                if r.blacklist_id not in seen_ids:
                    seen_ids.add(r.blacklist_id)
                    matches.append(CheckMatch(**r.model_dump(), matched_by=how))

        add(exact.get(ident.casefold(), []), "identifier")
        if brand_rules:
            base = brand_base(ident).casefold()
            add((r for r in brand_rules if base.startswith(r.identifier.casefold())), "brand")
        add(by_asin.get(ident.upper(), []), "asin")

        hard = any(m.matched_by != "asin" for m in matches)
        status = "blacklisted" if hard else ("review" if matches else "clear")
        matches.sort(key=lambda m: (_ORDER[m.matched_by], m.channel_id))
        results.append(BulkCheckResult(identifier=ident, status=status, blacklisted=hard, matches=matches))
    return results
