# Catalog Verifier — Amazon Listing Verification

A local web app that automates the vetting of CPG vendor catalog products against their Amazon listings. Built for high-volume daily use by an e-commerce ops team.

Two tabs, two workflows:

- **Verification** — attach a vendor catalog + Keepa/Amazon export, run the confidence engine, review and export results.
- **Analytics** — upload a catalog file, let the SP-API search engine find the ASINs for you, then vet the matches.

---

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # add your API keys
python main.py
```

Open `http://127.0.0.1:8000`.

---

## Environment variables (`.env`)

| Variable | Required | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | Optional | AI Extractor and AI Re-check (GPT-4o / GPT-4o-mini) |
| `ANTHROPIC_API_KEY` | Optional | Claude-backed AI checks when configured |
| `AMZ_CLIENT_ID` / `SP_API_CLIENT_ID` | Optional | Analytics SP-API search |
| `AMZ_CLIENT_SECRET` / `SP_API_CLIENT_SECRET` | Optional | Analytics SP-API search |
| `REFRESH_TOKEN` / `SP_API_REFRESH_TOKEN` | Optional | Analytics SP-API search |
| `AMZ_SELLER_ID` / `SP_API_SELLER_ID` | Optional | Listing eligibility checks |
| `MARKETPLACE_ID` / `SP_API_MARKETPLACE_ID` | Optional | Defaults to `ATVPDKIKX0DER` (US) |
| `CATALOG_VERIFIER_API_TOKEN` | Optional | Requires `X-CV-Token` on API calls when set |
| `CATALOG_VERIFIER_MAX_UPLOAD_MB` | Optional | Upload size cap; defaults to `25` |

All are optional. Features that need a missing key are disabled with a clear UI message.

---

## Verification tab

### Workflow

1. Upload a vendor catalog (`.xlsx` / `.csv`) and map columns (UPC, Item ID, Title, ASIN).
2. Attach the matching Keepa Product Viewer export or standard Amazon export.
3. Click **Run Verification**. Results appear in three tabs: Approved / Review / Not Approved.
4. Work through the Review tab — Approve or Discard each item.
5. Export to a colour-coded `.xlsx` with all signals and a plain-English reason column.

### Confidence signals

| Signal | Weight | Notes |
|---|---|---|
| UPC / EAN | 55 pts | Exact match. No-UPC items score 0 here but aren't penalised further. |
| Item ID | 10 pts | Allows minor suffix variants; fuzzy fallback. |
| Brand | 5 pts | Exact or fuzzy match against Amazon Brand or Manufacturer. |
| Title alignment | 25 pts | Fuzzy token match across all Amazon text fields (title, bullets, description) + dedicated attribute columns (Color, Scent, Size). Size / pack / variant bonuses and penalties applied. |
| Pack count | 5 pts | Catalog pack vs Amazon `Unit Details: Unit Value`. Multiples detected and flagged. |

**ASIN floor** — when the catalog supplies an ASIN and the title similarity is ≥ 40%, the confidence is floored at the Approved threshold regardless of UPC, so pre-researched ASINs are never incorrectly downgraded just because the UPC was missing or changed.

**No-ASIN skip** — rows with no ASIN in the catalog are skipped entirely.

### Verdict thresholds (adjustable in Settings)

| Verdict | Default |
|---|---|
| Approved | ≥ 85 |
| Review | ≥ 35 |
| Not Approved | < 35 |

### Override actions

| Tab | Available actions |
|---|---|
| Approved | **Reject** → moves to Not Approved, blacklists the pair |
| Review | **Approve** (tags *Reviewed*) · **Discard** → Not Approved, blacklists |
| Not Approved | **Promote to Approved** (tags *Manually Approved*) |

Bulk buttons per tab apply the action to every visible row.

### Export (20 columns)

`UPC/EAN · Item ID · Vendor Title · Brand · ASIN · Amazon Title · Confidence Score · Verdict · Review Status · UPC Signal · Item ID Signal · Brand Signal · Title Signal · Pack Signal · Amz Pack · Barcode DB Match · Duplicate Flag · Original Verdict · Notes · Reason`

---

## Analytics tab

### Workflow

1. Upload a vendor catalog file. Step through the wizard: pick the header row, map columns (UPC, Title, Brand, Item ID).
2. Choose search methods (UPC, title keyword, brand+keyword) and page depth.
3. Click **Start Run**. The SP-API engine searches Amazon for each catalog item, scores the candidates, and assigns verdicts.
4. Review results in the run detail view — promote, reject, or bulk-action by verdict bucket.

### Search methods

| Method | How it works |
|---|---|
| UPC barcode | `SearchCatalogItems` by `EAN` identifier |
| Title keyword | Cleans the vendor title (optionally via GPT-4o) and searches by keyword |
| Brand + keyword | Narrows the keyword search to the vendor's brand |

Results are deduplicated and ranked by confidence score.

---

## AI features

### AI Extractor
Calls GPT-4o to unabbreviate terse vendor titles token-by-token and extract structured attributes (product type, size, pack count, variant, form). Any new abbreviations discovered are silently added to the library under *Product Attributes*. Toggle the **AI Extract** button in the scan wizard.

### AI Re-check
A second-pass audit on existing scan results. For each row in the selected verdict buckets, GPT-4o-mini reviews the vendor title, Amazon title, current verdict, and signal breakdown, and returns a *suggested* verdict + one-line reason. Suggestions are displayed alongside the original verdict — nothing auto-applies. Available from the **AI Re-check** button in the scan header.

| Model | Approx. cost/row |
|---|---|
| gpt-4o-mini | ~$0.000083 |
| gpt-4o | ~$0.00083 |

---

## Pair Manager tab

Search the blacklist by UPC or ASIN. Shows the confidence at rejection, the failed signals, and the date. Use **Unlock Pair** to remove a pair from the blacklist if a rejection was made in error.

---

## Abbreviation library

10 built-in categories: Colors, Sizes, UOMs, Forms, Sterility, Materials, Scents, Flavors, Packaging, Product Attributes. Editable from the Settings side panel. The AI Extractor auto-learns into *Product Attributes*. The library is used by both the rule-based extractor and as known-abbreviation context for GPT-4o.

---

## Barcode lookup chain

Five keyless sources tried in order: Open Food Facts → Open Beauty Facts → Open Products Facts → UPC Item DB → DuckDuckGo scrape. Results are looked up on demand and retained on the current results row; each row has a **Clear cache** button to force a fresh lookup.

---

## Project structure

```
catalog-verifier/
├── main.py                         # FastAPI entrypoint
├── routers/
│   ├── scans.py                    # Scan lifecycle, verify, AI re-check
│   ├── verify.py                   # Single-item verify + attribute cache
│   ├── analytics.py                # Analytics runs CRUD + verdict overrides
│   ├── export.py                   # Excel export
│   ├── barcode.py                  # Barcode lookup
│   ├── pairs.py                    # Blacklist search + unlock
│   ├── library.py                  # Abbreviation library CRUD
│   └── settings.py                 # Thresholds
├── services/
│   ├── confidence.py               # Weighted signal scoring + ASIN floor
│   ├── extractor.py                # Rule-based + GPT-4o attribute extraction
│   ├── ai_recheck.py               # GPT-4o-mini second-pass verdict suggestions
│   ├── barcode_lookup.py           # 5-source barcode chain
│   ├── database.py                 # SQLite layer (auto-created on first run)
│   ├── analytics/
│   │   ├── runner.py               # SP-API search orchestration
│   │   ├── matcher.py              # Candidate scoring for analytics runs
│   │   └── parser.py              # Catalog file parsing
│   └── spapi/
│       ├── client.py               # SP-API HTTP client
│       ├── auth.py                 # LWA token refresh
│       ├── catalog.py              # SearchCatalogItems wrapper
│       ├── config.py               # Marketplace config
│       └── rate_limiter.py         # Token-bucket rate limiter
├── static/
│   ├── index.html                  # SPA shell
│   ├── app.js                      # All UI logic
│   ├── styles.css                  # Design tokens
│   └── sample/                     # Demo data (catalog + Keepa exports)
├── abbreviations.json              # Library seed data
├── requirements.txt
└── .env
```

---

## Database tables

| Table | Purpose |
|---|---|
| `scans` | Scan metadata (name, status, file names, counts) |
| `scan_catalog_rows` | Parsed catalog rows per scan |
| `scan_amazon_rows` | Attached Amazon/Keepa rows per scan, keyed by ASIN |
| `scan_results` | Per-row verdict + signals JSON |
| `analytics_runs` | Analytics run metadata |
| `analytics_catalog_rows` | Source rows for each analytics run |
| `analytics_candidates` | SP-API candidate matches with verdicts |
| `keepa_imports` | Raw Keepa rows keyed by ASIN, reusable across scans |
| `amazon_imports` | Raw Amazon export rows keyed by ASIN, reusable across scans |
| `blacklisted_pairs` | Rejected UPC/ASIN pairs with confidence + signals + date |
| `verified_items` | Final approved rows with review status |
| `attribute_cache` | Normalised attributes per (UPC, ASIN) |
| `abbreviation_library` | Categorised abbreviation entries (user + AI) |
| `settings` | Threshold key/value pairs |

---

## API surface

### Health
| Method | Endpoint | Returns |
|---|---|---|
| `GET` | `/api/health` | `{ status, ai_available }` |

### Scans
| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/scans/preview` | Parse a catalog file, return headers + sample rows |
| `POST` | `/api/scans` | Create a scan (multipart: file + mapping JSON) |
| `GET` | `/api/scans` | List all scans |
| `GET` | `/api/scans/{id}` | Scan detail + results |
| `DELETE` | `/api/scans/{id}` | Delete a scan |
| `POST` | `/api/scans/{id}/amazon` | Attach an Amazon/Keepa export to a scan |
| `POST` | `/api/scans/{id}/verify` | Run the confidence engine |
| `POST` | `/api/scans/{id}/verdict` | Update a single row verdict |
| `POST` | `/api/scans/{id}/bulk-verdict` | Update multiple row verdicts |
| `POST` | `/api/scans/{id}/mark-exported` | Mark scan as exported |
| `POST` | `/api/scans/{id}/ai-estimate` | Cost + row count preview for AI re-check |
| `POST` | `/api/scans/{id}/ai-recheck` | Run AI re-check on selected verdict buckets |

### Analytics
| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/analytics/preview` | Parse catalog file, return raw rows for wizard |
| `POST` | `/api/analytics/runs` | Create + start an analytics run |
| `GET` | `/api/analytics/runs` | List all runs |
| `GET` | `/api/analytics/runs/{id}` | Run detail + candidates |
| `GET` | `/api/analytics/status` | SP-API credentials probe |
| `POST` | `/api/analytics/runs/{id}/candidates/verdict` | Override one candidate verdict |
| `POST` | `/api/analytics/runs/{id}/candidates/bulk_verdict` | Bulk verdict override |

### Other
| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/verify/ai` | Single-title AI extraction |
| `POST` | `/api/barcode/lookup` | Barcode lookup chain |
| `POST` | `/api/export` | Generate Excel export |
| `POST` | `/api/pairs/search` | Search blacklist |
| `POST` | `/api/pairs/unlock` | Remove a pair from blacklist |
| `GET/POST` | `/api/library` | Abbreviation library CRUD |
| `GET/POST` | `/api/settings/thresholds` | Read/write verdict thresholds |

---

## Troubleshooting

**AI Re-check / AI Extractor does nothing** — check that `OPENAI_API_KEY` is set in `.env` and the server was restarted after editing it.

**Analytics runs stay in "Error"** — SP-API credentials are missing or invalid. Check `AMZ_*` or `SP_API_*` keys in `.env`. The `/api/analytics/status` endpoint confirms whether the credentials are accepted.

**Not Approved items all score 0** — their ASINs are missing from the Keepa export. Add the missing ASINs to the Keepa Product Viewer and re-export.

**Barcode lookups all return "Not Found"** — the public APIs may be rate-limiting. Click **Clear cache** on the row to retry.

**"Failed to fetch" on file upload** — hard-refresh the browser (Ctrl+Shift+R) to reload the latest `app.js`, then try again.

**Want to reset everything** — delete `catalog_verifier.db` and restart. The DB is recreated with seed data.
