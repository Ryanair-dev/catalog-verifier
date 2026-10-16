# Verify / Scan (legacy verification flow)

## What this does

The original vetting workflow: a user uploads a vendor catalog + a Keepa export, a confidence engine scores each row, the user reviews and exports to Excel. Two modes:
- **Normal mode**: catalog has ASINs pre-assigned; Keepa rows are looked up by ASIN.
- **Match-from-Keepa mode**: catalog has no ASINs; the engine searches the Keepa export itself to find candidate ASINs using selected methods (UPC, Item ID/MPN, Title). Up to 8 candidates per row; user reviews and approves one per row. One ASIN cannot be approved for two different catalog rows.

This predates the Analytics tab (`docs/features/offer_analytics.md`) and uses a completely separate scoring engine — the two must never be conflated when fixing bugs.

## Architecture

### Verify flow — scoring engine (`services/confidence.py`)

Different scorer from Analytics (`services/analytics/matcher.py`) — do not modify one for the other's bugs.

**Signal weights:**

```
UPC/EAN     55 pts   exact match only
Item ID     10 pts   fuzzy + suffix variant allowed
Brand        5 pts   exact or fuzzy vs Amazon Brand/Manufacturer
Title       25 pts   token_set_ratio across all Amazon text fields + attribute bonus/penalty
Pack         5 pts   catalog pack vs Amazon "Unit Details: Unit Value"
```
Total possible: 100.

**Identifier floors** (two, applied before the ASIN floor, in order):
1. **UPC + Item ID → Review floor**: if `upc.matched` AND `item_id.matched` AND no product-type mismatch → `confidence = max(confidence, review_threshold)`.
2. **Item ID + Brand → Review floor** *(no UPC)*: if `item_id.matched` AND `brand.matched` AND NOT `upc.matched` AND no product-type mismatch → `confidence = max(confidence, review_threshold)`. Handles MPN-identified products where the UPC differs (case-pack vs unit UPC) or is absent. Brand match prevents coincidental part-number collisions across brands.
3. **UPC + Brand + no contradiction → Approved floor**: if `upc.matched` AND `brand.matched` AND `" — "` not in title detail → `confidence = max(confidence, approved_threshold)`. Vendor shorthand titles score low but that is not a contradiction.

Category mismatch detection for floors 1 & 2: `" — "` exists in title detail AND text after it contains `"type "` (e.g. `"type dish soap vs paper towel"`).

**ASIN floor**: when the catalog row has an ASIN **and** title similarity ≥ **30%** (was 40%), confidence is floored at the Approved threshold — regardless of UPC match. Threshold is 30% because pre-researched ASINs may carry different marketing names for the same product (e.g. "Green Heritage Pro" vs "Pacific Blue Select by Georgia-Pacific PRO" — same MPN, same brand, only 32% title similarity).

### Match-from-Keepa mode (`services/keepa_matcher.py`)

New matching pipeline for scans where the vendor catalog has no pre-assigned ASINs. The Keepa export acts as the local Amazon database.

**How it works:**
1. User enables "Match from Keepa" toggle in the wizard step 2 (sets `match_from_keepa=True` on the scan, ASIN column not required).
2. User chooses methods: UPC, Item ID/MPN, Title (any combination).
3. After Keepa upload, user clicks "Run Verification" → hits `POST /api/scans/{id}/match` instead of `/verify`.
4. `find_candidates()` builds three indexes from the Keepa export (UPC digit index, MPN fuzzy index, title list) and searches for up to 8 candidates per catalog row using the selected methods.
5. All candidates are scored using the same `score_row()` engine as the normal verify flow.
6. Candidates saved to `scan_candidates` table; best-per-row also saved to `scan_results` for stats/export compat.
7. UI shows a candidates table grouped by catalog row. User approves one per row (others auto-discarded).
8. Approving a candidate: syncs `scan_results[row_idx]` so the normal export/stats pipeline works unchanged.
9. Unmatched rows → `Not Approved`, `review_status="No match found"`.

**Constraint: 1 ASIN per scan** — `get_asin_approved_for_scan(scan_id, asin)` checks if an ASIN is already approved for another row. The approve endpoint returns HTTP 409 if violated.

**New endpoints:**
| Method | Path | Notes |
|---|---|---|
| POST | `/api/scans/{id}/match` | Run match-from-Keepa pipeline (replaces `/verify` for match-mode scans) |
| GET | `/api/scans/{id}/candidates` | List all candidates + catalog rows for the scan |
| POST | `/api/scans/{id}/candidates/{cand_id}/verdict` | Approve or discard a candidate; body: `{verdict, review_status}` |

**`create_scan` fields**: `match_from_keepa: str = Form("false")`, `match_methods: str = Form("[]")` — appended by the wizard as `"true"/"false"` and a JSON array string.

**Wizard UI**: toggle `#map-match-toggle` in step 2; reveals `#map-match-methods` checkboxes (`#match-method-upc`, `#match-method-item-id`, `#match-method-title`). ASIN column dropdown becomes optional when toggle is on. State tracked in `state.wizard.matchFromKeepa` (bool) and `state.wizard.matchMethods` (array). Stored to `state.cpg.matchFromKeepa` / `state.cpg.matchMethods` after wizard submit.

**Candidates UI**: `renderCandidatesView()` + `_renderCandidateRows()` + `_setCandidateVerdict()` in `app.js`. Renders `#scan-candidates-section` (dynamically created div inserted before `#scan-summary-section`). Table grouped by row index; rowspan for vendor title and row number across multiple candidates. Approve → green highlight, others for same row discarded. Undo available. After approve/discard: re-fetches candidates from server and re-renders.

### Legacy endpoint

`routers/verify.py` `POST /api/verify` — the original single-shot flow, kept for compatibility. Do not remove until the UI is fully migrated to the scan-based flow.

### Attribute extraction (shared with other flows)

`services/extractor.py` — two functions called from multiple places:
- **`rule_extract(title, abbreviations)`** — regex-based. Returns `{product_type, size {value, unit, raw}, pack_count, variant {scent, color, flavor, form}, form, normalised_title}`. Uses `SIZE_RE` covering oz, lb, g, kg, ml, l, gal, fl oz.
- **`amz_extract(amz_row, abbreviations)`** — for Keepa/Excel rows. Combines all `_AMZ_TEXT_FIELDS` into a blob, then overrides with dedicated columns (Color, Scent, Flavor, Size). Use this for verify-flow Amazon rows, not for SP-API normalized dicts.
- **`ai_extract(title, abbreviations, extra_context)`** — GPT-4o with JSON schema. Returns same shape plus `expanded_title` and `new_abbreviations`.

The abbreviation library (see `docs/features/library.md`) is loaded via `database.flat_library()` (5-min in-memory cache). Always pass the library to `rule_extract` / `amz_extract` so vendor-specific abbreviations are expanded before attribute detection.

## Changelog

(No dated session entries specific to this legacy verify/scan flow were found in the original CLAUDE.md's changelog at the time of this doc split — all the explicit per-session verify-flow bug fixes that exist in the source history are attributed to the newer Analytics tab (`docs/features/offer_analytics.md`) instead, since that's where almost all active development has happened since the Analytics tab was introduced. The Architecture section above is accurate as of the 2026-10-16 doc split. Add new dated entries here for any future verify/scan-specific work.)
