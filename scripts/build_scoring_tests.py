"""
Build the scoring tests' files in tests/scoring/.

    python scripts/build_scoring_tests.py --anchors      # pick ~50-100 sure pairs from the eval set + save learned brands
    python scripts/build_scoring_tests.py --baseline     # save today's verdicts as the baseline (do this on purpose)
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from itertools import zip_longest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "scoring"))

import harness  # noqa: E402

SKIP_SOURCES = {"ndc_live", "oakridge_live"}
BETA_SOURCES = {"pdi_live", "essity_live"}   # the tester's hand-checked lists: all of their correct pairs go in
DOUBTFUL = {("essity_live_7705604", "B0012XUR0M"), ("essity_live_7237801", "B07HMXKKS6")}
PER_CATEGORY = 12
TRUSTED_GUESS = {"bundle", "other_size_or_color", "other_brand", "other_product_same_brand"}
DEFAULT_BRANDS = [("Hygea", "PDI"), ("SANI CLOTH", "PDI"), ("Sani Professional", "PDI"),
                  ("JOBST", "Essity"), ("Leukoplast", "Essity"), ("Hypafix", "Essity"),
                  ("Leukotape", "Essity"), ("BSN Medical", "Essity")]
_BUNDLE = ("bundle of", "combo pack", " + ")
_SIZE = re.compile(r"\b(xx?s|xx?l|xlfc|lfc|small|medium|large|x-large|petite|[sml])\b(?=\s*[/,-]|\s*$)", re.I)


def _shape(code: str) -> str:
    s = re.sub(r"[^A-Z0-9]", "", str(code or "").upper())
    s = re.sub(r"^[A-Z]{1,3}(?=\d)", "", s)
    return re.sub(r"\d", "9", re.sub(r"[A-Z]", "A", s))


def category(row: dict) -> str:
    import eval_matchers as em
    from services.analytics.data_cleaning.normalize import candidate_brand_matches, mpns_match
    from services.analytics.matching_core.matching.attribute_compare import Cmp, cmp_color, cmp_size
    from services.analytics.matching_core.models import Candidate, Offer
    o, c = row["offer"], row["candidate"]
    offer, cand = em._build(Offer, o), em._build(Candidate, c)
    same_brand = bool(o.get("brand") and c.get("brand")) and bool(
        candidate_brand_matches(o.get("brand"), c.get("brand"), c.get("manufacturer"), use_learned=False))
    o_upc, c_upc = re.sub(r"\D", "", str(o.get("upc") or "")), re.sub(r"\D", "", str(c.get("upc") or ""))
    if row["is_match"]:
        if len(o_upc) >= 11 and o_upc.lstrip("0") == c_upc.lstrip("0"):
            return "upc_match"
        if o.get("mpn") and c.get("mpn") and mpns_match(o["mpn"], c["mpn"]):
            return "mpn_match"
        if not same_brand:
            return "sub_brand"
        if _SIZE.search(str(o.get("title") or "")):
            return "size_variant"
        return "title_only"
    if any(w in str(c.get("title") or "").lower() for w in _BUNDLE):
        return "bundle"
    if Cmp.MISMATCH in (cmp_color(offer, cand), cmp_size(offer, cand)):
        return "other_size_or_color"
    if not same_brand:
        return "other_brand"
    if o.get("mpn") and c.get("mpn") and not mpns_match(o["mpn"], c["mpn"]) and _shape(o["mpn"]) == _shape(c["mpn"]):
        return "other_product_same_brand"
    return "wrong_other"


def pick_anchors(eval_set: Path) -> list[dict]:
    rows = json.loads(eval_set.read_text(encoding="utf-8"))
    beta: list[dict] = []
    by_cat: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r.get("_source") in SKIP_SOURCES or (r["offer"].get("offer_id"), r["candidate"].get("asin")) in DOUBTFUL:
            continue
        cat = category(r)
        human = r.get("_confidence_in_label") == "human_verified"
        if human and r["is_match"] and r.get("_source") in BETA_SOURCES:
            beta.append({**r, "category": cat})                  # always in, no limit
            continue
        if human or (not r["is_match"] and cat in TRUSTED_GUESS):
            by_cat[cat][r.get("_source") or "?"].append({**r, "category": cat})
    picked = list(beta)
    in_cat = defaultdict(int)
    for r in beta:
        in_cat[r["category"]] += 1
    for cat, per_source in sorted(by_cat.items()):           # fill each category up to the limit, vendors in turn
        mixed = [x for group in zip_longest(*per_source.values()) for x in group if x]
        picked += mixed[:max(0, PER_CATEGORY - in_cat[cat])]
    return picked


def save_brands() -> list[dict]:
    db = ROOT / "catalog_verifier.db"
    pairs = []
    if db.is_file():
        try:
            with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as conn:
                pairs = conn.execute("SELECT sub_brand, parent_brand FROM brand_families WHERE status='active' "
                                     "ORDER BY parent_brand, sub_brand").fetchall()
        except sqlite3.Error:
            pairs = []
    source = "your database" if pairs else "the default list (no learned brands found in your database)"
    out = [{"sub_brand": s, "parent_brand": p} for s, p in (pairs or DEFAULT_BRANDS)]
    harness.BRANDS.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"learned brands: {len(out)} pairs saved from {source} -> {harness.BRANDS}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--anchors", action="store_true", help="pick the test pairs and save the learned brands")
    ap.add_argument("--baseline", action="store_true", help="save today's verdicts as the expected baseline")
    ap.add_argument("--eval-set", type=Path, default=ROOT / "data" / "labeled" / "eval_set.json")
    args = ap.parse_args()
    if not (args.anchors or args.baseline):
        ap.error("choose --anchors and/or --baseline")

    if args.anchors:
        save_brands()
        anchors = pick_anchors(args.eval_set)
        harness.ANCHORS.write_text(json.dumps(anchors, indent=1, ensure_ascii=False), encoding="utf-8")
        cats = defaultdict(lambda: [0, 0])
        for a in anchors:
            cats[a["category"]][0 if a["is_match"] else 1] += 1
        print(f"test pairs: {len(anchors)} saved -> {harness.ANCHORS}")
        for cat, (pos, neg) in sorted(cats.items()):
            print(f"  {cat:26} {pos:3} correct  {neg:3} wrong")
        print("Read anchors.json, delete any pair you would not bet money on, then run --baseline.")

    if args.baseline:
        import tempfile
        anchors = harness.load_anchors()
        with tempfile.TemporaryDirectory() as tmp:
            harness.use_scoring_db(Path(tmp))
            verdicts = harness.score(anchors)
        harness.BASELINE.write_text(json.dumps(verdicts, indent=1, sort_keys=True), encoding="utf-8")
        print(f"baseline saved -> {harness.BASELINE}")
        for name, v in verdicts.items():
            s = harness.summary(anchors, v)
            print(f"\n{name}: weighted score {s['score']}  {s['total']}")
            for cat, counts in s["per_category"].items():
                print(f"  {cat:26} {counts}")


if __name__ == "__main__":
    main()