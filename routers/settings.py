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
