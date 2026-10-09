"""
PO Analytics automation — day-before-ETA trigger, driven entirely by Azure's
own Monday.com mirror (zero Monday API calls — see services/monday_eta.py).

Trigger rule (confirmed with the user, 2026-10-09): fire once per PO when its
tracked ETA is TOMORROW (the golden rule — a day's notice) OR already TODAY
or earlier (covers a same-day ETA revision that skips the normal day-before
window, and is a safe catch-up for a cycle that was somehow missed). A single
rule — "current eta <= tomorrow, and we haven't already sent for this PO" —
covers both cases without needing to track *why* the eta changed. Once sent
for a PO, it is never re-sent, even if the ETA moves again afterward.

No gating on Monday's own receiving status (it'll just say "Ordered" the
whole time pre-arrival, so it carries no signal for this trigger).

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
# catch-up window for a cycle genuinely missed — e.g. the server was down).
# WITHOUT this bound, the very first time recipients get enabled, every PO
# with an ETA anywhere in the board's history (some are a year+ old, long
# since resolved) would be treated as "due" all at once — a flood, not a
# catch-up. Anything older than this is just not relevant to send now.
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
"""


def init_schema() -> None:
    with database._LOCK, database._connect() as conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------- #
# Recipient settings (read by the Settings page / routers/po_automation.py)
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
        prev = conn.execute(
            "SELECT enabled FROM automation_recipients WHERE tool=?", (tool,)
        ).fetchone()
        was_enabled = bool(prev["enabled"]) if prev else False
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
    # First-time activation: seed the CURRENT backlog as already-handled so
    # turning the feature on doesn't fire a one-time flood of reports for
    # every PO that happens to already be in the due window at that moment —
    # only newly-due POs from this point forward trigger a real run+email.
    if tool == TOOL_KEY and enabled and not was_enabled:
        _seed_backlog_as_sent()


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
    """PO numbers currently due per the trigger rule (eta within the lookback
    window through tomorrow, not already sent, not Received/Cancelled)."""
    today = _today_et()
    tomorrow = today + dt.timedelta(days=1)
    earliest_relevant = today - dt.timedelta(days=_DUE_LOOKBACK_DAYS)
    rows = conn.execute(
        "SELECT po_number, last_eta, status FROM po_eta_automation "
        "WHERE sent_at IS NULL AND last_eta IS NOT NULL"
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


def _seed_backlog_as_sent() -> None:
    """Called once, the moment automation is first turned on: marks every PO
    that's ALREADY due at that moment as handled (without actually running or
    emailing it) so activation doesn't fire a one-time flood of reports for
    whatever happens to be in the due window right then. Only POs that become
    due AFTER this point trigger a real run+email."""
    with database._LOCK, database._connect() as conn:
        due = _compute_due(conn)
        for po in due:
            conn.execute(
                "UPDATE po_eta_automation SET sent_at=datetime('now'), sent_for_eta=last_eta, "
                "last_error='seeded on activation — not actually run/emailed' WHERE po_number=?",
                (po,),
            )
    log.info("[po_automation] activation: seeded %d already-due PO(s) as handled (no backlog flood)", len(due))


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
    """One check cycle: pull the live Monday mirror, update tracking, and
    run+email PO Analytics for every PO that just became due. Best-effort per
    PO — one PO's failure (SellerCloud error, bad recipient, etc.) is recorded
    on that PO's own row and does not block the others. Returns a summary."""
    items = monday_eta.fetch_inbound_items()
    _upsert_tracking(items)

    summary = {"checked": len(items), "due": 0, "sent": 0, "errors": []}

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
# Hourly scheduler
# --------------------------------------------------------------------------- #

_thread: threading.Thread | None = None
_stop = threading.Event()
_running = threading.Event()
INTERVAL_SECONDS = 3600


def _loop() -> None:
    while not _stop.is_set():
        _running.set()
        try:
            summary = check_and_trigger()
            if summary["sent"] or summary["errors"]:
                log.info("[po_automation] check cycle: %s", summary)
        except Exception:  # noqa: BLE001
            log.exception("[po_automation] check cycle failed")
        finally:
            _running.clear()
        if _stop.wait(INTERVAL_SECONDS):
            return


def start_scheduler() -> None:
    """Start the hourly ETA-check thread (idempotent). Called on app startup."""
    global _thread
    if _thread and _thread.is_alive():
        return
    init_schema()
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="po-eta-automation", daemon=True)
    _thread.start()
    log.info("[po_automation] hourly ETA-check scheduler started")


def stop_scheduler() -> None:
    _stop.set()


def scheduler_status() -> dict:
    with database._connect() as conn:
        tracked = conn.execute("SELECT COUNT(*) FROM po_eta_automation").fetchone()[0]
        sent = conn.execute("SELECT COUNT(*) FROM po_eta_automation WHERE sent_at IS NOT NULL").fetchone()[0]
    return {
        "running_now": _running.is_set(),
        "scheduler_alive": bool(_thread and _thread.is_alive()),
        "interval_seconds": INTERVAL_SECONDS,
        "tracked_pos": tracked,
        "sent_pos": sent,
    }
