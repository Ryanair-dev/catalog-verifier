"""
Settings endpoints.

Currently exposes the two adjustable confidence thresholds
(``verified`` boundary and ``review`` boundary). A third implicit boundary —
the 0% floor — is static.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services import database, po_automation

router = APIRouter()

# Which tools have an automated-email trigger today, and the label shown on
# the standalone Settings page. Add a new entry here as more automations are
# built — the recipient CRUD below is generic per `tool`, no schema change
# needed for a future tool.
AUTOMATION_TOOLS = {
    "po_analytics": "PO Analytics (day-before-ETA report)",
}


class Thresholds(BaseModel):
    verified: float
    review: float


@router.get("/settings/thresholds")
async def get_thresholds() -> dict:
    return database.get_thresholds()


@router.post("/settings/thresholds")
async def set_thresholds(body: Thresholds) -> dict:
    if not (0 <= body.review <= body.verified <= 100):
        raise HTTPException(
            status_code=400,
            detail="Thresholds must satisfy 0 ≤ review ≤ verified ≤ 100",
        )
    database.set_thresholds(body.verified, body.review)
    return database.get_thresholds()


class PrepFee(BaseModel):
    prep_out_fee: float


@router.get("/settings/prep-out-fee")
async def get_prep_out_fee() -> dict:
    return {"prep_out_fee": float(database.get_setting("prep_out_fee", "0.25"))}


@router.post("/settings/prep-out-fee")
async def set_prep_out_fee(body: PrepFee) -> dict:
    if body.prep_out_fee < 0:
        raise HTTPException(status_code=400, detail="Prep & Out fee must be ≥ 0.")
    database.set_setting("prep_out_fee", str(body.prep_out_fee))
    return {"prep_out_fee": float(database.get_setting("prep_out_fee", "0.25"))}


class AutomationRecipients(BaseModel):
    recipients: list[str]
    enabled: bool


@router.get("/settings/automation")
async def list_automation_settings() -> dict:
    """All tools with an automated-email trigger, for the standalone Settings
    page. These are GLOBAL (shared) settings today — there's no per-user login
    in this app yet; per-user settings are planned for when Microsoft Entra ID
    is added."""
    return {
        "tools": [
            {**po_automation.get_recipients(tool), "label": label}
            for tool, label in AUTOMATION_TOOLS.items()
        ]
    }


@router.post("/settings/automation/{tool}")
async def set_automation_settings(tool: str, body: AutomationRecipients) -> dict:
    if tool not in AUTOMATION_TOOLS:
        raise HTTPException(status_code=404, detail=f"Unknown automation tool '{tool}'.")
    cleaned = [e.strip() for e in body.recipients if e.strip()]
    for e in cleaned:
        if "@" not in e or " " in e:
            raise HTTPException(status_code=400, detail=f"'{e}' doesn't look like a valid email address.")
    po_automation.set_recipients(tool, cleaned, body.enabled)
    return {**po_automation.get_recipients(tool), "label": AUTOMATION_TOOLS[tool]}


# ── Tasks Running tab (2026-10-27) ──────────────────────────────────────────
# Aggregates every background-work tracker in the app into one place --
# there was previously no single endpoint (or UI) that showed "what's
# currently running" across features. Three distinct kinds of state source,
# since none of them share a schema:
#   1. In-memory "start -> poll -> done" job dicts (one per feature router) --
#      each exposes its own list_jobs_for_tasks_panel().
#   2. DB-row "run" tables (Offer Analysis, Brand Analytics, Health Check) --
#      queried directly here; the most recent N rows per table cover both
#      a currently-running row and recent history in one query.
#   3. Recurring background schedulers (mailbox sync, PO automation, Azure
#      SQL refresh, MPN check sync, SellerCloud catalog index) -- each
#      exposes its own scheduler_status(). health_check's scheduler is
#      covered by its runs table (#2), not listed again here.
_RECENT_RUNS_LIMIT = 5


def _recent_runs(table: str, feature: str, order_col: str = "updated_at") -> list[dict]:
    try:
        with database._connect() as conn:
            rows = conn.execute(
                f"""SELECT id, name, status, progress_phase, progress_done, progress_total,
                           created_at, updated_at
                    FROM {table} ORDER BY {order_col} DESC LIMIT ?""",
                (_RECENT_RUNS_LIMIT,),
            ).fetchall()
        return [
            {
                "feature": feature, "run_id": r[0], "name": r[1], "status": r[2],
                "phase": r[3], "done": r[4], "total": r[5],
                "created_at": r[6], "updated_at": r[7],
            }
            for r in rows
        ]
    except Exception:  # noqa: BLE001
        return []


def _recent_health_check_runs() -> list[dict]:
    try:
        with database._connect() as conn:
            rows = conn.execute(
                """SELECT run_id, started_at, finished_at, trigger, total, dog,
                          can_sell, needs_approval, restricted, errors
                   FROM health_check_runs ORDER BY started_at DESC LIMIT ?""",
                (_RECENT_RUNS_LIMIT,),
            ).fetchall()
        return [
            {
                "feature": "Pair Library Health Check", "run_id": r[0],
                "status": "running" if r[2] is None else "complete",
                "started_at": r[1], "finished_at": r[2], "trigger": r[3],
                "total": r[4], "dog": r[5], "can_sell": r[6],
                "needs_approval": r[7], "restricted": r[8], "errors": r[9],
            }
            for r in rows
        ]
    except Exception:  # noqa: BLE001
        return []


@router.get("/settings/tasks-running")
async def tasks_running() -> dict:
    from routers import eligibility, generic_check, pairs, po_analytics, seller_analytics, storage_fees
    from routers import vendor_offers as vo_router
    from services import azure_sql, health_check, mpn_check_sync
    from services import vendor_offers_mailbox_sync as vo_mail_sync
    from services.sellercloud import catalog_index as sc_catalog_index

    jobs: list[dict] = []
    for mod in (eligibility, storage_fees, generic_check, po_analytics, seller_analytics, pairs, vo_router):
        try:
            jobs.extend(mod.list_jobs_for_tasks_panel())
        except Exception:  # noqa: BLE001
            pass

    runs: list[dict] = []
    runs.extend(_recent_runs("analytics_runs", "Offer Analysis"))
    runs.extend(_recent_runs("brand_analytics_runs", "Brand Analytics"))
    runs.extend(_recent_health_check_runs())

    schedulers: list[dict] = []
    for feature, mod in (
        ("Vendor Offers: Mailbox Sync", vo_mail_sync),
        ("PO Analytics: Day-Before-ETA Automation", po_automation),
        ("SellerCloud Catalog (Azure SQL)", azure_sql),
        ("MPN Check: Local Mirror Sync", mpn_check_sync),
        ("Create SKUs: SellerCloud Catalog Snapshot", sc_catalog_index),
        ("Pair Library Health Check (scheduler)", health_check),
    ):
        try:
            schedulers.append({"feature": feature, **mod.scheduler_status()})
        except Exception:  # noqa: BLE001
            pass

    return {"jobs": jobs, "runs": runs, "schedulers": schedulers}
    return {**po_automation.get_recipients(tool), "label": AUTOMATION_TOOLS[tool]}
