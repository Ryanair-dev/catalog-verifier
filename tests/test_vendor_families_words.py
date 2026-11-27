from services.analytics.vendor_families import _missing_words


def test_missing_words_abbreviations_both_ways():
    assert _missing_words(["good", "grips", "peeler"], ["gg", "peeler"]) == []   # listing abbreviates
    assert _missing_words(["gg", "peeler"], ["good", "grips", "peeler"]) == []   # vendor abbreviates
    assert _missing_words(["casey", "lever"], ["casey"]) == ["lever"]
    assert _missing_words(["a"], ["a", "b"]) == []                               # all present: early return
