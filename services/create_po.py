"""
Create PO — SKU generator + import-file exporter (backend for the wizard UI).

Flow the UI drives:
  preview  : parse an uploaded sheet -> headers + sample rows (Step 1)
  brands   : resolve the batch's brands -> status / manufacturer / prefix (Step 2)
  generate : build MAIN / SHADOW(-FBA/-FBM) / KIT SKUs per row, check the live
             SellerCloud catalog so nothing is duplicated, return the review board
  export   : write the bulk / kit / amz import files (applying inline edits)

Rules (confirmed with the user):
- Main identifier: CPG/"UPC" -> last 6 digits of the UPC; Medical/"MPN" -> the MPN.
- Main existence: search by UPC, by UPC with a leading 0, and by MPN.
- Shadow/Kit existence: by ASIN. Company sets suffixes — Ford Medical -FBA/-FBM,
  Turba -FBATRB/-FBMTRB (collision ladder: -FBA2, -FBATRB2, ...).
- Brand is the grouping key; every row inherits its brand's manufacturer + prefix.
  In-catalog brands prefill from SellerCloud; new brands get an AI-suggested
  manufacturer + an auto-generated prefix (both editable).

NOTE: the export column layout is a first cut, mapped to the user's real SellerCloud
bulk / kit / amz templates once samples are provided.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field

from openpyxl import Workbook

from services.sellercloud.catalog_index import CatalogIndex

# Reuse the Analytics engine's brand normalisation so "NIVEA MEN" ≈ "NIVEA",
# "Parker Labs" ≈ "Parker Laboratories" — same rules everywhere.
from services.analytics.matcher import _normalize_brand as _norm_brand, _brands_match
try:
    from rapidfuzz import fuzz as _fuzz
except ImportError:  # pragma: no cover
    _fuzz = None

# Saved SellerCloud reference (brand→prefix, manufacturer→purchaser/sourcer). Wrapped
# so Create SKUs never breaks if the tables aren't built or Azure is unreachable.
def _sc_prefix(brand: str, company: str) -> str:
    try:
        from services import sc_reference
        return sc_reference.prefix_for(brand, company) or ""
    except Exception:
        return ""


def _sc_manufacturer(name: str, company: str):
    try:
        from services import sc_reference
        return sc_reference.lookup_manufacturer(name, company)
    except Exception:
        return None


def _sc_brand_manufacturer(brand: str, company: str) -> str:
    try:
        from services import sc_reference
        return sc_reference.brand_manufacturer(brand, company)
    except Exception:
        return ""


def save_brand_reference(brand: str, prefix: str, company: str, manufacturer: str = "") -> None:
    """Remember a (typically new) brand's prefix + manufacturer so future runs don't
    re-guess. Best-effort; never raises into the caller."""
    try:
        from services import sc_reference
        sc_reference.set_manual_prefix(brand, prefix, company, manufacturer)
    except Exception:
        pass


CPG_UPC_TAIL = 6
COMPANY_IDS = {"Ford Medical": 164, "Turba": 258}
COMPANY_SUFFIX = {
    "Ford Medical": ("-FBA", "-FBM"),
    "Turba": ("-FBATRB", "-FBMTRB"),
}


def _s(v) -> str:
    return str(v if v is not None else "").strip()


def _digits(v) -> str:
    return "".join(c for c in str(v or "") if c.isdigit())


def suffixes(company: str) -> tuple[str, str]:
    return COMPANY_SUFFIX.get(company, ("-FBA", "-FBM"))


def company_id(company: str) -> int:
    return COMPANY_IDS.get(company, 164)


# ── input normalisation via the user's column map ──────────────────────────
FIELD_KEYS = ("asin", "upc", "mpn", "brand", "name", "amzpack", "purchaser", "sourcer",
              "price", "qtycase", "costcase", "cost")


def normalize_rows(raw_rows: list[dict], col_map: dict) -> list[dict]:
    """col_map: { sheet_column_name: field_key }. Returns canonical item dicts."""
    field_to_col: dict[str, str] = {}
    for col, fld in (col_map or {}).items():
        if fld and fld not in ("ignore", "— none —") and fld not in field_to_col:
            field_to_col[fld] = col
    out = []
    for i, raw in enumerate(raw_rows):
        item = {k: "" for k in FIELD_KEYS}
        item["_raw"] = raw
        item["_id"] = f"r{i}"
        for fld, col in field_to_col.items():
            item[fld] = _s(raw.get(col))
        try:
            item["pack_qty"] = int(float(item["amzpack"])) if item["amzpack"] else 1
        except ValueError:
            item["pack_qty"] = 1
        out.append(item)
    return out


def brand_column(col_map: dict) -> str:
    for col, fld in (col_map or {}).items():
        if fld == "brand":
            return col
    return "Brand"


# ── brand resolution ────────────────────────────────────────────────────────
def _auto_prefix(brand: str, taken: set[str] | None = None) -> str:
    """A new brand's SKU prefix = its first 3 alphanumerics, upper-cased
    ('NIVEA MEN' -> 'NIV', 'TRESemmé' -> 'TRE'). Kept to 3 chars: we do NOT append a
    uniqueness digit. Prefixes are shared across brands by design in the catalog
    (363/1315 are — e.g. 'MMM' spans 32 brands, 'BAA' = Bad Air + Banana Boat); a
    main SKU is unique on prefix+identifier, not prefix alone, so 'NIV2' is both
    unwanted and unnecessary. ``taken`` is accepted for call-site compatibility but
    intentionally ignored."""
    return re.sub(r"[^A-Za-z0-9&]", "", brand or "").upper()[:3] or "SKU"


@dataclass
class BrandInfo:
    brand: str
    status: str            # 'in_catalog' | 'new'
    manufacturer: str
    prefix: str
    ai_mfr: bool
    row_count: int
    purchaser: str = ""
    sourcer: str = ""
    brand_name: str = ""    # canonical, case-sensitive brand from the catalog mapping


def _match_catalog_brand(brand: str, catalog_upper: set[str], norm_map: dict[str, str]) -> str | None:
    """The catalog brand name that `brand` refers to, or None. Exact (case-insensitive)
    first; then normalized-equal ("Shea Moisture"="SheaMoisture", suffix-stripped);
    then sub-brand prefix containment (≥5 chars: "NIVEA MEN"→"NIVEA"); then fuzzy ≥88
    ("Moleskine"="Moleskin"). `norm_map` = {normalized → canonical catalog name}."""
    up = brand.strip().upper()
    if up in catalog_upper:
        return up
    nb = _norm_brand(brand)
    if not nb:
        return None
    if nb in norm_map:
        return norm_map[nb]
    best, best_score = None, 0.0
    for cn, name in norm_map.items():
        if len(nb) >= 5 and len(cn) >= 5 and (nb.startswith(cn) or cn.startswith(nb)):
            score = 200 + min(len(nb), len(cn))          # prefer longer overlap
        elif _fuzz is not None:
            r = _fuzz.ratio(nb, cn)
            score = r if r >= 88 else 0
        else:
            score = 0
        if score > best_score:
            best_score, best = score, name
    return best


def resolve_brands(
    items: list[dict],
    *,
    index: CatalogIndex,
    catalog_brand_names: set[str],
    overrides: dict | None = None,
    ai_suggest=None,
    mapping: dict | None = None,
    company: str = "Ford Medical",
) -> dict[str, BrandInfo]:
    """Build one BrandInfo per distinct brand in the batch.

    Brands are matched to the catalog with normalisation + fuzzy/prefix rules (so
    "NIVEA MEN" resolves to the catalog "NIVEA"), then inherit that catalog brand's
    manufacturer / prefix / purchaser / sourcer. `mapping` is the brand_map table
    ({brand_lower: {manufacturer, purchaser, sourcer, brand}}) from the SellerCloud
    catalog — the preferred source for those fields.
    """
    overrides = overrides or {}
    mapping = mapping or {}
    counts: dict[str, int] = {}
    for it in items:
        b = it.get("brand", "")
        if b:
            counts[b] = counts.get(b, 0) + 1

    # Normalised index of catalog brand names (built once) for smart matching.
    norm_map: dict[str, str] = {}
    for cn in catalog_brand_names:
        k = _norm_brand(cn)
        if k and k not in norm_map:
            norm_map[k] = cn

    out: dict[str, BrandInfo] = {}
    for brand, cnt in counts.items():
        ov = overrides.get(brand, {})
        matched = _match_catalog_brand(brand, catalog_brand_names, norm_map)   # canonical catalog name or None
        in_catalog = matched is not None
        lookup = matched or brand      # look everything up under the matched catalog brand
        status = "in_catalog" if in_catalog else "new"
        # mapping keyed by lowercased brand — try the matched name, then the raw input
        mp = mapping.get(lookup.strip().lower()) or mapping.get(brand.strip().lower()) or {}
        ai_mfr = False
        # manufacturer precedence: catalog mapping > SellerCloud index > AI guess
        manufacturer = _s(mp.get("manufacturer"))
        if not manufacturer and in_catalog and index:
            manufacturer = index.manufacturer_for_brand(lookup)
        if not manufacturer and not in_catalog:
            # a previously-saved (confirmed/exported) new brand — reuse its manufacturer
            manufacturer = _sc_brand_manufacturer(brand, company)
        if not manufacturer and not in_catalog and ai_suggest:
            try:
                manufacturer = ai_suggest(brand) or ""
                ai_mfr = bool(manufacturer)
            except Exception:
                manufacturer = ""
        # Prefix: prefer the saved SellerCloud reference table (3-char, company-scoped,
        # manual exceptions like Dove→DOV) over the live index computation.
        prefix = _sc_prefix(lookup, company) or (_sc_prefix(brand, company) if not in_catalog else "")
        if not prefix:
            if in_catalog:
                prefix = (index.prefix_for_brand(lookup) if index else None) or _auto_prefix(brand)
            else:
                prefix = _auto_prefix(brand)
        purchaser = _s(mp.get("purchaser"))
        sourcer = _s(mp.get("sourcer"))
        # apply overrides last
        manufacturer = _s(ov.get("manufacturer")) or manufacturer
        prefix = (_s(ov.get("prefix")) or prefix).upper()
        purchaser = _s(ov.get("purchaser")) or purchaser
        sourcer = _s(ov.get("sourcer")) or sourcer

        # Reconcile the manufacturer against SellerCloud's saved manufacturers
        # (case/spacing/accent-insensitive): "Nestle" → "Nestlé S.A.". When the brand
        # has no purchaser/sourcer yet — the typical NEW-brand case — inherit the
        # manufacturer's. Never clobbers a value the user explicitly set.
        if manufacturer:
            mref = _sc_manufacturer(manufacturer, company)
            if mref:
                if not _s(ov.get("manufacturer")):
                    manufacturer = mref.get("manufacturer") or manufacturer
                    ai_mfr = False
                if not purchaser:
                    purchaser = mref.get("purchaser", "")
                if not sourcer:
                    sourcer = mref.get("sourcer", "")
        # brand NAME written to the export (BrandName column): user override, else the
        # matched catalog brand's canonical casing, else the raw grouping key.
        brand_name = _s(ov.get("brand_name")) or _s(mp.get("brand")) or matched or brand
        out[brand] = BrandInfo(brand, status, manufacturer, prefix, ai_mfr, cnt,
                               purchaser, sourcer, brand_name)
    return out


# ── SKU derivation ────────────────────────────────────────────────────────
@dataclass
class RowResult:
    item: dict
    brand: str
    brand_status: str
    id_value: str
    id_kind: str
    main: str | None
    main_on_sc: bool
    fba: str | None
    fba_on_sc: bool
    fbm: str | None
    fbm_on_sc: bool
    kit_value: str | None
    kit_shadows: str | None
    note: str
    is_kit: bool = False        # this ASIN is a multipack → fba/fbm ARE the kit shadows
    pack: int = 1
    manufacturer: str = ""
    prefix: str = ""
    purchaser: str = ""
    sourcer: str = ""
    brand_name: str = ""


def _main_exists(index: CatalogIndex, upc: str, mpn: str, create_by: str,
                 brand: str = "") -> list[dict]:
    """Existing MAIN SKUs for this product. Search by UPC, UPC with a leading 0,
    and MPN.

    UPC is a GLOBAL identifier (same barcode = same product), so a UPC hit counts
    regardless of brand. An MPN/part number is only unique WITHIN a manufacturer —
    "4284" exists for many brands — so an MPN hit only counts when it's the SAME
    brand. Without this, a Cardinal Health item (prefix CAH) matched an unrelated
    "EMO4284"/"SMM4284" and was wrongly flagged "already on SellerCloud"."""
    hits = []
    d = _digits(upc)
    if d:
        hits += index.main_by_upc(d)
        hits += index.main_by_upc("0" + d)
    if mpn:
        mpn_hits = index.main_by_mpn(mpn)
        if brand:
            mpn_hits = [r for r in mpn_hits if _brands_match(brand, _s(r.get("BrandName")))]
        hits += mpn_hits
    return hits


def derive_rows(
    items: list[dict],
    *,
    config: dict,
    index: CatalogIndex,
    brands: dict[str, BrandInfo],
    edits: dict | None = None,
) -> list[RowResult]:
    edits = edits or {}
    create_by = config.get("create_by", "UPC")
    company = config.get("company", "Ford Medical")
    create = config.get("create", {"main": True, "shadow": True, "kit": True})
    single = config.get("brand_mode") == "single"
    suf_fba, suf_fbm = suffixes(company)

    # Batch-aware SKU uniqueness: seed with the live catalog's SKUs, then reserve
    # every SKU we hand out so two rows of the SAME product (same main) can't collide
    # on a child name — a second ASIN climbs to -FBA2, -FBA3, ... instead of clashing.
    used_skus: set[str] = {s.upper() for s in index.all_skus} if index else set()

    def _claim(base: str, sfx: str) -> str:
        stem = f"{base}{sfx}"
        cand, n = stem, 2
        while cand.upper() in used_skus:
            cand = f"{stem}{n}"; n += 1
        used_skus.add(cand.upper())
        return cand

    results = []
    for it in items:
        brand = it.get("brand", "")
        if single:
            # Resolve the single brand from the roster so the prefix/manufacturer
            # fall back to SellerCloud (or an auto prefix) when the user leaves the
            # single-brand fields blank — otherwise the Main SKU comes out empty.
            resolved = brands.get(brand) or brands.get(config.get("single_brand", ""))
            bname = brand or config.get("single_brand", "")
            r_pfx = resolved.prefix if resolved else _auto_prefix(bname, set())
            r_mfr = resolved.manufacturer if resolved else ""
            r_pur = resolved.purchaser if resolved else ""
            r_src = resolved.sourcer if resolved else ""
            bi = BrandInfo(
                bname, resolved.status if resolved else "new",
                _s(config.get("single_mfr")) or r_mfr,
                (_s(config.get("single_prefix")) or r_pfx).upper(),
                resolved.ai_mfr if resolved else False,
                resolved.row_count if resolved else 0,
                _s(config.get("single_purchaser")) or r_pur,
                _s(config.get("single_sourcer")) or r_src,
                (resolved.brand_name if resolved else "") or bname,
            )
        else:
            bi = brands.get(brand) or BrandInfo(brand, "new", "", "", True, 0)

        upc, mpn = it.get("upc", ""), it.get("mpn", "")
        if create_by == "UPC":
            id_part = _digits(upc)[-CPG_UPC_TAIL:] if _digits(upc) else mpn
            id_value, id_kind = (upc or "—"), "UPC"
        else:
            id_part = mpn or (_digits(upc)[-CPG_UPC_TAIL:] if _digits(upc) else "")
            id_value, id_kind = (mpn or "—"), "MPN"

        def ov(field, default):
            return edits.get(f"{it['_id']}.{field}", default)

        base_main = f"{bi.prefix}{id_part}" if (bi.prefix and id_part) else ""
        existing = _main_exists(index, upc, mpn, create_by, brand=bi.brand or brand) if index else []
        if existing and create.get("main", True):
            main = _s(existing[0].get("ProductID") or existing[0].get("ID"))
        else:
            main = base_main
        main = ov("main", main) or None
        # "Already on SellerCloud" reflects the FINAL main SKU string (after any hand
        # edit) — not just the UPC/MPN lookup. Reserve it so children never reuse it.
        main_on_sc = bool(main) and index is not None and index.sku_exists(main)
        if main:
            used_skus.add(main.upper())

        # One child SKU per channel for THIS ASIN: a multipack (pack>1) → the _QY{n}
        # KIT shadow (qty n); otherwise a single-unit shadow (qty 1). Never both, and
        # every name is unique across the whole batch.
        pack = int(it.get("pack_qty", 1) or 1)
        is_kit = bool(main) and create.get("kit", True) and pack > 1
        fba = fbm = kit_value = kit_shadows = None
        if main and create.get("shadow", True):
            if is_kit:
                kit_value = f"{main}_QY{pack}"
                fba = ov("fba", _claim(kit_value, suf_fba))
                fbm = ov("fbm", _claim(kit_value, suf_fbm))
                kit_shadows = f"{fba}  ·  {fbm}"
            else:
                fba = ov("fba", _claim(main, suf_fba))
                fbm = ov("fbm", _claim(main, suf_fbm))
            for s in (fba, fbm):        # reserve hand-edited values too
                if s:
                    used_skus.add(s.upper())

        asin = it.get("asin", "")
        sc_shadows = {_s(r.get("ProductID") or r.get("ID")).upper()
                      for r in (index.shadows_for_asin(asin) if (index and asin) else [])}
        fba_on_sc = bool(fba and fba.upper() in sc_shadows)
        fbm_on_sc = bool(fbm and fbm.upper() in sc_shadows)

        # note
        note = ""
        if main_on_sc:
            note = "Already on SellerCloud — will not recreate"
        elif bi.status == "new":
            note = "New brand — manufacturer AI-suggested" if bi.ai_mfr else "New brand"
        elif is_kit:
            note = f"amz pack {pack} → kit"

        results.append(RowResult(
            item=it, brand=brand or bi.brand, brand_status=bi.status,
            id_value=id_value, id_kind=id_kind,
            main=main, main_on_sc=main_on_sc,
            fba=fba, fba_on_sc=fba_on_sc, fbm=fbm, fbm_on_sc=fbm_on_sc,
            kit_value=kit_value, kit_shadows=kit_shadows, note=note,
            is_kit=is_kit, pack=pack,
            manufacturer=bi.manufacturer, prefix=bi.prefix,
            purchaser=bi.purchaser, sourcer=bi.sourcer,
            brand_name=(bi.brand_name or brand or bi.brand),
        ))
    return results


def build_groups(results: list[RowResult]) -> list[dict]:
    """Group review rows by brand for the board."""
    order: list[str] = []
    by: dict[str, list[RowResult]] = {}
    for r in results:
        if r.brand not in by:
            by[r.brand] = []
            order.append(r.brand)
        by[r.brand].append(r)
    groups = []
    for b in order:
        rows = by[b]
        first = rows[0]
        groups.append({
            "brand": b,
            "brand_name": first.brand_name or b,
            "status": first.brand_status,
            "manufacturer": first.manufacturer,
            "prefix": first.prefix,
            "row_count": len(rows),
            "rows": [{
                "id": r.item["_id"], "asin": r.item.get("asin", ""),
                "id_value": r.id_value, "id_kind": r.id_kind,
                "main": r.main, "main_on_sc": r.main_on_sc,
                "fba": r.fba, "fba_on_sc": r.fba_on_sc,
                "fbm": r.fbm, "fbm_on_sc": r.fbm_on_sc,
                "kit_value": r.kit_value, "kit_shadows": r.kit_shadows,
                "is_kit": r.is_kit, "pack": r.pack,
                "note": r.note,
            } for r in rows],
        })
    return groups


def counts(results: list[RowResult]) -> dict:
    # Count DISTINCT mains (rows sharing a product share one main) so "New mains"
    # isn't inflated by multiple ASIN rows of the same product.
    main_sc: dict[str, bool] = {}
    for r in results:
        if r.main:
            k = r.main.upper()
            main_sc[k] = main_sc.get(k, False) or r.main_on_sc
    on_sc = sum(1 for v in main_sc.values() if v)
    new_brands = sorted({r.brand for r in results if r.brand_status == "new"})
    return {
        "total": len(results),
        "on_sc": on_sc,
        "new": sum(1 for v in main_sc.values() if not v),
        "new_brands": new_brands,
        "new_brand_count": len(new_brands),
        "new_brand_rows": sum(1 for r in results if r.brand_status == "new"),
    }


# ── export — matches the user's real SellerCloud templates ──────────────────
# bulk template.xlsx (sheet "Bulk"):
BULK_HEADERS = [
    "ProductID", "ProductName", "ASIN", "UPC", "QTY", "AmazonEnabled", "FulfilledBy",
    "AmazonPrice", "AmazonPriceUseDefault", "QtyPerCase", "CostPerCase",
    "ManufacturerSKU", "BrandName", "ProductGroupName", "ManufacturerID",
    "SOURCE_LEAD", "Purchaser",
]
# Column widths + second sheet copied from the user's real bulk template so the
# exported file is laid out exactly like the one they sent.
BULK_COL_WIDTHS = {
    "A": 18.6, "B": 55.2, "C": 12.7, "D": 16.7, "E": 6.3, "F": 14.1, "G": 12.6,
    "H": 11.6, "I": 20.9, "J": 10.4, "K": 11.3, "L": 15.6, "M": 13.3, "N": 17.4,
    "O": 17.4, "Q": 21.7,
}


def _person(cfg: dict, item: dict) -> str:
    cfg = cfg or {}
    if cfg.get("mode") == "value":
        return _s(cfg.get("value"))
    col = cfg.get("col")
    return _s((item.get("_raw") or {}).get(col)) if col else ""


def _num(v):
    """Parse a possibly-formatted number ('$1,234.50') → float, else None."""
    if v is None:
        return None
    s = str(v).replace(",", "").replace("$", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


_TITLE_PACK_OF_RE = re.compile(r"pack\s+of\s+(\d+)", re.I)
_TITLE_COUNT_RE = re.compile(r"(\d+)\s*(?:-\s*)?(counts?|ct|cnt|packs?|pk)\b", re.I)


def _title_pack(title: str) -> tuple[int, str] | None:
    """A count/pack quantity stated IN the title → (n, 'count'|'pack'), else None.
    '…3 Count' → (3,'count'); '…3-pack'/'pack of 3' → (3,'pack'). Only n≥2 (a real
    multi-unit; a bare '1 count' is just 'Each'). Used for the MAIN SKU's UOM suffix."""
    t = title or ""
    m = _TITLE_PACK_OF_RE.search(t)
    if m:
        n = int(m.group(1))
        return (n, "pack") if n >= 2 else None
    for m in _TITLE_COUNT_RE.finditer(t):
        n = int(m.group(1))
        if n < 2:
            continue
        w = m.group(2).lower()
        return n, ("count" if w.startswith(("count", "ct", "cnt")) else "pack")
    return None


def build_files(results: list[RowResult], config: dict, tag: str = "") -> list[tuple[str, bytes]]:
    """Return [(filename, xlsx-bytes), ...] — the three SellerCloud import files:

      bulk.xlsx        — one row PER SKU (main + -FBA/-FBM shadows + kit _QY shadows),
                         17-col Bulk template with every column filled per the rules.
      kits.xlsx        — ParentSKU(kit) / ChildSKU(main) / QTY / InventoryDependantOption
      amz_shadows.xlsx — ParentSKU(main) / ShadowSKU / CompanyID

    Bulk column rules (per the user's spec):
      ProductID=sku · ProductName=Brand[+MPN if medical]+title+(UOM) ·
      ASIN=only FBA/FBM · UPC=main only · QTY=main 0 / non-QY shadow 1 / kit N ·
      AmazonEnabled=TRUE for FBA/FBM else blank · FulfilledBy=Amazon for -FBA else Merchant ·
      AmazonPrice=FBA buybox×1.2, FBM=FBA×1.1 (round to 2dp), only FBA/FBM ·
      AmazonPriceUseDefault=FALSE always · QtyPerCase/CostPerCase=main only (rule 11) ·
      ManufacturerSKU=MPN (main only) · BrandName=canonical case-sensitive ·
      ProductGroupName=CPG or blank · ManufacturerID=manufacturer name ·
      SOURCE_LEAD/Purchaser=per-brand from the catalog.
    Skips SKUs already on SellerCloud."""
    company = config.get("company", "Ford Medical")
    cid = company_id(company)
    suf_fba, suf_fbm = suffixes(company)
    create = config.get("create", {"main": True, "shadow": True, "kit": True})
    batch_type = str(config.get("batch_type", "CPG"))
    is_cpg = not batch_type.lower().startswith("med")
    purchaser_cfg = config.get("purchaser", {})
    sourcer_cfg = config.get("sourcer", {})

    def uom(qty: int) -> str:
        return f"Pack of {qty}" if qty and qty > 1 else "Each"

    def product_name(r: RowResult, name_qty: int, is_main: bool = False) -> str:
        brand = r.brand_name or r.brand
        # prefer the AI-cleaned description (set on the item by the export route);
        # fall back to the raw vendor title.
        title = _s(r.item.get("clean_name")) or _s(r.item.get("name"))
        parts = [brand, title] if is_cpg else [brand, _s(r.item.get("mpn")), title]
        base = " ".join(p for p in parts if p).strip()
        # UOM suffix rules differ by SKU type:
        #   MAIN     → the count/pack stated IN the title ('3 Count' → '3 count'), else 'Each'
        #   FBA/FBM  → the amz-pack multiplier ('Each' for 1, 'Pack of N' for a kit)
        if is_main:
            tp = _title_pack(title)
            suffix = f"{tp[0]} {tp[1]}" if tp else "Each"
        else:
            suffix = uom(name_qty)
        return f"{base} ({suffix})".strip() if base else ""

    def cost_and_qtycase(r: RowResult):
        """(CostPerCase, QtyPerCase) per rule 11."""
        cc = _num(r.item.get("costcase"))
        uc = _num(r.item.get("cost"))
        qpc = _num(r.item.get("qtycase"))
        if cc is not None:
            return cc, qpc
        if uc is not None and qpc:
            return round(uc * qpc, 2), qpc
        if uc is not None:
            return uc, None            # only unit cost → leave QtyPerCase empty
        return None, qpc

    bulk_wb = Workbook(); bs = bulk_wb.active; bs.title = "Bulk"
    bs.append(BULK_HEADERS)
    _rows: dict = {"main": [], "fba": [], "fbm": []}   # written as mains, then FBA, then FBM

    def emit(sku: str, kind: str, r: RowResult, name_qty: int) -> None:
        # kind: 'main' | 'fba' | 'fbm'. Shadows/kits (fba/fbm) are the Amazon SKUs.
        is_amazon = kind in ("fba", "fbm")
        is_fba = kind == "fba"
        is_main = kind == "main"
        qty = 0 if is_main else name_qty          # main 0; non-QY shadow 1; kit N
        buybox = _num(r.item.get("price"))
        price = None
        if is_amazon and buybox is not None:
            fba_p = round(buybox * 1.2, 2)
            price = fba_p if is_fba else round(fba_p * 1.1, 2)
        # UPC / QtyPerCase / CostPerCase / ManufacturerSKU go on EVERY sku (main +
        # its FBA/FBM/kit shadows), not just the main.
        cpc, qpc = cost_and_qtycase(r)
        upc = _s(r.item.get("upc")) or None
        mpn = _s(r.item.get("mpn")) or None
        _rows[kind].append([
            sku,                                                        # ProductID
            product_name(r, name_qty, is_main),                         # ProductName
            (_s(r.item.get("asin")) or None) if is_amazon else None,    # ASIN
            upc,                                                        # UPC
            qty,                                                        # QTY
            "TRUE" if is_amazon else None,                              # AmazonEnabled
            "Amazon" if is_fba else "Merchant",                         # FulfilledBy
            price,                                                      # AmazonPrice
            ("FALSE" if is_amazon else None),                           # AmazonPriceUseDefault: blank on main
            qpc,                                                        # QtyPerCase
            cpc,                                                        # CostPerCase
            mpn,                                                        # ManufacturerSKU
            (r.brand_name or r.brand or None),                          # BrandName
            ("CPG" if is_cpg else None),                                # ProductGroupName
            (r.manufacturer or None),                                   # ManufacturerID
            (r.sourcer or _person(sourcer_cfg, r.item) or None),        # SOURCE_LEAD
            (r.purchaser or _person(purchaser_cfg, r.item) or None),    # Purchaser
        ])

    emitted_mains: set = set()
    for r in results:
        # Each unique main is emitted ONCE (rows sharing a product share one main).
        if create.get("main", True) and r.main and not r.main_on_sc and r.main.upper() not in emitted_mains:
            emit(r.main, "main", r, 1)
            emitted_mains.add(r.main.upper())
        # One child per channel for this ASIN: kit shadow (qty=pack) or single (qty=1).
        child_qty = r.pack if r.is_kit else 1
        if r.fba and not r.fba_on_sc:
            emit(r.fba, "fba", r, child_qty)
        if r.fbm and not r.fbm_on_sc:
            emit(r.fbm, "fbm", r, child_qty)
    # write grouped by type: all mains, then all FBA, then all FBM (cleaner + mains
    # precede their shadows)
    for _kind in ("main", "fba", "fbm"):
        for _row in _rows[_kind]:
            bs.append(_row)
    for _col, _w in BULK_COL_WIDTHS.items():
        bs.column_dimensions[_col].width = _w

    # kits.xlsx — connect each _QY kit shadow (r.fba/r.fbm on a kit row) to its main
    kit_wb = Workbook(); ks = kit_wb.active; ks.title = "Sheet1"
    ks.append(["ParentSKU", "ChildSKU", "QTY", "InventoryDependantOption"])
    for r in results:
        if r.is_kit and r.main:
            if r.fba:
                ks.append([r.fba, r.main, r.pack, "Independent"])
            if r.fbm:
                ks.append([r.fbm, r.main, r.pack, "All_Components"])

    # amz_shadows.xlsx — connect the non-kit shadows to their main
    amz_wb = Workbook(); az = amz_wb.active; az.title = "Sheet1"
    az.append(["ParentSKU", "ShadowSKU", "CompanyID"])
    for r in results:
        if not create.get("shadow", True) or r.is_kit:   # kit shadows live in kits.xlsx
            continue
        if r.fbm and not r.fbm_on_sc:
            az.append([r.main, r.fbm, cid])
        if r.fba and not r.fba_on_sc:
            az.append([r.main, r.fba, cid])

    sfx = f"_{tag}" if tag else ""

    def _save(wb) -> bytes:
        b = io.BytesIO(); wb.save(b); return b.getvalue()

    # bulk always; kits/amz only when they actually have data rows (a sheet with
    # just its header has max_row == 1) — so no kit file when there are no kits.
    out = [(f"Bulk{sfx}.xlsx", _save(bulk_wb))]
    if ks.max_row > 1:
        out.append((f"Kits{sfx}.xlsx", _save(kit_wb)))
    if az.max_row > 1:
        out.append((f"AMZ{sfx}.xlsx", _save(amz_wb)))
    return out
