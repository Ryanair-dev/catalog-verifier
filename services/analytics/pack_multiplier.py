"""
Pack multiplier: how many VENDOR UNITS one Amazon sale uses (1X, 2X, 3X ...).
"""
from __future__ import annotations

import re
from contextlib import closing
from typing import Any

from services import database
from services.analytics.matching_core.matching.attr_extract import read_counts

SURE, PROBABLY, NOT_SURE, CONFIRMED = "sure", "probably", "not_sure", "confirmed"

_CASE_OF = re.compile(r"\bcase\s+of\s+(\d+)\b", re.IGNORECASE)
_TOTAL = re.compile(r"\btotal(?:\s+of)?\s*:?\s*(\d+)\b", re.IGNORECASE)          # "Total: 24 Batteries"
_N_X_M = re.compile(r"\b(\d+)\s*[x\u00d7]\s*(\d+)\s*[- ]?(?:count|ct|pack|pk|pcs|pieces|ea|each)\b",
                    re.IGNORECASE)                                                   # "6 X 4 Count Packs"
_QTY = re.compile(r"\b(?:quantity|qty)\s*:?\s*(\d+)\b", re.IGNORECASE)          # "9-Volt - Quantity 6"
_BOX_OF = re.compile(r"\b(?:box|bag|tub|bottle|canister|jar)\s+of\s+(\d+)\b", re.IGNORECASE)
# Vendor unit-of-measure codes: one item vs. a container of several.
_UOM_EACH = {"EA", "EACH", "PC", "PCS", "PIECE", "UN", "UNIT"}
_UOM_MULTI = {"CS", "CA", "CASE", "BX", "BOX", "PK", "PACK", "PKG", "CT", "CTN", "DZ", "DOZ", "DOZEN"}


# --------------------------------------------------------------------------- #
# Reading numbers
# --------------------------------------------------------------------------- #

def _int(v: Any) -> int | None:
    try:
        n = int(float(str(v).strip().replace(",", "")))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _text_units(text: str, *, amazon: bool) -> tuple[int | None, int | None, str | None]:
    """(eaches, multiplier, kind) printed in one title.
    eaches: None when no count is printed (or counts that don't add up).
    kind:   "pack" for "Pack of N" / "M Packs of K" / (Amazon) "Case of N" = N of the unit;
            "bundle" for "N-Pack" / "N Pack", which on Amazon is often the number of ITEMS ("AA Batteries (2 Pack)").
    "36 Count, Pack of 4" -> (144, 4, pack); "72 Count, 3 Packs of 24" -> (72, 3, pack); "(2 Pack)" -> (None, 2, bundle)."""
    c = read_counts(text or "")
    counts = sorted(set(c["count"]))
    packs = list(c["pack"])
    m = _BOX_OF.search(text or "")
    if m and 1 < int(m.group(1)) <= 10000:
        box = int(m.group(1))                   # "Box of 100" = 100 items in ONE box (read_counts calls it a pack)
        if box in packs:
            packs.remove(box)
        if not counts:
            counts = [box]
    mult, kind = (packs[0], "pack") if packs else (None, None)
    nxm = _N_X_M.search(text or "")
    if nxm and mult is None and 1 < int(nxm.group(1)) <= 1000:
        mult, kind = int(nxm.group(1)), "pack"     # "6 X 4 Count" = 6 packs of 4
        counts = [int(nxm.group(2))]
    if not counts:
        q = _QTY.search(text or "")
        if q and 1 < int(q.group(1)) <= 10000:
            counts = [int(q.group(1))]
    if mult is None and amazon:
        m = _CASE_OF.search(text or "")
        if m and 1 < int(m.group(1)) <= 1000:
            mult, kind = int(m.group(1)), "pack"   # on Amazon, "Case of 12" is what the buyer gets
    if mult is None and c["bundle"]:
        mult, kind = c["bundle"][0], "bundle"
    total = _TOTAL.search(text or "")
    if total and 1 < int(total.group(1)) <= 100000:
        n = int(total.group(1))                   # a printed total wins: "4-Count Packs - Total: 24"
        if mult is None and len(counts) == 1 and counts[0] < n and n % counts[0] == 0:
            mult, kind = n // counts[0], "pack"
        return n, mult, kind
    if not counts:
        return None, mult, kind
    if len(counts) == 1:
        return counts[0] * (mult or 1), mult, kind
    if mult and counts[-1] == mult * counts[0]:
        return counts[-1], mult, kind           # the total is printed too ("72 Count, 3 Packs of 24")
    return None, mult, kind                     # several counts that don't add up: ambiguous


def _vendor_count_from_matcher(source: dict, extracted: dict | None) -> tuple[int | None, str | None]:
    """The count the matcher itself read for this vendor row (attr_extract.extract_vendor): the vendor file's
    columns, then the AI's reading of the title ("AA-4" -> 4), then the title. Same reading as the variant gate."""
    try:
        from services.analytics.matching_core.matching.attr_extract import extract_vendor
        box = (extract_vendor(source, extracted).get("fields") or {}).get("count")
    except Exception:  # noqa: BLE001 - a reader failure only means "no count"
        return None, None
    if not isinstance(box, dict) or box.get("conflict"):
        return None, None
    src = str(box.get("source") or "")
    return _int(box.get("value")), ("the AI's reading of the vendor title" if src.startswith("ai")
                                    else "the vendor file's columns" if src == "vendor_column" else "the vendor title")


def _ratio(amazon_eaches: int | None, vendor_eaches: int | None) -> tuple[int | None, str | None]:
    """Whole-number multiplier, or (None, problem)."""
    if not amazon_eaches or not vendor_eaches:
        return None, None
    if amazon_eaches < vendor_eaches:
        return None, "less_than_one"
    if amazon_eaches % vendor_eaches:
        return None, "not_whole"
    return amazon_eaches // vendor_eaches, None


def _vendor_columns(raw: dict | None) -> tuple[str | None, int | None]:
    """(UoM code, case pack) from the vendor file's own columns, when it has them."""
    uom = case_pack = None
    for header, value in (raw or {}).items():
        h = re.sub(r"[^a-z]", " ", str(header).lower()).split()
        joined = " ".join(h)
        if uom is None and ("uom" in h or h == ["um"] or "unit of measure" in joined or joined.endswith("selling unit")):
            code = re.sub(r"[^A-Z]", "", str(value or "").upper())
            uom = code or None
        elif case_pack is None and ("cspk" in h or "case pack" in joined or "casepack" in h
                                    or "qty per case" in joined or "units per case" in joined):
            case_pack = _int(value)
    return uom, case_pack


def _digits(v: Any) -> str:
    return re.sub(r"\D", "", str(v or "")).lstrip("0")


def _same_barcode(source: dict, amazon: dict) -> bool:
    mine = _digits(source.get("upc"))
    if len(mine) < 6:
        return False
    theirs = [amazon.get("upc"), amazon.get("ean"), amazon.get("gtin"),
              *(amazon.get("upcs") or []), *(amazon.get("eans") or []), *(amazon.get("gtins") or [])]
    return any(_digits(t) == mine for t in theirs if t)


# --------------------------------------------------------------------------- #
# Confirmed numbers (set by a person on the run page)
# --------------------------------------------------------------------------- #

def pair_key(source: dict, asin: str) -> str:
    """Same rule as the Monday reports' Pair key: ASIN + the vendor's item id (else UPC, else title)."""
    item = str(source.get("itemid") or source.get("mpn") or source.get("upc") or "").strip().lower()
    if not item:
        item = "title:" + re.sub(r"\s+", " ", str(source.get("title") or "").strip().lower())[:80]
    return f"{str(asin or '').strip().upper()}|{item}"


def ensure_table() -> None:
    with database._LOCK, closing(database._connect()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pack_confirmations (
                pair_key     TEXT PRIMARY KEY,
                asin         TEXT NOT NULL,
                multiplier   INTEGER NOT NULL,
                confirmed_by TEXT DEFAULT '',
                run_id       INTEGER,
                confirmed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")


_cache: dict[str, Any] = {"db": None, "rows": None}


def _confirmed(key: str) -> dict | None:
    if _cache["db"] != str(database.DB_PATH) or _cache["rows"] is None:
        ensure_table()
        with closing(database._connect()) as conn:
            _cache["rows"] = {r["pair_key"]: dict(r) for r in conn.execute("SELECT * FROM pack_confirmations")}
        _cache["db"] = str(database.DB_PATH)
    return _cache["rows"].get(key)


def confirm(source: dict, asin: str, multiplier: int, confirmed_by: str = "", run_id: int | None = None) -> str:
    """Save a person's number for this pair. Later runs and rescores use it."""
    if not isinstance(multiplier, int) or multiplier < 1 or multiplier > 10000:
        raise ValueError("multiplier must be a whole number from 1 to 10000")
    ensure_table()
    key = pair_key(source, asin)
    with database._LOCK, closing(database._connect()) as conn, conn:
        conn.execute(
            "INSERT INTO pack_confirmations(pair_key, asin, multiplier, confirmed_by, run_id) VALUES (?,?,?,?,?) "
            "ON CONFLICT(pair_key) DO UPDATE SET multiplier=excluded.multiplier, "
            "confirmed_by=excluded.confirmed_by, run_id=excluded.run_id, confirmed_at=CURRENT_TIMESTAMP",
            (key, str(asin).upper(), multiplier, (confirmed_by or "")[:80], run_id),
        )
    _cache["rows"] = None
    return key


# --------------------------------------------------------------------------- #
# The multiplier
# --------------------------------------------------------------------------- #

def compute(source: dict, amazon: dict, vendor_raw: dict | None = None,
            extracted: dict | None = None) -> dict:
    """source = the vendor row (title, upc, itemid, raw); amazon = the normalized SP-API record;
    extracted = the row's saved AI fields (what the AI read from the vendor title)."""
    asin = str(amazon.get("asin") or "")
    key = pair_key(source, asin)
    done = _confirmed(key)
    if done:
        return {"multiplier": int(done["multiplier"]), "status": CONFIRMED, "source": "person",
                "why": f"Set by {done.get('confirmed_by') or 'a person'} on {str(done.get('confirmed_at'))[:10]}.",
                "title": None, "fields": None, "same_barcode": None, "pair_key": key}

    notes: list[str] = []
    vendor_title = str(source.get("title") or "")
    raw = source.get("raw") or vendor_raw or {}

    # ---- vendor unit, in eaches ----
    v_eaches, v_mult, _ = _text_units(vendor_title, amazon=False)
    if v_eaches is None:
        n, where = _vendor_count_from_matcher({**source, "raw": raw}, extracted)
        if n:
            v_eaches = n
            notes.append(f"vendor count {n} comes from {where}")
    vendor_said_nothing = v_eaches is None and v_mult is None
    vendor_unclear = False
    uom, case_pack = _vendor_columns(raw)
    if uom in _UOM_MULTI:
        if case_pack and case_pack > 1:
            v_eaches = v_eaches * case_pack if v_eaches else None
            v_mult = (v_mult or 1) * case_pack
            vendor_said_nothing = False
            notes.append(f"vendor unit is {uom} of {case_pack}")
        else:
            vendor_unclear = True
            notes.append(f"vendor unit is {uom}, but the file has no case pack")
    elif uom in _UOM_EACH:
        vendor_said_nothing = False             # the file says one unit = one item
        notes.append("vendor unit is EA")
    elif _CASE_OF.search(vendor_title):
        vendor_unclear = True
        notes.append("vendor title says 'case of', so the vendor unit is unclear")

    # ---- estimate 1: the Amazon title ----
    a_eaches, a_mult, a_kind = _text_units(str(amazon.get("title") or ""), amazon=True)
    ambiguous = False
    if a_eaches and v_eaches:
        title_est, title_problem = _ratio(a_eaches, v_eaches)
    elif a_kind == "bundle" and v_eaches and not v_mult:
        # "AA Batteries (2 Pack)" next to a vendor count: on Amazon "N Pack" is usually N items
        title_est, title_problem = _ratio(a_mult, v_eaches)
        if title_est is None:
            title_est, title_problem = a_mult, None     # fewer items than the vendor unit: read it as N units
        ambiguous = a_mult != v_eaches
    elif a_mult or v_mult:
        title_est, title_problem = _ratio(a_mult or 1, v_mult or 1)
    else:
        title_est, title_problem = None, None

    # ---- estimate 2: Amazon's fields (independent of the title) ----
    uc_type = str(amazon.get("unit_count_type") or "").lower()
    uc = _int(amazon.get("unit_count")) if any(w in uc_type for w in ("count", "each", "piece")) else None
    pack_field = _int(amazon.get("item_package_quantity")) or _int(amazon.get("pack_count"))
    from_count = _ratio(uc, v_eaches)[0] if (uc and v_eaches) else None
    from_pack = _ratio(pack_field, v_mult or 1)[0] if (pack_field and pack_field > 1) else None
    # Amazon's fields can disagree with each other (unit_count filled per pack, item_package_quantity = 6)
    fields_conflict = bool(from_count and from_pack and from_count != from_pack)
    field_est = from_pack if fields_conflict else (from_count or from_pack)

    same = _same_barcode(source, amazon)
    out = {"title": title_est, "fields": field_est, "same_barcode": same, "pair_key": key,
           "vendor_eaches": v_eaches, "amazon_eaches": a_eaches}

    def done_(mult: int | None, status: str, src: str, why: str) -> dict:
        return {**out, "multiplier": mult, "status": status, "source": src,
                "why": " ".join([why, *(n[0].upper() + n[1:] + "." for n in notes)]).strip()}

    if title_problem == "less_than_one":
        return done_(None, NOT_SURE, "title", "The Amazon listing holds less than one vendor unit.")
    if title_problem == "not_whole":
        return done_(None, NOT_SURE, "title", "Amazon's amount is not a whole number of vendor units.")
    guess = title_est or field_est or 1
    src = "title" if title_est else ("fields" if field_est else "default")
    if vendor_unclear:
        return done_(guess, NOT_SURE, src, "Not sure what one vendor unit is.")
    amazon_says_more = (a_eaches or 1) > 1 or (a_mult or 1) > 1 or (uc or 1) > 1 or (pack_field or 1) > 1
    if vendor_said_nothing and amazon_says_more:
        return done_(guess, NOT_SURE, src,
                     "The vendor title gives no count, so we can't tell how many vendor units one sale uses.")
    if fields_conflict:
        return done_(title_est or field_est, NOT_SURE, src,
                     f"Amazon's fields disagree: its count field says {from_count}, its pack field says {from_pack}.")
    if title_est and field_est:
        if title_est == field_est:
            return done_(title_est, SURE, "title+fields", f"Title and Amazon fields both say {title_est}.")
        return done_(title_est, NOT_SURE, "title", f"Title says {title_est}, Amazon fields say {field_est}.")
    one = title_est or field_est
    if one:
        if ambiguous:
            return done_(one, NOT_SURE, src,
                         f"The listing says '{a_mult} Pack': that could be {a_mult} items ({one}X) or "
                         f"{a_mult} of the vendor's units.")
        return done_(one, PROBABLY, src, f"Only the {src} gives a number: {one}.")
    return done_(1, PROBABLY, "default",
                 "Nothing in the title or Amazon's fields says pack, so 1 is assumed (check the pictures if unsure).")
