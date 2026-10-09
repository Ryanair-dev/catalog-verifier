"""
PO Analytics automation — day-before-ETA trigger, driven entirely by Azure's
own Monday.com mirror (zero Monday API calls — see services/monday_eta.py).

Scope (2026-10-09, revised): the Inbound Shipments board tracks POs for the
WHOLE company, not just the ecommerce/Amazon side — so this automation only
ever acts on a user-curated WATCHLIST of PO numbers (`po_automation_watchlist`)
saved via the Automation Settings page, never the full board. Checked once a
day at 12:00 America/New_York (was hourly-over-everything in the first cut of
this feature) — a small, explicit list doesn't need hourly polling.

Trigger rule (confirmed with the user, 2026-10-09): fire once per watched PO
when its tracked ETA is TOMORROW (the golden rule — a day's notice) OR already
TODAY or earlier (covers a same-day ETA revision that skips the normal
day-before window, and is a safe catch-up for a cycle that was somehow
missed). A single rule — "current eta <= tomorrow, and we haven't already
sent for this PO" — covers both cases without needing to track *why* the eta
changed. Once sent for a PO, it is never re-sent, even if the ETA moves again
afterward.

No gating on Monday's own receiving status (it'll just say "Ordered" the
whole time pre-arrival, so it carries no signal for this trigger) beyond
excluding the two unambiguous terminal states (Received/Cancelled).

Each fire calls `services.analytics.po_analytics.gather(..., require_received
=False)` — the PO is used in whatever SellerCloud status it's actually in,
with "PO qty" switched to the ORDERED quantity (nothing has arrived yet) —
then emails the resulting workbook via services.graph_mail to whoever is
configured for the 'po_analytics' tool in `automation_recipients`.

`automation_recipients` is a small generic table (keyed by an arbitrary
`tool` string) so another automated tool can reuse the same settings-page
machinery later without a schema change — only 'po_analytics' is actually
wired to a real trigger today.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from zoneinfo import ZoneInfo

from services import database, graph_mail, monday_eta
from services.analytics import po_analytics

log = logging.getLogger(__name__)

_TZ = ZoneInfo("America/New_York")
TOOL_KEY = "po_analytics"

# How far back an already-elapsed ETA is still treated as "due" (a short
# catch-up window for a daily check genuinely missed — e.g. the server was
# down that day). Anything older than this is just not relevant to send now.
_DUE_LOOKBACK_DAYS = 3

# No positive status is required to fire (per the user: it'll just say
# "Ordered" pre-arrival, which carries no signal). But a PO that's already
# Received or Cancelled is categorically done — this automation is forward-
# looking, so those are excluded regardless of ETA. Everything else (Back
# Order, Invoice Uploaded, exception states like "FDA not released", etc.)
# still fires — flagged to the user as worth revisiting if an exception
# status turns out to be noisy in practice.
_EXCLUDE_STATUSES = {"Received", "Cancelled"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS po_eta_automation (
    po_number      INTEGER PRIMARY KEY,
    first_seen_eta TEXT,
    last_eta       TEXT,
    status         TEXT,
    sent_at        TEXT,
    sent_for_eta   TEXT,
    last_error     TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    updated_at     TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS automation_recipients (
    tool            TEXT PRIMARY KEY,
    recipients_json TEXT DEFAULT '[]',
    enabled         INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS po_automation_watchlist (
    po_number  INTEGER PRIMARY KEY,
    note       TEXT DEFAULT '',
    added_at   TEXT DEFAULT (datetime('now'))
);
"""


def init_schema() -> None:
    with database._LOCK, database._connect() as conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------- #
# Watchlist (the user-curated "these are my ecommerce POs" list)
# --------------------------------------------------------------------------- #

def get_watchlist() -> list[dict]:
    """Every watched PO, with its last-known tracking info if any (so the
    Settings page can show the current ETA/status/sent-state inline)."""
    with database._connect() as conn:
        rows = conn.execute(
            """
            SELECT w.po_number, w.note, w.added_at,
                   t.last_eta, t.status, t.sent_at, t.last_error
            FROM po_automation_watchlist w
            LEFT JOIN po_eta_automation t ON t.po_number = w.po_number
            ORDER BY w.added_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def add_to_watchlist(po_numbers: list[int], note: str = "") -> int:
    """Adds PO numbers to the watchlist (no-op for ones already present).
    Returns how many were newly added."""
    added = 0
    with database._LOCK, database._connect() as conn:
        for po in po_numbers:
            cur = conn.execute(
                "INSERT INTO po_automation_watchlist (po_number, note) VALUES (?, ?) "
                "ON CONFLICT(po_number) DO NOTHING",
                (po, note),
            )
            added += cur.rowcount
    return added


def remove_from_watchlist(po_number: int) -> bool:
    with database._LOCK, database._connect() as conn:
        cur = conn.execute("DELETE FROM po_automation_watchlist WHERE po_number=?", (po_number,))
        return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Recipient settings (read by the Settings page / routers)
# --------------------------------------------------------------------------- #

def get_recipients(tool: str) -> dict:
    with database._connect() as conn:
        row = conn.execute(
            "SELECT recipients_json, enabled FROM automation_recipients WHERE tool=?",
            (tool,),
        ).fetchone()
    if not row:
        return {"tool": tool, "recipients": [], "enabled": False}
    return {
        "tool": tool,
        "recipients": json.loads(row["recipients_json"] or "[]"),
        "enabled": bool(row["enabled"]),
    }


def set_recipients(tool: str, recipients: list[str], enabled: bool) -> None:
    with database._LOCK, database._connect() as conn:
        conn.execute(
            """
            INSERT INTO automation_recipients (tool, recipients_json, enabled, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(tool) DO UPDATE SET
                recipients_json = excluded.recipients_json,
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (tool, json.dumps(list(recipients)), 1 if enabled else 0),
        )
    # No backlog-seeding step here (unlike the first cut of this feature):
    # since the watchlist is a small, user-curated list, there's no flood
    # risk — if someone adds a PO and turns this on, they clearly want it
    # checked, including right away if it's already due.


# --------------------------------------------------------------------------- #
# Trigger check
# --------------------------------------------------------------------------- #

def _today_et() -> dt.date:
    return dt.datetime.now(_TZ).date()


def _upsert_tracking(items: list[dict]) -> None:
    with database._LOCK, database._connect() as conn:
        for it in items:
            po = it.get("po_number")
            if not po:
                continue
            eta = it.get("eta_to_warehouse")
            status = it.get("status")
            conn.execute(
                """
                INSERT INTO po_eta_automation (po_number, first_seen_eta, last_eta, status, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(po_number) DO UPDATE SET
                    last_eta = excluded.last_eta,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (po, eta, eta, status),
            )


def _compute_due(conn) -> list[int]:
    """Watched PO numbers currently due per the trigger rule (eta within the
    lookback window through tomorrow, not already sent, not Received/
    Cancelled). JOINed against the watchlist directly — so a PO can never
    fire just because a stale tracking row happens to linger from before it
    was added to (or after it's removed from) the watchlist."""
    today = _today_et()
    tomorrow = today + dt.timedelta(days=1)
    earliest_relevant = today - dt.timedelta(days=_DUE_LOOKBACK_DAYS)
    rows = conn.execute(
        """
        SELECT t.po_number, t.last_eta, t.status
        FROM po_eta_automation t
        JOIN po_automation_watchlist w ON w.po_number = t.po_number
        WHERE t.sent_at IS NULL AND t.last_eta IS NOT NULL
        """
    ).fetchall()
    due: list[int] = []
    for r in rows:
        if (r["status"] or "") in _EXCLUDE_STATUSES:
            continue
        try:
            eta = dt.date.fromisoformat(r["last_eta"])
        except ValueError:
            continue
        if earliest_relevant <= eta <= tomorrow:
            due.append(r["po_number"])
    return due


def _run_and_send_one(po_number: int) -> str:
    """Runs PO Analytics for one PO (ordered-qty mode) and emails the result.
    Returns the filename sent. Raises on any failure — caller records it."""
    gathered = po_analytics.gather([po_number], require_received=False)
    blob = po_analytics.enrich_and_build(gathered)
    filename = po_analytics.po_analytics_filename(
        gathered.get("resolved_po_numbers") or [po_number], gathered["brand_label"]
    )
    recip = get_recipients(TOOL_KEY)
    subject = f"PO Analytics (automated) — {filename.rsplit('.', 1)[0]}"
    body = (
        f"<p>Automated PO Analytics for PO <b>{po_number}</b>, generated the day "
        f"before its tracked ETA on the Inbound Shipments board.</p>"
        f"<p>Figures in the attached report are based on <b>ordered</b> quantity — "
        f"nothing has been received yet.</p>"
    )
    graph_mail.send_mail(recip["recipients"], subject, body, attachments=[(filename, blob)])
    return filename


def check_and_trigger() -> dict:
    """One check cycle: pull the live Monday mirror, update tracking for
    WATCHED POs only, and run+email PO Analytics for every watched PO that
    just became due. Best-effort per PO — one PO's failure (SellerCloud
    error, bad recipient, etc.) is recorded on that PO's own row and does not
    block the others. Returns a summary."""
    watch = {w["po_number"] for w in get_watchlist()}
    summary = {"checked": 0, "due": 0, "sent": 0, "errors": []}
    if not watch:
        return summary  # nothing saved to watch yet — don't touch Azure at all

    items = monday_eta.fetch_inbound_items()
    relevant = [it for it in items if it.get("po_number") in watch]
    _upsert_tracking(relevant)
    summary["checked"] = len(relevant)

    recip = get_recipients(TOOL_KEY)
    if not recip["enabled"] or not recip["recipients"]:
        return summary  # nothing to send to — don't bother running analytics

    with database._connect() as conn:
        due = _compute_due(conn)

    summary["due"] = len(due)
    for po in due:
        try:
            _run_and_send_one(po)
            summary["sent"] += 1
            with database._LOCK, database._connect() as conn:
                conn.execute(
                    "UPDATE po_eta_automation SET sent_at=datetime('now'), "
                    "sent_for_eta=last_eta, last_error=NULL WHERE po_number=?",
                    (po,),
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("[po_automation] PO %s failed", po)
            summary["errors"].append({"po": po, "error": str(exc)[:300]})
            with database._LOCK, database._connect() as conn:
                conn.execute(
                    "UPDATE po_eta_automation SET last_error=? WHERE po_number=?",
                    (str(exc)[:500], po),
                )
    return summary


# --------------------------------------------------------------------------- #
# Daily (12:00 America/New_York) scheduler
# --------------------------------------------------------------------------- #

_thread: threading.Thread | None = None
_stop = threading.Event()
_running = threading.Event()


def next_run_time(now: dt.datetime | None = None) -> dt.datetime:
    """Next 12:00 America/New_York at or after `now`."""
    now = now or dt.datetime.now(_TZ)
    target = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return target


def _loop() -> None:
    while not _stop.is_set():
        target = next_run_time()
        log.info("[po_automation] next scheduled check: %s", target.isoformat())
        while not _stop.is_set():
            remaining = (target - dt.datetime.now(_TZ)).total_seconds()
            if remaining <= 0:
                break
            if _stop.wait(min(remaining, 300)):   # re-check every 5 min (DST/clock)
                return
        if _stop.is_set():
            return
        try:
            _running.set()
            summary = check_and_trigger()
            if summary["sent"] or summary["errors"]:
                log.info("[po_automation] check cycle: %s", summary)
        except Exception:  # noqa: BLE001
            log.exception("[po_automation] check cycle failed")
        finally:
            _running.clear()
        _stop.wait(120)   # step past noon before recomputing the next run


def start_scheduler() -> None:
    """Start the daily (12:00 ET) check thread (idempotent). Called on app startup."""
    global _thread
    if _thread and _thread.is_alive():
        return
    init_schema()
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="po-eta-automation", daemon=True)
    _thread.start()
    log.info("[po_automation] daily scheduler started (12:00 America/New_York)")


def stop_scheduler() -> None:
    _stop.set()


def scheduler_status() -> dict:
    with database._connect() as conn:
        tracked = conn.execute("SELECT COUNT(*) FROM po_eta_automation").fetchone()[0]
        sent = conn.execute("SELECT COUNT(*) FROM po_eta_automation WHERE sent_at IS NOT NULL").fetchone()[0]
        watched = conn.execute("SELECT COUNT(*) FROM po_automation_watchlist").fetchone()[0]
    return {
        "running_now": _running.is_set(),
        "scheduler_alive": bool(_thread and _thread.is_alive()),
        "next_run": next_run_time().isoformat(),
        "timezone": "America/New_York",
        "watched_pos": watched,
        "tracked_pos": tracked,
        "sent_pos": sent,
    }
