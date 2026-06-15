-- ============================================================
-- catalog-verifier  —  Azure SQL Database schema
-- ============================================================
-- Clean-start schema for Azure SQL (T-SQL).
-- All SQLite additive migrations are baked in.
-- All schema optimisations applied (redundant indexes removed,
-- missing indexes added, FK fixed, types normalised).
--
-- Run order: execute the full file once against a fresh DB.
-- Every statement is idempotent (IF OBJECT_ID / IF NOT EXISTS).
--
-- Changes vs SQLite schema
-- ────────────────────────
-- REMOVED (redundant indexes):
--   idx_scan_catalog_rows_scan, idx_scan_amazon_rows_scan,
--   idx_scan_results_scan, idx_scan_candidates_scan,
--   idx_analytics_catalog_run
--
-- ADDED (missing indexes):
--   idx_analytics_candidates_verdict  (run_id, verdict, confidence DESC)
--   idx_analytics_candidates_ai       (run_id, ai_verdict)
--   idx_analytics_candidates_rank     (run_id, sales_rank)
--   idx_brand_items_run_bsr           (run_id, bsr ASC)
--   idx_brand_items_ai_fill           (run_id, ai_fill_status)
--   idx_brand_library_type            (entity_type)
--   idx_brand_library_parent          (parent_manufacturer)
--   idx_verified_upc / idx_verified_asin
--   idx_keepa_imported / idx_amazon_imported / idx_global_asin_updated
--   idx_scans_status
--   idx_analytics_runs_status
--   idx_brand_runs_status
--   idx_scan_results_verdict          (scan_id, verdict)
--
-- FIXED:
--   brand_analytics_items — FK to brand_analytics_runs was missing
--     (cascade delete silently failed in SQLite)
--
-- TYPE CHANGES:
--   INTEGER  → INT  (or BIT for 0/1 flags)
--   TEXT     → NVARCHAR(MAX) or sized NVARCHAR where appropriate
--   REAL     → FLOAT
--   TIMESTAMP / TEXT timestamps → DATETIME2(0)
--   CURRENT_TIMESTAMP → GETUTCDATE()
--   AUTOINCREMENT → IDENTITY(1,1)
-- ============================================================


-- ============================================================
-- 1.  SETTINGS  (must exist before seed inserts below)
-- ============================================================

IF OBJECT_ID('dbo.settings', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.settings (
        [key]   NVARCHAR(200) NOT NULL,
        [value] NVARCHAR(MAX)     NULL,
        CONSTRAINT PK_settings PRIMARY KEY ([key])
    );
END;

-- Seed defaults (idempotent)
IF NOT EXISTS (SELECT 1 FROM dbo.settings WHERE [key] = 'threshold_verified')
    INSERT INTO dbo.settings ([key],[value]) VALUES ('threshold_verified','85');
IF NOT EXISTS (SELECT 1 FROM dbo.settings WHERE [key] = 'threshold_review')
    INSERT INTO dbo.settings ([key],[value]) VALUES ('threshold_review','35');


-- ============================================================
-- 2.  LEGACY GLOBAL CACHES
-- ============================================================

IF OBJECT_ID('dbo.keepa_imports', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.keepa_imports (
        asin        NVARCHAR(20)  NOT NULL,
        data_json   NVARCHAR(MAX) NOT NULL,
        imported_at DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT PK_keepa_imports PRIMARY KEY (asin)
    );
    -- Supports TTL purge: DELETE WHERE imported_at < DATEADD(day,-90,GETUTCDATE())
    CREATE INDEX idx_keepa_imported ON dbo.keepa_imports (imported_at);
END;

IF OBJECT_ID('dbo.amazon_imports', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.amazon_imports (
        asin        NVARCHAR(20)  NOT NULL,
        data_json   NVARCHAR(MAX) NOT NULL,
        imported_at DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT PK_amazon_imports PRIMARY KEY (asin)
    );
    CREATE INDEX idx_amazon_imported ON dbo.amazon_imports (imported_at);
END;

IF OBJECT_ID('dbo.attribute_cache', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.attribute_cache (
        upc             NVARCHAR(20)  NOT NULL,
        asin            NVARCHAR(20)  NOT NULL,
        attributes_json NVARCHAR(MAX) NOT NULL,
        [source]        NVARCHAR(100)     NULL,
        last_updated    DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT PK_attribute_cache PRIMARY KEY (upc, asin)
    );
END;

IF OBJECT_ID('dbo.global_asin_cache', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.global_asin_cache (
        asin       NVARCHAR(20)  NOT NULL,
        data_json  NVARCHAR(MAX) NOT NULL,
        updated_at DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT PK_global_asin_cache PRIMARY KEY (asin)
    );
    -- Supports TTL purge
    CREATE INDEX idx_global_asin_updated ON dbo.global_asin_cache (updated_at);
END;


-- ============================================================
-- 3.  PAIR STORE  (blacklist + verified)
-- ============================================================

IF OBJECT_ID('dbo.blacklisted_pairs', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.blacklisted_pairs (
        upc              NVARCHAR(20)  NOT NULL,
        asin             NVARCHAR(20)  NOT NULL,
        confidence_score FLOAT             NULL,
        failed_signals   NVARCHAR(MAX)     NULL,
        date_blacklisted DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT PK_blacklisted_pairs PRIMARY KEY (upc, asin)
    );
    -- Single-column lookups (e.g. "all pairs for this UPC")
    CREATE INDEX idx_bl_upc  ON dbo.blacklisted_pairs (upc);
    CREATE INDEX idx_bl_asin ON dbo.blacklisted_pairs (asin);
END;

IF OBJECT_ID('dbo.verified_items', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.verified_items (
        upc           NVARCHAR(20)  NOT NULL,
        asin          NVARCHAR(20)  NOT NULL,
        data_json     NVARCHAR(MAX) NOT NULL,
        review_status NVARCHAR(200) NOT NULL DEFAULT '',
        verified_at   DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT PK_verified_items PRIMARY KEY (upc, asin)
    );
    -- FIX: these were missing in the SQLite schema
    CREATE INDEX idx_verified_upc  ON dbo.verified_items (upc);
    CREATE INDEX idx_verified_asin ON dbo.verified_items (asin);
END;


-- ============================================================
-- 4.  ABBREVIATION LIBRARY
-- ============================================================

IF OBJECT_ID('dbo.abbreviation_library', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.abbreviation_library (
        id         INT           NOT NULL IDENTITY(1,1),
        category   NVARCHAR(100) NOT NULL,
        abbr       NVARCHAR(200) NOT NULL,
        full_form  NVARCHAR(500) NOT NULL,
        added_by   NVARCHAR(50)  NOT NULL DEFAULT 'system',
        created_at DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT PK_abbreviation_library PRIMARY KEY (id),
        -- The unique index on (category, abbr) also covers ORDER BY category, abbr
        CONSTRAINT UQ_abbreviation_library UNIQUE (category, abbr)
    );
END;


-- ============================================================
-- 5.  VERIFY FLOW
-- ============================================================

IF OBJECT_ID('dbo.scans', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.scans (
        id                  INT           NOT NULL IDENTITY(1,1),
        name                NVARCHAR(500) NOT NULL,
        marketplace         NVARCHAR(10)  NOT NULL DEFAULT 'US',
        condition           NVARCHAR(50)  NOT NULL DEFAULT 'New',
        status              NVARCHAR(50)  NOT NULL DEFAULT 'pending',
        mapping_json        NVARCHAR(MAX)     NULL,
        catalog_filename    NVARCHAR(500)     NULL,
        catalog_count       INT           NOT NULL DEFAULT 0,
        amazon_filename     NVARCHAR(500)     NULL,
        amazon_source       NVARCHAR(100)     NULL,
        amazon_count        INT           NOT NULL DEFAULT 0,
        ai_mode             BIT           NOT NULL DEFAULT 0,
        match_from_keepa    BIT           NOT NULL DEFAULT 0,
        match_methods       NVARCHAR(MAX)     NULL,   -- JSON array e.g. ["upc","item_id"]
        verified_count      INT           NOT NULL DEFAULT 0,
        review_count        INT           NOT NULL DEFAULT 0,
        not_approved_count  INT           NOT NULL DEFAULT 0,
        reviewed_count      INT           NOT NULL DEFAULT 0,
        exported_at         DATETIME2(0)      NULL,
        created_at          DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        updated_at          DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT PK_scans PRIMARY KEY (id)
    );
    CREATE INDEX idx_scans_created ON dbo.scans (created_at DESC);
    CREATE INDEX idx_scans_status  ON dbo.scans (status);
END;

IF OBJECT_ID('dbo.scan_catalog_rows', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.scan_catalog_rows (
        scan_id   INT           NOT NULL,
        row_idx   INT           NOT NULL,
        data_json NVARCHAR(MAX) NOT NULL,
        CONSTRAINT PK_scan_catalog_rows PRIMARY KEY (scan_id, row_idx),
        CONSTRAINT FK_scan_catalog_scan
            FOREIGN KEY (scan_id) REFERENCES dbo.scans(id) ON DELETE CASCADE
    );
    -- NOTE: no separate index on scan_id — the clustered PK (scan_id, row_idx)
    -- already handles WHERE scan_id = ? efficiently (leading-column range scan).
END;

IF OBJECT_ID('dbo.scan_amazon_rows', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.scan_amazon_rows (
        scan_id   INT           NOT NULL,
        asin      NVARCHAR(20)  NOT NULL,
        data_json NVARCHAR(MAX) NOT NULL,
        CONSTRAINT PK_scan_amazon_rows PRIMARY KEY (scan_id, asin),
        CONSTRAINT FK_scan_amazon_scan
            FOREIGN KEY (scan_id) REFERENCES dbo.scans(id) ON DELETE CASCADE
    );
    -- NOTE: same — clustered PK covers WHERE scan_id = ?
END;

IF OBJECT_ID('dbo.scan_results', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.scan_results (
        scan_id       INT           NOT NULL,
        row_idx       INT           NOT NULL,
        upc           NVARCHAR(20)      NULL,
        asin          NVARCHAR(20)      NULL,
        verdict       NVARCHAR(50)      NULL,
        score         FLOAT             NULL,
        review_status NVARCHAR(200) NOT NULL DEFAULT '',
        data_json     NVARCHAR(MAX) NOT NULL,
        CONSTRAINT PK_scan_results PRIMARY KEY (scan_id, row_idx),
        CONSTRAINT FK_scan_results_scan
            FOREIGN KEY (scan_id) REFERENCES dbo.scans(id) ON DELETE CASCADE
    );
    -- Pair-store cross-reference (is this UPC+ASIN already verified/blacklisted?)
    CREATE INDEX idx_scan_results_upc_asin ON dbo.scan_results (upc, asin);
    -- Verdict filter (used by recompute_scan_stats and future server-side filtering)
    CREATE INDEX idx_scan_results_verdict  ON dbo.scan_results (scan_id, verdict);
END;

IF OBJECT_ID('dbo.scan_candidates', 'U') IS NULL
BEGIN
    -- Match-from-Keepa mode: up to 8 candidates per catalog row
    CREATE TABLE dbo.scan_candidates (
        id            INT           NOT NULL IDENTITY(1,1),
        scan_id       INT           NOT NULL,
        row_idx       INT           NOT NULL,
        asin          NVARCHAR(20)  NOT NULL,
        confidence    FLOAT         NOT NULL DEFAULT 0,
        verdict       NVARCHAR(50)  NOT NULL DEFAULT 'review',
        review_status NVARCHAR(200) NOT NULL DEFAULT '',
        match_method  NVARCHAR(100)     NULL,
        data_json     NVARCHAR(MAX) NOT NULL,
        CONSTRAINT PK_scan_candidates PRIMARY KEY (id),
        CONSTRAINT UQ_scan_candidates UNIQUE (scan_id, row_idx, asin),
        CONSTRAINT FK_scan_candidates_scan
            FOREIGN KEY (scan_id) REFERENCES dbo.scans(id) ON DELETE CASCADE
    );
    -- (scan_id, row_idx) — sibling discard: UPDATE … WHERE scan_id=? AND row_idx=? AND id!=?
    CREATE INDEX idx_scan_candidates_row  ON dbo.scan_candidates (scan_id, row_idx);
    -- (scan_id, asin)    — uniqueness check: has this ASIN been approved in this scan?
    CREATE INDEX idx_scan_candidates_asin ON dbo.scan_candidates (scan_id, asin);
    -- NOTE: a scan_id-only index is NOT needed — idx_scan_candidates_row covers it.
END;


-- ============================================================
-- 6.  ANALYTICS (ROI & COST)
-- ============================================================

IF OBJECT_ID('dbo.analytics_runs', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.analytics_runs (
        id                     INT           NOT NULL IDENTITY(1,1),
        name                   NVARCHAR(500)     NULL,
        marketplace            NVARCHAR(10)  NOT NULL DEFAULT 'US',
        search_methods         NVARCHAR(MAX)     NULL,   -- JSON array ["UPC","ItemID","Title"]
        pages_per_title        INT           NOT NULL DEFAULT 5,
        ai_clean_titles        BIT           NOT NULL DEFAULT 0,
        vetting_mode           NVARCHAR(20)  NOT NULL DEFAULT 'cpg',   -- 'cpg' | 'medical'
        total_catalog_items    INT           NOT NULL DEFAULT 0,
        total_candidates_found INT           NOT NULL DEFAULT 0,
        verified_count         INT           NOT NULL DEFAULT 0,
        review_count           INT           NOT NULL DEFAULT 0,
        not_approved_count     INT           NOT NULL DEFAULT 0,
        min_rank               INT           NOT NULL DEFAULT 0,
        max_rank               INT           NOT NULL DEFAULT 0,
        brand_col              NVARCHAR(200) NOT NULL DEFAULT '',
        brand_mode             NVARCHAR(20)  NOT NULL DEFAULT 'col',   -- 'col' | 'text'
        status                 NVARCHAR(50)  NOT NULL DEFAULT 'Pending',
        progress_phase         NVARCHAR(100)     NULL,
        progress_done          INT           NOT NULL DEFAULT 0,
        progress_total         INT           NOT NULL DEFAULT 0,
        ai_check_status        NVARCHAR(50)      NULL,
        ai_check_done          INT           NOT NULL DEFAULT 0,
        ai_check_total         INT           NOT NULL DEFAULT 0,
        created_at             DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        updated_at             DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT PK_analytics_runs PRIMARY KEY (id)
    );
    CREATE INDEX idx_analytics_runs_created ON dbo.analytics_runs (created_at DESC);
    CREATE INDEX idx_analytics_runs_status  ON dbo.analytics_runs (status);
END;

IF OBJECT_ID('dbo.analytics_catalog_rows', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.analytics_catalog_rows (
        run_id         INT           NOT NULL,
        row_idx        INT           NOT NULL,
        data_json      NVARCHAR(MAX) NOT NULL,
        extracted_json NVARCHAR(MAX)     NULL,   -- AI-extracted brand/product fields
        CONSTRAINT PK_analytics_catalog_rows PRIMARY KEY (run_id, row_idx),
        CONSTRAINT FK_analytics_catalog_run
            FOREIGN KEY (run_id) REFERENCES dbo.analytics_runs(id) ON DELETE CASCADE
    );
    -- NOTE: clustered PK (run_id, row_idx) covers WHERE run_id = ? — no extra index needed.
END;

IF OBJECT_ID('dbo.analytics_candidates', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.analytics_candidates (
        run_id        INT           NOT NULL,
        row_idx       INT           NOT NULL,
        asin          NVARCHAR(20)  NOT NULL,
        sources       NVARCHAR(MAX)     NULL,   -- JSON array e.g. ["UPC","Title"]
        confidence    FLOAT             NULL,
        verdict       NVARCHAR(50)      NULL,   -- 'verified'|'review'|'not_approved'
        amz_pack      INT               NULL,
        review_status NVARCHAR(200) NOT NULL DEFAULT '',
        sales_rank    INT               NULL,
        ai_verdict    NVARCHAR(20)      NULL,   -- 'approve'|'reject'|'uncertain'
        ai_reasoning  NVARCHAR(300)     NULL,
        data_json     NVARCHAR(MAX) NOT NULL,
        created_at    DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT PK_analytics_candidates PRIMARY KEY (run_id, row_idx, asin),
        CONSTRAINT FK_analytics_candidates_run
            FOREIGN KEY (run_id) REFERENCES dbo.analytics_runs(id) ON DELETE CASCADE
    );
    -- ★ CRITICAL: covers every tab-filter query (Approved/Review/Not Approved)
    --   + eliminates filesort on confidence.  This replaces the old run_id-only index.
    CREATE INDEX idx_analytics_candidates_verdict
        ON dbo.analytics_candidates (run_id, verdict, confidence DESC);
    -- AI check filter: WHERE run_id=? AND ai_verdict IS NOT NULL
    CREATE INDEX idx_analytics_candidates_ai
        ON dbo.analytics_candidates (run_id, ai_verdict);
    -- Rank-window filters during result browsing/export.
    CREATE INDEX idx_analytics_candidates_rank
        ON dbo.analytics_candidates (run_id, sales_rank);
END;


-- ============================================================
-- 7.  BRAND ANALYTICS
-- ============================================================

IF OBJECT_ID('dbo.brand_library', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.brand_library (
        id                  INT           NOT NULL IDENTITY(1,1),
        entity_type         NVARCHAR(50)  NOT NULL,               -- 'brand'|'manufacturer'
        name                NVARCHAR(500) NOT NULL,
        parent_manufacturer NVARCHAR(500)     NULL,
        sub_brands          NVARCHAR(MAX) NOT NULL DEFAULT '[]',  -- JSON array
        aliases             NVARCHAR(MAX) NOT NULL DEFAULT '[]',  -- JSON array
        discovered_by       NVARCHAR(20)  NOT NULL DEFAULT 'user',-- 'ai'|'user'
        created_at          DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        updated_at          DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT PK_brand_library PRIMARY KEY (id),
        CONSTRAINT UQ_brand_library_name UNIQUE (name)
    );
    -- Library panel tabs: WHERE entity_type = 'brand' / 'manufacturer'
    CREATE INDEX idx_brand_library_type   ON dbo.brand_library (entity_type);
    -- Manufacturer hierarchy grouping
    CREATE INDEX idx_brand_library_parent ON dbo.brand_library (parent_manufacturer);
END;

IF OBJECT_ID('dbo.brand_analytics_runs', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.brand_analytics_runs (
        id                   INT           NOT NULL IDENTITY(1,1),
        name                 NVARCHAR(500) NOT NULL,
        search_type          NVARCHAR(50)  NOT NULL,  -- 'brand'|'manufacturer'
        search_terms         NVARCHAR(MAX) NOT NULL,  -- JSON array of brand strings searched
        library_id           INT               NULL,  -- FK to brand_library (nullable)
        vetting_mode         NVARCHAR(20)  NOT NULL DEFAULT 'cpg',
        status               NVARCHAR(50)  NOT NULL DEFAULT 'Pending',
        progress_phase       NVARCHAR(100)     NULL,
        progress_done        INT           NOT NULL DEFAULT 0,
        progress_total       INT           NOT NULL DEFAULT 0,
        min_rank             INT           NOT NULL DEFAULT 0,
        max_rank             INT           NOT NULL DEFAULT 0,
        pages_per_brand      INT           NOT NULL DEFAULT 3,
        last_asin_updated_at DATETIME2(0)      NULL,  -- cache freshness check
        ai_fill_status       NVARCHAR(50)      NULL,
        ai_fill_done         INT           NOT NULL DEFAULT 0,
        ai_fill_total        INT           NOT NULL DEFAULT 0,
        created_at           DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        updated_at           DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT PK_brand_analytics_runs PRIMARY KEY (id)
    );
    CREATE INDEX idx_brand_runs_created ON dbo.brand_analytics_runs (created_at DESC);
    CREATE INDEX idx_brand_runs_status  ON dbo.brand_analytics_runs (status);
END;

IF OBJECT_ID('dbo.brand_analytics_items', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.brand_analytics_items (
        id             INT           NOT NULL IDENTITY(1,1),
        run_id         INT           NOT NULL,
        brand_searched NVARCHAR(500) NOT NULL,
        asin           NVARCHAR(20)  NOT NULL,
        title          NVARCHAR(MAX)     NULL,
        bsr            INT               NULL,
        bsr_category   NVARCHAR(200)     NULL,
        mpn            NVARCHAR(200)     NULL,
        upc            NVARCHAR(20)      NULL,
        ean            NVARCHAR(20)      NULL,
        gtin           NVARCHAR(20)      NULL,
        image_url      NVARCHAR(MAX)     NULL,
        ai_mpn         NVARCHAR(200)     NULL,
        ai_upc         NVARCHAR(20)      NULL,
        ai_ean         NVARCHAR(20)      NULL,
        ai_gtin        NVARCHAR(20)      NULL,
        ai_fill_status NVARCHAR(50)      NULL,  -- NULL | 'done' | 'error'
        pack_qty       NVARCHAR(50)      NULL,
        uom_qty        NVARCHAR(50)      NULL,
        data_json      NVARCHAR(MAX) NOT NULL DEFAULT '{}',
        updated_at     DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT PK_brand_analytics_items PRIMARY KEY (id),
        CONSTRAINT UQ_brand_analytics_items UNIQUE (run_id, asin),
        -- FIX: this FK was missing in the SQLite schema — cascade delete silently failed
        CONSTRAINT FK_brand_items_run
            FOREIGN KEY (run_id) REFERENCES dbo.brand_analytics_runs(id) ON DELETE CASCADE
    );
    -- ★ Covers WHERE run_id=? ORDER BY bsr (default brand run view)
    CREATE INDEX idx_brand_items_run_bsr
        ON dbo.brand_analytics_items (run_id, bsr ASC);
    -- AI Fill: WHERE run_id=? AND (ai_fill_status IS NULL OR ai_fill_status != 'done')
    CREATE INDEX idx_brand_items_ai_fill
        ON dbo.brand_analytics_items (run_id, ai_fill_status);
END;

IF OBJECT_ID('dbo.asin_identifier_overrides', 'U') IS NULL
BEGIN
    -- Global per-ASIN user corrections (persist across all brand runs)
    CREATE TABLE dbo.asin_identifier_overrides (
        asin       NVARCHAR(20)  NOT NULL,
        mpn        NVARCHAR(200)     NULL,
        upc        NVARCHAR(20)      NULL,
        ean        NVARCHAR(20)      NULL,
        gtin       NVARCHAR(20)      NULL,
        updated_at DATETIME2(0)  NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT PK_asin_identifier_overrides PRIMARY KEY (asin)
    );
END;


-- ============================================================
-- 8.  SCAN_STATS VIEW  (replaces denormalized counter columns)
-- ============================================================
-- Optional: if you want live-accurate stats without maintaining
-- counter columns on dbo.scans, create this view and read from
-- it instead of scans.verified_count etc.
--
-- Usage:  SELECT * FROM dbo.scan_stats WHERE scan_id = @id
-- ============================================================

IF OBJECT_ID('dbo.scan_stats', 'V') IS NULL
    EXEC('
        CREATE VIEW dbo.scan_stats AS
        SELECT
            scan_id,
            COUNT(CASE WHEN LOWER(REPLACE(verdict, ''_'', '' '')) IN (''approved'', ''verified'')
                            THEN 1 END)                 AS verified_count,
            COUNT(CASE WHEN LOWER(REPLACE(verdict, ''_'', '' '')) = ''review''
                            THEN 1 END)                 AS review_count,
            COUNT(CASE WHEN LOWER(REPLACE(verdict, ''_'', '' '')) IN (''not approved'',''not verified'')
                            THEN 1 END)                 AS not_approved_count,
            COUNT(CASE WHEN review_status != ''''
                            THEN 1 END)                 AS reviewed_count
        FROM dbo.scan_results
        GROUP BY scan_id
    ');


-- ============================================================
-- END OF SCHEMA
-- ============================================================
--
-- APPLICATION-LEVEL NOTES (not schema, but required before go-live)
-- ─────────────────────────────────────────────────────────────────
-- 1. UPSERT pattern
--    SQLite "INSERT ... ON CONFLICT DO UPDATE" → T-SQL MERGE statement.
--    Every upsert in database.py must be rewritten as MERGE.
--
-- 2. LIMIT / OFFSET
--    SQLite "LIMIT n OFFSET m" → T-SQL "ORDER BY … OFFSET m ROWS FETCH NEXT n ROWS ONLY"
--    Requires an ORDER BY clause — add one if missing.
--
-- 3. Last inserted ID
--    SQLite "cur.lastrowid" → append "OUTPUT INSERTED.id" to the INSERT statement.
--
-- 4. PRAGMA statements
--    Remove all PRAGMA calls from _connect().  Azure SQL handles journaling,
--    foreign-key enforcement, and synchronisation internally.
--    Recommended SQLAlchemy engine settings instead:
--      pool_size=10, max_overflow=20, pool_pre_ping=True
--
-- 5. Python driver
--    pip install sqlalchemy pyodbc
--    Connection string (store in .env):
--      mssql+pyodbc://user:pass@server.database.windows.net/catalog_verifier
--        ?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no
--
-- 6. Row access
--    Replace sqlite3.Row name-access with SQLAlchemy result.mappings()
--    so row["column"] still works across all of database.py.
--
-- 7. _column_exists / PRAGMA table_info
--    Replace with INFORMATION_SCHEMA.COLUMNS query (see database.py refactor guide).
--
-- 8. TTL cache purge  (run on a schedule or on startup)
--    DELETE FROM dbo.global_asin_cache WHERE updated_at < DATEADD(day,-90,GETUTCDATE());
--    DELETE FROM dbo.keepa_imports      WHERE imported_at < DATEADD(day,-90,GETUTCDATE());
--    DELETE FROM dbo.amazon_imports     WHERE imported_at < DATEADD(day,-90,GETUTCDATE());
--
-- 9. COLLATION
--    Default Azure SQL collation is SQL_Latin1_General_CP1_CI_AS (case-insensitive).
--    All LOWER() calls in database.py are safe to remove but harmless if kept.
-- ============================================================
