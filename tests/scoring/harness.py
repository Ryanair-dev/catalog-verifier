"""Shared code for the scoring tests: load the test pairs, score them offline, compare with the baseline.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

ANCHORS = HERE / "anchors.json"
BRANDS = HERE / "learned_brands.json"
BASELINE = HERE / "baseline.json"

MATCHERS = ("cascade", "weighted")      # the classifier is left out: retraining changes it on purpose
MODE = "medical"
# What each outcome costs.  A correct pair that is approved costs nothing, a wrong pair rejected costs nothing.
WEIGHTS = {"wrong_approved": -10, "correct_rejected": -3, "correct_review": -1, "wrong_review": 0}


def anchor_id(row: dict) -> str:
    return f"{row.get('_source')}|{row['offer'].get('offer_id')}|{row['candidate'].get('asin')}"


def load_anchors() -> list[dict]:
    return json.loads(ANCHORS.read_text(encoding="utf-8"))


def use_scoring_db(folder: Path) -> Path:
    """Point the app at a fresh database in `folder` that holds only the frozen learned brands."""
    from services import database
    database.DB_PATH = Path(folder) / "scoring_test.db"
    if database.DB_PATH.exists():
        database.DB_PATH.unlink()
    database.init_db()
    import services.analytics.brand_families as bf
    bf._db().close()                                    # creates the brand tables
    brands = json.loads(BRANDS.read_text(encoding="utf-8")) if BRANDS.is_file() else []
    with database._connect() as conn:
        for b in brands:
            conn.execute(
                "INSERT OR IGNORE INTO brand_families(sub_key, parent_key, sub_brand, parent_brand, status) "
                "VALUES (?,?,?,?, 'active')",
                (bf._key(b["sub_brand"]), bf._key(b["parent_brand"]), b["sub_brand"], b["parent_brand"]))
    bf._invalidate()
    return database.DB_PATH


def score(anchors: list[dict]) -> dict[str, dict[str, str]]:
    """{matcher: {anchor id: verdict}}, scored exactly like the eval (same variant check)."""
    import eval_matchers as em
    from services.analytics.matching_core.models import Candidate, Offer
    pairs = [em.Pair(em._build(Offer, r["offer"]), em._build(Candidate, r["candidate"]), bool(r["is_match"]))
             for r in anchors]
    out = {}
    for name in MATCHERS:
        results = em.run_matcher(name, pairs, MODE)
        out[name] = {anchor_id(r): res.verdict.value for r, res in zip(anchors, results)}
    return out


def outcome(is_match: bool, verdict: str) -> str:
    pile = {"verified": "approved", "review": "review"}.get(verdict, "rejected")
    return f"{'correct' if is_match else 'wrong'}_{pile}"


def summary(anchors: list[dict], verdicts: dict[str, str]) -> dict:
    """Outcome counts per category and overall, plus the weighted score."""
    per_cat: dict[str, Counter] = defaultdict(Counter)
    for r in anchors:
        per_cat[r["category"]][outcome(bool(r["is_match"]), verdicts[anchor_id(r)])] += 1
    total = sum(per_cat.values(), Counter())
    return {"score": sum(WEIGHTS.get(k, 0) * v for k, v in total.items()),
            "total": dict(total), "per_category": {k: dict(v) for k, v in sorted(per_cat.items())}}


def changes(anchors: list[dict], before: dict[str, str], now: dict[str, str]) -> dict[str, list[str]]:
    """Pairs whose outcome changed, grouped: new wrong approvals, correct pairs newly rejected, other."""
    found = {"new_wrong_approvals": [], "correct_newly_rejected": [], "other_changes": []}
    for r in anchors:
        aid = anchor_id(r)
        if aid not in before or before[aid] == now[aid]:
            continue
        was, isn = outcome(bool(r["is_match"]), before[aid]), outcome(bool(r["is_match"]), now[aid])
        line = (f"[{r['category']}] {aid}: {was} -> {isn}\n      vendor: {r['offer'].get('title', '')[:80]}"
                f"\n      amazon: {r['candidate'].get('title', '')[:80]}")
        if isn == "wrong_approved":
            found["new_wrong_approvals"].append(line)
        elif isn == "correct_rejected":
            found["correct_newly_rejected"].append(line)
        else:
            found["other_changes"].append(line)
    return found


def table(title: str, base: dict, now: dict) -> str:
    keys = ("correct_approved", "correct_review", "correct_rejected", "wrong_approved", "wrong_review", "wrong_rejected")
    lines = [title, f"  {'category':26} " + " ".join(f"{k:>16}" for k in keys)]
    for cat in sorted(set(base["per_category"]) | set(now["per_category"])):
        b, n = base["per_category"].get(cat, {}), now["per_category"].get(cat, {})
        cells = []
        for k in keys:
            x, y = b.get(k, 0), n.get(k, 0)
            cells.append(f"{x:>7} -> {y:<6}" if x != y else f"{y:>16}")
        lines.append(f"  {cat:26} " + " ".join(cells))
    lines.append(f"  weighted score: {base['score']} -> {now['score']}   (wrong approval {WEIGHTS['wrong_approved']}, "
                 f"correct rejected {WEIGHTS['correct_rejected']}, correct in review {WEIGHTS['correct_review']})")
    return "\n".join(lines)
