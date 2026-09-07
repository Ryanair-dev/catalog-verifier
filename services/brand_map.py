"""
Brand → (manufacturer, purchaser, sourcer) mappings for Create SKUs.

Sourced from the SellerCloud catalog export ("Sku Data extended" sheet: BrandName,
Manufacturer, Purchaser, SOURCE_LEAD). Stored in catalog_verifier.db table
``brand_mappings`` so the wizard can auto-fill these per brand without asking the
user. Refresh with :func:`import_from_sku_extended` (e.g. after a new catalog dump);
when the Azure DB is wired up this becomes a live query instead.

Columns: brand (PK, case-insensitive), manufacturer, purchaser, sourcer.
"""
from __future__ import annotations

import collections
import time

from services.database import _LOCK, _connect

_UPSERT = """
INSERT INTO brand_mappings (brand, manufacturer, purchaser, sourcer, updated_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(brand) DO UPDATE SET
  manufacturer = excluded.manufacturer,
  purchaser    = excluded.purchaser,
  sourcer      = excluded.sourcer,
  updated_at   = excluded.updated_at
"""


def init() -> None:
    with _LOCK, _connect() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS brand_mappings (
            brand        TEXT PRIMARY KEY COLLATE NOCASE,
            manufacturer TEXT DEFAULT '',
            purchaser    TEXT DEFAULT '',
            sourcer      TEXT DEFAULT '',
            updated_at   TEXT DEFAULT ''
        )""")


def fordmed_email(name: str) -> str:
    """A purchaser/sourcer username -> '<name>@fordmed.com'. Leaves blanks, the
    'Other' placeholder, and values that are already emails unchanged."""
    n = (name or "").strip()
    if not n or "@" in n or n.lower() == "other":
        return n
    return f"{n}@fordmed.com"


def _row_to_dict(r) -> dict:
    return {
        "brand": r["brand"],
        "manufacturer": r["manufacturer"] or "",
        "purchaser": fordmed_email(r["purchaser"] or ""),
        "sourcer": fordmed_email(r["sourcer"] or ""),
    }


def get_map() -> dict[str, dict]:
    """All mappings keyed by lower-cased brand — cheap enough to load per request."""
    init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT brand, manufacturer, purchaser, sourcer FROM brand_mappings"
        ).fetchall()
    return {r["brand"].strip().lower(): _row_to_dict(r) for r in rows}


def get(brand: str) -> dict:
    if not brand:
        return {}
    init()
    with _connect() as conn:
        r = conn.execute(
            "SELECT brand, manufacturer, purchaser, sourcer FROM brand_mappings "
            "WHERE brand = ? COLLATE NOCASE", (brand.strip(),)
        ).fetchone()
    return _row_to_dict(r) if r else {}


def count() -> int:
    init()
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM brand_mappings").fetchone()[0]


def upsert(brand: str, manufacturer: str = "", purchaser: str = "", sourcer: str = "") -> None:
    if not (brand or "").strip():
        return
    init()
    with _LOCK, _connect() as conn:
        conn.execute(_UPSERT, (brand.strip(), manufacturer or "", purchaser or "",
                               sourcer or "", str(time.time())))


def import_from_sku_extended(
    path: str, *, sheet: str = "Sku Data extended",
    brand_col: str = "BrandName", mfr_col: str = "Manufacturer",
    purch_col: str = "Purchaser", src_col: str = "SOURCE_LEAD",
) -> int:
    """Aggregate the catalog dump per brand (most-common non-blank value for each
    field) and upsert. Returns the number of brands written."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet]
    it = ws.iter_rows(values_only=True)
    hdr = list(next(it))
    idx = {h: i for i, h in enumerate(hdr)}
    bi, mi, pi, si = idx[brand_col], idx[mfr_col], idx[purch_col], idx[src_col]

    def s(v) -> str:
        return str(v).strip() if v is not None else ""

    aggm: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    aggp: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    aggs: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in it:
        b = s(r[bi])
        if not b or b == "0":
            continue
        if s(r[mi]):
            aggm[b][s(r[mi])] += 1
        # Same "Other"-placeholder exclusion as azure_sql.brand_map() -- a value of
        # "Other" means unassigned, not a real purchaser/sourcer, so it must not
        # outvote an actual name just because more legacy rows were left unassigned.
        if s(r[pi]) and s(r[pi]) != "0" and s(r[pi]).lower() != "other":
            aggp[b][s(r[pi])] += 1
        if s(r[si]) and s(r[si]) != "0" and s(r[si]).lower() != "other":
            aggs[b][s(r[si])] += 1

    brands = set(aggm) | set(aggp) | set(aggs)
    init()
    ts = str(time.time())
    with _LOCK, _connect() as conn:
        for b in brands:
            m = aggm[b].most_common(1)[0][0] if aggm[b] else ""
            p = aggp[b].most_common(1)[0][0] if aggp[b] else ""
            sc = aggs[b].most_common(1)[0][0] if aggs[b] else ""
            conn.execute(_UPSERT, (b, m, p, sc, ts))
    return len(brands)
