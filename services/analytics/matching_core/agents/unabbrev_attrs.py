"""
Unabbreviation agent for ATTRIBUTE EXTRACTION (not for search).

Problem: vendor titles like "OXO GG SS Peeler BLK" or "KWS 916 SN PRIV LEVER" hide their attributes in
abbreviations, so the attribute reader (attr_extract.py) cannot see "black", "stainless steel",
"satin nickel", "privacy".

How it works (the LLM never writes the title itself, so it cannot add information):
  1. Pick the vendor rows whose title has abbreviation-looking words (BLK, GG, SS, WHT, PRIV, BAT. ...).
  2. Send them to the same Anthropic model the query agent uses, 20 titles per call.
     The model returns PAIRS only:  {"from": "BLK", "to": "Black"}.
  3. The code checks every pair: "from" must be a word that really is in the title, "to" must be a short
     plain phrase.  Bad pairs are dropped.
  4. The code applies the pairs to the title  ->  "OXO Good Grips Stainless Steel Peeler Black".
  5. The result is saved on the vendor row:  analytics_catalog_rows.data_json["ai_unabbrev"]
       {"expanded_title", "pairs", "dropped", "model", "prompt_version", "at"}
     attr_extract.py (v10) reads it as an extra, low-trust source called "ai_title" when the switch
     MATCH_AI_TITLE_ATTRS=1 is set.  Values from it are marked "soft": they may send a pair to Review,
     never to Rejected (needs the matching check in variant_gate.py).

Commands (repo root):
  uv run python -m services.analytics.matching_core.agents.unabbrev_attrs --run 12 --list
        which rows would be sent (no LLM call, free)
  uv run python -m services.analytics.matching_core.agents.unabbrev_attrs --run 12 --dry-run
        call the LLM and print the expansions, write nothing (costs tokens)
  uv run python -m services.analytics.matching_core.agents.unabbrev_attrs --run 12
        call the LLM and save the expansions on the vendor rows; then Re-score the run
  options: --all (send every row, not only abbreviated ones), --force (redo rows that already have one),
           --limit N (first N rows only)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

PROMPT_VERSION = "unabbrev-attrs-v1"
MODEL = os.getenv("UNABBREV_MODEL", "claude-sonnet-4-6")      # same model as the query agent by default
BATCH_SIZE = 20
MAX_TO_WORDS = 5
FILE_VERSION = "2026-10-22 unabbrev_attrs v1"

SYSTEM = """\
You expand abbreviations in vendor product titles so that a program can read the product's attributes
(color, finish, size, flavor, scent, material, function type, count, unit).

The catalogues are consumer packaged goods, medical supplies, kitchenware and door hardware.
Their titles are terse and full of abbreviations.

Rules:
- Only expand words that ARE written in the title. Never add information that is not there.
- Answer with pairs: "from" = the token exactly as written in the title, "to" = the full word(s).
- Leave brand names, model numbers, part numbers, SKUs and ordinary words alone. Do not list them.
- If you are not sure what an abbreviation means in this title, do not guess: put it in "unsure".
- Keep "to" short: at most 5 words.

Real examples from these catalogues:
  BLK -> Black            WHT -> White            GRY / GRY. -> Gray        NAT -> Natural
  SS -> Stainless Steel   SN -> Satin Nickel      VB -> Venetian Bronze     ORB -> Oil Rubbed Bronze
  PRIV -> Privacy         PASS -> Passage         GG -> Good Grips          LG -> Large
  SM -> Small             MED -> Medium           QT -> Quart               GL / GAL -> Gallon
  PK -> Pack              CT -> Count             BAT. -> Battery           ALK. -> Alkaline
  DISP -> Disposable      STRL -> Sterile         LTX -> Latex              VAN -> Vanilla
  CHOC -> Chocolate       STRAW -> Strawberry     UNSCNT -> Unscented       LAV -> Lavender

Reply with JSON only, no prose:
{"items": [{"id": "<id>", "expansions": [{"from": "BLK", "to": "Black"}], "unsure": ["XQZ"]}]}
"""

_VOWELS = set("aeiouy")
_COMMON = frozenset((
    "and the for with of in to by new a an or on at no pro plus max mini ultra extra super pack set kit "
    "oz ml l lb lbs g kg mg mcg fl ct pc pcs ea in ft mm cm yd qty usa us led usb lcd tv dvd cd ac dc uv "
    "xl xxl xs xxs s m l"
).split())


# --------------------------------------------------------------------------- #
# Which titles look abbreviated
# --------------------------------------------------------------------------- #

def _known_words() -> frozenset[str]:
    try:
        from services.analytics.matching_core.matching.lexicon import get_lexicon
        lex = get_lexicon()
        return frozenset(set(lex.colors) | set(lex.flavors) | set(lex.scents) | set(lex.sizes)
                         | set(lex.gender) | set(lex.line_filler) | set(lex.tones) | _COMMON)
    except Exception:  # noqa: BLE001
        return _COMMON


def abbreviated_tokens(title: Any, brand: Any = "") -> list[str]:
    """Words of the title that look like abbreviations: no vowel ("BLK", "WHT", "SS"), a short all-caps word
    ("GG", "PRIV", "SUD"), or a word ending in a dot ("BAT.", "ALK.").  Brand words and known words are skipped."""
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


# --------------------------------------------------------------------------- #
# Checking and applying the pairs (plain code, no LLM)
# --------------------------------------------------------------------------- #

def _token_re(token: str) -> re.Pattern:
    """The token as a whole word, with or without a trailing dot ("BAT" and "BAT." both match)."""
    core = re.escape(token.rstrip("."))
    return re.compile(rf"(?<![A-Za-z0-9]){core}\.?(?![A-Za-z0-9])", re.IGNORECASE)


def check_pairs(title: str, pairs: Any) -> tuple[list[dict], list[dict]]:
    """(kept, dropped).  Kept: "from" really is a word of the title, "to" is a short plain phrase that differs."""
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
    for p in sorted(pairs, key=lambda q: -len(q["from"])):        # longer tokens first: "GRY." before "GR"
        out = _token_re(p["from"]).sub(p["to"], out)
    return re.sub(r"\s+", " ", out).strip()


# --------------------------------------------------------------------------- #
# The LLM call
# --------------------------------------------------------------------------- #

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
    return Anthropic(api_key=key, max_retries=3, timeout=60.0)


def expand_batch(client: Any, rows: list[dict]) -> tuple[dict[str, dict], dict]:
    """rows: [{"id", "title", "brand", "mpn"}]  ->  ({id: raw model answer}, usage)"""
    lines = []
    for r in rows:
        extra = f" | vendor part number: {r['mpn']}" if r.get("mpn") else ""
        lines.append(f'id {r["id"]} | brand: {r.get("brand") or "-"} | title: {r["title"]}{extra}')
    started = time.monotonic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": "Titles:\n" + "\n".join(lines)}],
    )
    usage = {"tokens_in": response.usage.input_tokens, "tokens_out": response.usage.output_tokens,
             "seconds": round(time.monotonic() - started, 2)}
    parsed = _parse_json(response.content[0].text if response.content else "")
    answers = {str(it.get("id")): it for it in parsed.get("items") or [] if isinstance(it, dict)}
    return answers, usage


def expand_rows(rows: list[dict], client: Any = None) -> tuple[dict[str, dict], dict]:
    """rows: [{"id", "title", "brand", "mpn"}]  ->  ({id: ai_unabbrev record}, totals)"""
    client = client or _client()
    results: dict[str, dict] = {}
    totals = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "seconds": 0.0}
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        answers, usage = expand_batch(client, batch)
        totals["calls"] += 1
        for k in ("tokens_in", "tokens_out", "seconds"):
            totals[k] += usage[k]
        for r in batch:
            ans = answers.get(str(r["id"])) or {}
            kept, dropped = check_pairs(r["title"], ans.get("expansions"))
            results[str(r["id"])] = {
                "expanded_title": apply_pairs(r["title"], kept) if kept else r["title"],
                "original_title": r["title"],
                "pairs": kept,
                "dropped": dropped,
                "unsure": [str(u) for u in ans.get("unsure") or []][:20],
                "model": MODEL,
                "prompt_version": PROMPT_VERSION,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
    totals["seconds"] = round(totals["seconds"], 2)
    return results, totals


def cost_usd(tokens_in: int, tokens_out: int) -> float:
    """Claude Sonnet 4.6 list price: $3 / 1M input, $15 / 1M output."""
    return round(tokens_in * 3 / 1_000_000 + tokens_out * 15 / 1_000_000, 4)


# --------------------------------------------------------------------------- #
# Database: one run
# --------------------------------------------------------------------------- #

def _json(text: Any) -> dict:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def rows_to_send(run_id: Any, send_all: bool = False, force: bool = False, limit: int = 0) -> list[dict]:
    from services import database
    conn = database._connect()
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute("SELECT row_idx, data_json FROM analytics_catalog_rows WHERE run_id = ? "
                             "ORDER BY row_idx", (run_id,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in found:
        data = _json(r["data_json"])
        title = str(data.get("title") or "").strip()
        if not title or (data.get("ai_unabbrev") and not force):
            continue
        tokens = abbreviated_tokens(title, data.get("brand"))
        if not tokens and not send_all:
            continue
        out.append({"id": str(r["row_idx"]), "row_idx": r["row_idx"], "title": title,
                    "brand": str(data.get("brand") or ""), "mpn": str(data.get("mpn") or data.get("itemid") or ""),
                    "tokens": tokens})
        if limit and len(out) >= limit:
            break
    return out


def save(run_id: Any, results: dict[str, dict]) -> int:
    from services import database
    written = 0
    with database._LOCK:
        conn = database._connect()
        conn.row_factory = sqlite3.Row
        try:
            for rid, record in results.items():
                row = conn.execute("SELECT data_json FROM analytics_catalog_rows WHERE run_id = ? AND row_idx = ?",
                                   (run_id, int(rid))).fetchone()
                if row is None:
                    continue
                data = _json(row["data_json"])
                data["ai_unabbrev"] = record
                conn.execute("UPDATE analytics_catalog_rows SET data_json = ? WHERE run_id = ? AND row_idx = ?",
                             (json.dumps(data), run_id, int(rid)))
                written += 1
            conn.commit()
        finally:
            conn.close()
    return written


def main(argv: list[str] | None = None) -> int:
    try:                                             # .env at the repo root (ANTHROPIC_API_KEY)
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[4] / ".env")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="Unabbreviate vendor titles for attribute extraction")
    ap.add_argument("--run", required=True, help="run id")
    ap.add_argument("--list", action="store_true", help="only list the rows that would be sent (free)")
    ap.add_argument("--dry-run", action="store_true", help="call the LLM, print, write nothing")
    ap.add_argument("--all", action="store_true", help="send every row, not only abbreviated titles")
    ap.add_argument("--force", action="store_true", help="redo rows that already have an expansion")
    ap.add_argument("--limit", type=int, default=0, help="first N rows only")
    a = ap.parse_args(argv)

    rows = rows_to_send(a.run, send_all=a.all, force=a.force, limit=a.limit)
    print(f"run {a.run}: {len(rows)} vendor row(s) to send  ({FILE_VERSION}, model {MODEL})")
    if a.list or not rows:
        for r in rows:
            print(f"  row {int(r['row_idx']) + 1:>5}  {r['title'][:80]}   abbreviations: {', '.join(r['tokens']) or '-'}")
        return 0

    results, totals = expand_rows(rows)
    for r in rows:
        rec = results.get(r["id"]) or {}
        print(f"  row {int(r['row_idx']) + 1:>5}  {r['title'][:70]}")
        print(f"         -> {rec.get('expanded_title', '')[:90]}")
        if rec.get("pairs"):
            print("            " + "; ".join(f"{p['from']} = {p['to']}" for p in rec["pairs"]))
        if rec.get("dropped"):
            print("            dropped: " + "; ".join(f"{p['from']} = {p['to']} ({p['reason']})" for p in rec["dropped"]))
        if rec.get("unsure"):
            print("            unsure: " + ", ".join(rec["unsure"]))
    print(f"\n{totals['calls']} LLM call(s), {totals['tokens_in']} tokens in, {totals['tokens_out']} out, "
          f"~${cost_usd(totals['tokens_in'], totals['tokens_out'])}, {totals['seconds']}s")
    if a.dry_run:
        print("DRY RUN: nothing written.")
        return 0
    print(f"saved on {save(a.run, results)} vendor row(s). Set MATCH_AI_TITLE_ATTRS=1 in .env, restart, then Re-score.")
    return 0


if __name__ == "__main__":
    sys.exit(main())