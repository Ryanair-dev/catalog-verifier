<!-- doc-check: library-7559 -->
# Library (Abbreviation / Brand / Manufacturer)

## What this does

A shared reference library the app builds up over time: known abbreviation→full-form expansions (for product titles), plus brand and manufacturer entities. Distinct from the **Pair Library** (`docs/features/pair_manager.md`), which is an identifier→ASIN mapping — "Library" here is about vocabulary/entity knowledge, not ASIN pairing.

## Architecture

- **`abbreviation_library` table** (see `docs/infra/database.md`): categorised abbreviation/full-form entries, with `added_by` (`system`/`user`/`ai`) tracking provenance. Seeded once from `abbreviations.json` on first run.
- **`services/database.py flat_library()`** — loads the whole library into a flat abbr→full dict, 5-minute in-memory cache, invalidated on any write. Used by `services/extractor.py`'s `rule_extract`/`amz_extract` (see `docs/features/verify_scan.md`) to expand vendor-specific abbreviations before attribute detection, and by Create SKUs' AI title-cleaning (`docs/features/create_skus.md`) to keep abbreviation expansion consistent and to save newly-discovered AI expansions back via `database.add_library_entry(..., added_by="ai")`.
- **UI**: a slide-in sidebar panel, renamed from "Abbreviation Library" → **"Library"** (2026-05-21-era change) with `.lib-type-tabs` — **Abbreviations / Brands / Manufacturers** tabs. Brand/manufacturer CRUD under this panel is served by the Brand Analytics `brand_library` table/endpoints (`GET/POST /api/brand-analytics/library`, `DELETE /api/brand-analytics/library/{id}` — see `docs/features/brand_analytics.md`), not a separate brand table — the Library panel is the single UI surface for both abbreviation vocabulary and the brand/manufacturer entity store. Library data is loaded into `state.brandAnalytics.library` for the Brand Analytics wizard's autocomplete.
- **AI-learned abbreviation toast**: when an AI process (Create SKUs title cleaning, or an extraction pass) discovers a new abbreviation expansion not already in the library, it's saved with `added_by="ai"` and a small toast notifies the user (`_showAILearnedToast({abbr, full, category})`, `static/app.js` — this function name was specifically chosen to avoid colliding with the main `showToast`, after a real bug where both shared the name `showToast` and JS hoisting broke the primary toast system — see `docs/features/offer_analytics.md`'s 2026-05-20 entry for that fix's detail, which lives there because it was fixed in the same session as several Offer-Analysis-area changes even though the bug itself was library/toast-system-wide).

## Changelog

(No dated session entries exclusively about the Library panel itself were isolated during the 2026-10-16 doc split — most Library-table changes happened as a small part of larger Create SKUs or Brand Analytics sessions and are documented in those files' own Changelogs, e.g. `docs/features/create_skus.md`'s entry on AI title cleaning being wired to the shared Abbreviation Library, and `docs/features/brand_analytics.md`'s "Universal Library panel" UI note. Add new dated entries here for any future Library-panel-specific work.)
