"""
MPN Check -- case-pack quantity from AccessGUDID (FDA's public Global Unique
Device Identification Database, via the openFDA device/udi.json mirror).

Free, public, no API key needed. Replaces trusting fbidb's ambiguous
`packaged_products` "CS" tag for qty/case (per user 2026-09-25: "please dont
take the case values from FBI" -- confirmed live that field can mislabel an
intermediate packaging tier as the full case, e.g. a real Quality King item
whose true case is 12 units showed fbidb "CS"=3).

Verified live against 5 real MPNs from our own vendor data:
  STL2644 / Centurion       -> case=100  (single Package->Primary level)
  CURCHSIL0753 / Medline    -> case=24   (two-level hierarchy: 6 x 4)
  1-0425N / Medline         -> found, but NO case-level packaging in GUDID
  1271 / Teleflex           -> NOT in GUDID under Teleflex (10 hits, all
                                other companies -- catalog numbers collide
                                across manufacturers here just like in our
                                own data, so a company-name match is required,
                                never just the first hit)
  0000TD0320L / Filmop      -> not in GUDID at all (non-medical-device vendor)

So GUDID coverage is real but partial: some MPNs aren't FDA-regulated
devices at all, some are but never had case-level UDI data submitted. Both
are honest "not found" cases, not this module's bug.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

_ENDPOINT = "https://api.fda.gov/device/udi.json"
_CASE_TYPES = {"CASE", "CS", "CTN", "CARTON", "BOX"}


def _norm(s) -> str:
    return "".join(c for c in str(s or "").lower() if c.isalnum())


def _company_matches(company_name: str, manufacturer: str) -> bool:
    a, b = _norm(company_name), _norm(manufacturer)
    return bool(a and b and (a in b or b in a))


def _case_breakdown_from_identifiers(ids: list[dict]) -> dict | None:
    """Walk the GS1/HIBCC packaging hierarchy from whichever identifier is
    tagged a case/carton down to the Primary (base sellable) unit, multiplying
    quantity_per_package at each level -- a case tier's quantity_per_package
    is relative to its OWN child tier, not always the primary unit directly
    (verified live: CURCHSIL0753's "CS" tier holds 6 of an INTERMEDIATE
    package that itself holds 4 primaries -> 24 real units/case, not 6).

    Returns {qty_case, qty_case_uom, ea_case}:
      ea_case    = the FULLY multiplied-down total base-unit ("each") count
                   (24 for CURCHSIL0753 -- same number this function used to
                   return on its own before "Qty/Case" and "EA/Case" became
                   two separate columns, 2026-10-08).
      qty_case   = the count at the HIGHEST sub-case packaging tier only --
                   i.e. the case's OWN quantity_per_package, one level down,
                   NOT multiplied any further (6 for CURCHSIL0753 -- "6 boxes
                   per case", not the 24 total eaches).
      qty_case_uom = that tier's label (Pack/Box/Bag/Dozen/...), straight from
                   GUDID's own `package_type` on that child identifier WHEN
                   GUDID actually supplies one. Confirmed live this is often
                   blank (CURCHSIL0753's own intermediate "4" tier has
                   package_type=None in the real GUDID record) -- GUDID
                   reliably labels only the top (case) tier, not what's
                   inside it. Falls back to "Each" only when the case's
                   immediate child IS the Primary (base) unit itself (i.e.
                   there's no intermediate tier at all, so qty_case==ea_case
                   by construction) -- that's a real fact from GUDID's own
                   `type` marker, not a guess. Otherwise stays None rather
                   than inventing a label the source data doesn't provide.

    None if there's no case-tagged identifier or the packaging data is
    incomplete (a missing quantity_per_package partway down the chain)."""
    by_id = {i["id"]: i for i in ids if i.get("id")}
    case = next((i for i in ids if str(i.get("package_type", "")).upper() in _CASE_TYPES), None)
    if not case:
        return None
    total = 1
    qty_case: int | None = None
    qty_case_uom: str | None = None
    cur, seen, first_step = case, set(), True
    while cur:
        qty = cur.get("quantity_per_package")
        if qty is None:
            break
        try:
            qty_i = int(qty)
        except (TypeError, ValueError):
            return None
        total *= qty_i
        nxt_id = cur.get("unit_of_use_id")
        nxt = by_id.get(nxt_id) if nxt_id and nxt_id not in seen else None
        if first_step:
            qty_case = qty_i
            if nxt and nxt.get("type") == "Primary":
                qty_case_uom = "Each"
            elif nxt:
                qty_case_uom = (str(nxt.get("package_type") or "").strip() or None)
            first_step = False
        if not nxt_id or nxt_id in seen:
            break
        seen.add(nxt_id)
        cur = nxt
        if cur and cur.get("type") == "Primary":
            break
    if qty_case is None:
        return None
    return {"qty_case": qty_case, "qty_case_uom": qty_case_uom, "ea_case": total}


_NOT_FOUND = {"qty_case": None, "qty_case_uom": None, "ea_case": None}


def lookup_case_qty(mpn: str, manufacturer: str) -> dict:
    """Free, live GUDID lookup. Returns {qty_case, qty_case_uom, ea_case, matched,
    note} -- qty_case/qty_case_uom = the count+label at the highest sub-case
    packaging tier (e.g. 6 'Box'); ea_case = the fully multiplied-down total
    base-unit count (e.g. 24). Never raises -- network/API problems come back
    as a clear note with all three quantities None, same contract as the paid
    AI fallback so callers can treat them uniformly."""
    mpn = (mpn or "").strip()
    manufacturer = (manufacturer or "").strip()
    if not mpn or not manufacturer:
        return {**_NOT_FOUND, "matched": False, "note": "missing MPN or manufacturer"}
    try:
        resp = requests.get(_ENDPOINT, params={"search": f'catalog_number:"{mpn}"', "limit": 20},
                            timeout=15)
        if resp.status_code == 404:
            return {**_NOT_FOUND, "matched": False, "note": "not found in AccessGUDID"}
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except Exception as exc:  # noqa: BLE001
        log.warning("[mpn_check_gudid] lookup failed for %s/%s: %s", mpn, manufacturer, str(exc)[:150])
        return {**_NOT_FOUND, "matched": False, "note": f"AccessGUDID lookup failed: {str(exc)[:100]}"}

    # A single hit for this catalog number is unambiguous even when the
    # company name doesn't match what we call the manufacturer -- verified
    # live: STL2644 is GUDID-labeled "Centurion Medical Products" (the actual
    # regulatory labeler) but we (correctly) know it as "Medline" (the brand/
    # distributor we order it as) -- an OEM/private-label relationship, very
    # common in medical distribution. Requiring a company match here would
    # reject a genuinely correct, unambiguous record. Company-matching is only
    # needed to DISAMBIGUATE when the catalog number collides across multiple
    # unrelated companies (verified live: "1271" returns 20 hits across 4+
    # companies, none of them Teleflex -- there it's essential).
    if len(results) == 1:
        rec = results[0]
    else:
        rec = next((r for r in results if _company_matches(r.get("company_name") or "", manufacturer)), None)
        if rec is None:
            return {**_NOT_FOUND, "matched": False,
                    "note": f"AccessGUDID has {len(results)} hit(s) for this MPN, none from {manufacturer!r}"}

    breakdown = _case_breakdown_from_identifiers(rec.get("identifiers") or [])
    if breakdown is None:
        return {**_NOT_FOUND, "matched": True,
                "note": f"found in AccessGUDID ({rec.get('company_name')}) but no case-level packaging data"}
    return {**breakdown, "matched": True, "note": f"AccessGUDID ({rec.get('company_name')})"}
