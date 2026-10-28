"""
The word library.  Every list the attribute reader uses lives HERE, in one place:
    colors          word, canonical, family, in_titles     (steel -> gray; onyx -> black ...)
    flavors         word
    scents          word
    sizes           alias, canonical                       (sm -> S, 2xl -> XXL)
    generic_values  value                                  ("No Flavor", "One Color", "Multicolor" ...)
    spelling        from, to                               (grey -> gray)
    category_boxes  category, boxes                        (which attributes can be a real variant for a
                                                            product category from product_categorizer.py)
    tones           word                                   (light / dark / deep ...: shade qualifiers)
    gender          word, value, in_titles                 (men / women / kids words)
    line_filler     word                                   (words ignored by the product-line check)
    finishes        phrase, canonical                      (hardware finishes: satin nickel, venetian bronze ...)
    types           phrase, canonical                      (function types: privacy, passage, dummy, entry ...)

WHO EDITS IT: developers only.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

LEXICON_DIR = Path(os.getenv("LEXICON_DIR") or (Path(__file__).resolve().parent.parent / "lexicon"))

# --------------------------------------------------------------------------- #
# Built-in starter lists (the same text the --write-seed command writes to the CSV files)
# --------------------------------------------------------------------------- #

_DEFAULTS: dict[str, str] = {}

_DEFAULTS["colors"] = """
# word,canonical,family,in_titles
# family   = the base color the word belongs to.  Two words of the SAME family are the same color
#            (black = onyx).  Two words of DIFFERENT families are different colors (black / steel -> gray).
#            Leave it blank when unsure: a difference then goes to review instead of being rejected.
#            "~" marks a qualifier that is ignored (light, dark, matte ...).
# in_titles = 1: the word may be read from free text.  0: only understood inside a color field
#            ("steel" must not be read from "stainless steel", "tan" not from "tan line").
black,black,black,1
white,white,white,1
gray,gray,gray,1
brown,brown,brown,1
red,red,red,1
orange,orange,orange,1
yellow,yellow,yellow,1
green,green,green,1
blue,blue,blue,1
purple,purple,purple,1
pink,pink,pink,1
navy,navy,blue,1
maroon,maroon,red,1
burgundy,burgundy,red,1
teal,teal,,1
turquoise,turquoise,,1
clear,clear,clear,1
transparent,transparent,clear,1
translucent,translucent,clear,1
silver,silver,gray,1
gold,gold,yellow,1
beige,beige,beige,1
ivory,ivory,white,1
khaki,khaki,beige,1
charcoal,charcoal,gray,1
cyan,cyan,blue,1
magenta,magenta,pink,1
indigo,indigo,,1
violet,violet,purple,1
lavender,lavender,purple,1
blonde,blonde,blonde,1
brunette,brunette,brown,1
# names understood in color fields only
steel,steel,gray,0
slate,slate,gray,0
granite,granite,gray,0
graphite,graphite,gray,0
pewter,pewter,gray,0
smoke,smoke,gray,0
ash,ash,gray,0
onyx,onyx,black,0
jet,jet,black,0
ebony,ebony,black,0
obsidian,obsidian,black,0
raven,raven,black,0
mahogany,mahogany,brown,0
espresso,espresso,brown,0
chestnut,chestnut,brown,0
walnut,walnut,brown,0
sand,sand,beige,0
taupe,taupe,beige,0
camel,camel,beige,0
oatmeal,oatmeal,beige,0
tan,tan,beige,0
snow,snow,white,0
olive,olive,green,0
forest,forest,green,0
emerald,emerald,green,0
hunter,hunter,green,0
kelly,kelly,green,0
moss,moss,green,0
army,army,green,0
cobalt,cobalt,blue,0
sapphire,sapphire,blue,0
denim,denim,blue,0
azure,azure,blue,0
fuchsia,fuchsia,pink,0
blush,blush,pink,0
crimson,crimson,red,0
scarlet,scarlet,red,0
ruby,ruby,red,0
wine,wine,red,0
brick,brick,red,0
plum,plum,purple,0
lilac,lilac,purple,0
mauve,mauve,purple,0
eggplant,eggplant,purple,0
amethyst,amethyst,purple,0
mustard,mustard,yellow,0
celestine,celestine,blue,0
midnight,midnight,blue,0
marina,marina,blue,0
ocean,ocean,blue,0
glacier,glacier,blue,0
chili,chili,red,0
mint,mint,green,0
lime,lime,green,0
basil,basil,green,0
sage,sage,green,0
storm,storm,gray,0
dune,dune,beige,0
seascape,seascape,,0
coral,coral,,0
twilight,twilight,,0
alpine,alpine,,0
atlantis,atlantis,,0
poseidon,poseidon,,0
geranium,geranium,,0
# qualifiers (ignored when comparing colors)
light,light,~,0
dark,dark,~,0
bright,bright,~,0
deep,deep,~,0
pale,pale,~,0
hot,hot,~,0
neon,neon,~,0
royal,royal,~,0
heather,heather,~,0
heathered,heathered,~,0
matte,matte,~,0
satin,satin,~,0
metallic,metallic,~,0
pastel,pastel,~,0
mid,mid,~,0
"""

_DEFAULTS["flavors"] = """
# word   (one per line)
vanilla
chocolate
cocoa
strawberry
cherry
orange
lemon
lime
grapefruit
mint
peppermint
spearmint
apple
raspberry
blueberry
mango
peach
coconut
grape
watermelon
melon
pomegranate
banana
pineapple
caramel
cinnamon
ginger
pumpkin
honey
berry
butterscotch
mocha
coffee
hazelnut
almond
peanut
maple
cheddar
ranch
barbecue
punch
tropical
citrus
lemonade
cola
"""

_DEFAULTS["scents"] = """
# word   (one per line)
lavender
eucalyptus
pine
cedar
spruce
fig
rose
jasmine
floral
chamomile
aloe
bamboo
sandalwood
freesia
lilac
magnolia
gardenia
hibiscus
orchid
peony
blossom
tahitian
hawaiian
mediterranean
honeysuckle
ylang
bergamot
basil
sage
thyme
rosemary
cucumber
ocean
marine
aqua
glacier
arctic
rain
breeze
splash
burst
dew
wave
tide
mist
cardamom
clove
nutmeg
cotton
linen
meadow
garden
wildflower
sunflower
"""

_DEFAULTS["sizes"] = """
# alias,canonical      (clothing size written as a code; trusted only inside a field meant to hold a size)
xxs,XXS
xs,XS
s,S
sm,S
m,M
md,M
med,M
l,L
lg,L
xl,XL
xxl,XXL
2xl,XXL
xxxl,XXXL
3xl,XXXL
"""

_DEFAULTS["generic_values"] = """
# value   (carries no information: dropped from color / flavor / scent / size fields)
none
no flavor
no flavour
no smell
no scent
unflavored
unscented
fragrance free
one color
one colour
multicolor
multicolour
multi color
multi colored
multi
multiple
assorted
assorted colors
variety
n a
n/a
na
unknown
default
not applicable
other
see description
as shown
"""

_DEFAULTS["tones"] = """
# word   (shade qualifiers on a light <-> dark axis: hair color, cosmetics.  From the old matcher's _TONE_WORDS)
light
lightest
lighter
medium
dark
darkest
darker
deep
deepest
"""

_DEFAULTS["gender"] = """
# word,value,in_titles
# value: men | women | boys | girls | kids | unisex   (From the old matcher's _MALE_TERMS / _FEMALE_TERMS, plus kids)
# in_titles 0 = understood only inside a field that is meant to hold a gender (a Gender column,
#               Amazon's target_gender), never from a title ("male adapter", "Spider Man")
men,men,1
mens,men,1
gentlemen,men,1
him,men,1
his,men,1
women,women,1
womens,women,1
ladies,women,1
lady,women,1
her,women,1
hers,women,1
boys,boys,1
boy,boys,1
girls,girls,1
girl,girls,1
kids,kids,1
kid,kids,1
youth,kids,1
junior,kids,1
juniors,kids,1
toddler,kids,1
toddlers,kids,1
infant,kids,1
infants,kids,1
unisex,unisex,1
male,men,0
female,women,0
man,men,0
woman,women,0
"""

_DEFAULTS["line_filler"] = """
# word   (words that never name a product LINE; ignored by the product-line check, together with colors,
#         sizes, genders, units and brand words, which are removed automatically)
for
and
the
with
of
in
by
a
an
to
from
new
socks
sock
toesocks
toe
set
kit
"""

_DEFAULTS["finishes"] = """
# phrase,canonical      (hardware finishes: the "color" of a lock, hinge, faucet.  Whole phrases, longest match wins:
#                        "satin nickel" is read as one finish, never as "satin" + "nickel")
satin nickel,satin nickel
polished nickel,polished nickel
antique nickel,antique nickel
brushed nickel,brushed nickel
venetian bronze,venetian bronze
oil rubbed bronze,oil rubbed bronze
aged bronze,aged bronze
antique bronze,antique bronze
polished brass,polished brass
lifetime polished brass,lifetime polished brass
antique brass,antique brass
satin brass,satin brass
vintage brass,vintage brass
polished chrome,polished chrome
satin chrome,satin chrome
matte black,matte black
flat black,flat black
iron black,iron black
black stainless,black stainless
stainless steel,stainless steel
satin stainless,satin stainless
brass,brass
bronze,bronze
chrome,chrome
nickel,nickel
copper,copper
"""

_DEFAULTS["types"] = """
# phrase,canonical      (what the product DOES: the function type that separates look-alike listings of one line)
privacy,privacy
bed and bath,privacy
bedroom and bathroom,privacy
bed bath,privacy
passage,passage
hall closet,passage
hallway and closet,passage
hall and closet,passage
dummy,dummy
keyed entry,entry
entry,entry
deadbolt,deadbolt
handleset,handleset
keypad,keypad
smart lock,smart lock
"""

_DEFAULTS["variant_words"] = """
# word   (a size, unit or quantity word: it describes a variant or an amount, never the product itself.
#         Dropped from a title's product words when vendor rows are grouped into families.)
# size words
xxs
xs
s
m
l
xl
xxl
xxxl
small
medium
large
extra
x
xx
xxx
size
sizes
# units and quantities
oz
ounce
ounces
fl
ml
liter
liters
litre
lb
lbs
pound
pounds
kg
g
gram
grams
mg
gal
gallon
count
ct
pack
packs
pk
pcs
pc
piece
pieces
each
ea
pair
pairs
case
box
bundle
inch
inches
qty
"""

_DEFAULTS["category_boxes"] = """
# category,boxes     (boxes: color | size | flavor | scent | shade, separated by |)
# Which attributes can be a REAL variant for a product category.  Used only by the rule
# "the listing states a variant but the vendor row does not -> review".  A category that is not
# listed here uses all five.  Category names are the ones in product_categorizer.py.
# flavor / scent / shade are ALSO only read from titles for categories that list them
# (so "cotton" or "mint" in a sock title is never taken for a scent or a flavor).
# An empty list means: for this category, never ask for review because a variant is not stated.
CPG/Household,flavor|scent|size
Janitorial/Cleaning,scent
Food/Beverage,flavor|scent
Laboratory/Scientific,size
Medical/Healthcare,color|size|flavor|scent
Dental/Oral,color|flavor|scent
Industrial/Safety,color|size
Clothing/Apparel/Footwear,color|size
Personal Care/Beauty,color|size|flavor|scent|shade
Baby/Infant,color|size|flavor|scent
Pet Supplies,color|size|flavor|scent
Home/Kitchen,color|scent
Garden/Lawn,color
Tools/Hardware,color
Sports/Outdoors,color|size
Automotive/Powersports,color
Electronics/Technology,color
Arts/Crafts,color
Office/School,color
Toys/Games,color
Firearms/Hunting,color
Jewelry/Watches,color
Books/Media/Entertainment,
"""

_DEFAULTS["spelling"] = """
# from,to
grey,gray
colour,color
colours,colors
flavour,flavor
multicolour,multicolor
"""

TABLES = tuple(_DEFAULTS)
_TRUE = {"1", "y", "yes", "true"}


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def _rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            rows.append([cell.strip() for cell in line.split(",")])
    return rows


def _basic_norm(text: Any) -> str:
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(text or "")).lower().replace("&", " and ")   # glued words: "KnobBrown"
    s = re.sub(r"[^a-z0-9/]+", " ", s)
    return re.sub(r"\s*/\s*", "/", s).strip()


def _apply_spelling(s: str, spelling: dict[str, str]) -> str:
    parts = re.split(r"([ /])", s)
    return re.sub(r"\s+", " ", "".join(spelling.get(p, p) for p in parts)).strip()


@dataclass(frozen=True)
class Lexicon:
    colors: dict[str, tuple[str, str, bool]]      # word -> (canonical, family, readable in titles)
    flavors: frozenset[str]
    scents: frozenset[str]
    sizes: dict[str, str]
    generic: frozenset[str]
    spelling: dict[str, str]
    category_boxes: dict[str, frozenset[str]]       # category (lowercase) -> boxes that can vary
    tones: frozenset[str]
    gender: dict[str, tuple[str, bool]]             # word -> (value, readable in titles)
    line_filler: frozenset[str]
    finishes: dict[str, str]                        # phrase -> canonical finish
    types: dict[str, str]                           # phrase -> canonical type
    variant_words: frozenset[str]                   # size / unit / quantity words (never a product name)
    version: str
    sources: dict[str, str]                        # table -> "file" | "built-in"


def _text_for(name: str) -> tuple[str, str]:
    path = LEXICON_DIR / f"{name}.csv"
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8-sig"), "file"
    except OSError:
        pass
    return _DEFAULTS[name], "built-in"


@lru_cache(maxsize=1)
def get_lexicon() -> Lexicon:
    texts: dict[str, str] = {}
    sources: dict[str, str] = {}
    for name in TABLES:
        texts[name], sources[name] = _text_for(name)
    spelling = {r[0].lower(): r[1].lower() for r in _rows(texts["spelling"]) if len(r) >= 2 and r[0] and r[1]}

    def n(value: str) -> str:
        return _apply_spelling(_basic_norm(value), spelling)

    colors: dict[str, tuple[str, str, bool]] = {}
    for r in _rows(texts["colors"]):
        word = n(r[0])
        if not word:
            continue
        canonical = n(r[1]) if len(r) > 1 and r[1] else word
        family = r[2].strip().lower() if len(r) > 2 else ""
        in_titles = (r[3].strip().lower() in _TRUE) if len(r) > 3 else True
        colors[word] = (canonical, family, in_titles)
    flavors = frozenset(n(r[0]) for r in _rows(texts["flavors"]) if r and n(r[0]))
    scents = frozenset(n(r[0]) for r in _rows(texts["scents"]) if r and n(r[0]))
    sizes = {r[0].lower(): r[1] for r in _rows(texts["sizes"]) if len(r) >= 2 and r[0] and r[1]}
    generic = frozenset({""} | {n(r[0]) for r in _rows(texts["generic_values"]) if r})

    category_boxes: dict[str, frozenset[str]] = {}
    for r in _rows(texts["category_boxes"]):
        if r and r[0]:
            category_boxes[r[0].lower()] = frozenset(
                b.strip().lower() for b in (r[1] if len(r) > 1 else "").split("|") if b.strip())

    tones = frozenset(n(r[0]) for r in _rows(texts["tones"]) if r and n(r[0]))
    gender: dict[str, tuple[str, bool]] = {}
    for r in _rows(texts["gender"]):
        if len(r) >= 2 and r[0] and r[1]:
            gender[n(r[0])] = (r[1].strip().lower(), (r[2].strip().lower() in _TRUE) if len(r) > 2 else True)

    line_filler = frozenset(n(r[0]) for r in _rows(texts["line_filler"]) if r and n(r[0]))

    def phrases(name: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for r in _rows(texts[name]):
            phrase = n(r[0])
            if phrase:
                out[phrase] = n(r[1]) if len(r) > 1 and r[1] else phrase
        return out

    finishes, types = phrases("finishes"), phrases("types")
    variant_words = frozenset(r[0].strip().lower() for r in _rows(texts["variant_words"]) if r and r[0].strip())

    digest = hashlib.sha1()
    for name in TABLES:
        digest.update(name.encode())
        digest.update(repr(sorted(tuple(r) for r in _rows(texts[name]))).encode())
    return Lexicon(colors, flavors, scents, sizes, generic, spelling, category_boxes, tones, gender, line_filler,
                   finishes, types, variant_words, digest.hexdigest()[:10], sources)


def reload_lexicon() -> Lexicon:
    """Forget the cached lists and read them again (tests, or after editing the CSV files)."""
    get_lexicon.cache_clear()
    return get_lexicon()


# --------------------------------------------------------------------------- #
# What the rest of the code asks the library
# --------------------------------------------------------------------------- #

def norm(text: Any) -> str:
    """lowercase, punctuation to spaces (keeps '/'), one spelling for grey/gray etc."""
    return _apply_spelling(_basic_norm(text), get_lexicon().spelling)


def title_color_words() -> frozenset[str]:
    """Color words that may be read from free text (a title)."""
    return frozenset(w for w, (_, fam, in_titles) in get_lexicon().colors.items() if in_titles and fam != "~")


def color_families(value: Any) -> frozenset[str] | None:
    """The base colors a color value stands for, e.g. 'dark gray' -> {gray}, 'black/gray' -> {black, gray}.
    None when the value contains a word the library does not know or has no family for
    ('poseidon', 'teal', '3 pairs orange') -> the caller cannot decide and should ask for review."""
    lex = get_lexicon()
    families: set[str] = set()
    for word in norm(value).replace("/", " ").split():
        info = lex.colors.get(word)
        if info is None:
            return None
        family = info[1]
        if family in ("~", "clear"):          # qualifiers and transparency never decide a comparison
            continue
        if not family:
            return None
        families.add(family)
    return frozenset(families) if families else None


def colors_relation(a: Any, b: Any) -> str:
    """'same' (one shared base color), 'different' (both known, nothing shared) or 'unknown'."""
    fa, fb = color_families(a), color_families(b)
    if fa is None or fb is None:
        return "unknown"
    return "same" if fa & fb else "different"


def size_code(token: str) -> str | None:
    return get_lexicon().sizes.get(token.lower())


@lru_cache(maxsize=4)
def _gender_for(version: str, in_field: bool) -> dict[str, str]:
    return {w: v for w, (v, in_titles) in get_lexicon().gender.items() if in_field or in_titles}


def gender_tokens(in_field: bool = False) -> dict[str, str]:
    """word -> value (men / women / boys / girls / kids / unisex).  Title words only, or with
    in_field=True also the words that are trusted only inside a gender field ('male', 'female')."""
    return _gender_for(get_lexicon().version, in_field)


def read_phrases(text: Any, table: str) -> list[str]:
    """Canonical values of every phrase of the 'finishes' or 'types' table found in the text, in the order they
    appear.  Whole phrases, the longest one wins: 'satin nickel' is one finish, not 'satin' + 'nickel'."""
    phrases = getattr(get_lexicon(), table)
    tokens = norm(text).replace("/", " ").split()
    used = [False] * len(tokens)
    found: list[tuple[int, str]] = []
    for phrase in sorted(phrases, key=lambda p: -len(p.split())):
        words = phrase.split()
        size = len(words)
        for i in range(len(tokens) - size + 1):
            if tokens[i:i + size] == words and not any(used[i:i + size]):
                for j in range(i, i + size):
                    used[j] = True
                found.append((i, phrases[phrase]))
    out: list[str] = []
    for _, value in sorted(found):
        if value not in out:
            out.append(value)
    return out


def variant_boxes_for(category: Any) -> frozenset[str] | None:
    """The attributes that can be a real variant for a product category, or None (= all of them)
    when the category is unknown or not listed."""
    if not category:
        return None
    return get_lexicon().category_boxes.get(str(category).strip().lower())


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def write_seed(force: bool = False) -> list[str]:
    LEXICON_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for name in TABLES:
        path = LEXICON_DIR / f"{name}.csv"
        if path.exists() and not force:
            continue
        path.write_text(_DEFAULTS[name].lstrip("\n"), encoding="utf-8")
        written.append(path.name)
    reload_lexicon()
    return written


def check() -> list[str]:
    lex = reload_lexicon()
    problems: list[str] = []
    print(f"Lexicon version {lex.version}   folder: {LEXICON_DIR}")
    for name in TABLES:
        text, source = _text_for(name)
        rows = _rows(text)
        print(f"  {name:15s} {len(rows):4d} rows   from {source}")
        seen: dict[str, int] = {}
        for i, r in enumerate(rows, 1):
            key = r[0].lower()
            seen[key] = seen.get(key, 0) + 1
            need = {"colors": 3, "sizes": 2, "spelling": 2}.get(name, 1)
            if len(r) < need or not r[0]:
                problems.append(f"{name}.csv row {i}: needs at least {need} column(s): {','.join(r)}")
        problems += [f"{name}.csv: '{k}' appears {c} times" for k, c in seen.items() if c > 1]
    try:
        from services.analytics.product_categorizer import ALL_CATEGORIES
        known_categories = {c.name.lower() for c in ALL_CATEGORIES}
    except Exception:  # noqa: BLE001
        known_categories = set()
    for cat, boxes in lex.category_boxes.items():
        if known_categories and cat not in known_categories:
            problems.append(f"category_boxes.csv: '{cat}' is not a category name in product_categorizer.py")
        for b in boxes - {"color", "size", "flavor", "scent", "shade"}:
            problems.append(f"category_boxes.csv: '{cat}' lists an unknown box '{b}' (use color | size | flavor | scent | shade)")
    families = sorted({fam for _, fam, _ in lex.colors.values() if fam and fam not in ("~",)})
    print(f"  color families: {', '.join(families)}")
    blank = sorted(w for w, (_, fam, _) in lex.colors.items() if not fam)
    if blank:
        print(f"  colors with NO family (a difference involving them goes to review): {', '.join(blank)}")
    both = sorted((title_color_words() & (lex.flavors | lex.scents)))
    if both:
        print(f"  words that are a color AND a flavor/scent (the color wins in a title): {', '.join(both)}")
    for bad in sorted(w for w, (_, _, t) in lex.colors.items() if t and w in {"steel", "tan", "sand", "ash", "wine", "forest", "army"}):
        problems.append(f"colors.csv: '{bad}' is readable from titles but is also an ordinary word (set in_titles to 0)")
    print("  problems: " + ("none" if not problems else ""))
    for p in problems:
        print("   - " + p)
    return problems


def old_candidates() -> None:
    """Words in the OLD lists (patterns.py and the old matcher) that the library does not have yet.
    Written to a file the app never loads, for a developer to read and move over what is wanted."""
    lex = reload_lexicon()
    found: list[tuple[str, str, str]] = []

    def grab(module_name: str, attr: str, table: str, known: Any) -> None:
        try:
            module = __import__(module_name, fromlist=[attr])
            words = getattr(module, attr)
        except Exception:  # noqa: BLE001 - an old module may not import here; skip it
            print(f"  (could not read {module_name}.{attr})")
            return
        values = words.keys() if isinstance(words, dict) else words
        for w in sorted(str(x).lower() for x in values):
            if norm(w) and norm(w) not in known:
                found.append((table, w, f"{module_name.rsplit('.', 1)[-1]}.{attr}"))

    colors_known = set(lex.colors)
    grab("services.analytics.matching_core.matching.patterns", "COLOR_WORDS", "colors", colors_known)
    grab("services.analytics.matcher", "_COLOR_WORDS", "colors", colors_known)
    grab("services.analytics.matcher", "_TONE_WORDS", "colors", colors_known)
    grab("services.analytics.matcher", "_SCENT_WORDS", "scents", lex.scents | lex.flavors)
    grab("services.analytics.matcher", "_MALE_TERMS", "gender_men", set())
    grab("services.analytics.matcher", "_FEMALE_TERMS", "gender_women", set())
    LEXICON_DIR.mkdir(parents=True, exist_ok=True)
    path = LEXICON_DIR / "_candidates_from_old_lists.csv"
    path.write_text("# table,word,found in      (review: move the lines you want into colors.csv / scents.csv)\n"
                    + "".join(f"{t},{w},{s}\n" for t, w, s in found), encoding="utf-8")
    print(f"  {len(found)} word(s) in the old lists are not in the library yet -> {path}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Word library tools")
    ap.add_argument("--write-seed", action="store_true", help="create the CSV files from the built-in lists")
    ap.add_argument("--force", action="store_true", help="with --write-seed: overwrite existing files")
    ap.add_argument("--check", action="store_true", help="validate the lists and print a summary")
    ap.add_argument("--old-candidates", action="store_true", help="list words of the old lists not in the library")
    a = ap.parse_args(argv)
    if not (a.write_seed or a.check or a.old_candidates):
        ap.print_help()
        return 0
    if a.write_seed:
        written = write_seed(force=a.force)
        print(f"wrote {len(written)} file(s) to {LEXICON_DIR}: {', '.join(written) or 'nothing (files already exist; use --force)'}")
    if a.old_candidates:
        old_candidates()
    if a.check:
        return 1 if check() else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())


FILE_VERSION = "2026-10-21 v8"