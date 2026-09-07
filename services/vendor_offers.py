"""
Vendor Offer Analytics — ported from the standalone vendor-offer-analytics tool
into catalog-verifier (Phase 1 / MVP: buy-side price comparison).

Vendors send catalog files on their own cadence. Each ingest MERGES into
`vo_vendor_offers` (never overwrites): prices refresh for items a file mentions,
new items are added, and unmentioned items are kept with their old `last_seen` as
the staleness marker. The date-guarded upsert makes re-ingest idempotent and
order-independent. The cheapest-vendor / savings / spread answers are derived at
read time from `vo_vendor_best_offer`.

The vendor parsers (Quality King / Cencora / Diamond / BILO) and the accumulate
semantics (`norm_upc`, `offer_key`, `UPSERT_SQL`, `dedupe_offers`) are ported
VERBATIM from build_database.py — each parser has hard-won traps (BILO needs
xlrd, Diamond is cp1252 with 14 fields vs 13 headers + `12DGTUPC`, Quality King
is a 7-snapshot stack replayed oldest-first). Do NOT "clean up" those.

ASINs are NOT loaded from a 55 MB listings master here — they're reused from
catalog-verifier's existing Pair Library (UPC→ASIN + amz_pack) via
`sync_asins_from_pair_library()`, plus per-item manual assignment.
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
import re
from pathlib import Path

import pandas as pd

from services import database

log = logging.getLogger(__name__)

# Uploaded vendor catalogs are saved here, one subfolder per vendor.
INCOMING_DIR = Path(__file__).resolve().parent.parent / "data" / "vendor_incoming"

VENDOR_NAMES = [
    "Quality King Distributors",
    "Cencora",
    "Diamond Wholesale",
    "BILO",
    "Victory Wholesale Grocers",
]

# Diamond's CSV has 13 header names but 14 fields per data row (trailing comma).
DIAMOND_NAMES = [
    "blank0", "UPC", "CASEUPC", "DESCRIPTION", "CSPK", "INN1", "INN2",
    "SIZE", "QUANTITY", "PRICE", "MFG", "blank1", "12DGTUPC", "trailing",
]

# Namespaced (vo_*) so it coexists with the rest of catalog_verifier.db.
SCHEMA_VO = """
CREATE TABLE IF NOT EXISTS vo_vendors (
    vendor_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS vo_products (
    upc   TEXT PRIMARY KEY,
    title TEXT
);

CREATE TABLE IF NOT EXISTS vo_asin_listings (
    asin              TEXT PRIMARY KEY,
    upc               TEXT NOT NULL,
    alt_upc           TEXT,
    amazon_pack_size  INTEGER,
    status            TEXT,
    internally_active INTEGER,
    source            TEXT DEFAULT 'pair_library'   -- 'pair_library' | 'manual'
);
CREATE INDEX IF NOT EXISTS idx_vo_asin_upc ON vo_asin_listings(upc);
CREATE INDEX IF NOT EXISTS idx_vo_asin_alt ON vo_asin_listings(alt_upc);

CREATE TABLE IF NOT EXISTS vo_vendor_offers (
    offer_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_key      TEXT NOT NULL UNIQUE,
    vendor_id      INTEGER NOT NULL REFERENCES vo_vendors(vendor_id),
    vendor_item_id TEXT,
    upc            TEXT NOT NULL,
    cost           REAL NOT NULL,
    qty_per_case   INTEGER,
    avail_qty      REAL,
    description    TEXT,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vo_offers_upc ON vo_vendor_offers(upc);
CREATE INDEX IF NOT EXISTS idx_vo_offers_vendor ON vo_vendor_offers(vendor_id);
CREATE INDEX IF NOT EXISTS idx_vo_offers_last_seen ON vo_vendor_offers(last_seen);

CREATE VIEW IF NOT EXISTS vo_asin_by_upc AS
SELECT upc, asin, amazon_pack_size, status, internally_active, source
FROM vo_asin_listings
UNION
SELECT alt_upc AS upc, asin, amazon_pack_size, status, internally_active, source
FROM vo_asin_listings
WHERE alt_upc IS NOT NULL AND alt_upc <> upc;

CREATE VIEW IF NOT EXISTS vo_vendor_best_offer AS
SELECT upc, vendor_id, vendor_name, vendor_item_id, cost, qty_per_case,
       avail_qty, description, first_seen, last_seen
FROM (
    SELECT vo.*, v.vendor_name,
           ROW_NUMBER() OVER (
               PARTITION BY vo.upc, vo.vendor_id
               ORDER BY vo.cost ASC,
                        COALESCE(vo.avail_qty, -1) DESC,
                        COALESCE(vo.qty_per_case, 2147483647) ASC,
                        vo.offer_id ASC
           ) AS rn
    FROM vo_vendor_offers vo
    JOIN vo_vendors v ON v.vendor_id = vo.vendor_id
)
WHERE rn = 1;
"""


def init_schema() -> None:
    """Create the vo_* tables/views (idempotent) and seed the vendor list."""
    with database._LOCK, database._connect() as conn:
        conn.executescript(SCHEMA_VO)
        for n in VENDOR_NAMES:
            conn.execute("INSERT OR IGNORE INTO vo_vendors (vendor_name) VALUES (?)", (n,))


# --------------------------------------------------------------------------
# UPC normalization + helpers (ported verbatim)
# --------------------------------------------------------------------------

def norm_upc(value):
    """Strip non-digits, strip leading zeros, re-pad to 12 only if <= 12 digits."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, float):
        text = str(int(round(value)))
    elif isinstance(value, int):
        text = str(value)
    else:
        text = str(value)
    digits = re.sub(r"\D", "", text).lstrip("0")
    if not digits:
        return None
    return digits.zfill(12) if len(digits) <= 12 else digits


def as_int(value):
    n = pd.to_numeric(value, errors="coerce")
    if pd.isna(n):
        return None
    return int(n)


def file_date(path: Path) -> str:
    return dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


def pick_columns(df: pd.DataFrame, colmap: dict, source: str) -> pd.DataFrame:
    df = df.rename(columns={c: str(c).strip() for c in df.columns})
    missing = [c for c in colmap if c not in df.columns]
    if missing:
        raise ValueError(
            f"{source}: expected column(s) {missing} not found. "
            f"Columns present: {list(df.columns)}. "
            f"If the vendor changed their export format, update this loader's column map."
        )
    return df[list(colmap)].rename(columns=colmap)


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return str(value).strip() or None


def _block_dates(path: Path, n: int) -> list:
    """Approximate a date for each stacked snapshot in the Quality King file."""
    end = dt.datetime.fromtimestamp(path.stat().st_mtime)
    sheet = pd.ExcelFile(path).sheet_names[0]
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", sheet)
    if n == 1 or not m:
        return [end.strftime("%Y-%m-%d")] * n
    start = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if start >= end:
        return [end.strftime("%Y-%m-%d")] * n
    span = (end - start) / (n - 1)
    return [(start + span * i).strftime("%Y-%m-%d") for i in range(n)]


# --------------------------------------------------------------------------
# Vendor loaders (ported verbatim — each carries a trap; do not simplify)
# --------------------------------------------------------------------------

def load_quality_king(path: Path, block: int | None = None):
    """fd51126.xlsx — a STACK of catalog snapshots, replayed oldest-first."""
    df = pick_columns(pd.read_excel(path, sheet_name=0), {
        "ITEM": "item", "DESCRIPTION": "desc", "UPC": "upc", "UM": "uom",
        "PACK": "pack", "ONHAND": "onhand", "QKD PRICE": "cost",
    }, "Quality King fd51126.xlsx").reset_index(drop=True)

    uom_col = df["uom"].astype(str).str.strip()
    header_rows = df.index[uom_col == "UM"].tolist()
    bounds = [0] + [h + 1 for h in header_rows] + [len(df)]
    blocks = [(lo, hi) for lo, hi in zip(bounds, bounds[1:]) if hi > lo]
    if block is not None:
        blocks = [blocks[block]]
    dates = _block_dates(path, len(blocks))

    batches, skipped = [], []
    for (lo, hi), stamp in zip(blocks, dates):
        rows = []
        for r in df.iloc[lo:hi].to_dict("records"):
            upc = norm_upc(r["upc"])
            cost = pd.to_numeric(r["cost"], errors="coerce")
            uom = _clean(r["uom"])
            item = _clean(r["item"])
            reason = None
            if uom != "EA":
                reason = f"UoM={uom!r} not EA (repeated header row or non-each pricing)"
            elif upc is None:
                reason = "missing/unparseable UPC"
            elif pd.isna(cost) or cost <= 0:
                reason = "missing or non-positive QKD PRICE"
            if reason:
                skipped.append({"vendor": "Quality King Distributors", "item": item,
                                "description": _clean(r["desc"]), "reason": reason})
                continue
            rows.append({
                "vendor_item_id": item, "upc": upc, "cost": float(cost),
                "qty_per_case": as_int(r["pack"]),
                "avail_qty": pd.to_numeric(r.get("onhand"), errors="coerce"),
                "description": _clean(r["desc"]), "seen": stamp,
            })
        batches.append((stamp, rows))
    return batches, skipped


def load_cencora(path: Path):
    """Cencora product catalog. Only ABC Selling UoM == 'EA' rows loaded."""
    df = pick_columns(pd.read_excel(path, sheet_name="Results", skiprows=1), {
        "ABC #": "item", "Product Description": "desc", "ABC Selling UoM": "uom",
        "Current Acq Cost": "cost", "UPC Barcode": "upc", "FDB Case Pack": "case_pack",
    }, "Cencora product catalog")
    stamp = file_date(path)
    rows, skipped = [], []
    for r in df.to_dict("records"):
        uom = _clean(r["uom"])
        cost = pd.to_numeric(r["cost"], errors="coerce")
        upc = norm_upc(r["upc"])
        reason = None
        if uom != "EA":
            reason = f"UoM={uom!r} -- cost is per case/pack, no reliable per-each divisor"
        elif upc is None:
            reason = "missing/unparseable UPC Barcode"
        elif pd.isna(cost) or cost <= 0:
            reason = "missing or non-positive Current Acq Cost"
        if reason:
            skipped.append({"vendor": "Cencora", "item": _clean(r["item"]),
                            "description": _clean(r["desc"]), "reason": reason})
            continue
        rows.append({
            "vendor_item_id": _clean(r["item"]), "upc": upc, "cost": float(cost),
            "qty_per_case": as_int(r["case_pack"]), "avail_qty": None,
            "description": _clean(r["desc"]), "seen": stamp,
        })
    return [(stamp, rows)], skipped


def load_diamond(path: Path):
    """FORDMED.CSV — cp1252, 14 fields vs 13 headers, barcode from 12DGTUPC."""
    raw = pd.read_csv(path, dtype=str, encoding="cp1252", skiprows=1,
                      header=None, names=DIAMOND_NAMES, index_col=False)
    df = pick_columns(raw, {
        "UPC": "line_code", "DESCRIPTION": "desc", "CSPK": "case_pack",
        "QUANTITY": "qty", "PRICE": "cost", "12DGTUPC": "upc",
    }, "Diamond FORDMED.CSV")
    stamp = file_date(path)
    rows, skipped = [], []
    for r in df.to_dict("records"):
        upc = norm_upc(r["upc"])
        cost = pd.to_numeric(r["cost"], errors="coerce")
        reason = None
        if upc is None:
            reason = "missing/unparseable 12DGTUPC"
        elif pd.isna(cost) or cost <= 0:
            reason = "missing or non-positive PRICE"
        if reason:
            skipped.append({"vendor": "Diamond Wholesale", "item": _clean(r["line_code"]),
                            "description": _clean(r["desc"]), "reason": reason})
            continue
        rows.append({
            "vendor_item_id": _clean(r["line_code"]), "upc": upc, "cost": float(cost),
            "qty_per_case": as_int(r["case_pack"]),
            "avail_qty": pd.to_numeric(r["qty"], errors="coerce"),
            "description": _clean(r["desc"]), "seen": stamp,
        })
    return [(stamp, rows)], skipped


def load_bilo(path: Path):
    """BILO UPCHARGE .xls (real BIFF8 — needs xlrd). DL rows -> per-each."""
    df = pick_columns(pd.read_excel(path, sheet_name="Sheet1"), {
        "Item No": "item", "Item Description": "desc", "Qty per UOM": "per_uom",
        "Unit of Measure": "uom", "Item Price": "cost", "UPC Code": "upc",
        "Quantity": "qty", "Case Pack": "case_pack",
    }, "BILO UPCHARGE .xls")
    stamp = file_date(path)
    rows, skipped = [], []
    for r in df.to_dict("records"):
        upc = norm_upc(r["upc"])
        price = pd.to_numeric(r["cost"], errors="coerce")
        per_uom = pd.to_numeric(r["per_uom"], errors="coerce")
        reason = None
        if upc is None:
            reason = "missing/unparseable UPC Code"
        elif pd.isna(price) or price <= 0:
            reason = "missing or non-positive Item Price"
        elif pd.isna(per_uom) or per_uom <= 0:
            reason = "missing or non-positive Qty per UOM (can't derive per-each cost)"
        if reason:
            skipped.append({"vendor": "BILO", "item": _clean(r["item"]),
                            "description": _clean(r["desc"]), "reason": reason})
            continue
        rows.append({
            "vendor_item_id": _clean(r["item"]), "upc": upc,
            "cost": float(price) / float(per_uom),
            "qty_per_case": as_int(r["case_pack"]),
            "avail_qty": pd.to_numeric(r["qty"], errors="coerce"),
            "description": _clean(r["desc"]), "seen": stamp,
        })
    return [(stamp, rows)], skipped


VENDOR_LOADERS = {
    "Quality King Distributors": load_quality_king,
    "Cencora": load_cencora,
    "Diamond Wholesale": load_diamond,
    "BILO": load_bilo,
    # Victory Wholesale Grocers: no parseable offer file format yet
}


def dedupe_offers(rows: list, vendor: str):
    """Collapse offer lines identical in item id, UPC, cost and case pack."""
    first: dict = {}
    kept, dropped = [], []
    for r in rows:
        key = (r["vendor_item_id"], r["upc"], round(r["cost"], 6), r["qty_per_case"])
        if key in first:
            prev = first[key]
            a, b = prev.get("avail_qty"), r.get("avail_qty")
            if b is not None and not pd.isna(b) and (a is None or pd.isna(a) or b > a):
                prev["avail_qty"] = b
            dropped.append({"vendor": vendor, "item": r["vendor_item_id"],
                            "description": r["description"],
                            "reason": "identical to another offer line"})
            continue
        first[key] = r
        kept.append(r)
    return kept, dropped


UPSERT_SQL = """
INSERT INTO vo_vendor_offers (offer_key, vendor_id, vendor_item_id, upc, cost,
                              qty_per_case, avail_qty, description, first_seen, last_seen)
VALUES (:offer_key, :vendor_id, :vendor_item_id, :upc, :cost,
        :qty_per_case, :avail_qty, :description, :seen, :seen)
ON CONFLICT(offer_key) DO UPDATE SET
    cost        = CASE WHEN excluded.last_seen >= vo_vendor_offers.last_seen
                       THEN excluded.cost        ELSE vo_vendor_offers.cost        END,
    avail_qty   = CASE WHEN excluded.last_seen >= vo_vendor_offers.last_seen
                       THEN excluded.avail_qty   ELSE vo_vendor_offers.avail_qty   END,
    description = CASE WHEN excluded.last_seen >= vo_vendor_offers.last_seen
                       THEN COALESCE(excluded.description, vo_vendor_offers.description)
                       ELSE vo_vendor_offers.description END,
    last_seen   = MAX(vo_vendor_offers.last_seen,  excluded.last_seen),
    first_seen  = MIN(vo_vendor_offers.first_seen, excluded.first_seen)
"""


def offer_key(vid: int, r: dict) -> str:
    return "|".join([
        str(vid), r["vendor_item_id"] or "", r["upc"],
        "" if r["qty_per_case"] is None else str(r["qty_per_case"]),
    ])


def _vendor_id(conn, name: str) -> int:
    row = conn.execute("SELECT vendor_id FROM vo_vendors WHERE vendor_name=?", (name,)).fetchone()
    if row is None:
        raise ValueError(f"Vendor {name!r} is not in vo_vendors.")
    return row[0]


def _ensure_products(conn, upc_titles: dict) -> None:
    conn.executemany(
        "INSERT INTO vo_products (upc, title) VALUES (?, ?) "
        "ON CONFLICT(upc) DO UPDATE SET title = COALESCE(vo_products.title, excluded.title)",
        list(upc_titles.items()),
    )


def _upsert_offers(conn, vname: str, rows: list) -> dict:
    vid = _vendor_id(conn, vname)
    payload = [{**r, "vendor_id": vid, "offer_key": offer_key(vid, r),
                "avail_qty": None if r.get("avail_qty") is None or pd.isna(r["avail_qty"])
                             else float(r["avail_qty"])}
               for r in rows]
    before = conn.execute("SELECT COUNT(*) FROM vo_vendor_offers WHERE vendor_id=?", (vid,)).fetchone()[0]
    conn.executemany(UPSERT_SQL, payload)
    after = conn.execute("SELECT COUNT(*) FROM vo_vendor_offers WHERE vendor_id=?", (vid,)).fetchone()[0]
    return {"added": after - before, "total": after, "submitted": len(rows)}


def ingest_vendor_file(vname: str, path: Path) -> dict:
    """Merge one uploaded vendor catalog into vo_vendor_offers (§5B semantics).

    Returns {submitted, added, total, skipped, arrivals}. Raises ValueError on an
    unknown vendor or a column/format mismatch."""
    if vname not in VENDOR_LOADERS:
        raise ValueError(f"No loader for {vname!r}. Known: {sorted(VENDOR_LOADERS)}")
    loader = VENDOR_LOADERS[vname]
    kwargs = {"block": None} if vname == "Quality King Distributors" else {}
    batches, skipped = loader(path, **kwargs)

    cleaned = []
    for stamp, rows in batches:
        rows, dupes = dedupe_offers(rows, vname)
        skipped += dupes
        cleaned.append((stamp, rows))

    submitted = added = 0
    with database._LOCK, database._connect() as conn:
        # seed products (union of every UPC seen) so offers are never dropped
        titles = {}
        for _, rows in cleaned:
            for r in rows:
                titles.setdefault(r["upc"], r["description"])
        if titles:
            _ensure_products(conn, titles)
        # merge each arrival oldest-first (Quality King's stack)
        total = 0
        for stamp, rows in sorted(cleaned, key=lambda b: b[0]):
            st = _upsert_offers(conn, vname, rows)
            submitted += st["submitted"]
            added += st["added"]
            total = st["total"]
    return {"vendor": vname, "submitted": submitted, "added": added,
            "total": total, "skipped": len(skipped),
            "arrivals": len(cleaned)}


# --------------------------------------------------------------------------
# ASINs — reused from catalog-verifier's Pair Library (the integration win)
# --------------------------------------------------------------------------

def sync_asins_from_pair_library() -> dict:
    """Attach ASINs to vendor offers from the Pair Library — BY UPC ONLY.

    A vendor offer links to a Pair-Library ASIN when the offer's UPC matches a
    Pair-Library UPC/EAN identifier. Nothing else joins (no title/fuzzy matching,
    and no item-id↔MPN matching — a distributor's item number is not the
    manufacturer part number, so that produced only coincidental collisions).
    A UPC may map to several ASINs; all are attached so the desk shows "N ASINs"
    rather than silently guessing one. Manual assignments (source='manual') always
    win and are never overwritten. Idempotent: rebuilds all source='pair_library'
    rows from the current library + offers on each run. Returns counts.

    NOTE: this Pair Library is currently MPN-keyed with no UPC identifiers, so this
    attaches nothing until UPCs are added to the library."""
    init_schema()
    with database._connect() as conn:
        try:
            id_rows = conn.execute(
                "SELECT asin, identifier FROM pair_library_ids "
                "WHERE lower(id_type) IN ('upc','ean')"
            ).fetchall()
            packs = {r[0]: r[1] for r in conn.execute(
                "SELECT asin, amz_pack FROM pair_library WHERE amz_pack IS NOT NULL AND amz_pack<>''"
            )}
        except Exception as exc:  # pair library tables may not exist
            log.warning("[vendor_offers] pair library unavailable: %s", exc)
            return {"asins": 0, "note": "pair library not available"}
        offer_upcs = [r[0] for r in conn.execute("SELECT DISTINCT upc FROM vo_vendor_offers")]

    # barcode → {ASINs}
    upc2asins: dict = {}
    for asin, ident in id_rows:
        a = (asin or "").strip().upper()
        k = norm_upc(ident)
        if a and k:
            upc2asins.setdefault(k, set()).add(a)

    def _pack(asin):
        p = packs.get(asin)
        try:
            return int(float(p)) if p not in (None, "") else None
        except (ValueError, TypeError):
            return None

    seen_asin: dict = {}          # asin -> (upc, pack) — PK is asin, first wins
    link_upcs: set = set()
    offers_by_upc = 0
    for upc in offer_upcs:
        nupc = norm_upc(upc)
        if not nupc or nupc not in upc2asins:
            continue
        offers_by_upc += 1
        link_upcs.add(nupc)
        for a in upc2asins[nupc]:
            seen_asin.setdefault(a, (nupc, _pack(a)))

    rows = [(a, u, None, pk) for a, (u, pk) in seen_asin.items()]

    with database._LOCK, database._connect() as conn:
        # Rebuild only the pair-library rows; manual assignments are left untouched
        # and win over pair-library via INSERT OR IGNORE (PK is asin).
        conn.execute("DELETE FROM vo_asin_listings WHERE source='pair_library'")
        if link_upcs:
            conn.executemany("INSERT OR IGNORE INTO vo_products (upc, title) VALUES (?, NULL)",
                             [(u,) for u in link_upcs])
        if rows:
            conn.executemany("""
                INSERT OR IGNORE INTO vo_asin_listings
                    (asin, upc, alt_upc, amazon_pack_size, status, internally_active, source)
                VALUES (?, ?, ?, ?, NULL, 1, 'pair_library')
            """, rows)
    return {"asins": len(rows), "offers_matched_by_upc": offers_by_upc,
            "library_upcs": len(upc2asins)}


def _mirror_pairs_to_library(pairs) -> int:
    """Also record Price-Desk UPC↔ASIN imports in the Pair Library so the curated
    ASIN set grows as the desk is populated (and the UPC↔ASIN sync then has real
    barcodes to match on). Writes asin + upc + amz_pack, plus a best-effort brand
    taken from the matching vendor offer's description (a vendor offer carries no
    structured brand/manufacturer, only a free-text description whose leading token
    is usually the brand — the weekly health-check enriches brand/manufacturer with
    Amazon's real values). Best-effort: never raises, so a Pair-Library hiccup can't
    break the desk assignment. `pairs` = iterable of {upc, asin, pack}."""
    pairs = [p for p in (pairs or []) if p.get("upc") and p.get("asin")]
    if not pairs:
        return 0
    upcs = list({p["upc"] for p in pairs})
    desc_by_upc: dict = {}
    try:
        with database._connect() as conn:
            qmarks = ",".join("?" * len(upcs))
            for u, d in conn.execute(
                f"SELECT upc, description FROM vo_vendor_offers "
                f"WHERE upc IN ({qmarks}) AND description IS NOT NULL AND description <> '' "
                f"GROUP BY upc", upcs):
                desc_by_upc[u] = d
    except Exception as exc:  # noqa: BLE001
        log.warning("[vendor_offers] offer-desc lookup failed: %s", exc)
    n = 0
    for p in pairs:
        try:
            desc = (desc_by_upc.get(p["upc"]) or "").strip()
            brand = desc.split()[0] if desc else ""     # leading token ≈ brand
            database.upsert_library_pair(
                asin=p["asin"],
                ids={"upc": [p["upc"]]},
                brand=brand,
                manufacturer="",
                amz_pack=str(p["pack"]) if p.get("pack") else "",
            )
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("[vendor_offers] pair-library mirror failed for %s: %s", p.get("asin"), exc)
    return n


def assign_asin(upc: str, asin: str, amazon_pack_size=None) -> dict:
    """Hand-assign one ASIN to a UPC (source='manual', always wins). Also mirrors
    the pair into the Pair Library (asin + upc + amz_pack + best-effort brand)."""
    upc = norm_upc(upc)
    asin = (asin or "").strip().upper()
    if not upc or not re.match(r"^B0[A-Z0-9]{8}$", asin):
        raise ValueError("A valid UPC and ASIN (B0XXXXXXXX) are required.")
    pack = as_int(amazon_pack_size) or 1
    with database._LOCK, database._connect() as conn:
        conn.execute("INSERT OR IGNORE INTO vo_products (upc, title) VALUES (?, NULL)", (upc,))
        conn.execute("""
            INSERT INTO vo_asin_listings (asin, upc, alt_upc, amazon_pack_size,
                                          status, internally_active, source)
            VALUES (?, ?, NULL, ?, NULL, 1, 'manual')
            ON CONFLICT(asin) DO UPDATE SET
                upc=excluded.upc, amazon_pack_size=excluded.amazon_pack_size, source='manual'
        """, (asin, upc, pack))
    # mirror OUTSIDE the vo lock (upsert_library_pair takes database._LOCK itself)
    mirrored = _mirror_pairs_to_library([{"upc": upc, "asin": asin, "pack": pack}])
    return {"upc": upc, "asin": asin, "amazon_pack_size": pack, "library_upserts": mirrored}


def parse_pasted_pairs(text: str):
    """Parse pasted 'UPC,ASIN[,pack]' lines, order-independent. ASIN by shape;
    the UPC is the non-ASIN token with the MOST digits (any length — short internal
    UPCs under 8 digits are allowed, and still won't be confused with a 1–3 digit
    pack because the pack has fewer digits); pack is a remaining 1–3 digit integer.
    Returns (pairs, rejected)."""
    pairs, rejected = [], []
    for i, raw in enumerate(str(text or "").splitlines()):
        line = raw.strip()
        if not line:
            continue
        toks = [t for t in re.split(r"[\t,;|]+|\s{2,}| +", line) if t]
        asin = next((t.upper() for t in toks if re.fullmatch(r"B0[A-Z0-9]{8}", t.upper())), None)
        # UPC = the non-ASIN token containing the most digits (tie → earliest).
        upc, best = None, (-1, 0)
        for j, t in enumerate(toks):
            if asin and t.upper() == asin:
                continue
            ndig = len(re.sub(r"\D", "", t))
            if ndig == 0:
                continue
            score = (ndig, -j)   # most digits wins; earliest breaks ties
            if score > best:
                best, upc = score, t
        if not asin or not upc:
            low = line.lower()
            if i == 0 and ("asin" in low or "upc" in low or "barcode" in low):
                continue  # header row, skipped not rejected
            rejected.append(f"line {i+1}: " + (
                "no ASIN found (needs B0 + 8 chars)" if not asin else "no UPC found (no digits)"))
            continue
        pack = 1
        for t in toks:
            if (asin and t.upper() == asin) or t == upc:
                continue
            if re.fullmatch(r"\d{1,3}", t):
                pack = int(t)
                break
        nupc = norm_upc(upc)
        if not nupc:
            rejected.append(f"line {i+1}: UPC {upc!r} didn't normalize")
            continue
        pairs.append({"upc": nupc, "asin": asin, "pack": pack})
    return pairs, rejected


def assign_bulk(text: str, dry: bool = False) -> dict:
    """Bulk UPC->ASIN assign from pasted text. Dry run returns the preview counts
    without writing. Shape matches the page's renderPairsResult()."""
    init_schema()
    pairs, rejected = parse_pasted_pairs(text)
    seen = {}
    for p in pairs:
        seen[p["asin"]] = p          # dedupe within the paste (last wins)
    uniq = list(seen.values())

    existing = set()
    if uniq:
        with database._connect() as conn:
            qmarks = ",".join("?" * len(uniq))
            existing = {r[0] for r in conn.execute(
                f"SELECT asin FROM vo_asin_listings WHERE asin IN ({qmarks})",
                [p["asin"] for p in uniq])}
    new_pairs = [p for p in uniq if p["asin"] not in existing]

    library_upserts = 0
    if not dry and new_pairs:
        with database._LOCK, database._connect() as conn:
            conn.executemany("INSERT OR IGNORE INTO vo_products (upc, title) VALUES (?, NULL)",
                             [(p["upc"],) for p in new_pairs])
            conn.executemany("""
                INSERT INTO vo_asin_listings (asin, upc, alt_upc, amazon_pack_size,
                                              status, internally_active, source)
                VALUES (?, ?, NULL, ?, NULL, 1, 'manual')
                ON CONFLICT(asin) DO UPDATE SET
                    upc=excluded.upc, amazon_pack_size=excluded.amazon_pack_size, source='manual'
            """, [(p["asin"], p["upc"], p["pack"]) for p in new_pairs])
        # mirror OUTSIDE the vo lock (upsert_library_pair takes database._LOCK itself)
        library_upserts = _mirror_pairs_to_library(new_pairs)

    return {
        "parsed": len(uniq),
        "new": len(new_pairs),
        "duplicates": len(uniq) - len(new_pairs),
        "library_upserts": library_upserts,
        "rejected": rejected[:30],
        "rejectedTotal": len(rejected),
        "sample": [{"upc": p["upc"], "asin": p["asin"], "pack": p["pack"]} for p in new_pairs[:12]],
    }


# --------------------------------------------------------------------------
# Ledger (ported from export_web_data.build) — the Price Desk payload
# --------------------------------------------------------------------------

def build_ledger() -> dict:
    """Compact price-comparison payload. items: [upc,title,asin,pack,status,offers[]]
    where offer = [vendorIdx, cost, qty_per_case, avail_qty, last_seen, first_seen].
    Cheapest / savings / spread are derived client-side (single source of truth)."""
    init_schema()
    with database._connect() as conn:
        conn.row_factory = None
        vendors = [r[0] for r in conn.execute(
            "SELECT DISTINCT vendor_name FROM vo_vendor_best_offer ORDER BY vendor_name")]
        vidx = {v: i for i, v in enumerate(vendors)}
        vlatest = {r[0]: r[1] for r in conn.execute(
            "SELECT vendor_name, MAX(last_seen) FROM vo_vendor_best_offer GROUP BY vendor_name")}
        meta = {r[0]: (r[1], r[2], r[3]) for r in conn.execute("""
            SELECT upc, asin, amazon_pack_size, status FROM (
                SELECT a.*, ROW_NUMBER() OVER (
                           PARTITION BY a.upc
                           ORDER BY COALESCE(a.internally_active,0) DESC,
                                    CASE WHEN a.status IS NULL THEN 0 ELSE 1 END ASC,
                                    a.asin ASC) AS rn
                FROM vo_asin_by_upc a
            ) WHERE rn=1
        """)}
        n_asins = {r[0]: r[1] for r in conn.execute(
            "SELECT upc, COUNT(DISTINCT asin) FROM vo_asin_by_upc GROUP BY upc")}
        titles = {r[0]: r[1] for r in conn.execute(
            "SELECT upc, title FROM vo_products WHERE title IS NOT NULL")}

        items = {}
        for r in conn.execute("""
            SELECT upc, vendor_name, cost, qty_per_case, avail_qty, description,
                   first_seen, last_seen
            FROM vo_vendor_best_offer ORDER BY upc
        """):
            it = items.setdefault(r[0], {"o": [], "d": None})
            if it["d"] is None:
                it["d"] = r[5]
            it["o"].append([
                vidx[r[1]], round(r[2], 4), r[3],
                None if r[4] is None else round(r[4], 1), r[7], r[6],
            ])

    out_items = []
    for upc, it in items.items():
        m = meta.get(upc)
        out_items.append([
            upc, titles.get(upc) or it["d"] or "",
            (m[0] if m else None), (m[1] if m else None), (m[2] if m else None),
            n_asins.get(upc, 0), it["o"],
        ])
    out_items.sort(key=lambda x: -(max(o[1] for o in x[6]) - min(o[1] for o in x[6])))

    return {
        "vendors": vendors,
        "vendorLatest": vlatest,
        "uploadVendors": sorted(VENDOR_LOADERS),
        "asOf": max(vlatest.values()) if vlatest else None,
        "items": out_items,
    }
