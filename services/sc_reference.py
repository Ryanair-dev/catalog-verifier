"""
Local SellerCloud reference tables — a persistent "memory" of the parts of the
catalog Create SKUs looks up on every run, so it reads them instantly instead of
re-parsing the ~179k-row Azure view each time.

Two tables (per COMPANY — 'Ford Medical' or 'Turba', matching the wizard):
  * sc_brand_prefix   brand   -> prefix (3-char), manufacturer
  * sc_manufacturer   manuf   -> canonical name, purchaser, sourcer

Rules baked in:
  * Prefix is the MODE of each main SKU's leading letters, CAPPED AT 3 chars
    (so Medline 'MDLMDS…'/'MDLDYND…' -> MDL, not MDLMDS).
  * Scoped to the SELECTED company only (Ford Medical, LLC or Turba).
  * Manual exceptions (e.g. Dove -> DOV) are source='manual' and are NEVER
    overwritten by a refresh — they win over the computed value.

build_reference() populates both tables from azure_sql.fetch_rows(). Read paths:
prefix_for(brand, company) and lookup_manufacturer(name, company) (case-insensitive
+ fuzzy, so AI's "Nestle" resolves to SellerCloud's "Nestlé S.A." with its
purchaser/sourcer).
"""
from __future__ import annotations

import time
import unicodedata
from collections import Counter, defaultdict

from rapidfuzz import fuzz

from services import azure_sql, database
from services.brand_map import fordmed_email
from services.sellercloud.catalog_index import (
    _lead_prefix, _is_shadow_sku, _choose_brand_prefix,
)

# Manual brand->prefix exceptions seeded on every build (source='manual', never
# clobbered). Umbrella/parent-company codes that outnumber the brand's own code
# (Dove's ULV=Unilever vs DOV) can't be resolved by any count heuristic — they
# are pinned here. Keyed by (brand_upper, company).
_MANUAL_PREFIX: dict[tuple[str, str], str] = {
    ("DOVE", "Ford Medical"): "DOV",
}

_COMPANIES = ("Ford Medical", "Turba")


def _mkey(s: str) -> str:
    """Normalised manufacturer key for fuzzy matching — accent-folded (é→e so
    'Nestlé S.A.' keys as 'nestlesa'), lower-cased, alnum only."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def ensure_tables() -> None:
    with database._LOCK, database._connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sc_brand_prefix (
              brand        TEXT NOT NULL COLLATE NOCASE,
              company      TEXT NOT NULL,
              prefix       TEXT NOT NULL,
              manufacturer TEXT DEFAULT '',
              sku_count    INTEGER DEFAULT 0,
              source       TEXT DEFAULT 'computed',
              updated_at   REAL DEFAULT 0,
              PRIMARY KEY (brand, company)
            );
            CREATE TABLE IF NOT EXISTS sc_manufacturer (
              manuf_key      TEXT NOT NULL,
              company        TEXT NOT NULL,
              canonical_name TEXT NOT NULL,
              purchaser      TEXT DEFAULT '',
              sourcer        TEXT DEFAULT '',
              sku_count      INTEGER DEFAULT 0,
              updated_at     REAL DEFAULT 0,
              PRIMARY KEY (manuf_key, company)
            );
            CREATE INDEX IF NOT EXISTS idx_scbp_company ON sc_brand_prefix(company);
            CREATE INDEX IF NOT EXISTS idx_scmf_company ON sc_manufacturer(company);
            """
        )


def _build_company(conn, company: str, rows: list[dict]) -> tuple[int, int]:
    now = time.time()
    brand_pfx: dict[str, Counter] = defaultdict(Counter)   # brand -> Counter(3-char prefix)
    brand_mfr: dict[str, Counter] = defaultdict(Counter)   # brand -> Counter(manufacturer)
    mfr: dict[str, dict] = defaultdict(lambda: {"names": Counter(), "pur": Counter(),
                                                "src": Counter(), "n": 0})
    for r in rows:
        bn = (r.get("BrandName") or "").strip()
        mn = (r.get("ManufacturerName") or "").strip()
        pid = (r.get("ProductID") or "").strip()
        # prefix learned from MAINS only (no shadow/kit SKUs)
        if bn and pid and not (r.get("ShadowOf") or "").strip() and not _is_shadow_sku(pid):
            p = _lead_prefix(pid)
            if p:
                brand_pfx[bn][p[:3].upper()] += 1   # CAP AT 3 CHARS
        if bn and mn:
            brand_mfr[bn][mn] += 1
        if mn:
            m = mfr[_mkey(mn)]
            m["n"] += 1
            m["names"][mn] += 1
            if r.get("_purchaser") and r["_purchaser"] != "0":
                m["pur"][r["_purchaser"]] += 1
            if r.get("_sourcer") and r["_sourcer"] != "0":
                m["src"][r["_sourcer"]] += 1

    def top(c: Counter) -> str:
        return c.most_common(1)[0][0] if c else ""

    # brand -> prefix (computed rows never clobber a manual row).
    # Use the same alignment-aware chooser as the live index (on the 3-char-capped
    # prefixes) so a sub-brand keeps its OWN code where the catalog has one
    # (Curad→CUR, Colgate→COL) instead of the parent umbrella (MDL, J&J); brands
    # with only the umbrella code in the catalog still get it (Metamucil→P&G).
    n_brand = 0
    for bn, pc in brand_pfx.items():
        prefix = _choose_brand_prefix(bn, pc)[0]
        if not prefix:
            continue
        conn.execute(
            """INSERT INTO sc_brand_prefix(brand,company,prefix,manufacturer,sku_count,source,updated_at)
               VALUES(?,?,?,?,?, 'computed', ?)
               ON CONFLICT(brand,company) DO UPDATE SET
                 prefix=excluded.prefix, manufacturer=excluded.manufacturer,
                 sku_count=excluded.sku_count, updated_at=excluded.updated_at
               WHERE sc_brand_prefix.source!='manual'""",
            (bn, company, prefix, top(brand_mfr[bn]), sum(pc.values()), now),
        )
        n_brand += 1

    # manufacturer -> canonical + purchaser + sourcer
    n_mfr = 0
    for key, m in mfr.items():
        if not key:
            continue
        conn.execute(
            """INSERT INTO sc_manufacturer(manuf_key,company,canonical_name,purchaser,sourcer,sku_count,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(manuf_key,company) DO UPDATE SET
                 canonical_name=excluded.canonical_name, purchaser=excluded.purchaser,
                 sourcer=excluded.sourcer, sku_count=excluded.sku_count, updated_at=excluded.updated_at""",
            (key, company, top(m["names"]), top(m["pur"]), top(m["src"]), m["n"], now),
        )
        n_mfr += 1
    return n_brand, n_mfr


def _seed_exceptions(conn) -> None:
    now = time.time()
    for (brand, company), prefix in _MANUAL_PREFIX.items():
        conn.execute(
            """INSERT INTO sc_brand_prefix(brand,company,prefix,manufacturer,sku_count,source,updated_at)
               VALUES(?,?,?,'',0,'manual',?)
               ON CONFLICT(brand,company) DO UPDATE SET
                 prefix=excluded.prefix, source='manual', updated_at=excluded.updated_at""",
            (brand, company, prefix, now),
        )


def build_reference(force: bool = False) -> dict:
    """Populate both tables from the live Azure view (all companies). Returns
    {'brands': N, 'manufacturers': N} totals across companies."""
    ensure_tables()
    rows = azure_sql.fetch_rows(force=force)
    tb = tm = 0
    with database._LOCK, database._connect() as conn:
        # Manufacturer table has no manual rows — rebuild it clean each time so a
        # changed key normalisation doesn't leave orphaned rows. (Brand prefixes
        # are upserted, preserving source='manual' exceptions like Dove.)
        conn.execute("DELETE FROM sc_manufacturer")
        for company in _COMPANIES:
            scoped = azure_sql._rows_for_company(rows, company)
            b, m = _build_company(conn, company, scoped)
            tb += b
            tm += m
        _seed_exceptions(conn)
    return {"brands": tb, "manufacturers": tm}


# ── read paths ───────────────────────────────────────────────────────────────
def prefix_for(brand: str, company: str = "Ford Medical") -> str | None:
    if not brand:
        return None
    with database._connect() as conn:
        row = conn.execute(
            "SELECT prefix FROM sc_brand_prefix WHERE brand=? AND company=?",
            (brand.strip(), company),
        ).fetchone()
    return row[0] if row and row[0] else None


def brand_manufacturer(brand: str, company: str = "Ford Medical") -> str:
    """The saved manufacturer for a brand (from a prior computed/confirmed row),
    or '' — lets a remembered NEW brand skip the AI manufacturer guess."""
    if not brand:
        return ""
    with database._connect() as conn:
        row = conn.execute(
            "SELECT manufacturer FROM sc_brand_prefix WHERE brand=? AND company=?",
            (brand.strip(), company),
        ).fetchone()
    return row[0] if row and row[0] else ""


def lookup_manufacturer(name: str, company: str = "Ford Medical") -> dict | None:
    """Resolve a (possibly mis-spelled/differently-cased) manufacturer to its
    SellerCloud canonical name + purchaser/sourcer.

    A name can match several stored spellings — e.g. "Nestle" matches both a tiny
    stray "Nestle" (3 SKUs) and the real "Nestlé S.A." (968 SKUs). We bucket
    matches into STRONG (exact key, or close-length substring) vs WEAK (fuzzy)
    and then prefer the highest SKU count within the best bucket — so the
    dominant spelling wins over an exact match to a junk entry."""
    key = _mkey(name)
    if not key:
        return None
    with database._connect() as conn:
        cands = conn.execute(
            "SELECT manuf_key, canonical_name, purchaser, sourcer, sku_count "
            "FROM sc_manufacturer WHERE company=?",
            (company,),
        ).fetchall()

    def _contained(a: str, b: str) -> bool:
        # substring match, but only when the two are similar in length so
        # "dial" ⊄ "dialysis" (0.5) while "nestle" ⊂ "nestlesa" (0.75)
        if not (a in b or b in a):
            return False
        lo, hi = sorted((len(a), len(b)))
        return hi > 0 and lo / hi >= 0.67

    best = None  # (bucket, sku_count, (canon, pur, src))
    for mk, canon, pur, src, cnt in cands:
        if mk == key or (len(key) >= 4 and _contained(key, mk)):
            bucket = 2                       # strong: exact or close substring
        elif fuzz.ratio(key, mk) >= 88:
            bucket = 1                       # weak: fuzzy only
        else:
            continue
        cand = (bucket, cnt or 0, (canon, pur, src))
        if best is None or cand[:2] > best[:2]:
            best = cand
    if not best:
        return None
    canon, pur, src = best[2]
    return {"manufacturer": canon,
            "purchaser": fordmed_email(pur) if pur else "",
            "sourcer": fordmed_email(src) if src else ""}


def set_manual_prefix(brand: str, prefix: str, company: str = "Ford Medical",
                      manufacturer: str = "") -> None:
    """Pin a brand's prefix (source='manual') — used for exceptions and for
    remembering a confirmed new brand. Never overwritten by build_reference."""
    ensure_tables()
    with database._LOCK, database._connect() as conn:
        conn.execute(
            """INSERT INTO sc_brand_prefix(brand,company,prefix,manufacturer,sku_count,source,updated_at)
               VALUES(?,?,?,?,0,'manual',?)
               ON CONFLICT(brand,company) DO UPDATE SET
                 prefix=excluded.prefix, manufacturer=excluded.manufacturer,
                 source='manual', updated_at=excluded.updated_at""",
            (brand.strip(), company, prefix.strip().upper(), manufacturer.strip(), time.time()),
        )


def stats() -> dict:
    ensure_tables()
    with database._connect() as conn:
        b = conn.execute("SELECT COUNT(*) FROM sc_brand_prefix").fetchone()[0]
        bm = conn.execute("SELECT COUNT(*) FROM sc_brand_prefix WHERE source='manual'").fetchone()[0]
        m = conn.execute("SELECT COUNT(*) FROM sc_manufacturer").fetchone()[0]
    return {"brands": b, "manual_prefixes": bm, "manufacturers": m}
