"""
Match Reports -> Monday board
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from services import database

log = logging.getLogger(__name__)

API_URL = "https://api.monday.com/v2"
DEFAULT_API_VERSION = "2026-10"
TIMEOUT_SECONDS = 20
LOOP_SECONDS = 300          # background check every 5 min (plus an immediate send after each report)
MAX_BACKOFF_MINUTES = 360   # retry waits 1, 2, 4, ... minutes, never more than 6 h; never gives up
BATCH = 50

# key -> (column title on the board, accepted Monday column types)
COLUMNS: dict[str, tuple[str, tuple[str, ...]]] = {
    "vendor_run":   ("Vendor / Run", ("text",)),
    "vendor_title": ("Vendor title", ("text", "long_text")),
    "amazon_title": ("Amazon title", ("text", "long_text")),
    "asin":         ("ASIN", ("link",)),
    "tool_said":    ("Tool said", ("status",)),
    "scorer":       ("Scorer + version", ("text",)),
    "user_says":    ("User says", ("status",)),
    "reason":       ("Reason", ("dropdown",)),
    "note":         ("Note", ("long_text",)),
    "reported_by":  ("Reported by", ("text", "people")),
    "date":         ("Date", ("date",)),
    "times":        ("Times reported", ("numbers",)),
    "status":       ("Status", ("status",)),
    "pair_key":     ("Pair key", ("text",)),
    "report_ids":   ("Report IDs", ("long_text",)),
}
# Without these the app can't find an item again or count reports, so it waits instead of sending.
# Every other column is optional: if a dev removes one, the rest is still sent.
REQUIRED = ("pair_key", "report_ids", "times")

TOOL_SAID = {"verified": "Approved", "review": "Review", "not_approved": "Not approved"}
USER_SAYS = {1: "Same", 0: "Different"}
# App reason codes (services/match_feedback.py) -> the board's Reason dropdown labels.
# Anything not listed becomes "other"; the full reason text always goes into the Note.
REASON_TO_BOARD = {
    "pack_size": "pack size",
    "pack_equivalent": "pack size",
    "size_volume": "size",
    "variant": "variant",
    "brand": "brand",
    "id_mismatch": "part number",
    "different_product": "different product",
}


class MondayError(Exception):
    """Anything that stops a report from reaching the board. The report stays pending."""


class MondayUnreachable(MondayError):
    """Network down / Monday down / rate limited: stop this batch, try later."""


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def _token() -> str:
    return os.getenv("MONDAY_API_TOKEN", "").strip()


def board_id() -> str:
    return os.getenv("MONDAY_REPORTS_BOARD_ID", "").strip()


def is_configured() -> bool:
    return bool(_token()) and board_id().isdigit()


def _reporter_ids() -> dict[str, int]:
    out: dict[str, int] = {}
    for part in os.getenv("MONDAY_REPORTERS", "").split(","):
        name, _, uid = part.partition("=")
        if name.strip() and uid.strip().isdigit():
            out[name.strip().lower()] = int(uid.strip())
    return out


# --------------------------------------------------------------------------- #
# Small helpers shared with services/match_feedback.py
# --------------------------------------------------------------------------- #

def new_report_uid() -> str:
    return uuid.uuid4().hex[:12]


def _vendor_item(rec: dict) -> str:
    for k in ("vendor_mpn", "vendor_upc"):
        v = str(rec.get(k) or "").strip()
        if v:
            return v
    return ""


def pair_key(rec: dict) -> str:
    """Same vendor item + same ASIN = same pair, whichever run or PC it came from."""
    asin = str(rec.get("asin") or "").strip().upper()
    item = _vendor_item(rec).lower()
    if not item:
        item = "title:" + re.sub(r"\s+", " ", str(rec.get("vendor_title") or "").strip().lower())[:80]
    return f"{asin}|{item}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime) -> str:
    # Same text format as SQLite CURRENT_TIMESTAMP (UTC), so string comparison works.
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _local_date(created_at: str | None) -> str:
    try:
        dt = datetime.strptime(str(created_at)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return datetime.now().strftime("%Y-%m-%d")


def _reasons(rec: dict) -> list[str]:
    try:
        v = json.loads(rec.get("reasons") or "[]")
    except (TypeError, ValueError):
        return []
    return [str(c) for c in v] if isinstance(v, list) else []


# --------------------------------------------------------------------------- #
# Monday API
# --------------------------------------------------------------------------- #

def _post(query: str, variables: dict | None = None) -> dict:
    """One GraphQL call. Raises MondayError / MondayUnreachable. Never puts the token in a message."""
    headers = {
        "Authorization": _token(),
        "API-Version": os.getenv("MONDAY_API_VERSION", "").strip() or DEFAULT_API_VERSION,
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(API_URL, json={"query": query, "variables": variables or {}},
                             headers=headers, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise MondayUnreachable(f"Monday not reachable ({type(exc).__name__})") from exc
    if resp.status_code in (401, 403):
        raise MondayError(f"Monday refused the API token (HTTP {resp.status_code}) - check MONDAY_API_TOKEN")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise MondayUnreachable(f"Monday busy or down (HTTP {resp.status_code})")
    try:
        body = resp.json()
    except ValueError:
        raise MondayError(f"Monday answered HTTP {resp.status_code} without JSON") from None
    errors = body.get("errors") or []
    if body.get("error_message"):
        errors = [*errors, {"message": body["error_message"]}]
    if errors or resp.status_code >= 400:
        msg = "; ".join(str(e.get("message", e) if isinstance(e, dict) else e)[:200] for e in errors[:3])
        if any("complexity" in str(e).lower() or "rate limit" in str(e).lower() for e in errors):
            raise MondayUnreachable("Monday rate limit: " + msg)
        raise MondayError("Monday: " + (msg or f"HTTP {resp.status_code}"))
    return body.get("data") or {}


def resolve_columns() -> tuple[str, dict[str, dict], list[str]]:
    """Read the board's columns once. Returns (board name, key -> {id, type}, problems).
    Raises MondayError if the board can't be read or a required column is missing."""
    data = _post("query ($b: [ID!]) { boards(ids: $b) { id name columns { id title type } } }",
                 {"b": [board_id()]})
    boards = data.get("boards") or []
    if not boards:
        raise MondayError(f"Board {board_id()} not found, or this token can't see it")
    board = boards[0]
    by_title = {str(c.get("title") or "").strip().lower(): c for c in board.get("columns") or []}
    cols: dict[str, dict] = {}
    problems: list[str] = []
    for key, (title, types) in COLUMNS.items():
        c = by_title.get(title.lower())
        if not c:
            problems.append(f'missing column "{title}" ({" or ".join(types)})')
        elif c.get("type") not in types:
            problems.append(f'column "{title}" is {c.get("type")}, expected {" or ".join(types)}')
        else:
            cols[key] = {"id": c["id"], "type": c["type"]}
    missing = [COLUMNS[k][0] for k in REQUIRED if k not in cols]
    if missing:
        raise MondayError("Board is missing app columns: " + ", ".join(missing))
    return str(board.get("name") or ""), cols, problems


def _value(col_type: str, v: Any) -> Any:
    if col_type == "long_text":
        return {"text": str(v)}
    if col_type == "status":
        return {"label": str(v)}
    if col_type == "dropdown":
        return {"labels": list(v)}
    if col_type == "date":
        return {"date": str(v)}
    if col_type == "numbers":
        return str(v)
    if col_type == "link":
        return {"url": f"https://www.amazon.com/dp/{v}", "text": str(v)}
    if col_type == "people":
        return {"personsAndTeams": [{"id": int(i), "kind": "person"} for i in v]}
    return str(v)[:500]  # text


def _put(values: dict, cols: dict, key: str, v: Any) -> None:
    if key in cols and v not in (None, "", []):
        values[cols[key]["id"]] = _value(cols[key]["type"], v)


def _reporters_value(cols: dict, names: list[str], people_ids: list[int]) -> Any:
    if "reported_by" not in cols:
        return None
    if cols["reported_by"]["type"] == "people":
        return people_ids
    return ", ".join(names)


def _reason_lines(rec: dict) -> list[str]:
    from services.match_feedback import REASON_LABELS
    lines = []
    codes = _reasons(rec)
    if codes:
        lines.append("Reasons: " + "; ".join(REASON_LABELS.get(c, c) for c in codes))
    if rec.get("correct_asin"):
        lines.append(f"Correct ASIN: {rec['correct_asin']}")
    return lines


def _create_values(rec: dict, cols: dict, uid: str) -> dict:
    name = (rec.get("reported_by") or "").strip() or "unknown"
    pid = _reporter_ids().get(name.lower())
    scorer = " · ".join(p for p in (
        rec.get("scoring_method") or "matcher",
        f"build {rec.get('code_version') or 'unknown'}",
        f"model {rec['model_hash']}" if rec.get("model_hash") else "",
    ) if p)
    note = "\n".join([*( [rec["note"]] if rec.get("note") else [] ), *_reason_lines(rec)])
    board_reasons = sorted({REASON_TO_BOARD.get(c, "other") for c in _reasons(rec)})
    verdict = str(rec.get("verdict") or "").lower()

    v: dict = {}
    _put(v, cols, "vendor_run", f"{rec.get('run_name') or 'run'} (run {rec.get('run_id')})")
    _put(v, cols, "vendor_title", rec.get("vendor_title") or "")
    _put(v, cols, "amazon_title", rec.get("amazon_title") or "")
    _put(v, cols, "asin", rec.get("asin") or "")
    _put(v, cols, "tool_said", TOOL_SAID.get(verdict, verdict.replace("_", " ").capitalize()))
    _put(v, cols, "scorer", scorer)
    _put(v, cols, "user_says", USER_SAYS.get(rec.get("label")))
    _put(v, cols, "reason", board_reasons)
    _put(v, cols, "note", note)
    _put(v, cols, "reported_by", _reporters_value(cols, [name], [pid] if pid else []))
    _put(v, cols, "date", _local_date(rec.get("created_at")))
    _put(v, cols, "times", 1)
    _put(v, cols, "status", "New")
    _put(v, cols, "pair_key", rec["pair_key"])
    _put(v, cols, "report_ids", uid)
    return v


def _item_name(rec: dict) -> str:
    item = _vendor_item(rec) or f"row {int(rec.get('row_idx') or 0) + 1}"
    return f"{rec.get('asin')} · {item}"[:250]


def _item_query_fields(cols: dict) -> tuple[str, list[str]]:
    ids = [cols[k]["id"] for k in ("pair_key", "report_ids", "times", "reported_by") if k in cols]
    return "id state column_values(ids: $c) { id text value }", ids


def _pick(items: list[dict], key: str, cols: dict) -> dict | None:
    pk = cols["pair_key"]["id"]
    hits = []
    for it in items or []:
        if str(it.get("state") or "active") != "active":
            continue                      # archived / deleted by a dev: a new report starts a new item
        cv = {c.get("id"): c for c in it.get("column_values") or []}
        if str((cv.get(pk) or {}).get("text") or "").strip() == key:
            hits.append(it)
    return min(hits, key=lambda it: int(it["id"])) if hits else None


def _find_item(rec: dict, cols: dict, known_item_id: str | None) -> dict | None:
    fields, ids = _item_query_fields(cols)
    if known_item_id:
        data = _post(f"query ($i: [ID!], $c: [String!]) {{ items(ids: $i) {{ {fields} }} }}",
                     {"i": [known_item_id], "c": ids})
        hit = _pick(data.get("items") or [], rec["pair_key"], cols)
        if hit:
            return hit
    data = _post(
        "query ($b: ID!, $q: [ItemsPageByColumnValuesQuery!], $c: [String!]) {"
        f" items_page_by_column_values(limit: 25, board_id: $b, columns: $q) {{ items {{ {fields} }} }} }}",
        {"b": board_id(), "q": [{"column_id": cols["pair_key"]["id"], "column_values": [rec["pair_key"]]}],
         "c": ids},
    )
    return _pick((data.get("items_page_by_column_values") or {}).get("items") or [], rec["pair_key"], cols)


def _existing_state(item: dict, cols: dict) -> tuple[list[str], list[str], list[int]]:
    cv = {c.get("id"): c for c in item.get("column_values") or []}
    uids = [u for u in re.split(r"[\s,]+", str((cv.get(cols["report_ids"]["id"]) or {}).get("text") or "")) if u]
    names: list[str] = []
    pids: list[int] = []
    if "reported_by" in cols:
        rb = cv.get(cols["reported_by"]["id"]) or {}
        names = [n.strip() for n in str(rb.get("text") or "").split(",") if n.strip()]
        if cols["reported_by"]["type"] == "people":
            try:
                pt = (json.loads(rb.get("value") or "{}") or {}).get("personsAndTeams") or []
                pids = [int(p["id"]) for p in pt if p.get("kind", "person") == "person"]
            except (TypeError, ValueError, KeyError):
                pids = []
    return uids, names, pids


def send_record(rec: dict, cols: dict, known_item_id: str | None = None) -> tuple[str, str]:
    """Put one local report on the board. Returns (item_id, "created" | "counted" | "already").
    rec is a match_feedback row (dict) with report_uid and pair_key set."""
    uid = rec["report_uid"]
    item = _find_item(rec, cols, known_item_id)
    if item is None:
        data = _post(
            "mutation ($b: ID!, $n: String!, $v: JSON!) {"
            " create_item(board_id: $b, item_name: $n, column_values: $v, create_labels_if_missing: true) { id } }",
            {"b": board_id(), "n": _item_name(rec), "v": json.dumps(_create_values(rec, cols, uid))},
        )
        return str(data["create_item"]["id"]), "created"

    item_id = str(item["id"])
    uids, names, pids = _existing_state(item, cols)
    if uid in uids:
        return item_id, "already"        # counted before; the answer got lost on the way back

    name = (rec.get("reported_by") or "").strip() or "unknown"
    if name.lower() not in {n.lower() for n in names}:
        names.append(name)
    pid = _reporter_ids().get(name.lower())
    if pid and pid not in pids:
        pids.append(pid)
    uids.append(uid)

    v: dict = {}
    _put(v, cols, "report_ids", ", ".join(uids))
    _put(v, cols, "times", len(uids))
    _put(v, cols, "reported_by", _reporters_value(cols, names, pids))
    _post(
        "mutation ($b: ID!, $i: ID!, $v: JSON!) {"
        " change_multiple_column_values(board_id: $b, item_id: $i, column_values: $v,"
        " create_labels_if_missing: true) { id } }",
        {"b": board_id(), "i": item_id, "v": json.dumps(v)},
    )
    # The new reporter's own view, as an update on the item (Monday notifies subscribers).
    says = USER_SAYS.get(rec.get("label"), "?")
    body = "\n".join([
        f"Reported again by {name} on {_local_date(rec.get('created_at'))}: says {says}.",
        *_reason_lines(rec),
        *( [f"Note: {rec['note']}"] if rec.get("note") else [] ),
        f"Run: {rec.get('run_name') or ''} (run {rec.get('run_id')}), tool said "
        f"{TOOL_SAID.get(str(rec.get('verdict') or '').lower(), rec.get('verdict'))}.",
    ])
    try:
        _post("mutation ($i: ID!, $t: String!) { create_update(item_id: $i, body: $t) { id } }",
              {"i": item_id, "t": body})
    except MondayError:
        # The count is already saved; losing this comment is better than counting twice on retry.
        log.warning("[monday_reports] item %s counted, but posting the update failed", item_id)
    return item_id, "counted"


# --------------------------------------------------------------------------- #
# Board setup: create the columns this design needs (scripts/monday_reports_check.py --setup)
# --------------------------------------------------------------------------- #

# Columns only Claude and the devs fill; created by --setup so the board matches the design.
DEV_COLUMNS = [("Cause", "dropdown"), ("Urgency", "status"), ("Add to eval set", "checkbox")]
SETUP_LABELS = {
    "Tool said": ["Approved", "Review", "Not approved"],
    "User says": ["Same", "Different"],
    "Reason": ["pack size", "size", "variant", "brand", "part number", "different product", "other"],
    "Status": ["New", "Confirmed", "Won't fix", "Fixed"],
    "Urgency": ["High", "Medium", "Low"],
}


def _create_column(title: str, col_type: str, defaults: dict | None) -> None:
    if defaults is None:
        _post("mutation ($b: ID!, $t: String!, $ty: ColumnType!) {"
              " create_column(board_id: $b, title: $t, column_type: $ty) { id } }",
              {"b": board_id(), "t": title, "ty": col_type})
    else:
        _post("mutation ($b: ID!, $t: String!, $ty: ColumnType!, $d: JSON) {"
              " create_column(board_id: $b, title: $t, column_type: $ty, defaults: $d) { id } }",
              {"b": board_id(), "t": title, "ty": col_type, "d": json.dumps(defaults)})


def setup_board_columns() -> list[str]:
    """Create every missing column on the configured board. Never changes or deletes an existing
    column. Returns one line per column saying what happened."""
    data = _post("query ($b: [ID!]) { boards(ids: $b) { id name columns { id title type } } }",
                 {"b": [board_id()]})
    boards = data.get("boards") or []
    if not boards:
        raise MondayError(f"Board {board_id()} not found, or this token can't see it")
    existing = {str(c.get("title") or "").strip().lower(): c for c in boards[0].get("columns") or []}
    wanted = [(title, types) for title, types in COLUMNS.values()] + [(t, (ty,)) for t, ty in DEV_COLUMNS]
    out: list[str] = []
    for title, types in wanted:
        col_type = types[0]
        c = existing.get(title.lower())
        if c:
            if c.get("type") in types:
                out.append(f"  ok        {title}")
            else:
                out.append(f"  FIX       {title}: is {c.get('type')}, should be {col_type}. "
                           "Delete or rename it in Monday, then run --setup again.")
            continue
        labels = SETUP_LABELS.get(title)
        if labels:
            if col_type == "status":
                defaults = {"labels": {str(i): lab for i, lab in enumerate(labels)}}
            else:
                defaults = {"settings": {"labels": [{"id": i + 1, "name": lab} for i, lab in enumerate(labels)]}}
            try:
                _create_column(title, col_type, defaults)
                out.append(f"  created   {title} ({col_type}, with labels)")
                continue
            except MondayUnreachable:
                raise
            except MondayError:
                pass                      # this API version wants another label format: create it plain
        _create_column(title, col_type, None)
        note = ""
        if labels:
            note = (", labels come by themselves when the app uses them" if title in ("Tool said", "User says", "Reason")
                    else ", add labels by hand: " + " / ".join(labels))
        out.append(f"  created   {title} ({col_type}{note})")
    return out


# --------------------------------------------------------------------------- #
# Local queue (match_feedback.monday_* columns)
# --------------------------------------------------------------------------- #

def _due_rows(limit: int = BATCH) -> list[dict]:
    with closing(database._connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM match_feedback WHERE monday_state='pending' "
            "AND (monday_next_try IS NULL OR monday_next_try <= ?) ORDER BY id LIMIT ?",
            (_ts(_now()), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _known_item(key: str) -> str | None:
    with closing(database._connect()) as conn:
        r = conn.execute(
            "SELECT monday_item_id FROM match_feedback WHERE pair_key=? AND monday_state='sent' "
            "AND monday_board_id=? AND monday_item_id IS NOT NULL ORDER BY id DESC LIMIT 1",
            (key, board_id()),
        ).fetchone()
    return r["monday_item_id"] if r else None


def _mark_sent(fid: int, item_id: str) -> None:
    with database._LOCK, closing(database._connect()) as conn, conn:
        conn.execute(
            "UPDATE match_feedback SET monday_state='sent', monday_item_id=?, monday_board_id=?, "
            "monday_sent_at=?, monday_error=NULL, monday_next_try=NULL WHERE id=?",
            (item_id, board_id(), _ts(_now()), fid),
        )


def _mark_failed(row: dict, err: str) -> None:
    attempts = int(row.get("monday_attempts") or 0) + 1
    wait = min(2 ** (attempts - 1), MAX_BACKOFF_MINUTES)
    with database._LOCK, closing(database._connect()) as conn, conn:
        conn.execute(
            "UPDATE match_feedback SET monday_attempts=?, monday_error=?, monday_next_try=? WHERE id=?",
            (attempts, err[:300], _ts(_now() + timedelta(minutes=wait)), row["id"]),
        )


def send_due() -> dict[str, int]:
    """Send every pending report whose retry time has come. Safe to call any time."""
    result = {"created": 0, "counted": 0, "already": 0, "failed": 0}
    if not is_configured():
        return result
    rows = _due_rows()
    if not rows:
        return result
    try:
        _, cols, _ = resolve_columns()
    except MondayError as exc:
        if not isinstance(exc, MondayUnreachable):
            log.warning("[monday_reports] %s", exc)
        for row in rows:
            _mark_failed(row, str(exc))
        result["failed"] = len(rows)
        return result

    while rows:
        for row in rows:
            try:
                item_id, action = send_record(row, cols, _known_item(row["pair_key"]))
            except MondayUnreachable as exc:
                _mark_failed(row, str(exc))
                result["failed"] += 1
                return result               # the rest would fail the same way; next loop retries
            except Exception as exc:  # noqa: BLE001  (one bad report must not block the others)
                if not isinstance(exc, MondayError):
                    log.exception("[monday_reports] report %s failed", row.get("id"))
                _mark_failed(row, str(exc) or type(exc).__name__)
                result["failed"] += 1
            else:
                _mark_sent(row["id"], item_id)
                result[action] += 1
        rows = _due_rows() if len(rows) == BATCH else []
    return result


def retry_now() -> int:
    """Make every pending report due immediately (admin button / after fixing the key)."""
    with database._LOCK, closing(database._connect()) as conn, conn:
        n = conn.execute("UPDATE match_feedback SET monday_next_try=NULL WHERE monday_state='pending'").rowcount
    kick()
    return n


def backfill() -> int:
    """Queue reports made before Monday sending existed (monday_state IS NULL)."""
    with closing(database._connect()) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM match_feedback WHERE monday_state IS NULL "
                                               "AND kind='report'").fetchall()]
    with database._LOCK, closing(database._connect()) as conn, conn:
        for r in rows:
            conn.execute(
                "UPDATE match_feedback SET monday_state='pending', report_uid=COALESCE(report_uid, ?), "
                "pair_key=COALESCE(pair_key, ?) WHERE id=?",
                (new_report_uid(), pair_key(r), r["id"]),
            )
    return len(rows)


def status() -> dict[str, Any]:
    with closing(database._connect()) as conn:
        counts = {(r["s"] or "before_monday"): r["n"] for r in conn.execute(
            "SELECT monday_state AS s, COUNT(*) AS n FROM match_feedback WHERE kind='report' GROUP BY monday_state")}
        oldest = conn.execute(
            "SELECT MIN(created_at) AS t FROM match_feedback WHERE monday_state='pending'").fetchone()["t"]
        errors = [dict(r) for r in conn.execute(
            "SELECT id, created_at, asin, monday_attempts AS attempts, monday_error AS error, "
            "monday_next_try AS next_try FROM match_feedback WHERE monday_state='pending' "
            "AND monday_error IS NOT NULL ORDER BY id DESC LIMIT 5")]
    return {
        "configured": is_configured(), "board_id": board_id() or None,
        "sent": counts.get("sent", 0), "pending": counts.get("pending", 0),
        "before_monday": counts.get("before_monday", 0),
        "oldest_pending": oldest, "last_errors": errors,
    }


# --------------------------------------------------------------------------- #
# Background sending
# --------------------------------------------------------------------------- #

_RUN_LOCK = threading.Lock()
_AGAIN = threading.Event()
_STOP = threading.Event()
_thread: threading.Thread | None = None


def _drain() -> None:
    if not _RUN_LOCK.acquire(blocking=False):
        _AGAIN.set()                      # a send is running; it will go round once more
        return
    try:
        while True:
            _AGAIN.clear()
            try:
                send_due()
            except Exception:  # noqa: BLE001
                log.exception("[monday_reports] send loop failed")
            if not _AGAIN.is_set():
                break
    finally:
        _RUN_LOCK.release()


def kick() -> None:
    """Send now, in the background. Called right after a report is saved."""
    if is_configured():
        threading.Thread(target=_drain, name="monday-reports-send", daemon=True).start()


def start_scheduler() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _STOP.clear()

    def _loop() -> None:
        while not _STOP.is_set():
            _drain()
            _STOP.wait(LOOP_SECONDS)

    _thread = threading.Thread(target=_loop, name="monday-reports", daemon=True)
    _thread.start()


def stop_scheduler() -> None:
    _STOP.set()
