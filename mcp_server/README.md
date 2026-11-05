# Catalog Verifier MCP server

The MCP server exposes Catalog Verifier tools to Claude Code over stdio. The
Catalog Verifier app must also be running. MCP calls its local HTTP API, and
reads saved Offer Analysis history directly from the app database in read-only
mode. Tools that start jobs, submit feedback, or save files require explicit
`confirm=true`.

Available tools:

- `find_asins`: search Amazon listings from a UPC, item ID, or title.
- `lookup_product`: check the Pair Library and blacklist, and search saved
  Offer Analysis results by UPC, ASIN, or item ID. Historical results come from
  a read-only database query. `cheapest_offer` is the separate Price Desk
  lookup.
- `mpn_lookup`: check MPN and manufacturer against the local mirror and FDA
  AccessGUDID. Its optional Claude web-search fallback requires `confirm=true`
  and may incur variable API charges.
- `check_asins`: start eligibility, storage-fee, and/or generic checks for up
  to 100 ASINs.
- `check_brand`: start a sampled brand gating check.
- `job_status`: poll a check job; completed ASIN-check CSVs are saved under
  `Downloads\Catalog Verifier` by default.
- `list_runs` / `run_status`: see recent Offer Analysis runs and check progress
  using the safe runs-list API.
- `estimate_run`: estimate processing time and AI cost for a local catalog;
  this does not call Amazon or create a run.
- `start_run`: preview the estimate first, then call again with `confirm=true`
  to start an Offer Analysis background run.
- `export_run`: save an Offer Analysis Excel workbook locally; requires
  `confirm=true`.
- `report_match`: submit candidate feedback using the app's report endpoint;
  requires `confirm=true`.
- `search_pairs`: search the Pair Library and blacklist.
- Pair writes are not exposed. Existing app APIs do not provide both an exact
  preview and an audited single-pair write. MCP will not duplicate that logic
  or write to the database directly.
- `brand_runs` / `brand_run`: list recent Brand Analytics runs or read one
  run's results.
- `start_brand_search`: the first call reports that the existing app API has
  no pre-run cost estimate. After explicit confirmation, it calls the
  existing discovery endpoint. Review its output and pass the chosen
  `search_terms` with `confirm=true` to start the run.
- `seller_analysis`: look up a seller, then require `confirm=true` to start
  the full background export. The completed workbook is saved locally through
  `job_status`.
- `create_skus`: preview a local catalog file, then require `confirm=true` to
  generate SellerCloud import files locally. This does not write to
  SellerCloud; AI title cleanup is off by default.
- `po_watchlist` / `po_pnl`: read the PO watchlist and PO P&L reports.
  P&L values come from the app API; `all` returns separate active and sold-out
  reports.
- `analyze_po`: require `confirm=true` to start a PO Analytics export; the
  completed workbook is saved locally through `job_status`.
- `cheapest_offer`: return the row from the app-generated Price Desk lookup
  workbook for one UPC or ASIN. MCP only reads the workbook; the app selects
  the cheapest vendor.
- `export_offers`: require `confirm=true` to export the Price Desk workbook
  locally.
- `job_status`: poll a background job and download its completed export.
  Supported kinds include ASIN checks, brand-gating checks, Seller Analytics,
  and PO Analytics.

`explain_pair` returns the scorer explanation already saved for a candidate;
it does not call the candidate-detail endpoint or recalculate the verdict.
`pile_summary` is not exposed. The read-only history tools use the database
path from the app's `services.database.DB_PATH`; set
`CATALOG_VERIFIER_DB_PATH` if the database is stored elsewhere. Catalog file
tools accept absolute paths to CSV, TSV, XLSX, or XLSM files and use the app's
configured upload-size limit. For
`estimate_run` and `start_run`, pass settings as an object, for example:

```json
{
  "header_row": 0,
  "mapping": {"upc": 0, "title": 1, "itemid": 2},
  "search_methods": ["UPC", "ItemID", "Title"],
  "pages_per_title": 5,
  "vetting_mode": "cpg",
  "scoring_method": "weighted"
}
```

`header_row` and mapped column indexes are zero-based. `report_match.row` is a
one-based catalog data-row number, matching the Offer Analysis results table.

## Install

From the repository root, install the project's normal dependencies, including
the MCP SDK:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If the virtual environment has not been created yet:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Keep using `requirements.txt` and pip for this project; the MCP SDK is listed
there alongside the app's pinned dependencies.

## Run with Claude Code

1. Start Catalog Verifier in one terminal:

   ```powershell
   .\.venv\Scripts\python.exe main.py
   ```

2. From the repository root, register the server with Claude Code (replace the
   path if the repository is elsewhere):

   ```powershell
   claude mcp add catalog-verifier -- "C:\Users\KateStolbun\catalog-verifier\.venv\Scripts\python.exe" -m mcp_server.server
   ```

3. In Claude Code, run `/mcp` and confirm `catalog-verifier` is connected.
   Ask a question such as:

   > Find Amazon listings for UPC `012345678905` using Catalog Verifier. Show
   > the best candidates and their verdicts.

   Claude should call `find_asins`. Replace the example UPC with a real product
   identifier from your catalog. The app needs working SP-API credentials for
   the search to return candidates.

The server reads `CV_API_BASE` and `CATALOG_VERIFIER_API_TOKEN` from the
repository's `.env`. `CV_API_BASE` defaults to `http://localhost:8000`. If
`CATALOG_VERIFIER_API_TOKEN` is configured, it must match the token used by the
app. `CATALOG_VERIFIER_DB_PATH` is optional and only changes where the
read-only history tools look for the SQLite database.

## Unit tests

The MCP tests mock the HTTP API and use temporary SQLite databases for saved
history tests; they do not require the app or SP-API access:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests\mcp -v
```

## Changelog

**2026-11-06** — Removed MCP-side business decisions and stopped relying on
app endpoints added only to support MCP behavior. Product lookup now calls the
existing Pair Library and blacklist searches; cheapest-offer lookup reads the
existing Price Desk lookup workbook; PO reports use the app's active/archive
endpoints. Pair writes remain unavailable because the existing API has no
audited single-pair preview/write flow. Brand discovery requires confirmation
before calling the existing discovery endpoint, which does not provide a
pre-run cost estimate; users choose the terms before run creation. Verified
with mocked MCP tests. Restart the MCP server to load the updated tools.

**2026-11-05** — Added read-only access to saved Offer Analysis history for
`lookup_product`, including UPC, ASIN, and item ID searches, and added
`explain_pair` to return the saved scorer output without using the app's
mutating candidate-detail endpoint or recalculating a verdict. The database is
opened with SQLite read-only and query-only settings; no app code or schema was
changed. Verified with scratch-database tests, including checks that reads do
not change the database. Restart the MCP server to load the tools.

**2026-11-05** — Added Parts 4–9: Pair Library search and audited,
confirmation-gated pair/unpair; Brand Analytics run listing/detail and
cost-previewed search; Seller Analytics; local-file Create SKUs; PO watchlist,
analysis and P&L; and Vendor Offers cheapest-offer/export. Long Seller and PO
exports are polled/downloaded through `job_status`. Domain operations
(normalization, pair previews, cheapest-offer selection, brand workflow, PO
parsing and P&L bucket aggregation) run in the Catalog Verifier app API;
MCP gates confirmation, transports requests, and saves client-side files.
Verified with the MCP mocked unit suite; restart the MCP server to load the
tools and the app to create the audit table/register its new endpoints.

**2026-11-04** — Added Offer Analysis matching tools: `list_runs`,
`run_status`, `estimate_run`, `start_run`, `export_run`, and `report_match`.
Local file uploads are validated and sent only to the running app's HTTP API;
starting a run, saving an export, and submitting feedback require
`confirm=true`. `pile_summary` and `explain_pair` remain omitted because the
existing detail endpoint can repair legacy verdicts on read. Verified with
mocked HTTP/MCP tests, including multipart uploads and confirmation gates.
Restart the MCP server to load the new tools; the app routes and UI did not
change.

**2026-11-04** — Added the remaining Part 1 and Part 2 research/check tools:
`lookup_product`, `mpn_lookup`, `check_asins`, `check_brand`, and shared
`job_status`. Lookup reports Pair Library, blacklist, and Price Desk data;
Offer Analysis run history is deliberately omitted because the available
read endpoint can repair legacy saved verdicts. MPN AI web search requires
`confirm=true` after showing a variable-cost warning. Completed ASIN check
jobs save their CSV export locally. Verified with mocked unit tests for the
tool result shapes, limits, confirmation behavior, job polling, and file
download. Restart the MCP server to load the new tools; the app routes and UI
did not change.
