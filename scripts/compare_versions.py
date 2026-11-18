"""
Compare two versions of the code on the eval set: per vendor, per how the pair was found, and pair by pair.

    python scripts/compare_versions.py --before HEAD~1                    # last commit vs your current files
    python scripts/compare_versions.py --before main --after HEAD         # two commits / branches
    python scripts/compare_versions.py --before HEAD~1 --matcher all      # weighted + cascade + classifier

--before / --after take any git commit, branch or tag.  "--after WORKTREE" (the default) means the files as
they are on disk now, committed or not.  The other version is checked out into a temporary folder; your
files are never touched.

A version without services/analytics/matching_core (the old code on main) is scored with its old matcher,
services/analytics/matcher.py score_amazon_item().  It has one scorer, so its answers are compared with each
scorer of the other version.

Both versions read the SAME copy of your database (catalog_verifier.db), so learned sub-brands and
other saved data are equal on both sides: only the code differs.

Better / worse, per pair:
  a CORRECT pair is better when it moves up   (rejected -> review -> approved)
  a WRONG pair   is better when it moves down (approved -> review -> rejected)
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
RANK = {"rejected": 0, "not_approved": 0, "review": 1, "verified": 2}
PILE = {0: "rejected", 1: "review", 2: "approved"}


# --------------------------------------------------------------------------- #
# dump mode: runs INSIDE one version of the code and writes one verdict per pair
# --------------------------------------------------------------------------- #

def dump(root: Path, eval_set: Path, matchers: list[str], mode: str, out: Path, db: Path | None) -> None:
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "scripts"))
    if db:                       # same database for both versions (learned sub-brands live there)
        from services import database
        database.DB_PATH = db
    rows = json.loads(eval_set.read_text(encoding="utf-8"))
    if not (root / "services" / "analytics" / "matching_core").is_dir():
        result = dump_old_matcher(rows, matchers)
        result["_rows"] = _row_info(rows)
        out.write_text(json.dumps(result), encoding="utf-8")
        return
    import eval_matchers as em  # the eval helpers of THIS version
    pairs = em.load_eval_set(eval_set, None)
    result = {}
    for name in matchers:
        try:
            res = em.run_matcher(name, pairs, mode)
        except Exception as exc:  # noqa: BLE001  (e.g. no classifier model in that version)
            print(f"  {name}: skipped ({type(exc).__name__}: {exc})", file=sys.stderr)
            continue
        result[name] = [{"verdict": r.verdict.value, "confidence": r.confidence} for r in res]
    result["_rows"] = _row_info(rows)
    out.write_text(json.dumps(result), encoding="utf-8")


def _row_info(rows: list[dict]) -> list[dict]:
    return [{"source": r.get("_source") or "?", "found_via": r.get("_found_via") or "?",
             "offer_id": r["offer"].get("offer_id"), "asin": r["candidate"].get("asin"),
             "is_match": bool(r["is_match"]),
             "vendor_title": (r["offer"].get("title") or "")[:70],
             "amazon_title": (r["candidate"].get("title") or "")[:70]} for r in rows]


def dump_old_matcher(rows: list[dict], matchers: list[str]) -> dict:
    """The old code (before matching_core): services/analytics/matcher.py score_amazon_item(source, amazon).
    One scorer, so the same answers are filed under every scorer name the other version is compared with."""
    from services.analytics.matcher import score_amazon_item
    verdicts = []
    for r in rows:
        o, c = r["offer"], r["candidate"]
        source = {"title": o.get("title") or o.get("raw_text") or "", "brand": o.get("brand") or "",
                  "upc": o.get("upc") or "", "itemid": o.get("mpn") or "", "mpn": o.get("mpn") or ""}
        amazon = {"asin": c.get("asin"), "title": c.get("title") or "", "brand": c.get("brand") or "",
                  "manufacturer": c.get("manufacturer") or "", "upc": c.get("upc") or "",
                  "mpn": c.get("mpn") or "", "attributes": c.get("attributes") or {}}
        s = score_amazon_item(source, amazon)
        verdicts.append({"verdict": s["verdict"], "confidence": s.get("confidence_score")})
    print("  scored with the OLD matcher (services/analytics/matcher.py)", file=sys.stderr)
    return {name: verdicts for name in matchers} | {"_engine": "old matcher"}


def run_version(ref: str, eval_set: Path, matchers: list[str], mode: str, tmp: Path, db: Path | None) -> dict:
    out = tmp / f"verdicts_{abs(hash(ref))}.json"
    if ref == "WORKTREE":
        root, cleanup = HERE, None
    else:
        root = tmp / f"code_{abs(hash(ref))}"
        subprocess.run(["git", "-C", str(HERE), "worktree", "add", "--detach", "-f", str(root), ref],
                       check=True, capture_output=True)
        cleanup = root
    try:
        print(f"scoring with {ref} ...", file=sys.stderr)
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "--dump", "--root", str(root),
                        "--eval-set", str(eval_set), "--mode", mode, "--out", str(out),
                        "--matcher", ",".join(matchers)] + (["--db", str(db)] if db else []), check=True)
    finally:
        if cleanup:
            subprocess.run(["git", "-C", str(HERE), "worktree", "remove", "--force", str(cleanup)],
                           capture_output=True)
    return json.loads(out.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def counts(rows: list[dict], verdicts: list[dict], idx: list[int]) -> dict:
    c = defaultdict(int)
    for i in idx:
        r, v = rows[i], RANK[verdicts[i]["verdict"]]
        if r["is_match"]:
            c["correct"] += 1
            c[("ok_appr", "ok_rev", "ok_rej")[2 - v]] += 1
        else:
            c["wrong_appr"] += v == 2
            c["review"] += v == 1
        c["review_all"] += v == 1
    return c


def table(title: str, groups: dict[str, list[int]], rows, before, after) -> None:
    print(f"\n{title}")
    print(f"  {'':22} {'pairs':>5} | {'correct approved':>17} | {'correct rejected':>17} | {'WRONG approved':>15} | {'review pile':>12}")
    for name, idx in sorted(groups.items()):
        b, a = counts(rows, before, idx), counts(rows, after, idx)

        def cell(key, width, good_up):
            x, y = b[key], a[key]
            mark = "" if x == y else ("  +" if (y > x) == good_up else "  -")
            return f"{x:>4} -> {y:<4}{mark}".rjust(width)
        print(f"  {name:22} {len(idx):>5} | {cell('ok_appr', 17, True)} | {cell('ok_rej', 17, False)} | "
              f"{cell('wrong_appr', 15, False)} | {cell('review_all', 12, False)}")


def changed_pairs(rows, before, after, limit: int) -> None:
    better, worse = [], []
    for i, r in enumerate(rows):
        b, a = RANK[before[i]["verdict"]], RANK[after[i]["verdict"]]
        if a == b:
            continue
        good = (a > b) if r["is_match"] else (a < b)
        line = (f"  [{r['source']}] {r['offer_id']} -> {r['asin']}  {'correct' if r['is_match'] else 'wrong  '} "
                f"pair: {PILE[b]} -> {PILE[a]}\n      vendor: {r['vendor_title']}\n      amazon: {r['amazon_title']}")
        (better if good else worse).append(line)
    print(f"\nPAIRS THAT GOT WORSE ({len(worse)})" + ("" if worse else ": none"))
    print("\n".join(worse[:limit]))
    print(f"\nPAIRS THAT GOT BETTER ({len(better)})" + ("" if better else ": none"))
    print("\n".join(better[:limit]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--before", default="HEAD")
    ap.add_argument("--after", default="WORKTREE")
    ap.add_argument("--matcher", default="cascade", help="cascade, weighted, classifier, or all")
    ap.add_argument("--mode", default="medical", choices=["cpg", "medical"])
    ap.add_argument("--eval-set", type=Path, default=HERE / "data" / "labeled" / "eval_set.json")
    ap.add_argument("--limit", type=int, default=40, help="max changed pairs to list per section")
    # internal: dump mode
    ap.add_argument("--dump", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--out", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--db", type=Path, help=argparse.SUPPRESS)
    args = ap.parse_args()

    matchers = ["weighted", "cascade", "classifier"] if args.matcher == "all" else args.matcher.split(",")
    eval_set = args.eval_set.resolve()
    if args.dump:
        dump(args.root, eval_set, matchers, args.mode, args.out, args.db)
        return

    with tempfile.TemporaryDirectory() as tmp:
        db = None
        real_db = HERE / "catalog_verifier.db"
        if real_db.is_file():    # one copy, used by both versions; your real database is never written
            db = Path(tmp) / "catalog_verifier_copy.db"
            shutil.copyfile(real_db, db)
        else:
            print("note: no catalog_verifier.db found - both versions run without learned sub-brands",
                  file=sys.stderr)
        before = run_version(args.before, eval_set, matchers, args.mode, Path(tmp), db)
        after = run_version(args.after, eval_set, matchers, args.mode, Path(tmp), db)
    rows = before["_rows"]
    by_vendor, by_found = defaultdict(list), defaultdict(list)
    for i, r in enumerate(rows):
        by_vendor[r["source"]].append(i)
        by_found[r["found_via"]].append(i)
    print(f"\nBEFORE = {args.before}   AFTER = {args.after}   ({len(rows)} pairs, eval set {eval_set.name})")
    for tag, data in (("BEFORE", before), ("AFTER", after)):
        if data.get("_engine") == "old matcher":
            print(f"{tag} uses the old matcher (one scorer): every table compares it with that scorer of the other side")
    print("'+' = better, '-' = worse")
    for name in matchers:
        if name not in before or name not in after:
            print(f"\n### {name}: missing in one version, skipped")
            continue
        print(f"\n################ {name.upper()} ################")
        table("ALL PAIRS", {"all": list(range(len(rows)))}, rows, before[name], after[name])
        table("PER VENDOR", by_vendor, rows, before[name], after[name])
        table("PER HOW THE PAIR WAS FOUND", by_found, rows, before[name], after[name])
        changed_pairs(rows, before[name], after[name], args.limit)


if __name__ == "__main__":
    main()