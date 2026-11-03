"""
Auto-alert: when the mailbox sync (services/vendor_offers_mailbox_sync.py)
pulls in a genuinely new offer file for one of the 5 synced vendors, check
whether that vendor is now "cheapest & unique" (the same win logic
vendor_offers_export.build_xlsx's focus_vendor column uses -- lowest cost
among every vendor carrying the UPC, ties included, sole-source counts too)
on any products, and if so build the Keepa-enriched template export scoped
to just those UPCs and email it, with the file attached, via
services/graph_mail.py send_mail() -- the exact same email mechanism the
Price Desk export's own "email it when done" option already uses (see
routers/vendor_offers.py's analytics_export completion path).

Originally built 2026-10-26 to post to Teams instead (see that date's
changelog entry in docs/features/vendor_offers.md for the Teams webhook
file-attachment investigation) -- switched to email 2026-10-27 per user
request ("revert all the changes of adding to teams. Instead send via
mail... the usual thing that worked when we used the price desk").
Recipients are fixed (see RECIPIENTS below), not configurable per-run like
the manual export's recipient field.

Scoped down with build_template_xlsx's own existing filters -- per user
request 2026-10-26, after the raw winning-UPC count turned out to be
thousands per vendor per day (effectively the vendor's whole catalog
position, not an actionable alert):
  - apply_filters=True: excludes DOG/Restricted ASINs and anything under
    the existing 50-units/month, <90% Amazon-BB-share "sellable & selling
    only" thresholds (services/vendor_offers_template_export.py).
  - only_available=True: excludes UPCs where the focus vendor itself has
    no/zero available qty right now.
  - Also only considers winning UPCs that already have an ASIN assigned
    (vo_asin_by_upc) -- a UPC with no ASIN can't be Keepa/eligibility
    enriched at all, so it would be filtered out by apply_filters anyway;
    skipping it upfront avoids wasting a Keepa batch slot on it.

Called from sync_all_from_mailbox() right before it returns -- so this runs
for both the 30-minute scheduler and the manual "sync all" endpoint, since
both go through that one function. Each vendor's work happens in its own
daemon thread (not inline) so a slow Keepa/SP-API enrichment pass never
delays the scheduler's own 30-minute cadence. The dispatch itself (this
module's own import, done lazily by the caller) also happens off the
scheduler's thread -- see vendor_offers_mailbox_sync.py's 2026-10-26
changelog entry for why: an early version of this hook, imported inline in
sync_all_from_mailbox() itself, coincided with a real live-server hang the
first time it was deployed (later root-caused to an unrelated startup
scheduler pile-up, not this module -- but the decoupling is kept as cheap
defense-in-depth regardless).

Inert (silent no-op) unless graph_mail.is_configured() (.env OUTLOOK_*) --
mirrors every other notification call site's "never break the real
feature" convention.
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import re
import threading
import time

from openpyxl import load_workbook

from services import database
from services import graph_mail
from services import job_persistence
from services import vendor_offers_export as vo_export
from services import vendor_offers_template_export as vo_tpl

log = logging.getLogger(__name__)

RECIPIENTS = ["raiann@fordmed.com", "AviS@fordmed.com", "DinaB@fordmed.com"]

# Restart survival (2026-11-03): an alert takes minutes to an hour (Keepa +
# SP-API per ASIN) in a daemon thread, so a laptop sleep/shutdown or server
# restart mid-build used to lose it silently (2026-10-30 night: Diamond +74,
# Victory +29, Cencora +1 new rows loaded, no alert ever sent). Each alert is
# now saved in background_jobs (job_persistence) while it runs and cleared only
# once it has finished -- sent, or nothing qualified. A pending alert is re-run
# after the next startup, delayed past the staggered startup schedulers.
RELAUNCH_DELAY_SECONDS = 720
MAX_ATTEMPTS = 3   # runs (failed or interrupted) before a pending alert is dropped

_state_lock = threading.Lock()
_running: dict[str, float] = {}   # vendor -> started_at
_rerun: set[str] = set()          # vendors whose newer sync arrived while an alert was running
_resume_pending: set[str] = set() # vendors with a relaunch timer that hasn't fired yet


def _job_id(vendor: str) -> str:
    return "vo-auto-alert-" + re.sub(r"[^a-z0-9]+", "-", vendor.lower()).strip("-")


def _asin_having_upcs(upcs: list[str]) -> set[str]:
    if not upcs:
        return set()
    with database._connect() as conn:
        placeholders = ",".join("?" * len(upcs))
        rows = conn.execute(
            f"SELECT DISTINCT upc FROM vo_asin_by_upc WHERE upc IN ({placeholders})", upcs
        ).fetchall()
    return {r[0] for r in rows}


def _row_count(blob: bytes) -> int:
    wb = load_workbook(io.BytesIO(blob), read_only=True)
    # The template has 4 sheets (Analytics/Order/Keepa/offer) and wb.active is
    # NOT Analytics once re-read -- counting wb.active always returned 0, so
    # the alert silently skipped every real run.
    ws = wb["Analytics"] if "Analytics" in wb.sheetnames else wb.active
    n = max(0, (ws.max_row or 1) - 1)   # minus the header row
    wb.close()
    return n


def _alert_for_vendor(vendor: str) -> bool:
    """Build + email one vendor's alert. True when finished (sent, or nothing
    qualified); False on a failure worth retrying after the next restart."""
    try:
        winning = vo_export.winning_upcs_for_vendor(vendor)
        have_asin = _asin_having_upcs(winning)
        upcs = [u for u in winning if u in have_asin]
        if not upcs:
            log.info("[vendor_offers_auto_alert] %s: no cheapest/unique/ASIN-having UPCs, skipping", vendor)
            return True
        blob = vo_tpl.build_template_xlsx(
            upcs=upcs, focus_vendor=vendor, live=True,
            apply_filters=True, only_available=True, populate_keepa=True,
        )
        count = _row_count(blob)
        if count <= 0:
            log.info("[vendor_offers_auto_alert] %s: 0 rows survived filters, skipping", vendor)
            return True
        filename = f"{vendor.replace(' ', '_')}_cheapest_unique_{dt.date.today().isoformat()}.xlsx"
        subject = f"Cheapest & unique opportunity — {vendor} ({count} product{'s' if count != 1 else ''})"
        body_html = (
            f"<p><b>{vendor}</b> is now the cheapest vendor on <b>{count}</b> "
            f"product{'s' if count != 1 else ''} (sole-source or price-winning), "
            f"filtered to in-stock, non-DOG/Restricted, ≥50 units/month sellers.</p>"
            f"<p>Attached: <b>{filename}</b>.</p>"
        )
        graph_mail.send_mail(RECIPIENTS, subject, body_html, attachments=[(filename, blob)])
        log.info("[vendor_offers_auto_alert] %s: emailed opportunity alert, %d rows", vendor, count)
        return True
    except Exception:
        log.exception("[vendor_offers_auto_alert] %s: alert failed", vendor)
        return False


def _start(vendor: str, attempt: int = 1) -> None:
    """Run one vendor's alert in a daemon thread, saved as pending until it finishes.
    A second sync for a vendor whose alert is still running queues ONE re-run
    (its winners were computed before the newer file landed) instead of a
    parallel duplicate."""
    with _state_lock:
        if vendor in _running:
            _rerun.add(vendor)
            return
        _running[vendor] = time.time()
    job_persistence.save_job(_job_id(vendor), "vo_auto_alert", {"vendor": vendor, "attempt": attempt})

    def run() -> None:
        ok = _alert_for_vendor(vendor)
        with _state_lock:
            _running.pop(vendor, None)
            again = vendor in _rerun
            _rerun.discard(vendor)
            if ok:   # a fresh sync's run already covers a not-yet-fired resume
                _resume_pending.discard(vendor)
        if ok or attempt >= MAX_ATTEMPTS:
            job_persistence.finish_job(_job_id(vendor))
            if not ok:
                log.error("[vendor_offers_auto_alert] %s: giving up after %d attempts", vendor, attempt)
        # else: left pending -> retried after the next restart (attempt + 1)
        if again:
            _start(vendor)

    threading.Thread(target=run, name=f"vendor-offers-auto-alert-{vendor}", daemon=True).start()


def _relaunch(params: dict) -> None:
    """Startup relauncher (job_persistence.resume_orphaned_jobs): re-save the
    pending row right away (resume clears it before relaunching), then run the
    alert after the staggered startup schedulers have done their first pass.
    The interrupted run counts as an attempt."""
    vendor = str(params.get("vendor") or "").strip()
    if not vendor or not graph_mail.is_configured():
        return
    prev = int(params.get("attempt") or 1)
    if prev >= MAX_ATTEMPTS:
        log.error("[vendor_offers_auto_alert] %s: dropped after %d interrupted/failed attempts", vendor, prev)
        return
    job_persistence.save_job(_job_id(vendor), "vo_auto_alert", {"vendor": vendor, "attempt": prev})
    with _state_lock:
        _resume_pending.add(vendor)
    timer = threading.Timer(RELAUNCH_DELAY_SECONDS, _resume, args=(vendor, prev + 1))
    timer.daemon = True
    timer.start()
    log.info("[vendor_offers_auto_alert] %s: interrupted alert re-runs in %ds (attempt %d)",
             vendor, RELAUNCH_DELAY_SECONDS, prev + 1)


def _resume(vendor: str, attempt: int) -> None:
    with _state_lock:
        if vendor not in _resume_pending:
            return          # a fresh sync's alert for this vendor already finished
        _resume_pending.discard(vendor)
        if vendor in _running:
            return          # a fresh sync's alert is running now and covers it
    _start(vendor, attempt)


job_persistence.register_relauncher("vo_auto_alert", _relaunch)


def list_jobs_for_tasks_panel() -> list[dict]:
    """Settings > Tasks Running: alerts being built right now."""
    with _state_lock:
        items = list(_running.items())
    return [{"feature": f"Vendor offer alert: {v}", "job_id": _job_id(v), "status": "running",
             "phase": "building + emailing the cheapest & unique export", "done": None, "total": None,
             "started_at": t, "completed_at": None} for v, t in items]


def process_sync_summary(summary: dict) -> None:
    """Fire an opportunity alert for every vendor whose mailbox sync actually
    merged new offer rows this run (summary["results"][v]["added"] > 0 -- the
    no-new-email/unchanged-since-last-sync status dicts have no "added" key at
    all, so .get(..., 0) naturally skips them)."""
    if not graph_mail.is_configured():
        return
    for vendor, result in (summary.get("results") or {}).items():
        if isinstance(result, dict) and result.get("added", 0) > 0:
            _start(vendor)
