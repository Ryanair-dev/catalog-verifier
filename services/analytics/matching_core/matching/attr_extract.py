"""
Shared attribute reader.  Reads color, flavor, scent etc."""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from services.analytics.matching_core.matching.lexicon import (
    color_families,
    colors_relation,
    gender_tokens,
    get_lexicon,
    norm as _norm,
    read_phrases,
    size_code,
    title_color_words,
    variant_boxes_for,
)
try:      # the repo's own keyword categorizer (no API calls): product category from title + Amazon's rank category
    from services.analytics.product_categorizer import categorize as _categorize
except Exception:  # noqa: BLE001 - the reader works without it, the category is just left empty
    _categorize = None
from services.analytics.matching_core.matching.patterns import (
    GARMENT_NORM,
    GARMENT_SIZE_RE,
    SIZE_RE,
    VOLUME_TO_ML,
    WEIGHT_RE,
    WEIGHT_TO_G,
)

# Sources whose values are "soft": they can never be the only reason for a rejection.
SOFT_SOURCES = frozenset({"ai_title"})


def ai_title_enabled() -> bool:
    """Use what the AI title reader found, when a run has it.  The reader only runs for runs where the user
    ticked "AI title reader" in the wizard, so its results only exist for those runs.  MATCH_AI_TITLE_ATTRS=0
    in .env is a developer kill switch that ignores them everywhere."""
    raw = os.getenv("MATCH_AI_TITLE_ATTRS")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return True


# --------------------------------------------------------------------------- #
# Word lists: ALL of them come from the word library (lexicon.py + the CSV files in
# matching_core/lexicon/).  Nothing is hard-coded here any more.
# --------------------------------------------------------------------------- #


def _words(name: str) -> frozenset[str]:
    lex = get_lexicon()
    if name == "color_words":
        return title_color_words()
    if name == "flavor_words":
        return lex.flavors
    if name == "scent_words":
        return lex.scents
    if name == "generic_values":
        return lex.generic
    if name == "tone_words":
        return lex.tones
    raise KeyError(name)


def _category_name(title: Any, rank_category: Any = None) -> str:
    """Product category name from product_categorizer.py ('Clothing/Apparel/Footwear' ...), or '' when
    it is unknown.  Amazon's own sales-rank category is looked at first (the categorizer's rule)."""
    if _categorize is None:
        return ""
    try:
        category = _categorize(title=str(title or "") or None, sales_rank_category=str(rank_category or "") or None)
    except Exception:  # noqa: BLE001
        return ""
    name = str(getattr(category, "name", "") or "")
    return "" if name.lower() == "unknown" else name


def is_generic(value: Any) -> bool:
    """'No Flavor', 'One Color', 'Multicolor', empty ... carry no information."""
    return _norm(value) in get_lexicon().generic


# --------------------------------------------------------------------------- #
# Readers for free text (a title, or an Amazon text field)
# --------------------------------------------------------------------------- #

def _tokens(text: Any) -> list[str]:
    spelling = get_lexicon().spelling
    glued = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(text or ""))      # "DoorsSatin Nickel" -> "Doors Satin Nickel"
    return [spelling.get(t, t) for t in re.findall(r"[a-z0-9]+", glued.lower())]


def _read_words(text: Any, vocab: frozenset[str], skip: frozenset[str] = frozenset()) -> str | None:
    seen: list[str] = []
    for tok in _tokens(text):
        if tok in vocab and tok not in skip and tok not in seen:
            seen.append(tok)
    return "/".join(seen) or None


# Gender / age group.  Title words are read only as whole words; "male" / "female" are NOT read from
# titles (hardware "male adapter"), only inside a field that is meant to hold a gender.
_GENDER_EXPAND = {"unisex": {"men", "women", "boys", "girls", "kids"},
                  "kids": {"boys", "girls", "kids"}, "boys": {"boys", "kids"}, "girls": {"girls", "kids"}}


def _read_gender(text: Any, skip: frozenset[str] = frozenset(), field: bool = False) -> str | None:
    table = gender_tokens(in_field=field)
    found: list[str] = []
    for tok in _tokens(text):
        value = table.get(tok)
        if value and tok not in skip and value not in found:
            found.append(value)
    return "/".join(found) or None


def _gender_set(value: Any) -> set[str]:
    out: set[str] = set()
    for tok in _norm(value).replace("/", " ").split():
        out |= _GENDER_EXPAND.get(tok, {tok})
    return out


# The repo's own abbreviation list (abbreviations.json at the repo root, or the file named in
# ABBREVIATIONS_FILE).  It is used ONLY to turn an abbreviation into a word the reader already
# knows (a color, flavor, scent or clothing size word):  "blk" -> "black", "med" -> "medium".
# Every other entry ("in" -> "inch", "no" -> "number") is ignored on purpose.
_ABBREV_FILE = Path(__file__).resolve().parents[4] / "abbreviations.json"
_KNOWN_SIZE_WORDS = frozenset({"small", "medium", "large", "extra", "xs", "xl", "xxl", "xxxl", "xxs"})


def _abbrev_pairs(data: Any):
    """Yield (abbreviation, full text) from the common JSON shapes."""
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, str):
                yield str(key), value
            elif isinstance(value, (list, tuple)) and value and all(isinstance(x, str) for x in value):
                yield str(key), value[0]
            elif isinstance(value, (dict, list)):
                yield from _abbrev_pairs(value)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                abbr = next((item[k] for k in ("abbr", "abbreviation", "short", "key", "from") if isinstance(item.get(k), str)), None)
                full = next((item[k] for k in ("full", "expansion", "meaning", "long", "value", "to") if isinstance(item.get(k), str)), None)
                if abbr and full:
                    yield abbr, full
            elif isinstance(item, (list, tuple)) and len(item) >= 2 and all(isinstance(x, str) for x in item[:2]):
                yield item[0], item[1]


@lru_cache(maxsize=1)
def _abbrev_map() -> dict[str, str]:
    """abbreviation -> attribute words, from abbreviations.json (repo root, or ABBREVIATIONS_FILE) AND from
    matching_core/lexicon/abbreviations_learned.csv (pairs a person accepted from attr_ai suggestions).
    Only expansions into words the reader already knows (colors, flavors, scents, clothing sizes) are kept:
    "blk" -> "black", "med" -> "medium".  Restart the app after accepting new pairs (this is cached)."""
    pairs: list[tuple[str, str]] = []
    path = Path(os.getenv("ABBREVIATIONS_FILE") or _ABBREV_FILE)
    try:
        pairs.extend(_abbrev_pairs(json.loads(path.read_text(encoding="utf-8"))))
    except (OSError, ValueError):
        pass
    try:
        from services.analytics.matching_core.matching.lexicon import LEXICON_DIR
        learned = LEXICON_DIR / "abbreviations_learned.csv"
        if learned.is_file():
            for line in learned.read_text(encoding="utf-8-sig").splitlines():
                line = line.split("#", 1)[0].strip()
                if "," in line:
                    abbr, full = (part.strip() for part in line.split(",", 1))
                    if abbr and full:
                        pairs.append((abbr.rstrip("."), full))
    except OSError:
        pass
    known = _words("color_words") | _words("flavor_words") | _words("scent_words") | _KNOWN_SIZE_WORDS
    out: dict[str, str] = {}
    for abbr, full in pairs:
        a = abbr.strip().lower()
        words = _tokens(full)
        if (re.fullmatch(r"[a-z0-9]{2,8}", a) and not a.isdigit() and a not in known
                and words and all(w in known for w in words)):
            out[a] = " ".join(words)
    return out


def _expand_abbreviations(text: str) -> str:
    amap = _abbrev_map()
    if not amap or not text:
        return text
    return re.sub(r"[A-Za-z0-9]+", lambda m: amap.get(m.group(0).lower(), m.group(0)), text)


# Units Amazon uses in its measurement fields ("fluid_ounces", "grams", "inches" ...).
_WEIGHT_G = {"gram": 1.0, "grams": 1.0, "g": 1.0, "milligram": 0.001, "milligrams": 0.001,
             "kilogram": 1000.0, "kilograms": 1000.0, "kg": 1000.0,
             "ounce": 28.3495, "ounces": 28.3495, "oz": 28.3495,
             "pound": 453.592, "pounds": 453.592, "lb": 453.592, "lbs": 453.592}
_VOLUME_ML = {"milliliter": 1.0, "milliliters": 1.0, "ml": 1.0, "liter": 1000.0, "liters": 1000.0, "l": 1000.0,
              "fluid ounce": 29.5735, "fluid ounces": 29.5735, "fl oz": 29.5735,
              "gallon": 3785.41, "gallons": 3785.41, "quart": 946.353, "quarts": 946.353,
              "pint": 473.176, "pints": 473.176, "cup": 236.588, "cups": 236.588}
_LENGTH_IN = {"inch": 1.0, "inches": 1.0, "in": 1.0, "foot": 12.0, "feet": 12.0, "ft": 12.0,
              "yard": 36.0, "yards": 36.0, "centimeter": 0.393701, "centimeters": 0.393701, "cm": 0.393701,
              "millimeter": 0.0393701, "millimeters": 0.0393701, "mm": 0.0393701,
              "meter": 39.3701, "meters": 39.3701}


def _convert(value: Any, unit: Any, table: dict[str, float]) -> float | None:
    n = _num(value)
    factor = table.get(_norm(str(unit or "").replace("_", " ")))
    return round(float(n) * factor, 2) if n is not None and factor else None


def _dims_in(entry: Any) -> list[float] | None:
    """{"length": {"unit", "value"}, "width": ..., "height": ...}  ->  [L, W, H] in inches."""
    if not isinstance(entry, dict):
        return None
    out = []
    for side in ("length", "width", "height"):
        part = entry.get(side)
        value = _convert(part.get("value"), part.get("unit"), _LENGTH_IN) if isinstance(part, dict) else None
        if value is not None:
            out.append(value)
    return out or None


def _volume_ml(text: str) -> float | None:
    m = SIZE_RE.search((text or "").lower())
    if not m:
        return None
    factor = VOLUME_TO_ML.get(m.group(2).replace(" ", "").replace(".", ""))
    return round(float(m.group(1)) * factor, 1) if factor else None


def _weight_g(text: str) -> float | None:
    m = WEIGHT_RE.search((text or "").lower())
    if not m:
        return None
    factor = WEIGHT_TO_G.get(m.group(2).replace(" ", "").replace(".", ""))
    return round(float(m.group(1)) * factor, 1) if factor else None


def _garment(text: str) -> str | None:
    found: list[str] = []
    for m in GARMENT_SIZE_RE.finditer(text or ""):
        norm = GARMENT_NORM.get(m.group(1).lower().replace("-", "").replace(" ", ""))
        if norm and norm not in found:
            found.append(norm)
    return "/".join(found) or None




_SIZE_TOKEN = re.compile(GARMENT_SIZE_RE.pattern + r"|\b[a-z0-9]+\b", re.IGNORECASE)


def _size_field(text: Any) -> str | None:
    """Clothing size from a field that is meant to hold a size: 'Medium', 'M', 'S/M', 'XS/S', 'XL'.
    Size words (Small, X-Large ...) are always read.  Bare letters (S, M, L) are trusted only when
    the whole field is nothing but sizes, never inside a title or next to other words ("1 L")."""
    matches = list(_SIZE_TOKEN.finditer(str(text or "")))

    def code_of(m: re.Match) -> str | None:
        if m.group(1):
            return GARMENT_NORM.get(m.group(1).lower().replace("-", "").replace(" ", ""))
        return size_code(m.group(0))

    only_sizes = bool(matches) and all(code_of(m) for m in matches)
    found: list[str] = []
    for m in matches:
        code = code_of(m) if (m.group(1) or only_sizes) else None
        if code and code not in found:
            found.append(code)
    return "/".join(found) or None


# Model / part numbers: "93002-943", "98150-024", "819SCELIP-26", "916".  At least 3 digits, never a measure
# ("500 mg", "100 ml"), a year, or a decimal.  Compared dash-insensitively (93002-943 = 93002943).
_MODEL_RE = re.compile(r"[A-Za-z]{0,6}\d{3,6}[A-Za-z0-9]{0,8}(?:[-/][A-Za-z0-9]{1,6})*")
_MODEL_UNITS = {"oz", "ml", "l", "g", "kg", "lb", "lbs", "mg", "mcg", "iu", "ct", "count", "pack", "pk", "pcs", "pc",
                "piece", "pieces", "in", "inch", "inches", "cm", "mm", "ft", "yd", "w", "v", "mah", "hz", "gb", "tb",
                "mp", "k", "x", "pair", "pairs", "ea", "each", "gal", "fl", "cs", "case", "box", "bag", "roll",
                "rolls", "sheets", "feet", "percent"}


def _model_tokens(text: Any) -> list[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9./-]*[A-Za-z0-9]|[A-Za-z0-9]", str(text or ""))
    out: list[str] = []
    for i, word in enumerate(words):
        core = word.strip(".-/")
        if "." in core or not _MODEL_RE.fullmatch(core) or len(re.sub(r"\D", "", core)) < 3:
            continue
        if re.fullmatch(r"(19|20)\d{2}", core):
            continue
        if i + 1 < len(words) and words[i + 1].lower() in _MODEL_UNITS:
            continue
        key = re.sub(r"[^A-Z0-9]", "", core.upper())
        if key not in out:
            out.append(key)
    return out


def _looks_like_barcode(token: str) -> bool:
    """760172011715 (a UPC) or INJ760172021332 (a UPC with a short prefix): 11+ digits in a row."""
    return bool(re.fullmatch(r"[A-Z]{0,4}\d{11,14}", token))


def _model_value(value: Any) -> str | None:
    """A model number from a field that is meant to hold one (Amazon part number / model number, a vendor item id).
    Barcodes in such a field are skipped: they are not model numbers."""
    tokens = [t for t in _model_tokens(value) if not _looks_like_barcode(t)]
    if not tokens and _model_tokens(value):
        return None
    if not tokens:
        whole = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
        if len(whole) >= 3 and any(c.isdigit() for c in whole) and " " not in str(value or "").strip():
            tokens = [whole]
    return "/".join(tokens) or None


# Explicit shade codes: "#46", "No. 47", "Shade 3N", "Color 46", and a bare number right before a tone word
# ("48 Dark Chestnut").  Copied from the old matcher's _SHADE_NUM_RE.  Hair color and cosmetics are
# keyed by this code: brand, size and type are identical across a whole line and only the shade differs.
_SHADE_NUM_RE = re.compile(
    r'#\s*(\d{1,3}[a-z]?)\b'
    r'|\bno\.?\s*(\d{1,3}[a-z]?)\b'
    r'|\bshade\s*(\d{1,3}[a-z]?)\b'
    r'|\bcolou?r\s+(\d{1,3}[a-z]?)\b'
    r'|\b(\d{1,3})\s+(?:light|lightest|medium|dark|darkest|deep|ash|blonde|blond|brown|chestnut|auburn|burgundy)\b',
    re.IGNORECASE,
)


def _shade_codes(text: Any) -> list[str]:
    out: list[str] = []
    for m in _SHADE_NUM_RE.finditer(str(text or "")):
        for g in m.groups():
            if g and g.lower() not in out:
                out.append(g.lower())
    return out


_MULTI_PACKS = re.compile(r"\b(\d+)\s+packs?\s+of\s+(\d+)\b")            # "3 Packs of 24"
_PACK_OF = re.compile(r"\b(?:pack|box|set|bundle)\s+of\s+(\d+)\b")        # "Pack of 12"  (not "case of": vendor case quantity)
_COUNT = re.compile(r"\b(\d+)\s*[-\s]?(?:count|ct|each|ea|pcs?|pieces?|pairs?|rolls?|tubes?|vials?|sachets?)\b")  # "4 Count", "2 Each", "6 Rolls"
_BUNDLE = re.compile(r"\b(\d+)\s*[-\s]?(?:pack|pk)\b")                    # "4 Pack": count OR pack
_MAX_NUMBER = 1000          # bigger numbers are usually SKU codes, not pack sizes


def _counts(text: str) -> dict[str, list[int]]:
    """Counts written in a text, kept apart because the words are ambiguous:
    count = units in one package ('4 Count', '2 Each'), pack = number of packages
    ('Pack of 12'), bundle = '4 Pack' (can mean either)."""
    s = (text or "").lower()
    out: dict[str, list[int]] = {"count": [], "pack": [], "bundle": []}

    def keep(name: str, raw: str) -> None:
        n = int(raw)
        if 0 < n <= _MAX_NUMBER and n not in out[name]:
            out[name].append(n)

    for m in _MULTI_PACKS.finditer(s):
        keep("pack", m.group(1)); keep("count", m.group(2))
    s = _MULTI_PACKS.sub(" ", s)
    for m in _PACK_OF.finditer(s):
        keep("pack", m.group(1))
    s = _PACK_OF.sub(" ", s)
    for m in _COUNT.finditer(s):
        keep("count", m.group(1))
    s = _COUNT.sub(" ", s)
    for m in _BUNDLE.finditer(s):
        keep("bundle", m.group(1))
    return out


# --------------------------------------------------------------------------- #
# Collecting values from several sources, then resolving them
# --------------------------------------------------------------------------- #

_NUMERIC = {"volume_ml", "weight_g", "count", "pack", "bundle"}


class _Bag:
    def __init__(self, allowed: frozenset[str] | None = None) -> None:
        # allowed = the boxes that can vary for this item's product category (None = category unknown: all)
        self.allowed = allowed
        self.items: dict[str, list[tuple[Any, str]]] = {}
        self.all_numbers: dict[tuple[str, str], list[int]] = {}

    def _read(self, box: str, source: str) -> bool:
        """Flavor / scent / shade words are read from a TITLE only for categories where they can vary
        ("cotton", "mint", "sunflower" in a sock title are not scents or flavors).  The agent's expanded
        title ("ai_title") follows the same rule."""
        return source not in ("title", "ai_title") or self.allowed is None or box in self.allowed

    def add(self, field: str, value: Any, source: str) -> None:
        if value is None or value == "" or value == []:
            return
        self.items.setdefault(field, []).append((value, source))

    def add_text(self, text: Any, source: str, skip_flavor_scent: frozenset[str] = frozenset(),
                 words: bool = True) -> None:
        """Read the boxes that can be read from free text.  words=False (used for SIZE fields) reads only
        sizes, volumes, weights and counts, never color / flavor / scent / shade / gender words: the word
        "Medium" in a size field is a size, not a shade."""
        t = _expand_abbreviations(str(text or ""))
        if not t.strip():
            return
        if words:
            self.add("color", _read_words(t, _words("color_words")), source)
            if self._read("flavor", source):
                self.add("flavor", _read_words(t, _words("flavor_words"), skip_flavor_scent), source)
            if self._read("scent", source):
                scent_skip = skip_flavor_scent | _words("flavor_words")
                self.add("scent", _read_words(t, _words("scent_words"), scent_skip), source)
            if self._read("shade", source):
                codes = _shade_codes(t)
                self.add("shade_code", "/".join(codes) or None, source)
                # tone words (light, medium, dark ...) are everywhere in titles ("Medium" is usually a size), so
                # they are read only for a shade product: a shade code next to them, or a hair / cosmetics category
                if codes or (self.allowed is not None and "shade" in self.allowed):
                    self.add("tone", _read_words(t, _words("tone_words")), source)
            self.add("gender", _read_gender(t, skip_flavor_scent), source)
            self.add("finish", "/".join(read_phrases(t, "finishes")) or None, source)      # satin nickel, venetian bronze
            self.add("type", "/".join(read_phrases(t, "types")) or None, source)           # privacy, passage, dummy, entry
            self.add("model", _model_value(text), source)                                  # 93002-943
        self.add("size", _garment(t), source)
        self.add("volume_ml", _volume_ml(t), source)
        self.add("weight_g", _weight_g(t), source)
        for name, nums in _counts(t).items():
            if nums:
                self.add(name, nums[0], source)
                if len(nums) > 1:                      # keep the others for later study
                    self.all_numbers[(name, source)] = nums

    def resolve(self) -> dict[str, dict]:
        self._drop_color_words_from_flavor_scent()
        out: dict[str, dict] = {}
        for field, entries in self.items.items():
            value, source = entries[0]
            box: dict[str, Any] = {"value": value, "source": source}
            if source in SOFT_SOURCES:
                box["soft"] = True                     # never the only reason for a rejection
            if field == "color":
                families = color_families(value)
                if families:
                    box["family"] = sorted(families)
            if (field, source) in self.all_numbers:
                box["all"] = self.all_numbers[(field, source)]
            others = [(v, s) for v, s in entries[1:] if not _agree(field, value, v)]
            if others:
                box["conflict"] = True
                box["also"] = [{"value": v, "source": s} for v, s in others]
            out[field] = box
        return out


def _drop_color_words(self: "_Bag") -> None:
    """If a title word is already the item's color (Mint, Lime, Sunflower ...), it is not also a
    flavor or a scent. Only title-read values are dropped; values from Amazon or the AI stay."""
    colors: set[str] = set()
    for value, _ in self.items.get("color", []):
        colors |= set(_norm(value).replace("/", " ").split())
    if not colors:
        return
    for box in ("flavor", "scent"):
        kept = []
        for value, source in self.items.get(box, []):
            words = set(_norm(value).replace("/", " ").split())
            if source in ("title", "ai_title") and words and words <= colors:
                continue
            kept.append((value, source))
        if kept:
            self.items[box] = kept
        else:
            self.items.pop(box, None)


_Bag._drop_color_words_from_flavor_scent = _drop_color_words


def _size_set(value: Any) -> frozenset[str]:
    return frozenset(t for t in str(value or "").upper().replace(" ", "").split("/") if t)


def size_relation(a: Any, b: Any) -> str:
    """'same' (S vs S), 'partial' (S vs XS/S, M vs M/L: one combined size shares a part) or 'different'."""
    sa, sb = _size_set(a), _size_set(b)
    if not sa or not sb:
        return "different"
    if sa == sb:
        return "same"
    return "partial" if sa & sb else "different"


def _agree(field: str, a: Any, b: Any) -> bool:
    if field == "size":
        return size_relation(a, b) == "same"
    if field == "gender":
        return bool(_gender_set(a) & _gender_set(b))
    if field in _NUMERIC:
        try:
            x, y = float(a), float(b)
        except (TypeError, ValueError):
            return False
        return abs(x - y) <= 0.02 * max(abs(x), abs(y), 1e-9)
    words = lambda v: set(_norm(v).replace("/", " ").split())
    if field in ("finish", "type", "model"):
        return bool(set(str(a).split("/")) & set(str(b).split("/")))      # whole phrases / whole numbers
    if field == "color":
        relation = colors_relation(a, b)            # base colors: black = onyx, black != steel
        if relation != "unknown":
            return relation == "same"
    if field in ("flavor", "scent"):
        # the old matcher's rule: "Lavender" ~ "Lavender Vanilla" (one contains the other) agree, but
        # "Tahitian Grapefruit Splash" vs "Grapefruit Orange Blossom" (each has its own extra word) differ
        wa, wb = words(a), words(b)
        return wa <= wb or wb <= wa
    return bool(words(a) & words(b))


def _clean_value(value: Any, brand: str = "") -> str | None:
    """A usable text value, or None for empty/generic values and for a repeated brand name."""
    v = _norm(value)
    if not v or is_generic(v):
        return None
    b = _norm(brand)
    if b:                                   # whole WORDS, never letters: brand "B" must not drop "Apple Blossom"
        value_words, brand_words = set(v.replace("/", " ").split()), set(b.replace("/", " ").split())
        if value_words <= brand_words or brand_words <= value_words:
            return None
    return v


_COLOR_NOISE_OK = frozenset({"and", "with"})


def _clean_color(value: Any) -> str | None:
    """A color value with the junk taken out.  When the value holds at least one real color word
    (any word of the color library, also the field-only ones like granite) and also other words
    ("liner socks gray", "e4824 black 3 pairs"), only the color words (and their qualifiers: light,
    dark ...) are kept.  A value with no known color word ("poseidon", "celestine") is kept as it is."""
    v = _clean_value(value)
    if not v:
        return None
    colors = get_lexicon().colors
    parts = []
    for part in v.split("/"):
        words = part.split()
        known = [w for w in words if w in colors]
        if any(colors[w][1] != "~" for w in known) and len(known) < len(words):
            words = known
        if words:
            parts.append(" ".join(words))
    cleaned = "/".join(dict.fromkeys(p for p in parts if p))
    return cleaned or None


def _num(value: Any) -> float | None:
    try:
        n = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return int(n) if n == int(n) else n


# --------------------------------------------------------------------------- #
# The vendor file's own columns ("Color", "Size", "Flavor" ...)
# --------------------------------------------------------------------------- #

# (words that must be in the column header, box).  Headers about case/carton/pallet/order
# quantities are never read: a case quantity is not a pack size.
_COLUMN_RULES = (
    (("color", "colour", "shade"), "color"),
    (("flavor", "flavour"), "flavor"),
    (("scent", "fragrance"), "scent"),
    (("material", "fabric"), "material"),
    (("size",), "size_text"),
    (("gender", "department", "sex"), "gender"),
    (("unit count", "item count", "count per"), "count"),
)
_COLUMN_SKIP = ("case", "carton", "pallet", "min", "order", "master", "inner", "weight class")
_COLUMN_SKIP_WORDS = frozenset({"code", "sku", "upc", "ean", "gtin", "number", "num", "no", "id",
                                "ref", "reference", "hex", "rgb"})


_STYLE_HEADER = re.compile(r"\bstyle\b")


def _style_value(text: str) -> str | None:
    """'052100', '213130-01': one code with at least 3 digits (a style NAME like 'Run Lightweight' is not one)."""
    t = re.sub(r"\.0+$", "", text.strip())                    # 52100.0 (a number read from Excel)
    code = re.sub(r"[^A-Z0-9]", "", t.upper())
    return code if " " not in t and len(re.sub(r"\D", "", code)) >= 3 and len(code) <= 20 else None


def _read_columns(bag: "_Bag", raw: dict[str, Any], brand: str) -> list[str]:
    used: list[str] = []
    for header, value in (raw or {}).items():
        h = _norm(header)
        text = "" if value is None else str(value).strip()
        if not h or not text or is_generic(text):
            continue
        if _STYLE_HEADER.search(h) and _style_value(text):      # STYLE NUMBER / STYLE # / STYLE CODE
            bag.add("style", _style_value(text), "vendor_column")
            used.append(str(header))
            continue
        if any(bad in h for bad in _COLUMN_SKIP) or (set(h.split()) & _COLUMN_SKIP_WORDS):
            continue
        for words, box in _COLUMN_RULES:
            if not any(w in h for w in words):
                continue
            if box in ("color",):
                bag.add("color", _clean_color(text), "vendor_column")
            elif box in ("flavor", "scent", "material"):
                bag.add(box, _clean_value(text, brand), "vendor_column")
            elif box == "size_text":
                bag.add("size", _size_field(text), "vendor_column")      # the column's own full value first: "XS/S", not "XS"
                bag.add_text(text, "vendor_column", words=False)
            elif box == "gender":
                bag.add("gender", _read_gender(text, field=True), "vendor_column")
            elif box == "count":
                n = _num(text)
                if n:
                    bag.add("count", n, "vendor_column")
            used.append(str(header))
            break
    return used


def _ai_record(source: dict[str, Any], extracted: dict[str, Any]) -> dict | None:
    """What the AI title reader saved for this vendor row (agents/attr_ai.py), when the switch is on."""
    if not ai_title_enabled():
        return None
    record = source.get("ai_unabbrev") or (extracted or {}).get("ai_unabbrev")
    return record if isinstance(record, dict) else None


def _expanded_title(record: dict | None, title: Any) -> str:
    """The title with its abbreviations written out ('OXO GG SS Peeler' -> 'OXO Good Grips Stainless Steel Peeler')."""
    expanded = str((record or {}).get("expanded_title") or "").strip()
    return "" if not expanded or expanded.lower() == str(title or "").strip().lower() else expanded


def _add_ai_attributes(bag: "_Bag", record: dict | None, title: Any, brand: str) -> None:
    """The attributes the AI read from the title, each already checked against a quote from the title.
    Added LAST, so they only fill what the rules left empty (a disagreement makes the attribute 'unclear').
    A value whose words are written in the title as they are counts like a rule (source 'ai_read'); a value decoded
    from an abbreviation or code ('15' -> satin nickel) stays soft (source 'ai_title': never rejects alone)."""
    title_words = set(_tokens(title))
    for box, entry in ((record or {}).get("attributes") or {}).items():
        value = str((entry or {}).get("value") if isinstance(entry, dict) else entry or "").strip()
        if not value:
            continue
        source = "ai_read" if set(_tokens(value)) <= title_words else "ai_title"
        if box in ("flavor", "scent") and not (bag.allowed is None or box in bag.allowed):
            continue                                   # same category rule as for title words
        if box == "color":
            bag.add("color", _clean_color(value), source)
        elif box == "finish":
            bag.add("finish", "/".join(read_phrases(value, "finishes")) or _norm(value), source)
        elif box == "type":
            bag.add("type", "/".join(read_phrases(value, "types")) or _norm(value), source)
        elif box == "size":
            bag.add("size", _size_field(value) or _garment(value), source)
        elif box in ("flavor", "scent", "material"):
            bag.add(box, _clean_value(value, brand), source)
        elif box == "gender":
            bag.add("gender", _read_gender(value, field=True), source)
        elif box == "count":
            n = _num(re.sub(r"[^\d.]", "", value))
            if n:
                bag.add("count", n, source)
        elif box == "volume":
            bag.add("volume_ml", _volume_ml(value), source)
        elif box == "weight":
            bag.add("weight_g", _weight_g(value), source)


# --------------------------------------------------------------------------- #
# Public: vendor side and Amazon side
# --------------------------------------------------------------------------- #

def extract_vendor(source: dict[str, Any], extracted: dict[str, Any] | None = None) -> dict[str, Any]:
    """Boxes for one vendor row.  source = the row's dict (title, brand, and raw = the original
    columns of the vendor file); extracted = the saved AI fields for the row.
    Order of trust: the vendor file's own columns, then the AI, then the title, then (only with
    MATCH_AI_TITLE_ATTRS=1) the unabbreviation agent's expanded title, which is "soft"."""
    ai = extracted or {}
    brand = str(source.get("brand") or ai.get("brand") or "")
    skip = frozenset(_tokens(brand))
    category = _category_name(f"{source.get('title') or ''} {ai.get('product_type') or ''}")
    bag = _Bag(variant_boxes_for(category))
    columns_used = _read_columns(bag, source.get("raw") or {}, brand)
    # open words: the AI first (when it returns them), then the title
    bag.add("color", _clean_color(ai.get("color")), "ai")
    for field in ("flavor", "scent", "material"):
        bag.add(field, _clean_value(ai.get(field), brand), "ai")
    for model_text in (source.get("mpn"), source.get("itemid")):
        bag.add("model", _model_value(model_text), "vendor_column")
    bag.add_text(source.get("title"), "title", skip)
    # the AI title reader (agents/attr_ai.py): read LAST, so it only fills boxes nothing above stated; when it
    # disagrees with something above, the box gets "conflict" (= unknown, Review) instead of a new value
    record = _ai_record(source, ai)
    expanded = _expanded_title(record, source.get("title"))
    if expanded:
        bag.add_text(expanded, "ai_title", skip)
    _add_ai_attributes(bag, record, source.get("title"), brand)
    # numbers and units: the title first, then what the AI wrote in its size / pack fields
    bag.add_text(" ".join(str(ai.get(k) or "") for k in ("size", "pack_info")), "ai", skip, words=False)
    n = _num(ai.get("unit_count"))
    if n:
        bag.add("count", n, "ai")
    identity = {
        "brand": brand,
        "title": str(source.get("title") or ""),
        "mpn": str(source.get("mpn") or source.get("itemid") or ""),
        "upcs": [u for u in (str(source.get("upc") or "").strip(),) if u],
        "product_type": str(ai.get("product_type") or ""),
        "model": str(ai.get("model") or ""),
        "category": category,
    }
    notes = {"brand": brand, "columns_used": columns_used}
    if expanded:
        notes["ai_expanded_title"] = expanded
    if record:
        notes["ai_read"] = {"why": record.get("why"), "attributes": record.get("attributes") or {}}
    return {"identity": identity, "fields": bag.resolve(), "notes": notes}


def extract_amazon(item: dict[str, Any]) -> dict[str, Any]:
    """Boxes for one Amazon item (the dict from normalize_amazon_item).  Works on older saved
    items too, because it falls back to the raw Amazon response kept inside the item."""
    attrs = item.get("attributes") or {}
    raw = item.get("_raw") or {}
    raw_attrs = raw.get("attributes") or {}
    raw_sum = (raw.get("summaries") or [{}])[0] or {}

    def raw_attr(key: str) -> str:
        entries = raw_attrs.get(key) or []
        first = entries[0] if entries else None
        value = first.get("value") if isinstance(first, dict) else first
        return "" if value is None else str(value).strip()

    def raw_entry(key: str) -> dict:
        entries = raw_attrs.get(key) or []
        return entries[0] if entries and isinstance(entries[0], dict) else {}

    def raw_type(key: str) -> str:
        t = raw_entry(key).get("type")
        return str(t.get("value") if isinstance(t, dict) else t or "").strip()

    def pick(*candidates: Any) -> str:
        return next((str(c).strip() for c in candidates if c not in (None, "")), "")

    brand = pick(item.get("brand"), item.get("manufacturer"))
    skip = frozenset(_tokens(brand))
    category = _category_name(item.get("title"), item.get("sales_rank_category"))
    bag = _Bag(variant_boxes_for(category))
    # --- fields Amazon filled in ---
    bag.add("color", _clean_color(attrs.get("color")), "amazon_field")
    bag.add("color", _clean_color(pick(item.get("summary_color"), raw_sum.get("colorName"))), "amazon_field")
    bag.add("flavor", _clean_value(pick(item.get("flavor"), raw_attr("flavor")), brand), "amazon_field")
    bag.add("scent", _clean_value(pick(item.get("scent"), raw_attr("scent")), brand), "amazon_field")
    bag.add("material", _clean_value(pick(attrs.get("material"), raw_attr("material")), brand), "amazon_field")
    # Amazon's "size" field is a mixed bag: "4 Count (Pack of 1)", "AA", "Large" ...
    for size_text in (attrs.get("size"), item.get("summary_size"), raw_sum.get("sizeName")):
        if size_text and not is_generic(size_text):
            bag.add_text(size_text, "amazon_field", skip, words=False)
            if "size" not in bag.items:
                bag.add("size", _size_field(size_text), "amazon_field")
    unit_type = _norm(pick(item.get("unit_count_type"), raw_type("unit_count")).replace("_", " "))
    unit_count = _num(pick(item.get("unit_count"), raw_attr("unit_count")))
    if unit_count and unit_type in ("", "count", "each", "piece", "pieces"):
        bag.add("count", unit_count, "amazon_field")
    elif unit_count:        # unit_count can also be a measure: 8 "Fluid Ounces", 100 "Grams"
        bag.add("volume_ml", _convert(unit_count, unit_type, _VOLUME_ML), "amazon_field")
        bag.add("weight_g", _convert(unit_count, unit_type, _WEIGHT_G), "amazon_field")
    # gender: Amazon fields first (here "male" / "female" are real values), then the title below
    for gender_text in (pick(item.get("target_gender"), raw_attr("target_gender")),
                        pick(item.get("department"), raw_attr("department")),
                        pick(item.get("age_range_description"), raw_attr("age_range_description"))):
        bag.add("gender", _read_gender(gender_text, field=True), "amazon_field")
    for model_text in (item.get("mpn"), item.get("model_number"), item.get("model_name"), raw_attr("model_number"),
                       raw_attr("part_number"), raw_attr("model_name"), raw_sum.get("partNumber"), raw_sum.get("modelNumber")):
        bag.add("model", _model_value(model_text), "amazon_field")
    # --- the title ---
    bag.add_text(item.get("title"), "title", skip)
    # "Women's" / "Men's" in the title beats Amazon's gender field "unisex" (seen on Injinji listings)
    genders = bag.items.get("gender") or []
    if genders and genders[0][0] == "unisex":
        titled = [g for g in genders if g[1] == "title" and g[0] in ("men", "women")]
        if titled:
            bag.items["gender"] = titled + [g for g in genders if g not in titled]

    barcodes: list[str] = []
    for value in [*(item.get("upcs") or []), *(item.get("eans") or []), *(item.get("gtins") or []),
                  item.get("upc"), item.get("ean"), item.get("gtin")]:
        if value and str(value).strip() not in barcodes:
            barcodes.append(str(value).strip())
    for group in raw.get("identifiers") or []:                 # older saved items: read them all
        for ident in (group or {}).get("identifiers", []):
            value = str(ident.get("identifier") or "").strip()
            if value and value not in barcodes:
                barcodes.append(value)
    identity = {
        "asin": str(item.get("asin") or raw.get("asin") or ""),
        "brand": brand,
        "manufacturer": pick(item.get("manufacturer"), raw_sum.get("manufacturer")),
        "title": str(item.get("title") or ""),
        "mpn": pick(item.get("mpn"), raw_sum.get("partNumber"), raw_sum.get("modelNumber")),
        "upcs": barcodes,
        "model_name": pick(item.get("model_name"), raw_attr("model_name")),
        "model_number": pick(raw_attr("model_number"), raw_sum.get("modelNumber")),
        "category": category,
    }
    notes = {
        "brand": brand,
        "item_weight_g": _convert(raw_entry("item_weight").get("value"), raw_entry("item_weight").get("unit"), _WEIGHT_G),
        "package_weight_g": _convert(raw_entry("item_package_weight").get("value"),
                                     raw_entry("item_package_weight").get("unit"), _WEIGHT_G),
        "item_dimensions_in": _dims_in(raw_entry("item_dimensions")),
        "package_dimensions_in": _dims_in(raw_entry("item_package_dimensions")),
        "style": pick(item.get("style"), raw_attr("style"), raw_sum.get("styleName")),
        "item_form": pick(item.get("item_form"), raw_attr("item_form")),
        "amazon_counts": {            # kept raw: these three often disagree on the same listing
            "unit_count": unit_count,
            "number_of_items": _num(pick(item.get("number_of_items"), raw_attr("number_of_items"))),
            "item_package_quantity": _num(pick(item.get("item_package_quantity"),
                                               raw_attr("item_package_quantity"))),
        },
    }
    return {"identity": identity, "fields": bag.resolve(), "notes": notes}


