# Database (`services/database.py`)

## What this does

All SQLite I/O for the whole app lives in `services/database.py` — a single file `catalog_verifier.db`, WAL mode, a Python-level `_LOCK` serialising writes (reads don't need it — WAL handles concurrent reads). Every feature's own tables are listed below; this file is the single source of truth for schema across the project.

## Architecture

### Database schema (tables)

| Table | Purpose |
|---|---|
| `scans` | One row per scan, lifecycle state machine |
| `scan_catalog_rows` | Parsed catalog rows per scan |
| `scan_amazon_rows` | Keepa/Amazon rows per scan, keyed by ASIN |
| `scan_results` | Per-row verdict + full JSON signals |
| `analytics_runs` | Analytics run metadata, incl. `max_rank`, `min_rank`, `vetting_mode` (TEXT DEFAULT 'cpg'), `ai_check_status`, `ai_check_done`, `ai_check_total`, `duplicate_rows_removed` (INTEGER DEFAULT 0 — duplicate catalog rows stripped at run creation), `ai_decisions_applied` (INTEGER DEFAULT 0 — set to 1 when Apply AI Decisions is clicked; reset to 0 when a new AI check is started) |
| `scan_candidates` | Per-candidate matches for match-from-Keepa scans. PK: `id`. Unique: `(scan_id, row_idx, asin)`. Columns: `confidence`, `verdict`, `review_status`, `match_method`, `data_json`. |
| `analytics_catalog_rows` | Source rows per analytics run |
| `analytics_candidates` | SP-API candidate matches with verdicts; incl. `sales_rank`, `ai_verdict`, `ai_reasoning` |
| `keepa_imports` | Global Keepa cache keyed by ASIN (legacy cross-scan pool) |
| `amazon_imports` | Global Amazon export cache keyed by ASIN |
| `blacklisted_pairs` | Rejected UPC/ASIN pairs — written by verify UI buttons AND analytics verdict endpoint. Loaded at run/rescore/verify start to auto-reject pairs. |
| `verified_items` | Approved UPC/ASIN pairs — written by verify UI buttons AND analytics verdict endpoint. Loaded at run/rescore/verify start to auto-approve pairs (confidence floor applies). |
| `global_asin_cache` | SP-API normalized data for every ASIN ever fetched (no `_raw`). Written by `_upsert_candidate` on every analytics run. Used to avoid redundant SP-API calls in future. |
| `attribute_cache` | Normalised attributes per (UPC, ASIN) — verify flow |
| `abbreviation_library` | Categorised abbreviation/full-form entries |
| `settings` | threshold_verified (default 85), threshold_review (default 35) |
| `brand_library` | Brand/manufacturer entity store: sub-brands, aliases, discovered_by (ai/user). UNIQUE on name. |
| `brand_analytics_runs` | One run per brand search. Tracks status, progress, BSR limits, pages_per_brand, ai_fill_status/done/total, last_asin_updated_at. |
| `brand_analytics_items` | One row per (run_id, asin). Stores BSR, UPC/EAN/GTIN/MPN (from SP-API or global overrides) and ai_mpn/ai_upc/ai_ean/ai_gtin (from AI Fill). UNIQUE(run_id, asin). |
| `asin_identifier_overrides` | Global per-ASIN user corrections for mpn/upc/ean/gtin. PRIMARY KEY on asin. Written by PATCH endpoint; read by `_upsert_item` in brand_runner so every future brand run for this ASIN uses the corrected values. |
| `pair_library` / `pair_library_ids` | Pair Manager's manually-confirmed identifier→ASIN library. See `docs/features/pair_manager.md`. |
| `po_eta_automation` / `po_automation_watchlist` / `automation_recipients` | PO Analytics' day-before-ETA automation. See `docs/features/po_analytics.md`. |
| `vo_vendors` / `vo_products` / `vo_asin_listings` / `vo_vendor_offers` | Vendor Offers / Price Desk. See `docs/features/vendor_offers.md`. |
| `sku_exports` | Create SKUs export log (incl. `files_blob` for re-export). See `docs/features/create_skus.md`. |
| `background_jobs` | Shared "survive a restart" shadow table for in-memory background jobs. See `docs/infra/job_persistence.md`. |
| `storage_fee_cache` | 60-day TTL cache of computed FBA storage fees per ASIN. See `docs/features/toolbox.md`. |
| `health_check_runs` / `health_check_results` | Weekly catalog health-check job (Eligibility + DOG over the Pair Library). See `docs/features/pair_manager.md`. |
| `brand_mappings` | Legacy Excel-import brand→manufacturer/purchaser/sourcer fallback for Create SKUs. See `docs/features/create_skus.md`. |

### Pair manager integration

Both the verify flow and the analytics flow share a global `blacklisted_pairs` + `verified_items` pair store:

**Verify flow** (`routers/scans.py`):
- `blacklisted_pairs` loaded at scan start. If `(upc, asin)` is blacklisted → verdict forced to "Not Approved", `review_status="Pair Blacklisted"`.
- `verified_items` loaded at scan start. If `(upc, asin)` is verified AND `confidence >= 40` AND verdict wasn't already "Not Approved" → verdict forced to "Approved", `review_status="Pair Verified"`.
- Approve/Promote UI buttons → write to `verified_items`. Discard/Reject UI buttons → write to `blacklisted_pairs`. (Handled by existing `/api/verified` + `/api/blacklist` endpoints.)
- Export: saves only manually handled pairs. Approved rows saved only when `review_status` is one of `{"Manually Approved", "Reviewed", "Pair Verified", "ai-accepted"}`. Not Approved rows saved only when `review_status` is `"Manually Rejected"` or `"Pair Blacklisted"`. Auto-scored rows (empty `review_status`) are never persisted.

**Analytics flow** (`services/analytics/runner.py` + `routers/analytics.py`):
- `blacklisted_pairs` + `verified_items` loaded before the vetting loop and before rescore. Same override logic as verify flow (blacklist → not_approved; verified + no hard_reject + conf ≥ 35 → verified).
- Analytics verdict override endpoint: any manual verdict change to `not_approved` → writes to `blacklisted_pairs`; any change to `verified` → writes to `verified_items`. UPC looked up from `analytics_catalog_rows`.
- All fetched ASIN normalized data saved to `global_asin_cache` via `_upsert_candidate`.

For the Pair Library's OWN tables (`pair_library`/`pair_library_ids`, separate from blacklist/verified_items) and how it takes precedence over them at read time, see `docs/features/pair_manager.md`.

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
- `analytics_runs.vetting_mode` — TEXT DEFAULT 'cpg'
- `analytics_runs.brand_col` — TEXT DEFAULT '' (column name or literal text value used as brand at wizard/rescore time)
- `analytics_runs.brand_mode` — TEXT DEFAULT 'col' ('col' = column lookup, 'text' = literal override)
- `scans.match_from_keepa` — INTEGER DEFAULT 0
- `scans.match_methods` — TEXT DEFAULT NULL (JSON array e.g. `["upc","item_id","title"]`)
- `global_asin_cache` — CREATE TABLE IF NOT EXISTS (asin PK, data_json, updated_at)
- `analytics_runs.ai_decisions_applied` — INTEGER DEFAULT 0

(Feature-specific additive migrations — e.g. `pair_library`'s `amz_pack` column, `po_eta_automation`'s `vendor`/`goods`/`amount` columns, `sku_exports.files_blob` — are documented in their own feature docs, not duplicated here.)

### Connection tuning (`_connect()`)

- `synchronous=NORMAL` (safe with WAL, faster fsync)
- `cache_size=-32768` (32 MB page cache)
- `temp_store=MEMORY` (temp tables in RAM)
- `journal_mode=WAL`
- `foreign_keys=ON`

A fresh `sqlite3.connect()` + these 5 PRAGMA statements happen on every `_connect()` call — cheap for one-off calls, but a real cost multiplier if called in a tight per-row loop (see `docs/features/pair_manager.md`'s changelog for a real case where this caused an HTTP 524 on a large import, fixed by batching many rows under one connection).

## Changelog

**Changes this session (2026-06-18) — new authoritative Azure SQL schema file `azure_sql_schema.sql` (design artifact, not yet migrated to):**
- Full audit of the LIVE `catalog_verifier.db` (22 tables — an earlier `SCHEMA.md` was stale, missing `pair_library`/`pair_library_ids` and the newer `analytics_runs` columns) and a from-scratch Azure SQL (T-SQL) target schema, created at the project root. Distinct from an older `schema_azure_sql.sql` (a conservative 1:1 `dbo` port keeping composite PKs and original names). The NEW file follows opinionated rules: everything in the **`analytics` schema**, plural snake_case tables, a surrogate **`id INT IDENTITY(1,1)`** PK on every table with the old natural/composite keys demoted to UNIQUE constraints, `DATETIME2 DEFAULT GETUTCDATE()`, **BIT `is_*`** booleans (renames: `ai_mode→is_ai_mode`, `match_from_keepa→is_match_from_keepa`, `ai_clean_titles→is_ai_clean_titles`, `ai_decisions_applied→is_ai_decisions_applied`), sized `NVARCHAR(n)` (MAX only for per-row JSON), `DECIMAL(5,2)` for 0-100 confidence/score, `_json` suffix on JSON columns (renames: `sources/search_methods/search_terms/sub_brands/aliases/match_methods/passthrough_cols → *_json`), table renames (`abbreviation_library→abbreviation_entries`, `global_asin_cache→global_asin_caches`, `attribute_cache→attribute_caches`, `brand_library→brand_entities`, `pair_library→pair_library_entries`), indexes on every FK/asin/upc/ean/mpn + the analytics_candidates covering index `(run_id,verdict,confidence DESC)`, and explicit FK `ON DELETE` (adds the **missing** `brand_analytics_items→brand_analytics_runs` CASCADE and a new `pair_library_ids→pair_library_entries` CASCADE). Includes a commented-out DROP block, `CREATE SCHEMA` guard, `analytics.scan_stats` view, settings seed, and 208 idempotent `system` abbreviation seeds (generated from the live DB).
- **This is a target design — migrating to it requires renaming columns in `services/database.py` + the JSON queries in `routers/analytics.py`/runner; the conservative `schema_azure_sql.sql` is the lower-churn alternative if minimal code change is preferred.** Not yet migrated to as of this entry.
