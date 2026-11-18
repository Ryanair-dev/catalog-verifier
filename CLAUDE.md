# CLAUDE.md — Project Intelligence for catalog-verifier

This file is read automatically by Claude Code at the start of every session. It is deliberately **lean and evergreen** — it holds architecture, conventions, and cross-cutting facts that are true regardless of which feature a session is touching. It does **not** hold a dated changelog.

## ⚠️ Before you touch a feature, read its doc. After you finish, update it.

**Before making any change to a specific feature, read its doc in `docs/features/` (or the relevant `docs/infra/` doc for shared services) first.** Each doc's own `## Architecture` section is the actual source of truth for how that feature works today — more current and more detailed than anything that could be summarized here. Its `## Changelog` section is the project's own memory of what's already been tried, fixed, reverted, or flagged as a known tradeoff for that feature; skipping it risks re-discovering (or re-breaking) something already settled.

**Verification marker — prove the doc was actually read.** Every file in `docs/features/` and `docs/infra/` has a unique `<!-- doc-check: <token> -->` comment as its literal first line. These sub-docs are NOT auto-loaded the way this root file is, so the only way to genuinely have a doc's token is to have opened that exact file this session. **The first time in a reply that work touches a given doc, quote its token back** (e.g. "Read `po_analytics.md` [doc-check: &lt;token&gt;] — ...") before describing or making the change. Do not guess, recall from an earlier point in a long conversation without re-reading, or copy a token from anywhere other than that file's own first line — the whole point is that a stale or fabricated token is a visible, checkable signal that the doc was skipped. **Do not list these tokens anywhere in this root file** (or in any other file auto-loaded every session) — doing so would let a future session "verify" itself by quoting this list instead of actually opening the doc, defeating the mechanism. If a doc doesn't yet have a token (a newly-created doc), add one in the same `<!-- doc-check: <feature-name>-<4-hex-chars> -->` format before relying on this check for it.

**After finishing work, add a dated entry to that doc's `## Changelog` section** (newest entry at the top), in the same style as the existing entries there: what the user asked, what was found, what was changed, how it was verified, and whether a restart/rescore/hard-refresh is needed. Copy this project's established convention — these entries are a durable record for future sessions, not a commit message; don't summarize them away later.

**If a session touches multiple features, split the changelog entry across each relevant doc** — write the portion relevant to each feature into that feature's own file, and add a one-line cross-reference ("see also `docs/features/<other>.md` for this same session's X") when one portion depends on a fact documented in another file. Never duplicate the full entry into every touched file.

**Only update this root `CLAUDE.md` itself when the actual architecture, file map, or shared schema changes** — not for routine feature work. A new table that's entirely owned by one feature belongs in that feature's doc (with a one-line pointer added to `docs/infra/database.md`'s table list), not here.

## Feature & infra docs

**Features** (`docs/features/`):

| Doc | Covers |
|---|---|
| [`reports.md`](docs/features/reports.md) | Reports — new top-level dashboard section; PO P&L (per-PO bought/sold/left) is dashboard #1 |
| [`po_analytics.md`](docs/features/po_analytics.md) | PO Analytics — on-demand + day-before-ETA-automated PO profitability reports |
| [`offer_analytics.md`](docs/features/offer_analytics.md) | Offer Analysis (Analytics tab) — SP-API search/match/score pipeline, AI Check, Quick Search; the biggest doc, includes the full scoring-engine/pipeline/API-endpoint architecture |
| [`brand_analytics.md`](docs/features/brand_analytics.md) | Brand Analytics — Keepa+SP-API brand/manufacturer ASIN discovery, AI Fill, category filtering |
| [`seller_analytics.md`](docs/features/seller_analytics.md) | Seller Analytics — analyze a competitor's (or your own) full Amazon seller ASIN catalog in the Offer Analytics template format |
| [`vendor_offers.md`](docs/features/vendor_offers.md) | Vendor Offers / Price Desk — cross-vendor buy-side price comparison + "Offer Analytics export" (NOT the same thing as `offer_analytics.md` — see that doc's own note on the naming collision) |
| [`create_skus.md`](docs/features/create_skus.md) | Create SKUs — SellerCloud SKU generation + bulk-import file wizard |
| [`pair_manager.md`](docs/features/pair_manager.md) | Pair Manager / Pair Library — manually-confirmed identifier→ASIN library, spreadsheet import/export, weekly catalog health-check job |
| [`library.md`](docs/features/library.md) | Library (Abbreviations / Brands / Manufacturers) — vocabulary/entity knowledge, distinct from the Pair Library |
| [`toolbox.md`](docs/features/toolbox.md) | Toolbox — Eligibility/Storage/Generic ASIN Check, Check Brand, MPN Check, Quick Search |
| [`walmart_catalog.md`](docs/features/walmart_catalog.md) | Walmart Catalog — Walmart marketplace Offer Analysis |
| [`verify_scan.md`](docs/features/verify_scan.md) | Verify / Scan — the original (legacy) verify-flow scoring engine + Match-from-Keepa mode |

**Infra** (`docs/infra/`):

| Doc | Covers |
|---|---|
| [`database.md`](docs/infra/database.md) | Full database schema (all tables), the Pair-Manager blacklist/verified-items override mechanism, additive migrations, connection tuning |
| [`azure_sql.md`](docs/infra/azure_sql.md) | Azure SQL — the live SellerCloud catalog mirror read by Create SKUs and PO Analytics |
| [`sp_api.md`](docs/infra/sp_api.md) | Amazon SP-API client — multi-store support, rate limiting, catalog search |
| [`keepa.md`](docs/infra/keepa.md) | Keepa API client — Product Finder discovery + live `/product` pricing/fee data |
| [`job_persistence.md`](docs/infra/job_persistence.md) | Background job restart-survival — **partially documented**; built after this restructure's source material ended |
| [`ui_theme.md`](docs/infra/ui_theme.md) | Shared CSS design tokens/components used by every view — whole-app visual changes that don't belong to one feature |

---

## What this project is

A local FastAPI web app that vets CPG vendor catalog products against Amazon listings. Two workflows:

- **Verification tab** — user uploads a vendor catalog + Keepa export, the confidence engine scores each row, user reviews and exports to Excel. See `docs/features/verify_scan.md`.
  - **Normal mode**: catalog has ASINs pre-assigned; Keepa rows are looked up by ASIN.
  - **Match-from-Keepa mode**: catalog has no ASINs; engine searches the Keepa export to find candidate ASINs using selected methods (UPC, Item ID/MPN, Title). Up to 8 candidates per row; user reviews and approves one per row. One ASIN cannot be approved for two different catalog rows.
- **Analytics tab** ("Offer Analysis") — user uploads a catalog, SP-API searches Amazon for matching ASINs, candidates are scored and presented for review. See `docs/features/offer_analytics.md`.
- **Brand Analytics tab** — user enters a brand or manufacturer name; AI/Keepa discovers sub-brands; a combined Keepa+SP-API search finds all their ASINs; results include BSR, UPC, EAN, GTIN, MPN. Results cached in DB (7-day freshness). AI Fill extracts identifiers from title/description. See `docs/features/brand_analytics.md`.

The app has since grown well beyond these three original tabs — PO Analytics, Vendor Offers/Price Desk, Create SKUs, the Pair Library, a general-purpose Toolbox, Walmart Catalog, and Seller Analytics are all now full features of their own; see the index above.

Target scale: **hundreds of concurrent users** (multi-tenant SaaS). Architecture is currently SQLite + single-process uvicorn. SQLite WAL mode is enabled; Python-level `_LOCK` serialises writes.

---

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI 0.115, uvicorn, Pydantic v2, Python 3.11+ |
| Database | SQLite (WAL mode), single file `catalog_verifier.db` |
| AI | Anthropic `claude-sonnet-4-6` (verdict) + `claude-haiku-4-5-20251001` (extraction); OpenAI `gpt-4o-mini` retained as fallback when `ANTHROPIC_API_KEY` absent |
| Amazon search | SP-API Catalog Items v2022-04-01 via `services/spapi/` (see `docs/infra/sp_api.md`) |
| Product discovery | Keepa API via `services/keepa.py` (see `docs/infra/keepa.md`) |
| SellerCloud catalog | Azure SQL live mirror via `services/azure_sql.py` (see `docs/infra/azure_sql.md`) |
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
│   ├── analytics.py                 Analytics (Offer Analysis) runs CRUD + verdict overrides + export
│   ├── brand_analytics.py           Brand Analytics CRUD + discover + AI fill + export
│   ├── reports.py                   Reports dashboards (PO P&L today; more register here as built)
│   ├── po_analytics.py              PO Analytics on-demand run + automation endpoints
│   ├── vendor_offers.py             Vendor Offers / Price Desk CRUD + exports + DB sync
│   ├── create_po.py                 Create SKUs: upload sheet -> SKUs -> import files
│   ├── pairs.py                     Pair Library + blacklist search/unlock
│   ├── library.py                   Abbreviation library CRUD
│   ├── eligibility.py               Toolbox: ASIN eligibility + Check Brand
│   ├── storage_fees.py              Toolbox: FBA storage fee check
│   ├── generic_check.py             Toolbox: Generic/DOG ASIN check
│   ├── mpn_check.py                 Toolbox: MPN → qty/case lookup
│   ├── health_check.py              Pair Library weekly catalog health-check job
│   ├── walmart.py                   Walmart Catalog Offer Analysis
│   ├── export.py                    Excel export for verify flow (20-column layout)
│   ├── barcode.py                   5-source barcode lookup chain
│   └── settings.py                  Threshold read/write
├── services/
│   ├── confidence.py                Weighted signal scorer + ASIN floor logic (verify flow)
│   ├── keepa_matcher.py             Match-from-Keepa candidate search engine (verify flow)
│   ├── extractor.py                 rule_extract() + ai_extract() + amz_extract()
│   ├── ai_recheck.py                Second-pass verdict suggestions; make_client() used by brand modules
│   ├── barcode_lookup.py            Async 5-source barcode chain
│   ├── database.py                  All SQLite I/O, migrations, caches (see docs/infra/database.md)
│   ├── file_parser.py               Shared Excel/CSV parser (multiple routers use this)
│   ├── create_po.py                 Create SKUs generator + .xlsx exporter
│   ├── sc_reference.py              Create SKUs' local "SellerCloud memory" (brand prefix/manufacturer)
│   ├── brand_map.py                 Create SKUs' legacy Excel-import brand→manufacturer fallback
│   ├── sku_exports.py               Create SKUs export history log
│   ├── azure_sql.py                 Live SellerCloud catalog mirror (see docs/infra/azure_sql.md)
│   ├── keepa.py                     Keepa API client (see docs/infra/keepa.md)
│   ├── monday_eta.py                PO Analytics' Monday.com board mirror reader (also: Reports' purchaser lookup)
│   ├── po_automation.py             PO Analytics' day-before-ETA automation orchestration
│   ├── reports_po_pnl.py            Reports: PO P&L dashboard (queries the Azure PO→sale allocation view)
│   ├── graph_mail.py                PO Analytics' Microsoft Graph sendMail wrapper
│   ├── teams_notify.py              Shared Teams webhook notifier — Offer/Brand Analytics + Vendor Offers export completion pings
│   ├── vendor_offers.py             Vendor Offers core ingest/pairing/ledger logic
│   ├── vendor_offers_db_sync.py     Vendor Offers Azure DB sync (5 known vendors) — superseded by mailbox_sync below, kept as a manual fallback
│   ├── vendor_offers_mailbox_sync.py  Vendor Offers: direct-from-email sync for the same 5 vendors (bypasses FBI/Azure entirely)
│   ├── vendor_offers_template_export.py  Vendor Offers formatted Excel export + enrichment
│   ├── health_check.py              Pair Library weekly health-check job
│   ├── generic_check.py             Toolbox Generic/DOG ASIN classification
│   ├── storage_fees.py              Toolbox FBA storage fee computation + cache
│   ├── restrictions.py              SP-API eligibility/restrictions classification
│   ├── mpn_check.py / mpn_check_gudid.py / mpn_check_sync.py  Toolbox MPN qty/case lookup (AccessGUDID + local mirror)
│   ├── walmart/                     Walmart Marketplace API client + offer analysis
│   ├── analytics/
│   │   ├── runner.py                SP-API search orchestration (background thread); normalize_amazon_item() reused by brand_runner
│   │   ├── matcher.py               Candidate confidence scoring for Offer Analysis runs
│   │   ├── parser.py                Catalog file parsing for Offer Analysis
│   │   ├── ai_check.py              Batch AI verification of Offer Analysis candidates
│   │   ├── eligibility_check.py     Offer Analysis Approval-status + storage-fee enrichment
│   │   ├── brand_discovery.py       AI sub-brand discovery (Claude/GPT-4o-mini)
│   │   ├── brand_runner.py          Brand Analytics Keepa+SP-API background search + AI Fill
│   │   ├── brand_extractor.py       Pre-search AI title field extraction
│   │   ├── product_categorizer.py   Category-distance hard-reject classifier
│   │   └── po_analytics.py          PO Analytics report generation (gather + enrich_and_build)
│   ├── spapi/
│   │   ├── client.py                SP-API HTTP client + MultiStoreCatalogAPI
│   │   ├── auth.py                  LWA token refresh
│   │   ├── catalog.py               SearchCatalogItems wrapper
│   │   ├── config.py                Marketplace + multi-store config
│   │   ├── rate_limiter.py          Token-bucket rate limiter (per-store)
│   │   └── reports.py               Reports API (GET_SALES_AND_TRAFFIC_REPORT)
│   └── sellercloud/                 SellerCloud REST client (Delta API, server fml) — Create PO
│       ├── client.py                Token + Catalog reads + PO reads/writes + custom-export/queued-jobs
│       ├── catalog_index.py         Per-brand index: existence, prefix (mode+conf), collision ladder
│       ├── auth.py                  Bearer token refresh (POST /api/token)
│       └── config.py                Credential loading (SELLERCLOUD_*)
├── static/
│   ├── index.html                   SPA shell
│   ├── app.js                       All UI logic
│   ├── create_po.js                 Create SKUs wizard (self-contained module)
│   ├── create_po.css                Create SKUs wizard styling (scoped .cpo)
│   ├── price_desk.html              Vendor Offers / Price Desk page (served standalone or iframed)
│   └── styles.css
├── abbreviations.json               Seed data for the Library (loaded once on first run)
├── requirements.txt
├── .env                             API keys/credentials — never commit
└── CLAUDE.md                        This file
```

---

## Database

Full schema (every table across every feature), the shared Pair-Manager blacklist/verified-items override mechanism used by both the verify flow and Offer Analysis, the additive-migration list, and connection tuning all live in **`docs/infra/database.md`** — read that before touching schema. The Pair Library's own tables (`pair_library`/`pair_library_ids`, a separate and newer mechanism that takes precedence over blacklist/verified-items) are documented in `docs/features/pair_manager.md`.

---

## Attribute extraction (`services/extractor.py`)

Small, shared utility used by multiple flows — kept here rather than duplicated per-feature.

- **`rule_extract(title, abbreviations)`** — regex-based. Returns `{product_type, size {value, unit, raw}, pack_count, variant {scent, color, flavor, form}, form, normalised_title}`. Uses `SIZE_RE` covering oz, lb, g, kg, ml, l, gal, fl oz.
- **`amz_extract(amz_row, abbreviations)`** — for Keepa/Excel rows. Combines all `_AMZ_TEXT_FIELDS` into a blob, then overrides with dedicated columns (Color, Scent, Flavor, Size). Use this for verify-flow Amazon rows, not for SP-API normalized dicts.
- **`ai_extract(title, abbreviations, extra_context)`** — GPT-4o with JSON schema. Returns same shape plus `expanded_title` and `new_abbreviations`.

The abbreviation library (see `docs/features/library.md`) is loaded via `database.flat_library()` (5-min in-memory cache). Always pass the library to `rule_extract` / `amz_extract` so vendor-specific abbreviations are expanded before attribute detection.

---

## Performance optimisations in place

- **SQLite WAL mode** — concurrent reads don't block writes.
- **PRAGMA tuning**: `synchronous=NORMAL` (safe with WAL, faster fsync), `cache_size=-32768` (32 MB page cache), `temp_store=MEMORY` (temp tables in RAM). Applied in `_connect()`.
- **Indexes**: `scan_catalog_rows(scan_id)`, `scan_amazon_rows(scan_id)`, `scan_results(upc, asin)`, `analytics_catalog_rows(run_id)`, `analytics_candidates(run_id, verdict)`, `analytics_candidates(sales_rank)`, `brand_analytics_items(asin)`.
- **Library cache** — `flat_library()` is cached in-memory with 5-min TTL; invalidated on any write.
- **Batch blacklist** — `load_blacklist_set()` loads all pairs once; verify loop uses O(1) set lookup.
- **Parallelised AI extraction** — when `ai_mode=True`, all per-row GPT-4o calls run in `ThreadPoolExecutor(max_workers=6)`.
- **GZip middleware** — responses ≥ 1 KB are compressed.
- **Shared file parser** — `services/file_parser.py` is the single source of truth for Excel/CSV ingestion.
- **Single-pass catalog parsing in rescore** — `_rescore_pipeline` builds `catalog_map`, `title_by_row`, and `brand_by_row` in one loop, not one loop per field.
- **Single connection per write** — several hot paths (brand_runner's `_upsert_item`, AI Fill progress updates) were fixed to do their read+write in one locked connection instead of two.
- **Job store TTL eviction** — `eligibility.py` and `storage_fees.py` evict completed/error jobs older than 1 hour on every `_get_job()` call.
- **Shared caches, not per-call pulls** — Azure SQL (`docs/infra/azure_sql.md`), the MPN-check local mirror, and the FBA storage-fee cache (`storage_fee_cache`, see `docs/features/toolbox.md`) all exist specifically to avoid repeating slow live Azure/SP-API pulls on every request.

---

## Startup scheduler staggering (`main.py` lifespan)

`main.py`'s lifespan starts several in-process background schedulers (`azure_sql`, `vendor_offers_mailbox_sync`, `mpn_check_sync`, `services.sellercloud.catalog_index`, `po_automation`), each of which runs its **first loop iteration immediately** on startup (not after its own interval) so the app is warm right away. **These first runs are individually expensive** — `azure_sql`'s own code comment documents a "~165s pull inline" on a cold cache, and `vendor_offers_mailbox_sync`'s first run against a real day's vendor emails measured ~100–116s.

Starting all of them at once caused a real incident (2026-10-26): the server became unresponsive for 10+ minutes on every restart (CPU climbing into the hundreds of seconds with no recovery), reproduced with a single, clean process running nothing but the stock startup sequence — confirmed independent of any same-day feature code. Fix: each scheduler's very first start is staggered via `threading.Timer` (`_delayed_start(seconds, fn)` in `main.py`) — `azure_sql` at 0s, `vendor_offers_mailbox_sync` at +210s, `mpn_check_sync` at +360s, `services.sellercloud.catalog_index` at +450s, `po_automation` at +540s — so only one scheduler is doing its heavy first-run work at any given moment. `health_check` is exempt (it only fires Friday 19:00 America/New_York, never at startup).

**If you add a new "fires immediately on startup" scheduler, give it its own staggered slot** (past the existing ones, or interleaved if you've profiled its actual first-run cost) rather than calling `start_scheduler()` directly in the lifespan — the whole point of this section is that direct, unstaggered calls are exactly what caused the incident.

---

## Known scaling limitations (SQLite single-writer)

For true multi-hundred-user concurrency, the current bottleneck is SQLite's single-writer lock. Future migration path:
1. Replace `_LOCK + sqlite3` with SQLAlchemy async + PostgreSQL.
2. Add Redis for library/threshold caching.
3. Run multiple uvicorn workers behind a reverse proxy (nginx/caddy).

A from-scratch opinionated Azure SQL (T-SQL) target schema already exists as a design artifact (`azure_sql_schema.sql` at the project root) — see `docs/infra/database.md`'s changelog for what it would require to actually migrate to it.

---

## Unused / legacy code to be aware of

- **`routers/verify.py` `POST /api/verify`** — legacy single-shot flow. Do not remove until the UI is fully migrated to the scan-based flow (`docs/features/verify_scan.md`).
- **`keepa_imports` / `amazon_imports` tables** — global cross-scan ASIN cache. Still populated but not actively queried by the current scan flow.
- **`attribute_cache` table** — keyed by (UPC, ASIN). Used by the verify flow. Offer Analysis uses a different approach: attributes are embedded in `analytics_candidates.data_json.scores`.

---

## Conventions

- All route handlers are `async def`. Blocking work (AI calls, heavy computation) must be offloaded to `asyncio.to_thread()` or `ThreadPoolExecutor` — never block the event loop inline.
- DB reads don't need `_LOCK` (WAL handles concurrent reads). Only writes use `with _LOCK, _connect()`.
- The `UPC` field from Excel comes in as `int`. Always coerce: `str(row.get("UPC") or "").strip()`.
- Export layout for the legacy verify flow is 20 fixed columns — see `routers/export.py` `OUTPUT_COLUMNS`. Column order matters.
- `app.js` uses the `api()` helper for all fetch calls. Retries once on network error; throws `Error(detail)` on non-2xx.
- Background threads (Offer Analysis runs, Brand Analytics runs, PO Analytics jobs, Vendor Offers exports) are daemon threads — they die with the uvicorn process unless the job-persistence system (`docs/infra/job_persistence.md`) covers that job type. Long-running Offer Analysis runs survive server restarts via `resume_run()`.
- **Settings (`#settings-panel`) has tabs, not a single flat panel** — as of 2026-10-27 (see `docs/infra/job_persistence.md`'s changelog) it has "Thresholds" (the original content) and "Tasks Running" (a live aggregation of every background job/run/scheduler app-wide), using the same `.lib-type-tabs.tools-tab-bar` pattern as the Toolbox's `#tools-panel` sub-tabs. A new feature's own background job should add a small `list_jobs_for_tasks_panel()` accessor to its router (mirroring the 7 existing ones) so it shows up there automatically, rather than building separate one-off status UI.
- This project runs destructive/irreversible actions (live emails, real writes to shared Azure data, bulk SellerCloud updates) only after explicit user confirmation, and prefers verifying against a disposable scratch copy of the live database over experimenting on the live file directly — this pattern recurs throughout every feature doc's changelog and should be followed for new work too.
- When investigating a bug, confirm the real root cause with live evidence (read the actual API response, query the actual live data) before writing a fix — multiple changelog entries across every feature doc document a wrong first guess being corrected this way. Don't guess when you can check.

---

## Environment variables

| Variable | Required | Used by |
|---|---|---|
| `ANTHROPIC_API_KEY` | Optional | AI check/extraction/brand-discovery across most features — **preferred** over OpenAI |
| `OPENAI_API_KEY` | Optional | Fallback AI provider when `ANTHROPIC_API_KEY` is absent |
| `KEEPA_API` | Optional | `services/keepa.py` — see `docs/infra/keepa.md` |
| SP-API credentials (`AMZ_CLIENT_ID`/`AMZ_CLIENT_SECRET`/refresh token/seller ID, several naming variants accepted) | Optional | `services/spapi/` — see `docs/infra/sp_api.md` for the multi-store naming scheme |
| `AZURE_CLIENT_SECRET` + related Azure AD app credentials | Optional | `services/azure_sql.py` — see `docs/infra/azure_sql.md`. **Never echo this value.** |
| `SELLERCLOUD_USERNAME` / `SELLERCLOUD_PASSWORD` / `SELLERCLOUD_API_BASE` | Optional | `services/sellercloud/config.py` — Create SKUs (SellerCloud REST API) |
| `WALMART_CLIENT_ID` / `WALMART_CLIENT_SECRET` | Optional | `services/walmart/config.py` — see `docs/features/walmart_catalog.md` |
| `TEAMS_WEBHOOK_URL` | Optional | `services/teams_notify.py` — Offer/Brand Analytics + Vendor Offers export completion pings to a Teams group chat |
| `FBI_API_KEY` | Optional | Ford Medical's internal FBI REST API (investigated, not used as the primary path — see `docs/features/vendor_offers.md`'s 2026-09-23 entry) |

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
