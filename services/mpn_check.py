"""
MPN Check -- paste MPNs (+ manufacturer/brand), look up fields against a LOCAL
mirror of Ford Medical's internal "fbidb" Azure database (see
services/mpn_check_sync.py) to confirm the product exists in our own data,
PLUS a live AccessGUDID lookup for the actual qty/case answer.

qty_case is NOT sourced from fbidb's own packaged_products table -- per user
2026-09-25: "please dont take the case values from FBI", confirmed live that
its "CS" tag can mislabel an intermediate packaging tier as the full case
(a real Quality King item whose true case is 12 units showed fbidb "CS"=3).
Instead qty_case comes from services.mpn_check_gudid (FDA's public, free
AccessGUDID database, searchable by MPN/catalog number) -- verified live
against 5 real MPNs from our own vendor data, including a genuine private-
label case (STL2644 is GUDID-labeled "Centurion" but we know it as "Medline")
and a genuine cross-manufacturer MPN collision ("1271" matches 20 unrelated
companies' devices, correctly rejected since none is Teleflex).

An MPN alone is not globally unique -- the same part-number string can belong
to entirely different manufacturers (Create SKUs hit this exact problem for
_main_exists() on 2026-07-17: "MPN existence check is brand-scoped"). So every
lookup here is scoped to (normalized MPN, resolved manufacturer). The user
supplies a manufacturer/brand per MPN (or one for the whole paste) specifically
to disambiguate.

Currently supported field: qty_case. Designed to grow: SUPPORTED_FIELDS is the
extension point for whatever the user asks for next.

Separate, opt-in AI web-search fallback (ai_lookup_qty_case / ai_fallback) for
rows NEITHER the local mirror NOR AccessGUDID can answer -- NOT part of
check_mpns; the caller decides whether to run it, per the user's explicit
"the LLM should be a separate option."
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from services import database
from services import mpn_check_gudid as gudid

log = logging.getLogger(__name__)

SUPPORTED_FIELDS = {"qty_case"}


def _norm_txt(s) -> str:
    return "".join(c for c in str(s or "").lower() if c.isalnum())


def _norm_mpn(s) -> str:
    """Matches fbidb's own products.normalized_manufacturer_part_number
    convention exactly: lowercase, alnum-only, THEN strip leading zeros --
    verified live ('0000TD0320L' -> 'td0320l', not '0000td0320l')."""
    n = _norm_txt(s)
    stripped = n.lstrip("0")
    return stripped if stripped else n


def _chunks(items, n=400):
    items = list(items)
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _local_product_id(pairs_norm: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """(normalized_mpn, normalized_manufacturer) -> local product_id, informational
    only now (confirms the MPN exists in OUR OWN mirrored data) -- no longer the
    source of qty_case."""
    uniq_mfr = sorted({nm for _, nm in pairs_norm if nm})
    if not uniq_mfr:
        return {}
    with database._connect() as conn:
        mfr_map: dict[str, set[int]] = {}
        for chunk in _chunks(uniq_mfr):
            qmarks = ",".join(["?"] * len(chunk))
            for norm_name, mid in conn.execute(
                f"SELECT normalized_name, manufacturer_id FROM mpn_manufacturer_lookup "
                f"WHERE normalized_name IN ({qmarks})", chunk):
                mfr_map.setdefault(norm_name, set()).add(mid)

        mfr_ids_by_row = {(nmpn, nmfr): mfr_map.get(nmfr, set()) for nmpn, nmfr in pairs_norm}
        uniq_mpn = sorted({nmpn for nmpn, _ in pairs_norm if nmpn})
        prod_map: dict[tuple[str, int], int] = {}
        for chunk in _chunks(uniq_mpn):
            qmarks = ",".join(["?"] * len(chunk))
            for pid, nmpn, mid in conn.execute(
                f"SELECT product_id, normalized_mpn, manufacturer_id FROM mpn_products "
                f"WHERE normalized_mpn IN ({qmarks})", chunk):
                prod_map.setdefault((nmpn, mid), pid)

    out: dict[tuple[str, str], int] = {}
    for nmpn, nmfr in pairs_norm:
        for mid in mfr_ids_by_row.get((nmpn, nmfr), ()):
            if (nmpn, mid) in prod_map:
                out[(nmpn, nmfr)] = prod_map[(nmpn, mid)]
                break
    return out


def check_mpns(pairs: list[tuple[str, str]], fields: set[str] | None = None) -> list[dict]:
    """pairs: [(mpn, manufacturer), ...] (raw, as typed/pasted). Returns one
    dict per input pair, SAME ORDER, duplicates preserved:
    {mpn, manufacturer, matched, product_id, qty_case, note}.

    product_id comes from the local mirror (confirms the MPN/manufacturer
    exists in our own data -- informational). qty_case comes from a LIVE
    AccessGUDID lookup (services.mpn_check_gudid), run concurrently across all
    rows -- free, no fbidb "CS" tag involved. matched=True iff AccessGUDID
    returned a real case quantity."""
    fields = (fields or {"qty_case"}) & SUPPORTED_FIELDS

    rows = [{"mpn": (m or "").strip(), "manufacturer": (b or "").strip()} for m, b in pairs]
    for r in rows:
        r["_n_mpn"] = _norm_mpn(r["mpn"])
        r["_n_mfr"] = _norm_txt(r["manufacturer"])

    valid_idx = [i for i, r in enumerate(rows) if r["mpn"] and r["manufacturer"]]

    pid_map = _local_product_id([(rows[i]["_n_mpn"], rows[i]["_n_mfr"]) for i in valid_idx])

    gudid_by_idx: dict[int, dict] = {}
    if "qty_case" in fields and valid_idx:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(gudid.lookup_case_qty, rows[i]["mpn"], rows[i]["manufacturer"]): i
                    for i in valid_idx}
            for fut in as_completed(futs):
                gudid_by_idx[futs[fut]] = fut.result()

    out: list[dict] = []
    for i, r in enumerate(rows):
        pid = pid_map.get((r["_n_mpn"], r["_n_mfr"]))
        if not r["mpn"] or not r["manufacturer"]:
            row_out = {"mpn": r["mpn"], "manufacturer": r["manufacturer"], "matched": False,
                       "product_id": pid, "note": "missing MPN or manufacturer/brand"}
        elif "qty_case" not in fields:
            row_out = {"mpn": r["mpn"], "manufacturer": r["manufacturer"],
                       "matched": pid is not None, "product_id": pid, "note": ""}
        else:
            g = gudid_by_idx.get(i, {"qty_case": None, "matched": False, "note": ""})
            row_out = {"mpn": r["mpn"], "manufacturer": r["manufacturer"],
                       "matched": g["matched"], "product_id": pid, "note": g["note"]}
        if "qty_case" in fields:
            row_out["qty_case"] = gudid_by_idx.get(i, {}).get("qty_case") if r["mpn"] and r["manufacturer"] else None
        out.append(row_out)
    return out


# --------------------------------------------------------------------------
# AI web-search fallback -- SEPARATE, opt-in (per user: "The LLM should be a
# separate option"). Only meant to be called for rows check_mpns() couldn't
# answer. Mirrors the web_search_20250305 pattern already validated in this
# app for Brand Analytics AI Fill (2026-08-12: "verifies rather than guesses").
# --------------------------------------------------------------------------

_QTY_RE = re.compile(r"\b(\d{1,5})\b")


def ai_lookup_qty_case(mpn: str, manufacturer: str, client) -> dict:
    """One web-search-backed lookup. Returns {qty_case, confidence, note}.
    qty_case is None unless the model is confident it found the SAME product's
    real case-pack quantity -- never a guess. Never raises; client must be an
    Anthropic client (services.ai_recheck.make_client()) with web search access."""
    prompt = (
        f"Find the case-pack quantity (how many individual units ship in one "
        f"case/carton) for this exact product:\n"
        f"  Manufacturer: {manufacturer}\n"
        f"  Manufacturer Part Number (MPN): {mpn}\n\n"
        f"Search the web (manufacturer spec sheets, distributor listings like "
        f"Medline/McKesson/Henry Schein, product datasheets) for this SPECIFIC "
        f"part number from this SPECIFIC manufacturer. Only report a quantity "
        f"you can verify actually belongs to this MPN -- do not guess, and do "
        f"not report a quantity for a similar-but-different part number.\n\n"
        f'Respond with ONLY a JSON object, no other text: '
        f'{{"qty_case": <integer or null>, "confidence": "high"|"medium"|"low", '
        f'"source": "<short description of where you found it, or empty>"}}'
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return {"qty_case": None, "confidence": "low", "note": "AI: no parseable answer"}
        data = json.loads(m.group())
        qty = data.get("qty_case")
        conf = str(data.get("confidence") or "low").lower()
        if qty is None or conf == "low":
            return {"qty_case": None, "confidence": conf,
                    "note": "AI: could not verify a case-pack quantity"}
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            return {"qty_case": None, "confidence": conf, "note": "AI: non-numeric answer"}
        src = (data.get("source") or "").strip()[:80]
        return {"qty_case": qty, "confidence": conf,
                "note": f"AI ({conf} confidence){': ' + src if src else ''}"}
    except Exception as exc:  # noqa: BLE001
        log.warning("[mpn_check] AI web lookup failed for %s/%s: %s", mpn, manufacturer, str(exc)[:150])
        return {"qty_case": None, "confidence": "low", "note": f"AI lookup failed: {str(exc)[:100]}"}
