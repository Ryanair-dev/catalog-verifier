/* ===========================================================================
   catalog-verifier — Azure SQL Database schema  (target: Azure SQL / T-SQL)
   ---------------------------------------------------------------------------
   Built from the LIVE catalog_verifier.db (22 tables introspected) — not from
   the stale SCHEMA.md (which predates pair_library/pair_library_ids and the
   newer analytics_runs columns).

   DESIGN RULES APPLIED
     • All objects live in the [analytics] schema.
     • snake_case, plural table names.
     • Every table has a surrogate  id INT IDENTITY(1,1)  primary key.
       The old SQLite natural/composite keys become UNIQUE constraints
       (e.g. UNIQUE(asin) on keepa_imports, UNIQUE(run_id,row_idx,asin) on
       analytics_candidates) so existing upsert logic still has a key to target.
     • Timestamps: DATETIME2 DEFAULT GETUTCDATE()  (SQLite stored these as a mix
       of TIMESTAMP and TEXT — normalised here).
     • Booleans: BIT NOT NULL DEFAULT 0, named is_<x>  (SQLite stored 0/1 INTEGER).
     • TEXT → NVARCHAR(n) with real lengths; NVARCHAR(MAX) only for unbounded
       per-row JSON payloads.
     • Prices → DECIMAL(10,2); 0-100 confidence/score → DECIMAL(5,2)
       (see note below — these are NOT 0-1 ratios, so DECIMAL(5,4) is not used).
     • JSON payload columns end in _json.
     • Indexes on every FK, every WHERE/ORDER column, and on
       asin / upc / ean / mpn(item_id) wherever they appear.
     • Every FK has an explicit ON DELETE behaviour.

   RENAMES (commented at each site; the app's data layer must be updated to match):
     TABLES   abbreviation_library → abbreviation_entries
              global_asin_cache    → global_asin_caches
              attribute_cache      → attribute_caches
              brand_library        → brand_entities
              pair_library         → pair_library_entries
     BOOLEANS ai_mode→is_ai_mode  match_from_keepa→is_match_from_keepa
              ai_clean_titles→is_ai_clean_titles
              ai_decisions_applied→is_ai_decisions_applied
     JSON     sources→sources_json  search_methods→search_methods_json
              search_terms→search_terms_json  sub_brands→sub_brands_json
              aliases→aliases_json  match_methods→match_methods_json
              passthrough_cols→passthrough_cols_json

   AMBIGUITY DECISIONS (each also commented inline):
     • No DECIMAL(10,2) price columns exist — vendor/Amazon prices live INSIDE the
       data_json blobs and the analytics passthrough columns, never as their own
       relational column, so there is nothing to type as DECIMAL here.
     • confidence/score are 0-100 match scores, stored DECIMAL(5,2). They are not
       0-1 ratios, so the DECIMAL(5,4) ratio rule does not apply.
     • blacklisted_pairs.failed_signals is a COMMA-separated string, not JSON, so
       it keeps its name (no _json suffix) and stays NVARCHAR.
     • Reserved words ([key],[value],[condition],[source]) are bracketed rather
       than renamed, to keep the column names the app already uses.
     • The denormalised *_count columns on scans/analytics_runs are kept (the app
       reads them directly); analytics.scan_stats VIEW is provided as the
       live-accurate alternative if you ever want to drop them.
   =========================================================================== */


/* ---------------------------------------------------------------------------
   0.  SAFETY DROP BLOCK  —  COMMENTED OUT BY DEFAULT.
       Uncomment the whole block to rebuild from scratch. Children first so the
       FK cascade order is satisfied even if cascade is later disabled.
   --------------------------------------------------------------------------- */
/*
DROP TABLE IF EXISTS analytics.brand_analytics_items;
DROP TABLE IF EXISTS analytics.brand_analytics_runs;
DROP TABLE IF EXISTS analytics.analytics_candidates;
DROP TABLE IF EXISTS analytics.analytics_catalog_rows;
DROP TABLE IF EXISTS analytics.analytics_runs;
DROP TABLE IF EXISTS analytics.scan_candidates;
DROP TABLE IF EXISTS analytics.scan_results;
DROP TABLE IF EXISTS analytics.scan_amazon_rows;
DROP TABLE IF EXISTS analytics.scan_catalog_rows;
DROP TABLE IF EXISTS analytics.scans;
DROP TABLE IF EXISTS analytics.pair_library_ids;
DROP TABLE IF EXISTS analytics.pair_library_entries;
DROP TABLE IF EXISTS analytics.brand_entities;
DROP TABLE IF EXISTS analytics.asin_identifier_overrides;
DROP TABLE IF EXISTS analytics.blacklisted_pairs;
DROP TABLE IF EXISTS analytics.verified_items;
DROP TABLE IF EXISTS analytics.attribute_caches;
DROP TABLE IF EXISTS analytics.global_asin_caches;
DROP TABLE IF EXISTS analytics.amazon_imports;
DROP TABLE IF EXISTS analytics.keepa_imports;
DROP TABLE IF EXISTS analytics.abbreviation_entries;
DROP TABLE IF EXISTS analytics.settings;
GO
*/


/* ---------------------------------------------------------------------------
   1.  SCHEMA
   --------------------------------------------------------------------------- */
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'analytics')
    EXEC('CREATE SCHEMA analytics');
GO


/* ===========================================================================
   2.  STANDALONE CONFIG / LIBRARIES  (no FK dependencies)
   =========================================================================== */

-- Key/value application settings (verdict thresholds, etc.).
CREATE TABLE analytics.settings (
    id            INT            IDENTITY(1,1) NOT NULL,
    [key]         NVARCHAR(100)  NOT NULL,         -- reserved word, bracketed (was PK in SQLite)
    [value]       NVARCHAR(500)      NULL,         -- short scalar values only
    created_at    DATETIME2      NOT NULL DEFAULT GETUTCDATE(),
    CONSTRAINT pk_settings PRIMARY KEY (id),
    CONSTRAINT uq_settings_key UNIQUE ([key])
);

-- Categorised abbreviation/term expansions used by the extraction engine + Library UI.
CREATE TABLE analytics.abbreviation_entries (          -- was abbreviation_library
    id            INT            IDENTITY(1,1) NOT NULL,
    category      NVARCHAR(100)  NOT NULL,
    abbr          NVARCHAR(200)  NOT NULL,
    full_form     NVARCHAR(500)  NOT NULL,
    added_by      NVARCHAR(50)   NOT NULL DEFAULT 'system',   -- 'system' | 'user' | 'ai'
    created_at    DATETIME2      NOT NULL DEFAULT GETUTCDATE(),
    CONSTRAINT pk_abbreviation_entries PRIMARY KEY (id),
    CONSTRAINT uq_abbreviation_entries UNIQUE (category, abbr)
);


/* ===========================================================================
   3.  GLOBAL CACHES  (keyed by ASIN / UPC — no FK dependencies)
   =========================================================================== */

-- Global Keepa export cache, one row per ASIN (reused across scans).
CREATE TABLE analytics.keepa_imports (
    id            INT            IDENTITY(1,1) NOT NULL,
    asin          NVARCHAR(20)   NOT NULL,
    data_json     NVARCHAR(MAX)  NOT NULL,
    imported_at   DATETIME2      NOT NULL DEFAULT GETUTCDATE(),
    CONSTRAINT pk_keepa_imports PRIMARY KEY (id),
    CONSTRAINT uq_keepa_imports_asin UNIQUE (asin)      -- natural business key
);

-- Global Amazon export cache, one row per ASIN (reused across scans).
CREATE TABLE analytics.amazon_imports (
    id            INT            IDENTITY(1,1) NOT NULL,
    asin          NVARCHAR(20)   NOT NULL,
    data_json     NVARCHAR(MAX)  NOT NULL,
    imported_at   DATETIME2      NOT NULL DEFAULT GETUTCDATE(),
    CONSTRAINT pk_amazon_imports PRIMARY KEY (id),
    CONSTRAINT uq_amazon_imports_asin UNIQUE (asin)
);

-- Normalised SP-API payload for every ASIN ever fetched (avoids repeat API calls).
CREATE TABLE analytics.global_asin_caches (            -- was global_asin_cache
    id            INT            IDENTITY(1,1) NOT NULL,
    asin          NVARCHAR(20)   NOT NULL,
    data_json     NVARCHAR(MAX)  NOT NULL,
    updated_at    DATETIME2      NOT NULL DEFAULT GETUTCDATE(),
    CONSTRAINT pk_global_asin_caches PRIMARY KEY (id),
    CONSTRAINT uq_global_asin_caches_asin UNIQUE (asin)
);

-- Normalised product attributes per (UPC, ASIN) — used by the verify flow.
CREATE TABLE analytics.attribute_caches (              -- was attribute_cache
    id              INT           IDENTITY(1,1) NOT NULL,
    upc             NVARCHAR(20)  NOT NULL,
    asin            NVARCHAR(20)  NOT NULL,
    attributes_json NVARCHAR(MAX) NOT NULL,
    [source]        NVARCHAR(100)     NULL,        -- reserved word, bracketed
    last_updated    DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
    CONSTRAINT pk_attribute_caches PRIMARY KEY (id),
    CONSTRAINT uq_attribute_caches UNIQUE (upc, asin)
);


/* ===========================================================================
   4.  PAIR STORE  (approved / rejected UPC↔ASIN pairs + per-ASIN corrections)
   =========================================================================== */

-- Globally approved UPC↔ASIN pairs (auto-approve memory for runs/scans).
CREATE TABLE analytics.verified_items (
    id            INT            IDENTITY(1,1) NOT NULL,
    upc           NVARCHAR(20)   NOT NULL,
    asin          NVARCHAR(20)   NOT NULL,
    data_json     NVARCHAR(MAX)  NOT NULL,
    review_status NVARCHAR(100)  NOT NULL DEFAULT '',
    verified_at   DATETIME2      NOT NULL DEFAULT GETUTCDATE(),
    CONSTRAINT pk_verified_items PRIMARY KEY (id),
    CONSTRAINT uq_verified_items UNIQUE (upc, asin)
);

-- Globally rejected UPC↔ASIN pairs (auto-reject memory for runs/scans).
CREATE TABLE analytics.blacklisted_pairs (
    id               INT           IDENTITY(1,1) NOT NULL,
    upc              NVARCHAR(20)  NOT NULL,
    asin             NVARCHAR(20)  NOT NULL,
    confidence_score DECIMAL(5,2)      NULL,      -- 0-100 score at rejection (not a 0-1 ratio)
    failed_signals   NVARCHAR(500)     NULL,      -- COMMA-separated names, not JSON
    date_blacklisted DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
    CONSTRAINT pk_blacklisted_pairs PRIMARY KEY (id),
    CONSTRAINT uq_blacklisted_pairs UNIQUE (upc, asin)
);

-- Permanent per-ASIN identifier corrections applied to all future brand runs.
CREATE TABLE analytics.asin_identifier_overrides (
    id            INT            IDENTITY(1,1) NOT NULL,
    asin          NVARCHAR(20)   NOT NULL,
    mpn           NVARCHAR(100)      NULL,        -- mpn == the "item_id" identifier in this app
    upc           NVARCHAR(20)       NULL,
    ean           NVARCHAR(20)       NULL,
    gtin          NVARCHAR(20)       NULL,
    updated_at    DATETIME2      NOT NULL DEFAULT GETUTCDATE(),
    CONSTRAINT pk_asin_identifier_overrides PRIMARY KEY (id),
    CONSTRAINT uq_asin_identifier_overrides_asin UNIQUE (asin)
);

-- Manually-confirmed identifier→ASIN library (parent: one row per ASIN).
CREATE TABLE analytics.pair_library_entries (          -- was pair_library
    id            INT            IDENTITY(1,1) NOT NULL,
    asin          NVARCHAR(20)   NOT NULL,
    brand         NVARCHAR(200)  NOT NULL DEFAULT '',
    manufacturer  NVARCHAR(200)  NOT NULL DEFAULT '',
    created_at    DATETIME2      NOT NULL DEFAULT GETUTCDATE(),
    updated_at    DATETIME2      NOT NULL DEFAULT GETUTCDATE(),
    CONSTRAINT pk_pair_library_entries PRIMARY KEY (id),
    CONSTRAINT uq_pair_library_entries_asin UNIQUE (asin)
);

-- Identifiers (UPC/EAN/MPN) attached to each pair-library ASIN; one UPC may map
-- to many ASINs, and an ASIN keeps its own primary + alias identifiers.
CREATE TABLE analytics.pair_library_ids (
    id            INT            IDENTITY(1,1) NOT NULL,
    asin          NVARCHAR(20)   NOT NULL,
    id_type       NVARCHAR(20)   NOT NULL,        -- 'upc' | 'ean' | 'mpn'
    identifier    NVARCHAR(100)  NOT NULL,        -- MPNs can be long, so 100 not 20
    is_primary    BIT            NOT NULL DEFAULT 0,
    added_at      DATETIME2      NOT NULL DEFAULT GETUTCDATE(),
    CONSTRAINT pk_pair_library_ids PRIMARY KEY (id),
    CONSTRAINT uq_pair_library_ids UNIQUE (asin, id_type, identifier),
    -- NEW FK (absent in SQLite): deleting a pair-library entry removes its identifiers.
    CONSTRAINT fk_pair_library_ids_entry
        FOREIGN KEY (asin) REFERENCES analytics.pair_library_entries(asin) ON DELETE CASCADE
);


/* ===========================================================================
   5.  BRAND MASTER STORE
   =========================================================================== */

-- Master brand/manufacturer entity store (sub-brands, aliases) for the wizard.
CREATE TABLE analytics.brand_entities (                -- was brand_library
    id                  INT           IDENTITY(1,1) NOT NULL,
    entity_type         NVARCHAR(50)  NOT NULL,        -- 'brand' | 'manufacturer'
    name                NVARCHAR(500) NOT NULL,
    parent_manufacturer NVARCHAR(500)     NULL,
    sub_brands_json     NVARCHAR(2000) NOT NULL DEFAULT '[]',  -- bounded JSON array, not MAX
    aliases_json        NVARCHAR(2000) NOT NULL DEFAULT '[]',  -- bounded JSON array, not MAX
    discovered_by       NVARCHAR(20)  NOT NULL DEFAULT 'user', -- 'ai' | 'user'
    created_at          DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
    updated_at          DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
    CONSTRAINT pk_brand_entities PRIMARY KEY (id),
    CONSTRAINT uq_brand_entities_name UNIQUE (name)
);


/* ===========================================================================
   6.  VERIFY FLOW  (scans + children)
   =========================================================================== */

-- One verification session; parent of all scan_* child tables.
CREATE TABLE analytics.scans (
    id                  INT           IDENTITY(1,1) NOT NULL,
    name                NVARCHAR(500) NOT NULL,
    marketplace         NVARCHAR(20)  NOT NULL DEFAULT 'US',
    [condition]         NVARCHAR(50)  NOT NULL DEFAULT 'New',   -- reserved word, bracketed
    status              NVARCHAR(50)  NOT NULL DEFAULT 'pending',
    mapping_json        NVARCHAR(MAX)     NULL,
    catalog_filename    NVARCHAR(500)     NULL,
    catalog_count       INT           NOT NULL DEFAULT 0,
    amazon_filename     NVARCHAR(500)     NULL,
    amazon_source       NVARCHAR(50)      NULL,             -- 'keepa' | 'amazon'
    amazon_count        INT           NOT NULL DEFAULT 0,
    is_ai_mode          BIT           NOT NULL DEFAULT 0,   -- was ai_mode
    is_match_from_keepa BIT           NOT NULL DEFAULT 0,   -- was match_from_keepa
    match_methods_json  NVARCHAR(200)     NULL,             -- bounded JSON array, was match_methods
    verified_count      INT           NOT NULL DEFAULT 0,   -- denormalised cache (see scan_stats view)
    review_count        INT           NOT NULL DEFAULT 0,
    not_approved_count  INT           NOT NULL DEFAULT 0,
    reviewed_count      INT           NOT NULL DEFAULT 0,
    exported_at         DATETIME2         NULL,
    created_at          DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
    updated_at          DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
    CONSTRAINT pk_scans PRIMARY KEY (id)
);

-- Parsed vendor catalog rows for a scan (raw row stored as JSON).
CREATE TABLE analytics.scan_catalog_rows (
    id            INT            IDENTITY(1,1) NOT NULL,
    scan_id       INT            NOT NULL,
    row_idx       INT            NOT NULL,
    data_json     NVARCHAR(MAX)  NOT NULL,
    CONSTRAINT pk_scan_catalog_rows PRIMARY KEY (id),
    CONSTRAINT uq_scan_catalog_rows UNIQUE (scan_id, row_idx),
    CONSTRAINT fk_scan_catalog_rows_scan
        FOREIGN KEY (scan_id) REFERENCES analytics.scans(id) ON DELETE CASCADE
);

-- Amazon/Keepa rows uploaded for a scan, keyed by ASIN.
CREATE TABLE analytics.scan_amazon_rows (
    id            INT            IDENTITY(1,1) NOT NULL,
    scan_id       INT            NOT NULL,
    asin          NVARCHAR(20)   NOT NULL,
    data_json     NVARCHAR(MAX)  NOT NULL,
    CONSTRAINT pk_scan_amazon_rows PRIMARY KEY (id),
    CONSTRAINT uq_scan_amazon_rows UNIQUE (scan_id, asin),
    CONSTRAINT fk_scan_amazon_rows_scan
        FOREIGN KEY (scan_id) REFERENCES analytics.scans(id) ON DELETE CASCADE
);

-- Final per-row verdict + full scoring signals for a scan.
CREATE TABLE analytics.scan_results (
    id            INT            IDENTITY(1,1) NOT NULL,
    scan_id       INT            NOT NULL,
    row_idx       INT            NOT NULL,
    upc           NVARCHAR(20)       NULL,
    asin          NVARCHAR(20)       NULL,
    verdict       NVARCHAR(50)       NULL,        -- 'verified' | 'review' | 'not approved'
    score         DECIMAL(5,2)       NULL,        -- 0-100 confidence
    review_status NVARCHAR(100)  NOT NULL DEFAULT '',
    data_json     NVARCHAR(MAX)  NOT NULL,
    CONSTRAINT pk_scan_results PRIMARY KEY (id),
    CONSTRAINT uq_scan_results UNIQUE (scan_id, row_idx),
    CONSTRAINT fk_scan_results_scan
        FOREIGN KEY (scan_id) REFERENCES analytics.scans(id) ON DELETE CASCADE
);

-- Up to 8 candidate ASINs per catalog row (Match-from-Keepa mode).
CREATE TABLE analytics.scan_candidates (
    id            INT            IDENTITY(1,1) NOT NULL,
    scan_id       INT            NOT NULL,
    row_idx       INT            NOT NULL,
    asin          NVARCHAR(20)   NOT NULL,
    confidence    DECIMAL(5,2)   NOT NULL DEFAULT 0,
    verdict       NVARCHAR(50)   NOT NULL DEFAULT 'review',
    review_status NVARCHAR(100)  NOT NULL DEFAULT '',
    match_method  NVARCHAR(50)       NULL,        -- 'upc' | 'item_id' | 'title'
    data_json     NVARCHAR(MAX)  NOT NULL,
    CONSTRAINT pk_scan_candidates PRIMARY KEY (id),
    CONSTRAINT uq_scan_candidates UNIQUE (scan_id, row_idx, asin),
    CONSTRAINT fk_scan_candidates_scan
        FOREIGN KEY (scan_id) REFERENCES analytics.scans(id) ON DELETE CASCADE
);


/* ===========================================================================
   7.  ANALYTICS FLOW  (SP-API runs + children)
   =========================================================================== */

-- One SP-API analytics run: configuration, progress, and summary counts.
CREATE TABLE analytics.analytics_runs (
    id                     INT           IDENTITY(1,1) NOT NULL,
    name                   NVARCHAR(500)     NULL,
    marketplace            NVARCHAR(20)  NOT NULL DEFAULT 'US',
    search_methods_json    NVARCHAR(200)     NULL,            -- bounded JSON array, was search_methods
    pages_per_title        INT           NOT NULL DEFAULT 5,
    is_ai_clean_titles     BIT           NOT NULL DEFAULT 0,  -- was ai_clean_titles
    vetting_mode           NVARCHAR(20)  NOT NULL DEFAULT 'cpg',  -- 'cpg' | 'medical'
    total_catalog_items    INT           NOT NULL DEFAULT 0,
    total_candidates_found INT           NOT NULL DEFAULT 0,
    verified_count         INT           NOT NULL DEFAULT 0,  -- denormalised cache
    review_count           INT           NOT NULL DEFAULT 0,
    not_approved_count     INT           NOT NULL DEFAULT 0,
    min_rank               INT           NOT NULL DEFAULT 0,
    max_rank               INT           NOT NULL DEFAULT 0,
    brand_col              NVARCHAR(200) NOT NULL DEFAULT '',
    brand_mode             NVARCHAR(20)  NOT NULL DEFAULT 'col',  -- 'col' | 'text'
    status                 NVARCHAR(50)  NOT NULL DEFAULT 'Pending',
    progress_phase         NVARCHAR(200)     NULL,
    progress_done          INT           NOT NULL DEFAULT 0,
    progress_total         INT           NOT NULL DEFAULT 0,
    ai_check_status        NVARCHAR(50)      NULL,
    ai_check_done          INT           NOT NULL DEFAULT 0,
    ai_check_total         INT           NOT NULL DEFAULT 0,
    is_ai_decisions_applied BIT          NOT NULL DEFAULT 0,  -- was ai_decisions_applied
    duplicate_rows_removed INT           NOT NULL DEFAULT 0,
    passthrough_cols_json  NVARCHAR(2000) NOT NULL DEFAULT '', -- bounded JSON array, was passthrough_cols
    created_at             DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
    updated_at             DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
    CONSTRAINT pk_analytics_runs PRIMARY KEY (id)
);

-- One uploaded vendor catalog row per analytics run (+ AI-extracted fields).
CREATE TABLE analytics.analytics_catalog_rows (
    id             INT           IDENTITY(1,1) NOT NULL,
    run_id         INT           NOT NULL,
    row_idx        INT           NOT NULL,
    data_json      NVARCHAR(MAX) NOT NULL,
    extracted_json NVARCHAR(MAX)     NULL,        -- AI-extracted brand/product fields
    CONSTRAINT pk_analytics_catalog_rows PRIMARY KEY (id),
    CONSTRAINT uq_analytics_catalog_rows UNIQUE (run_id, row_idx),
    CONSTRAINT fk_analytics_catalog_rows_run
        FOREIGN KEY (run_id) REFERENCES analytics.analytics_runs(id) ON DELETE CASCADE
);

-- Core vetting results: one row per (catalog row × candidate ASIN). Largest table.
-- NOTE: on a table this size (~340k+ rows) consider making this PK NONCLUSTERED
-- and CLUSTERING on (run_id, row_idx, asin) instead, since nearly every query is
-- run-scoped. Left as id-clustered to satisfy the surrogate-key rule.
CREATE TABLE analytics.analytics_candidates (
    id            INT            IDENTITY(1,1) NOT NULL,
    run_id        INT            NOT NULL,
    row_idx       INT            NOT NULL,
    asin          NVARCHAR(20)   NOT NULL,
    sources_json  NVARCHAR(200)      NULL,        -- bounded JSON array, was sources
    confidence    DECIMAL(5,2)       NULL,        -- 0-100 score
    verdict       NVARCHAR(50)       NULL,        -- 'verified' | 'review' | 'not_approved'
    amz_pack      INT                NULL,
    review_status NVARCHAR(100)  NOT NULL DEFAULT '',
    sales_rank    INT                NULL,
    ai_verdict    NVARCHAR(20)       NULL,        -- 'approve' | 'reject' | 'uncertain'
    ai_reasoning  NVARCHAR(400)      NULL,        -- app caps at ~300 chars
    data_json     NVARCHAR(MAX)  NOT NULL,
    created_at    DATETIME2      NOT NULL DEFAULT GETUTCDATE(),
    CONSTRAINT pk_analytics_candidates PRIMARY KEY (id),
    CONSTRAINT uq_analytics_candidates UNIQUE (run_id, row_idx, asin),
    CONSTRAINT fk_analytics_candidates_run
        FOREIGN KEY (run_id) REFERENCES analytics.analytics_runs(id) ON DELETE CASCADE
);


/* ===========================================================================
   8.  BRAND ANALYTICS FLOW  (brand runs + items)
   =========================================================================== */

-- One brand/manufacturer discovery run.
CREATE TABLE analytics.brand_analytics_runs (
    id                   INT           IDENTITY(1,1) NOT NULL,
    name                 NVARCHAR(500) NOT NULL,
    search_type          NVARCHAR(50)  NOT NULL,            -- 'brand' | 'manufacturer'
    search_terms_json    NVARCHAR(2000) NOT NULL,           -- bounded JSON array, was search_terms
    library_id           INT               NULL,            -- FK → brand_entities (nullable)
    vetting_mode         NVARCHAR(20)  NOT NULL DEFAULT 'cpg',
    status               NVARCHAR(50)  NOT NULL DEFAULT 'Pending',
    progress_phase       NVARCHAR(200)     NULL,
    progress_done        INT           NOT NULL DEFAULT 0,
    progress_total       INT           NOT NULL DEFAULT 0,
    min_rank             INT           NOT NULL DEFAULT 0,
    max_rank             INT           NOT NULL DEFAULT 0,
    pages_per_brand      INT           NOT NULL DEFAULT 3,
    last_asin_updated_at DATETIME2         NULL,            -- cache freshness check
    ai_fill_status       NVARCHAR(50)      NULL,
    ai_fill_done         INT           NOT NULL DEFAULT 0,
    ai_fill_total        INT           NOT NULL DEFAULT 0,
    created_at           DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
    updated_at           DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
    CONSTRAINT pk_brand_analytics_runs PRIMARY KEY (id),
    -- Deleting a library entity must NOT delete its runs → SET NULL.
    CONSTRAINT fk_brand_analytics_runs_library
        FOREIGN KEY (library_id) REFERENCES analytics.brand_entities(id) ON DELETE SET NULL
);

-- One discovered ASIN per brand run (core Brand Analytics results table).
CREATE TABLE analytics.brand_analytics_items (
    id             INT            IDENTITY(1,1) NOT NULL,
    run_id         INT            NOT NULL,
    brand_searched NVARCHAR(500)  NOT NULL,      -- which sub-brand keyword found this ASIN
    asin           NVARCHAR(20)   NOT NULL,
    amz_brand      NVARCHAR(200)      NULL,       -- actual Amazon brand field value
    title          NVARCHAR(1000)     NULL,       -- Amazon titles are long; sized generously, not MAX
    bsr            INT                NULL,
    bsr_category   NVARCHAR(200)      NULL,
    mpn            NVARCHAR(100)      NULL,        -- mpn == the "item_id" identifier
    upc            NVARCHAR(20)       NULL,
    ean            NVARCHAR(20)       NULL,
    gtin           NVARCHAR(20)       NULL,
    image_url      NVARCHAR(1000)     NULL,
    ai_mpn         NVARCHAR(100)      NULL,
    ai_upc         NVARCHAR(20)       NULL,
    ai_ean         NVARCHAR(20)       NULL,
    ai_gtin        NVARCHAR(20)       NULL,
    ai_fill_status NVARCHAR(50)       NULL,        -- NULL | 'done' | 'error'
    pack_qty       NVARCHAR(50)       NULL,        -- kept as text: holds values like "2/1200ML"
    uom_qty        NVARCHAR(50)       NULL,        -- kept as text: non-numeric values occur
    data_json      NVARCHAR(MAX)  NOT NULL DEFAULT '{}',
    updated_at     DATETIME2      NOT NULL DEFAULT GETUTCDATE(),
    CONSTRAINT pk_brand_analytics_items PRIMARY KEY (id),
    CONSTRAINT uq_brand_analytics_items UNIQUE (run_id, asin),
    -- NEW FK (absent in SQLite, where cascade delete silently failed).
    CONSTRAINT fk_brand_analytics_items_run
        FOREIGN KEY (run_id) REFERENCES analytics.brand_analytics_runs(id) ON DELETE CASCADE
);
GO


/* ===========================================================================
   9.  INDEXES  (grouped by table; natural-key UNIQUE constraints already index
       their columns, so those are not repeated here)
   =========================================================================== */

-- abbreviation_entries: uq(category, abbr) already covers category-leading lookups.

-- keepa_imports / amazon_imports / global_asin_caches: support TTL purge sweeps.
CREATE INDEX ix_keepa_imports_imported_at      ON analytics.keepa_imports (imported_at);
CREATE INDEX ix_amazon_imports_imported_at     ON analytics.amazon_imports (imported_at);
CREATE INDEX ix_global_asin_caches_updated_at  ON analytics.global_asin_caches (updated_at);

-- attribute_caches: uq(upc, asin) covers upc; add the asin-only lookup.
CREATE INDEX ix_attribute_caches_asin          ON analytics.attribute_caches (asin);

-- verified_items: uq(upc, asin) covers upc; add asin lookup ("all pairs for ASIN").
CREATE INDEX ix_verified_items_asin            ON analytics.verified_items (asin);

-- blacklisted_pairs: uq(upc, asin) covers upc; add asin lookup.
CREATE INDEX ix_blacklisted_pairs_asin         ON analytics.blacklisted_pairs (asin);

-- asin_identifier_overrides: uq(asin) is the only access path.
-- NOTE: upc/ean/gtin/mpn here are correction OUTPUT values, never lookup keys,
--       so per "best decision" they are intentionally NOT indexed.

-- pair_library_entries: uq(asin) covers ASIN; brand drives the per-brand export.
CREATE INDEX ix_pair_library_entries_brand     ON analytics.pair_library_entries (brand);

-- pair_library_ids: uq(asin,id_type,identifier) covers the asin FK; the runtime
-- lookup is (id_type, identifier) → set of ASINs.
CREATE INDEX ix_pair_library_ids_lookup        ON analytics.pair_library_ids (id_type, identifier);

-- brand_entities: uq(name) covers name; Library tabs filter by type/parent.
CREATE INDEX ix_brand_entities_type            ON analytics.brand_entities (entity_type);
CREATE INDEX ix_brand_entities_parent          ON analytics.brand_entities (parent_manufacturer);

-- scans: run list ordering + status filtering.
CREATE INDEX ix_scans_created_at               ON analytics.scans (created_at DESC);
CREATE INDEX ix_scans_status                   ON analytics.scans (status);

-- scan_catalog_rows: uq(scan_id,row_idx) already covers WHERE scan_id = ?.

-- scan_amazon_rows: uq(scan_id,asin) covers the FK; add global asin lookup.
CREATE INDEX ix_scan_amazon_rows_asin          ON analytics.scan_amazon_rows (asin);

-- scan_results: uq(scan_id,row_idx) covers the FK.
CREATE INDEX ix_scan_results_upc_asin          ON analytics.scan_results (upc, asin);  -- pair-store cross-ref
CREATE INDEX ix_scan_results_asin              ON analytics.scan_results (asin);
CREATE INDEX ix_scan_results_verdict           ON analytics.scan_results (scan_id, verdict);

-- scan_candidates: uq(scan_id,row_idx,asin) covers (scan_id,row_idx); add the
-- "is this ASIN approved in this scan?" path and a global asin lookup.
CREATE INDEX ix_scan_candidates_scan_asin      ON analytics.scan_candidates (scan_id, asin);
CREATE INDEX ix_scan_candidates_asin           ON analytics.scan_candidates (asin);

-- analytics_runs: run list ordering + status filtering.
CREATE INDEX ix_analytics_runs_created_at      ON analytics.analytics_runs (created_at DESC);
CREATE INDEX ix_analytics_runs_status          ON analytics.analytics_runs (status);

-- analytics_catalog_rows: uq(run_id,row_idx) already covers WHERE run_id = ?.

-- analytics_candidates: the hot table. Covering index for every tab query
-- (verdict filter + confidence sort), plus AI-filter, rank-window, and asin.
CREATE INDEX ix_analytics_candidates_verdict_conf
    ON analytics.analytics_candidates (run_id, verdict, confidence DESC);
CREATE INDEX ix_analytics_candidates_ai        ON analytics.analytics_candidates (run_id, ai_verdict);
CREATE INDEX ix_analytics_candidates_rank      ON analytics.analytics_candidates (run_id, sales_rank);
CREATE INDEX ix_analytics_candidates_asin      ON analytics.analytics_candidates (asin);

-- brand_analytics_runs: run list ordering, status filter, library FK.
CREATE INDEX ix_brand_analytics_runs_created_at ON analytics.brand_analytics_runs (created_at DESC);
CREATE INDEX ix_brand_analytics_runs_status     ON analytics.brand_analytics_runs (status);
CREATE INDEX ix_brand_analytics_runs_library    ON analytics.brand_analytics_runs (library_id);

-- brand_analytics_items: default view sorts by BSR; AI Fill scans by status;
-- identifiers (asin/upc/ean/mpn/gtin) are all user-facing lookup keys.
CREATE INDEX ix_brand_analytics_items_run_bsr   ON analytics.brand_analytics_items (run_id, bsr);
CREATE INDEX ix_brand_analytics_items_ai_fill   ON analytics.brand_analytics_items (run_id, ai_fill_status);
CREATE INDEX ix_brand_analytics_items_asin      ON analytics.brand_analytics_items (asin);
CREATE INDEX ix_brand_analytics_items_upc        ON analytics.brand_analytics_items (upc);
CREATE INDEX ix_brand_analytics_items_ean        ON analytics.brand_analytics_items (ean);
CREATE INDEX ix_brand_analytics_items_mpn        ON analytics.brand_analytics_items (mpn);
CREATE INDEX ix_brand_analytics_items_gtin       ON analytics.brand_analytics_items (gtin);
-- NOTE: ai_upc/ai_ean/ai_mpn/ai_gtin are derived values, rarely filtered → not indexed.
GO


/* ===========================================================================
   10.  OPTIONAL: live-accurate scan stats view
        (use instead of the denormalised scans.*_count columns if you prefer)
   =========================================================================== */
IF OBJECT_ID('analytics.scan_stats', 'V') IS NOT NULL
    DROP VIEW analytics.scan_stats;
GO
CREATE VIEW analytics.scan_stats AS
    SELECT
        scan_id,
        COUNT(CASE WHEN LOWER(REPLACE(verdict,'_',' ')) IN ('approved','verified')      THEN 1 END) AS verified_count,
        COUNT(CASE WHEN LOWER(REPLACE(verdict,'_',' ')) = 'review'                       THEN 1 END) AS review_count,
        COUNT(CASE WHEN LOWER(REPLACE(verdict,'_',' ')) IN ('not approved','not verified') THEN 1 END) AS not_approved_count,
        COUNT(CASE WHEN review_status <> ''                                              THEN 1 END) AS reviewed_count
    FROM analytics.scan_results
    GROUP BY scan_id;
GO


/* ===========================================================================
   11.  SEED — static data
   =========================================================================== */

-- settings: verdict thresholds (idempotent).
IF NOT EXISTS (SELECT 1 FROM analytics.settings WHERE [key] = 'threshold_verified')
    INSERT INTO analytics.settings ([key],[value]) VALUES ('threshold_verified','85');
IF NOT EXISTS (SELECT 1 FROM analytics.settings WHERE [key] = 'threshold_review')
    INSERT INTO analytics.settings ([key],[value]) VALUES ('threshold_review','35');
GO

-- abbreviation_entries: default 'system' library (generated from the live DB).
-- Each INSERT is guarded so re-running the script never duplicates a (category,abbr).
-- 208 default entries seeded below (idempotent via NOT EXISTS on the (category, abbr) business key).
INSERT INTO analytics.abbreviation_entries (category, abbr, full_form, added_by)
SELECT v.category, v.abbr, v.full_form, 'system'
FROM (VALUES
    (N'Colors', N'beige', N'beige'),
    (N'Colors', N'black', N'black'),
    (N'Colors', N'blue', N'blue'),
    (N'Colors', N'brown', N'brown'),
    (N'Colors', N'clear', N'clear'),
    (N'Colors', N'cream', N'cream'),
    (N'Colors', N'gold', N'gold'),
    (N'Colors', N'gray', N'gray'),
    (N'Colors', N'green', N'green'),
    (N'Colors', N'grey', N'grey'),
    (N'Colors', N'ivory', N'ivory'),
    (N'Colors', N'navy', N'navy'),
    (N'Colors', N'orange', N'orange'),
    (N'Colors', N'pink', N'pink'),
    (N'Colors', N'purple', N'purple'),
    (N'Colors', N'red', N'red'),
    (N'Colors', N'silver', N'silver'),
    (N'Colors', N'transparent', N'transparent'),
    (N'Colors', N'white', N'white'),
    (N'Colors', N'yellow', N'yellow'),
    (N'Flavors', N'bubblegum', N'bubblegum'),
    (N'Flavors', N'cherry', N'cherry'),
    (N'Flavors', N'chocolate', N'chocolate'),
    (N'Flavors', N'citrus', N'citrus'),
    (N'Flavors', N'grape', N'grape'),
    (N'Flavors', N'lemon', N'lemon'),
    (N'Flavors', N'mint', N'mint'),
    (N'Flavors', N'mixed berry', N'mixed berry'),
    (N'Flavors', N'orange', N'orange'),
    (N'Flavors', N'original', N'original'),
    (N'Flavors', N'spearmint', N'spearmint'),
    (N'Flavors', N'strawberry', N'strawberry'),
    (N'Flavors', N'unflavored', N'unflavored'),
    (N'Flavors', N'vanilla', N'vanilla'),
    (N'Flavors', N'watermelon', N'watermelon'),
    (N'Forms', N'balm', N'balm'),
    (N'Forms', N'bar', N'bar'),
    (N'Forms', N'capsule', N'capsule'),
    (N'Forms', N'cream', N'cream'),
    (N'Forms', N'drop', N'drop'),
    (N'Forms', N'drops', N'drops'),
    (N'Forms', N'emulsion', N'emulsion'),
    (N'Forms', N'foam', N'foam'),
    (N'Forms', N'gel', N'gel'),
    (N'Forms', N'liquid', N'liquid'),
    (N'Forms', N'lotion', N'lotion'),
    (N'Forms', N'oil', N'oil'),
    (N'Forms', N'ointment', N'ointment'),
    (N'Forms', N'paste', N'paste'),
    (N'Forms', N'patch', N'patch'),
    (N'Forms', N'powder', N'powder'),
    (N'Forms', N'serum', N'serum'),
    (N'Forms', N'solution', N'solution'),
    (N'Forms', N'spray', N'spray'),
    (N'Forms', N'stick', N'stick'),
    (N'Forms', N'strip', N'strip'),
    (N'Forms', N'suspension', N'suspension'),
    (N'Forms', N'tablet', N'tablet'),
    (N'Forms', N'wipe', N'wipe'),
    (N'Materials', N'bamboo', N'bamboo'),
    (N'Materials', N'cotton', N'cotton'),
    (N'Materials', N'elastic', N'elastic'),
    (N'Materials', N'fabric', N'fabric'),
    (N'Materials', N'foam', N'foam'),
    (N'Materials', N'latex', N'latex'),
    (N'Materials', N'lycra', N'lycra'),
    (N'Materials', N'microfiber', N'microfiber'),
    (N'Materials', N'nitrile', N'nitrile'),
    (N'Materials', N'non-woven', N'non-woven'),
    (N'Materials', N'nonwoven', N'nonwoven'),
    (N'Materials', N'nylon', N'nylon'),
    (N'Materials', N'paper', N'paper'),
    (N'Materials', N'plastic', N'plastic'),
    (N'Materials', N'polyester', N'polyester'),
    (N'Materials', N'silicone', N'silicone'),
    (N'Materials', N'spandex', N'spandex'),
    (N'Materials', N'vinyl', N'vinyl'),
    (N'Materials', N'woven', N'woven'),
    (N'Packaging', N'blister pack', N'blister pack'),
    (N'Packaging', N'bulk', N'bulk'),
    (N'Packaging', N'canister', N'canister'),
    (N'Packaging', N'clamshell', N'clamshell'),
    (N'Packaging', N'dispenser', N'dispenser'),
    (N'Packaging', N'dropper', N'dropper'),
    (N'Packaging', N'flip cap', N'flip cap'),
    (N'Packaging', N'individually wrapped', N'individually wrapped'),
    (N'Packaging', N'jar', N'jar'),
    (N'Packaging', N'pump', N'pump'),
    (N'Packaging', N'resealable', N'resealable'),
    (N'Packaging', N'retail', N'retail'),
    (N'Packaging', N'sealed', N'sealed'),
    (N'Packaging', N'tub', N'tub'),
    (N'Packaging', N'twist cap', N'twist cap'),
    (N'Packaging', N'zip lock', N'zip lock'),
    (N'Product Attributes', N'BPA free', N'BPA free'),
    (N'Product Attributes', N'BPA-free', N'BPA-free'),
    (N'Product Attributes', N'absorbent', N'absorbent'),
    (N'Product Attributes', N'alcohol free', N'alcohol free'),
    (N'Product Attributes', N'alcohol-free', N'alcohol-free'),
    (N'Product Attributes', N'antibacterial', N'antibacterial'),
    (N'Product Attributes', N'antifungal', N'antifungal'),
    (N'Product Attributes', N'antimicrobial', N'antimicrobial'),
    (N'Product Attributes', N'breathable', N'breathable'),
    (N'Product Attributes', N'extra strength', N'extra strength'),
    (N'Product Attributes', N'gentle', N'gentle'),
    (N'Product Attributes', N'heavy duty', N'heavy duty'),
    (N'Product Attributes', N'hypoallergenic', N'hypoallergenic'),
    (N'Product Attributes', N'latex free', N'latex free'),
    (N'Product Attributes', N'latex-free', N'latex-free'),
    (N'Product Attributes', N'maximum strength', N'maximum strength'),
    (N'Product Attributes', N'oil free', N'oil free'),
    (N'Product Attributes', N'oil-free', N'oil-free'),
    (N'Product Attributes', N'regular strength', N'regular strength'),
    (N'Product Attributes', N'sensitive', N'sensitive'),
    (N'Product Attributes', N'ultra absorbent', N'ultra absorbent'),
    (N'Product Attributes', N'ultra thin', N'ultra thin'),
    (N'Product Attributes', N'water resistant', N'water resistant'),
    (N'Product Attributes', N'waterproof', N'waterproof'),
    (N'Scents', N'aloe', N'aloe'),
    (N'Scents', N'chamomile', N'chamomile'),
    (N'Scents', N'citrus', N'citrus'),
    (N'Scents', N'coconut', N'coconut'),
    (N'Scents', N'eucalyptus', N'eucalyptus'),
    (N'Scents', N'fragrance free', N'fragrance free'),
    (N'Scents', N'fragrance-free', N'fragrance-free'),
    (N'Scents', N'lavender', N'lavender'),
    (N'Scents', N'lemon', N'lemon'),
    (N'Scents', N'mint', N'mint'),
    (N'Scents', N'peppermint', N'peppermint'),
    (N'Scents', N'rose', N'rose'),
    (N'Scents', N'tea tree', N'tea tree'),
    (N'Scents', N'unscented', N'unscented'),
    (N'Scents', N'vanilla', N'vanilla'),
    (N'Sizes', N'L', N'L'),
    (N'Sizes', N'M', N'M'),
    (N'Sizes', N'OS', N'OS'),
    (N'Sizes', N'S', N'S'),
    (N'Sizes', N'XL', N'XL'),
    (N'Sizes', N'XS', N'XS'),
    (N'Sizes', N'XXL', N'XXL'),
    (N'Sizes', N'XXXL', N'XXXL'),
    (N'Sizes', N'extra large', N'extra large'),
    (N'Sizes', N'lge', N'lge'),
    (N'Sizes', N'med', N'med'),
    (N'Sizes', N'one size', N'one size'),
    (N'Sizes', N'petite', N'petite'),
    (N'Sizes', N'plus', N'plus'),
    (N'Sizes', N'regular', N'regular'),
    (N'Sizes', N'sm', N'sm'),
    (N'Sterility', N'disposable', N'disposable'),
    (N'Sterility', N'individually wrapped', N'individually wrapped'),
    (N'Sterility', N'non sterile', N'non sterile'),
    (N'Sterility', N'non-sterile', N'non-sterile'),
    (N'Sterility', N'single use', N'single use'),
    (N'Sterility', N'single-use', N'single-use'),
    (N'Sterility', N'sterile', N'sterile'),
    (N'Sterility', N'steriled', N'steriled'),
    (N'UOMs', N'L', N'L'),
    (N'UOMs', N'bag', N'bag'),
    (N'UOMs', N'bags', N'bags'),
    (N'UOMs', N'bottle', N'bottle'),
    (N'UOMs', N'bottles', N'bottles'),
    (N'UOMs', N'box', N'box'),
    (N'UOMs', N'bx', N'bx'),
    (N'UOMs', N'case', N'case'),
    (N'UOMs', N'count', N'count'),
    (N'UOMs', N'cs', N'cs'),
    (N'UOMs', N'ct', N'ct'),
    (N'UOMs', N'cup', N'cup'),
    (N'UOMs', N'ea', N'ea'),
    (N'UOMs', N'each', N'each'),
    (N'UOMs', N'fl oz', N'fl oz'),
    (N'UOMs', N'g', N'g'),
    (N'UOMs', N'gal', N'gal'),
    (N'UOMs', N'gallon', N'gallon'),
    (N'UOMs', N'kg', N'kg'),
    (N'UOMs', N'lb', N'lb'),
    (N'UOMs', N'lbs', N'lbs'),
    (N'UOMs', N'mL', N'mL'),
    (N'UOMs', N'mg', N'mg'),
    (N'UOMs', N'ml', N'ml'),
    (N'UOMs', N'oz', N'oz'),
    (N'UOMs', N'pack', N'pack'),
    (N'UOMs', N'pad', N'pad'),
    (N'UOMs', N'pads', N'pads'),
    (N'UOMs', N'pair', N'pair'),
    (N'UOMs', N'pairs', N'pairs'),
    (N'UOMs', N'piece', N'piece'),
    (N'UOMs', N'pieces', N'pieces'),
    (N'UOMs', N'pint', N'pint'),
    (N'UOMs', N'pk', N'pk'),
    (N'UOMs', N'pouch', N'pouch'),
    (N'UOMs', N'pouches', N'pouches'),
    (N'UOMs', N'pt', N'pt'),
    (N'UOMs', N'qt', N'qt'),
    (N'UOMs', N'quart', N'quart'),
    (N'UOMs', N'roll', N'roll'),
    (N'UOMs', N'rolls', N'rolls'),
    (N'UOMs', N'sheet', N'sheet'),
    (N'UOMs', N'sheets', N'sheets'),
    (N'UOMs', N'tbsp', N'tbsp'),
    (N'UOMs', N'tsp', N'tsp'),
    (N'UOMs', N'tube', N'tube'),
    (N'UOMs', N'tubes', N'tubes'),
    (N'UOMs', N'unit', N'unit'),
    (N'UOMs', N'units', N'units'),
    (N'UOMs', N'wipe', N'wipe'),
    (N'UOMs', N'wipes', N'wipes')
) AS v(category, abbr, full_form)
WHERE NOT EXISTS (SELECT 1 FROM analytics.abbreviation_entries e
                  WHERE e.category = v.category AND e.abbr = v.abbr);
GO

