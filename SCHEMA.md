# Catalog Verifier — Database Schema

**Database:** `catalog_verifier.db`  
**Engine:** SQLite (WAL mode)  
**Tables:** 20  
**Document date:** 2026-05-18

---

## Table of Contents

1. [Configuration](#1-configuration)
2. [Global Caches](#2-global-caches)
3. [Pair Store](#3-pair-store)
4. [Abbreviation Library](#4-abbreviation-library)
5. [Verify Flow](#5-verify-flow)
6. [Analytics (ROI & Cost)](#6-analytics-roi--cost)
7. [Brand Analytics](#7-brand-analytics)
8. [Entity Relationship Summary](#8-entity-relationship-summary)

---

## 1. Configuration

### `settings`
Key/value store for application-level thresholds.

| Column | Type    | Constraints        | Description                              |
|--------|---------|--------------------|------------------------------------------|
| `key`  | TEXT    | PRIMARY KEY        | Setting name                             |
| `value`| TEXT    |                    | Setting value (stored as string)         |

**Seeded defaults**

| Key                  | Default |
|----------------------|---------|
| `threshold_verified` | `85`    |
| `threshold_review`   | `35`    |

---

## 2. Global Caches

Long-lived cross-run caches that reduce repeat API calls.

### `keepa_imports`
Global Keepa data cache, keyed by ASIN.

| Column        | Type      | Constraints        | Description                  |
|---------------|-----------|--------------------|------------------------------|
| `asin`        | TEXT      | PRIMARY KEY        | Amazon ASIN                  |
| `data_json`   | TEXT      | NOT NULL           | Full Keepa row as JSON       |
| `imported_at` | TIMESTAMP | DEFAULT NOW        | Import timestamp             |

---

### `amazon_imports`
Global Amazon export cache, keyed by ASIN.

| Column        | Type      | Constraints        | Description                  |
|---------------|-----------|--------------------|------------------------------|
| `asin`        | TEXT      | PRIMARY KEY        | Amazon ASIN                  |
| `data_json`   | TEXT      | NOT NULL           | Full Amazon row as JSON      |
| `imported_at` | TIMESTAMP | DEFAULT NOW        | Import timestamp             |

---

### `global_asin_cache`
Normalised SP-API data for every ASIN ever fetched. Shared across all Analytics and Brand Analytics runs to avoid redundant API calls.

| Column      | Type      | Constraints | Description                            |
|-------------|-----------|-------------|----------------------------------------|
| `asin`      | TEXT      | PRIMARY KEY | Amazon ASIN                            |
| `data_json` | TEXT      | NOT NULL    | Normalised SP-API payload (no raw)     |
| `updated_at`| TIMESTAMP | DEFAULT NOW | Last refresh timestamp                 |

---

### `attribute_cache`
Normalised product attributes per UPC/ASIN pair. Used by the Verify flow.

| Column            | Type      | Constraints      | Description                        |
|-------------------|-----------|------------------|------------------------------------|
| `upc`             | TEXT      | PK (composite)   | Product UPC                        |
| `asin`            | TEXT      | PK (composite)   | Amazon ASIN                        |
| `attributes_json` | TEXT      | NOT NULL         | Normalised attributes as JSON      |
| `source`          | TEXT      |                  | Data source identifier             |
| `last_updated`    | TIMESTAMP | DEFAULT NOW      | Last refresh timestamp             |

**Primary Key:** `(upc, asin)`

---

## 3. Pair Store

Global shared store of approved and rejected UPC/ASIN pairs. Written by both the Verify and Analytics flows; read at the start of every run to auto-approve or auto-reject known pairs.

### `verified_items`
Globally approved UPC/ASIN pairs.

| Column         | Type      | Constraints      | Description                             |
|----------------|-----------|------------------|-----------------------------------------|
| `upc`          | TEXT      | PK (composite)   | Product UPC                             |
| `asin`         | TEXT      | PK (composite)   | Amazon ASIN                             |
| `data_json`    | TEXT      | NOT NULL         | Full result row at time of approval     |
| `review_status`| TEXT      | DEFAULT `''`     | Label e.g. `Manually Approved`          |
| `verified_at`  | TIMESTAMP | DEFAULT NOW      | Approval timestamp                      |

**Primary Key:** `(upc, asin)`

---

### `blacklisted_pairs`
Globally rejected UPC/ASIN pairs.

| Column            | Type      | Constraints      | Description                             |
|-------------------|-----------|------------------|-----------------------------------------|
| `upc`             | TEXT      | PK (composite)   | Product UPC                             |
| `asin`            | TEXT      | PK (composite)   | Amazon ASIN                             |
| `confidence_score`| REAL      |                  | Score at time of rejection              |
| `failed_signals`  | TEXT      |                  | Comma-separated failed signal names     |
| `date_blacklisted`| TIMESTAMP | DEFAULT NOW      | Rejection timestamp                     |

**Primary Key:** `(upc, asin)`  
**Indexes:** `idx_bl_upc (upc)`, `idx_bl_asin (asin)`

---

### `asin_identifier_overrides`
Permanent per-ASIN user corrections for product identifiers. Written when a user manually corrects a UPC/EAN/MPN in Brand Analytics; applied automatically to all future runs for that ASIN.

| Column      | Type      | Constraints | Description                    |
|-------------|-----------|-------------|--------------------------------|
| `asin`      | TEXT      | PRIMARY KEY | Amazon ASIN                    |
| `mpn`       | TEXT      |             | Corrected manufacturer part no.|
| `upc`       | TEXT      |             | Corrected UPC                  |
| `ean`       | TEXT      |             | Corrected EAN                  |
| `gtin`      | TEXT      |             | Corrected GTIN                 |
| `updated_at`| TEXT      | DEFAULT NOW | Last correction timestamp      |

---

## 4. Abbreviation Library

### `abbreviation_library`
Categorised abbreviation/full-form mappings used by the AI extraction engine and the "Library" panel in the UI.

| Column      | Type      | Constraints                | Description                          |
|-------------|-----------|----------------------------|--------------------------------------|
| `id`        | INTEGER   | PRIMARY KEY AUTOINCREMENT  | Row ID                               |
| `category`  | TEXT      | NOT NULL                   | One of 10 fixed categories (see below)|
| `abbr`      | TEXT      | NOT NULL                   | Abbreviation e.g. `oz`               |
| `full_form` | TEXT      | NOT NULL                   | Expanded form e.g. `ounces`          |
| `added_by`  | TEXT      | DEFAULT `system`           | `system` / `user` / `ai`            |
| `created_at`| TIMESTAMP | DEFAULT NOW                | Creation timestamp                   |

**Unique constraint:** `(category, abbr)`

**Valid categories:** Colors, Sizes, UOMs, Forms, Sterility, Materials, Scents, Flavors, Packaging, Product Attributes

---

## 5. Verify Flow

Supports file-based catalog verification against a Keepa/Amazon export, or Match-from-Keepa mode where ASINs are discovered automatically.

### `scans`
One row per verification session. Acts as the parent for all scan sub-tables.

| Column               | Type      | Default      | Description                                          |
|----------------------|-----------|--------------|------------------------------------------------------|
| `id`                 | INTEGER   | PK AUTO      | Scan ID                                              |
| `name`               | TEXT      | NOT NULL     | User-supplied scan name                              |
| `marketplace`        | TEXT      | `US`         | Amazon marketplace                                   |
| `condition`          | TEXT      | `New`        | Product condition filter                             |
| `status`             | TEXT      | `pending`    | Lifecycle state (see below)                          |
| `mapping_json`       | TEXT      |              | Column mapping config as JSON                        |
| `catalog_filename`   | TEXT      |              | Uploaded catalog filename                            |
| `catalog_count`      | INTEGER   | `0`          | Number of catalog rows                               |
| `amazon_filename`    | TEXT      |              | Uploaded Amazon/Keepa filename                       |
| `amazon_source`      | TEXT      |              | `keepa` or `amazon`                                  |
| `amazon_count`       | INTEGER   | `0`          | Number of Amazon rows loaded                         |
| `ai_mode`            | INTEGER   | `0`          | 1 = AI extraction enabled                            |
| `match_from_keepa`   | INTEGER   | `0`          | 1 = Match-from-Keepa mode (no pre-assigned ASINs)   |
| `match_methods`      | TEXT      |              | JSON array e.g. `["upc","item_id","title"]`          |
| `verified_count`     | INTEGER   | `0`          | Cached count of Verified rows                        |
| `review_count`       | INTEGER   | `0`          | Cached count of Review rows                          |
| `not_approved_count` | INTEGER   | `0`          | Cached count of Not Approved rows                    |
| `reviewed_count`     | INTEGER   | `0`          | Cached count of manually reviewed rows               |
| `exported_at`        | TIMESTAMP |              | Timestamp of last Excel export                       |
| `created_at`         | TIMESTAMP | NOW          | Creation timestamp                                   |
| `updated_at`         | TIMESTAMP | NOW          | Last update timestamp                                |

**Indexes:** `idx_scans_created_at (created_at DESC)`

**Status lifecycle:**
```
pending → ready → verifying → verified_unreviewed → verified_partial → verified_complete
```

---

### `scan_catalog_rows`
Parsed catalog rows for a scan, stored as JSON.

| Column      | Type    | Constraints                                   | Description              |
|-------------|---------|-----------------------------------------------|--------------------------|
| `scan_id`   | INTEGER | PK (composite), FK → `scans(id)` CASCADE      | Parent scan              |
| `row_idx`   | INTEGER | PK (composite)                                | Row index (0-based)      |
| `data_json` | TEXT    | NOT NULL                                      | Raw catalog row as JSON  |

**Primary Key:** `(scan_id, row_idx)`

---

### `scan_amazon_rows`
Amazon/Keepa rows uploaded for a scan, keyed by ASIN.

| Column      | Type    | Constraints                                   | Description               |
|-------------|---------|-----------------------------------------------|---------------------------|
| `scan_id`   | INTEGER | PK (composite), FK → `scans(id)` CASCADE      | Parent scan               |
| `asin`      | TEXT    | PK (composite)                                | Amazon ASIN               |
| `data_json` | TEXT    | NOT NULL                                      | Raw Amazon row as JSON    |

**Primary Key:** `(scan_id, asin)`

---

### `scan_results`
One row per catalog row, containing the final verification verdict and full scoring detail.

| Column         | Type    | Constraints                                   | Description                            |
|----------------|---------|-----------------------------------------------|----------------------------------------|
| `scan_id`      | INTEGER | PK (composite), FK → `scans(id)` CASCADE      | Parent scan                            |
| `row_idx`      | INTEGER | PK (composite)                                | Row index matching `scan_catalog_rows` |
| `upc`          | TEXT    |                                               | Catalog UPC                            |
| `asin`         | TEXT    |                                               | Matched Amazon ASIN                    |
| `verdict`      | TEXT    |                                               | `verified` / `review` / `not approved`|
| `score`        | REAL    |                                               | Confidence score (0–100)               |
| `review_status`| TEXT    | DEFAULT `''`                                  | Manual review label                    |
| `data_json`    | TEXT    | NOT NULL                                      | Full scoring signals as JSON           |

**Primary Key:** `(scan_id, row_idx)`  
**Indexes:** `idx_scan_results_upc_asin (upc, asin)`

---

### `scan_candidates`
Up to 8 candidate ASINs per catalog row, used in Match-from-Keepa mode. The user approves one per row.

| Column         | Type    | Constraints                                   | Description                             |
|----------------|---------|-----------------------------------------------|-----------------------------------------|
| `id`           | INTEGER | PRIMARY KEY AUTOINCREMENT                     | Candidate ID                            |
| `scan_id`      | INTEGER | FK → `scans(id)` CASCADE                      | Parent scan                             |
| `row_idx`      | INTEGER | NOT NULL                                      | Catalog row this candidate belongs to   |
| `asin`         | TEXT    | NOT NULL                                      | Candidate ASIN                          |
| `confidence`   | REAL    | DEFAULT `0`                                   | Match confidence score (0–100)          |
| `verdict`      | TEXT    | DEFAULT `review`                              | `review` / `Not Approved` / approved    |
| `review_status`| TEXT    | DEFAULT `''`                                  | `Approved` / `Discarded` / etc.         |
| `match_method` | TEXT    |                                               | `upc` / `item_id` / `title`             |
| `data_json`    | TEXT    | NOT NULL                                      | Full scoring detail as JSON             |

**Unique:** `(scan_id, row_idx, asin)` — one candidate per ASIN per row per scan  
**Indexes:** `idx_scan_candidates_row (scan_id, row_idx)`, `idx_scan_candidates_asin (scan_id, asin)`

---

## 6. Analytics (ROI & Cost)

SP-API driven catalog-to-Amazon matching. User uploads a vendor catalog; the system searches Amazon for matching ASINs and scores each candidate.

### `analytics_runs`
One row per analytics run. Tracks configuration, progress, and summary counts.

| Column                  | Type      | Default    | Description                                      |
|-------------------------|-----------|------------|--------------------------------------------------|
| `id`                    | INTEGER   | PK AUTO    | Run ID                                           |
| `name`                  | TEXT      |            | User-supplied run name                           |
| `marketplace`           | TEXT      | `US`       | Amazon marketplace                               |
| `search_methods`        | TEXT      |            | JSON array e.g. `["UPC","ItemID","Title"]`       |
| `pages_per_title`       | INTEGER   | `5`        | SP-API search pages per title keyword            |
| `ai_clean_titles`       | INTEGER   | `0`        | 1 = AI title cleaning enabled                    |
| `vetting_mode`          | TEXT      | `cpg`      | `cpg` or `medical` — affects scoring thresholds  |
| `total_catalog_items`   | INTEGER   | `0`        | Number of catalog rows in this run               |
| `total_candidates_found`| INTEGER   | `0`        | Total SP-API candidates found                    |
| `verified_count`        | INTEGER   | `0`        | Verified candidate count                         |
| `review_count`          | INTEGER   | `0`        | Review candidate count                           |
| `not_approved_count`    | INTEGER   | `0`        | Not Approved candidate count                     |
| `min_rank`              | INTEGER   | `0`        | BSR floor (0 = no floor)                         |
| `max_rank`              | INTEGER   | `0`        | BSR cap (0 = no cap)                             |
| `brand_col`             | TEXT      | `''`       | Column name or literal used as brand signal      |
| `brand_mode`            | TEXT      | `col`      | `col` = column lookup, `text` = literal override |
| `status`                | TEXT      | `Pending`  | `Pending`/`Searching`/`Vetting`/`Complete`/`Error`|
| `progress_phase`        | TEXT      |            | Current phase label shown in UI                  |
| `progress_done`         | INTEGER   | `0`        | Items processed so far                           |
| `progress_total`        | INTEGER   | `0`        | Total items to process                           |
| `ai_check_status`       | TEXT      |            | `running` / `done` / `error`                     |
| `ai_check_done`         | INTEGER   | `0`        | AI-checked candidates so far                     |
| `ai_check_total`        | INTEGER   | `0`        | Total candidates queued for AI check             |
| `created_at`            | TIMESTAMP | NOW        | Creation timestamp                               |
| `updated_at`            | TIMESTAMP | NOW        | Last update timestamp                            |

**Indexes:** `idx_analytics_runs_created (created_at DESC)`

---

### `analytics_catalog_rows`
One row per line of the uploaded vendor catalog, per run.

| Column          | Type    | Constraints                                          | Description                              |
|-----------------|---------|------------------------------------------------------|------------------------------------------|
| `run_id`        | INTEGER | PK (composite), FK → `analytics_runs(id)` CASCADE   | Parent run                               |
| `row_idx`       | INTEGER | PK (composite)                                       | Row index (0-based)                      |
| `data_json`     | TEXT    | NOT NULL                                             | Raw catalog row as JSON                  |
| `extracted_json`| TEXT    |                                                      | AI-extracted brand/product fields (JSON) |

**Primary Key:** `(run_id, row_idx)`

---

### `analytics_candidates`
One row per catalog row × candidate ASIN — the core vetting results table.

| Column         | Type      | Constraints                                          | Description                              |
|----------------|-----------|------------------------------------------------------|------------------------------------------|
| `run_id`       | INTEGER   | PK (composite), FK → `analytics_runs(id)` CASCADE   | Parent run                               |
| `row_idx`      | INTEGER   | PK (composite)                                       | Catalog row index                        |
| `asin`         | TEXT      | PK (composite)                                       | Candidate ASIN                           |
| `sources`      | TEXT      |                                                      | JSON array of search methods that found this ASIN |
| `confidence`   | REAL      |                                                      | Match confidence score (0–100)           |
| `verdict`      | TEXT      |                                                      | `verified` / `review` / `not_approved`   |
| `amz_pack`     | INTEGER   |                                                      | Amazon pack count (multi-pack detection) |
| `review_status`| TEXT      | DEFAULT `''`                                         | Manual override label                    |
| `sales_rank`   | INTEGER   |                                                      | Amazon Best Seller Rank                  |
| `ai_verdict`   | TEXT      |                                                      | AI second-pass: `approve`/`reject`/`uncertain` |
| `ai_reasoning` | TEXT      |                                                      | AI reasoning (≤300 chars)               |
| `data_json`    | TEXT      | NOT NULL                                             | Full scoring signals as JSON             |
| `created_at`   | TIMESTAMP | DEFAULT NOW                                          | Row creation timestamp                   |

**Primary Key:** `(run_id, row_idx, asin)`  
**Indexes:** `idx_analytics_candidates_run (run_id)`

---

## 7. Brand Analytics

SP-API driven brand/manufacturer product discovery. User enters a brand or manufacturer name; AI discovers sub-brands; system searches Amazon for all their products and returns a structured catalog with identifiers.

### `brand_library`
Master entity store for known brands and manufacturers. Used to pre-populate the search wizard and avoid repeat AI discovery calls.

| Column               | Type    | Constraints       | Description                                     |
|----------------------|---------|-------------------|-------------------------------------------------|
| `id`                 | INTEGER | PK AUTO           | Entry ID                                        |
| `entity_type`        | TEXT    | NOT NULL          | `brand` or `manufacturer`                       |
| `name`               | TEXT    | NOT NULL, UNIQUE  | Brand or manufacturer name                      |
| `parent_manufacturer`| TEXT    |                   | Parent company name (for brands only)           |
| `sub_brands`         | TEXT    | DEFAULT `[]`      | JSON array of sub-brand name strings            |
| `aliases`            | TEXT    | DEFAULT `[]`      | JSON array of alternate spellings               |
| `discovered_by`      | TEXT    | DEFAULT `user`    | `ai` or `user`                                  |
| `created_at`         | TEXT    | DEFAULT NOW       | Creation timestamp                              |
| `updated_at`         | TEXT    | DEFAULT NOW       | Last update timestamp                           |

---

### `brand_analytics_runs`
One row per brand search run.

| Column                | Type    | Default    | Description                                           |
|-----------------------|---------|------------|-------------------------------------------------------|
| `id`                  | INTEGER | PK AUTO    | Run ID                                                |
| `name`                | TEXT    | NOT NULL   | User-supplied run name                                |
| `search_type`         | TEXT    | NOT NULL   | `brand` or `manufacturer`                             |
| `search_terms`        | TEXT    | NOT NULL   | JSON array of brand strings actually searched         |
| `library_id`          | INTEGER |            | FK to `brand_library` (nullable)                      |
| `vetting_mode`        | TEXT    | `cpg`      | `cpg` or `medical`                                    |
| `status`              | TEXT    | `Pending`  | `Pending`/`Searching`/`Complete`/`Error`/`Stopped`    |
| `progress_phase`      | TEXT    |            | Current phase label                                   |
| `progress_done`       | INTEGER | `0`        | Items processed so far                                |
| `progress_total`      | INTEGER | `0`        | Total items to process                                |
| `min_rank`            | INTEGER | `0`        | BSR floor (0 = no floor)                              |
| `max_rank`            | INTEGER | `0`        | BSR cap (0 = no cap)                                  |
| `pages_per_brand`     | INTEGER | `3`        | SP-API pages searched per brand keyword               |
| `last_asin_updated_at`| TEXT    |            | Timestamp of last full SP-API refresh (cache check)   |
| `ai_fill_status`      | TEXT    |            | `running` / `done` / `error`                          |
| `ai_fill_done`        | INTEGER | `0`        | Items AI-filled so far                                |
| `ai_fill_total`       | INTEGER | `0`        | Total items queued for AI fill                        |
| `created_at`          | TEXT    | NOW        | Creation timestamp                                    |
| `updated_at`          | TEXT    | NOW        | Last update timestamp                                 |

**Indexes:** `idx_brand_runs_created (created_at DESC)`

---

### `brand_analytics_items`
One row per discovered ASIN per run. Core results table for Brand Analytics.

| Column          | Type    | Constraints                     | Description                                     |
|-----------------|---------|---------------------------------|-------------------------------------------------|
| `id`            | INTEGER | PK AUTO                         | Row ID                                          |
| `run_id`        | INTEGER | NOT NULL, FK → `brand_analytics_runs(id)` | Parent run                         |
| `brand_searched`| TEXT    | NOT NULL                        | Which sub-brand keyword found this ASIN         |
| `asin`          | TEXT    | NOT NULL                        | Amazon ASIN                                     |
| `title`         | TEXT    |                                 | Amazon product title                            |
| `bsr`           | INTEGER |                                 | Amazon Best Seller Rank                         |
| `bsr_category`  | TEXT    |                                 | BSR category name                               |
| `mpn`           | TEXT    |                                 | Manufacturer Part Number (from SP-API)          |
| `upc`           | TEXT    |                                 | UPC (from SP-API)                               |
| `ean`           | TEXT    |                                 | EAN (from SP-API)                               |
| `gtin`          | TEXT    |                                 | GTIN (from SP-API)                              |
| `image_url`     | TEXT    |                                 | Product image URL                               |
| `ai_mpn`        | TEXT    |                                 | AI-extracted MPN                                |
| `ai_upc`        | TEXT    |                                 | AI-extracted UPC                                |
| `ai_ean`        | TEXT    |                                 | AI-extracted EAN                                |
| `ai_gtin`       | TEXT    |                                 | AI-extracted GTIN                               |
| `ai_fill_status`| TEXT    |                                 | `NULL` / `done` / `error`                       |
| `pack_qty`      | TEXT    |                                 | Pack quantity extracted from listing            |
| `uom_qty`       | TEXT    |                                 | Unit-of-measure quantity extracted from listing |
| `data_json`     | TEXT    | DEFAULT `{}`                    | Full SP-API normalised payload as JSON          |
| `updated_at`    | TEXT    | DEFAULT NOW                     | Last update timestamp                           |

**Unique:** `(run_id, asin)` — one row per ASIN per run  
**Indexes:** `idx_brand_items_run (run_id)`

---

## 8. Entity Relationship Summary

```
settings                        (standalone config)
abbreviation_library            (standalone library)
keepa_imports                   (global cache)
amazon_imports                  (global cache)
global_asin_cache               (global cache)
attribute_cache                 (global cache, keyed by upc+asin)
verified_items                  (global pair store)
blacklisted_pairs               (global pair store)
asin_identifier_overrides       (global corrections)

scans
  ├── scan_catalog_rows         (cascade delete)
  ├── scan_amazon_rows          (cascade delete)
  ├── scan_results              (cascade delete)
  └── scan_candidates           (cascade delete, match-from-Keepa only)

analytics_runs
  ├── analytics_catalog_rows    (cascade delete)
  └── analytics_candidates      (cascade delete)

brand_library                   (standalone master entity store)

brand_analytics_runs
  └── brand_analytics_items     (no declared cascade — deleted manually)
```

---

*Generated from live database · catalog-verifier v1.0.0*
