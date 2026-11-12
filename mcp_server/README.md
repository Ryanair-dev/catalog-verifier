# Catalog Verifier MCP server

The MCP server exposes Catalog Verifier tools to Claude Code over stdio. The
Catalog Verifier app must also be running. MCP calls its local HTTP API, and
reads saved Offer Analysis history directly from the app database in read-only
mode. 

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

Ready-made prompts are also available in MCP clients that support prompts:

- `Research Amazon ASINs`: search from a UPC, item ID, or description and,
  when possible, check saved Catalog Verifier pair/history data too.
- `Check ASINs`: start the selected eligibility, storage-fee, and/or generic
  checks, then guide Claude to report job status and results.

Prompts guide Claude to use the tools above; they do not add or copy app
business logic. Prompt display depends on the MCP client. Users can still ask
Claude a question without selecting a prompt.

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

