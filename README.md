# Catalog Verifier — Amazon Listing Verification (CPG Phase, v2)

A local web app that automates the vetting of CPG vendor catalog products against their pre-matched Amazon listings. Built for high-volume daily use by an e-commerce ops team.

Two Excel files in — one colour-coded Excel out, with a strict override flow, a persistent UPC/ASIN blacklist, categorised abbreviation library, and an adjustable confidence model.

---

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Optional: enable the AI Extractor
# edit .env → OPENAI_API_KEY=sk-...

python main.py                     # or: uvicorn main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. A sample CPG dataset is preloaded on first open — click **Run Verification**.

---

## What's new in v2

**Barcode lookup chain** — five sources tried in order, first hit wins: Open Food Facts → Open Beauty Facts → Open Products Facts → UPC Item DB → DuckDuckGo scrape. All keyless, all async, all non-blocking.

**Strict override flow** — each tab exposes only the actions that make sense for it:
- Approved tab → **Reject** (tags row as *Manually Rejected*, blacklists the pair)
- Review tab → **Approve** (tags *Reviewed*) or **Discard** (moves to Not Approved, blacklists)
- Not Approved tab → **Promote to Approved** (tags *Manually Approved*)
- Auto-verified rows get **no tag**

Bulk buttons per tab apply the same action to every currently-visible row.

**Adjustable thresholds** — a Settings side panel holds two number inputs (verified boundary, review boundary). Defaults are **85 / 35** (was 85 / 60). Persisted in SQLite.

**Not Verified → Not Approved** — verdict label renamed throughout UI and export.

**Pre-export warning** — if the Review tab still has unreviewed items, a modal asks to confirm: "Export Anyway" or "Go Back to Review". Never blocks.

**Categorised abbreviation library (SQLite)** — 10 named categories (Colors, Sizes, UOMs, Forms, Sterility, Materials, Scents, Flavors, Packaging, Product Attributes), each with a pre-populated starter set. Editable per category from the side panel. The AI Extractor auto-learns: any unabbreviated term it extracts that isn't already in the library is added silently to "Product Attributes".

**AI Extract button relocated** — removed from the header, now sits to the right of the Vendor Catalog upload card. Teal when active, grey when off.

**Pair Manager tab** — a new top-level tool with search-only access to the blacklist. Enter a UPC or ASIN, see matching rows (UPC · ASIN · Confidence at blacklist · Failed signals · Date · **Unlock Pair** button).

**SQLite persistence** — single file `catalog_verifier.db` (auto-created next to `main.py`). Tables:

| Table                 | Purpose |
|-|-|
| `keepa_imports`       | Raw Keepa rows keyed by ASIN, reusable across sessions |
| `amazon_imports`      | Raw Amazon export rows keyed by ASIN, reusable across sessions |
| `blacklisted_pairs`   | UPC/ASIN pairs rejected by user or engine (with confidence + failed signals + date) |
| `verified_items`      | Final approved rows with their review status |
| `attribute_cache`     | Normalised attributes per (UPC, ASIN) — source-tagged |
| `abbreviation_library`| Categorised abbreviation/full-form entries (user + AI additions) |
| `settings`            | Verified / Review threshold key/value |

**Attribute cache + refresh** — each results row has a **Clear cache** button. Clicking it deletes the cached entry and immediately triggers a fresh lookup through the barcode chain, with a spinner on that row while it runs.

---

## Confidence signals (unchanged weights)

| Signal | Weight | Logic |
|-|-|-|
| UPC / EAN | 25% | Exact match = full points. Mismatch = 0 but never auto-fails (UPCs change often). |
| Item ID / Part Number / Model | 20% | Allows minor suffix variants (`40065` vs `40065-100`) and fuzzy fallback. |
| Brand | 15% | Exact or fuzzy match against Amazon brand **or** manufacturer. |
| Title alignment | 25% | Extracts product type, size, variant, form; fuzzy-matches with penalties for type/size/variant mismatches. |
| Pack count | 15% | Compares catalog pack vs Amazon pack/quantity. Multiple detected (e.g. Amz pack 12 is 4× catalog pack 3) and recorded in `Amz Pack`. |

---

## Project structure

```
catalog-verifier/
├── main.py                        # FastAPI entrypoint; registers all routers, starts DB
├── catalog_verifier.db            # Created on first run — not shipped
├── routers/
│   ├── verify.py                  # /api/verify, /api/verify/ai, attribute cache, blacklist
│   ├── barcode.py                 # /api/barcode/lookup
│   ├── export.py                  # /api/export
│   ├── pairs.py                   # /api/pairs/search, /api/pairs/unlock
│   ├── library.py                 # /api/library CRUD
│   └── settings.py                # /api/settings/thresholds
├── services/
│   ├── confidence.py              # Weighted signal scoring + Not Approved verdict
│   ├── extractor.py               # Rule-based + GPT-4o attribute extraction
│   ├── barcode_lookup.py          # 5-source chain (OFF→OBF→OPF→UPCItemDB→DDG)
│   └── database.py                # SQLite layer (seeded at import)
├── static/
│   ├── index.html                 # SPA with CPG + Pair Manager views
│   ├── app.js                     # Uploads, review flow, tag transitions, pair search
│   ├── styles.css                 # Design tokens on top of Tailwind
│   └── sample/                    # Pre-loaded demo CPG dataset
├── abbreviations.json             # Seed data for the categorised library
├── .env
├── requirements.txt
└── README.md
```

---

## API surface

| Method | Endpoint | Body | Returns |
|-|-|-|-|
| `GET`  | `/api/health`                       | — | `{ status, ai_available }` |
| `POST` | `/api/verify`                       | multipart: `catalog_file`, `amazon_file`, `amazon_source`, `ai_mode` | per-row results |
| `POST` | `/api/verify/ai`                    | `{ title, context }` | `{ extracted }` (auto-learns into library) |
| `GET`  | `/api/attributes/{upc}/{asin}`      | — | `{ cached }` |
| `POST` | `/api/attributes/clear`             | `{ upc, asin }` | `{ cleared }` |
| `POST` | `/api/blacklist`                    | `{ upc, asin, confidence, failed_signals }` | `{ ok }` |
| `POST` | `/api/verified`                     | `{ upc, asin, review_status, data }` | `{ ok }` |
| `POST` | `/api/barcode/lookup`               | `{ upc, brand?, vendor_title? }` | `{ found, source, chain, aligned, ... }` |
| `POST` | `/api/export`                       | `{ results, abbreviations }` | `.xlsx` |
| `POST` | `/api/pairs/search`                 | `{ query }` | `{ results: [...] }` |
| `POST` | `/api/pairs/unlock`                 | `{ upc, asin }` | `{ unlocked }` |
| `GET`  | `/api/library`                      | — | `{ categories, library }` |
| `POST` | `/api/library`                      | `{ category, abbr, full }` | `{ ok, library }` |
| `POST` | `/api/library/delete`               | `{ id }` | `{ deleted, library }` |
| `GET`  | `/api/settings/thresholds`          | — | `{ verified, review }` |
| `POST` | `/api/settings/thresholds`          | `{ verified, review }` | saved thresholds |

---

## Troubleshooting

- **AI Extract button is grey even though it looks armed** — open `.env` and set `OPENAI_API_KEY`, restart the server.
- **Barcode lookups all say "Not Found"** — the public APIs can rate-limit or be offline. Hit **Clear cache** on the row to retry.
- **Pair Manager is empty** — nothing has been blacklisted yet. Rejecting a row from the Approved tab, or discarding a row from the Review tab, puts it there.
- **Want to reset everything** — delete `catalog_verifier.db` and restart. Seeds the library and thresholds again.
