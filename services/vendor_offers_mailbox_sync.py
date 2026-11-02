"""
Vendor Offer Analytics -- direct ingestion straight from the vendors' own
catalog emails, bypassing FBI entirely for the 5 vendors below (replaces
services/vendor_offers_db_sync.py for them, per user request 2026-10-23:
"FBI for some reason has awful data structure. Lets take it all directly
from the offers and inbox ... but make the data clean ourselves.").

## Where this came from

A separate, already-working standalone pipeline exists on this machine
(Desktop/fbi-offer-upload, repo Fordmed-Dev/fbi-offer-upload) that fetches
these same vendor emails every 30 minutes and uploads them INTO FBI. Its
fetcher.py/transformer.py are the source of truth this module ports from --
a peer Claude Code session ("fbi offer catalog search") that had already
investigated that repo supplied the exact mailbox/matching-rule/column
details below; several of them (ABC, Diamond, Victory) were independently
re-confirmed here against that repo's own real, live, same-day cached input
files (input/amerisourcebergen, input/diamondw, input/victory) before being
ported, not just copied from the other session's report.

## What this does differently from FBI

FBI's `vendor_account_product_listings` has no delete -- a new upload only
updates the SKUs it mentions; stale ones are supposed to lapse via a 48-hour
expiry set at upload, but in practice that lags badly (confirmed by the
peer session: Quality King shows ~17k "live" FBI listings vs ~3.7k actually
in each real file; ABC ~42k vs ~11.4k). services/vendor_offers_db_sync.py
reads from those same FBI-populated Azure tables, so it inherits that same
staleness, only partially masked by its own `trashed_at`/`discontinued_at`/
`expires_at` filters. This module reads the vendor's raw file directly and,
per the user's explicit instruction, ZEROES every existing offer's avail_qty
for that vendor before applying the new file (services.vendor_offers.
merge_batches(..., reset_avail=True)) -- so a SKU missing from today's file
reads 0 immediately, not whenever FBI's 48h timer eventually catches up.

## Mailbox / auth

Group mailbox (Microsoft 365 Group, NOT a normal shared mailbox -- read via
Graph's group-conversations API, not /me/messages or /users/{mailbox}/messages):
  GROUP_ID = "cdec98fb-2033-4ff7-88ac-a6e2a3408601" (the address itself was
  never independently confirmed by either session -- .env's own stale
  SHARED_MAILBOX=catalogs@fordmed.com guess is unverified; the GROUP_ID is
  what's actually used for every Graph call here, so the address doesn't
  matter operationally).
Auth reuses services/graph_mail.py's EXISTING cached token helper (same
OUTLOOK_CLIENT_ID/SECRET/TENANT_ID/REFRESH_TOKEN already in .env, delegated,
signs in as datateamautomations@fordmed.com) -- confirmed by the peer
session that this exact credential set already has live group-read access
(fbi-offer-upload's own older credentials had expired and it was switched
over to this same set). No new credentials needed.

## Per-vendor parsing (ported, NOT reused verbatim from services/vendor_offers.py)

Diamond's mailbox CSV format is IDENTICAL to services.vendor_offers.load_diamond
(cp1252, 13 header fields vs 14 data fields, 12DGTUPC) -- confirmed live
against fbi-offer-upload's cached real file -- so Diamond calls that loader's
logic style directly (see _parse_diamond_mailbox).

Cencora/ABC, BILO, and Quality King's mailbox files are each a DIFFERENT
file/column layout than the one services.vendor_offers.py's existing
load_cencora/load_bilo/load_quality_king parse (those were built for
whatever files were manually uploaded before; the daily email attachment is
a different export each vendor also sends). Rather than risk silently
breaking either path by merging them, each gets its OWN dedicated parser
here (_parse_cencora_mailbox / _parse_bilo_mailbox / _parse_qkd_mailbox),
leaving the existing manual-upload loaders completely untouched. Victory has
no existing loader at all (manual upload never supported it) -- new here.

BILO and Quality King's column layouts were NOT independently re-verified
against a live same-day file in this session (neither vendor's email had
arrived yet when this was built -- BILO lands ~7pm, QK late morning) -- they
are ported as-is from fbi-offer-upload's transformer.py, which IS actively
processing real production emails (its git history shows a real bug fixed
3 days before this was written). Flag if either vendor's first real sync
comes back with 0 rows or a "missing column" error.

## Dedup

No date-cutoff/"today" window (unlike fbi-offer-upload, which runs once and
decides what counts as "today's" offer) -- this runs continuously via its
own scheduler, so it always just takes the single MOST RECENT matching email
per vendor and dedupes via an MD5 content hash stored in the `settings`
table (key vendor_offers_mailbox_sync_dedup), mirroring fbi-offer-upload's
own processed.json pattern. A vendor whose latest email hasn't changed since
the last successful sync is a no-op, not a re-merge.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import logging
import os
import threading

import pandas as pd
import requests
import urllib3.util.connection

from services import database
from services import graph_mail
from services import vendor_offers as vo

log = logging.getLogger(__name__)

# Force IPv4 for Graph/login.microsoftonline.com requests. Confirmed live
# 2026-10-23: on this network the IPv6 route to login.microsoftonline.com
# stalls indefinitely (observed 60s+ with zero response, no fallback) rather
# than failing fast -- the exact same issue fbi-offer-upload's own config.py
# works around the same way, after hitting it in production. This is a
# process-wide urllib3 setting, same as that pipeline applies for its whole
# process; nothing else in this app depends on IPv6 specifically.
urllib3.util.connection.HAS_IPV6 = False

GROUP_ID = "cdec98fb-2033-4ff7-88ac-a6e2a3408601"
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

# (vendor key -- matches services.vendor_offers.VENDOR_NAMES, or a pseudo-key
#  for a support file that isn't its own vendor), match type, match value,
#  accepted extensions. Ported from fbi-offer-upload/fetcher.py's VENDOR_RULES.
VENDOR_RULES: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("Cencora",                   "subject", "Completed:Product Catalog Ford Medical", (".xlsx",)),
    ("Cencora_inventory",         "sender",  "PTD_BATCH@ABC.LOCAL",                    (".txt",)),
    ("Diamond Wholesale",         "subject", "SALES CATALOG FROM DIAMOND",             (".csv",)),
    ("BILO",                      "sender",  "f.patruno@biloltd.net",                  (".xls",)),
    ("Quality King Distributors", "subject", "Updated File - QKD",                     (".xlsx", ".csv")),
    ("Victory Wholesale Grocers", "subject", "Victory DPA for Ford Medical",           (".xls", ".xlsx")),
]

VENDOR_KEYS = [
    "Cencora", "Diamond Wholesale", "BILO",
    "Quality King Distributors", "Victory Wholesale Grocers",
]


def is_configured() -> bool:
    return graph_mail.is_configured()


# --------------------------------------------------------------------------
# Graph helpers -- group conversations/threads/posts/attachments
# --------------------------------------------------------------------------

def _access_token() -> str:
    # Reuses graph_mail's own cached token (same creds, same cache) rather
    # than duplicating the OAuth exchange.
    return graph_mail._get_access_token()


def _graph_get(token: str, endpoint: str) -> dict:
    url = endpoint if endpoint.startswith("http") else f"{GRAPH_BASE_URL}{endpoint}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _graph_get_all(token: str, endpoint: str) -> list[dict]:
    items: list[dict] = []
    while endpoint:
        data = _graph_get(token, endpoint)
        items.extend(data.get("value") or [])
        endpoint = data.get("@odata.nextLink")
    return items


def _match_vendor(subject: str, sender: str) -> tuple[str | None, tuple[str, ...] | None]:
    subj = (subject or "").lower()
    sendr = (sender or "").lower()
    for key, mtype, value, exts in VENDOR_RULES:
        if mtype == "subject" and value.lower() in subj:
            return key, exts
        if mtype == "sender" and value.lower() == sendr:
            return key, exts
    return None, None


def fetch_latest_vendor_posts(token: str) -> dict[str, dict]:
    """Scan the group's conversations (newest first), returning the single
    MOST RECENT matching post per vendor key. No "today" cutoff -- unlike
    fbi-offer-upload (a once-a-day job), this sync runs continuously and
    dedups via content hash, so it always just wants whatever's currently
    the latest file for each vendor, however old or fresh."""
    latest: dict[str, dict] = {}
    want = {key for key, *_ in VENDOR_RULES}
    endpoint = f"/groups/{GROUP_ID}/conversations?$top=10&$orderby=lastDeliveredDateTime desc"

    while endpoint:
        data = _graph_get(token, endpoint)
        conversations = data.get("value") or []
        if not conversations:
            break

        for conv in conversations:
            conv_id = conv.get("id")
            subject = conv.get("topic", "")
            threads = _graph_get_all(token, f"/groups/{GROUP_ID}/conversations/{conv_id}/threads")

            for thread in threads:
                thread_id = thread.get("id")
                # A long-running vendor thread (QK, Diamond) can span more than
                # one page, so the newest post isn't necessarily on page 1.
                posts = _graph_get_all(token, f"/groups/{GROUP_ID}/threads/{thread_id}/posts")

                for post in posts:
                    if not post.get("hasAttachments"):
                        continue
                    sender = post.get("from", {}).get("emailAddress", {}).get("address", "")
                    received = post.get("receivedDateTime", "")
                    post_id = post.get("id")

                    key, extensions = _match_vendor(subject, sender)
                    if not key:
                        continue

                    if key not in latest or received > latest[key]["date"]:
                        latest[key] = {
                            "key": key, "thread_id": thread_id, "post_id": post_id,
                            "subject": subject, "sender": sender, "date": received,
                            "extensions": extensions,
                        }

        if want <= set(latest):
            break
        endpoint = data.get("@odata.nextLink")

    return latest


def _download_attachments(token: str, thread_id: str, post_id: str,
                           extensions: tuple[str, ...]) -> list[tuple[str, bytes]]:
    data = _graph_get(token, f"/groups/{GROUP_ID}/threads/{thread_id}/posts/{post_id}/attachments")
    out: list[tuple[str, bytes]] = []
    for att in data.get("value") or []:
        filename = att.get("name", "")
        ext = os.path.splitext(filename.lower())[1]
        if ext not in extensions:
            continue
        content_b64 = att.get("contentBytes")
        if not content_b64:
            continue
        out.append((filename, base64.b64decode(content_b64)))
    return out


# --------------------------------------------------------------------------
# Per-vendor parsers -> (rows, skipped[, warnings]) in services.vendor_offers'
# own native row shape: {vendor_item_id, upc, cost, qty_per_case, avail_qty,
# description, seen}. See module docstring for which of these are verified
# against a real live file vs ported from fbi-offer-upload as-is.
# --------------------------------------------------------------------------

def _parse_cencora_mailbox(catalog_bytes: bytes, inventory_bytes: bytes | None,
                            stamp: str) -> tuple[list[dict], list[dict], list[str]]:
    """ABC/Cencora catalog xlsx (header on row 2) + the separate inventory txt
    that supplies real quantities (joined on ABC # = Material Number).
    Verified live 2026-10-23 against fbi-offer-upload's cached real same-day
    file -- columns match exactly. Differs from services.vendor_offers.
    load_cencora (same underlying file) by: excluding Ford Medical's own
    "Supplier Name" rows (our own inventory being listed back at us is not a
    vendor offer) and joining the inventory file for real quantities instead
    of always leaving avail_qty blank. Shares load_cencora's CS/PK
    per-each-price-from-description conversion (see
    vo.extract_case_qty_from_desc, which since 2026-10-28 always returns a
    divisor) -- per user confirmation 2026-10-27/28."""
    df = pd.read_excel(io.BytesIO(catalog_bytes), skiprows=1, dtype=str)
    df = vo.pick_columns(df, {
        "ABC #": "item", "Product Description": "desc", "ABC Selling UoM": "uom",
        "Current Acq Cost": "cost", "UPC Barcode": "upc", "FDB Case Pack": "case_pack",
        "Supplier Name": "supplier",
    }, "ABC/Cencora mailbox catalog")

    warnings: list[str] = []
    inv_qty: dict[str, float] = {}
    if inventory_bytes:
        try:
            idf = pd.read_csv(io.BytesIO(inventory_bytes), dtype=str)
            idf.columns = idf.columns.str.strip()
            if {"Material Number", "Quantity"}.issubset(idf.columns):
                idf = idf.apply(lambda c: c.str.strip() if c.dtype == "object" else c)
                idf["Quantity"] = pd.to_numeric(idf["Quantity"], errors="coerce").fillna(0)
                idf = idf.drop_duplicates(subset="Material Number", keep="first")
                inv_qty = dict(zip(idf["Material Number"], idf["Quantity"]))
            else:
                warnings.append(
                    "Cencora inventory file is missing Material Number/Quantity columns -- "
                    "quantities are 0."
                )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Cencora inventory file unreadable ({exc}) -- quantities are 0.")
    else:
        warnings.append("No Cencora inventory file found yet today -- quantities are 0.")

    rows, skipped = [], []
    for r in df.to_dict("records"):
        if (vo._clean(r.get("supplier")) or "").upper() == "FORD MEDICAL, LLC":
            continue
        uom = vo._clean(r["uom"])
        cost = pd.to_numeric(r["cost"], errors="coerce")
        upc = vo.norm_upc(r["upc"])
        desc = vo._clean(r["desc"])
        reason = None
        divisor = 1
        if uom == "EA":
            divisor = 1
        elif uom in ("CS", "PK"):
            divisor = vo.extract_case_qty_from_desc(desc)
            if divisor is None:
                reason = f"UoM={uom!r} but no case-quantity ('NxSIZE') pattern found in description"
        else:
            reason = f"UoM={uom!r} -- cost is per case/pack, no reliable per-each divisor"
        if reason is None:
            if upc is None:
                reason = "missing/unparseable UPC Barcode"
            elif not vo.is_valid_upc_a(upc):
                reason = "UPC Barcode isn't a valid UPC-A (likely an internal/warehouse code)"
            elif pd.isna(cost) or cost <= 0:
                reason = "missing or non-positive Current Acq Cost"
        if reason:
            skipped.append({"vendor": "Cencora", "item": vo._clean(r["item"]),
                            "description": desc, "reason": reason})
            continue
        item = vo._clean(r["item"])
        avail = inv_qty.get(item)
        rows.append({
            "vendor_item_id": item, "upc": upc, "cost": float(cost) / divisor,
            "qty_per_case": divisor,
            "avail_qty": float(avail) if avail is not None else None,
            "description": desc, "seen": stamp,
        })
    return rows, skipped, warnings


def _parse_diamond_mailbox(data: bytes, stamp: str) -> tuple[list[dict], list[dict]]:
    """FORDMED.CSV -- cp1252, 13 header fields vs 14 data fields per row
    (trailing comma), barcode from 12DGTUPC. Verified live 2026-10-23 against
    fbi-offer-upload's cached real same-day file. IDENTICAL layout to
    services.vendor_offers.load_diamond -- this mirrors that function's body
    exactly, just reading from in-memory bytes instead of a file path."""
    raw = pd.read_csv(io.BytesIO(data), dtype=str, encoding="cp1252",
                      skiprows=1, header=None, names=vo.DIAMOND_NAMES, index_col=False)
    df = vo.pick_columns(raw, {
        "UPC": "line_code", "DESCRIPTION": "desc", "CSPK": "case_pack",
        "QUANTITY": "qty", "PRICE": "cost", "12DGTUPC": "upc",
    }, "Diamond FORDMED.CSV (mailbox)")

    rows, skipped = [], []
    for r in df.to_dict("records"):
        upc = vo.norm_upc(r["upc"])
        cost = pd.to_numeric(r["cost"], errors="coerce")
        reason = None
        if upc is None:
            reason = "missing/unparseable 12DGTUPC"
        elif not vo.is_valid_upc_a(upc):
            reason = "12DGTUPC isn't a valid UPC-A (likely an internal/warehouse code)"
        elif pd.isna(cost) or cost <= 0:
            reason = "missing or non-positive PRICE"
        if reason:
            skipped.append({"vendor": "Diamond Wholesale", "item": vo._clean(r["line_code"]),
                            "description": vo._clean(r["desc"]), "reason": reason})
            continue
        rows.append({
            "vendor_item_id": vo._clean(r["line_code"]), "upc": upc, "cost": float(cost),
            "qty_per_case": vo.as_int(r["case_pack"]),
            "avail_qty": pd.to_numeric(r["qty"], errors="coerce"),
            "description": vo._clean(r["desc"]), "seen": stamp,
        })
    return rows, skipped


def _parse_bilo_mailbox(data: bytes, stamp: str) -> tuple[list[dict], list[dict]]:
    """BILO's daily emailed .xls (sender f.patruno@biloltd.net). NOT
    independently verified against a live file this session (BILO's email
    typically lands ~7pm) -- ported as-is from fbi-offer-upload's
    transform_bilo. NOTE this is a DIFFERENT column layout than
    services.vendor_offers.load_bilo's manual-upload format (that one has a
    "Qty per UOM" case-pack divisor; this one doesn't) -- confirmed real
    discrepancy between the two file variants, not an oversight. Flag if the
    first real sync errors on a missing column."""
    df = pd.read_excel(io.BytesIO(data), dtype=str, engine="xlrd")
    df = vo.pick_columns(df, {
        "UPC Code": "upc", "Quantity": "qty", "Item Price": "cost",
        "Vendor": "mfg", "Item No": "item", "Item Description": "desc",
    }, "BILO mailbox file")

    rows, skipped = [], []
    for r in df.to_dict("records"):
        upc = vo.norm_upc(r["upc"])
        cost = pd.to_numeric(r["cost"], errors="coerce")
        qty = pd.to_numeric(r["qty"], errors="coerce")
        reason = None
        if upc is None:
            reason = "missing/unparseable UPC Code"
        elif not vo.is_valid_upc_a(upc):
            reason = "UPC Code isn't a valid UPC-A (likely an internal/warehouse code)"
        elif pd.isna(cost) or cost <= 0:
            reason = "missing or non-positive Item Price"
        elif pd.isna(qty) or qty == 0:
            reason = "zero/missing Quantity"
        if reason:
            skipped.append({"vendor": "BILO", "item": vo._clean(r["item"]),
                            "description": vo._clean(r["desc"]), "reason": reason})
            continue
        rows.append({
            "vendor_item_id": vo._clean(r["item"]), "upc": upc, "cost": float(cost),
            "qty_per_case": None, "avail_qty": float(qty),
            "description": vo._clean(r["desc"]), "seen": stamp,
        })
    return rows, skipped


def _parse_qkd_mailbox(data: bytes, filename: str, stamp: str) -> tuple[list[dict], list[dict]]:
    """Quality King's daily emailed single-snapshot file (subject contains
    "Updated File - QKD"). Ported as-is from fbi-offer-upload's
    transform_qkd, EXCEPT for qty_per_case (see below). NOTE this is a
    DIFFERENT, simpler file than services.vendor_offers.load_quality_king's
    manual-upload format (that one is a stacked multi-snapshot historical
    file; this one is one plain daily snapshot) -- but it DOES carry the
    same UM/INNER/PACK columns the manual loader uses (confirmed live
    2026-10-27 against a real same-day file, FDM102626.xlsx -- the
    2026-10-23 note claiming this file lacked an INNER column was wrong,
    or the format has changed since).

    qty_per_case = INNER x PACK (not PACK alone), and cost is only trusted
    when UM == "EA" -- identical to load_quality_king's own logic (see that
    function's 2026-09-24 note: using PACK alone silently under-counted
    every case size by a factor of INNER)."""
    ext = os.path.splitext(filename)[1].lower()
    df = (pd.read_excel(io.BytesIO(data), dtype=str) if ext == ".xlsx"
          else pd.read_csv(io.BytesIO(data), dtype=str))
    df = vo.pick_columns(df, {
        "UPC": "upc", "QKD PRICE": "cost", "MFG NAME": "mfg", "UM": "uom",
        "INNER": "inner", "PACK": "pack",
        "ITEM": "item", "DESCRIPTION": "desc", "ONHAND": "qty",
    }, "QKD mailbox file")

    rows, skipped = [], []
    for r in df.to_dict("records"):
        upc = vo.norm_upc(r["upc"])
        cost = pd.to_numeric(r["cost"], errors="coerce")
        uom = vo._clean(r.get("uom"))
        reason = None
        if uom != "EA":
            reason = f"UoM={uom!r} not EA (repeated header row or non-each pricing)"
        elif upc is None:
            reason = "missing/unparseable UPC"
        elif not vo.is_valid_upc_a(upc):
            reason = "UPC isn't a valid UPC-A (likely an internal/warehouse code)"
        elif pd.isna(cost) or cost <= 0:
            reason = "missing or non-positive QKD PRICE"
        if reason:
            skipped.append({"vendor": "Quality King Distributors", "item": vo._clean(r["item"]),
                            "description": vo._clean(r["desc"]), "reason": reason})
            continue
        rows.append({
            "vendor_item_id": vo._clean(r["item"]), "upc": upc, "cost": float(cost),
            "qty_per_case": (vo.as_int(r.get("inner")) or 1) * (vo.as_int(r.get("pack")) or 1),
            "avail_qty": pd.to_numeric(r["qty"], errors="coerce"),
            "description": vo._clean(r["desc"]), "seen": stamp,
        })
    return rows, skipped


def _parse_victory_mailbox(data: bytes, stamp: str) -> tuple[list[dict], list[dict]]:
    """Victory DPA (vwgAll.xls). Verified live 2026-10-23 against
    fbi-offer-upload's cached real same-day file. No existing catalog-verifier
    loader -- manual upload never supported Victory at all. Prices are per
    case of `Pack` items; loaded per-each (cost = Price / Pack), matching the
    convention every other vendor's loader already uses (cost is always a
    real per-each price; qty_per_case separately records the case multiplier)."""
    df = pd.read_excel(io.BytesIO(data), dtype=str)
    df = vo.pick_columns(df, {
        "BTC": "item", "Item UPC": "upc", "Manufacturer": "mfg",
        "Item Description": "desc", "Pack": "pack", "Avail": "qty", "Price": "cost",
    }, "Victory mailbox file")

    rows, skipped = [], []
    for r in df.to_dict("records"):
        upc = vo.norm_upc(r["upc"])
        price = pd.to_numeric(r["cost"], errors="coerce")
        pack = pd.to_numeric(r["pack"], errors="coerce")
        unit_price = (price / pack) if (pack is not None and not pd.isna(pack) and pack > 0) else None
        reason = None
        if upc is None:
            reason = "missing/unparseable Item UPC"
        elif not vo.is_valid_upc_a(upc):
            reason = "Item UPC isn't a valid UPC-A (likely an internal/warehouse code)"
        elif unit_price is None or pd.isna(unit_price) or unit_price <= 0:
            reason = "missing/non-positive Price or Pack (can't derive per-each cost)"
        if reason:
            skipped.append({"vendor": "Victory Wholesale Grocers", "item": vo._clean(r["item"]),
                            "description": vo._clean(r["desc"]), "reason": reason})
            continue
        rows.append({
            "vendor_item_id": vo._clean(r["item"]), "upc": upc, "cost": float(unit_price),
            "qty_per_case": vo.as_int(r["pack"]),
            "avail_qty": pd.to_numeric(r["qty"], errors="coerce"),
            "description": vo._clean(r["desc"]), "seen": stamp,
        })
    return rows, skipped


# --------------------------------------------------------------------------
# Dedup state (settings kv table) -- mirrors fbi-offer-upload's processed.json
# --------------------------------------------------------------------------

def _content_hash(*blobs: bytes) -> str:
    h = hashlib.md5()
    for b in blobs:
        h.update(b)
    return h.hexdigest()


def _dedup_state() -> dict:
    raw = database.get_setting("vendor_offers_mailbox_sync_dedup")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}


def _save_dedup_state(state: dict) -> None:
    database.set_setting("vendor_offers_mailbox_sync_dedup", json.dumps(state))


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def _process_vendor(token: str, vname: str, posts: dict) -> dict:
    post = posts.get(vname)
    if not post:
        return {"vendor": vname, "status": "no matching email found"}

    files = _download_attachments(token, post["thread_id"], post["post_id"], post["extensions"])
    if not files:
        return {"vendor": vname, "status": "email matched but no usable attachment",
                "subject": post["subject"]}

    warnings: list[str] = []
    inventory_bytes = None
    if vname == "Cencora":
        inv_post = posts.get("Cencora_inventory")
        if inv_post:
            inv_files = _download_attachments(
                token, inv_post["thread_id"], inv_post["post_id"], inv_post["extensions"])
            if inv_files:
                inventory_bytes = inv_files[0][1]

    filename, data = files[0]
    dedup_blobs = (data, inventory_bytes) if inventory_bytes else (data,)
    content_hash = _content_hash(*dedup_blobs)

    state = _dedup_state()
    if state.get(vname) == content_hash:
        return {"vendor": vname, "status": "unchanged since last sync", "subject": post["subject"]}

    stamp = post["date"][:10] if post.get("date") else dt.date.today().isoformat()

    if vname == "Cencora":
        rows, skipped, w = _parse_cencora_mailbox(data, inventory_bytes, stamp)
        warnings += w
    elif vname == "Diamond Wholesale":
        rows, skipped = _parse_diamond_mailbox(data, stamp)
    elif vname == "BILO":
        rows, skipped = _parse_bilo_mailbox(data, stamp)
    elif vname == "Quality King Distributors":
        rows, skipped = _parse_qkd_mailbox(data, filename, stamp)
    elif vname == "Victory Wholesale Grocers":
        rows, skipped = _parse_victory_mailbox(data, stamp)
    else:
        raise ValueError(f"No mailbox parser for {vname!r}")

    # reset_avail=True: this is a trusted, complete snapshot of the vendor's
    # current catalog, so anything it doesn't mention should read 0 right
    # away rather than keep a stale quantity -- see module docstring.
    result = vo.merge_batches(vname, [(stamp, rows)], skipped, reset_avail=True)
    result["subject"] = post["subject"]
    result["received"] = post["date"]
    result["warnings"] = warnings

    state[vname] = content_hash
    _save_dedup_state(state)
    return result


def sync_vendor_from_mailbox(vname: str) -> dict:
    """Fetch + parse + merge ONE vendor's latest catalog email, if it's new
    since the last successful sync (dedup via content hash)."""
    if vname not in VENDOR_KEYS:
        raise ValueError(f"{vname!r} has no mailbox parser. Known: {VENDOR_KEYS}")
    vo.init_schema()
    token = _access_token()
    posts = fetch_latest_vendor_posts(token)
    return _process_vendor(token, vname, posts)


def sync_all_from_mailbox() -> dict:
    """Best-effort sync of every mapped vendor. One vendor's failure doesn't
    block the rest. One mailbox scan shared across all 5 (not re-fetched per
    vendor). Also clears avail_qty on any offer (any vendor, any source --
    file upload, DB sync, or this mailbox sync) that's gone stale
    (vo.STALE_AVAIL_QTY_DAYS) -- this used to piggyback on
    vendor_offers_db_sync's hourly scheduler, which is no longer started now
    that this sync replaces it for these 5 vendors; moved here so that
    system-wide maintenance isn't silently dropped."""
    vo.init_schema()
    token = _access_token()
    posts = fetch_latest_vendor_posts(token)

    results, errors = {}, {}
    for v in VENDOR_KEYS:
        try:
            results[v] = _process_vendor(token, v, posts)
        except Exception as exc:  # noqa: BLE001
            log.exception("[vendor_offers_mailbox_sync] %s failed", v)
            errors[v] = str(exc)[:300]

    try:
        stale = vo.clear_stale_available_qty()
    except Exception:  # noqa: BLE001
        log.exception("[vendor_offers_mailbox_sync] stale avail_qty cleanup failed")
        stale = {"cleared": 0, "cutoff": None}

    finished = dt.datetime.utcnow().isoformat()
    summary = {"finished_at": finished, "results": results, "errors": errors, "stale_avail_qty": stale}
    try:
        database.set_setting("vendor_offers_mailbox_sync_last", json.dumps(summary))
    except Exception:  # noqa: BLE001
        log.warning("[vendor_offers_mailbox_sync] failed to persist sync status", exc_info=True)

    # Dispatched on its own thread, including the import itself -- this
    # function is what the 30-min scheduler (and the manual "sync all"
    # endpoint) depend on for core data freshness, so nothing about the new
    # auto-alert feature is allowed to add so much as a millisecond to its
    # own execution time, whatever that feature ends up doing.
    def _dispatch_auto_alert() -> None:
        try:
            from services import vendor_offers_auto_alert as voaa
            voaa.process_sync_summary(summary)
        except Exception:  # noqa: BLE001
            log.exception("[vendor_offers_mailbox_sync] auto-alert dispatch failed")

    threading.Thread(target=_dispatch_auto_alert, name="vendor-offers-auto-alert-dispatch", daemon=True).start()

    return summary


def last_sync_status() -> dict | None:
    raw = database.get_setting("vendor_offers_mailbox_sync_last")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# 30-minute in-process scheduler (matches fbi-offer-upload's own cadence;
# mirrors services/vendor_offers_db_sync.py's pattern)
# --------------------------------------------------------------------------

_thread: threading.Thread | None = None
_stop = threading.Event()
_running = threading.Event()
INTERVAL_SECONDS = 1800


def _loop() -> None:
    while not _stop.is_set():
        _running.set()
        try:
            sync_all_from_mailbox()
        except Exception:  # noqa: BLE001
            log.exception("[vendor_offers_mailbox_sync] scheduled sync failed")
        finally:
            _running.clear()
        if _stop.wait(INTERVAL_SECONDS):
            return


def start_scheduler() -> None:
    """Start the 30-minute sync thread (idempotent). Called on app startup."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="vendor-offers-mailbox-sync", daemon=True)
    _thread.start()
    log.info("[vendor_offers_mailbox_sync] 30-minute scheduler started")


def stop_scheduler() -> None:
    _stop.set()


def scheduler_status() -> dict:
    last = last_sync_status()
    next_run = None
    if last and last.get("finished_at"):
        try:
            last_dt = dt.datetime.fromisoformat(last["finished_at"])
            next_run = (last_dt + dt.timedelta(seconds=INTERVAL_SECONDS)).isoformat()
        except Exception:  # noqa: BLE001
            pass
    return {
        "running_now": _running.is_set(),
        "scheduler_alive": bool(_thread and _thread.is_alive()),
        "interval_seconds": INTERVAL_SECONDS,
        "next_run": next_run,
        "last": last,
        "vendors": VENDOR_KEYS,
    }
