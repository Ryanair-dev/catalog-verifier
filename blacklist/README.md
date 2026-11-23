# Marketplace Blacklist

Internal tool that replaces the `BlacklistData` sheet in the Restock workbooks. It tracks seller SKUs,
ASINs and brand codes that must not be bought for, or sent to, a channel (Amazon - Preferred Pharmacy
Plus, Amazon - Preferred Pharmacy Plus CA, Amazon - Turba, Amazon - Harper Wrens, Walmart, Temu, Ebay
and Target).

**Current state:** in-memory, loaded from `app/seed_data.json`, which is generated from
`vetter/files/RestockR5.xlsx`. Changes made in the app reset on restart. It will be backed by
`analytics.blacklist_listings` once on Azure SQL.

## Features

| Tab | What it does |
|---|---|
| Quick check | Look up a SKU, ASIN or brand code. Shows every store it's blocked on, *how* it matched (exact SKU or brand rule), the reason, notes, date and who added it. |
| Bulk check | Paste up to 5,000 identifiers (straight from Excel) or drop a CSV/TXT. Results are **Blacklisted / Review / Clear**, filterable, with a CSV download. |
| Browse | Search by SKU, ASIN, notes or initials. Filter by store, reason and type. Edit or delete entries, and export to CSV. |
| Add | Add one entry, or many identifiers across several stores at once. Existing entries are skipped, never overwritten. |

### Matching rules (`app/matching.py`)

| Match | When | Result |
|---|---|---|
| Exact | Input equals a blacklisted identifier (case-insensitive, trimmed) | Blacklisted |
| Brand rule | Input **starts with** a blacklisted brand code for that store (`PDI` → `PDI10254-FBA`). Amazon-generated `amzn.gr.<SKU>-…-LN` SKUs are unwrapped first. Only when an identifier type named `Brand prefix` exists. | Blacklisted |
| ASIN | Input is the ASIN recorded on a blacklisted SKU. Only when ASIN data exists (memory mode) and no type filter is set. | **Review**: another SKU may still legitimately sell that ASIN (e.g. "duplicated ASIN" rows) |

These rules reproduce the workbook's `BlackList` column. Checked against all 783 rows of
`RestockR5.xlsx › Data`, the app agrees on 782. The one difference is a spreadsheet bug (next section).

## What the Excel import found

`scripts/import_excel.py` reads `BlacklistData`: brand rules in A:C, SKU rules in E:J and stores in M.

- **28 brand rules and 2,541 SKU rows became 2,402 entries.** 167 rows repeated the same SKU and
  store, which the SQL `UNIQUE` constraint would reject. They are merged, with distinct notes joined
  by ` | ` and the earliest date kept.
- **Hidden whitespace in 26 SKUs** (leading line breaks, non-breaking spaces). Excel's exact match
  silently misses these. For example, `MMM2864-FBA4CA` is blacklisted on PPP CA, but the workbook
  shows it as sendable. The app trims them.
- **Brand formulas disagree between workbooks.** R5 uses `LEFT(sku,3)`, which never matches the
  4-letter brands `POWE` and `SKIL`. R4 uses `SEARCH()`, which matches a code anywhere in the SKU
  (`REM` inside `…PREMIER…`). The app uses a prefix match, which is correct for both.
- **Reasons are free text.** They're mapped into 14 reason codes by keyword (`REASONS` in the
  importer). The original note is kept word for word in `blacklist_notes`.
- **DateAdded / AddedBy exist on only 712 entries.** For the other 1,690 the app shows
  **"Not available"** rather than inventing a value. Anything missing from the sheet is shown that way.
- **Not imported (not blacklist data):** R4's `CustomData` tag lists (`{DNS}`, `{RES}`, `{HREV}`…) and
  the "se" SKU list ("Send until Finished", "do not send - better asins"). These are restock
  instructions, and the "se" list could become a separate status later.

Re-run it any time the sheet changes:

```powershell
.\.venv\Scripts\python scripts\import_excel.py ..\files\RestockR5.xlsx
```

## Schema additions (proposed)

`sql/proposed_changes.sql` has the DDL plus the reference data for the lookup tables.

| Change | Why |
|---|---|
| `asin VARCHAR(10) NULL` on `blacklist_listings` | 1,862 SKU rows carry an ASIN. It's needed for the ASIN "review" check and for searching. |
| `created_by NVARCHAR(100) NULL` | The sheet records AddedBy (LF, AA, PB…). Later this comes from the signed-in Entra user. |
| `created_at` / `updated_at` → `NULL` allowed | Imported rows without a DateAdded stay empty ("Not available"). The defaults still stamp new rows. |
| `pl_identifier_types`: Marketplace Fulfilled SKU, Listing ID, Merchant Fulfilled SKU | The types describe how a listing is fulfilled. Which identifiers each channel uses under each type is in `docs/identifier-types.md`. |
| `pl_channels`: 8 channel names | The Amazon stores are part of the name (`Amazon - Turba`); Walmart, Temu, Ebay and Target stand alone. No new column. |
| `pl_blacklist_reason_codes`: 14 codes | The values are in `data/lookups/pl_blacklist_reason_codes.csv`. |

## Azure SQL backend

`app/sql_repository.py` talks to the live `analytics.blacklist_listings`. It uses pure-Python `pytds` with an Entra
ID service principal (same approach as catalog-verifier), so no ODBC driver is needed.

- **Settings** live in `.env` (gitignored): `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`,
  `AZURE_SQL_SERVER_HOST`, `AZURE_SQL_SERVER_DATABASE`, and `BLACKLIST_BACKEND=sql` (or `memory` for the
  local Excel import).
- **The live schema differs from the original DDL.** `identifier_type_id` maps to the API's
  `identifier_type_code`. `blacklist_added_by_initials VARCHAR(4) NOT NULL` maps to `created_by`, so
  initials are required. `changed_by` / `changed_by_app` are filled by defaults, and the app connects
  as `blacklist-app`. The table is system-versioned, so every edit and delete is kept in
  `blacklist_listings_history`. There is **no ASIN column**, so ASIN "review" matches only work in
  memory mode.
- **Identifier type IDs are auto-generated**, so the app never relies on a fixed type number. Exact
  matching works for every identifier type, whatever its name. Brand-prefix rules apply only when a
  type named exactly `Brand prefix` exists (`BRAND_PREFIX_TYPE_NAME` in `app/matching.py`); without
  one they are skipped.
- **Lookup data:** edit `data/lookups/pl_channels.csv`, `pl_identifier_types.csv` and
  `pl_blacklist_reason_codes.csv` (or drop in `.xlsx` files with the same names). Then run
  `python scripts/load_lookups.py` for a write-free preview, and `--apply` to insert. Existing names
  are skipped. Each file has one column of names; the identifier type names can be anything.
- **Live test:** `python scripts/sql_smoke_test.py` runs 23 checks: CRUD, UNIQUE/FK errors,
  case-insensitive OPENJSON matching, brand rules and temporal history. Everything runs in one
  transaction that is **always rolled back**, and it confirms the row counts are unchanged afterwards.

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m uvicorn app.main:app --reload --port 8080
```

App: http://127.0.0.1:8080 · API docs: http://127.0.0.1:8080/docs · Tests: `.\.venv\Scripts\python -m pytest`

Deep links: `/?q=<SKU>` runs a quick check, `/#bulk` opens Browse in Check List mode, and `/#add` and `/#add-many` open the Add tab in that mode.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/lookups` | Stores, reason codes, identifier types |
| GET | `/api/blacklist?q=&channel_id=&blacklist_reason_code_id=&identifier_type_code=&page=&page_size=` | Search / browse |
| GET | `/api/blacklist/export.csv?…same filters…` | CSV export |
| GET | `/api/blacklist/{id}` | One entry |
| POST | `/api/blacklist` | Create (409 if it already exists) |
| PATCH | `/api/blacklist/{id}` | Update any field (`""` clears `asin` / `created_by`) |
| DELETE | `/api/blacklist/{id}` | Remove |
| POST | `/api/blacklist/check` | Check `{identifiers[], channel_id?, identifier_type_code?}` → status + matches with `matched_by` |
| POST | `/api/blacklist/bulk` | Bulk add `{identifiers[], identifier_type_code, channel_ids[], blacklist_reason_code_id, blacklist_notes, created_by?}` |
| POST | `/api/blacklist/bulk-rows` | Add many (the grid): `{rows[{identifier, identifier_type_code, channel_id, blacklist_reason_code_id, blacklist_notes?}], created_by}`. One result per row, in order: `created`, `existing` (not overwritten) or `error` |
| GET | `/healthz` | Health probe for App Service |

## Layout

```
app/
  main.py            FastAPI routes; get_repository() picks the data source
  models.py          Request/response schemas (field names = SQL column names)
  matching.py        Exact / brand-prefix / ASIN matching (shared by all repositories)
  repository.py      BlacklistRepository interface + InMemoryRepository
  seed_data.py/.json Pre-launch data imported from Excel - delete at launch
data/lookups/        Lookup values (CSV) loaded by scripts/load_lookups.py
docs/                identifier-types.md: identifier types and the identifiers each channel uses
scripts/
  import_excel.py    RestockR5.xlsx › BlacklistData → app/seed_data.json
sql/
  proposed_changes.sql
static/              Frontend (plain HTML/CSS/JS, no build step)
tests/               API + matching tests (own fixture data, independent of the import)
```

## Moving to Azure SQL

1. Apply `sql/proposed_changes.sql`: new columns and lookup values.
2. Load `app/seed_data.json` into `analytics.blacklist_listings` as a one-off import. Let IDENTITY
   assign `blacklist_id`, and pass `created_at` explicitly.
3. Add `SqlRepository(BlacklistRepository)` using `pyodbc` (or `mssql-python`) with managed-identity
   auth. Map unique violations (2627/2601) → `DuplicateError` and FK violations (547) → `InvalidReferenceError`.
   For `find_by_identifiers` / `find_by_asins`, pass the list as JSON and join on `OPENJSON(?)`, so
   5,000 inputs take one round trip. `list_by_type(<Brand prefix type id>)` returns the handful of brand rules, and
   `matching.py` does the rest unchanged.
4. On `UPDATE`, set `updated_at = SYSDATETIMEOFFSET()`. The default only fires on insert.
5. Add `CREATE INDEX IX_blacklist_listings_asin ON analytics.blacklist_listings (asin) WHERE asin IS NOT NULL`.
6. Point `get_repository()` at the SQL repository, then delete `seed_data.py` and `seed_data.json`.
7. Once the restock workbooks read from SQL (or call `/api/blacklist/check`), retire the `BlacklistData` sheet.

## Deploying to Azure App Service (Linux, Python 3.13)

- Startup command: `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`
- Health check path: `/healthz`
- Turn on **App Service Authentication (Easy Auth) with Microsoft Entra ID** so only company accounts
  can reach the app. Then fill `created_by` from the `X-MS-CLIENT-PRINCIPAL-NAME` header instead
  of the typed initials.
- Connection string: store it in App Settings / Key Vault, never in code.
- `seed_data.json` contains real company data. Keep the repo private, or delete it after the SQL load.
