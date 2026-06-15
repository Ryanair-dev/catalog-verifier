"""
Keyword-based product categorizer.

Assigns products to broad categories using keyword/phrase matching on titles
and Amazon sales_rank_category.  No API calls — pure dictionary lookup.

Categories are ordered on a numeric scale so that related categories have
close rank values.  This lets callers compute a simple distance:

    distance = abs(cat_a.rank - cat_b.rank)

A small distance (e.g. Medical=1 vs Laboratory=3) means the products are in
related domains; a large distance (e.g. Medical=1 vs Entertainment=21) means
they are almost certainly unrelated.
"""
import math
import re
from dataclasses import dataclass

# ─────────────────────────────────────────────────────────────────────
# Category definitions — 2D coordinate system
#
# Each category has (x, y) coordinates.  Distance is Euclidean so
# categories that are conceptually related cluster together regardless
# of where they sit in a list.
#
#   X axis: body/health ←→ mechanical/technical
#   Y axis: specialized/professional ←→ general/consumer
#
#             Y (specialized)
#             9 │  Lab
#             8 │  Medical ·····  Industrial
#             7 │  Dental          Tools
#             6 │          Janitor  Automotive  Firearms
#             5 │  Baby     ·       Electronics
#             4 │  PersCarePet Home  Garden
#             3 │  CPG  Food  Office  Sports
#             2 │           Arts
#             1 │  Clothing  Jewelry  Toys
#             0 │         Entertainment
#               └──────────────────────────── X (mechanical)
#               0  1  2  3  4  5  6  7
# ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Category:
    x: float
    y: float
    name: str

#                                     x    y
MEDICAL           = Category(0,   8,  "Medical/Healthcare")
DENTAL            = Category(0,   7,  "Dental/Oral")
LABORATORY        = Category(1,   9,  "Laboratory/Scientific")
PERSONAL_CARE     = Category(1,   4,  "Personal Care/Beauty")
BABY              = Category(1,   5,  "Baby/Infant")
CPG               = Category(2,   3,  "CPG/Household")
JANITORIAL        = Category(3,   6,  "Janitorial/Cleaning")
INDUSTRIAL        = Category(4,   8,  "Industrial/Safety")
OFFICE            = Category(4,   3,  "Office/School")
FOOD              = Category(2,   2,  "Food/Beverage")
PET               = Category(2,   4,  "Pet Supplies")
HOME              = Category(3,   3,  "Home/Kitchen")
GARDEN            = Category(4,   4,  "Garden/Lawn")
TOOLS             = Category(5,   7,  "Tools/Hardware")
SPORTS            = Category(5,   3,  "Sports/Outdoors")
CLOTHING          = Category(3,   1,  "Clothing/Apparel/Footwear")
AUTOMOTIVE        = Category(5,   6,  "Automotive/Powersports")
ELECTRONICS       = Category(6,   5,  "Electronics/Technology")
ARTS              = Category(4,   2,  "Arts/Crafts")
TOYS              = Category(4,   1,  "Toys/Games")
ENTERTAINMENT     = Category(5,   0,  "Books/Media/Entertainment")
FIREARMS          = Category(6,   6,  "Firearms/Hunting")
JEWELRY           = Category(3,   0,  "Jewelry/Watches")
UNKNOWN           = Category(99, 99,  "Unknown")

ALL_CATEGORIES = [
    MEDICAL, DENTAL, LABORATORY, PERSONAL_CARE, BABY, CPG, JANITORIAL,
    INDUSTRIAL, OFFICE, FOOD, PET, HOME, GARDEN, TOOLS, SPORTS, CLOTHING,
    AUTOMOTIVE, ELECTRONICS, ARTS, TOYS, ENTERTAINMENT, FIREARMS, JEWELRY,
]


def category_distance(a: Category, b: Category) -> float:
    """Euclidean distance between two categories. Lower = more related.

    Key distances:
      Medical ↔ Industrial  = 4.0   (related — PPE, safety)
      Medical ↔ Dental      = 1.0   (very related)
      Automotive ↔ Industrial = 2.2  (related — parts, tools)
      Automotive ↔ Tools    = 1.0   (very related)
      Medical ↔ Entertainment = 9.4  (unrelated)
      Medical ↔ Automotive  = 5.4   (moderately far)
      Home ↔ Garden         = 1.4   (related)
    """
    if a == UNKNOWN or b == UNKNOWN:
        return 99.0
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2)


# ─────────────────────────────────────────────────────────────────────
# Keyword → Category maps
#
# Two tiers:
#   _PHRASE_MAP  — multi-word phrases checked first (higher precision)
#   _WORD_MAP    — single-word fallback (broader recall)
#
# All keys are lowercase.  Matching is case-insensitive.
# ─────────────────────────────────────────────────────────────────────

# Phrases are checked first — order matters only for readability
_PHRASE_MAP: list[tuple[str, Category]] = [
    # ── MEDICAL ──────────────────────────────────────────────────────
    ("adhesive bandage",      MEDICAL),
    ("wound dressing",        MEDICAL),
    ("wound care",            MEDICAL),
    ("first aid",             MEDICAL),
    ("gauze sponge",          MEDICAL),
    ("gauze bandage",         MEDICAL),
    ("gauze pad",             MEDICAL),
    ("gauze roll",            MEDICAL),
    ("medical gauze",         MEDICAL),
    ("medical tape",          MEDICAL),
    ("surgical tape",         MEDICAL),
    ("surgical glove",        MEDICAL),
    ("surgical mask",         MEDICAL),
    ("surgical gown",         MEDICAL),
    ("surgical drape",        MEDICAL),
    ("surgical instrument",   MEDICAL),
    ("surgical suture",       MEDICAL),
    ("surgical blade",        MEDICAL),
    ("surgical dressing",     MEDICAL),
    ("exam glove",            MEDICAL),
    ("exam table",            MEDICAL),
    ("exam gown",             MEDICAL),
    ("exam shorts",           MEDICAL),
    ("exam paper",            MEDICAL),
    ("medical exam",          MEDICAL),
    ("blood pressure",        MEDICAL),
    ("pulse oximeter",        MEDICAL),
    ("heart monitor",         MEDICAL),
    ("patient monitor",       MEDICAL),
    ("oxygen mask",           MEDICAL),
    ("oxygen tubing",         MEDICAL),
    ("oxygen cannula",        MEDICAL),
    ("nasal cannula",         MEDICAL),
    ("nebulizer mask",        MEDICAL),
    ("cpap mask",             MEDICAL),
    ("cpap machine",          MEDICAL),
    ("cpap hose",             MEDICAL),
    ("cpap filter",           MEDICAL),
    ("cpap headgear",         MEDICAL),
    ("cpap accessori",        MEDICAL),
    ("tracheostomy tube",     MEDICAL),
    ("endotracheal tube",     MEDICAL),
    ("foley catheter",        MEDICAL),
    ("urinary catheter",      MEDICAL),
    ("catheter bag",          MEDICAL),
    ("catheter tray",         MEDICAL),
    ("feeding tube",          MEDICAL),
    ("nasogastric tube",      MEDICAL),
    ("suction catheter",      MEDICAL),
    ("iv catheter",           MEDICAL),
    ("iv tubing",             MEDICAL),
    ("iv set",                MEDICAL),
    ("iv pole",               MEDICAL),
    ("iv start kit",          MEDICAL),
    ("infusion set",          MEDICAL),
    ("syringe needle",        MEDICAL),
    ("hypodermic needle",     MEDICAL),
    ("insulin syringe",       MEDICAL),
    ("safety needle",         MEDICAL),
    ("sharps container",      MEDICAL),
    ("needle destroy",        MEDICAL),
    ("blood collection",      MEDICAL),
    ("specimen container",    MEDICAL),
    ("specimen cup",          MEDICAL),
    ("urine cup",             MEDICAL),
    ("drug test",             MEDICAL),
    ("alcohol prep",          MEDICAL),
    ("alcohol swab",          MEDICAL),
    ("povidone iodine",       MEDICAL),
    ("betadine",              MEDICAL),
    ("antiseptic wipe",       MEDICAL),
    ("sterile pad",           MEDICAL),
    ("sterile dressing",      MEDICAL),
    ("sterile wrap",          MEDICAL),
    ("sterile glove",         MEDICAL),
    ("latex glove",           MEDICAL),
    ("nitrile glove",         MEDICAL),
    ("vinyl glove",           MEDICAL),
    ("compression sock",      MEDICAL),
    ("compression stocking",  MEDICAL),
    ("compression sleeve",    MEDICAL),
    ("compression tight",     MEDICAL),
    ("elastic bandage",       MEDICAL),
    ("cohesive bandage",      MEDICAL),
    ("tubular bandage",       MEDICAL),
    ("cast padding",          MEDICAL),
    ("casting tape",          MEDICAL),
    ("splint roll",           MEDICAL),
    ("finger splint",         MEDICAL),
    ("wrist brace",           MEDICAL),
    ("ankle brace",           MEDICAL),
    ("knee brace",            MEDICAL),
    ("elbow brace",           MEDICAL),
    ("arm sling",             MEDICAL),
    ("cervical collar",       MEDICAL),
    ("neck brace",            MEDICAL),
    ("back brace",            MEDICAL),
    ("abdominal binder",      MEDICAL),
    ("heel cup",              MEDICAL),
    ("heel cushion",          MEDICAL),
    ("orthotic insert",       MEDICAL),
    ("shoe insole",           MEDICAL),
    ("walker wheel",          MEDICAL),
    ("rolling walker",        MEDICAL),
    ("wheelchair cushion",    MEDICAL),
    ("wheelchair",            MEDICAL),
    ("hospital bed",          MEDICAL),
    ("bed rail",              MEDICAL),
    ("bed pan",               MEDICAL),
    ("bedpan",                MEDICAL),
    ("urinal bottle",         MEDICAL),
    ("emesis basin",          MEDICAL),
    ("face mask medical",     MEDICAL),
    ("procedure mask",        MEDICAL),
    ("face shield medical",   MEDICAL),
    ("isolation gown",        MEDICAL),
    ("disposable gown",       MEDICAL),
    ("patient gown",          MEDICAL),
    ("underpads",             MEDICAL),
    ("chux pad",              MEDICAL),
    ("incontinence pad",      MEDICAL),
    ("incontinence brief",    MEDICAL),
    ("adult diaper",          MEDICAL),
    ("bladder control",       MEDICAL),
    ("ostomy pouch",          MEDICAL),
    ("ostomy bag",            MEDICAL),
    ("ostomy barrier",        MEDICAL),
    ("ostomy ring",           MEDICAL),
    ("ostomy wafer",          MEDICAL),
    ("colostomy",             MEDICAL),
    ("ileostomy",             MEDICAL),
    ("urostomy",              MEDICAL),
    ("skin barrier",          MEDICAL),
    ("stoma powder",          MEDICAL),
    ("saline solution",       MEDICAL),
    ("saline flush",          MEDICAL),
    ("normal saline",         MEDICAL),
    ("irrigation solution",   MEDICAL),
    ("hydrogen peroxide",     MEDICAL),
    ("isopropyl alcohol",     MEDICAL),
    ("rubbing alcohol",       MEDICAL),
    ("hand sanitizer",        MEDICAL),
    ("cold pack",             MEDICAL),
    ("hot pack",              MEDICAL),
    ("ice pack medical",      MEDICAL),
    ("instant cold",          MEDICAL),
    ("kinesiology tape",      MEDICAL),
    ("athletic tape",         MEDICAL),
    ("medical cotton",        MEDICAL),
    ("cotton tipped applicat",MEDICAL),
    ("tongue depressor",      MEDICAL),
    ("cotton ball",           MEDICAL),
    ("cotton swab medical",   MEDICAL),
    ("pill count",            MEDICAL),
    ("pill counting",         MEDICAL),
    ("pill tray",             MEDICAL),
    ("counting tray",         MEDICAL),
    ("counting dish",         MEDICAL),
    ("pill crusher",          MEDICAL),
    ("pill cutter",           MEDICAL),
    ("medicine cup",          MEDICAL),
    ("medicine dispenser",    MEDICAL),
    ("pill organizer",        MEDICAL),
    ("diabetes test",         MEDICAL),
    ("blood glucose",         MEDICAL),
    ("lancet device",         MEDICAL),
    ("test strip",            MEDICAL),
    ("peak flow meter",       MEDICAL),
    ("spirometer",            MEDICAL),
    ("thermometer",           MEDICAL),
    ("stethoscope",           MEDICAL),
    ("otoscope",              MEDICAL),
    ("ophthalmoscope",        MEDICAL),
    ("penlight medical",      MEDICAL),
    ("vaginal specul",        MEDICAL),
    ("disposable specul",     MEDICAL),
    ("skin stapler",          MEDICAL),
    ("wound closure",         MEDICAL),
    ("steri strip",           MEDICAL),
    ("butterfly closure",     MEDICAL),
    ("transparent dressing",  MEDICAL),
    ("tegaderm",              MEDICAL),
    ("hydrocolloid",          MEDICAL),
    ("hydrogel dressing",     MEDICAL),
    ("foam dressing",         MEDICAL),
    ("alginate dressing",     MEDICAL),
    ("collagen dressing",     MEDICAL),
    ("silicone dressing",     MEDICAL),
    ("negative pressure",     MEDICAL),
    ("wound vac",             MEDICAL),
    ("abdominal pad",         MEDICAL),
    ("eye pad",               MEDICAL),
    ("eye patch medical",     MEDICAL),
    ("eye wash",              MEDICAL),
    ("eye shield",            MEDICAL),
    ("protective eyewear",    MEDICAL),
    ("face mask with ear",    MEDICAL),
    ("earloop mask",          MEDICAL),
    ("n95 respirator",        MEDICAL),
    ("face respirator",       MEDICAL),
    ("protective mask",       MEDICAL),
    ("disposable mask",       MEDICAL),
    ("bouffant cap",          MEDICAL),
    ("surgical cap",          MEDICAL),
    ("shoe cover disposabl",  MEDICAL),
    ("boot cover",            MEDICAL),
    ("fracture boot",         MEDICAL),
    ("walking boot",          MEDICAL),
    ("post op shoe",          MEDICAL),
    ("cast boot",             MEDICAL),
    ("maternity pad",         MEDICAL),
    ("perineal pad",          MEDICAL),

    # ── DENTAL ───────────────────────────────────────────────────────
    ("dental cement",         DENTAL),
    ("dental adhesive",       DENTAL),
    ("dental impression",     DENTAL),
    ("dental syringe",        DENTAL),
    ("dental bur",            DENTAL),
    ("dental mirror",         DENTAL),
    ("dental floss",          DENTAL),
    ("dental pick",           DENTAL),
    ("denture adhesive",      DENTAL),
    ("denture cleaner",       DENTAL),
    ("dental prophy",         DENTAL),
    ("orthodontic",           DENTAL),
    ("dental composite",      DENTAL),
    ("dental curing",         DENTAL),

    # ── LABORATORY ───────────────────────────────────────────────────
    ("test tube",             LABORATORY),
    ("petri dish",            LABORATORY),
    ("lab pipette",           LABORATORY),
    ("pipette tip",           LABORATORY),
    ("microscope slide",      LABORATORY),
    ("centrifuge tube",       LABORATORY),
    ("lab beaker",            LABORATORY),
    ("erlenmeyer flask",      LABORATORY),
    ("lab flask",             LABORATORY),
    ("lab reagent",           LABORATORY),
    ("chromatography",        LABORATORY),
    ("sample vial",           LABORATORY),
    ("lab filter paper",      LABORATORY),
    ("culture media",         LABORATORY),
    ("agar plate",            LABORATORY),
    ("biological indicator",  LABORATORY),
    ("sterilization pouch",   LABORATORY),
    ("autoclave bag",         LABORATORY),
    ("blood collection tube", LABORATORY),
    ("vacutainer",            LABORATORY),
    ("hematocrit tube",       LABORATORY),
    ("cuvette",               LABORATORY),
    ("ph test",               LABORATORY),
    ("lab glassware",         LABORATORY),

    # ── PERSONAL CARE / BEAUTY ───────────────────────────────────────
    ("hair shampoo",          PERSONAL_CARE),
    ("hair conditioner",      PERSONAL_CARE),
    ("hair color",            PERSONAL_CARE),
    ("hair dye",              PERSONAL_CARE),
    ("hair spray",            PERSONAL_CARE),
    ("hair gel",              PERSONAL_CARE),
    ("hair brush",            PERSONAL_CARE),
    ("hair dryer",            PERSONAL_CARE),
    ("flat iron",             PERSONAL_CARE),
    ("curling iron",          PERSONAL_CARE),
    ("hair clip",             PERSONAL_CARE),
    ("hair extension",        PERSONAL_CARE),
    ("hair removal",          PERSONAL_CARE),
    ("hair mask",             PERSONAL_CARE),
    ("body lotion",           PERSONAL_CARE),
    ("body wash",             PERSONAL_CARE),
    ("body cream",            PERSONAL_CARE),
    ("body oil",              PERSONAL_CARE),
    ("face moisturizer",      PERSONAL_CARE),
    ("facial serum",          PERSONAL_CARE),
    ("facial cleanser",       PERSONAL_CARE),
    ("facial mask",           PERSONAL_CARE),
    ("face mask beauty",      PERSONAL_CARE),
    ("facial toner",          PERSONAL_CARE),
    ("eye cream",             PERSONAL_CARE),
    ("lip balm",              PERSONAL_CARE),
    ("lip gloss",             PERSONAL_CARE),
    ("lipstick",              PERSONAL_CARE),
    ("mascara",               PERSONAL_CARE),
    ("eyeliner",              PERSONAL_CARE),
    ("eyeshadow",             PERSONAL_CARE),
    ("eye shadow",            PERSONAL_CARE),
    ("foundation makeup",     PERSONAL_CARE),
    ("concealer makeup",      PERSONAL_CARE),
    ("blush makeup",          PERSONAL_CARE),
    ("nail polish",           PERSONAL_CARE),
    ("nail file",             PERSONAL_CARE),
    ("nail clipper",          PERSONAL_CARE),
    ("manicure set",          PERSONAL_CARE),
    ("pedicure",              PERSONAL_CARE),
    ("perfume",               PERSONAL_CARE),
    ("eau de parfum",         PERSONAL_CARE),
    ("eau de toilette",       PERSONAL_CARE),
    ("cologne",               PERSONAL_CARE),
    ("deodorant",             PERSONAL_CARE),
    ("antiperspirant",        PERSONAL_CARE),
    ("shaving cream",         PERSONAL_CARE),
    ("shaving gel",           PERSONAL_CARE),
    ("razor blade",           PERSONAL_CARE),
    ("electric shaver",       PERSONAL_CARE),
    ("aftershave",            PERSONAL_CARE),
    ("sunscreen",             PERSONAL_CARE),
    ("self tanner",           PERSONAL_CARE),
    ("skin care",             PERSONAL_CARE),
    ("acne treatment",        PERSONAL_CARE),
    ("anti aging",            PERSONAL_CARE),
    ("wrinkle cream",         PERSONAL_CARE),
    ("tooth whitening",       PERSONAL_CARE),
    ("teeth whitening",       PERSONAL_CARE),
    ("toothpaste",            PERSONAL_CARE),
    ("toothbrush",            PERSONAL_CARE),
    ("mouthwash",             PERSONAL_CARE),
    ("foot cream",            PERSONAL_CARE),
    ("foot file",             PERSONAL_CARE),
    ("bath bomb",             PERSONAL_CARE),
    ("bath salt",             PERSONAL_CARE),
    ("bath soap",             PERSONAL_CARE),
    ("bar soap",              PERSONAL_CARE),
    ("hand soap",             PERSONAL_CARE),
    ("hand cream",            PERSONAL_CARE),
    ("hand lotion",           PERSONAL_CARE),
    ("makeup brush",          PERSONAL_CARE),
    ("makeup remover",        PERSONAL_CARE),
    ("cosmetic bag",          PERSONAL_CARE),
    ("beauty blender",        PERSONAL_CARE),
    ("cotton pad facial",     PERSONAL_CARE),
    ("cleansing wipe",        PERSONAL_CARE),
    ("feminine wash",         PERSONAL_CARE),
    ("feminine hygiene",      PERSONAL_CARE),
    ("sanitary napkin",       PERSONAL_CARE),
    ("tampon",                PERSONAL_CARE),
    ("menstrual cup",         PERSONAL_CARE),

    # ── BABY ─────────────────────────────────────────────────────────
    ("baby bottle",           BABY),
    ("baby formula",          BABY),
    ("baby food",             BABY),
    ("baby diaper",           BABY),
    ("baby wipe",             BABY),
    ("baby lotion",           BABY),
    ("baby shampoo",          BABY),
    ("baby powder",           BABY),
    ("baby monitor",          BABY),
    ("baby gate",             BABY),
    ("baby carrier",          BABY),
    ("stroller",              BABY),
    ("car seat infant",       BABY),
    ("pacifier",              BABY),
    ("teething ring",         BABY),
    ("sippy cup",             BABY),
    ("baby swing",            BABY),
    ("baby bath",             BABY),
    ("nursing pad",           BABY),
    ("breast pump",           BABY),
    ("bottle brush baby",     BABY),
    ("pediatric",             BABY),

    # ── CPG / HOUSEHOLD ──────────────────────────────────────────────
    ("paper towel",           CPG),
    ("toilet paper",          CPG),
    ("tissue box",            CPG),
    ("facial tissue",         CPG),
    ("trash bag",             CPG),
    ("garbage bag",           CPG),
    ("plastic bag",           CPG),
    ("zip lock bag",          CPG),
    ("storage bag",           CPG),
    ("aluminum foil",         CPG),
    ("plastic wrap",          CPG),
    ("laundry detergent",     CPG),
    ("fabric softener",       CPG),
    ("dryer sheet",           CPG),
    ("dish soap",             CPG),
    ("dishwasher detergent",  CPG),
    ("air freshener",         CPG),
    ("candle scented",        CPG),
    ("light bulb",            CPG),
    ("led bulb",              CPG),
    ("battery aa",            CPG),
    ("battery aaa",           CPG),
    ("alkaline battery",      CPG),
    ("disposable cup",        CPG),
    ("paper plate",           CPG),
    ("plastic utensil",       CPG),
    ("disposable fork",       CPG),
    ("disposable spoon",      CPG),
    ("napkin",                CPG),
    ("paper napkin",          CPG),

    # ── JANITORIAL / CLEANING ────────────────────────────────────────
    ("floor cleaner",         JANITORIAL),
    ("glass cleaner",         JANITORIAL),
    ("surface cleaner",       JANITORIAL),
    ("disinfectant spray",    JANITORIAL),
    ("disinfectant wipe",     JANITORIAL),
    ("bleach cleaner",        JANITORIAL),
    ("mop head",              JANITORIAL),
    ("mop pad",               JANITORIAL),
    ("broom",                 JANITORIAL),
    ("dust pan",              JANITORIAL),
    ("scrub brush",           JANITORIAL),
    ("scouring pad",          JANITORIAL),
    ("cleaning sponge",       JANITORIAL),
    ("latex cleaning glove",  JANITORIAL),
    ("rubber cleaning glove", JANITORIAL),
    ("toilet cleaner",        JANITORIAL),
    ("drain cleaner",         JANITORIAL),
    ("pressure washer",       JANITORIAL),
    ("carpet cleaner",        JANITORIAL),

    # ── INDUSTRIAL / SAFETY ──────────────────────────────────────────
    ("safety goggle",         INDUSTRIAL),
    ("safety glass",          INDUSTRIAL),
    ("safety vest",           INDUSTRIAL),
    ("hard hat",              INDUSTRIAL),
    ("ear plug safety",       INDUSTRIAL),
    ("ear muff safety",       INDUSTRIAL),
    ("hearing protection",    INDUSTRIAL),
    ("safety harness",        INDUSTRIAL),
    ("fall protection",       INDUSTRIAL),
    ("work glove",            INDUSTRIAL),
    ("welding glove",         INDUSTRIAL),
    ("welding helmet",        INDUSTRIAL),
    ("cut resistant",         INDUSTRIAL),
    ("respirator cartridge",  INDUSTRIAL),
    ("dust mask",             INDUSTRIAL),
    ("warning sign",          INDUSTRIAL),
    ("safety sign",           INDUSTRIAL),
    ("safety tape",           INDUSTRIAL),
    ("caution tape",          INDUSTRIAL),
    ("barricade tape",        INDUSTRIAL),
    ("lockout tagout",        INDUSTRIAL),
    ("fire extinguisher",     INDUSTRIAL),
    ("first aid kit",         INDUSTRIAL),
    ("eye wash station",      INDUSTRIAL),
    ("spill kit",             INDUSTRIAL),
    ("absorbent pad",         INDUSTRIAL),
    ("o-ring",                INDUSTRIAL),
    ("ball bearing",          INDUSTRIAL),
    ("drive belt industrial", INDUSTRIAL),
    ("v-belt",                INDUSTRIAL),
    ("timing belt industrial",INDUSTRIAL),
    ("hydraulic fitting",     INDUSTRIAL),
    ("pipe fitting",          INDUSTRIAL),
    ("barbed fitting",        INDUSTRIAL),
    ("hose clamp",            INDUSTRIAL),
    ("cable tie",             INDUSTRIAL),
    ("zip tie",               INDUSTRIAL),
    ("shrink tubing",         INDUSTRIAL),
    ("electrical connector",  INDUSTRIAL),

    # ── OFFICE / SCHOOL ──────────────────────────────────────────────
    ("ballpoint pen",         OFFICE),
    ("gel ink pen",           OFFICE),
    ("felt tip pen",          OFFICE),
    ("marker pen",            OFFICE),
    ("highlighter pen",       OFFICE),
    ("pencil set",            OFFICE),
    ("mechanical pencil",     OFFICE),
    ("sticky note",           OFFICE),
    ("post-it note",          OFFICE),
    ("file folder",           OFFICE),
    ("binder clip",           OFFICE),
    ("paper clip",            OFFICE),
    ("stapler",               OFFICE),
    ("tape dispenser",        OFFICE),
    ("scotch tape",           OFFICE),
    ("packing tape",          OFFICE),
    ("shipping label",        OFFICE),
    ("printer paper",         OFFICE),
    ("copy paper",            OFFICE),
    ("notebook",              OFFICE),
    ("planner",               OFFICE),
    ("calendar desk",         OFFICE),
    ("desk organizer",        OFFICE),
    ("whiteboard",            OFFICE),
    ("dry erase",             OFFICE),
    ("laminating",            OFFICE),
    ("shredder",              OFFICE),
    ("calculator",            OFFICE),
    ("envelope",              OFFICE),
    ("greeting card",         OFFICE),

    # ── FOOD / BEVERAGE ──────────────────────────────────────────────
    ("salad dressing",        FOOD),
    ("pasta sauce",           FOOD),
    ("hot sauce",             FOOD),
    ("barbecue sauce",        FOOD),
    ("soy sauce",             FOOD),
    ("olive oil",             FOOD),
    ("cooking oil",           FOOD),
    ("coconut oil food",      FOOD),
    ("protein powder",        FOOD),
    ("protein bar",           FOOD),
    ("energy drink",          FOOD),
    ("coffee bean",           FOOD),
    ("coffee ground",         FOOD),
    ("coffee pod",            FOOD),
    ("tea bag",               FOOD),
    ("herbal tea",            FOOD),
    ("green tea",             FOOD),
    ("chocolate bar",         FOOD),
    ("candy",                 FOOD),
    ("gummy bear",            FOOD),
    ("snack bar",             FOOD),
    ("granola bar",           FOOD),
    ("cereal",                FOOD),
    ("oatmeal",               FOOD),
    ("peanut butter",         FOOD),
    ("almond butter",         FOOD),
    ("dried fruit",           FOOD),
    ("beef jerky",            FOOD),
    ("salami",                FOOD),
    ("canned food",           FOOD),
    ("spice rack",            FOOD),
    ("seasoning",             FOOD),
    ("vitamin supplement",    FOOD),
    ("dietary supplement",    FOOD),
    ("probiotic",             FOOD),
    ("fish oil supplement",   FOOD),
    ("multivitamin",          FOOD),
    ("vitamin c",             FOOD),
    ("vitamin d",             FOOD),
    ("magnesium supplement",  FOOD),
    ("calcium supplement",    FOOD),
    ("collagen supplement",   FOOD),
    ("herbal supplement",     FOOD),
    ("nutritional shake",     FOOD),
    ("meal replacement",      FOOD),

    # ── PET SUPPLIES ─────────────────────────────────────────────────
    ("dog food",              PET),
    ("dog treat",             PET),
    ("dog toy",               PET),
    ("dog collar",            PET),
    ("dog leash",             PET),
    ("dog bed",               PET),
    ("dog crate",             PET),
    ("dog harness",           PET),
    ("cat food",              PET),
    ("cat litter",            PET),
    ("cat toy",               PET),
    ("cat tree",              PET),
    ("fish tank",             PET),
    ("aquarium filter",       PET),
    ("bird cage",             PET),
    ("bird seed",             PET),
    ("pet shampoo",           PET),
    ("flea collar",           PET),
    ("flea treatment",        PET),
    ("pet carrier",           PET),

    # ── HOME / KITCHEN ───────────────────────────────────────────────
    ("cutting board",         HOME),
    ("kitchen knife",         HOME),
    ("chef knife",            HOME),
    ("paring knife",          HOME),
    ("can opener",            HOME),
    ("bottle opener",         HOME),
    ("corkscrew",             HOME),
    ("garlic press",          HOME),
    ("vegetable peeler",      HOME),
    ("grater",                HOME),
    ("spatula",               HOME),
    ("whisk",                 HOME),
    ("tongs",                 HOME),
    ("ladle",                 HOME),
    ("measuring cup",         HOME),
    ("measuring spoon",       HOME),
    ("mixing bowl",           HOME),
    ("baking sheet",          HOME),
    ("cake pan",              HOME),
    ("muffin tin",            HOME),
    ("cookie cutter",         HOME),
    ("rolling pin",           HOME),
    ("colander",              HOME),
    ("strainer",              HOME),
    ("food storage",          HOME),
    ("storage container",     HOME),
    ("glass jar",             HOME),
    ("water bottle",          HOME),
    ("insulated bottle",      HOME),
    ("insulated tumbler",     HOME),
    ("travel mug",            HOME),
    ("coffee mug",            HOME),
    ("dinner plate",          HOME),
    ("dinnerware set",        HOME),
    ("wine glass",            HOME),
    ("drinking glass",        HOME),
    ("flatware",              HOME),
    ("silverware",            HOME),
    ("dish rack",             HOME),
    ("pot holder",            HOME),
    ("oven mitt",             HOME),
    ("table cloth",           HOME),
    ("place mat",             HOME),
    ("area rug",              HOME),
    ("bath towel",            HOME),
    ("hand towel",            HOME),
    ("shower curtain",        HOME),
    ("curtain panel",         HOME),
    ("curtain rod",           HOME),
    ("throw pillow",          HOME),
    ("bed sheet",             HOME),
    ("pillow case",           HOME),
    ("comforter",             HOME),
    ("blanket",               HOME),
    ("door knob",             HOME),
    ("door lever",            HOME),
    ("door handle",           HOME),
    ("deadbolt",              HOME),
    ("door lock",             HOME),
    ("cabinet pull",          HOME),
    ("cabinet knob",          HOME),
    ("furniture pad",         HOME),
    ("picture frame",         HOME),
    ("wall art",              HOME),
    ("decorative sign",       HOME),
    ("vase",                  HOME),
    ("artificial flower",     HOME),
    ("candle holder",         HOME),

    # ── GARDEN / LAWN ────────────────────────────────────────────────
    ("lawn mower",            GARDEN),
    ("string trimmer",        GARDEN),
    ("leaf blower",           GARDEN),
    ("chainsaw",              GARDEN),
    ("hedge trimmer",         GARDEN),
    ("garden hose",           GARDEN),
    ("sprinkler",             GARDEN),
    ("garden tool",           GARDEN),
    ("pruning shear",         GARDEN),
    ("garden glove",          GARDEN),
    ("potting soil",          GARDEN),
    ("fertilizer",            GARDEN),
    ("plant food",            GARDEN),
    ("seed packet",           GARDEN),
    ("flower pot",            GARDEN),
    ("planter box",           GARDEN),
    ("raised bed garden",     GARDEN),
    ("weed killer",           GARDEN),
    ("herbicide",             GARDEN),
    ("insecticide",           GARDEN),
    ("pest control",          GARDEN),
    ("bird feeder",           GARDEN),
    ("outdoor flag",          GARDEN),
    ("garden statue",         GARDEN),
    ("solar light garden",    GARDEN),
    ("patio furniture",       GARDEN),
    ("grill cover",           GARDEN),
    ("grill grate",           GARDEN),
    ("snow blower",           GARDEN),
    ("pool filter",           GARDEN),
    ("pool chemical",         GARDEN),
    ("tree cutting",          GARDEN),
    ("willow tree cutting",   GARDEN),

    # ── TOOLS / HARDWARE ─────────────────────────────────────────────
    ("power drill",           TOOLS),
    ("drill bit",             TOOLS),
    ("screwdriver set",       TOOLS),
    ("wrench set",            TOOLS),
    ("socket set",            TOOLS),
    ("pliers",                TOOLS),
    ("hammer",                TOOLS),
    ("tape measure",          TOOLS),
    ("level tool",            TOOLS),
    ("utility knife",         TOOLS),
    ("box cutter",            TOOLS),
    ("saw blade",             TOOLS),
    ("circular saw",          TOOLS),
    ("jig saw",               TOOLS),
    ("table saw",             TOOLS),
    ("band saw",              TOOLS),
    ("router bit",            TOOLS),
    ("sander",                TOOLS),
    ("sandpaper",             TOOLS),
    ("paint brush",           TOOLS),
    ("paint roller",          TOOLS),
    ("paint sprayer",         TOOLS),
    ("caulk gun",             TOOLS),
    ("wood glue",             TOOLS),
    ("epoxy",                 TOOLS),
    ("super glue",            TOOLS),
    ("duct tape",             TOOLS),
    ("electrical tape",       TOOLS),
    ("wire stripper",         TOOLS),
    ("soldering iron",        TOOLS),
    ("multimeter",            TOOLS),
    ("stud finder",           TOOLS),
    ("nail gun",              TOOLS),
    ("screw assortment",      TOOLS),
    ("bolt assortment",       TOOLS),
    ("anchor",                TOOLS),
    ("hinge",                 TOOLS),
    ("bracket",               TOOLS),
    ("clamp",                 TOOLS),
    ("vice grip",             TOOLS),
    ("workbench",             TOOLS),
    ("tool box",              TOOLS),
    ("tool bag",              TOOLS),
    ("ceremonial shovel",     TOOLS),
    ("jack stand",            TOOLS),

    # ── SPORTS / OUTDOORS ────────────────────────────────────────────
    ("camping tent",          SPORTS),
    ("sleeping bag",          SPORTS),
    ("hiking boot",           SPORTS),
    ("hiking backpack",       SPORTS),
    ("water filter outdoor",  SPORTS),
    ("dry bag",               SPORTS),
    ("headlamp",              SPORTS),
    ("flashlight",            SPORTS),
    ("pocket knife",          SPORTS),
    ("multi tool",            SPORTS),
    ("fishing rod",           SPORTS),
    ("fishing reel",          SPORTS),
    ("fishing lure",          SPORTS),
    ("tackle box",            SPORTS),
    ("kayak",                 SPORTS),
    ("canoe paddle",          SPORTS),
    ("life jacket",           SPORTS),
    ("swim goggle",           SPORTS),
    ("yoga mat",              SPORTS),
    ("resistance band",       SPORTS),
    ("dumbbell",              SPORTS),
    ("kettlebell",            SPORTS),
    ("jump rope",             SPORTS),
    ("bicycle",               SPORTS),
    ("bike tire",             SPORTS),
    ("bike helmet",           SPORTS),
    ("bike lock",             SPORTS),
    ("bike light",            SPORTS),
    ("golf club",             SPORTS),
    ("golf ball",             SPORTS),
    ("golf cart",             SPORTS),
    ("tennis racket",         SPORTS),
    ("basketball",            SPORTS),
    ("football",              SPORTS),
    ("soccer ball",           SPORTS),
    ("baseball glove",        SPORTS),
    ("baseball bat",          SPORTS),
    ("skateboard",            SPORTS),
    ("ski goggle",            SPORTS),
    ("snowboard",             SPORTS),
    ("climbing rope",         SPORTS),
    ("carabiner",             SPORTS),

    # ── CLOTHING / APPAREL / FOOTWEAR ────────────────────────────────
    ("t-shirt",               CLOTHING),
    ("tee shirt",             CLOTHING),
    ("polo shirt",            CLOTHING),
    ("button down shirt",     CLOTHING),
    ("dress shirt",           CLOTHING),
    ("hoodie",                CLOTHING),
    ("sweatshirt",            CLOTHING),
    ("sweater",               CLOTHING),
    ("jacket",                CLOTHING),
    ("rain coat",             CLOTHING),
    ("winter coat",           CLOTHING),
    ("down jacket",           CLOTHING),
    ("jeans",                 CLOTHING),
    ("cargo pants",           CLOTHING),
    ("dress pants",           CLOTHING),
    ("yoga pants",            CLOTHING),
    ("leggings",              CLOTHING),
    ("shorts",                CLOTHING),
    ("athletic shorts",       CLOTHING),
    ("underwear",             CLOTHING),
    ("boxer brief",           CLOTHING),
    ("sports bra",            CLOTHING),
    ("everyday bra",          CLOTHING),
    ("pajama",                CLOTHING),
    ("bathrobe",              CLOTHING),
    ("swimsuit",              CLOTHING),
    ("bikini",                CLOTHING),
    ("wetsuit",               CLOTHING),
    ("crew sock",             CLOTHING),
    ("ankle sock",            CLOTHING),
    ("dress sock",            CLOTHING),
    ("hiking sock",           CLOTHING),
    ("running shoe",          CLOTHING),
    ("sneaker",               CLOTHING),
    ("loafer",                CLOTHING),
    ("oxford shoe",           CLOTHING),
    ("sandal",                CLOTHING),
    ("flip flop",             CLOTHING),
    ("boot",                  CLOTHING),
    ("slipper",               CLOTHING),
    ("clog",                  CLOTHING),
    ("mule shoe",             CLOTHING),
    ("high heel",             CLOTHING),
    ("platform shoe",         CLOTHING),
    ("shoelace",              CLOTHING),
    ("shoe charm",            CLOTHING),
    ("belt",                  CLOTHING),
    ("tie",                   CLOTHING),
    ("scarf",                 CLOTHING),
    ("gloves winter",         CLOTHING),
    ("hat beanie",            CLOTHING),
    ("baseball cap",          CLOTHING),
    ("sun hat",               CLOTHING),
    ("backpack",              CLOTHING),
    ("handbag",               CLOTHING),
    ("wallet",                CLOTHING),
    ("sunglasses",            CLOTHING),
    ("eyewear frame",         CLOTHING),
    ("eyeglass frame",        CLOTHING),
    ("contact lens",          CLOTHING),

    # ── AUTOMOTIVE / POWERSPORTS ─────────────────────────────────────
    ("headlight bulb",        AUTOMOTIVE),
    ("tail light bulb",       AUTOMOTIVE),
    ("fog light bulb",        AUTOMOTIVE),
    ("turn signal bulb",      AUTOMOTIVE),
    ("brake light bulb",      AUTOMOTIVE),
    ("headlight assembly",    AUTOMOTIVE),
    ("tail light assembly",   AUTOMOTIVE),
    ("air filter automotive", AUTOMOTIVE),
    ("oil filter",            AUTOMOTIVE),
    ("fuel filter",           AUTOMOTIVE),
    ("cabin air filter",      AUTOMOTIVE),
    ("spark plug",            AUTOMOTIVE),
    ("ignition coil",         AUTOMOTIVE),
    ("alternator",            AUTOMOTIVE),
    ("starter motor",         AUTOMOTIVE),
    ("fuel pump",             AUTOMOTIVE),
    ("water pump automotive", AUTOMOTIVE),
    ("radiator hose",         AUTOMOTIVE),
    ("brake pad",             AUTOMOTIVE),
    ("brake rotor",           AUTOMOTIVE),
    ("brake caliper",         AUTOMOTIVE),
    ("brake line",            AUTOMOTIVE),
    ("shock absorber",        AUTOMOTIVE),
    ("strut assembly",        AUTOMOTIVE),
    ("control arm",           AUTOMOTIVE),
    ("tie rod end",           AUTOMOTIVE),
    ("ball joint",            AUTOMOTIVE),
    ("wheel bearing",         AUTOMOTIVE),
    ("cv axle",               AUTOMOTIVE),
    ("drive shaft",           AUTOMOTIVE),
    ("engine mount",          AUTOMOTIVE),
    ("exhaust pipe",          AUTOMOTIVE),
    ("muffler",               AUTOMOTIVE),
    ("catalytic converter",   AUTOMOTIVE),
    ("oxygen sensor",         AUTOMOTIVE),
    ("carburetor",            AUTOMOTIVE),
    ("fuel injector",         AUTOMOTIVE),
    ("throttle body",         AUTOMOTIVE),
    ("power steering",        AUTOMOTIVE),
    ("transmission fluid",    AUTOMOTIVE),
    ("motor oil",             AUTOMOTIVE),
    ("windshield wiper",      AUTOMOTIVE),
    ("wiper blade",           AUTOMOTIVE),
    ("car battery",           AUTOMOTIVE),
    ("car cover",             AUTOMOTIVE),
    ("floor mat car",         AUTOMOTIVE),
    ("seat cover car",        AUTOMOTIVE),
    ("dash cam",              AUTOMOTIVE),
    ("car charger",           AUTOMOTIVE),
    ("phone mount car",       AUTOMOTIVE),
    ("tire",                  AUTOMOTIVE),
    ("wheel rim",             AUTOMOTIVE),
    ("hubcap",                AUTOMOTIVE),
    ("bumper sticker",        AUTOMOTIVE),
    ("car decal",             AUTOMOTIVE),
    ("touchup paint car",     AUTOMOTIVE),
    ("motorcycle helmet",     AUTOMOTIVE),

    # ── ELECTRONICS / TECHNOLOGY ─────────────────────────────────────
    ("laptop computer",       ELECTRONICS),
    ("desktop computer",      ELECTRONICS),
    ("computer monitor",      ELECTRONICS),
    ("computer keyboard",     ELECTRONICS),
    ("computer mouse",        ELECTRONICS),
    ("graphics card",         ELECTRONICS),
    ("computer memory",       ELECTRONICS),
    ("solid state drive",     ELECTRONICS),
    ("hard drive",            ELECTRONICS),
    ("power supply computer", ELECTRONICS),
    ("computer fan",          ELECTRONICS),
    ("motherboard",           ELECTRONICS),
    ("cpu cooler",            ELECTRONICS),
    ("usb cable",             ELECTRONICS),
    ("usb hub",               ELECTRONICS),
    ("usb flash drive",       ELECTRONICS),
    ("hdmi cable",            ELECTRONICS),
    ("ethernet cable",        ELECTRONICS),
    ("power strip",           ELECTRONICS),
    ("surge protector",       ELECTRONICS),
    ("phone case",            ELECTRONICS),
    ("screen protector",      ELECTRONICS),
    ("tablet case",           ELECTRONICS),
    ("laptop charger",        ELECTRONICS),
    ("laptop battery",        ELECTRONICS),
    ("laptop sleeve",         ELECTRONICS),
    ("wireless earbuds",      ELECTRONICS),
    ("bluetooth speaker",     ELECTRONICS),
    ("headphone",             ELECTRONICS),
    ("earpad replacement",    ELECTRONICS),
    ("smart watch band",      ELECTRONICS),
    ("camera battery",        ELECTRONICS),
    ("camera lens",           ELECTRONICS),
    ("sd card",               ELECTRONICS),
    ("memory card",           ELECTRONICS),
    ("printer ink",           ELECTRONICS),
    ("printer toner",         ELECTRONICS),
    ("ink cartridge",         ELECTRONICS),
    ("toner cartridge",       ELECTRONICS),
    ("remote control",        ELECTRONICS),
    ("led strip light",       ELECTRONICS),
    ("smart plug",            ELECTRONICS),
    ("smart bulb",            ELECTRONICS),
    ("security camera",       ELECTRONICS),
    ("video doorbell",        ELECTRONICS),
    ("router wireless",       ELECTRONICS),
    ("wifi extender",         ELECTRONICS),

    # ── ARTS / CRAFTS ────────────────────────────────────────────────
    ("acrylic paint",         ARTS),
    ("watercolor paint",      ARTS),
    ("oil paint art",         ARTS),
    ("paint brush art",       ARTS),
    ("canvas art",            ARTS),
    ("sketch pad",            ARTS),
    ("drawing pencil",        ARTS),
    ("colored pencil",        ARTS),
    ("crayon",                ARTS),
    ("modeling clay",         ARTS),
    ("polymer clay",          ARTS),
    ("sewing machine",        ARTS),
    ("sewing thread",         ARTS),
    ("sewing needle",         ARTS),
    ("fabric",                ARTS),
    ("yarn",                  ARTS),
    ("knitting needle",       ARTS),
    ("crochet hook",          ARTS),
    ("embroidery",            ARTS),
    ("cross stitch",          ARTS),
    ("scrapbook",             ARTS),
    ("sticker craft",         ARTS),
    ("glitter",               ARTS),
    ("bead craft",            ARTS),
    ("jewelry making",        ARTS),
    ("resin craft",           ARTS),

    # ── TOYS / GAMES ─────────────────────────────────────────────────
    ("action figure",         TOYS),
    ("collectible figure",    TOYS),
    ("building block",        TOYS),
    ("lego set",              TOYS),
    ("board game",            TOYS),
    ("card game",             TOYS),
    ("jigsaw puzzle",         TOYS),
    ("stuffed animal",        TOYS),
    ("teddy bear",            TOYS),
    ("toy car",               TOYS),
    ("remote control car",    TOYS),
    ("drone toy",             TOYS),
    ("nerf gun",              TOYS),
    ("water gun",             TOYS),
    ("play doh",              TOYS),
    ("doll house",            TOYS),
    ("barbie doll",           TOYS),
    ("video game",            TOYS),
    ("game controller",       TOYS),
    ("trading card",          TOYS),
    ("word search",           TOYS),

    # ── ENTERTAINMENT (Books/DVDs/Music) ─────────────────────────────
    ("blu-ray",               ENTERTAINMENT),
    ("blu ray",               ENTERTAINMENT),
    ("dvd movie",             ENTERTAINMENT),
    ("vhs tape",              ENTERTAINMENT),
    ("vinyl record",          ENTERTAINMENT),
    ("audio cd",              ENTERTAINMENT),
    ("music cd",              ENTERTAINMENT),
    ("sheet music",           ENTERTAINMENT),
    ("guitar string",         ENTERTAINMENT),
    ("piano keyboard",        ENTERTAINMENT),
    ("drum stick",            ENTERTAINMENT),
    ("microphone",            ENTERTAINMENT),
    ("karaoke",               ENTERTAINMENT),
    ("movie poster",          ENTERTAINMENT),
    ("paperback",             ENTERTAINMENT),
    ("hardcover",             ENTERTAINMENT),

    # ── FIREARMS / HUNTING ───────────────────────────────────────────
    ("gun sight",             FIREARMS),
    ("rifle scope",           FIREARMS),
    ("gun holster",           FIREARMS),
    ("magazine pouch",        FIREARMS),
    ("gun cleaning",          FIREARMS),
    ("bore brush",            FIREARMS),
    ("ammunition",            FIREARMS),
    ("shotgun shell",         FIREARMS),
    ("hunting knife",         FIREARMS),
    ("hunting blind",         FIREARMS),
    ("trail camera",          FIREARMS),
    ("archery bow",           FIREARMS),
    ("crossbow",              FIREARMS),
    ("arrow shaft",           FIREARMS),
    ("airsoft gun",           FIREARMS),
    ("paintball",             FIREARMS),

    # ── JEWELRY / WATCHES ────────────────────────────────────────────
    ("necklace",              JEWELRY),
    ("pendant",               JEWELRY),
    ("bracelet",              JEWELRY),
    ("earring",               JEWELRY),
    ("ring sterling",         JEWELRY),
    ("engagement ring",       JEWELRY),
    ("wedding band",          JEWELRY),
    ("wrist watch",           JEWELRY),
    ("watch band",            JEWELRY),
    ("watch repair",          JEWELRY),
    ("cufflink",              JEWELRY),
    ("brooch",                JEWELRY),
    ("charm",                 JEWELRY),
]

# ─────────────────────────────────────────────────────────────────────
# Single-word fallback (checked only if no phrase matches)
# ─────────────────────────────────────────────────────────────────────
_WORD_MAP: dict[str, Category] = {
    # Medical
    "gauze":          MEDICAL,
    "bandage":        MEDICAL,
    "dressing":       MEDICAL,
    "catheter":       MEDICAL,
    "syringe":        MEDICAL,
    "scalpel":        MEDICAL,
    "suture":         MEDICAL,
    "cannula":        MEDICAL,
    "nebulizer":      MEDICAL,
    "stethoscope":    MEDICAL,
    "otoscope":       MEDICAL,
    "sphygmomanometer": MEDICAL,
    "oximeter":       MEDICAL,
    "wheelchair":     MEDICAL,
    "walker":         MEDICAL,
    "crutch":         MEDICAL,
    "ostomy":         MEDICAL,
    "colostomy":      MEDICAL,
    "tracheostomy":   MEDICAL,
    "speculum":       MEDICAL,
    "tourniquet":     MEDICAL,
    "lancet":         MEDICAL,
    "hemostat":       MEDICAL,
    "forceps":        MEDICAL,
    "retractor":      MEDICAL,
    "curette":        MEDICAL,
    "trocar":         MEDICAL,
    "endoscope":      MEDICAL,
    "laryngoscope":   MEDICAL,
    "defibrillator":  MEDICAL,
    "ventilator":     MEDICAL,
    "cpap":           MEDICAL,
    "bipap":          MEDICAL,
    "spirometer":     MEDICAL,
    "glucometer":     MEDICAL,
    "insulin":        MEDICAL,
    "prosthetic":     MEDICAL,
    "orthopedic":     MEDICAL,
    "antimicrobial":  MEDICAL,
    "antiseptic":     MEDICAL,
    "betadine":       MEDICAL,
    "tegaderm":       MEDICAL,
    "hydrocolloid":   MEDICAL,
    "incontinence":   MEDICAL,
    "underpad":       MEDICAL,
    "bedpan":         MEDICAL,

    # Dental
    "orthodontic":    DENTAL,
    "denture":        DENTAL,

    # Laboratory
    "pipette":        LABORATORY,
    "centrifuge":     LABORATORY,
    "microscope":     LABORATORY,
    "chromatography": LABORATORY,
    "spectrophotometer": LABORATORY,
    "cuvette":        LABORATORY,
    "vacutainer":     LABORATORY,

    # Personal care
    "shampoo":        PERSONAL_CARE,
    "conditioner":    PERSONAL_CARE,
    "moisturizer":    PERSONAL_CARE,
    "lipstick":       PERSONAL_CARE,
    "mascara":        PERSONAL_CARE,
    "eyeliner":       PERSONAL_CARE,
    "eyeshadow":      PERSONAL_CARE,
    "concealer":      PERSONAL_CARE,
    "sunscreen":      PERSONAL_CARE,
    "perfume":        PERSONAL_CARE,
    "cologne":        PERSONAL_CARE,
    "deodorant":      PERSONAL_CARE,
    "antiperspirant": PERSONAL_CARE,
    "mouthwash":      PERSONAL_CARE,
    "toothpaste":     PERSONAL_CARE,
    "toothbrush":     PERSONAL_CARE,
    "tweezers":       PERSONAL_CARE,

    # Baby
    "pacifier":       BABY,
    "stroller":       BABY,

    # CPG
    "detergent":      CPG,

    # Janitorial
    "disinfectant":   JANITORIAL,

    # Food
    "supplement":     FOOD,
    "vitamin":        FOOD,
    "probiotic":      FOOD,
    "multivitamin":   FOOD,

    # Pet
    "aquarium":       PET,

    # Garden
    "fertilizer":     GARDEN,
    "herbicide":      GARDEN,
    "insecticide":    GARDEN,
    "chainsaw":       GARDEN,

    # Automotive
    "carburetor":     AUTOMOTIVE,
    "alternator":     AUTOMOTIVE,
    "radiator":       AUTOMOTIVE,

    # Electronics
    "motherboard":    ELECTRONICS,
    "laptop":         ELECTRONICS,
    "smartphone":     ELECTRONICS,
    "bluetooth":      ELECTRONICS,
    "wifi":           ELECTRONICS,

    # Arts
    "embroidery":     ARTS,
    "crochet":        ARTS,
    "knitting":       ARTS,

    # Entertainment
    "dvd":            ENTERTAINMENT,
    "blu-ray":        ENTERTAINMENT,
    "paperback":      ENTERTAINMENT,

    # Firearms
    "ammunition":     FIREARMS,
    "crossbow":       FIREARMS,
    "airsoft":        FIREARMS,
    "paintball":      FIREARMS,
}

# ─────────────────────────────────────────────────────────────────────
# Sales-rank-category keyword map (Amazon's own classification)
# These keywords appear in sales_rank_category strings.
# ─────────────────────────────────────────────────────────────────────
_SRC_KEYWORD_MAP: list[tuple[str, Category]] = [
    # Medical
    ("medical",           MEDICAL),
    ("surgical",          MEDICAL),
    ("first aid",         MEDICAL),
    ("wound",             MEDICAL),
    ("bandage",           MEDICAL),
    ("bandaging",         MEDICAL),
    ("gauze",             MEDICAL),
    ("compression sock",  MEDICAL),
    ("compression stock", MEDICAL),
    ("compression tight", MEDICAL),
    ("stethoscope",       MEDICAL),
    ("sphygmomanometer",  MEDICAL),
    ("otoscope",          MEDICAL),
    ("oximeter",          MEDICAL),
    ("blood pressure",    MEDICAL),
    ("cpap",              MEDICAL),
    ("ostomy",            MEDICAL),
    ("incontinence",      MEDICAL),
    ("bladder control",   MEDICAL),
    ("catheter",          MEDICAL),
    ("syringe",           MEDICAL),
    ("needle destruct",   MEDICAL),
    ("sharps container",  MEDICAL),
    ("wheelchair",        MEDICAL),
    ("mobility scooter",  MEDICAL),
    ("walker",            MEDICAL),
    ("fracture",          MEDICAL),
    ("brace",             MEDICAL),
    ("splint",            MEDICAL),
    ("orthopedic",        MEDICAL),
    ("prosthetic",        MEDICAL),
    ("diabetic",          MEDICAL),
    ("disposable mask",   MEDICAL),
    ("disposable respirator", MEDICAL),
    ("exam glove",        MEDICAL),
    ("cast",              MEDICAL),
    ("kinesiology",       MEDICAL),
    ("athletic tape",     MEDICAL),
    ("cold pack",         MEDICAL),
    ("hot pack",          MEDICAL),
    ("heating pad",       MEDICAL),
    ("eye patch",         MEDICAL),
    ("eye mask",          MEDICAL),
    ("pill",              MEDICAL),
    ("cotton swab",       MEDICAL),
    ("cotton",            MEDICAL),
    ("oxygen",            MEDICAL),
    ("heel cushion",      MEDICAL),
    ("heel cup",          MEDICAL),
    ("insole",            MEDICAL),
    ("antimicrobial",     MEDICAL),
    ("topical",           MEDICAL),
    ("pain relief",       MEDICAL),
    ("homeopathic",       MEDICAL),
    ("eczema",            MEDICAL),
    ("psoriasis",         MEDICAL),
    ("scar",              MEDICAL),
    ("adhesive heat",     MEDICAL),
    ("cleansing cloth",   MEDICAL),
    ("bed underpad",      MEDICAL),

    # Dental
    ("dental",            DENTAL),
    ("denture",           DENTAL),
    ("orthodontic",       DENTAL),

    # Laboratory
    ("lab ",              LABORATORY),
    ("chromatography",    LABORATORY),
    ("ph test",           LABORATORY),

    # Personal care
    ("shampoo",           PERSONAL_CARE),
    ("conditioner",       PERSONAL_CARE),
    ("hair color",        PERSONAL_CARE),
    ("hair brush",        PERSONAL_CARE),
    ("hair extension",    PERSONAL_CARE),
    ("hair removal",      PERSONAL_CARE),
    ("hair spray",        PERSONAL_CARE),
    ("hairpiece",         PERSONAL_CARE),
    ("hair mascara",      PERSONAL_CARE),
    ("body lotion",       PERSONAL_CARE),
    ("body cream",        PERSONAL_CARE),
    ("body oil",          PERSONAL_CARE),
    ("face moisturizer",  PERSONAL_CARE),
    ("facial serum",      PERSONAL_CARE),
    ("facial toner",      PERSONAL_CARE),
    ("facial mask",       PERSONAL_CARE),
    ("pore cleansing",    PERSONAL_CARE),
    ("lip balm",          PERSONAL_CARE),
    ("lip moisturizer",   PERSONAL_CARE),
    ("foundation makeup", PERSONAL_CARE),
    ("makeup blend",      PERSONAL_CARE),
    ("nail polish",       PERSONAL_CARE),
    ("nail file",         PERSONAL_CARE),
    ("manicure",          PERSONAL_CARE),
    ("pedicure",          PERSONAL_CARE),
    ("perfume",           PERSONAL_CARE),
    ("eau de ",           PERSONAL_CARE),
    ("cologne",           PERSONAL_CARE),
    ("deodorant",         PERSONAL_CARE),
    ("antiperspirant",    PERSONAL_CARE),
    ("shaving",           PERSONAL_CARE),
    ("razor",             PERSONAL_CARE),
    ("sunscreen",         PERSONAL_CARE),
    ("toothpaste",        PERSONAL_CARE),
    ("toothbrush",        PERSONAL_CARE),
    ("mouthwash",         PERSONAL_CARE),
    ("bath soap",         PERSONAL_CARE),
    ("bath sponge",       PERSONAL_CARE),
    ("bath bomb",         PERSONAL_CARE),
    ("hand sanitizer",    PERSONAL_CARE),
    ("foot cream",        PERSONAL_CARE),
    ("foot file",         PERSONAL_CARE),
    ("foot lotion",       PERSONAL_CARE),
    ("feminine",          PERSONAL_CARE),
    ("sanitary napkin",   PERSONAL_CARE),
    ("eye wrinkle",       PERSONAL_CARE),
    ("wrinkle pad",       PERSONAL_CARE),
    ("tweezer",           PERSONAL_CARE),
    ("sexual lubricant",  PERSONAL_CARE),
    ("hosiery",           PERSONAL_CARE),
    ("sheers",            PERSONAL_CARE),
    ("tights",            PERSONAL_CARE),

    # Baby
    ("baby bottle",       BABY),
    ("baby",              BABY),
    ("infant",            BABY),
    ("pediatric",         BABY),
    ("nursing pad",       BABY),
    ("breast pump",       BABY),

    # CPG
    ("paper towel",       CPG),
    ("toilet paper",      CPG),
    ("tissue",            CPG),
    ("trash bag",         CPG),
    ("disposable cup",    CPG),
    ("battery",           CPG),
    ("bulb",              CPG),

    # Janitorial
    ("cleaning sponge",   JANITORIAL),
    ("cleaning glove",    JANITORIAL),
    ("household clean",   JANITORIAL),
    ("mop pad",           JANITORIAL),
    ("scouring",          JANITORIAL),
    ("all-purpose",       JANITORIAL),
    ("fabric deodor",     JANITORIAL),
    ("disinfectant",      JANITORIAL),

    # Industrial
    ("industrial",        INDUSTRIAL),
    ("safety work",       INDUSTRIAL),
    ("safety glass",      INDUSTRIAL),
    ("safety sign",       INDUSTRIAL),
    ("warning sign",      INDUSTRIAL),
    ("cut resistant",     INDUSTRIAL),
    ("respirator",        INDUSTRIAL),
    ("drive belt",        INDUSTRIAL),
    ("v-belt",            INDUSTRIAL),
    ("timing belt",       INDUSTRIAL),
    ("bearing",           INDUSTRIAL),
    ("o-ring",            INDUSTRIAL),
    ("capacitor",         INDUSTRIAL),
    ("pipe fitting",      INDUSTRIAL),
    ("barbed",            INDUSTRIAL),
    ("duct tape",         INDUSTRIAL),
    ("mounting tape",     INDUSTRIAL),
    ("plastic sheet",     INDUSTRIAL),
    ("hvac",              INDUSTRIAL),
    ("electric fan motor",INDUSTRIAL),
    ("fluorescent tube",  INDUSTRIAL),

    # Electronics (BSR categories)
    ("electronics",       ELECTRONICS),
    ("computers &",       ELECTRONICS),   # "Computers & Accessories"

    # Food (BSR categories)
    ("grocery",           FOOD),
    ("gourmet food",      FOOD),

    # Office
    ("pen ",              OFFICE),
    ("pencil",            OFFICE),
    ("notebook",          OFFICE),
    ("planner",           OFFICE),
    ("greeting card",     OFFICE),
    ("gel ink",           OFFICE),
    ("stylus",            OFFICE),

    # Food / supplement
    ("supplement",        FOOD),
    ("vitamin",           FOOD),
    ("mineral supplement",FOOD),
    ("salad dressing",    FOOD),
    ("red yeast",         FOOD),
    ("herbal supplement", FOOD),

    # Pet
    ("dog ",              PET),
    ("cat ",              PET),
    ("pet ",              PET),
    ("aquarium",          PET),
    ("bird ",             PET),

    # Home / Kitchen
    ("cutting board",     HOME),
    ("can opener",        HOME),
    ("peeler",            HOME),
    ("grater",            HOME),
    ("spatula",           HOME),
    ("tumbler",           HOME),
    ("water bottle",      HOME),
    ("insulated",         HOME),
    ("dinner plate",      HOME),
    ("dinnerware",        HOME),
    ("mug",               HOME),
    ("dish rack",         HOME),
    ("area rug",          HOME),
    ("curtain",           HOME),
    ("door lever",        HOME),
    ("door knob",         HOME),
    ("door handle",       HOME),
    ("deadbolt",          HOME),
    ("door lock",         HOME),
    ("cabinet",           HOME),
    ("furniture pad",     HOME),
    ("vase",              HOME),
    ("artificial flower", HOME),
    ("poster",            HOME),
    ("decorative",        HOME),
    ("refrigerator magnet", HOME),
    ("sign",              HOME),
    ("food container",    HOME),
    ("kitchen storage",   HOME),

    # Garden
    ("lawn mower",        GARDEN),
    ("string trimmer",    GARDEN),
    ("leaf blower",       GARDEN),
    ("chainsaw",          GARDEN),
    ("snow blower",       GARDEN),
    ("garden",            GARDEN),
    ("pool filter",       GARDEN),
    ("pool ",             GARDEN),
    ("tree plant",        GARDEN),
    ("grill ",            GARDEN),
    ("flag",              GARDEN),
    ("sports & outdoors",  INDUSTRIAL),  # hot/cold packs, braces, athletic tape live here
    ("outdoor",           GARDEN),

    # Tools
    ("power tool",        TOOLS),
    ("router bit",        TOOLS),
    ("sander",            TOOLS),
    ("hook & loop disc",  TOOLS),

    # Sports
    ("golf ",             SPORTS),
    ("bike ",             SPORTS),
    ("bicycle",           SPORTS),
    ("hiking",            SPORTS),
    ("camping",           SPORTS),
    ("marine dry",        SPORTS),
    ("binocular",         SPORTS),

    # Clothing
    ("t-shirt",           CLOTHING),
    ("novelty t-shirt",   CLOTHING),
    ("sneaker",           CLOTHING),
    ("running shoe",      CLOTHING),
    ("clog",              CLOTHING),
    ("mule",              CLOTHING),
    ("loafer",            CLOTHING),
    ("sandal",            CLOTHING),
    ("flip-flop",         CLOTHING),
    ("slipper",           CLOTHING),
    ("oxford",            CLOTHING),
    ("skateboarding shoe",CLOTHING),
    ("jeans",             CLOTHING),
    ("sock",              CLOTHING),
    ("belt",              CLOTHING),
    ("hat",               CLOTHING),
    ("cap",               CLOTHING),
    ("shirt",             CLOTHING),
    ("dress",             CLOTHING),
    ("fashion",           CLOTHING),
    ("hoodie",            CLOTHING),
    ("sweatshirt",        CLOTHING),
    ("athletic",          CLOTHING),
    ("activewear",        CLOTHING),
    ("sunglasses",        CLOTHING),
    ("eyewear",           CLOTHING),
    ("eyeglass",          CLOTHING),
    ("women's",           CLOTHING),
    ("men's",             CLOTHING),
    ("girls'",            CLOTHING),
    ("boys'",             CLOTHING),
    ("shoelace",          CLOTHING),
    ("shoe charm",        CLOTHING),
    ("leg warmer",        CLOTHING),
    ("sports bra",        CLOTHING),

    # Automotive
    ("automotive",        AUTOMOTIVE),
    ("powersport",        AUTOMOTIVE),
    ("car ",              AUTOMOTIVE),
    ("tire",              AUTOMOTIVE),
    ("bumper sticker",    AUTOMOTIVE),
    ("decal",             AUTOMOTIVE),
    ("touchup paint",     AUTOMOTIVE),

    # Electronics
    ("laptop",            ELECTRONICS),
    ("computer",          ELECTRONICS),
    ("printer",           ELECTRONICS),
    ("ink cartridge",     ELECTRONICS),
    ("toner cartridge",   ELECTRONICS),
    ("cell phone",        ELECTRONICS),
    ("tablet case",       ELECTRONICS),
    ("usb ",              ELECTRONICS),
    ("remote control",    ELECTRONICS),
    ("led strip",         ELECTRONICS),
    ("single board",      ELECTRONICS),
    ("smart watch",       ELECTRONICS),
    ("smartwatch",        ELECTRONICS),
    ("headphone",         ELECTRONICS),
    ("earpad",            ELECTRONICS),
    ("camera",            ELECTRONICS),
    ("enclosure",         ELECTRONICS),
    ("power converter",   ELECTRONICS),
    ("monitor",           ELECTRONICS),

    # Arts
    ("sewing",            ARTS),
    ("pattern",           ARTS),
    ("yarn",              ARTS),
    ("fabric",            ARTS),
    ("craft",             ARTS),
    ("pottery",           ARTS),
    ("clay",              ARTS),
    ("art paint",         ARTS),
    ("crayon",            ARTS),
    ("coloring pen",      ARTS),
    ("marker",            ARTS),

    # Toys
    ("action figure",     TOYS),
    ("collectible figure",TOYS),
    ("building set",      TOYS),
    ("jigsaw puzzle",     TOYS),
    ("stuffed animal",    TOYS),
    ("teddy bear",        TOYS),
    ("word search",       TOYS),
    ("trading card",      TOYS),
    ("hobby remote",      TOYS),

    # Entertainment — BSR categories
    # "Movies & TV" and its sub-categories all contain "movies"; "music" is also here.
    # "exercise & fitness" DVDs land in Sports/Health BSR but are media products —
    # map them to ENTERTAINMENT so medical mode rejects them (dist ≈ 9.4).
    ("movies",            ENTERTAINMENT),   # "Movies & TV", "Movies & TV > ..."
    ("exercise & fitness dvd", ENTERTAINMENT),  # exercise instruction DVDs
    ("fitness dvd",       ENTERTAINMENT),
    # BSR barcode-format entries
    ("dvd",               ENTERTAINMENT),
    ("blu-ray",           ENTERTAINMENT),
    ("blu ray",           ENTERTAINMENT),
    ("cd & vinyl",        ENTERTAINMENT),
    ("rock (",            ENTERTAINMENT),
    ("comedy (",          ENTERTAINMENT),
    ("drama ",            ENTERTAINMENT),
    ("horror ",           ENTERTAINMENT),
    ("action & adventure",ENTERTAINMENT),
    ("mystery",           ENTERTAINMENT),
    ("anime",             ENTERTAINMENT),
    ("documentary",       ENTERTAINMENT),
    ("kids & family",     ENTERTAINMENT),
    ("special interest",  ENTERTAINMENT),
    ("history (book",     ENTERTAINMENT),
    ("literature",        ENTERTAINMENT),
    ("romance (book",     ENTERTAINMENT),
    ("contemporary romance", ENTERTAINMENT),
    ("reference (book",   ENTERTAINMENT),
    ("book",              ENTERTAINMENT),

    # Firearms
    ("gun sight",         FIREARMS),
    ("rifle scope",       FIREARMS),
    ("gun holster",       FIREARMS),
    ("airsoft",           FIREARMS),

    # Jewelry
    ("necklace",          JEWELRY),
    ("pendant",           JEWELRY),
    ("bracelet",          JEWELRY),
    ("earring",           JEWELRY),
    ("wrist watch",       JEWELRY),
    ("watch band",        JEWELRY),
    ("watch repair",      JEWELRY),
    ("coin & button",     JEWELRY),
    ("jewelry",           JEWELRY),
    ("collectible coin",  JEWELRY),

    # ── BROAD CATCH-ALL SRC KEYWORDS (lower priority, listed last) ───

    # Medical / health (broad)
    ("support",           MEDICAL),   # "Hip & Waist Supports", "Arm Supports", etc.
    ("glove",             MEDICAL),   # "Non-Sterile Disposable Safety Gloves"
    ("multidrug test",    MEDICAL),
    ("nicotine",          MEDICAL),
    ("diaper cream",      MEDICAL),
    ("aftercare",         MEDICAL),   # tattoo aftercare etc.

    # Personal care (broad)
    ("soap",              PERSONAL_CARE),
    ("wash",              PERSONAL_CARE),
    ("lotion",            PERSONAL_CARE),
    ("cream",             PERSONAL_CARE),
    ("wipe",              PERSONAL_CARE),
    ("depilator",         PERSONAL_CARE),
    ("hair treatment",    PERSONAL_CARE),
    ("wig",               PERSONAL_CARE),
    ("false eyelash",     PERSONAL_CARE),
    ("eyelash",           PERSONAL_CARE),
    ("mirror",            PERSONAL_CARE),
    ("bath ",             PERSONAL_CARE),
    ("loofah",            PERSONAL_CARE),
    ("essential oil",     PERSONAL_CARE),
    ("tattoo",            PERSONAL_CARE),
    ("cleansing",         PERSONAL_CARE),

    # CPG (broad)
    ("batteries",         CPG),
    ("freshener",         CPG),
    ("laundry",           CPG),
    ("candle",            CPG),
    ("gift wrap",         CPG),

    # Janitorial / cleaning (broad)
    ("vacuum ",           JANITORIAL),
    ("filter",            JANITORIAL),  # furnace, air purifier, vacuum filters

    # Industrial / safety (broad)
    ("adhesive",          INDUSTRIAL),
    ("sealant",           INDUSTRIAL),
    ("tape",              INDUSTRIAL),
    ("fuse",              INDUSTRIAL),
    ("chemical resist",   INDUSTRIAL),
    ("padlock",           INDUSTRIAL),
    ("security",          INDUSTRIAL),
    ("keypad",            INDUSTRIAL),
    ("fireplace",         INDUSTRIAL),
    ("motor",             INDUSTRIAL),

    # Office (broad)
    ("label",             OFFICE),
    ("luggage tag",       OFFICE),

    # Food (broad)
    ("ice cream scoop",   FOOD),
    ("home & kitchen",     PERSONAL_CARE),   # washcloths, toothbrush holders, patient care
    ("kitchen & dining",   PERSONAL_CARE),   # same — personal care items sold here
    ("kitchen",           FOOD),
    ("strainer",          FOOD),
    ("straw",             FOOD),

    # Home (broad)
    ("replacement part",  HOME),
    ("pressure cooker",   HOME),
    ("tablecloth",        HOME),
    ("soap dish",         HOME),
    ("water filter",      HOME),
    ("drain",             HOME),
    ("strainer",          HOME),

    # Garden (broad)
    ("agricultural",      GARDEN),
    ("rv ",               GARDEN),
    ("power chain saw",   GARDEN),
    ("chain saw",         GARDEN),
    ("sprayer",           GARDEN),

    # Tools (broad)
    ("polishing",         TOOLS),
    ("buffing",           TOOLS),
    ("grinding",          TOOLS),
    ("sanding",           TOOLS),
    ("bracket",           TOOLS),
    ("power tool",        TOOLS),

    # Sports (broad)
    ("optic",             SPORTS),
    ("daypack",           SPORTS),
    ("duffel",            SPORTS),
    ("packing organiz",   SPORTS),
    ("knife",             SPORTS),  # pocket/folding knives

    # Clothing (broad)
    ("bra",               CLOTHING),
    ("charm",             CLOTHING),  # shoe decoration charms

    # Automotive (broad)
    ("emblem",            AUTOMOTIVE),
    ("body repair",       AUTOMOTIVE),
    ("vehicle",           AUTOMOTIVE),
    ("gps",               AUTOMOTIVE),

    # Electronics (broad)
    ("cable",             ELECTRONICS),
    ("mice",              ELECTRONICS),
    ("mouse",             ELECTRONICS),
    ("gaming",            ELECTRONICS),
    ("radio",             ELECTRONICS),
    ("rc ",               ELECTRONICS),
    ("remote control",    ELECTRONICS),
    ("microphone",        ELECTRONICS),
    ("power strip",       ELECTRONICS),

    # Arts (broad)
    ("bobbin",            ARTS),

    # Toys (broad)
    ("stacking block",    TOYS),
    ("figurine",          TOYS),
    ("collectible",       TOYS),

    # Entertainment (broad)
    ("music",             ENTERTAINMENT),
    ("performing art",    ENTERTAINMENT),
    ("concert",           ENTERTAINMENT),
    ("romance (",         ENTERTAINMENT),
    ("history",           ENTERTAINMENT),
    ("england",           ENTERTAINMENT),

    # Firearms (broad)
    ("gunsmith",          FIREARMS),

    # ── VERY BROAD CATCH-ALLS (last resort — SRC context only) ───────

    # Medical extras
    ("electrode",         MEDICAL),
    ("sterilization",     MEDICAL),
    ("diagnostic",        MEDICAL),
    ("therapy",           MEDICAL),
    ("rehab",             MEDICAL),
    ("orthotic",          MEDICAL),
    ("prosthes",          MEDICAL),

    # Industrial / safety extras
    ("earplug",           INDUSTRIAL),
    ("earmuff",           INDUSTRIAL),
    ("ear protection",    INDUSTRIAL),
    ("face shield",       INDUSTRIAL),
    ("protective",        INDUSTRIAL),
    ("rubber sheet",      INDUSTRIAL),
    ("rubber strip",      INDUSTRIAL),
    ("gasket",            INDUSTRIAL),
    ("rope",              INDUSTRIAL),
    ("lanyard",           INDUSTRIAL),
    ("restraint",         INDUSTRIAL),
    ("chain",             INDUSTRIAL),
    ("hook",              INDUSTRIAL),
    ("strap",             INDUSTRIAL),
    ("clamp",             INDUSTRIAL),
    ("manifold",          INDUSTRIAL),
    ("valve",             INDUSTRIAL),
    ("pump",              INDUSTRIAL),
    ("nozzle",            INDUSTRIAL),
    ("coupl",             INDUSTRIAL),
    ("connector",         INDUSTRIAL),
    ("hose",              INDUSTRIAL),
    ("tubing",            INDUSTRIAL),
    ("wire",              INDUSTRIAL),
    ("conduit",           INDUSTRIAL),
    ("insulation",        INDUSTRIAL),
    ("seal",              INDUSTRIAL),

    # Tools extras
    ("wheel",             TOOLS),    # cut-off wheels, grinding wheels
    ("disc",              TOOLS),    # flap discs, sanding discs
    ("caulk",             TOOLS),
    ("paint",             TOOLS),    # spray paint, paint buckets
    ("applicator",        TOOLS),
    ("gun",               TOOLS),    # caulking gun, spray gun
    ("saw",               TOOLS),
    ("cutter",            TOOLS),
    ("knife",             TOOLS),    # taping knives, utility knives
    ("blade",             TOOLS),
    ("bit",               TOOLS),
    ("wrench",            TOOLS),
    ("plier",             TOOLS),
    ("file",              TOOLS),
    ("scraper",           TOOLS),
    ("tool",              TOOLS),
    ("multitool",         TOOLS),
    ("level",             TOOLS),
    ("measuring",         TOOLS),

    # Automotive extras
    ("polish",            AUTOMOTIVE),
    ("wax",               AUTOMOTIVE),
    ("undercoat",         AUTOMOTIVE),
    ("emblem",            AUTOMOTIVE),
    ("gas tank",          AUTOMOTIVE),
    ("lens",              AUTOMOTIVE),  # light covers & lenses
    ("grille",            AUTOMOTIVE),
    ("fender",            AUTOMOTIVE),
    ("bumper",            AUTOMOTIVE),
    ("hood",              AUTOMOTIVE),
    ("trunk",             AUTOMOTIVE),
    ("door ",             AUTOMOTIVE),  # trailing space avoids "doorbell"

    # Electronics extras
    ("meter",             ELECTRONICS),
    ("tester",            ELECTRONICS),
    ("sensor",            ELECTRONICS),

    # Home extras
    ("pan",               HOME),     # roasting pans, frying pans
    ("pot",               HOME),
    ("skillet",           HOME),
    ("bakeware",          HOME),
    ("cookware",          HOME),
    ("storage",           HOME),
    ("organizer",         HOME),
    ("rack",              HOME),
    ("shelf",             HOME),
    ("hook",              HOME),
    ("hanger",            HOME),
    ("mat",               HOME),
    ("rug",               HOME),
    ("pillow",            HOME),
    ("bedding",           HOME),
    ("towel",             HOME),
    ("mirror",            HOME),
    ("lamp",              HOME),
    ("light",             HOME),
    ("fixture",           HOME),
    ("faucet",            HOME),
    ("knob",              HOME),
    ("handle",            HOME),
    ("hardware",          HOME),
    ("plumbing",          HOME),

    # Sports extras
    ("backpack",          SPORTS),
    ("bag",               SPORTS),

    # Catch remaining media
    ("series",            ENTERTAINMENT),
    ("season",            ENTERTAINMENT),
    ("edition",           ENTERTAINMENT),
    ("soundtrack",        ENTERTAINMENT),
    ("classical",         ENTERTAINMENT),
    ("jazz",              ENTERTAINMENT),
    ("r&b",               ENTERTAINMENT),
    ("hip-hop",           ENTERTAINMENT),
    ("pop (",             ENTERTAINMENT),
    ("country (",         ENTERTAINMENT),
    ("gospel",            ENTERTAINMENT),
    ("folk",              ENTERTAINMENT),
    ("world music",       ENTERTAINMENT),
    ("compilation",       ENTERTAINMENT),

    # ── ULTRA-BROAD (SRC-only, catches stragglers) ──────────────────

    # Medical catch-all
    ("health",            MEDICAL),
    ("clinical",          MEDICAL),
    ("patient",           MEDICAL),
    ("sterile",           MEDICAL),
    ("examination",       MEDICAL),

    # Personal care catch-all
    ("clipper",           PERSONAL_CARE),
    ("grooming",          PERSONAL_CARE),
    ("skincare",          PERSONAL_CARE),
    ("skin care",         PERSONAL_CARE),

    # CPG catch-all
    ("wrap",              CPG),
    ("tissue",            CPG),

    # Janitorial catch-all
    ("floor",             JANITORIAL),
    ("cleaning",          JANITORIAL),
    ("cloth",             JANITORIAL),
    ("dispenser",         JANITORIAL),
    ("mop",               JANITORIAL),

    # Industrial catch-all
    ("goggle",            INDUSTRIAL),
    ("helmet",            INDUSTRIAL),
    ("weld",              INDUSTRIAL),
    ("cement",            INDUSTRIAL),
    ("rubber",            INDUSTRIAL),
    ("silicone",          INDUSTRIAL),
    ("terminal",          INDUSTRIAL),
    ("rivet",             INDUSTRIAL),
    ("fastener",          INDUSTRIAL),
    ("screw",             INDUSTRIAL),
    ("bolt",              INDUSTRIAL),
    ("nut ",              INDUSTRIAL),
    ("washer",            INDUSTRIAL),
    ("spring",            INDUSTRIAL),
    ("bushing",           INDUSTRIAL),
    ("grommet",           INDUSTRIAL),
    ("spacer",            INDUSTRIAL),
    ("retainer",          INDUSTRIAL),
    ("bracket",           INDUSTRIAL),

    # Tools catch-all
    ("grinder",           TOOLS),
    ("filler",            TOOLS),

    # Home catch-all
    ("wallpaper",         HOME),
    ("decor",             HOME),
    ("furniture",         HOME),
    ("chair",             HOME),
    ("table ",            HOME),
    ("desk ",             HOME),
    ("bed ",              HOME),

    # Garden catch-all
    ("greenhouse",        GARDEN),
    ("irrigation",        GARDEN),
    ("compost",           GARDEN),

    # Electronics catch-all
    ("cord",              ELECTRONICS),
    ("adapter",           ELECTRONICS),
    ("charger",           ELECTRONICS),
    ("converter",         ELECTRONICS),
    ("regulator",         ELECTRONICS),
    ("amplifier",         ELECTRONICS),
    ("speaker",           ELECTRONICS),
    ("display",           ELECTRONICS),
    ("projector",         ELECTRONICS),
    ("printer",           ELECTRONICS),
    ("scanner",           ELECTRONICS),

    # Automotive catch-all
    ("oil",               AUTOMOTIVE),
    ("lube",              AUTOMOTIVE),
    ("fluid",             AUTOMOTIVE),
    ("grease",            AUTOMOTIVE),

    # Office catch-all
    ("note pad",          OFFICE),
    ("note ",             OFFICE),
    ("tab",               OFFICE),
    ("index",             OFFICE),
    ("tag",               OFFICE),

    # Toys catch-all
    ("toy ",              TOYS),
    ("puzzle",            TOYS),

    # Pet catch-all
    ("collar",            PET),

    # ── FINAL CATCH-ALL LAYER ──────────────────────────────────────

    # Medical final
    ("commode",           MEDICAL),
    ("antifungal",        MEDICAL),
    ("pregnancy test",    MEDICAL),
    ("urinary tract",     MEDICAL),
    ("bunion",            MEDICAL),
    ("snore",             MEDICAL),
    ("sleep mask",        MEDICAL),
    ("muscle stimulat",   MEDICAL),
    ("needle-free",       MEDICAL),
    ("reading glass",     MEDICAL),
    ("remedy",            MEDICAL),
    ("detox",             MEDICAL),
    ("cleanse",           MEDICAL),

    # Personal care final
    ("gel",               PERSONAL_CARE),
    ("butter",            PERSONAL_CARE),
    ("primer",            PERSONAL_CARE),
    ("cushion",           PERSONAL_CARE),
    ("mask",              PERSONAL_CARE),
    ("cigarette",         PERSONAL_CARE),

    # CPG final
    ("balloon",           CPG),
    ("christmas",         CPG),
    ("holiday",           CPG),
    ("party ",            CPG),
    ("calendar",          CPG),

    # Janitorial final
    ("duster",            JANITORIAL),
    ("tarp",              JANITORIAL),

    # Industrial final
    ("switch",            INDUSTRIAL),
    ("relay",             INDUSTRIAL),

    # Home final
    ("showerhead",        HOME),
    ("shower",            HOME),
    ("tree",              HOME),   # Christmas trees, artificial trees
    ("press",             HOME),   # garlic presses
    ("tongs",             HOME),
    ("cover",             HOME),   # exterior covers
    ("fan",               HOME),   # personal fans
    ("heater",            HOME),
    ("cooler",            HOME),
    ("fryer",             HOME),
    ("bottle",            HOME),   # condiment bottles
    ("paper",             HOME),   # carbonless paper etc.

    # Electronics final
    ("drive",             ELECTRONICS),
    ("cooling",           ELECTRONICS),
    ("heatsink",          ELECTRONICS),
    ("power supply",      ELECTRONICS),
    ("keyboard",          ELECTRONICS),

    # Automotive final
    ("tonneau",           AUTOMOTIVE),
    ("exterior",          AUTOMOTIVE),

    # Sports final
    ("knives",            SPORTS),
    ("skateboard",        SPORTS),
    ("skate",             SPORTS),

    # Arts final
    ("embroidery",        ARTS),
    ("cross-stitch",      ARTS),
    ("felt ",             ARTS),
    ("needl",             ARTS),

    # Entertainment final — (Movies & TV) pattern catches all genres
    ("(movies",           ENTERTAINMENT),
    ("(book",             ENTERTAINMENT),
    ("(cd",               ENTERTAINMENT),
    ("fiction",           ENTERTAINMENT),
    ("novel",             ENTERTAINMENT),
    ("comics",            ENTERTAINMENT),
    ("graphic novel",     ENTERTAINMENT),
    ("poetry",            ENTERTAINMENT),
    ("poem",              ENTERTAINMENT),
    ("humor",             ENTERTAINMENT),
    ("biography",         ENTERTAINMENT),
    ("memoir",            ENTERTAINMENT),
    ("prophecy",          ENTERTAINMENT),
    ("prophecies",        ENTERTAINMENT),
    ("counseling",        ENTERTAINMENT),
    ("programming",       ENTERTAINMENT),
    ("common core",       ENTERTAINMENT),
    ("christian",         ENTERTAINMENT),
    ("spiritual",         ENTERTAINMENT),
    ("science fiction",   ENTERTAINMENT),
    ("fantasy",           ENTERTAINMENT),
    ("horror",            ENTERTAINMENT),
    ("thriller",          ENTERTAINMENT),
    ("western",           ENTERTAINMENT),
    ("brewing",           ENTERTAINMENT),
    ("reagent",           LABORATORY),
    ("analytical",        LABORATORY),
    ("chemical",          LABORATORY),
    ("solution",          LABORATORY),

    # ── ABSOLUTE LAST RESORT (single words, SRC only) ──────────────

    # Medical
    ("suture",            MEDICAL),
    ("cot",               MEDICAL),   # finger cots
    ("infection",         MEDICAL),
    ("antacid",           MEDICAL),
    ("cane",              MEDICAL),   # walking canes
    ("massager",          MEDICAL),
    ("ovulation",         MEDICAL),
    ("marijuana test",    MEDICAL),
    ("ketone",            MEDICAL),
    ("stoma",             MEDICAL),

    # Personal care
    ("tampon",            PERSONAL_CARE),
    ("comb",              PERSONAL_CARE),
    ("mousse",            PERSONAL_CARE),
    ("fiber",             PERSONAL_CARE),  # hair building fibers
    ("regrowth",         PERSONAL_CARE),
    ("fragrance",         PERSONAL_CARE),
    ("fragrant",          PERSONAL_CARE),
    ("spray",             PERSONAL_CARE),
    ("room spray",        HOME),

    # Home
    ("filtration",        HOME),
    ("spoon",             HOME),
    ("teaspoon",          HOME),
    ("whisk",             HOME),
    ("bowl",              HOME),
    ("glass",             HOME),    # wine glasses, etc.
    ("plate",             HOME),
    ("cup",               HOME),    # toddler cups, souffle cups
    ("humidifier",        HOME),
    ("dehumidifier",      HOME),
    ("statue",            HOME),
    ("appliance",         HOME),
    ("range",             HOME),
    ("oven",              HOME),
    ("microwave",         HOME),
    ("blender",           HOME),
    ("mixer",             HOME),
    ("processor",         HOME),

    # Automotive
    ("automobile",        AUTOMOTIVE),
    ("visor",             AUTOMOTIVE),
    ("deflector",         AUTOMOTIVE),
    ("lift kit",          AUTOMOTIVE),

    # Tools
    ("glue",              TOOLS),
    ("key",               TOOLS),    # hex keys

    # Industrial
    ("resistor",          INDUSTRIAL),
    ("guide",             INDUSTRIAL),
    ("caliper",           INDUSTRIAL),
    ("plasma",            INDUSTRIAL),
    ("plug",              INDUSTRIAL),  # electric plugs
    ("collet",            INDUSTRIAL),

    # Electronics
    ("tripod",            ELECTRONICS),
    ("finder",            ELECTRONICS),
    ("device",            ELECTRONICS),
    ("system",            ELECTRONICS),
    ("equipment",         ELECTRONICS),

    # Office
    ("binder",            OFFICE),
    ("sleeve",            OFFICE),
    ("card",              OFFICE),
    ("sticker",           OFFICE),
    ("lock",              OFFICE),   # mailbox locks etc.
    ("box",               OFFICE),   # electrical boxes, mailboxes

    # Toys
    ("doll",              TOYS),
    ("bathtub toy",       TOYS),
    ("game",              TOYS),

    # Sports
    ("finder",            SPORTS),  # fish finders

    # Food
    ("protein drink",     FOOD),
    ("syrup",             FOOD),
    ("ashwagandha",       FOOD),

    # Entertainment
    ("dvds",              ENTERTAINMENT),
    ("nonfiction",        ENTERTAINMENT),
    ("self-help",         ENTERTAINMENT),
    ("philosophy",        ENTERTAINMENT),
    ("religion",          ENTERTAINMENT),
    ("education",         ENTERTAINMENT),
    ("children",          ENTERTAINMENT),
    ("parenting",         ENTERTAINMENT),
    ("cooking",           ENTERTAINMENT),  # cookbooks
    ("travel",            ENTERTAINMENT),
    ("guide",             ENTERTAINMENT),
    ("journal",           ENTERTAINMENT),
    ("diary",             ENTERTAINMENT),
    ("activity",          ENTERTAINMENT),
    ("coloring",          ENTERTAINMENT),
    ("workbook",          ENTERTAINMENT),

    # ── PUSH-TO-98% (aggregated broad patterns for SRC long tail) ──

    # Personal care / beauty (catches lipstick, eye liner, concealer, etc.)
    ("lipstick",          PERSONAL_CARE),
    ("eye liner",         PERSONAL_CARE),
    ("eyeshadow",         PERSONAL_CARE),
    ("face powder",       PERSONAL_CARE),
    ("concealer",         PERSONAL_CARE),
    ("lip gloss",         PERSONAL_CARE),
    ("nail ",             PERSONAL_CARE),
    ("eye ",              PERSONAL_CARE),
    ("lip ",              PERSONAL_CARE),
    ("serum",             PERSONAL_CARE),
    ("face ",             PERSONAL_CARE),
    ("cosmetic",          PERSONAL_CARE),
    ("makeup",            PERSONAL_CARE),

    # Medical
    ("crutch",            MEDICAL),
    ("heat pad",          MEDICAL),

    # Toys (catches all "Kids' Play X" categories)
    ("kids'",             TOYS),
    ("playset",           TOYS),
    ("play ",             TOYS),

    # Firearms / hunting
    ("hunting",           FIREARMS),
    ("boresight",         FIREARMS),

    # Sports / outdoors
    ("fishing",           SPORTS),
    ("hydration",         SPORTS),
    ("luggage",           SPORTS),
    ("carry-on",          SPORTS),
    ("suitcase",          SPORTS),

    # Food / beverage
    ("bacon",             FOOD),
    ("soda",              FOOD),
    ("drink",             FOOD),
    ("jerky",             FOOD),
    ("baking",            FOOD),

    # Automotive
    ("jump start",        AUTOMOTIVE),
    ("towing",            AUTOMOTIVE),
    ("hitch",             AUTOMOTIVE),
    ("starter",           AUTOMOTIVE),

    # Industrial
    ("sling",             INDUSTRIAL),
    ("solenoid",          INDUSTRIAL),
    ("ratchet",           INDUSTRIAL),
    ("fitting",           INDUSTRIAL),
    ("probe",             INDUSTRIAL),
    ("lead",              INDUSTRIAL),

    # Tools
    ("chuck",             TOOLS),
    ("lathe",             TOOLS),
    ("repair",            TOOLS),

    # Home
    ("blind",             HOME),
    ("sconce",            HOME),
    ("thermostat",        HOME),
    ("door",              HOME),
    ("window",            HOME),
    ("griddle",           HOME),
    ("sofa",              HOME),
    ("couch",             HOME),
    ("platter",           HOME),
    ("mandoline",         HOME),
    ("seed",              HOME),  # flower seeds
    ("rod ",              HOME),  # curtain rods, window rods
    ("valance",           HOME),

    # Office
    ("bulletin",          OFFICE),
    ("memo",              OFFICE),
    ("pen ",              OFFICE),  # fountain pens
    ("pencil",            OFFICE),

    # Arts
    ("drawing",           ARTS),
    ("craft",             ARTS),

    # Electronics
    ("headset",           ELECTRONICS),

    # Entertainment — broader genre patterns
    ("romance",           ENTERTAINMENT),
    ("mystery",           ENTERTAINMENT),
    ("suspense",          ENTERTAINMENT),
    ("adventure",         ENTERTAINMENT),

    # ── FINAL SWEEP (SRC long-tail, ~30+ products each) ────────────
    ("nut",               INDUSTRIAL),
    ("actuator",          INDUSTRIAL),
    ("sheave",            INDUSTRIAL),
    ("insert",            INDUSTRIAL),
    ("weather strip",     INDUSTRIAL),
    ("absorber",          INDUSTRIAL),
    ("hoist",             INDUSTRIAL),
    ("transformer",       INDUSTRIAL),
    ("inverter",          INDUSTRIAL),
    ("protractor",        INDUSTRIAL),
    ("end mill",          INDUSTRIAL),
    ("chisel",            INDUSTRIAL),
    ("crimper",           INDUSTRIAL),
    ("vacuum",            HOME),
    ("refrigerator",      HOME),
    ("freezer",           HOME),
    ("cooktop",           HOME),
    ("dryer",             HOME),
    ("spinner",           HOME),
    ("mount",             HOME),
    ("plant",             HOME),
    ("greenery",          HOME),
    ("preserved flower",  HOME),
    ("planter",           HOME),
    ("scissors",          HOME),
    ("shear",             HOME),
    ("mailer",            OFFICE),
    ("staple",            OFFICE),
    ("stamp",             OFFICE),
    ("ruled pad",         OFFICE),
    ("gemstone",          JEWELRY),
    ("zester",            HOME),
    ("moleskin",          MEDICAL),
    ("laxative",          MEDICAL),
    ("urinal",            MEDICAL),
    ("allergy",           MEDICAL),
    ("swaddl",            BABY),
    ("scalp",             PERSONAL_CARE),
    ("detangler",         PERSONAL_CARE),
    ("pomade",            PERSONAL_CARE),
    ("eyebrow",           PERSONAL_CARE),
    ("scrub",             PERSONAL_CARE),
    ("peel",              PERSONAL_CARE),
    ("puff",              PERSONAL_CARE),
    ("salt",              PERSONAL_CARE),
    ("soak",              PERSONAL_CARE),
    ("moisturizer",       PERSONAL_CARE),
    ("honey",             FOOD),
    ("candy",             FOOD),
    ("chocolate",         FOOD),
    ("tea",               FOOD),
    ("pouch",             FIREARMS),
    ("weapon",            FIREARMS),
    ("armor",             FIREARMS),
    ("pedal",             SPORTS),
    ("resistance band",   SPORTS),
    ("guitar",            ENTERTAINMENT),
    ("bridge part",       ENTERTAINMENT),
    ("bead",              ARTS),
    ("glaze",             ARTS),
    ("plush",             TOYS),
    ("squeeze toy",       TOYS),
    ("costume",           TOYS),
    ("background",        ELECTRONICS),  # photo backgrounds
    ("prototyping",       ELECTRONICS),
    ("surge",             ELECTRONICS),
    ("robotic",           ELECTRONICS),
    ("antitheft",         AUTOMOTIVE),
]


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────

def categorize(title: str = None,
               sales_rank_category: str = None) -> Category:
    """Categorize a product by keyword matching.

    Checks sales_rank_category first (Amazon's own classification is high
    signal), then falls back to title phrase/word matching.

    Args:
        title: product title string
        sales_rank_category: Amazon Best Sellers Rank category string

    Returns:
        Category dataclass with .rank and .name
    """
    # 1. Try sales_rank_category first (highest signal)
    if sales_rank_category:
        src_lower = sales_rank_category.lower()
        for keyword, cat in _SRC_KEYWORD_MAP:
            if keyword in src_lower:
                return cat

    # 2. Try title phrase matching
    if title:
        title_lower = title.lower()
        for phrase, cat in _PHRASE_MAP:
            if phrase in title_lower:
                return cat

        # 3. Fall back to single-word matching on title
        # Tokenize: split on non-alphanumeric, keep hyphenated words
        words = set(re.split(r'[^a-z0-9\-]+', title_lower))
        for word, cat in _WORD_MAP.items():
            if word in words:
                return cat

    return UNKNOWN
