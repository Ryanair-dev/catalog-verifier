<!-- doc-check: blacklist-0461 -->
# Marketplace Blacklist

## What this does

Replaces the `BlacklistData` sheet in the Restock workbooks: a shared list of seller SKUs / Listing IDs that must not be bought for, or sent to, a given marketplace store (Amazon - Preferred Pharmacy Plus, Amazon - Preferred Pharmacy Plus CA, Amazon - Turba, Amazon - Harper Wrens, Walmart, Temu, Ebay, Target). Planned home in this app: a **"Blacklist" item under the sidebar's "Marketplaces" group, next to Walmart Catalog**.

**Not the same thing as the Pair-Manager blacklist.** `blacklisted_pairs` (see `docs/infra/database.md` and `docs/features/pair_manager.md`) is a local SQLite list of rejected UPC→ASIN *pairs* used by scoring. This feature is a list of *listings per store*, stored in Azure SQL and shared with the rest of the company. The two never read each other.

**Status: standalone, not wired in yet.** The whole app lives in `blacklist/` as its own FastAPI app (originally the `Fordmed-Dev/ecomm-webapp` repo), so it can be ported into this app piece by piece. Nothing in `main.py`, `routers/`, `services/` or `static/` imports it.

## Architecture

- **Data**: Azure SQL `fbidb`, `analytics.blacklist_listings` (system-versioned; history in `analytics.blacklist_listings_history`, 90-day retention) with three pick-lists: `analytics.pl_channels`, `analytics.pl_identifier_types`, `analytics.pl_blacklist_reason_codes`. UNIQUE on `(identifier, identifier_type_id, channel_id)`. `blacklist_added_by_initials VARCHAR(4) NOT NULL`, so every write needs initials; `changed_by` / `changed_by_app` come from defaults (the app connects with `appname="blacklist-app"`). Loaded 2026-11-19: 2,373 rows, 8 stores, 3 types, 14 reasons.
- **This is a WRITE path to shared Azure data.** `docs/infra/azure_sql.md` covers a read-only mirror; this feature inserts and deletes rows in `analytics.blacklist_listings`. It uses the same service-principal env vars (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_SQL_SERVER_HOST`, `AZURE_SQL_SERVER_DATABASE`) via pure-Python `pytds` + `azure-identity`, so that principal needs INSERT/UPDATE/DELETE on that table.
- **Backend** (`blacklist/app/`): `main.py` (routes; `get_repository()` picks `SqlRepository` when `BLACKLIST_BACKEND=sql`, else an in-memory copy loaded from `seed_data.json`), `models.py` (Pydantic v2; field names = SQL column names), `matching.py` (exact / brand-prefix / ASIN matching, shared by both repositories), `repository.py` (interface + in-memory), `sql_repository.py` (Azure SQL; unique violation → 409, FK violation → 422; list lookups via one `OPENJSON` round trip).
- **API** (all under `/api/blacklist` inside the standalone app): search/browse, CSV export, get, create, PATCH, DELETE, `POST /check` (up to 5,000 identifiers → Blacklisted / Review / Clear), `POST /bulk` (one identifier list × several stores), `POST /bulk-rows` (the Add-many grid: per-row store/reason/notes, one `created` / `existing` / `error` result per row, never overwrites). Full table in `blacklist/README.md`.
- **UI** (`blacklist/static/`, vanilla JS, no build): Quick check, Browse (with a "Check List" toggle that turns it into the bulk check: paste or drop CSV/TXT), Add (one entry, or an Excel-like grid with paste from Excel, Fill ↓, per-row validation). Light/dark theme toggle. Colors/fonts already use this app's tokens (Inter, purple-500 `#6b50d4`, navy, greys, radii; see `docs/infra/ui_theme.md`).
- **Legacy reason "Do not send"** exists in the data but is refused for new entries.
- **Edit and sign-in are intentionally off.** Regular users don't edit (Ryan's call); the Edit dialog code stays but is unreachable. Entra sign-in (Easy Auth, fills `created_by` from `X-MS-CLIENT-PRINCIPAL-NAME`) is parked on branch `add-sign-in` in `Fordmed-Dev/ecomm-webapp`, to be added back at the very end.

### Porting notes (when it moves into this app)

- **Route clash:** this app already has `POST /api/blacklist` (`routers/verify.py`, Pair-Manager pair reject). Mount the marketplace blacklist under a different prefix, e.g. `/api/marketplace-blacklist`, and update the frontend's API base accordingly.
- **Sync vs async:** the blacklist routes are plain `def` (FastAPI runs them in a threadpool). This repo's convention is `async def` + `asyncio.to_thread()` for blocking work; convert when porting.
- **CSS:** `blacklist/static/styles.css` defines its own `:root` tokens and generic class names (`.btn`, `.card`, `.toggle`...). Scope it (e.g. under `.mbl`, like `create_po.css` uses `.cpo`) before loading it next to `static/styles.css`.
- **`.gitignore`:** this repo ignores `*.csv` and `data/`; the three lookup CSVs in `blacklist/data/lookups/` were force-added.
- **No data copy here:** `app/seed_data.json` (a full copy of the live rows, used only by memory mode) was left out of this repo on purpose and is gitignored in `blacklist/`. Use `BLACKLIST_BACKEND=sql`, or copy the file from `Fordmed-Dev/ecomm-webapp` for offline work. Without it, memory mode fails at startup; the tests don't need it (they use their own fixture data).

### Running it on its own

```bash
cd blacklist
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/python -m uvicorn app.main:app --port 8080   # needs BLACKLIST_BACKEND=sql + Azure vars in blacklist/.env
.venv/Scripts/python -m pytest                              # 42 tests, no network
.venv/Scripts/python scripts/sql_smoke_test.py              # live Azure check, always rolled back
```

## Changelog

**2026-11-20 — Added the standalone blacklist app to this repo (`blacklist/`), not wired in:**
- User (Dina, relaying Ryan): put the blacklist app we've been building into this repo as a PR, so Ryan can pull it into the app one piece at a time; it will become a tab next to Walmart Catalog.
- Copied `Fordmed-Dev/ecomm-webapp` @ `fc919c2` (master after PR #3) into `blacklist/` unchanged, minus its `.claude/` folder and `app/seed_data.json` (real company data; kept only in ecomm-webapp). Added this doc and its row in `CLAUDE.md`'s feature table. No existing file in this app was changed.
- Found while reading this repo: the `POST /api/blacklist` name clash with the Pair-Manager reject endpoint, the `*.csv` / `data/` ignore rules, and that `docs/infra/azure_sql.md` is read-only while this feature writes (all under "Porting notes" above).
- Verified: `pytest` in `blacklist/` → 42 passed. Against live Azure the same day (from ecomm-webapp): `scripts/sql_smoke_test.py` 25/25 (rolled back, row counts unchanged); a real write test added `ZZTEST-001` / `ZZTEST-002` through the Add-many grid, they showed up in `fbidb` with `changed_by_app = blacklist-app`, then were deleted through the app (ids 4763–4764; total back to 2,373; the two rows stay in history for 90 days).
- No restart needed (nothing in this app loads it).
