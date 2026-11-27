"""
Train the gradient-boosted classifier, test it the way the app scores, and save it as a CANDIDATE.

    python scripts/train_classifier.py                  # test (grouped by vendor item) + save the candidate
    python scripts/train_classifier.py --by-vendor      # also the hardest test: each vendor left out once
    python scripts/train_classifier.py --promote        # put the saved candidate live (old model kept as backup)
    python scripts/train_classifier.py --basic          # only the 15 basic features (compare / roll back)

Features: the 15 basic ones + 3 title features (letter similarity, shorter title inside the longer one, vendor
numbers missing on Amazon), picked by scripts/feature_search.py on 2026-11-24 (vendors left out: avg precision
0.60 -> 0.74, matches found 104 -> 118, wrong approvals 43 -> 44). The letter-pattern table they need is learned
from all titles of the eval set (no labels) and saved inside the model file; the app reads it from there.

What it prints for the NEW classifier, the LIVE classifier and the two rule scorers (weighted, cascade),
all on the same pairs: approved / review / rejected, wrong approvals, correct pairs rejected.

Rules this script keeps:
  - Test pairs are never seen in training, and every candidate of one vendor item stays on the same side.
    (Pairs of one item are near-copies; splitting them makes the score look better than reality.)
  - The new model is tested through the app's own path: same 0.5 / 0.2 thresholds, same variant check.
  - The LIVE model was trained on (some of) these pairs, so its numbers look better than they are.
  - The saved model is trained on ALL pairs and carries a record of the data it saw.
  - Nothing goes live without --promote.  The old model is kept as classifier.prev.pkl.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import eval_matchers as em  # noqa: E402  (load_eval_set, run_matcher, compute_metrics)
from services.analytics.matching_core.matching.classifier import (  # noqa: E402
    DEFAULT_MODEL_PATH, extract_features, new_text_vectorizer, raw_titles,
)
from services.analytics.matching_core.models import MatchResult, Verdict  # noqa: E402

APPROVE_AT, REVIEW_AT = 0.5, 0.2          # the same thresholds classifier.score() uses
DEFAULT_OUT = ROOT / "data" / "models" / "classifier_candidate.pkl"


def new_model():
    from sklearn.ensemble import GradientBoostingClassifier
    return GradientBoostingClassifier(n_estimators=200, max_depth=3, learning_rate=0.05,
                                      min_samples_leaf=3, random_state=42)


def as_results(probabilities, pairs) -> list[MatchResult]:
    """Model probabilities -> MatchResults exactly like classifier.score(), then the app's variant check."""
    from services.analytics.matching_core.matching.flags import unified_attrs_enabled
    out = []
    for p, pair in zip(probabilities, pairs):
        verdict = Verdict.VERIFIED if p >= APPROVE_AT else Verdict.REVIEW if p >= REVIEW_AT else Verdict.REJECTED
        out.append(MatchResult(offer_id=pair.offer.offer_id, asin=pair.cand.asin, confidence=round(p * 100, 1),
                               verdict=verdict, signals={"match_probability": p}, reasons=["rule:classifier"]))
    if unified_attrs_enabled():
        from services.analytics.matching_core.matching.variant_gate import apply_gate, gate_for_models
        out = [apply_gate(r, gate_for_models(pair.offer, pair.cand)) for pair, r in zip(pairs, out)]
    return out


def cross_val_probabilities(X, y, groups, folds: int) -> list[float]:
    """Every pair gets a probability from a model that never saw its vendor item."""
    from sklearn.model_selection import GroupKFold
    probs = [0.0] * len(y)
    for train_idx, test_idx in GroupKFold(n_splits=folds).split(X, y, groups):
        clf = new_model().fit([X[i] for i in train_idx], [y[i] for i in train_idx])
        for i, p in zip(test_idx, clf.predict_proba([X[i] for i in test_idx])[:, 1]):
            probs[i] = float(p)
    return probs


def line(label: str, results, pairs) -> dict:
    m = em.compute_metrics(results, pairs)
    prec = f"{m.precision:.3f}" if m.precision is not None else "  n/a"
    print(f"  {label:34} {m.verified:5} {m.review:6} {m.rejected:6} {m.fp:7} {m.missed:9}   {prec}  {m.recall:.3f}")
    return {"approved": m.verified, "review": m.review, "rejected": m.rejected, "wrong_approvals": m.fp,
            "correct_rejected": m.missed, "precision": m.precision, "recall": m.recall}


def header(title: str) -> None:
    print(f"\n{title}")
    print(f"  {'':34} {'appr':>5} {'review':>6} {'rejct':>6} {'wrong':>7} {'correct':>9}   {'prec':>5}  {'recall'}")
    print(f"  {'':34} {'':>5} {'':>6} {'':>6} {'appr.':>7} {'rejected':>9}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eval-set", type=Path, default=em.DEFAULT_EVAL_SET)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--mode", default="medical", choices=["cpg", "medical"], help="cascade only, as in eval_matchers")
    ap.add_argument("--by-vendor", action="store_true", help="also leave each vendor (_source) out once")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where the candidate model is saved")
    ap.add_argument("--promote", action="store_true", help="copy the saved candidate over the live model")
    ap.add_argument("--basic", action="store_true", help="only the 15 basic features, no title features")
    args = ap.parse_args()

    if args.promote:
        if not args.out.is_file():
            sys.exit(f"No candidate at {args.out}. Train first.")
        if DEFAULT_MODEL_PATH.is_file():
            backup = DEFAULT_MODEL_PATH.with_name("classifier.prev.pkl")
            shutil.copyfile(DEFAULT_MODEL_PATH, backup)
            print(f"old live model kept as {backup}")
        shutil.copyfile(args.out, DEFAULT_MODEL_PATH)
        print(f"candidate is live: {DEFAULT_MODEL_PATH}\nRestart the server so the app loads it.")
        return

    raw = args.eval_set.read_bytes()
    rows = json.loads(raw)
    pairs_all = em.load_eval_set(args.eval_set, None)
    vectorizer = None
    if not args.basic:
        titles = [t for row in rows for t in raw_titles(row)]
        vectorizer = new_text_vectorizer(titles)
    X, y, groups, sources, pairs = [], [], [], [], []
    skipped = 0
    for row, pair in zip(rows, pairs_all):
        try:
            feats = extract_features(row, vectorizer)
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        X.append(feats); y.append(int(row["is_match"])); pairs.append(pair)
        groups.append(str(row["offer"].get("offer_id"))); sources.append(str(row.get("_source") or "?"))
    names = list(X[0].keys())
    X = [[f[n] for n in names] for f in X]
    print(f"{len(y)} pairs ({sum(y)} correct, {len(y) - sum(y)} wrong), {len(set(groups))} vendor items, "
          f"{len(set(sources))} vendors" + (f", {skipped} skipped" if skipped else ""))

    # 1) the new model, tested on vendor items it never saw, through the app's path
    probs = cross_val_probabilities(X, y, groups, args.folds)
    new_results = as_results(probs, pairs)
    header(f"SAME PAIRS, {args.folds} folds grouped by vendor item")
    summary = {"new_classifier": line("NEW classifier (never saw the item)", new_results, pairs)}
    try:
        summary["live_classifier"] = line("live classifier (saw some pairs!)", em.run_matcher("classifier", pairs, args.mode), pairs)
    except FileNotFoundError:
        print("  live classifier: no model file")
    for name in ("weighted", "cascade"):
        summary[name] = line(f"{name} (rules, no training)", em.run_matcher(name, pairs, args.mode), pairs)

    # 2) per vendor, for the new model
    header("NEW classifier per vendor (same test as above)")
    for src in sorted(set(sources)):
        idx = [i for i, s in enumerate(sources) if s == src]
        line(src, [new_results[i] for i in idx], [pairs[i] for i in idx])

    # 3) optional: each vendor left out completely = how it does on a NEW vendor
    if args.by_vendor:
        from sklearn.model_selection import LeaveOneGroupOut
        header("NEW classifier on a vendor it never saw (each vendor left out once)")
        for train_idx, test_idx in LeaveOneGroupOut().split(X, y, sources):
            if len({y[i] for i in train_idx}) < 2:
                continue
            clf = new_model().fit([X[i] for i in train_idx], [y[i] for i in train_idx])
            p = clf.predict_proba([X[i] for i in test_idx])[:, 1]
            line(sources[test_idx[0]], as_results([float(v) for v in p], [pairs[i] for i in test_idx]),
                 [pairs[i] for i in test_idx])

    # 4) the candidate: trained on ALL pairs, saved with a record of what it saw
    clf = new_model().fit(X, y)
    importances = sorted(zip(names, clf.feature_importances_), key=lambda t: -t[1])
    print("\nMost important features:")
    for n, v in importances[:8]:
        print(f"  {n:28} {v:.3f}")
    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "eval_set": str(args.eval_set), "eval_set_sha256": hashlib.sha256(raw).hexdigest(),
        "pairs": len(y), "correct_pairs": sum(y), "vendors": sorted(set(sources)),
        "thresholds": {"approve": APPROVE_AT, "review": REVIEW_AT},
        "features": names,
        "test": summary,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("wb") as f:
        pickle.dump({"clf": clf, "feature_names": names, "text_vectorizer": vectorizer, "meta": meta}, f)
    print(f"\ncandidate saved: {args.out}")
    print("Not live yet. Compare the table above; if it is better, run with --promote.")


if __name__ == "__main__":
    main()