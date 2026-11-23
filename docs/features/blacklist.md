<!-- doc-check: blacklist-0461 -->
# Marketplace Blacklist

## What this does

Replaces the `BlacklistData` sheet in the Restock workbooks: a shared list of seller SKUs / Listing IDs that must not be bought for, or sent to, a given marketplace store (Amazon - Preferred Pharmacy Plus, Amazon - Preferred Pharmacy Plus CA, Amazon - Turba, Amazon - Harper Wrens, Walmart, Temu, Ebay, Target). Lives in this app as the **"Blacklist" item under the sidebar's "Marketplaces" group, next to Walmart Catalog**.

**Not the same thing as the Pair-Manager blacklist.** `blacklisted_pairs` (see `docs/infra/database.md` and `docs/features/pair_manager.md`) is a local SQLite list of rejected UPC→ASIN *pairs* used by scoring. This feature is a list of *listings per store*, stored in Azure SQL and shared with the rest of the company. The two never read each other.

**Status: wired in (2026-11-23).** The backend is ported into `services/marketplace_blacklist/` + `routers/marketplace_blacklist.py`; the UI is `static/marketplace_blacklist/`, iframed by the sidebar item. The original standalone copy in `blacklist/` (from the `Fordmed-Dev/ecomm-webapp` repo) is still in the repo as a reference, but **nothing loads it any more**: change the ported files, not `blacklist/`.

## Architecture

**In this app (since 2026-11-23):**

- **Sidebar to view:** `static/index.html` has the `data-view="marketplace-blacklist"` nav item and `#view-marketplace-blacklist` holding `<iframe id="marketplace-blacklist-frame">`. `static/app.js` `initMarketplaceBlacklist()` sets its `src` to `/static/marketplace_blacklist/index.html?v=N` on first open (same lazy-iframe pattern as Vendor Offers). The iframe keeps the page's own CSS (`:root` tokens, `.btn`, `.card`, ...) and JS globals out of the main app, so no CSS scoping was needed. **Bump `?v=` in `initMarketplaceBlacklist()` and the `?v=` on `styles.css` / `app.js` inside the page whenever those files change** (they are served by the plain `/static` mount, which sends no no-cache header).
- **Page** (`static/marketplace_blacklist/`: `index.html`, `app.js`, `styles.css`, `blacklist-icon.svg`): the standalone frontend, changed so far: asset links are relative to its own folder (`styles.css`, not `static/styles.css`); every API call goes to `/api/marketplace-blacklist...`; **Browse / Add are a floating bottom tab bar** (`nav.tabbar`, centred, with the brand pill `.tab-thumb` sliding to the active tab via the existing `moveThumb()`), replacing the original left icon sidebar (its logo, moon/sun toggle and initials badge are gone; `showMe()` is a no-op without `#me`, the Add form keeps the initials field); and **the theme follows the app-wide setting**: the inline `<head>` script reads `localStorage["cv-theme"]` (Settings > Appearance, see `docs/infra/ui_theme.md`) and listens for `storage` events, and the app's `applyTheme()` also sets the iframe's `data-theme` directly. The page keeps its own real dark palette; the app's root inversion filter is cancelled on this iframe.
- **API** (`routers/marketplace_blacklist.py`, included with `prefix="/api"`): `/api/marketplace-blacklist` (GET search, POST create), `/lookups`, `/export.csv`, `/check`, `/bulk`, `/bulk-rows`, `/{id}` (GET / PATCH / DELETE). Same behaviour as the standalone `blacklist/app/main.py`, but `async def` + `asyncio.to_thread()`, and one `_run()` helper maps repository errors: not found 404, duplicate 409, unknown store/reason/type 422 (missing initials and the legacy reason are 422 too), anything else (DB down) 503 `Database unavailable: ...`. It sits under `/api/`, so the optional `CATALOG_VERIFIER_API_TOKEN` check in `main.py` covers it like every other route. The Pair-Manager `POST /api/blacklist` is untouched.
- **Backend** (`services/marketplace_blacklist/`: `models.py`, `matching.py`, `repository.py`, `sql_repository.py`): copied from `blacklist/app/`. Always the Azure SQL repository: `get_repository()` returns one pooled `SqlRepository` (4 connections) and 503s with "Azure SQL is not configured" when an `AZURE_*` variable is missing. There is no in-memory `seed_data.json` mode here; `InMemoryRepository` is kept only for the tests. One addition: `SqlRepository._connect_or_raise()` turns a failed connect into a `RepositoryError` carrying the real cause (pytds hides it inside a bare `TimeoutError` after ~30 s of retries), so the UI shows e.g. the Azure firewall message instead of a blank 500.
- **Tests:** `tests/marketplace_blacklist/test_api.py` (42, offline, in-memory repo via `app.dependency_overrides[get_repository]`): `python -m pytest -q tests/marketplace_blacklist`. Not part of CI or the pre-push hook (those run `tests/scoring` only).
- **No background jobs or scheduler:** every call is a direct request, so there is nothing to add to Settings > Tasks Running and no startup slot.

**Original design (still true of the data and the UI):**

- **Data**: Azure SQL `fbidb`, `analytics.blacklist_listings` (system-versioned; history in `analytics.blacklist_listings_history`, 90-day retention) with three pick-lists: `analytics.pl_channels`, `analytics.pl_identifier_types`, `analytics.pl_blacklist_reason_codes`. UNIQUE on `(identifier, identifier_type_id, channel_id)`. `blacklist_added_by_initials VARCHAR(4) NOT NULL`, so every write needs initials; `changed_by` / `changed_by_app` come from defaults (the app connects with `appname="blacklist-app"`). Loaded 2026-11-19: 2,373 rows, 8 stores, 3 types, 14 reasons.
- **This is a WRITE path to shared Azure data.** `docs/infra/azure_sql.md` covers a read-only mirror; this feature inserts and deletes rows in `analytics.blacklist_listings`. It uses the same service-principal env vars (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_SQL_SERVER_HOST`, `AZURE_SQL_SERVER_DATABASE`) via pure-Python `pytds` + `azure-identity`, so that principal needs INSERT/UPDATE/DELETE on that table.
- **Backend** (`blacklist/app/`): `main.py` (routes; `get_repository()` picks `SqlRepository` when `BLACKLIST_BACKEND=sql`, else an in-memory copy loaded from `seed_data.json`), `models.py` (Pydantic v2; field names = SQL column names), `matching.py` (exact / brand-prefix / ASIN matching, shared by both repositories), `repository.py` (interface + in-memory), `sql_repository.py` (Azure SQL; unique violation → 409, FK violation → 422; list lookups via one `OPENJSON` round trip).
- **API** (all under `/api/blacklist` inside the standalone app): search/browse, CSV export, get, create, PATCH, DELETE, `POST /check` (up to 5,000 identifiers → Blacklisted / Review / Clear), `POST /bulk` (one identifier list × several stores), `POST /bulk-rows` (the Add-many grid: per-row store/reason/notes, one `created` / `existing` / `error` result per row, never overwrites). Full table in `blacklist/README.md`.
- **UI** (`blacklist/static/`, vanilla JS, no build): Quick check, Browse (with a "Check List" toggle that turns it into the bulk check: paste or drop CSV/TXT), Add (one entry, or an Excel-like grid with paste from Excel, Fill ↓, per-row validation). Light/dark theme toggle. Colors/fonts already use this app's tokens (Inter, purple-500 `#6b50d4`, navy, greys, radii; see `docs/infra/ui_theme.md`).
- **Legacy reason "Do not send"** exists in the data but is refused for new entries.
- **Edit and sign-in are intentionally off.** Regular users don't edit (Ryan's call); the Edit dialog code stays but is unreachable. Entra sign-in (Easy Auth, fills `created_by` from `X-MS-CLIENT-PRINCIPAL-NAME`) is parked on branch `add-sign-in` in `Fordmed-Dev/ecomm-webapp`, to be added back at the very end.

### Porting notes (written before the port; all four were handled on 2026-11-23, see Changelog)

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

**2026-11-23 (evening) — Bottom tab bar; theme moved to the app-wide setting:**
- User: "1. please remove the side bar inside the tab with the 'browse' and 'add'. Instead add them to the bottom as a bottom dynamic tab. 2. Make the dark mode toggle as a app wide option adding it to the settings."
- 1: the page's left icon sidebar (logo, Browse/Add, moon/sun toggle, initials badge) is replaced by a floating, centred bottom tab bar: frosted pill, slides up on load, and the purple active pill slides between Browse and Add with a slight overshoot (`.tab-thumb`, which the original code already positioned with `moveThumb()` but hid). `main` has 112 px bottom padding so the last row clears the bar; toasts sit above it. The initials badge went with the sidebar (purely informational; the Add form's "Added by" field and its remembered value are unchanged).
- 2: the page no longer has its own toggle or `blacklist-theme` key; it follows `cv-theme` from the new Settings > Appearance switch. The app-wide part is in `docs/infra/ui_theme.md` (same session).
- Verified with headless-Edge screenshots on live data in light and dark, plus flipping the switch with the Blacklist tab open (the iframe went dark immediately). `pytest tests/marketplace_blacklist` still 42 passed. Cache-bust: iframe `index.html?v=3`, page `styles.css?v=2` / `app.js?v=2`. No restart needed.

**2026-11-23 (later still) — Decision: no brand restriction (brand-prefix rules):**
- User: "we can not go forward with the brand restriction". So there will be no "Brand prefix" identifier type in `analytics.pl_identifier_types`, and only exact SKU / Listing ID matches block a listing.
- Code left as is: `matching.py` only applies brand rules when a type named "Brand prefix" exists, and the live table has none (3 types), so the brand path never runs. **Do not add that type** without revisiting this decision. (The tests still cover the brand rules because their fixture adds the type.)
- UI: the Browse hint said "Press Enter or Check to apply brand rules too". It now says "...for an exact Blacklisted / Clear check across stores" (`static/marketplace_blacklist/index.html`). Bumped the iframe `index.html?v=2` in `app.js` and `app.js?v=78` in the shell. No restart needed; a normal page reload picks it up.

**2026-11-23 (later) — Live Azure verification of the tab (read-only):**
- User switched on the VPN, which got past the Azure firewall block noted in the entry below (no code change needed).
- Through the live server (`/api/marketplace-blacklist/...`): lookups 8 stores / 14 reasons / 3 types (no "Brand prefix" type in the live table, so brand-rule matching is off, as designed); browse total 2,373 (~1.2 s); search `q=TORMS` 28; `check` on two real SKUs (one lower-cased) → both blacklisted on Amazon - Preferred Pharmacy Plus, a made-up SKU → clear (1.6 s); get-by-id OK; `export.csv` 2,373 rows (2.1 s).
- Write permission, read-only check: `HAS_PERMS_BY_NAME` on `analytics.blacklist_listings` → SELECT/INSERT/UPDATE/DELETE all 1 for this app's `.env` service principal. Row count unchanged at 2,373; **still no writes made from this app**.

**2026-11-23 — Wired in as the "Blacklist" sidebar tab (Marketplaces group):**
- User: after merging PR #12, "analyze our catalog-verifier app to then take the next step and add the blacklist app tab".
- Approach: port the backend into this app's structure, and embed the existing UI unchanged in an iframe (the Vendor Offers / Price Desk pattern) rather than rewriting 1,775 lines of JS and scoping 787 lines of CSS under `.mbl`. The iframe settles the CSS-clash porting note, and the page stays one self-contained folder.
- Porting notes resolved: route clash: `/api/marketplace-blacklist` (the frontend's API paths were repointed; the Pair-Manager `POST /api/blacklist` is unchanged and both show in the route table). Sync to async: `async def` + `to_thread`. CSS: iframe isolation. `.gitignore`: nothing needed (the lookup CSVs are only used by `blacklist/scripts/`).
- Compatibility: this app pins FastAPI 0.115 / Starlette 0.38, which has no `status.HTTP_422_UNPROCESSABLE_CONTENT` (the standalone app used FastAPI 0.141), so the router uses a plain `422`.
- New: connection failures return 503 with the real reason (see Architecture). Found because Azure SQL refused this machine's current public IP (`79.134.37.140`, "not allowed to access the server") during the live check. **The same firewall block hits the app's existing Azure features right now** (the live server's stderr log shows it on the `azure_sql` pulls); it needs a firewall rule in the Azure portal.
- Verified: `pytest tests/marketplace_blacklist` 42 passed (the 42 standalone tests, minus 3 that only made sense standalone (seed file, path-prefix mount, `/` page), plus a page/API-path test, a "not configured" 503 test and a firewall-error 503 test); `pytest tests/scoring` 13 passed; `import main` lists all 10 new routes beside the Pair-Manager `POST /api/blacklist`. Headless-Edge screenshots of the main shell with the Blacklist item clicked (served by a router-only preview on the in-memory fixture, so no schedulers and no Azure) show the page filling the view in light theme. Against live Azure, the firewall case returns the 503 with the firewall text after ~30 s.
- **Not yet verified against live Azure data** (lookups/search/check/add) because of the firewall block. Whether this app's `.env` service principal has INSERT/UPDATE/DELETE on `analytics.blacklist_listings` is also unconfirmed; once the IP is allowed, check it read-only with `SELECT HAS_PERMS_BY_NAME('analytics.blacklist_listings','OBJECT','INSERT')` (and UPDATE / DELETE).
- No writes were made to `analytics.blacklist_listings`.
- Needs a server restart (new router) and a normal page reload (`app.js?v=77`).

**2026-11-20 — Added the standalone blacklist app to this repo (`blacklist/`), not wired in:**
- User (Dina, relaying Ryan): put the blacklist app we've been building into this repo as a PR, so Ryan can pull it into the app one piece at a time; it will become a tab next to Walmart Catalog.
- Copied `Fordmed-Dev/ecomm-webapp` @ `fc919c2` (master after PR #3) into `blacklist/` unchanged, minus its `.claude/` folder and `app/seed_data.json` (real company data; kept only in ecomm-webapp). Added this doc and its row in `CLAUDE.md`'s feature table. No existing file in this app was changed.
- Found while reading this repo: the `POST /api/blacklist` name clash with the Pair-Manager reject endpoint, the `*.csv` / `data/` ignore rules, and that `docs/infra/azure_sql.md` is read-only while this feature writes (all under "Porting notes" above).
- Verified: `pytest` in `blacklist/` → 42 passed. Against live Azure the same day (from ecomm-webapp): `scripts/sql_smoke_test.py` 25/25 (rolled back, row counts unchanged); a real write test added `ZZTEST-001` / `ZZTEST-002` through the Add-many grid, they showed up in `fbidb` with `changed_by_app = blacklist-app`, then were deleted through the app (ids 4763–4764; total back to 2,373; the two rows stay in history for 90 days).
- No restart needed (nothing in this app loads it).
