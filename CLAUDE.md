# CLAUDE.md — Project Intelligence for catalog-verifier

_Last updated: 2026-05-05_

This file is read automatically by Claude Code at the start of every session.
**Update this file at the end of every task** — it is the single source of truth for session continuity.

---

## What this project is

A local FastAPI web app that vets CPG vendor catalog products against Amazon listings. Two workflows:

- **Verification tab** — user uploads a vendor catalog + Keepa export, the confidence engine scores each row, user reviews and exports to Excel.
- **Analytics tab** — user uploads a catalog, SP-API searches Amazon for matching ASINs, candidates are scored and presented for review.

Target scale: **hundreds of concurrent users** (multi-tenant SaaS). Architecture is currently SQLite + single-process uvicorn. SQLite WAL mode is enabled; Python-level `_LOCK` serialises writes.

---

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI 0.115, uvicorn, Pydantic v2, Python 3.11+ |
| Database | SQLite (WAL mode), single file `catalog_verifier.db` |
| AI | OpenAI `gpt-4o` (extraction) + `gpt-4o-mini` (re-check + title cleaning) |
| Amazon search | SP-API Catalog Items v2022-04-01 via `services/spapi/` |
| Frontend | Vanilla JS SPA (`static/app.js`) — no build step |
| Excel I/O | openpyxl |
| Fuzzy matching | rapidfuzz |

---

## File map

```
catalog-verifier/
├── main.py                          FastAPI app, middleware, router registration
├── routers/
│   ├── scans.py                     Scan lifecycle + verify engine + AI re-check
│   ├── verify.py                    Legacy single-shot verify (kept for compat)
│   ├── analytics.py                 Analytics runs CRUD + verdict overrides + export
│   ├── export.py                    Excel export for verify flow (20-column layout)
│   ├── barcode.py                   5-source barcode lookup chain
│   ├── pairs.py                     Blacklist search + unlock
│   ├── library.py                   Abbreviation library CRUD
│   └── settings.py                  Threshold read/write
├── services/
│   ├── confidence.py                Weighted signal scorer + ASIN floor logic (verify flow)
│   ├── extractor.py                 rule_extract() + ai_extract() + amz_extract()
│   ├── ai_recheck.py                GPT-4o-mini second-pass verdict suggestions
│   ├── barcode_lookup.py            Async 5-source barcode chain
│   ├── database.py                  All SQLite I/O, migrations, caches
│   ├── file_parser.py               Shared Excel/CSV parser (3 routers use this)
│   ├── analytics/
│   │   ├── runner.py                SP-API search orchestration (background thread)
│   │   ├── matcher.py               Candidate confidence scoring for analytics runs
│   │   ├── parser.py                Catalog file parsing for analytics
│   │   └── ai_check.py              GPT-4o-mini batch AI verification of analytics candidates
│   └── spapi/
│       ├── client.py                SP-API HTTP client
│       ├── auth.py                  LWA token refresh
│       ├── catalog.py               SearchCatalogItems wrapper
│       ├── config.py                Marketplace config
│       └── rate_limiter.py          Token-bucket rate limiter
├── static/
│   ├── index.html                   SPA shell
│   ├── app.js                       All UI logic (~3600 lines)
│   └── styles.css
├── abbreviations.json               Seed data for library (loaded once on first run)
├── requirements.txt
├── .env                             OPENAI_API_KEY, SP_API_* — never commit
└── CLAUDE.md                        This file
```

---

## Analytics tab — scoring engine (`services/analytics/matcher.py`)

This is a **separate scorer** from the verify-flow `services/confidence.py`. Do not confuse the two.

### Signal weights (analytics only)

| Signal | Max pts | Notes |
|---|---|---|
| Brand | 40 | Exact substring in Amazon title/brand/desc = full 40; fuzzy ≥70% = partial |
| Title similarity | 50 | `token_set_ratio` best-of-4 against Amazon title |
| MPN | 10 | Dash-normalised exact ratio |
| **Total** | **100** | Capped at 100 |

UPC/EAN exact match → adds **50-point bonus** on top of normal brand+title score (not auto-100). UPC + brand match = 90 → verified. UPC alone = ~50 → review. Wrong-product UPC hit = ~50 → review.

### Verdict thresholds (analytics)

| Score | Verdict |
|---|---|
| ≥ 90 | `verified` |
| ≥ 35 | `review` |
| < 35 | `not_approved` |

### Hard-reject caps (override thresholds)

These cap `confidence_score` at **29.0**, which falls below the review floor → forces `not_approved`:

| Condition | Trigger |
|---|---|
| **Size mismatch** | Both sides have a parseable size (volume OR weight) and differ by >10%. Checks vendor title vs Amazon title + `attributes.size` SP-API field. Handles oz/fl oz/ml/l/gal (volume) and lb/kg (weight). |
| **Gender mismatch** | BOTH titles carry an explicit gender marker (men/women/male/female/etc.) that contradict each other. If only one side specifies gender → no penalty. |
| **Color mismatch** | Both sides specify a colour from `_COLOR_WORDS` frozenset and they don't overlap. Amazon source priority: structured `attributes.color` → title → description + bullet points. |

`_COLOR_WORDS` is narrow by design — only unambiguous colors. Excluded: cream, olive, tan, lime, coral, amber, peach, ivory, gold, silver, beige (these are product-type or scent words in CPG).

### Soft-reject cap (pack mismatch)

When the Amazon listing covers multiple vendor units (e.g. vendor sells singles, Amazon lists "Pack of 6"), cap at **80.0** → `review`. The user can decide if the multi-pack price works.

UPC-confirmed matches with a pack mismatch → 80 (not 100), landing in Review.

### Hard-reject override precedence

```python
hard_reject = size_mismatch OR gender_mismatch OR color_mismatch

if hard_reject:      verdict = "not_approved"   # overrides pack_mismatch
elif conf >= 90:       verdict = "verified"
elif conf >= 35 OR pack_mismatch:  verdict = "review"
else:                  verdict = "not_approved"
```

---

## Analytics tab — pipeline (`services/analytics/runner.py`)

### UPC normalisation

Vendor catalogs often export UPC-A without the leading zero (11 digits instead of 12). `services/analytics/parser.py` zero-pads any 11-digit all-numeric UPC at parse time so the full 12-digit form is used everywhere: SP-API search, confidence scoring, and DB storage. This is done in the parser (not the runner) so the fix applies to both new runs and rescores.

### Brand extraction phase (pre-search)

Before any SP-API search, `services/analytics/brand_extractor.py` runs GPT-4o-mini on every vendor title to extract structured fields:
- `brand` — cleaner than raw catalog brand column
- `product_type` — product category (e.g. "deodorant", "dish soap")
- `model` — specific variant/model name
- `size` — size string
- `pack_info` — pack count or null

Stored in `analytics_catalog_rows.extracted_json` (additive migration). Used by:
- **Tier 3**: builds `"{brand} {product_type} {model}"` queries instead of raw title → better Amazon search relevance
- **Matcher**: uses `extracted.brand` as primary brand signal (overrides raw catalog text)
- **Matcher**: injects `product_type` + `model` as `additional_keywords` for scoring boost
- **AI Check**: includes `extracted_brand` and `product_type` in prompt context
- **Rescore**: loads stored extracted fields — no re-extraction needed

Only runs when `OPENAI_API_KEY` is present. Skipped gracefully if missing. Runs in parallel (4 workers, 20 titles per GPT-4o-mini call).

### 3-tier SP-API search

1. **Tier 1 — UPC batch**: `search_by_identifiers` up to 20 UPCs per call.
2. **Tier 2 — Item ID keyword**: one `search_by_keywords` call per unique Item ID.
3. **Tier 3 — Title keyword**: paginated `search_by_keywords` per unique title. Uses extracted brand+product_type when available; falls back to raw title.

### Max BSR (Best Seller Rank) cap

- Stored on `analytics_runs.max_rank INTEGER DEFAULT 0`.
- When `max_rank > 0`, any candidate whose `sales_rank > max_rank` is forced to `not_approved` **after** the confidence verdict is computed.
- NULL/unknown rank is never penalised.
- Set at run creation (wizard step 4) or overridden on rescore.
- Flows through: `start_analytics_run` → `_create_run` → `_run_pipeline` → `_upsert_candidate`.
- Rescore: `start_rescore(run_id, title_col, brand_col, max_rank)` persists the new max_rank to the run row before launching the thread.

### Sales rank extraction

`_extract_sales_rank(raw)` parses SP-API `salesRanks[].classificationRanks` and `displayGroupRanks`. Returns `(best_rank, best_category, all_ranks)`. `normalize_amazon_item()` populates `sales_rank`, `sales_rank_category`, `sales_ranks` on the normalized dict. Persisted in `analytics_candidates.sales_rank`.

### Rescore pipeline

`start_rescore(run_id, title_col, brand_col, max_rank)` kicks off `_rescore_pipeline` in a daemon thread. Re-scores every candidate using the chosen title column from raw catalog data. All verdict logic (hard rejects + max_rank cap) is re-applied identically to the initial run.

During rescore, `sales_rank` is re-extracted from `amazon_data["_raw"]` (the stored SP-API payload) when `amazon_data["sales_rank"]` is missing — this backfills rank for runs created before rank extraction was added. The `analytics_candidates.sales_rank` DB column is also updated in the rescore batch write.

---

## Analytics tab — API endpoints (`routers/analytics.py`)

| Method | Path | Notes |
|---|---|---|
| POST | `/api/analytics/preview` | Returns first 25 raw rows for wizard header-row picker |
| GET | `/api/analytics/status` | SP-API credentials probe |
| POST | `/api/analytics/runs` | Create run; form fields include `max_rank` |
| GET | `/api/analytics/runs` | List all runs (includes `max_rank` column) |
| GET | `/api/analytics/runs/{id}` | Run detail + paginated candidates. Params: `limit`, `offset`, `verdict`, `max_rank` (display filter) |
| POST | `/api/analytics/runs/{id}/candidates/verdict` | Single verdict override |
| POST | `/api/analytics/runs/{id}/candidates/bulk_verdict` | Bulk verdict override |
| POST | `/api/analytics/runs/{id}/rescore` | Body: `title_col`, `brand_col`, `max_rank` |
| POST | `/api/analytics/runs/{id}/control` | `pause` / `stop` / `resume` |
| DELETE | `/api/analytics/runs/{id}` | Stop + delete run |
| GET | `/api/analytics/runs/{id}/export` | Four-sheet Excel download (Approved / Review / Not Approved / Not Found). Query params: `min_rank`, `max_rank`, `skip_null_rank` mirror the toolbar BSR filter. |
| GET | `/api/analytics/runs/{id}/ai_check/estimate` | Returns `{candidate_count, model, cost_usd_est, duration_ms_est, already_running}`. Query param: `verdict` (default `review`). |
| POST | `/api/analytics/runs/{id}/ai_check` | Body: `{verdict}`. Starts background GPT-4o-mini AI check. Progress tracked via `ai_check_status/done/total` on `analytics_runs`. |

Candidate ORDER BY: `confidence DESC, row_idx, asin` — all tabs default to highest confidence first.

---

## Analytics tab — UI (analytics section of `static/app.js` + `static/index.html`)

- **All tabs (Approved / Review / Not Approved)** default sort: confidence DESC.
- **BSR column** in candidates table. Shown as formatted integer or "—".
- **Amazon Title** shown in "Review Before Export" export modal.
- **Min/Max BSR fields** in wizard step 4 (`#awiz-min-rank`, `#awiz-max-rank`) — applied at run creation. Min BSR: rank below this (too popular) → Not Approved. Max BSR: rank above this (too slow-moving) → Not Approved.
- **Min/Max BSR fields** in rescore modal (`#analytics-rescore-min-rank`, `#analytics-rescore-max-rank`) — pre-populated from run's stored values; changing them updates the run on confirm.
- **BSR display filter** in the results toolbar: `#analytics-run-rank-min` (Min), `#analytics-run-rank-max` (Max), `#analytics-run-rank-skip-null` (hide unranked checkbox). Client-side only — filters the already-loaded page of candidates without a server round-trip. Reset when opening a new run.
- **AI Check button** (`#analytics-run-ai-check`) — visible when run is complete/paused/stopped. Opens `#analytics-ai-check-modal` where user picks verdict filter (review-only or all), sees cost estimate, then starts GPT-4o-mini batch check. Button shows live progress ("Checking 45/200…") while running; polling continues via `fetchAnalyticsRunDetail`. On completion, AI verdict badges appear inline in the Verdict column: `✓ AI` (approve, green), `✗ AI` (reject, red), `? AI` (uncertain, grey), with reasoning in a tooltip.
- **AI verdict fields** on `analytics_candidates`: `ai_verdict` (approve/reject/uncertain), `ai_reasoning` (≤300 chars). Not shown if null. Does not change the scored verdict.

---

## Verify flow — scoring engine (`services/confidence.py`)

Different scorer from analytics. Do not modify analytics matcher for verify-flow bugs and vice versa.

### Signal weights (verify flow)

```
UPC/EAN     55 pts   exact match only
Item ID     10 pts   fuzzy + suffix variant allowed
Brand        5 pts   exact or fuzzy vs Amazon Brand/Manufacturer
Title       25 pts   token_set_ratio across all Amazon text fields + attribute bonus/penalty
Pack         5 pts   catalog pack vs Amazon "Unit Details: Unit Value"
```
Total possible: 100.

### ASIN floor (verify flow only)

When the catalog row has an ASIN **and** title similarity ≥ 40%, confidence is floored at the Approved threshold — regardless of UPC match. This prevents pre-researched ASINs from being downgraded just because the UPC changed.

---

## Database schema (15 tables)

| Table | Purpose |
|---|---|
| `scans` | One row per scan, lifecycle state machine |
| `scan_catalog_rows` | Parsed catalog rows per scan |
| `scan_amazon_rows` | Keepa/Amazon rows per scan, keyed by ASIN |
| `scan_results` | Per-row verdict + full JSON signals |
| `analytics_runs` | Analytics run metadata, incl. `max_rank`, `min_rank`, `ai_check_status`, `ai_check_done`, `ai_check_total` |
| `analytics_catalog_rows` | Source rows per analytics run |
| `analytics_candidates` | SP-API candidate matches with verdicts; incl. `sales_rank`, `ai_verdict`, `ai_reasoning` |
| `keepa_imports` | Global Keepa cache keyed by ASIN (legacy cross-scan pool) |
| `amazon_imports` | Global Amazon export cache keyed by ASIN |
| `blacklisted_pairs` | Rejected UPC/ASIN pairs |
| `verified_items` | Historical approved rows (legacy) |
| `attribute_cache` | Normalised attributes per (UPC, ASIN) — verify flow |
| `abbreviation_library` | Categorised abbreviation/full-form entries |
| `settings` | threshold_verified (default 85), threshold_review (default 35) |

### Additive migrations in `database.py` `init_db()`

Run on every startup; safe to re-run. Current migrations:
- `abbreviation_library.added_by` — TEXT DEFAULT 'system'
- `analytics_candidates.sales_rank` — INTEGER
- `analytics_runs.max_rank` — INTEGER DEFAULT 0
- `analytics_runs.min_rank` — INTEGER DEFAULT 0
- `analytics_candidates.ai_verdict` — TEXT
- `analytics_candidates.ai_reasoning` — TEXT
- `analytics_runs.ai_check_status` — TEXT
- `analytics_runs.ai_check_done` — INTEGER DEFAULT 0
- `analytics_runs.ai_check_total` — INTEGER DEFAULT 0

---

## Attribute extraction (`services/extractor.py`)

Two functions called from multiple places:

- **`rule_extract(title, abbreviations)`** — regex-based. Returns `{product_type, size {value, unit, raw}, pack_count, variant {scent, color, flavor, form}, form, normalised_title}`. Uses `SIZE_RE` covering oz, lb, g, kg, ml, l, gal, fl oz.
- **`amz_extract(amz_row, abbreviations)`** — for Keepa/Excel rows. Combines all `_AMZ_TEXT_FIELDS` into a blob, then overrides with dedicated columns (Color, Scent, Flavor, Size). Use this for verify-flow Amazon rows, not for SP-API normalized dicts.
- **`ai_extract(title, abbreviations, extra_context)`** — GPT-4o with JSON schema. Returns same shape plus `expanded_title` and `new_abbreviations`.

The abbreviation library is loaded via `database.flat_library()` (5-min in-memory cache). Always pass the library to `rule_extract` / `amz_extract` so vendor-specific abbreviations are expanded before attribute detection.

---

## Performance optimisations in place

- **SQLite WAL mode** — concurrent reads don't block writes.
- **Missing indexes added** (2026-04-27): `scan_catalog_rows(scan_id)`, `scan_amazon_rows(scan_id)`, `scan_results(upc, asin)`, `analytics_catalog_rows(run_id)`.
- **Library cache** — `flat_library()` is cached in-memory with 5-min TTL; invalidated on any write.
- **Batch blacklist** — `load_blacklist_set()` loads all pairs once; verify loop uses O(1) set lookup.
- **Parallelised AI extraction** — when `ai_mode=True`, all per-row GPT-4o calls run in `ThreadPoolExecutor(max_workers=6)`.
- **GZip middleware** — responses ≥ 1 KB are compressed.
- **Shared file parser** — `services/file_parser.py` is the single source of truth for Excel/CSV ingestion.

---

## Known scaling limitations (SQLite single-writer)

For true multi-hundred-user concurrency, the current bottleneck is SQLite's single-writer lock. Future migration path:
1. Replace `_LOCK + sqlite3` with SQLAlchemy async + PostgreSQL.
2. Add Redis for library/threshold caching.
3. Run multiple uvicorn workers behind a reverse proxy (nginx/caddy).

---

## Unused / legacy code to be aware of

- **`routers/verify.py` `POST /api/verify`** — legacy single-shot flow. Do not remove until UI is fully migrated.
- **`keepa_imports` / `amazon_imports` tables** — global cross-scan ASIN cache. Still populated but not actively queried by v3 scan flow.
- **`attribute_cache` table** — keyed by (UPC, ASIN). Used by verify flow. Analytics uses a different approach: attributes are embedded in `analytics_candidates.data_json.scores`.

---

## Conventions

- All route handlers are `async def`. Blocking work (OpenAI, heavy computation) must be offloaded to `asyncio.to_thread()` or `ThreadPoolExecutor` — never block the event loop inline.
- DB reads don't need `_LOCK` (WAL handles concurrent reads). Only writes use `with _LOCK, _connect()`.
- The `UPC` field from Excel comes in as `int`. Always coerce: `str(row.get("UPC") or "").strip()`.
- Export layout is 20 fixed columns — see `routers/export.py` `OUTPUT_COLUMNS`. Column order matters.
- `app.js` uses the `api()` helper for all fetch calls. Retries once on network error; throws `Error(detail)` on non-2xx.
- Analytics background threads are daemon threads — they die with the uvicorn process. Long-running runs survive server restarts via `resume_run()`.

---

## Environment variables

| Variable | Required | Used by |
|---|---|---|
| `OPENAI_API_KEY` | Optional | `services/extractor.py` (ai_extract), `services/ai_recheck.py`, `runner.py` (title cleaning) |
| `SP_API_REFRESH_TOKEN` | Optional | `services/spapi/auth.py` |
| `SP_API_CLIENT_ID` | Optional | `services/spapi/auth.py` |
| `SP_API_CLIENT_SECRET` | Optional | `services/spapi/auth.py` |
| `SP_API_MARKETPLACE_ID` | Optional | `services/spapi/config.py` (default: ATVPDKIKX0DER / US) |

---

## Running locally

```bash
python -m venv .venv && source .venv/Scripts/activate
pip install -r requirements.txt
python main.py          # → http://127.0.0.1:8000
```

For production (multiple workers):
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```
SQLite WAL supports multiple readers; writes serialise through `_LOCK`. With 4 workers, write-heavy operations queue — acceptable for moderate load.
