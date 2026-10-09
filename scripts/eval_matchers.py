"""
Run the current repo's matchers (weighted, cascade, classifier) against a labeled
eval set. Put this file at scripts/eval_matchers.py and run it from the repo root:

    uv run python scripts/eval_matchers.py --matcher all
    uv run python scripts/eval_matchers.py --matcher cascade --mode cpg
    uv run python scripts/eval_matchers.py --matcher all --audit          # + LLM auditor (costs tokens)
    uv run python scripts/eval_matchers.py --matcher all --by-combo --threshold-sweep
    uv run python scripts/eval_matchers.py --matcher all --eval-set path/to/eval_set.json

Eval set format (same as before): a JSON list of
    {"offer": {...}, "candidate": {...}, "is_match": true/false}

Definitions used below:
    precision        share of auto-approvals that are real matches
    false approvals  approved, but not a match          (the expensive error)
    missed           rejected, but actually a match     (lost for good)
    in review        real matches sent to review        (found, but costs manual work)
    recall           share of all real matches that were auto-approved
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")      # the auditor needs ANTHROPIC_API_KEY
except ImportError:
    pass

from services.analytics.matching_core.models import Candidate, Offer, Verdict  # noqa: E402

DEFAULT_EVAL_SET = ROOT / "data" / "labeled" / "eval_set.json"
MATCHER_NAMES = ("weighted", "cascade", "classifier")


# --------------------------------------------------------------------------- #
# matchers
# --------------------------------------------------------------------------- #

def get_matcher(name: str):
    if name == "weighted":
        from services.analytics.matching_core.matching.scorer import score
        return lambda o, c, mode: score(o, c)
    if name == "cascade":
        from services.analytics.matching_core.matching.cascade import score
        return lambda o, c, mode: score(o, c, mode=mode)
    if name == "classifier":
        from services.analytics.matching_core.matching.classifier import score
        return lambda o, c, mode: score(o, c)
    raise ValueError(name)


def review_floor(name: str) -> float:
    """Lowest confidence that still lands in review, per matcher."""
    if name == "weighted":
        from services.analytics.matching_core.matching.scorer import REVIEW_MIN
        return REVIEW_MIN
    if name == "cascade":
        from services.analytics.matching_core.matching.cascade import REVIEW_MIN
        return REVIEW_MIN
    return 20.0     # classifier: probability >= 0.2 -> review (confidence = probability x 100)


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #

@dataclass
class Pair:
    offer: Offer
    cand: Candidate
    is_match: bool


def _build(model, data: dict):
    """Build a pydantic model; drop keys the current model doesn't know about."""
    try:
        return model(**data)
    except Exception:
        known = set(getattr(model, "model_fields", {}))
        return model(**{k: v for k, v in data.items() if k in known})


def load_eval_set(path: Path, brand: str | None) -> list[Pair]:
    """brand is a FALLBACK only: rows already carry their own brand."""
    pairs = []
    for row in json.loads(path.read_text(encoding="utf-8")):
        offer = _build(Offer, row["offer"])
        if brand and not offer.brand:
            offer.brand = brand
        pairs.append(Pair(offer, _build(Candidate, row["candidate"]), bool(row["is_match"])))
    return pairs


# --------------------------------------------------------------------------- #
# running
# --------------------------------------------------------------------------- #

def run_matcher(name: str, pairs: list[Pair], mode: str, audit: bool = False) -> list:
    fn = get_matcher(name)
    results = [fn(p.offer, p.cand, mode) for p in pairs]
    if not audit:
        return results

    from services.analytics.matching_core.agents.verification_agent import (
        apply_audit, create_approval_auditor,
    )
    auditor = create_approval_auditor()
    to_audit = sum(1 for r in results if r.verdict is Verdict.VERIFIED)
    print(f"  auditing {to_audit} approvals...")
    out, n_flagged, tokens = [], 0, 0
    for p, r in zip(pairs, results):
        if r.verdict is Verdict.VERIFIED:
            a = auditor.audit(p.offer, p.cand, r)
            tokens += int(getattr(a, "tokens", 0) or 0)
            new = apply_audit(r, a)
            if new.verdict is not r.verdict:
                n_flagged += 1
            r = new
        out.append(r)
    print(f"  audited {to_audit}, flagged {n_flagged}"
          + (f", {tokens} tokens (~${tokens / 1e6 * 3:.3f})" if tokens else ""))
    return out


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #

@dataclass
class Metrics:
    total: int
    positives: int
    verified: int
    review: int
    rejected: int
    tp: int
    fp: int
    missed: int
    matches_in_review: int

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else None

    @property
    def recall(self) -> float | None:
        return self.tp / self.positives if self.positives else None

    @property
    def review_rate(self) -> float:
        return self.review / self.total if self.total else 0.0


def compute_metrics(results, pairs: list[Pair]) -> Metrics:
    v = rv = rj = tp = fp = missed = in_review = 0
    for r, p in zip(results, pairs):
        if r.verdict is Verdict.VERIFIED:
            v += 1
            tp += p.is_match
            fp += not p.is_match
        elif r.verdict is Verdict.REVIEW:
            rv += 1
            in_review += p.is_match
        else:
            rj += 1
            missed += p.is_match
    return Metrics(len(pairs), sum(p.is_match for p in pairs), v, rv, rj, tp, fp, missed, in_review)


def print_metrics(label: str, results, pairs: list[Pair]) -> Metrics:
    m = compute_metrics(results, pairs)
    print(f"\n[{label}]  n={m.total}  real matches={m.positives}")
    print(f"  approved={m.verified}  review={m.review}  rejected={m.rejected}  "
          f"review_rate={m.review_rate:.1%}")
    if m.precision is None:
        print("  precision: n/a (nothing approved)")
    else:
        print(f"  precision={m.precision:.3f}  recall={m.recall:.3f}")
    print(f"  false approvals={m.fp}  missed={m.missed}  real matches in review={m.matches_in_review}")
    return m


def print_rules_fired(results) -> None:
    rules = Counter(r for res in results for r in res.reasons if r.startswith("rule:"))
    if rules:
        print("\n  rules fired:")
        for rule, n in rules.most_common():
            print(f"    {n:4}  {rule.removeprefix('rule:')}")


def _short(text: str | None, n: int = 55) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def print_errors(results, pairs: list[Pair], limit: int) -> None:
    fps = [(r, p) for r, p in zip(results, pairs) if r.verdict is Verdict.VERIFIED and not p.is_match]
    fns = [(r, p) for r, p in zip(results, pairs) if r.verdict is Verdict.REJECTED and p.is_match]
    for title, rows in (("FALSE APPROVALS - approved but not a match", fps),
                        ("MISSED MATCHES - rejected but actually a match", fns)):
        print(f"\n  {title} ({len(rows)}):")
        if not rows:
            print("    none")
        for r, p in rows[:limit]:
            print(f"    {str(r.offer_id):>8} {r.asin:<11} conf={r.confidence:6.2f}  {r.reasons}")
            print(f"             vendor: {_short(p.offer.title or p.offer.raw_text)}")
            print(f"             amazon: {_short(p.cand.title)}")


def print_by_combo(results, pairs: list[Pair]) -> None:
    """Split by which identifiers the offer carried."""
    def combo(o: Offer) -> str:
        parts = [k for k, ok in (("upc", o.upc), ("mpn", o.mpn), ("title", o.title or o.raw_text)) if ok]
        return "+".join(parts) or "none"

    groups: dict[str, tuple[list, list]] = defaultdict(lambda: ([], []))
    for r, p in zip(results, pairs):
        groups[combo(p.offer)][0].append(r)
        groups[combo(p.offer)][1].append(p)
    for key in sorted(groups):
        print_metrics(f"combo: {key}", *groups[key])


def threshold_sweep(name: str, results, pairs: list[Pair]) -> None:
    """Re-verdict the same scores at different approval lines (review line unchanged)."""
    floor = review_floor(name)
    total_pos = sum(p.is_match for p in pairs)
    print(f"\n  THRESHOLD SWEEP ({name}; review band starts at {floor:g})")
    print(f"  {'approve_at':>10} {'review_rate':>12} {'approved':>9} {'precision':>10} "
          f"{'false_appr':>11} {'missed':>7}")
    for vmin in (100, 95, 90, 85, 80, 75, 70, 65, 60, 55, 50):
        approved = review = tp = fp = missed = 0
        for r, p in zip(results, pairs):
            if r.confidence >= vmin:
                approved += 1
                tp += p.is_match
                fp += not p.is_match
            elif r.confidence >= floor:
                review += 1
            else:
                missed += p.is_match
        prec = f"{tp / (tp + fp):.3f}" if (tp + fp) else "n/a"
        print(f"  {vmin:>10} {review / len(pairs):>11.1%} {approved:>9} {prec:>10} {fp:>11} {missed:>7}")


# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser(description="Evaluate weighted / cascade / classifier on a labeled eval set.")
    ap.add_argument("--matcher", default="all", choices=[*MATCHER_NAMES, "all"])
    ap.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_SET)
    ap.add_argument("--mode", default="medical", choices=["cpg", "medical"],
                    help="cascade only: cpg keys on UPC, medical on MPN")
    ap.add_argument("--brand", default=None, help="fallback brand for offers without one")
    ap.add_argument("--audit", action="store_true", help="run the LLM approval auditor on approvals")
    ap.add_argument("--by-combo", action="store_true", help="metrics per identifier combination")
    ap.add_argument("--threshold-sweep", action="store_true", help="approval-line tradeoff table")
    ap.add_argument("--errors", type=int, default=10, help="how many error rows to print (0 = none)")
    args = ap.parse_args()

    if not args.eval_set.exists():
        print(f"Eval set not found: {args.eval_set}")
        raise SystemExit(1)

    pairs = load_eval_set(args.eval_set, args.brand)
    n_pos = sum(p.is_match for p in pairs)
    print(f"Loaded {len(pairs)} labeled pairs from {args.eval_set}")
    print(f"  real matches={n_pos}  non-matches={len(pairs) - n_pos}  mode={args.mode}"
          + ("  AUDIT ON" if args.audit else ""))

    names = MATCHER_NAMES if args.matcher == "all" else (args.matcher,)
    summary: dict[str, Metrics] = {}
    for name in names:
        print("\n" + "=" * 70)
        print(f"MATCHER: {name}" + ("  + audit" if args.audit else ""))
        print("=" * 70)
        try:
            results = run_matcher(name, pairs, args.mode, audit=args.audit)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED  {type(exc).__name__}: {exc}")
            continue
        summary[name] = print_metrics("overall", results, pairs)
        print_rules_fired(results)
        if args.errors:
            print_errors(results, pairs, args.errors)
        if args.by_combo:
            print_by_combo(results, pairs)
        if args.threshold_sweep:
            threshold_sweep(name, results, pairs)

    if len(summary) > 1:
        print("\n" + "=" * 70)
        print("COMPARISON" + ("  (with audit)" if args.audit else ""))
        print("=" * 70)
        print(f"  {'matcher':12} {'review':>8} {'approved':>9} {'precision':>10} "
              f"{'false_appr':>11} {'missed':>7} {'in_review':>10}")
        for name, m in summary.items():
            prec = f"{m.precision:.3f}" if m.precision is not None else "n/a"
            print(f"  {name:12} {m.review_rate:>7.1%} {m.verified:>9} {prec:>10} "
                  f"{m.fp:>11} {m.missed:>7} {m.matches_in_review:>10}")


if __name__ == "__main__":
    main()