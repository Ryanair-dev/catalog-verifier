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

(2026-10-26/27: a second webhook + `send_teams_opportunity_alert()` were
briefly added here for a vendor-offer "cheapest & unique" auto-alert, to
post the generated Excel file into Teams. Reverted 2026-10-27 per user
request — that feature now emails the file instead, via
services/graph_mail.py, see services/vendor_offers_auto_alert.py.)
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(os.getenv("TEAMS_WEBHOOK_URL"))


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
