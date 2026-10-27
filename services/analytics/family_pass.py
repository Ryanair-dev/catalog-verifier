from __future__ import annotations

import argparse
import copy
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from typing import Any

from services.analytics.vendor_families import (
    EXACT,
    UNSTATED_BOXES,
    _names,
    _states,
    build_families,
    load_run,
    row_label,
)

FILE_VERSION = "2026-10-28 family_pass v2 (parent ASIN siblings)"

DEDUPE_STATUS = "ASIN claimed"
SETTLE_STATUS = "Variant settled"
SIBLINGS_STATUS = "Several variants approved"
OUR_STATUSES = frozenset({DEDUPE_STATUS, SETTLE_STATUS, SIBLINGS_STATUS})
# statuses written by automatic rules, not by a person ("ASIN Exclusivity" = a legacy rule in routers/analytics.py)
AUTO_STATUSES = OUR_STATUSES | {"ASIN Exclusivity"}
DEDUPE_CAP = 89.0
SETTLE_CAP = 29.0
SIBLINGS_CAP = 89.0


def _key(c: dict) -> tuple:
    return (c.get("row_idx"), str(c.get("asin") or ""))


def _status(c: dict) -> str:
    return str(c.get("review_status") or "").strip()


def _is_human(c: dict) -> bool:
    s = _status(c)
    return bool(s) and s not in AUTO_STATUSES


def _record(c: dict) -> dict | None:
    d = (c.get("data") or {}).get("family_pass")
    return d if isinstance(d, dict) else None


def _base(c: dict) -> tuple[str, str, float | None]:
    """(verdict, review_status, confidence) as they were BEFORE this pass touched the pair."""
    d = _record(c)
    if _status(c) in OUR_STATUSES and d:
        return str(d.get("was") or c.get("verdict") or ""), str(d.get("was_status") or ""), d.get("was_confidence")
    status = "" if _status(c) in OUR_STATUSES else _status(c)
    return str(c.get("verdict") or ""), status, c.get("confidence")


def _rows_text(rows: list) -> str:
    return ", ".join(str(row_label(r)) for r in rows)


# --------------------------------------------------------------------------- #
# Deciding (pure: no database)
# --------------------------------------------------------------------------- #

def _dedup(candidates: list[dict], fam: dict) -> tuple[dict[tuple, dict], list[dict]]:
    """{key: {"keep", "reason"}} for every claim of a listing, and the listings claimed across families."""
    fam_by_id = {f["id"]: f for f in fam["families"]}
    by_asin: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        if _base(c)[0] == "verified":
            by_asin[str(c.get("asin") or "")].append(c)

    decisions: dict[tuple, dict] = {}
    cross: list[dict] = []
    for asin, claims in by_asin.items():
        groups: dict[str, list[dict]] = defaultdict(list)
        for c in claims:
            row = fam["rows"].get(c.get("row_idx"))
            groups[row["family"] if row else f"#row{c.get('row_idx')}"].append(c)
        if len(groups) > 1:
            cross.append({"asin": asin, "families": {fid: sorted(x.get("row_idx") for x in g)
                                                     for fid, g in groups.items()}})
        for fid, group in groups.items():
            if len(group) > 1:
                decisions.update(_decide_group(asin, group, fam, fam_by_id.get(fid)))
    return decisions, cross


def _decide_group(asin: str, group: list[dict], fam: dict, family: dict | None) -> dict[tuple, dict]:
    out: dict[tuple, dict] = {}

    def pair(c: dict) -> dict:
        return fam["pairs"].get(_key(c)) or {}

    def this_row(c: dict) -> str:
        p = pair(c)
        return f"this row: {p.get('relation', '?')}" + (f" ({p['detail']})" if p.get("detail") else "")

    humans = [c for c in group if _is_human(c)]
    if humans:
        owners = sorted(c.get("row_idx") for c in humans)
        for c in group:
            out[_key(c)] = ({"keep": True} if c in humans else
                            {"keep": False, "reason": f"listing {asin} was approved by a person for row {_rows_text(owners)}"})
        return out

    listing = fam["listings"].get(asin) or {}
    proven = [c for c in group if pair(c).get("relation") == EXACT and pair(c).get("proof")]
    if proven:
        with_code = [c for c in proven if pair(c).get("barcode")]
        keeper = min(with_code or proven, key=lambda c: c.get("row_idx"))
        k = row_label(keeper.get("row_idx"))
        for c in group:
            if c is keeper:
                out[_key(c)] = {"keep": True}
            elif c in proven:
                if c in with_code:
                    reason = f"same barcode as row {k} (duplicate row); row {k} keeps listing {asin}"
                elif with_code:
                    reason = f"row {k} carries the barcode of listing {asin}; this row matches by variant only"
                else:
                    reason = f"same variant as row {k} (duplicate row); row {k} keeps listing {asin}"
                out[_key(c)] = {"keep": False, "reason": reason}
            else:
                out[_key(c)] = {"keep": False, "reason": f"row {k} matches listing {asin} "
                                                         f"({listing.get('variant') or 'no variant'}); {this_row(c)}"}
        return out

    varies = []
    for box in (family or {}).get("varying", {}):
        b = "units" if box == "count" else box
        if b in UNSTATED_BOXES and b not in varies:
            varies.append(b)
    missing = [b for b in varies if not _states(listing.get("fields") or {}, b)]
    head = (f"listing {asin} does not state {_names(missing)}, so it cannot be told which row of the family it is"
            if missing else f"no row's variant matches listing {asin} ({listing.get('variant') or 'no variant'})")
    for c in group:
        out[_key(c)] = {"keep": False, "reason": f"{head}; {this_row(c)}"}
    return out


def _siblings(candidates: list[dict], fam: dict) -> dict[tuple, str]:
    """{key: reason} for automatically approved pairs that must go to Review because the same vendor row was
    approved for several variants of one Amazon product (listings sharing a parent ASIN)."""
    listings = fam["listings"]
    by_row: dict[Any, list[dict]] = defaultdict(list)
    for c in candidates:
        approved = (str(c.get("verdict") or "") == "verified") if _is_human(c) else (_base(c)[0] == "verified")
        if approved:
            by_row[c.get("row_idx")].append(c)
    out: dict[tuple, str] = {}
    for group in by_row.values():
        by_parent: dict[str, list[dict]] = defaultdict(list)
        for c in group:
            parent = (listings.get(str(c.get("asin") or "")) or {}).get("parent")
            if parent:
                by_parent[parent].append(c)
        for parent, members in by_parent.items():
            if len(members) < 2:
                continue
            anchors = [c for c in members if (fam["pairs"].get(_key(c)) or {}).get("barcode") or _is_human(c)]
            movable = [c for c in members if c not in anchors and not _is_human(c)]
            if anchors:
                names = ", ".join(str(c.get("asin")) for c in anchors)
                why = "it carries this row's barcode" if any((fam["pairs"].get(_key(c)) or {}).get("barcode")
                                                            for c in anchors) else "a person approved it"
                reason = f"another variant of {names}, which is this row's product ({why})"
            else:
                names = ", ".join(sorted(str(c.get("asin")) for c in members))
                reason = (f"{len(members)} variants of one Amazon product ({parent}) were approved for this row "
                          f"({names}); at most one can be right")
            for c in movable:
                out[_key(c)] = reason
    return out


def plan_changes(vendor_rows: list[dict], candidates: list[dict], families: dict | None = None) -> dict:
    """{"changes": [...], "cross_family": [...], "counts": {...}}.  Pure: nothing is written."""
    fam = families or build_families(vendor_rows, candidates)
    dedup, cross = _dedup(candidates, fam)
    siblings = _siblings(candidates, fam)
    changes: list[dict] = []
    for c in candidates:
        if _is_human(c):
            continue
        key = _key(c)
        base_verdict, base_status, base_conf = _base(c)
        pair = fam["pairs"].get(key) or {}
        target_verdict, target_status, kind, reason = base_verdict, base_status, None, ""
        dec = dedup.get(key)
        if dec and not dec["keep"]:
            target_verdict, target_status, kind, reason = "review", DEDUPE_STATUS, "dedup", dec["reason"]
        elif key in siblings:
            target_verdict, target_status, kind, reason = "review", SIBLINGS_STATUS, "siblings", siblings[key]
        elif base_verdict == "verified" and pair.get("proof") == "parent" and not pair.get("barcode"):
            target_verdict, target_status, kind = "review", SIBLINGS_STATUS, "siblings"
            reason = pair.get("detail") or "another variant of this row's barcode match"
        elif base_verdict == "review" and pair.get("settle") and not pair.get("barcode"):
            target_verdict, target_status, kind = "not_approved", SETTLE_STATUS, "settle"
            reason = f"another variant of the same product: {pair.get('detail')}"

        current_status = _status(c)
        record = _record(c)
        same_state = (str(c.get("verdict") or "") == target_verdict and current_status == target_status)
        if kind and same_state and record and record.get("reason") == reason:
            continue                                   # already done, same reason
        if not kind and same_state and not record:
            continue                                   # nothing to do
        changes.append(_change(c, key, kind, reason, target_verdict, target_status,
                               base_verdict, base_status, base_conf))
    counts = Counter(ch["what"] for ch in changes)
    return {"changes": changes, "cross_family": cross, "counts": dict(counts), "families": fam}


def _summary_get(data: dict) -> str | None:
    exp = (data.get("scores") or {}).get("explanation") if isinstance(data.get("scores"), dict) else None
    if isinstance(exp, dict):
        value = exp.get("summary")
        return value if isinstance(value, str) else None
    return exp if isinstance(exp, str) else None


def _summary_set(data: dict, text: str | None) -> None:
    if text is None or not isinstance(data.get("scores"), dict):
        return
    exp = data["scores"].get("explanation")
    if isinstance(exp, dict):
        exp["summary"] = text
    elif isinstance(exp, str):
        data["scores"]["explanation"] = text


def _change(c: dict, key: tuple, kind: str | None, reason: str, verdict: str, status: str,
            base_verdict: str, base_status: str, base_conf: Any) -> dict:
    data = copy.deepcopy(c.get("data") or {})
    record = _record(c)
    summary_before = record.get("summary_before") if record else _summary_get(data)
    try:
        conf = float(base_conf) if base_conf is not None else float(c.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if kind:
        cap = {"dedup": DEDUPE_CAP, "siblings": SIBLINGS_CAP}.get(kind, SETTLE_CAP)
        data["family_pass"] = {"kind": kind, "reason": reason, "was": base_verdict, "was_status": base_status,
                               "was_confidence": conf, "summary_before": summary_before, "version": FILE_VERSION}
        if summary_before is not None:
            _summary_set(data, f"{summary_before.rstrip()} Family check: {reason}.")
        new_conf = min(conf, cap)
        what = kind
    else:
        data.pop("family_pass", None)
        if record and summary_before is not None:
            _summary_set(data, summary_before)
        new_conf = conf
        what = "restore"
    data["verdict"] = verdict
    if isinstance(data.get("scores"), dict):
        data["scores"]["verdict"] = verdict
    return {"key": key, "what": what, "verdict": verdict, "review_status": status or None,
            "confidence": round(new_conf, 1), "data": data, "reason": reason or "the earlier reason no longer applies"}


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

def _apply(run_id: Any, changes: list[dict]) -> None:
    from services import database
    with database._LOCK:
        conn = database._connect()
        try:
            for ch in changes:
                row_idx, asin = ch["key"]
                conn.execute("UPDATE analytics_candidates SET verdict = ?, review_status = ?, confidence = ?, "
                             "data_json = ? WHERE run_id = ? AND row_idx = ? AND asin = ?",
                             (ch["verdict"], ch["review_status"], ch["confidence"],
                              json.dumps(ch["data"], default=str), run_id, row_idx, asin))
            counts = {"verified": 0, "review": 0, "not_approved": 0}
            total = 0
            for verdict, n in conn.execute("SELECT verdict, COUNT(*) FROM analytics_candidates "
                                           "WHERE run_id = ? GROUP BY verdict", (run_id,)).fetchall():
                total += int(n)
                if str(verdict or "").lower() in counts:
                    counts[str(verdict).lower()] += int(n)
            conn.execute("UPDATE analytics_runs SET total_candidates_found = ?, verified_count = ?, review_count = ?, "
                         "not_approved_count = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                         (total, counts["verified"], counts["review"], counts["not_approved"], run_id))
            conn.commit()
        finally:
            conn.close()


def run_family_pass(run_id: Any, dry_run: bool = False) -> dict:
    """Plan and (unless dry_run) write the family decisions for one run.  Returns a summary."""
    vendor_rows, candidates = load_run(run_id)
    if not vendor_rows:
        return {"run_id": run_id, "changes": [], "counts": {}, "cross_family": [], "statuses": {}}
    planned = plan_changes(vendor_rows, candidates)
    if planned["changes"] and not dry_run:
        _apply(run_id, planned["changes"])
    return {
        "run_id": run_id,
        "dry_run": dry_run,
        "changes": planned["changes"],
        "counts": planned["counts"],
        "cross_family": planned["cross_family"],
        "statuses": dict(Counter(_status(c) or "(empty)" for c in candidates)),
        "version": FILE_VERSION,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Family pass (dedup + settle + siblings) for one run")
    ap.add_argument("--run", required=True, help="run id")
    ap.add_argument("--dry-run", action="store_true", help="show the changes, write nothing")
    a = ap.parse_args(argv)
    s = run_family_pass(a.run, dry_run=a.dry_run)
    print(f"run {a.run}  ({FILE_VERSION})  {'DRY RUN, nothing written' if a.dry_run else 'changes written'}")
    print(f"  changes: {s['counts'] or 'none'}   (dedup = lost a listing to another row, settle = Review -> "
          f"Not Approved, siblings = approved variant of the same Amazon product -> Review, "
          f"restore = an earlier change undone)")
    for ch in s["changes"]:
        row_idx, asin = ch["key"]
        print(f"  row {row_label(row_idx)!s:>5}  {asin}  {ch['what']:8s} -> {ch['verdict']} / {ch['review_status'] or '-'}")
        print(f"        {ch['reason']}")
    if s["cross_family"]:
        print(f"\n  listings approved for rows of DIFFERENT families (not changed, check by hand): {len(s['cross_family'])}")
        for x in s["cross_family"]:
            print(f"    {x['asin']}: " + "; ".join(f"{fid} rows {_rows_text(r)}" for fid, r in x["families"].items()))
    print("\n  review_status values in this run (anything not in "
          f"{sorted(AUTO_STATUSES)} and not empty is a person's decision and is never changed):")
    for value, n in sorted(s["statuses"].items(), key=lambda kv: -kv[1]):
        print(f"    {value!r}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
