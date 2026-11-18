"""
Live Inbound-Shipments ETA data, read from Azure's own Monday.com mirror
(`monday` schema) -- NOT the Monday API. Confirmed live (2026-10-09): Azure
SQL (`fbidb`) carries a same-day-fresh, complete mirror of every board/item/
column_value on this account's Monday.com (a Fivetran-style sync -- there's
an adjacent `fivetran_metadata` schema), so reading it costs zero Monday API
tokens, which is the whole point (the team's Monday API budget is limited).

Board 581141334 "Inbound Shipments" is the one that actually tracks ETAs,
splits, and receiving status for every PO placed -- confirmed as the real
live board (not "Archive 4", which looked right by name but stopped updating
in mid-July) by checking actual item activity directly, both via the live
Monday API and via this same Azure mirror's own `updated_at`.

IMPORTANT: an item's Monday `name` is NOT a reliable PO number. Two items on
this board were both literally named "51076" even though one of them was the
real split's own tracking item -- its PO link field actually said "51320".
The PO number MUST be read from the `link` column (parsed value), never
`items.name`.
"""
from __future__ import annotations

import logging

from services import azure_sql

log = logging.getLogger(__name__)

INBOUND_BOARD_ID = 581141334

# Azure's mirror stores column_id as the composite string
# "board_id:<id>|column_id:<name>" -- confirmed live. Using the exact strings
# (not a LIKE scan) keeps this index-friendly. vendor/goods/amount added
# 2026-10-16 for the Awaiting Analysis panel's hover detail (per-column
# titles confirmed live via monday.columns: "Vendor" is a dropdown column --
# its readable label lives in column_values.text, same place a plain text
# column's value does, verified against real rows). purchaser added 2026-10-19
# for the PO P&L report's "assigned by <person>" filter -- "Purchaser"
# (purchaser3) is a Monday PEOPLE column (can hold several names at once,
# e.g. "Mason Bennet, Avi Schoemann, Victoria Romanoff"), whose joined names
# land in column_values.text exactly like any other column -- confirmed live.
_FIELDS = {
    "po_number": "link",
    "eta_to_warehouse": "eta_to_warehouse",
    "status": "received9",
    "actual_arrived_date": "actual_arrived_date",
    "vendor": "dropdown_mkxpj7b",
    "goods": "goods",
    "amount": "amount",
    "purchaser": "purchaser3",
}


def _col_id(name: str) -> str:
    return f"board_id:{INBOUND_BOARD_ID}|column_id:{name}"


def fetch_inbound_items() -> list[dict]:
    """One row per active board item:
      {item_id, po_number (int|None), eta_to_warehouse (str 'YYYY-MM-DD'|None),
       status (str|None), actual_arrived_date (str|None), vendor (str|None),
       goods (str|None), amount (str|None), purchaser (str|None), item_updated_at}.

    Best-effort -- an Azure failure here must never crash a caller's scheduler
    loop; returns [] on any connection/query error.
    """
    if not azure_sql.is_configured():
        return []
    try:
        conn = azure_sql._connect()
    except Exception:  # noqa: BLE001
        log.exception("[monday_eta] Azure connection failed")
        return []
    try:
        cur = conn.cursor()
        (po_col, eta_col, status_col, arrived_col, vendor_col, goods_col, amount_col,
         purchaser_col) = (
            _col_id(_FIELDS["po_number"]), _col_id(_FIELDS["eta_to_warehouse"]),
            _col_id(_FIELDS["status"]), _col_id(_FIELDS["actual_arrived_date"]),
            _col_id(_FIELDS["vendor"]), _col_id(_FIELDS["goods"]), _col_id(_FIELDS["amount"]),
            _col_id(_FIELDS["purchaser"]),
        )
        cur.execute(
            """
            SELECT i.id, i.updated_at,
                   MAX(CASE WHEN cv.column_id = %s THEN cv.link_text_value END) AS po_number,
                   MAX(CASE WHEN cv.column_id = %s THEN cv.date_value END)      AS eta_to_warehouse,
                   MAX(CASE WHEN cv.column_id = %s THEN cv.text END)            AS status,
                   MAX(CASE WHEN cv.column_id = %s THEN cv.date_value END)      AS actual_arrived_date,
                   MAX(CASE WHEN cv.column_id = %s THEN cv.text END)            AS vendor,
                   MAX(CASE WHEN cv.column_id = %s THEN cv.text END)            AS goods,
                   MAX(CASE WHEN cv.column_id = %s THEN cv.text END)            AS amount,
                   MAX(CASE WHEN cv.column_id = %s THEN cv.text END)            AS purchaser
            FROM monday.items i
            JOIN monday.column_values cv ON cv.item_id = i.id
            WHERE i.board_id = %s AND i.state = 'active'
              AND cv.column_id IN (%s, %s, %s, %s, %s, %s, %s, %s)
            GROUP BY i.id, i.updated_at
            """,
            (po_col, eta_col, status_col, arrived_col, vendor_col, goods_col, amount_col,
             purchaser_col, INBOUND_BOARD_ID,
             po_col, eta_col, status_col, arrived_col, vendor_col, goods_col, amount_col,
             purchaser_col),
        )
        out: list[dict] = []
        for r in cur.fetchall():
            po_raw = (r[2] or "").strip()
            po_num = int(po_raw) if po_raw.isdigit() else None
            out.append({
                "item_id": r[0],
                "item_updated_at": r[1],
                "po_number": po_num,
                "eta_to_warehouse": r[3],
                "status": r[4],
                "actual_arrived_date": r[5],
                "vendor": (r[6] or "").strip() or None,
                "goods": (r[7] or "").strip() or None,
                "amount": (r[8] or "").strip() or None,
                "purchaser": (r[9] or "").strip() or None,
            })
        return out
    except Exception:  # noqa: BLE001
        log.exception("[monday_eta] query failed")
        return []
    finally:
        conn.close()


def pos_for_purchaser(name: str) -> set[int]:
    """PO numbers for every CURRENT (live, non-archived) Inbound Shipments
    board item whose Purchaser people-column includes `name` (substring,
    case-insensitive -- the column often holds several names at once, e.g.
    "Mason Bennet, Avi Schoemann, Victoria Romanoff", and this counts as a
    match on any of them, not just a sole assignee).

    KNOWN GAP: this only sees items on the LIVE board right now. Older POs
    that have since been archived off this board (the team has done this
    before -- see the module docstring's "Archive 4" note) won't show up
    here even if they're still sitting in the cash-cycle data. Flagged, not
    silently papered over -- a PO P&L dashboard built on this will undercount
    if archived-board POs are expected to still appear."""
    # Several purchasers, comma-separated ("Avi Schoemann, Victoria Romanoff")
    # -> union of their POs (2026-10-30, PO P&L scope = Avi + Victoria).
    names = [n.strip().lower() for n in (name or "").split(",") if n.strip()]
    if not names or not azure_sql.is_configured():
        return set()
    # Only the PO + Purchaser columns, filtered in SQL (2026-10-30): pivoting
    # all 8 columns of every board item via fetch_inbound_items() took ~5.6s
    # per PO P&L load; this returns the identical set (438 POs for Avi,
    # checked live) in ~0.3s.
    try:
        conn = azure_sql._connect()
    except Exception:  # noqa: BLE001
        log.exception("[monday_eta] Azure connection failed")
        return set()
    try:
        cur = conn.cursor()
        likes = " OR ".join(["LOWER(pu.text) LIKE %s"] * len(names))
        cur.execute(
            f"""
            SELECT po.link_text_value
            FROM monday.items i
            JOIN monday.column_values po ON po.item_id = i.id AND po.column_id = %s
            JOIN monday.column_values pu ON pu.item_id = i.id AND pu.column_id = %s
            WHERE i.board_id = %s AND i.state = 'active' AND ({likes})
            """,
            (_col_id(_FIELDS["po_number"]), _col_id(_FIELDS["purchaser"]),
             INBOUND_BOARD_ID, *[f"%{n}%" for n in names]),
        )
        return {int(r[0].strip()) for r in cur.fetchall() if r[0] and r[0].strip().isdigit()}
    except Exception:  # noqa: BLE001
        log.exception("[monday_eta] purchaser query failed")
        return set()
    finally:
        conn.close()
