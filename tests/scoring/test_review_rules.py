"""The tester's rule: what the tool is unsure about goes to Review, never to Not Approved."""
import harness
from services.analytics.matching_core.matching.attribute_compare import volume_unclear
from services.analytics.matching_core.matching.cascade import score
from services.analytics.matching_core.models import Candidate, Offer, Verdict

PROJECT = harness.ROOT      # importing harness puts the project folder on the import path


def pair(vendor, amazon, brand="Nestle", amazon_brand="Nestle"):
    return Offer(offer_id="t", title=vendor, brand=brand), Candidate(asin="B0TEST0001", title=amazon, brand=amazon_brand)


def test_strange_volume_is_unclear():
    assert volume_unclear(*pair("Compleat Standard 1.4 8.45 fl oz", "Compleat Standard 1.4mL, Vanilla, 24 Pack"))


def test_normal_volume_difference_is_not_unclear():
    assert not volume_unclear(*pair("Lotion 8 fl oz", "Lotion 16 fl oz"))


def test_strange_volume_goes_to_review_not_rejected():
    r = score(*pair("Nestle Compleat Standard 1.4 8.45 fl oz", "Nestle Compleat Standard 1.4mL, Vanilla Flavor, 24 Pack"), mode="medical")
    assert r.verdict is Verdict.REVIEW


def test_fairly_similar_title_goes_to_review_not_rejected():
    r = score(*pair("Nestle Compleat Pediatric 8.45 fl oz", "Compleat Pediatric 250mL, Unflavored, 24 Pack"), mode="medical")
    assert r.verdict is Verdict.REVIEW


def test_very_different_title_is_still_rejected():
    r = score(*pair("Boost Kid Essentials 1.5 Fiber Vanilla 8 fl oz", "Garden Hose Nozzle Spray Gun"), mode="medical")
    assert r.verdict is Verdict.REJECTED


def test_real_volume_difference_is_still_rejected():
    r = score(*pair("Nestle Lotion 8 fl oz", "Nestle Lotion 16 fl oz"), mode="medical")
    assert r.verdict is not Verdict.VERIFIED
