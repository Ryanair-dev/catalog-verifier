"""
Microsoft Graph email sending, via the `datateamautomations@fordmed.com`
mailbox's own OAuth2 app registration.

Credentials: OUTLOOK_CLIENT_ID / OUTLOOK_CLIENT_SECRET / OUTLOOK_TENANT_ID /
OUTLOOK_REFRESH_TOKEN in .env. Confirmed live (2026-10-09): the refresh token
exchanges successfully, grants Mail.Send + Mail.Read, and its own identity
(GET /me) resolves to datateamautomations@fordmed.com ("DataTeam Email
Automations") — so sending always goes through /me/sendMail, never a
SHARED_MAILBOX-style /users/{other}/sendMail (a stray SHARED_MAILBOX env var
exists but points at an address that 404s on Graph — unused, left alone).
"""
from __future__ import annotations

import base64
import logging
import os
import threading
import time

import requests

log = logging.getLogger(__name__)

_TOKEN_URL_FMT = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_SEND_URL = "https://graph.microsoft.com/v1.0/me/sendMail"
_SCOPE = "https://graph.microsoft.com/.default"

_token_lock = threading.Lock()
_token_cache: dict = {"access_token": None, "expires_at": 0.0}


def is_configured() -> bool:
    return bool(
        os.getenv("OUTLOOK_CLIENT_ID")
        and os.getenv("OUTLOOK_CLIENT_SECRET")
        and os.getenv("OUTLOOK_TENANT_ID")
        and os.getenv("OUTLOOK_REFRESH_TOKEN")
    )


def _get_access_token() -> str:
    with _token_lock:
        now = time.time()
        if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
            return _token_cache["access_token"]
        tenant = os.getenv("OUTLOOK_TENANT_ID")
        resp = requests.post(
            _TOKEN_URL_FMT.format(tenant=tenant),
            data={
                "client_id": os.getenv("OUTLOOK_CLIENT_ID"),
                "client_secret": os.getenv("OUTLOOK_CLIENT_SECRET"),
                "refresh_token": os.getenv("OUTLOOK_REFRESH_TOKEN"),
                "grant_type": "refresh_token",
                "scope": _SCOPE,
            },
            timeout=20,
        )
        resp.raise_for_status()
        j = resp.json()
        _token_cache["access_token"] = j["access_token"]
        _token_cache["expires_at"] = now + int(j.get("expires_in", 3600))
        return _token_cache["access_token"]


def send_mail(
    to: list[str],
    subject: str,
    body_html: str,
    attachments: list[tuple[str, bytes]] | None = None,
    cc: list[str] | None = None,
) -> None:
    """Send an HTML email as datateamautomations@fordmed.com.

    `attachments`: list of (filename, raw_bytes). Raises on any failure —
    callers that want a best-effort send should catch around this call
    themselves (so a bad recipient address doesn't silently vanish a report)."""
    if not is_configured():
        raise RuntimeError("Outlook/Graph credentials are not configured (.env OUTLOOK_*).")
    if not to:
        raise RuntimeError("send_mail: no recipients given.")

    token = _get_access_token()
    message: dict = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body_html},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
    }
    if cc:
        message["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
    if attachments:
        message["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": filename,
                "contentBytes": base64.b64encode(data).decode("ascii"),
            }
            for filename, data in attachments
        ]

    resp = requests.post(
        _SEND_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"message": message, "saveToSentItems": "true"},
        timeout=60,
    )
    if resp.status_code not in (200, 202):
        log.error("[graph_mail] sendMail failed %s: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
