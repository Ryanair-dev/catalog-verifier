"""Scoring quality tests: the sure pairs in anchors.json must not get worse than the saved baseline.

    python -m pytest -q tests/scoring          # runs before every push and on every GitHub PR

Three hard rules per scorer (cascade, weighted):
  1. no NEW wrong approvals            (a wrong pair that was not approved is now approved)
  2. no correct pair NEWLY rejected    (the tester's rule: correct pairs never land in Not Approved)
  3. the weighted score is not lower   (wrong approval -10, correct rejected -3, anything in review -1)

Better results pass.  To lock an improvement in, save a new baseline on purpose:
    python scripts/build_scoring_tests.py --baseline
"""
from __future__ import annotations

import json

import pytest

import harness  # tests/scoring/harness.py (pytest puts this folder on the path)


@pytest.fixture(scope="module")
def scored(tmp_path_factory):
    anchors = harness.load_anchors()
    harness.use_scoring_db(tmp_path_factory.mktemp("scoring_db"))
    baseline = json.loads(harness.BASELINE.read_text(encoding="utf-8"))
    return anchors, baseline, harness.score(anchors)


def _report(anchors, baseline, now, matcher) -> str:
    base_sum = harness.summary(anchors, baseline[matcher])
    now_sum = harness.summary(anchors, now[matcher])
    return harness.table(f"\n{matcher.upper()}: baseline -> now, per category", base_sum, now_sum)


@pytest.mark.parametrize("matcher", harness.MATCHERS)
def test_no_new_wrong_approvals(scored, matcher):
    anchors, baseline, now = scored
    found = harness.changes(anchors, baseline[matcher], now[matcher])["new_wrong_approvals"]
    assert not found, (f"{matcher}: {len(found)} wrong pair(s) are now APPROVED:\n    " + "\n    ".join(found)
                       + _report(anchors, baseline, now, matcher))


@pytest.mark.parametrize("matcher", harness.MATCHERS)
def test_no_correct_pair_newly_rejected(scored, matcher):
    anchors, baseline, now = scored
    found = harness.changes(anchors, baseline[matcher], now[matcher])["correct_newly_rejected"]
    assert not found, (f"{matcher}: {len(found)} correct pair(s) are now REJECTED:\n    " + "\n    ".join(found)
                       + _report(anchors, baseline, now, matcher))


@pytest.mark.parametrize("matcher", harness.MATCHERS)
def test_weighted_score_not_lower(scored, matcher):
    anchors, baseline, now = scored
    base_score = harness.summary(anchors, baseline[matcher])["score"]
    now_score = harness.summary(anchors, now[matcher])["score"]
    assert now_score >= base_score, (f"{matcher}: weighted score dropped {base_score} -> {now_score}"
                                     + _report(anchors, baseline, now, matcher))


def test_every_pair_has_a_baseline(scored):
    anchors, baseline, _ = scored
    missing = [harness.anchor_id(r) for r in anchors
               if any(harness.anchor_id(r) not in baseline.get(m, {}) for m in harness.MATCHERS)]
    assert not missing, ("These pairs have no saved verdict yet; run "
                         "`python scripts/build_scoring_tests.py --baseline`:\n  " + "\n  ".join(missing))
