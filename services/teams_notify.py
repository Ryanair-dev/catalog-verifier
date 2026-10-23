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

Payload shape: `{"text": message}` — the simplest, most broadly-compatible
body for a Teams Workflows webhook trigger (also matches the legacy
Incoming Webhook connector's format). NOT YET LIVE-TESTED against a real
webhook URL as of this writing (none was available) — the first real send
should be checked for delivery/formatting once a URL is configured, and
this payload shape adjusted if the specific configured workflow expects a
different field name.
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
    resp = requests.post(url, json={"text": text}, timeout=20)
    if resp.status_code not in (200, 202):
        log.error("[teams_notify] webhook post failed %s: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
