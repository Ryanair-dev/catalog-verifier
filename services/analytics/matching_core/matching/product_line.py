"""
The product-line check.

Color, size, count, flavor ... are VARIANTS of a product.  This check is about something else: the
product LINE.  "Sport Original Weight Mini-Crew" and "Run Original Weight Mini-Crew" are different
products even when color, size and brand are identical, and so are "Crew" and "Mini-Crew".

How it decides, without a hand-made list of line names:
  * The vendor file itself is the vocabulary.  Every word that appears in a title of a vendor row of the
    same brand (once variant words, brand and filler words are removed) is a "line word" of that
    catalog: sport, run, trail, liner, crew, mini, midweight ...
  * A listing is compared with the vendor row using ONLY those line words.  Words the vendor file never
    uses (marketing words, abbreviations: "Coolmax", "Toesocks", "GG") are ignored.  So the check stays
    quiet when it has no evidence.
  * It says "different line" in two cases:
      a) each side names a line word the other lacks        (vendor: sport | listing: run)
      b) the listing is exactly the line of ANOTHER vendor row of the same file, and not this row's
                                                             (vendor: trail midweight MINI crew |
                                                              listing = "trail midweight crew", which is
                                                              another row of the file)
  * With a confirmed UPC the check is not used.  With fewer than two comparable words it says nothing.

v9 (2026-10-22): an ABBREVIATION is not a different line.  Before the two cases above, a word on one side
that is the initials of consecutive words on the other side covers them: vendor "GG" = listing "Good Grips",
vendor "Good Grips" = listing "GG" (on OXO this one misfire carried 965 of 1191 rejections).  When such a
cover was used, case b) is skipped too.

Where the vendor titles come from: the run's own catalog rows (read once, cached for a few minutes), or
register_line_index() for tests and the eval set.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections import defaultdict
from functools import lru_cache
from typing import Any, Iterable

from services.analytics.matching_core.matching.attr_extract import _tokens
from services.analytics.matching_core.matching.lexicon import gender_tokens, get_lexicon

_SIZE_WORDS = {"small", "medium", "large", "extra", "x", "xs", "xl", "xxl", "xxxl", "xxs"}
_UNIT_WORDS = {"oz", "ounce", "ounces", "fl", "ml", "l", "liter", "liters", "litre", "lb", "lbs", "pound", "pounds",
               "kg", "g", "gram", "grams", "gal", "gallon", "count", "ct", "pack", "packs", "pk", "pcs", "pc",
               "piece", "pieces", "pair", "pairs", "each", "ea", "roll", "rolls", "tube", "tubes", "vial", "vials",
               "sachet", "sachets", "case", "box", "bundle", "size", "in", "inch", "inches"}
CACHE_SECONDS = 300


@lru_cache(maxsize=4)
def _drop_words(version: str) -> frozenset[str]:
    lex = get_lexicon()
    return frozenset(set(lex.colors) | set(lex.tones) | set(gender_tokens(in_field=True)) | set(lex.sizes)
                     | set(lex.line_filler) | _SIZE_WORDS | _UNIT_WORDS)


def line_tokens(title: Any, brand: Any = "") -> frozenset[str]:
    """The words of a title that name the product line (variant, brand, unit and filler words removed)."""
    drop = _drop_words(get_lexicon().version)
    brand_words = set(_tokens(brand))
    out = []
    for t in _tokens(title):
        if len(t) < 2 or t in drop or t in brand_words or re.fullmatch(r"\d+[a-z]{0,3}", t):
            continue
        out.append(t)
    return frozenset(out)


def _brand_key(brand: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(brand or "").lower())


def _ordered(words: Iterable[str], title: Any) -> str:
    """The words in the order they appear in the title (for a readable sentence)."""
    wanted, seen, out = set(words), set(), []
    for t in _tokens(title):
        if t in wanted and t not in seen:
            seen.add(t)
            out.append(t)
    return " ".join(out) or " ".join(sorted(wanted))


def _initial_runs(tokens: list[str]) -> list[tuple[str, list[str]]]:
    """(initials, words) for every run of 2-4 consecutive words: ['good', 'grips'] -> ('gg', ['good', 'grips'])."""
    out = []
    for i in range(len(tokens)):
        for j in range(i + 2, min(i + 4, len(tokens)) + 1):
            words = tokens[i:j]
            out.append(("".join(w[0] for w in words), words))
    return out


def _abbreviation_cover(v_only: set[str], a_only: set[str], vendor_title: Any,
                        amazon_title: Any) -> tuple[set[str], set[str], bool]:
    """Remove from both sides what an abbreviation explains: vendor 'gg' vs listing 'good grips' (or the reverse)."""
    v_only, a_only = set(v_only), set(a_only)
    used = False
    for initials, words in _initial_runs(_tokens(amazon_title)):     # vendor 'gg', listing 'good grips'
        if initials in v_only:
            v_only.discard(initials)
            a_only -= set(words)
            used = True
    for initials, words in _initial_runs(_tokens(vendor_title)):     # vendor 'good grips', listing 'gg'
        if initials in a_only:
            a_only.discard(initials)
            v_only -= set(words)
            used = True
    return v_only, a_only, used


class LineIndex:
    """The line vocabulary and the line signatures of one vendor file, per brand."""

    def __init__(self, rows: Iterable[tuple[Any, Any]]) -> None:
        self.vocab: dict[str, set[str]] = defaultdict(set)
        self.signatures: dict[str, set[frozenset[str]]] = defaultdict(set)
        for brand, title in rows:
            tokens = line_tokens(title, brand)
            if tokens:
                key = _brand_key(brand)
                self.vocab[key] |= tokens
                self.signatures[key].add(tokens)

    def conflict(self, vendor_title: Any, vendor_brand: Any, amazon_title: Any) -> str | None:
        """A short plain sentence when the listing is a different product line, else None."""
        key = _brand_key(vendor_brand)
        vocab = self.vocab.get(key)
        if not vocab:
            return None
        v = line_tokens(vendor_title, vendor_brand)
        a = line_tokens(amazon_title, vendor_brand) & vocab          # only words this catalog uses as line words
        if not v or not a:
            return None
        v_only, a_only, abbreviated = _abbreviation_cover(v - a, a - v, vendor_title, amazon_title)
        if v_only and a_only:
            return (f"vendor row says {_ordered(v_only, vendor_title)}; "
                    f"the listing says {_ordered(a_only, amazon_title)}")
        if not abbreviated and a != v and a in self.signatures[key]:   # the listing is exactly ANOTHER row's line
            return (f"the listing is the '{_ordered(a, amazon_title)}' line, "
                    f"the vendor row is '{_ordered(v, vendor_title)}'")
        return None


_INDEX: dict[Any, tuple[float, LineIndex]] = {}
_LOCK = threading.Lock()


def register_line_index(key: Any, rows: Iterable[tuple[Any, Any]]) -> LineIndex:
    """Build and store an index for a run id (or 'eval').  rows = (brand, title) of the vendor rows."""
    index = LineIndex(rows)
    with _LOCK:
        _INDEX[key] = (time.time(), index)
    return index


def _load_run(run_id: int) -> LineIndex | None:
    try:
        from services import database
        with database._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT data_json FROM analytics_catalog_rows WHERE run_id=?", (int(run_id),)).fetchall()
        pairs = []
        for r in rows:
            try:
                d = json.loads(r["data_json"] or "{}")
            except (TypeError, ValueError):
                continue
            pairs.append((d.get("brand") or "", d.get("title") or ""))
        return register_line_index(run_id, pairs) if pairs else None
    except Exception:  # noqa: BLE001 - without the vendor titles the check simply stays quiet
        return None


def index_for(key: Any) -> LineIndex | None:
    """The index for a run id (read from the run's catalog rows when needed) or for 'eval'."""
    if key is None:
        return None
    with _LOCK:
        hit = _INDEX.get(key)
    if hit and (key == "eval" or time.time() - hit[0] < CACHE_SECONDS):
        return hit[1]
    if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
        return _load_run(int(key)) or (hit[1] if hit else None)
    return hit[1] if hit else None


FILE_VERSION = "2026-10-22 v9 (abbreviations are not a different line)"