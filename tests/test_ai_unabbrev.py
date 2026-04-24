"""
Sanity test for the AI unabbreviation pass.

Goal — verify three properties without hitting the real OpenAI endpoint:

1. The deterministic library pass (`apply_abbreviations`) expands tokens
   correctly for representative medical-supply catalog titles, respecting
   the rule the user stated: abbreviations expand at the TOKEN level,
   compound meaning falls out from adjacent tokens.

2. The AI pass (`ai_extract`) correctly:
   - passes the known library into the prompt,
   - parses `expanded_title` + `new_abbreviations` out of the JSON response,
   - falls back safely when fields are missing / malformed.

3. The prompt contains the rule "tokens expand independently" so GPT follows
   the intended semantics.

Run with:  python -m pytest tests/test_ai_unabbrev.py  -v
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make `services.*` importable when the test is run from anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.extractor import (  # noqa: E402
    AI_SYSTEM_PROMPT,
    ai_extract,
    apply_abbreviations,
    rule_extract,
)


# ---------------------------------------------------------------------------
# Library fixture — a representative chunk of what a medical-supply vetter
# would accumulate. Token-level expansions only (no multi-word phrases).
# ---------------------------------------------------------------------------
LIBRARY = [
    {"abbr": "ADHSV", "full": "Adhesive"},
    {"abbr": "SPG",   "full": "Sponge"},
    {"abbr": "STR",   "full": "Sterile"},
    {"abbr": "NS",    "full": "Non-Sterile"},
    {"abbr": "NW",    "full": "Non-Woven"},
    {"abbr": "GZE",   "full": "Gauze"},
    {"abbr": "BDG",   "full": "Bandage"},
    {"abbr": "DRS",   "full": "Dressing"},
    {"abbr": "LF",    "full": "Latex-Free"},
    {"abbr": "EA",    "full": "each"},
]


# ---------------------------------------------------------------------------
# Part 1 — deterministic library pass
# ---------------------------------------------------------------------------
SAMPLES = [
    # (raw title, expected expansion)
    ("ADHSV SPG 4X4",            "Adhesive Sponge 4X4"),
    ("STR GZE BDG 2IN",          "Sterile Gauze Bandage 2IN"),
    ("NW SPG NS 2X2",            "Non-Woven Sponge Non-Sterile 2X2"),
    ("LF ADHSV BDG 1 EA",        "Latex-Free Adhesive Bandage 1 each"),
    ("DRS PKG 10 EA",            "Dressing PKG 10 each"),        # PKG stays (not in library)
    ("ADHSV",                    "Adhesive"),                    # single token — just expands
    ("Adhesive Sponge 4x4",      "Adhesive Sponge 4x4"),         # already expanded — no change
    ("",                         ""),                             # empty string
]


# Tokens with no word-boundary (abbr stuck to a digit, e.g. "1EA") are NOT
# expanded — the regex uses \b which needs a word boundary either side. This
# is deliberate: it prevents "Q1EARLY" or "ADHSVX" from being mangled. If the
# catalog uses attached forms, a pre-processing step should space them out.
ATTACHED_NON_EXPANDING = [
    ("1EA",          "1EA"),
    ("1ADHSV",       "1ADHSV"),
    ("100EA box",    "100EA box"),
]


def test_library_expansion_token_level() -> None:
    """apply_abbreviations expands each matching token and leaves the rest."""
    for raw, expected in SAMPLES:
        got = apply_abbreviations(raw, LIBRARY)
        assert got == expected, f"\n  input:    {raw!r}\n  expected: {expected!r}\n  got:      {got!r}"


def test_library_expansion_does_not_over_match() -> None:
    """Whole-word matching must not mangle adjacent text.

    ADHSV expands inside 'ADHSV SPG' but should NOT trigger inside a token
    like 'ADHSVX' or 'xADHSV'. Likewise for short abbrs like EA.
    """
    cases = [
        ("ADHSVX SPG",     "ADHSVX Sponge"),   # ADHSVX is one token — no expansion
        ("xADHSV",         "xADHSV"),          # suffix only
        ("EAGER",          "EAGER"),           # EA inside EAGER must NOT expand
        ("10 ea boxes",    "10 each boxes"),   # lowercase 'ea' is a case-insensitive match
    ]
    for raw, expected in cases:
        got = apply_abbreviations(raw, LIBRARY)
        assert got == expected, f"\n  input:    {raw!r}\n  expected: {expected!r}\n  got:      {got!r}"


def test_library_expansion_skips_attached_forms() -> None:
    """Digit-letter adjacency (e.g. "1EA") is conservatively left alone.

    Python's `\\b` is a boundary between word-char and non-word-char. Both
    digits and letters count as word chars, so there's no boundary between
    the `1` and the `E` in `1EA` — meaning `\\bEA\\b` will not match. This
    is deliberate: it prevents any unexpected mangling of tokens like
    `ADHSVX`, `Q1EARLY`, etc. If the catalog uses attached forms, a
    pre-processing pass should space them out first.
    """
    for raw, expected in ATTACHED_NON_EXPANDING:
        got = apply_abbreviations(raw, LIBRARY)
        assert got == expected, f"\n  input:    {raw!r}\n  expected: {expected!r}\n  got:      {got!r}"


def test_rule_extract_surfaces_expanded_title() -> None:
    """rule_extract normalises + lowercases the expansion for the scorer."""
    out = rule_extract("ADHSV SPG 4X4 STR", LIBRARY)
    assert out["normalised_title"] == "adhesive sponge 4x4 sterile", out


# ---------------------------------------------------------------------------
# Part 2 — AI pass with the OpenAI client mocked
# ---------------------------------------------------------------------------
def _mock_openai_response(payload: dict):
    """Build a mock OpenAI client that returns `payload` as JSON content."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=json.dumps(payload)))]
    )
    return mock_client


def test_ai_extract_parses_expanded_title_and_new_abbrs() -> None:
    """Happy path — GPT returns both new fields and ai_extract surfaces them."""
    payload = {
        "product_type": "sponge",
        "size": {"value": 4, "unit": "in"},
        "pack_count": 10,
        "variant": {},
        "form": "sponge",
        "expanded_title": "Adhesive Sponge Non-Woven 4x4 Pack-10",
        "new_abbreviations": [
            {"abbr": "PKG", "full": "Pack"},
        ],
    }
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), \
         patch("openai.OpenAI", return_value=_mock_openai_response(payload)):
        out = ai_extract("ADHSV SPG NW 4X4 PKG 10", abbreviations=LIBRARY)

    assert out["expanded_title"] == "Adhesive Sponge Non-Woven 4x4 Pack-10"
    assert out["new_abbreviations"] == [{"abbr": "PKG", "full": "Pack"}]
    assert out["normalised_title"] == "adhesive sponge non-woven 4x4 pack-10"
    assert "error" not in out


def test_ai_extract_prompt_includes_known_library() -> None:
    """The library must be threaded into the prompt so GPT treats it as truth."""
    payload = {
        "expanded_title": "Adhesive Sponge 4x4",
        "new_abbreviations": [],
    }
    mock_client = _mock_openai_response(payload)
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), \
         patch("openai.OpenAI", return_value=mock_client):
        ai_extract("ADHSV SPG 4X4", abbreviations=LIBRARY)

    # Grab the args passed to create(...).
    call = mock_client.chat.completions.create.call_args
    messages = call.kwargs["messages"]
    user_msg = next(m for m in messages if m["role"] == "user")["content"]
    system_msg = next(m for m in messages if m["role"] == "system")["content"]

    # Every library token must appear in the user prompt.
    for entry in LIBRARY:
        assert f"{entry['abbr']} -> {entry['full']}" in user_msg, \
            f"library entry {entry} missing from prompt"

    # System prompt enforces the core rule.
    assert "TOKEN level" in system_msg
    assert "independently" in system_msg
    assert "expanded_title" in system_msg
    assert "new_abbreviations" in system_msg


def test_ai_extract_defaults_on_missing_fields() -> None:
    """If GPT omits the new fields, ai_extract fills safe defaults."""
    payload = {"product_type": "sponge"}  # nothing else
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), \
         patch("openai.OpenAI", return_value=_mock_openai_response(payload)):
        out = ai_extract("ADHSV SPG 4X4", abbreviations=LIBRARY)

    assert out["new_abbreviations"] == []
    assert out["expanded_title"] == "ADHSV SPG 4X4"  # falls back to input
    assert out["normalised_title"] == "adhsv spg 4x4"


def test_ai_extract_handles_malformed_expanded_title() -> None:
    """Non-string / empty expanded_title should fall back to the input title."""
    payload = {"expanded_title": "", "new_abbreviations": "not a list"}
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), \
         patch("openai.OpenAI", return_value=_mock_openai_response(payload)):
        out = ai_extract("NW SPG 2X2", abbreviations=LIBRARY)

    assert out["expanded_title"] == "NW SPG 2X2"
    assert out["new_abbreviations"] == []


def test_ai_extract_missing_api_key() -> None:
    """Without the env var, ai_extract returns an error dict rather than crashing."""
    with patch.dict(os.environ, {}, clear=True):
        out = ai_extract("ADHSV SPG", abbreviations=LIBRARY)
    assert "error" in out
    assert "OPENAI_API_KEY" in out["error"]


# ---------------------------------------------------------------------------
# Part 3 — prompt quality
# ---------------------------------------------------------------------------
def test_system_prompt_states_the_token_rule() -> None:
    """Regression guard — the user-provided rule must stay in the prompt."""
    assert "ADHSV" in AI_SYSTEM_PROMPT
    assert "SPG"   in AI_SYSTEM_PROMPT
    assert "Adhesive Sponge" in AI_SYSTEM_PROMPT
    assert "single-word" in AI_SYSTEM_PROMPT.lower() or \
           "single word" in AI_SYSTEM_PROMPT.lower()
