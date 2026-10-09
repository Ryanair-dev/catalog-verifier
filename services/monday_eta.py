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
# (not a LIKE scan) keeps this index-friendly.
_FIELDS = {
    "po_number": "link",
    "eta_to_warehouse": "eta_to_warehouse",
    "status": "received9",
    "actual_arrived_date": "actual_arrived_date",
}


def _col_id(name: str) -> str:
    return f"board_id:{INBOUND_BOARD_ID}|column_id:{name}"


def fetch_inbound_items() -> list[dict]:
    """One row per active board item:
      {item_id, po_number (int|None), eta_to_warehouse (str 'YYYY-MM-DD'|None),
       status (str|None), actual_arrived_date (str|None), item_updated_at}.

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
        po_col, eta_col, status_col, arrived_col = (
            _col_id(_FIELDS["po_number"]), _col_id(_FIELDS["eta_to_warehouse"]),
            _col_id(_FIELDS["status"]), _col_id(_FIELDS["actual_arrived_date"]),
        )
        cur.execute(
            """
            SELECT i.id, i.updated_at,
                   MAX(CASE WHEN cv.column_id = %s THEN cv.link_text_value END) AS po_number,
                   MAX(CASE WHEN cv.column_id = %s THEN cv.date_value END)      AS eta_to_warehouse,
                   MAX(CASE WHEN cv.column_id = %s THEN cv.text END)            AS status,
                   MAX(CASE WHEN cv.column_id = %s THEN cv.date_value END)      AS actual_arrived_date
            FROM monday.items i
            JOIN monday.column_values cv ON cv.item_id = i.id
            WHERE i.board_id = %s AND i.state = 'active'
              AND cv.column_id IN (%s, %s, %s, %s)
            GROUP BY i.id, i.updated_at
            """,
            (po_col, eta_col, status_col, arrived_col,
             INBOUND_BOARD_ID,
             po_col, eta_col, status_col, arrived_col),
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
            })
        return out
    except Exception:  # noqa: BLE001
        log.exception("[monday_eta] query failed")
        return []
    finally:
        conn.close()
