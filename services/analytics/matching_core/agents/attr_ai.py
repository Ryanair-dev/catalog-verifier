"""
AI help for attribute extraction: rules first, AI only where the rules are unsure, the code checks the AI,
and what a person accepts becomes a rule (so AI calls drop run after run).

Switch: MATCH_AI_TITLE_ATTRS=1 (the same switch attr_extract.py v11 uses to READ the AI-expanded title).
Needs ANTHROPIC_API_KEY.  With the switch off nothing here calls an AI.

1. ROUTER + TITLE READING (runs inside every analytics run AND every re-score, before scoring: runner.py calls
   enrich_run).  It uses the QUERY AGENT's own unabbreviation step (query_agent.py, read_title = the same prompt
   it uses for the search, without the search); this file has no AI prompt of its own for it.
   The agent is applied to a vendor row only when the rules were not enough (router_reasons):
     - the title has abbreviations / codes that NO rule explains ("BLK", "GG", "RDT", "PGHXPSK"), or
     - the rules found none of the attributes that matter for the product category, or
     - door hardware without a function type (privacy / passage / entry: often only in the model series).
   Rows where the rules were enough are never sent (Injinji: color and size come from the vendor columns -> 0 calls).
   Learned pairs (abbreviations_learned.csv, step 3) are applied without any AI call.
   The agent returns, per title:
     - expansions: {"from": "SN", "to": "Satin Nickel"}  - checked: the short form must be a word of the title;
     - attributes: {"finish": {"value": "satin nickel", "evidence": "SN"}}  - checked: the evidence must really be
       written in the title or the vendor part number, else the attribute is dropped.
   When the search fallback runs the agent anyway (nothing found), record_from_agent_trace keeps what it read, so
   there is no second call.  Saved on the vendor row as data_json["ai_unabbrev"] (also "checked, nothing there").
   attr_extract reads it LAST, so it only fills attributes the rules left empty; when it disagrees with the rules
   the attribute becomes "unclear" (Review).  A value whose words are written in the title as they are
   ("Satin Nickel") counts like a rule ("ai_read"); a value decoded from a code ("15" -> satin nickel) stays soft
   ("ai_title"): it can send a pair to Review, never reject it alone.
2. CACHE: every AI answer is stored in the table attr_ai_cache, keyed by task + prompt version + model + the
   exact text.  The same title is never sent twice, also across runs.  Usage per run goes to attr_ai_usage.
3. SUGGESTIONS -> RULES (the learning loop)
     - every AI pair becomes a suggestion (table attr_ai_suggestions, kind "abbreviation");
     - color names the word library does not know ("Atlantis", "Celestine") are asked once for a base color
       (kind "color", only answers the AI is sure of);
     - a person accepts or rejects them (command below).  Accepted abbreviations are appended to
       matching_core/lexicon/abbreviations_learned.csv, accepted colors to matching_core/lexicon/colors.csv
       (the lexicon CSV files are created from the built-in lists first when they do not exist yet).
       Restart the app afterwards so it reads the new words.
     - suggestions never change a verdict by themselves.
4. AUDIT (on demand): a random sample of rows the rules handled CONFIDENTLY is shown to the AI together with
   what the rules extracted; it reports wrong values and missed attributes, each with a quote that must really be
   in the text.  Result: a workbook to read through.  Nothing is changed.

Commands (repo root):
  uv run python -m services.analytics.matching_core.agents.attr_ai --enrich --run 12 [--force] [--dry-run]
  uv run python -m services.analytics.matching_core.agents.attr_ai --colors --run 12
  uv run python -m services.analytics.matching_core.agents.attr_ai --suggestions [--kind color] [--status pending]
  uv run python -m services.analytics.matching_core.agents.attr_ai --accept 3,4,9      (or --reject 5)
  uv run python -m services.analytics.matching_core.agents.attr_ai --accept-all --kind abbreviation --min-seen 2
  uv run python -m services.analytics.matching_core.agents.attr_ai --audit --run 12 --sample 50 [--out audit.xlsx]
  uv run python -m services.analytics.matching_core.agents.attr_ai --usage
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

MODEL = os.getenv("UNABBREV_MODEL", "claude-sonnet-4-6")      # same model as the query agent by default
BATCH_SIZE = 20
MAX_TO_WORDS = 5


# --------------------------------------------------------------------------- #
# Small helpers (this file needs no other agent file)
# --------------------------------------------------------------------------- #

_VOWELS = set("aeiouy")
_COMMON = frozenset((
    "and the for with of in to by a an or on at no pro plus max mini ultra extra super pack set kit "
    "oz ml l lb lbs g kg mg mcg fl ct pc pcs ea in ft mm cm yd qty usa us led usb lcd tv dvd cd ac dc uv "
    "xl xxl xs xxs s m l box"
).split())


def _known_words() -> frozenset[str]:
    try:
        from services.analytics.matching_core.matching.lexicon import get_lexicon
        lex = get_lexicon()
        return frozenset(set(lex.colors) | set(lex.flavors) | set(lex.scents) | set(lex.sizes)
                         | set(lex.gender) | set(lex.line_filler) | set(lex.tones) | _COMMON)
    except Exception:  # noqa: BLE001
        return _COMMON


def abbreviated_tokens(title: Any, brand: Any = "") -> list[str]:
    """Words of the title that look like abbreviations or codes: no vowel ("BLK", "RDT", "PGHXPSK"), a short
    all-caps word ("GG", "PRIV", "SQT"), or a word ending in a dot ("BAT.").  Brand words and known words skipped."""
    known = _known_words()
    brand_words = {w.lower() for w in re.findall(r"[A-Za-z]+", str(brand or ""))}
    out: list[str] = []
    for m in re.finditer(r"[A-Za-z]+\.?", str(title or "")):
        raw = m.group(0)
        word = raw.rstrip(".")
        low = word.lower()
        if len(low) < 2 or low in brand_words or low in known:
            continue
        no_vowel = not (set(low) & _VOWELS)
        short_caps = word.isupper() and 2 <= len(word) <= 4
        dotted = raw.endswith(".") and len(word) <= 6
        if (no_vowel or short_caps or dotted) and raw not in out:
            out.append(raw)
    return out


def _token_re(token: str) -> re.Pattern:
    core = re.escape(token.rstrip("."))
    return re.compile(rf"(?<![A-Za-z0-9]){core}\.?(?![A-Za-z0-9])", re.IGNORECASE)


def check_pairs(title: str, pairs: Any) -> tuple[list[dict], list[dict]]:
    """(kept, dropped): "from" really is a word of the title, "to" is a short plain phrase that differs."""
    kept: list[dict] = []
    dropped: list[dict] = []
    for p in pairs if isinstance(pairs, list) else []:
        src = str((p or {}).get("from") or "").strip()
        dst = re.sub(r"\s+", " ", str((p or {}).get("to") or "")).strip()
        reason = ""
        if not src or not dst:
            reason = "empty"
        elif not _token_re(src).search(title):
            reason = "not a word of the title"
        elif len(dst.split()) > MAX_TO_WORDS:
            reason = "expansion too long"
        elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 &/'-]*", dst):
            reason = "expansion has odd characters"
        elif dst.lower() == src.rstrip(".").lower():
            reason = "no change"
        elif any(k["from"].lower() == src.lower() for k in kept):
            reason = "duplicate"
        (dropped if reason else kept).append({"from": src, "to": dst, **({"reason": reason} if reason else {})})
    return kept, dropped


def apply_pairs(title: str, pairs: list[dict]) -> str:
    out = title
    for p in sorted(pairs, key=lambda q: -len(q["from"])):
        out = _token_re(p["from"]).sub(p["to"], out)
    return re.sub(r"\s+", " ", out).strip()


def _parse_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.MULTILINE).strip()
    for candidate in (cleaned, (re.search(r"\{.*\}", cleaned, re.DOTALL) or [None])[0]):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
            return value if isinstance(value, dict) else {}
        except (TypeError, json.JSONDecodeError):
            continue
    return {}


def _client():
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (put it in .env)")
    from anthropic import Anthropic
    return Anthropic(api_key=key, max_retries=3, timeout=90.0)


def cost_usd(tokens_in: int, tokens_out: int) -> float:
    """Claude Sonnet 4.6 list price: $3 / 1M input, $15 / 1M output."""
    return round(tokens_in * 3 / 1_000_000 + tokens_out * 15 / 1_000_000, 4)

FILE_VERSION = "2026-10-22 attr_ai v4 (uses the query agent's unabbreviation step; router + checks only)"

COLOR_PROMPT = "color-family-v1"
AUDIT_PROMPT = "attr-audit-v1"
COLOR_BATCH = 40
AUDIT_BATCH = 10
AUDIT_BOXES = ("color", "size", "gender", "flavor", "scent", "finish", "type", "material",
               "volume_ml", "weight_g", "count", "shade_code")
BASE_COLORS = ("black", "white", "gray", "brown", "red", "orange", "yellow", "green", "blue", "purple", "pink",
               "beige", "clear")
LEARNED_FILE = "abbreviations_learned.csv"


def enabled() -> bool:
    """AI help for attribute extraction.  ON by default when an Anthropic key is set;
    MATCH_AI_TITLE_ATTRS=0 in .env switches it off."""
    raw = os.getenv("MATCH_AI_TITLE_ATTRS")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(os.getenv("ANTHROPIC_API_KEY"))


# --------------------------------------------------------------------------- #
# Database: cache, suggestions, usage
# --------------------------------------------------------------------------- #

_TABLES_READY = False


def _db() -> sqlite3.Connection:
    global _TABLES_READY
    from services import database
    conn = database._connect()
    conn.row_factory = sqlite3.Row
    if not _TABLES_READY:
        with database._LOCK:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS attr_ai_cache (
                    key TEXT PRIMARY KEY, task TEXT, prompt_version TEXT, model TEXT,
                    input_text TEXT, output_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS attr_ai_suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, key TEXT, value TEXT, example TEXT,
                    run_id INTEGER, seen INTEGER DEFAULT 1, status TEXT DEFAULT 'pending',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP, decided_at TEXT,
                    UNIQUE(kind, key, value));
                CREATE TABLE IF NOT EXISTS attr_ai_runs (
                    run_id INTEGER PRIMARY KEY, status TEXT, message TEXT, rows_total INTEGER,
                    rows_checked INTEGER, rows_sent INTEGER, rows_with_result INTEGER,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS attr_ai_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, task TEXT, rows_total INTEGER,
                    rows_flagged INTEGER, rows_sent INTEGER, cache_hits INTEGER, calls INTEGER,
                    tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            """)
            conn.commit()
        _TABLES_READY = True
    return conn


def _cache_key(task: str, prompt: str, text: str) -> str:
    return hashlib.sha1(f"{task}|{prompt}|{MODEL}|{text.strip().lower()}".encode("utf-8")).hexdigest()


def _cache_get(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute("SELECT output_json FROM attr_ai_cache WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    try:
        value = json.loads(row["output_json"] or "null")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _cache_put(conn: sqlite3.Connection, key: str, task: str, prompt: str, text: str, output: dict) -> None:
    from services import database
    with database._LOCK:
        conn.execute("INSERT OR REPLACE INTO attr_ai_cache(key, task, prompt_version, model, input_text, output_json) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (key, task, prompt, MODEL, text[:2000], json.dumps(output)))
        conn.commit()


def _suggest(conn: sqlite3.Connection, kind: str, key: str, value: str, example: str, run_id: Any) -> None:
    from services import database
    key, value = key.strip().lower(), value.strip().lower()
    if not key or not value or key == value:
        return
    with database._LOCK:
        conn.execute("INSERT INTO attr_ai_suggestions(kind, key, value, example, run_id) VALUES (?, ?, ?, ?, ?) "
                     "ON CONFLICT(kind, key, value) DO UPDATE SET seen = seen + 1",
                     (kind, key, value, example[:300], run_id))
        conn.commit()


def _usage(conn: sqlite3.Connection, run_id: Any, task: str, **numbers: Any) -> None:
    from services import database
    with database._LOCK:
        conn.execute("INSERT INTO attr_ai_usage(run_id, task, rows_total, rows_flagged, rows_sent, cache_hits, calls, "
                     "tokens_in, tokens_out, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (run_id, task, numbers.get("rows_total", 0), numbers.get("rows_flagged", 0),
                      numbers.get("rows_sent", 0), numbers.get("cache_hits", 0), numbers.get("calls", 0),
                      numbers.get("tokens_in", 0), numbers.get("tokens_out", 0),
                      cost_usd(numbers.get("tokens_in", 0), numbers.get("tokens_out", 0))))
        conn.commit()


def _set_status(run_id: Any, status: str, message: str = "", **numbers: Any) -> None:
    """What the AI step did for a run (shown in the UI): ran / off / failed, with the error message."""
    try:
        from services import database
        conn = _db()
        try:
            with database._LOCK:
                conn.execute("INSERT OR REPLACE INTO attr_ai_runs(run_id, status, message, rows_total, rows_checked, "
                             "rows_sent, rows_with_result, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                             (run_id, status, message[:1000], numbers.get("rows_total", 0),
                              numbers.get("rows_checked", 0), numbers.get("rows_sent", 0),
                              numbers.get("rows_with_result", 0)))
                conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - the status is only for display
        pass


def _json(text: Any) -> dict:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------- #
# Rules the AI does not need to repeat
# --------------------------------------------------------------------------- #

def _lexicon_dir() -> Path:
    from services.analytics.matching_core.matching.lexicon import LEXICON_DIR
    return LEXICON_DIR


def learned_pairs() -> dict[str, str]:
    """abbreviation (lowercase, no dot) -> full text, from abbreviations_learned.csv (pairs a person accepted)."""
    path = _lexicon_dir() / LEARNED_FILE
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    except OSError:
        text = ""
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if "," in line:
            abbr, full = (part.strip() for part in line.split(",", 1))
            if abbr and full:
                out[abbr.lower().rstrip(".")] = full
    return out


def _rule_known() -> set[str]:
    """Abbreviations the rules already explain (abbreviations.json attribute words + learned pairs)."""
    known: set[str] = set(learned_pairs())
    try:
        from services.analytics.matching_core.matching.attr_extract import _abbrev_map
        known |= set(_abbrev_map())
    except Exception:  # noqa: BLE001
        pass
    return known


def _norm_token(token: str) -> str:
    return token.rstrip(".").lower()


# --------------------------------------------------------------------------- #
# 1. Router + title reading (abbreviations AND attributes) for one run
# --------------------------------------------------------------------------- #

AI_BOXES = ("color", "finish", "size", "type", "flavor", "scent", "gender", "material", "count", "volume", "weight")
_RULE_BOXES = ("color", "finish", "size", "type", "flavor", "scent", "count", "volume_ml", "weight_g", "shade_code")
AGENT_WORKERS = 4                  # titles read at the same time (one agent call each)


def _prompt_version() -> str:
    """The query agent's prompt version (+attrs): a new prompt makes every title be read again."""
    from services.analytics.matching_core.agents.query_agent import READ_PROMPT_VERSION
    return READ_PROMPT_VERSION


def _read_with_agent(rows: list[dict]) -> tuple[dict[int, dict], dict]:
    """The query agent's own unabbreviation step (read_title: same prompt as for the search, no search) for each
    row, 4 at a time.  rows: [{"row_idx", "title", "brand", "mpn"}] -> ({row_idx: answer}, usage totals)."""
    from concurrent.futures import ThreadPoolExecutor
    from services.analytics.matching_core.agents.query_agent import create_title_reader
    from services.analytics.matching_core.models import Offer
    reader = create_title_reader()

    def one(r: dict) -> tuple[int, dict]:
        offer = Offer(offer_id=str(r["row_idx"]), title=r["title"], raw_text=r["title"],
                      brand=r["brand"] or None, mpn=r["mpn"] or None)
        return r["row_idx"], reader.read_title(offer)

    answers: dict[int, dict] = {}
    totals = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "errors": 0}
    with ThreadPoolExecutor(max_workers=AGENT_WORKERS) as pool:
        for row_idx, answer in pool.map(one, rows):
            answers[row_idx] = answer
            totals["calls"] += answer.get("llm_calls", 0)
            totals["tokens_in"] += answer.get("tokens_in", 0)
            totals["tokens_out"] += answer.get("tokens_out", 0)
            totals["errors"] += bool(answer.get("error"))
    return answers, totals


def _needed_boxes(category: str) -> set[str] | None:
    """The attributes that matter for this product category (word library category_boxes), or None = unknown."""
    from services.analytics.matching_core.matching.lexicon import variant_boxes_for
    allowed = variant_boxes_for(category)
    if allowed is None:
        return None
    return {b for b in allowed if b in ("color", "size", "flavor", "scent")}


_HARDWARE_RE = re.compile(r"\b(lock|lever|knob|deadbolt|handleset|handle|latch|keypad|smartcode|smartkey|hinge|"
                          r"door)\b", re.IGNORECASE)


def router_reasons(data: dict, extracted: dict, known: set[str]) -> tuple[list[str], list[str]]:
    """WHEN the AI is applied to a vendor row.  Returns (reasons, abbreviations no rule explains).
    No reason = the rules were enough, no AI call.  A reason:
      - the title has abbreviations / codes that no rule explains ("RDT", "BLK", "GG", "PGHXPSK");
      - the rules found NONE of the attributes that matter for the product category (a sock: color or size;
        unknown category: no attribute at all);
      - door hardware (a finish was found or the title says lock / lever / knob ...) with no function type
        (privacy / passage / entry ...): the type is often only in the model series (Kwikset 97300 = privacy)."""
    from services.analytics.matching_core.matching.attr_extract import extract_vendor
    title = str(data.get("title") or "")
    reasons: list[str] = []
    open_tokens = [t for t in abbreviated_tokens(title, data.get("brand")) if _norm_token(t) not in known]
    if open_tokens:
        reasons.append("abbreviations no rule explains: " + ", ".join(open_tokens[:6]))
    source = {k: v for k, v in data.items() if k != "ai_unabbrev"}          # the rules alone
    clean_extracted = {k: v for k, v in (extracted or {}).items() if k != "ai_unabbrev"}
    ext = extract_vendor(source, clean_extracted)
    found = set(ext["fields"])
    if "finish" in found:
        found.add("color")                                                  # a finish is the color of hardware
    need = _needed_boxes(ext["identity"].get("category") or "")
    if need is None or not need:
        if not found & set(_RULE_BOXES):
            reasons.append("the rules found no attribute")
    elif not found & need:
        reasons.append("the rules found no " + " / ".join(sorted(need)))
    if ("finish" in found or _HARDWARE_RE.search(title)) and "type" not in found:
        reasons.append("door hardware without a function type")
    return reasons, open_tokens


def check_attributes(title: str, attrs: Any, part_number: str = "") -> tuple[dict, list[dict]]:
    """Keep an AI attribute only when it has a short value and its evidence is really written in the title
    (or in the vendor part number)."""
    kept: dict = {}
    dropped: list[dict] = []
    text = re.sub(r"\s+", " ", f"{title} {part_number}".lower())
    for box, entry in (attrs.items() if isinstance(attrs, dict) else []):
        value = str((entry or {}).get("value") if isinstance(entry, dict) else entry or "").strip()
        evidence = str((entry or {}).get("evidence") or "").strip() if isinstance(entry, dict) else ""
        reason = ""
        if box not in AI_BOXES:
            reason = "unknown attribute"
        elif not value or len(value) > 40:
            reason = "empty or too long value"
        elif not evidence or re.sub(r"\s+", " ", evidence.lower()) not in text:
            reason = "evidence is not in the title or part number"
        if reason:
            dropped.append({"box": box, "value": value, "evidence": evidence, "reason": reason})
        else:
            kept[box] = {"value": value, "evidence": evidence}
    return kept, dropped


def enrich_run(run_id: Any, force: bool = False, dry_run: bool = False) -> dict:
    """Run the title reader for a run and record what happened (ran / off / failed + the error) for the UI.
    Errors are recorded and raised again, so the caller's log shows them too."""
    if not enabled():
        _set_status(run_id, "off", "MATCH_AI_TITLE_ATTRS=0 or no ANTHROPIC_API_KEY")
        return {"run_id": run_id, "enabled": False}
    try:
        summary = _enrich_run(run_id, force=force, dry_run=dry_run)
    except Exception as exc:
        _set_status(run_id, "failed", f"{type(exc).__name__}: {exc}")
        raise
    if not dry_run:
        _set_status(run_id, "ran", "", rows_total=summary["rows_total"],
                    rows_checked=summary["rows_flagged"] + summary["already_done"],
                    rows_sent=summary["rows_sent"], rows_with_result=summary["rows_with_result"])
    return summary


def _enrich_run(run_id: Any, force: bool = False, dry_run: bool = False) -> dict:
    """Read the vendor titles of a run where the rules were not enough: decode abbreviations and pull out the
    attributes, each attribute with a quote from the title.  Learned pairs cost nothing, answers are cached.
    Saves data_json["ai_unabbrev"] on every row it looked at (unless dry_run).  Returns numbers for the log."""
    summary = {"run_id": run_id, "rows_total": 0, "rows_flagged": 0, "rules_enough": 0, "already_done": 0,
               "rows_learned_only": 0, "rows_sent": 0, "cache_hits": 0, "calls": 0, "tokens_in": 0,
               "tokens_out": 0, "saved": 0, "rows_with_result": 0, "attributes_found": 0, "reasons": {},
               "enabled": enabled()}
    if not enabled():
        return summary
    conn = _db()
    try:
        rows = conn.execute("SELECT row_idx, data_json, extracted_json FROM analytics_catalog_rows "
                            "WHERE run_id = ? ORDER BY row_idx", (run_id,)).fetchall()
        learned = learned_pairs()
        known = _rule_known()
        prompt_version = _prompt_version()
        plan: list[dict] = []
        for r in rows:
            data, extracted = _json(r["data_json"]), _json(r["extracted_json"])
            title = str(data.get("title") or "").strip()
            if not title:
                continue
            summary["rows_total"] += 1
            record = data.get("ai_unabbrev") if isinstance(data.get("ai_unabbrev"), dict) else None
            if record and record.get("prompt_version") == prompt_version and not force:
                summary["already_done"] += 1
                continue
            reasons, _open = router_reasons(data, extracted, known)
            learned_hits = [{"from": t, "to": learned[_norm_token(t)]}
                            for t in abbreviated_tokens(title, data.get("brand")) if _norm_token(t) in learned]
            if not reasons and not learned_hits:
                summary["rules_enough"] += 1
                continue
            summary["rows_flagged"] += 1
            for reason in reasons:
                kind = reason.split(":")[0]
                summary["reasons"][kind] = summary["reasons"].get(kind, 0) + 1
            plan.append({"row_idx": r["row_idx"], "data": data, "title": title, "brand": str(data.get("brand") or ""),
                         "mpn": str(data.get("itemid") or ""), "learned": learned_hits, "reasons": reasons,
                         "answer": None, "from_cache": False})

        # the cache first, then the query agent's unabbreviation step for the titles that still need it
        to_send = []
        for p in plan:
            if not p["reasons"]:
                summary["rows_learned_only"] += 1
                continue
            cached = _cache_get(conn, _cache_key("title_read", prompt_version, f"{p['brand']}|{p['mpn']}|{p['title']}"))
            if cached is not None:
                p["answer"], p["from_cache"] = cached, True
                summary["cache_hits"] += 1
            else:
                to_send.append(p)
        if to_send:
            answers, usage = _read_with_agent([{"row_idx": p["row_idx"], "title": p["title"], "brand": p["brand"],
                                                "mpn": p["mpn"]} for p in to_send])
            summary["calls"] += usage["calls"]
            summary["tokens_in"] += usage["tokens_in"]
            summary["tokens_out"] += usage["tokens_out"]
            summary["errors"] = usage["errors"]
            for p in to_send:
                p["answer"] = answers.get(p["row_idx"]) or {}
                if not p["answer"].get("error"):          # a failed call is not cached: it is tried again next time
                    _cache_put(conn, _cache_key("title_read", prompt_version, f"{p['brand']}|{p['mpn']}|{p['title']}"),
                               "title_read", prompt_version, p["title"], p["answer"])
            summary["rows_sent"] = len(to_send)
            if usage["errors"] == len(to_send):
                raise RuntimeError("every agent call failed: " + str(next(
                    (a.get("error") for a in answers.values() if a.get("error")), "unknown error")))

        from services import database
        for p in plan:
            answer = p["answer"] or {}
            learned_kept, _ = check_pairs(p["title"], p["learned"])
            ai_kept, dropped = check_pairs(p["title"], answer.get("expansions"))
            learned_keys = {_norm_token(x["from"]) for x in learned_kept}
            ai_kept = [x for x in ai_kept if _norm_token(x["from"]) not in learned_keys]   # a rule beats the AI
            pairs = [{**x, "source": "learned"} for x in learned_kept] + [{**x, "source": "ai"} for x in ai_kept]
            attributes, dropped_attributes = check_attributes(p["title"], answer.get("attributes"), p["mpn"])
            record = {
                "expanded_title": apply_pairs(p["title"], pairs) if pairs else p["title"],
                "original_title": p["title"],
                "pairs": pairs,
                "attributes": attributes,
                "dropped": dropped,
                "dropped_attributes": dropped_attributes,
                "agent_comparison_title": answer.get("comparison_title") or "",
                "why": p["reasons"] or ["learned abbreviations only"],
                "model": answer.get("model") or "",
                "source": "query_agent.read_title",
                "prompt_version": prompt_version,
                "from_cache": p["from_cache"],
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            if pairs or attributes:
                summary["rows_with_result"] += 1
            summary["attributes_found"] += len(attributes)
            for x in ai_kept:
                _suggest(conn, "abbreviation", _norm_token(x["from"]), x["to"], p["title"], run_id)
            if dry_run:
                found = "; ".join(f"{b} {v['value']} (\"{v['evidence']}\")" for b, v in attributes.items()) or "-"
                print(f"  row {int(p['row_idx']) + 1:>5}  {p['title'][:70]}\n"
                      f"         why: {'; '.join(record['why'])}\n"
                      f"         abbreviations: {'; '.join(x['from'] + ' = ' + x['to'] for x in pairs) or '-'}\n"
                      f"         attributes: {found}"
                      + (f"\n         dropped: {'; '.join(d['box'] + ' ' + d['value'] + ' (' + d['reason'] + ')' for d in dropped_attributes)}"
                         if dropped_attributes else ""))
                continue
            data = dict(p["data"])
            data["ai_unabbrev"] = record                 # saved even when nothing was found: "checked, nothing there"
            with database._LOCK:
                conn.execute("UPDATE analytics_catalog_rows SET data_json = ? WHERE run_id = ? AND row_idx = ?",
                             (json.dumps(data, default=str), run_id, p["row_idx"]))
                conn.commit()
            summary["saved"] += 1
        if not dry_run:
            _usage(conn, run_id, "title_read", **{k: v for k, v in summary.items() if isinstance(v, int)})
    finally:
        conn.close()
    summary["cost_usd"] = cost_usd(summary["tokens_in"], summary["tokens_out"])
    return summary


def record_from_agent_trace(run_id: Any, row_idx: int, title: str, part_number: str, trace: dict) -> bool:
    """When the search fallback ran the query agent for a row anyway, keep what its unabbreviation step read
    (expansions + attributes, checked the same way) on the row, so no second call is needed.  A row that already
    has a reading of the current prompt version keeps it.  Returns True when something was saved."""
    if not enabled() or not isinstance(trace, dict) or trace.get("error"):
        return False
    pairs_in, attrs_in = trace.get("expansions"), trace.get("attributes")
    if not pairs_in and not attrs_in:
        return False
    from services import database
    conn = _db()
    try:
        row = conn.execute("SELECT data_json FROM analytics_catalog_rows WHERE run_id = ? AND row_idx = ?",
                           (run_id, row_idx)).fetchone()
        if not row:
            return False
        data = _json(row["data_json"])
        current = data.get("ai_unabbrev") if isinstance(data.get("ai_unabbrev"), dict) else None
        if current and current.get("prompt_version") == trace.get("prompt_version"):
            return False
        title = title or str(data.get("title") or "")
        kept, dropped = check_pairs(title, pairs_in)
        pairs = [{**x, "source": "ai"} for x in kept]
        attributes, dropped_attributes = check_attributes(title, attrs_in, part_number or str(data.get("itemid") or ""))
        data["ai_unabbrev"] = {
            "expanded_title": apply_pairs(title, pairs) if pairs else title,
            "original_title": title, "pairs": pairs, "attributes": attributes, "dropped": dropped,
            "dropped_attributes": dropped_attributes, "agent_comparison_title": trace.get("comparison_title") or "",
            "why": ["the search found nothing, so the query agent read the title"],
            "model": trace.get("model") or "", "source": "query_agent.run",
            "prompt_version": trace.get("prompt_version") or "", "from_cache": False,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with database._LOCK:
            conn.execute("UPDATE analytics_catalog_rows SET data_json = ? WHERE run_id = ? AND row_idx = ?",
                         (json.dumps(data, default=str), run_id, row_idx))
            conn.commit()
        for x in kept:
            _suggest(conn, "abbreviation", _norm_token(x["from"]), x["to"], title, run_id)
        return True
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 3a. Unknown color names -> suggestions (never a verdict change)
# --------------------------------------------------------------------------- #

COLOR_SYSTEM = f"""\
You map product color NAMES to a base color, for a program that compares product variants.

For each item you get a color name as a seller wrote it, and the product title for context.
Answer the base color the name stands for on that product: one of {", ".join(BASE_COLORS)}.
Answer "unknown" when the name alone does not say it (a pattern, a print, a collection name, a mix of colors,
a brand-specific name you do not know). Do not guess.

Reply with JSON only:
{{"items": [{{"id": "<id>", "family": "blue" | ... | "unknown", "sure": true | false}}]}}
"""


def _unknown_color_names(run_id: Any, conn: sqlite3.Connection) -> dict[str, str]:
    """{color name: an example title} for color values in this run that the word library cannot place."""
    from services.analytics.matching_core.matching.attr_extract import _clean_color
    from services.analytics.matching_core.matching.lexicon import get_lexicon
    lex_colors = get_lexicon().colors
    names: dict[str, str] = {}

    def consider(value: Any, title: str) -> None:
        cleaned = _clean_color(value)
        if not cleaned:
            return
        for part in cleaned.split("/"):
            part = part.strip()
            if part and len(part) <= 30 and not all(w in lex_colors for w in part.split()):
                names.setdefault(part, title)

    for r in conn.execute("SELECT data_json FROM analytics_catalog_rows WHERE run_id = ?", (run_id,)).fetchall():
        data = _json(r["data_json"])
        for header, value in (data.get("raw") or {}).items():
            if re.search(r"colou?r", str(header), re.IGNORECASE) and not re.search(r"code|hex|rgb", str(header), re.I):
                consider(value, str(data.get("title") or ""))
    for r in conn.execute("SELECT json_extract(data_json, '$.amazon.attributes.color') AS color, "
                          "json_extract(data_json, '$.amazon.summary_color') AS summary_color, "
                          "json_extract(data_json, '$.amazon.title') AS title "
                          "FROM analytics_candidates WHERE run_id = ?", (run_id,)).fetchall():
        consider(r["color"], str(r["title"] or ""))
        consider(r["summary_color"], str(r["title"] or ""))
    return names


def suggest_color_names(run_id: Any) -> dict:
    """Ask once (cached) what base color each unknown color name is; sure answers become suggestions."""
    summary = {"run_id": run_id, "names": 0, "asked": 0, "cache_hits": 0, "suggested": 0, "calls": 0,
               "tokens_in": 0, "tokens_out": 0, "enabled": enabled()}
    if not enabled():
        return summary
    conn = _db()
    try:
        names = _unknown_color_names(run_id, conn)
        summary["names"] = len(names)
        already = {row["key"] for row in conn.execute("SELECT key FROM attr_ai_suggestions WHERE kind = 'color'")}
        answers: dict[str, dict] = {}
        to_ask = []
        for name, title in names.items():
            if name in already:
                continue
            cached = _cache_get(conn, _cache_key("color", COLOR_PROMPT, name))
            if cached is not None:
                answers[name] = cached
                summary["cache_hits"] += 1
            else:
                to_ask.append((name, title))
        if to_ask:
            client = _client()
            for i in range(0, len(to_ask), COLOR_BATCH):
                batch = to_ask[i:i + COLOR_BATCH]
                lines = [f'id {n} | color name: {name} | product: {title[:120]}' for n, (name, title) in enumerate(batch)]
                response = client.messages.create(model=MODEL, max_tokens=3000, system=COLOR_SYSTEM,
                                                  messages=[{"role": "user", "content": "\n".join(lines)}])
                summary["calls"] += 1
                summary["tokens_in"] += response.usage.input_tokens
                summary["tokens_out"] += response.usage.output_tokens
                parsed = _parse_json(response.content[0].text if response.content else "")
                by_id = {str(it.get("id")): it for it in parsed.get("items") or [] if isinstance(it, dict)}
                for n, (name, _) in enumerate(batch):
                    ans = by_id.get(str(n)) or {"family": "unknown", "sure": False}
                    answers[name] = ans
                    _cache_put(conn, _cache_key("color", COLOR_PROMPT, name), "color", COLOR_PROMPT, name, ans)
            summary["asked"] = len(to_ask)
        for name, ans in answers.items():
            family = str(ans.get("family") or "").strip().lower()
            if ans.get("sure") and family in BASE_COLORS:
                _suggest(conn, "color", name, family, names.get(name, ""), run_id)
                summary["suggested"] += 1
        _usage(conn, run_id, "color", rows_total=summary["names"], rows_flagged=summary["names"],
               rows_sent=summary["asked"], cache_hits=summary["cache_hits"], calls=summary["calls"],
               tokens_in=summary["tokens_in"], tokens_out=summary["tokens_out"])
    finally:
        conn.close()
    summary["cost_usd"] = cost_usd(summary["tokens_in"], summary["tokens_out"])
    return summary


# --------------------------------------------------------------------------- #
# 3b. Accept / reject suggestions -> rules
# --------------------------------------------------------------------------- #

def _append_learned_abbreviation(key: str, value: str) -> bool:
    path = _lexicon_dir() / LEARNED_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if key in learned_pairs():
        return False
    header = "" if path.is_file() else ("# abbreviation,full text   (accepted by a person from attr_ai suggestions; "
                                        "one per line)\n")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{header}{key},{value}\n")
    return True


def _append_color(key: str, family: str) -> bool:
    from services.analytics.matching_core.matching import lexicon
    lexicon.write_seed(force=False)                  # creates the CSV files from the built-in lists if missing
    path = lexicon.LEXICON_DIR / "colors.csv"
    text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    existing = {line.split(",", 1)[0].strip().lower() for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")}
    if key in existing:
        return False
    with path.open("a", encoding="utf-8") as fh:
        fh.write(("" if text.endswith("\n") or not text else "\n") + f"{key},{key},{family},0\n")
    lexicon.reload_lexicon()
    return True


def decide(ids: list[int], accept: bool) -> list[str]:
    conn = _db()
    notes = []
    try:
        from services import database
        for sid in ids:
            row = conn.execute("SELECT * FROM attr_ai_suggestions WHERE id = ?", (sid,)).fetchone()
            if not row:
                notes.append(f"{sid}: not found")
                continue
            if accept:
                if row["kind"] == "abbreviation":
                    written = _append_learned_abbreviation(row["key"], row["value"])
                else:
                    written = _append_color(row["key"], row["value"])
                notes.append(f"{sid}: accepted {row['kind']} {row['key']} = {row['value']}"
                             + ("" if written else " (was already there)"))
            else:
                notes.append(f"{sid}: rejected {row['kind']} {row['key']} = {row['value']}")
            with database._LOCK:
                conn.execute("UPDATE attr_ai_suggestions SET status = ?, decided_at = CURRENT_TIMESTAMP WHERE id = ?",
                             ("accepted" if accept else "rejected", sid))
                conn.commit()
    finally:
        conn.close()
    return notes


def list_suggestions(kind: str = "", status: str = "pending", min_seen: int = 1) -> list[sqlite3.Row]:
    conn = _db()
    try:
        sql = "SELECT * FROM attr_ai_suggestions WHERE seen >= ?"
        params: list[Any] = [min_seen]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if status:
            sql += " AND status = ?"
            params.append(status)
        return conn.execute(sql + " ORDER BY seen DESC, id", params).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 4. Sampled audit of the rules (reads only)
# --------------------------------------------------------------------------- #

AUDIT_SYSTEM = """\
You check an attribute extractor for product catalog matching.

For each item you get the product text (title, and for vendor rows the spreadsheet columns) and the values the
extractor found (color, size, gender, flavor, scent, finish, type, material, volume, weight, count, shade).
Report ONLY real errors:
  wrong    - a value the extractor found that the text contradicts
  missing  - an attribute the text clearly states that the extractor did not find
Quote the words from the text that prove it in "evidence" - exactly as written, copied from the text.
Do not report style preferences, synonyms (gray = grey), or attributes you have to guess.

Reply with JSON only:
{"items": [{"id": "<id>",
            "wrong":   [{"box": "color", "extracted": "black", "correct": "navy", "evidence": "Navy"}],
            "missing": [{"box": "size", "value": "XL", "evidence": "X-Large"}]}]}
"""


def _vendor_text(data: dict) -> str:
    cols = [f"{k}: {v}" for k, v in (data.get("raw") or {}).items()
            if str(v or "").strip() and len(str(v)) <= 80][:15]
    return f"title: {data.get('title') or ''}" + (" | columns: " + "; ".join(cols) if cols else "")


def _listing_text(item: dict) -> str:
    attrs = item.get("attributes") or {}
    extra = [f"{k} field: {attrs.get(k)}" for k in ("color", "size", "material") if attrs.get(k)]
    extra += [f"{k} field: {item.get(k)}" for k in ("flavor", "scent", "unit_count") if item.get(k)]
    return f"title: {item.get('title') or ''}" + (" | " + "; ".join(extra) if extra else "")


def _extracted_text(fields: dict) -> str:
    return "; ".join(f"{b}: {(fields.get(b) or {}).get('value')}" for b in AUDIT_BOXES if fields.get(b)) or "nothing"


def audit_run(run_id: Any, sample: int = 50, out: str | None = None, seed: int = 7) -> dict:
    """Audit a random sample of confidently-extracted vendor rows and listings.  Writes a workbook; changes nothing."""
    from services.analytics.matching_core.matching.attr_extract import extract_amazon, extract_vendor
    conn = _db()
    try:
        vendor = [(f"row {int(r['row_idx']) + 1}", "vendor", _json(r["data_json"]), _json(r["extracted_json"]))
                  for r in conn.execute("SELECT row_idx, data_json, extracted_json FROM analytics_catalog_rows "
                                        "WHERE run_id = ?", (run_id,)).fetchall()]
        seen: set[str] = set()
        listings = []
        for r in conn.execute("SELECT asin, json_extract(data_json, '$.amazon') AS amazon "
                              "FROM analytics_candidates WHERE run_id = ?", (run_id,)).fetchall():
            if r["asin"] in seen:
                continue
            seen.add(r["asin"])
            listings.append((r["asin"], "listing", _json(r["amazon"]), None))
    finally:
        conn.close()

    rng = random.Random(seed)
    items = []
    for label, kind, data, extracted in vendor + listings:
        try:
            ext = extract_vendor(data, extracted) if kind == "vendor" else extract_amazon(data)
        except Exception:  # noqa: BLE001
            continue
        fields = ext["fields"]
        confident = fields and not any((b or {}).get("conflict") for b in fields.values())
        if kind == "vendor":
            confident = confident and not abbreviated_tokens(data.get("title"), data.get("brand"))
        if confident:
            text = _vendor_text(data) if kind == "vendor" else _listing_text(data)
            items.append({"label": label, "kind": kind, "text": text, "extracted": _extracted_text(fields)})
    half = max(1, sample // 2)
    v_items = [i for i in items if i["kind"] == "vendor"]
    l_items = [i for i in items if i["kind"] == "listing"]
    chosen = rng.sample(v_items, min(half, len(v_items))) + rng.sample(l_items, min(sample - half, len(l_items)))

    conn = _db()
    findings: list[dict] = []
    totals = {"items": len(chosen), "with_problem": 0, "calls": 0, "tokens_in": 0, "tokens_out": 0, "cache_hits": 0}
    try:
        to_ask = []
        for it in chosen:
            key = _cache_key("audit", AUDIT_PROMPT, it["text"] + " || " + it["extracted"])
            cached = _cache_get(conn, key)
            if cached is not None:
                it["answer"] = cached
                totals["cache_hits"] += 1
            else:
                it["key"] = key
                to_ask.append(it)
        if to_ask:
            client = _client()
            for i in range(0, len(to_ask), AUDIT_BATCH):
                batch = to_ask[i:i + AUDIT_BATCH]
                lines = [f"id {n}\ntext: {it['text']}\nextracted: {it['extracted']}" for n, it in enumerate(batch)]
                response = client.messages.create(model=MODEL, max_tokens=4000, system=AUDIT_SYSTEM,
                                                  messages=[{"role": "user", "content": "\n\n".join(lines)}])
                totals["calls"] += 1
                totals["tokens_in"] += response.usage.input_tokens
                totals["tokens_out"] += response.usage.output_tokens
                parsed = _parse_json(response.content[0].text if response.content else "")
                by_id = {str(x.get("id")): x for x in parsed.get("items") or [] if isinstance(x, dict)}
                for n, it in enumerate(batch):
                    it["answer"] = by_id.get(str(n)) or {"wrong": [], "missing": []}
                    _cache_put(conn, it["key"], "audit", AUDIT_PROMPT, it["text"], it["answer"])
        for it in chosen:
            answer = it.get("answer") or {}
            problems = []
            for kind in ("wrong", "missing"):
                for p in answer.get(kind) or []:
                    evidence = str((p or {}).get("evidence") or "").strip()
                    if not evidence or evidence.lower() not in it["text"].lower():
                        continue                                  # no real quote from the text: ignored
                    problems.append({"type": kind, "box": str(p.get("box") or ""),
                                     "extracted": str(p.get("extracted") or ""),
                                     "ai_says": str(p.get("correct") or p.get("value") or ""), "evidence": evidence})
            if problems:
                totals["with_problem"] += 1
            for p in problems or [{"type": "ok", "box": "", "extracted": "", "ai_says": "", "evidence": ""}]:
                findings.append({"item": it["label"], "kind": it["kind"], "text": it["text"],
                                 "all_extracted": it["extracted"], **p})
        _usage(conn, run_id, "audit", rows_total=len(items), rows_flagged=len(chosen), rows_sent=len(to_ask),
               cache_hits=totals["cache_hits"], calls=totals["calls"], tokens_in=totals["tokens_in"],
               tokens_out=totals["tokens_out"])
    finally:
        conn.close()

    path = out or f"attr_audit_run{run_id}.xlsx"
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Audit"
    cols = ["item", "kind", "type", "box", "extracted", "ai_says", "evidence", "text", "all extracted"]
    ws.append(cols)
    for f in findings:
        ws.append([f["item"], f["kind"], f["type"], f["box"], f["extracted"], f["ai_says"], f["evidence"],
                   f["text"], f["all_extracted"]])
    for i, w in enumerate((12, 9, 9, 10, 18, 18, 24, 70, 50), 1):
        ws.column_dimensions[chr(64 + i)].width = w
    for row in ws.iter_rows():
        for cell in row:
            cell.font = Font(name="Arial", size=10, bold=cell.row == 1)
            cell.alignment = Alignment(vertical="top", wrap_text=cell.column in (8, 9))
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)
    totals["workbook"] = path
    totals["cost_usd"] = cost_usd(totals["tokens_in"], totals["tokens_out"])
    totals["by_box"] = {}
    for f in findings:
        if f["type"] != "ok":
            totals["by_box"][f"{f['type']} {f['box']}"] = totals["by_box"].get(f"{f['type']} {f['box']}", 0) + 1
    return totals


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[4] / ".env")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="AI help for attribute extraction (router, cache, suggestions, audit)")
    ap.add_argument("--run", type=int, help="run id")
    ap.add_argument("--enrich", action="store_true", help="expand abbreviated vendor titles of the run")
    ap.add_argument("--force", action="store_true", help="with --enrich: redo rows that already have an expansion")
    ap.add_argument("--dry-run", action="store_true", help="with --enrich: print, save nothing on the rows")
    ap.add_argument("--colors", action="store_true", help="suggest base colors for unknown color names of the run")
    ap.add_argument("--suggestions", action="store_true", help="list suggestions")
    ap.add_argument("--kind", default="", help="abbreviation | color")
    ap.add_argument("--status", default="pending", help="pending | accepted | rejected | '' for all")
    ap.add_argument("--min-seen", type=int, default=1, help="only suggestions seen at least this often")
    ap.add_argument("--accept", default="", help="comma-separated suggestion ids")
    ap.add_argument("--reject", default="", help="comma-separated suggestion ids")
    ap.add_argument("--accept-all", action="store_true", help="accept every pending suggestion of --kind")
    ap.add_argument("--audit", action="store_true", help="sampled audit of the rules for --run")
    ap.add_argument("--sample", type=int, default=50, help="with --audit: how many items")
    ap.add_argument("--out", default="", help="with --audit: workbook path")
    ap.add_argument("--usage", action="store_true", help="AI usage per run")
    a = ap.parse_args(argv)

    needs_switch = a.enrich or a.colors or a.audit
    if needs_switch and not enabled():
        os.environ["MATCH_AI_TITLE_ATTRS"] = "1"        # the command itself is the explicit "yes"
        print("(MATCH_AI_TITLE_ATTRS is not set in .env; switched on for this command only)")
    if (a.enrich or a.colors or a.audit) and not a.run:
        ap.error("--run is required")
    if a.enrich:
        s = enrich_run(a.run, force=a.force, dry_run=a.dry_run)
        print(f"run {a.run}: {s['rows_total']} titles | rules were enough: {s['rules_enough']} | already read: "
              f"{s['already_done']} | needed help: {s['rows_flagged']} "
              f"({', '.join(f'{k} {n}' for k, n in s['reasons'].items()) or '-'})")
        print(f"  learned rules only (free): {s['rows_learned_only']} | from cache: {s['cache_hits']} | sent to the AI: "
              f"{s['rows_sent']} in {s['calls']} call(s), ~${s['cost_usd']}")
        print(f"  rows where the AI found something: {s['rows_with_result']} ({s['attributes_found']} attributes); "
              f"{'nothing saved (dry run)' if a.dry_run else str(s['saved']) + ' rows saved'}")
    if a.colors:
        s = suggest_color_names(a.run)
        print(f"run {a.run}: {s['names']} unknown color names, {s['cache_hits']} from cache, {s['asked']} asked, "
              f"{s['suggested']} suggestions, ~${s['cost_usd']}")
    if a.accept:
        for note in decide([int(x) for x in a.accept.split(",") if x.strip()], accept=True):
            print("  " + note)
    if a.reject:
        for note in decide([int(x) for x in a.reject.split(",") if x.strip()], accept=False):
            print("  " + note)
    if a.accept_all:
        if not a.kind:
            ap.error("--accept-all needs --kind abbreviation or --kind color")
        ids = [r["id"] for r in list_suggestions(a.kind, "pending", a.min_seen)]
        for note in decide(ids, accept=True):
            print("  " + note)
    if a.suggestions:
        rows = list_suggestions(a.kind, a.status, a.min_seen)
        print(f"{len(rows)} suggestion(s)")
        for r in rows:
            print(f"  {r['id']:>5}  {r['kind']:12s} {r['key']:>14s} = {r['value']:<24s} seen {r['seen']:>3}  "
                  f"{r['status']:9s} e.g. {str(r['example'])[:60]}")
    if a.accept or a.reject or a.accept_all:
        print("Restart the app so it reads the new words.")
    if a.audit:
        s = audit_run(a.run, sample=a.sample, out=a.out or None)
        print(f"run {a.run}: audited {s['items']} items, {s['with_problem']} with a problem the AI could quote, "
              f"{s['calls']} call(s), ~${s['cost_usd']}  -> {s['workbook']}")
        for k, n in sorted(s["by_box"].items(), key=lambda kv: -kv[1]):
            print(f"    {k}: {n}")
    if a.usage:
        conn = _db()
        try:
            rows = conn.execute("SELECT run_id, task, SUM(rows_total) t, SUM(rows_sent) s, SUM(cache_hits) c, "
                                "SUM(calls) k, ROUND(SUM(cost_usd), 4) usd FROM attr_ai_usage "
                                "GROUP BY run_id, task ORDER BY run_id DESC, task").fetchall()
        finally:
            conn.close()
        print("run   task       rows   sent  cache  calls   cost")
        for r in rows:
            print(f"{r['run_id']!s:>4}  {r['task']:9s} {r['t'] or 0:>5}  {r['s'] or 0:>5}  {r['c'] or 0:>5}  "
                  f"{r['k'] or 0:>5}  ${r['usd'] or 0}")
    return 0


if __name__ == "__main__":
    sys.exit(main())