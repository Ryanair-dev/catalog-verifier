"""
Microsoft Teams completion notifications — Offer Analysis, Vendor Offers
export, and Brand Analytics each post a short message to a Teams group chat
when a run finishes, via a Workflows ("Post to a chat when a webhook request
is received") webhook URL.

Credentials: TEAMS_WEBHOOK_URL in .env. Unconfigured = silently a no-op
everywhere this is called (every call site wraps send_teams_message in its
own try/except too, so a notification failure never breaks the actual
feature it's attached to — same pattern as services/graph_mail.py's email
sending).

Payload shape: an Adaptive Card wrapped in `attachments`, per the "Post to
a channel when a webhook request is received" Workflows template's expected
schema — live-tested against the real webhook: a plain `{"text": message}`
body triggers the flow (202) but the run then fails with "the attachment is
null", since the template's post-message action reads
attachments[0].content. The Adaptive Card shape posts successfully.
Separately, `send_teams_opportunity_alert()` posts a vendor-offer "cheapest
and unique" opportunity (see services/vendor_offers_auto_alert.py) WITH the
generated Excel file attached, via its own dedicated webhook/flow
(`TEAMS_OPPORTUNITY_WEBHOOK_URL`) — a different Power Automate flow than the
plain-text one above, since posting an actual file in Teams needs extra
flow steps (Create file in the channel's SharePoint library, then a Post
message step that attaches it) that the simple "post an Adaptive Card"
flow doesn't have. Payload carries the file as base64 (`fileContentBase64`)
for the flow's `base64ToBinary(...)` expression to decode.
"""
from __future__ import annotations

import base64
import logging
import os

import requests

log = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(os.getenv("TEAMS_WEBHOOK_URL"))


def is_opportunity_alert_configured() -> bool:
    return bool(os.getenv("TEAMS_OPPORTUNITY_WEBHOOK_URL"))


def send_teams_message(text: str) -> None:
    """POST a short text message to the configured Teams webhook.

    Raises on failure — callers that want a best-effort send (every call
    site in this app does) should catch around this themselves, same
    convention as services/graph_mail.py send_mail()."""
    url = os.getenv("TEAMS_WEBHOOK_URL")
    if not url:
        raise RuntimeError("Teams notifications are not configured (.env TEAMS_WEBHOOK_URL).")
    payload = {
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.0",
                    "body": [
                        {"type": "TextBlock", "text": text, "wrap": True},
                    ],
                },
            }
        ]
    }
    resp = requests.post(url, json=payload, timeout=20)
    if resp.status_code not in (200, 202):
        log.error("[teams_notify] webhook post failed %s: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()


def send_teams_opportunity_alert(vendor: str, count: int, filename: str, file_bytes: bytes) -> None:
    """POST a vendor cheapest-and-unique opportunity, with the generated
    Excel file attached, to the dedicated opportunity-alert webhook.

    Raises on failure — same best-effort convention as send_teams_message();
    callers should catch around this themselves."""
    url = os.getenv("TEAMS_OPPORTUNITY_WEBHOOK_URL")
    if not url:
        raise RuntimeError(
            "Teams opportunity alerts are not configured (.env TEAMS_OPPORTUNITY_WEBHOOK_URL)."
        )
    plural = "s" if count != 1 else ""
    payload = {
        "vendor": vendor,
        "count": count,
        "summary": f"{vendor} is now cheapest & unique on {count} product{plural} — see attached.",
        "fileName": filename,
        "fileContentBase64": base64.b64encode(file_bytes).decode("ascii"),
    }
    resp = requests.post(url, json=payload, timeout=120)
    if resp.status_code not in (200, 202):
        log.error("[teams_notify] opportunity webhook post failed %s: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
