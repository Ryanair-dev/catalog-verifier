# Create PO — SKU generation & SellerCloud bulk-import (design)

> Status: **ANALYSIS COMPLETE — build blocked pending SellerCloud REST API (read) access.**
> Scope when unblocked: **both paths** — medical (MPN-based mains) and CPG (UPC-based mains), plus FBA/FBM shadows and kits.
> This doc captures the naming rules, the real-data findings from `SC data.xlsx`, the existence/uniqueness algorithm, and what we need from SellerCloud.

## 1. SKU naming conventions (target rules)

- **Main SKU** = `[prefix][identifier]`
  - Medical: identifier = **MPN** (ManufacturerSKU), dashes/underscores **kept** — e.g. `MKS16-4292`, `DYN1113`, `MMMR1547`.
  - CPG: identifier = **UPC-based**.
  - `prefix` = maintained per-**manufacturer/brand** lookup (can contain `&`, e.g. `P&G`).
- **Shadow SKU** = `[Main]-[CHANNEL]`; Amazon = `-FBA`, merchant = `-FBM`.
- **Kit SKU** = `[Main]_QY[N]-[CHANNEL]`.
- **AmazonEnabled** = true **only** on Amazon shadows (`-FBA`) and kits — blank on mains.
- **ProductName** = source title + ` (UOM)`; do **not** prepend BrandName.
- Collision handling: if a shadow SKU name is already taken by a different ASIN, **append the next free integer** (`-FBA` → `-FBA2` → `-FBA3` …; same for `-FBM`).

## 2. Reference data — `SC data.xlsx` (live SellerCloud export, 4 sheets)

| Sheet | Rows | Columns | Role |
|---|---|---|---|
| **Sku Data extended** | 49,616 | ProductID, ProductName, UPC, ManufacturerSKU(=MPN), Manufacturer, ASIN, BrandName, AverageCost, LastCost, AggregateQty, OnOrder, QtyPerCase, FulfilledBy, Purchaser, ShadowOf, SOURCE_LEAD | **Mains master** → main-SKU existence + uniqueness, prefix source |
| **FBA** | 32,948 | FBA_SKU, FBM_SKU, ASIN, Column1(junk) | ASIN → existing shadow SKUs; collision ladder |
| **SellerSnapData** | 29,431 | asin, 30d sales, 30d returns, sku, MPN | **Returns/sales source**; extra shadow coverage |
| **Lowest cost** | 154,169 | Split PONumber, ProductID, QtyPerCase, CostPerCase, UnitPrice | PO cost history (optional) |

## 3. Divergences found (rules vs. actual data)

1. **`Main = prefix+MPN` reconstructs only ~36–40% of existing mains** (17,924 of 49,616 raw; 23,347 no-match). Cause: the table mixes **medical (MPN-based)** and **CPG (UPC-based)** mains + legacy hand-entry. → Check main existence by **UPC/MPN column lookup**, never by rebuilding the SKU string.
2. **Prefix is not 1:1 with manufacturer.** Head is clean (Welch Allyn→WEL, Hollister→HOL, McKesson→MKS[240] vs MCK[7], 3M→MMM[286] vs MMMT[2]), but long tail: P&G has 9 prefixes, Becton Dickinson 18, Medline 3; and **36 prefixes are shared across manufacturers** (COV→Medtronic+Cardinal, GOJ→GOJO+Dynarex). → Mode per manufacturer/brand is reliable for the head only; **authoritative prefixes must come from the SC API** (new prefixes minted daily).
3. **`-FBA` is the standard Amazon suffix** (26,311 uses); `-F` is a 2-count legacy fluke. Increment ladder is real and heavily used: `-FBA2` (3,852) … `-FBA15`; `-FBM2`…`-FBM12`.
4. **Dashes in MPNs are kept** in medical mains (2,983 confirmed, e.g. `MKS16-4292`) — do **not** strip.
5. **Returns data = `SellerSnapData`** (`30d sales`/`30d returns`) — not stored by the app today; that's why it's absent from the Azure schema.
6. `ShadowOf` blank throughout the mains table → mains-only; join to FBA sheet on ProductID for shadow relationships. UPC blank for 40% (consistent with medical). Legacy noise in the FBA column to ignore: 12,871 bare mains, 3,809 bare `_QY`, 3,605 dotted, 769 `TRGT` (Target).

## 4. Existence / uniqueness / collision algorithm

- **Main SKU:** index mains by **UPC** and by **MPN**. Incoming UPC or MPN already present → main exists, reuse its ProductID, **do not create**. (Enforces "every UPC/MPN → one unique Main SKU.")
- **Shadows:** ASIN → {existing FBA set, existing FBM set} from FBA sheet ∪ SellerSnapData, plus a global set of all shadow SKUs (noise filtered). ASIN already has a shadow → reuse it. New ASIN whose `[Main]-FBA` collides globally → climb `-FBA2/3/…` to the first free name.
- **Kits:** `[Main]_QY[N]-[CHANNEL]`; same existence/collision logic on the kit SKU name.

## 5. SellerCloud REST API — verified from the live spec

Source of truth: `https://tt.api.sellercloud.com/rest/swagger/docs/v1` (OpenAPI 2.0, "Delta Client API"). Server = **fml** (FordMed; login `fml.delta.sellercloud.com`). Base = `https://fml.api.sellercloud.com/rest`. All paths below are under `/rest`. (`tt` in SellerCloud's docs is only an example host.)

### Auth
- `POST /api/token` — JSON `{ "Username": "...", "Password": "..." }` -> `{ access_token, token_type, expires_in, username, ".issued", ".expires" }`.
- Send `Authorization: Bearer {access_token}` on every call. Re-request before `expires_in` lapses.

### Reading the catalog — `GET /api/Catalog`
Paged (`model.pageNumber`, `model.pageSize`), returns `{ Items[], TotalResults }`. Each item includes everything we need: `ManufacturerSKU, ManufacturerID, UPC, ASIN, BrandID, BrandName, ShadowOf, MainProductID, AmazonFBASKU, MerchantSKU, AmazonMerchantSKU, IsKit, AmazonEnabled, FulfilledBy, QtySold30/…, AverageCost, LastCost, ID`, etc.
- **Server-side filters that exist:** `model.sKU` (product IDs, comma list), `model.uPC`, `model.brandIds[]`, `model.vendorSKU[]`, `model.companyID[]`, `model.warehouse[]`, `model.displayShadows` (0 Non-shadow / 1 Shadow-only / 2 Shadow-parent), `model.selectedKits` (0–4), `model.activeStatus`, date-created / last-updated ranges, `model.keyword`.
- **NUANCE — no server-side filter for `ManufacturerSKU (MPN)` or `ASIN`.** They come back in the response but you can't query on them directly. Consequences for existence checks:
  - by **UPC** -> direct (`model.uPC`) [CPG path]
  - by **SKU/ProductID** -> direct (`model.sKU`)
  - by **MPN** -> `model.keyword=<mpn>` then match `ManufacturerSKU` exactly in results *(keyword-indexes-MPN to confirm live)*, or pull-and-index (below)
  - by **ASIN** -> `model.displayShadows=1` + keyword, or pull-and-index
- **Recommended existence strategy:** periodic **full catalog pull** (paged `GET /api/Catalog`, or a Custom Export via `/api/Catalog/Exports/*`) -> local index by UPC / MPN / ASIN / SKU. This reproduces `SC data.xlsx` automatically and fresh. Per run, optionally re-check the specific items live by UPC/SKU to catch anything created since the pull. Avoids per-item keyword hammering + rate limits.

### ID resolution (needed for creation)
- `GET /api/Settings/Manufacturers` -> name <-> **ManufacturerId** (numeric; required by product create).
- `GET /api/Settings/Brands` -> name <-> **BrandId** (numeric; used by `model.brandIds`).
- Prefix has **no field in SellerCloud** — still derived. But now derivable *live*: pull current products for a manufacturer/brand and take the mode of `ProductID − MPN`, at generation time (no stale snapshot).

### Creating records (API can do the whole thing — not just file generation)
- **Main product:** `POST /api/Products` — body `AddSingleProductModel { ProductSKU, ManufacturerId, AutoAssignUPC, UPC }`. Then fill the rest via `PUT /api/Catalog/BasicInfo` (`MasterSKU, ManufacturerSKU, UPC, ASIN, FNSKU, EAN`) and `PUT /api/Catalog/AdvancedInfo`.
- **Shadows:** `POST /api/Catalog/Imports/Shadows` — body `CatalogImportFileRequest { FileContents (base64 bytes), FileExtension, Format, Metadata:{ ScheduleDate, CompanyId } }`. Columns come from `GET /api/Catalog/Imports/Shadows/Template`.
- **Kits:** `POST /api/Catalog/Imports/Kits` — same wrapper; `Metadata:{ CreateParentProductIfDoesntExist, CreateChildProductIfDoesntExist, OverwriteExistingKits, InventoryCalculationKind, ScheduleDate }`. (`InventoryCalculationKind` <-> Independent vs component — maps to the -FBA/-FBM kit rule.)
- **Purchase order itself:** `POST /api/PurchaseOrders`, `POST /api/PurchaseOrders/Import/ImportPurchaseOrders`, `…/{id}/receive` — so "Create PO" can also cut the actual PO via API.
- **NUANCE — imports are ASYNC.** The `/Imports/*` endpoints enqueue work; poll `GET /api/QueuedJobs` for completion/errors rather than assuming success on the 200.
- **AmazonEnabled** is not a BasicImports type; it's a product/channel flag — set via `AdvancedInfo` or the shadow record. Confirm the exact mechanism live.

### Full API surface (for context)
20 groups: Catalog(47), Orders(44), Inventory(20), PurchaseOrders(19), Rma(17) *(returns/RMA — alternative returns source)*, Customers(7), QueuedJobs(7), Companies(6), Vendors(5), Products(4), ShippingContainers(4), Picklists(3), ProductImage(3), Settings(8), Warehouses(2), ProductConditions(2), ScheduledTasks(2), WarehouseInventoryTransfers(7), Diagnostics(1), token(1).

### Rate limits
Not in the spec — SellerCloud throttles; implement 429 backoff. Async imports (queued jobs) reduce load.

## 6. To verify once we have a token (nothing else blocks the build)
1. Does `model.keyword` match `ManufacturerSKU` and `ASIN`? (decides per-item vs pull-and-index for the medical/ASIN lookups)
2. Which response field carries the ProductID/SKU string (`ID` vs `ProductMasterSKU`)?
3. Exact `AmazonEnabled` set mechanism (AdvancedInfo vs shadow record).
4. Rate-limit thresholds and max `pageSize`.
5. Shadow/Kit template column layout (`GET …/Template`) — these become our output columns.

## 7. Open decisions (defer until build)
- **Workflow:** auto-create via API (POST Products + Imports) vs generate a reviewed file the user uploads. API can do either.
- Whether returns (`SellerSnapData` / `/api/Rma`) becomes an in-app table (independent of Create PO).
- Credentials: **RESOLVED** — server is `fml`, creds in `.env` (`SELLERCLOUD_*`). Token authenticates.

---

## 8. Live probe results — RESOLVED (2026-07-08, server `fml`)

Resolves §6. Verified with `python -m tests.probe_sellercloud`.
- **Auth OK** — bearer token, ~3600s TTL.
- **SKU field = `ProductID`** (JSON field `ID`; round-trips via `model.sKU`).
- **`model.keyword` matches ASIN (yes) but NOT ManufacturerSKU/MPN (no)** → ASIN lookups can be live queries; **MPN existence must use the local index**.
- **Catalog ≈ 176k products; `pageSize` hard-capped at 50** (50→50, 100→50, 200→50) → full pull via async Custom Export, never paging.
- **`Settings/Brands` = 3,487 `{Key=BrandID, Value=BrandName}`** (names may carry leading spaces — normalize on match). **`Settings/Manufacturers` 404s** (account permission) — worked around below.
- **Search API returns `Manufacturer=null` and often `ManufacturerID=0` on mains** (unreliable), BUT the **Custom Export exposes `ManufacturerName` + `ManufacturerID` columns** → build the manufacturer name→id map from the export. (Enabling the Manufacturers API permission is optional, not required.)
- Samples: mains have `ShadowOf=''`; a CPG main SKU = prefix + UPC tail (`ZYR206909` ← UPC `300450206909`); shadows carry `ASIN` + `ShadowOf`→main + `-FBM`/`-FBA` suffix. **CompanyID 164 = Ford Medical, LLC; 278 = Ford Distribution Services.** `AmazonEnabled=True` appears even on some mains (legacy — matches the earlier note).

## 9. Local catalog index — per-brand search slices (BUILT: `services/sellercloud/catalog_index.py`)

**There is no "export everything".** Both `Exports/Custom` and `Exports/Basic` REQUIRE an explicit `ProductIds` list (`[]` → 500 "Please select Products for Export"; omitted/null → 400 "required"). So the full-export plan is dead. (Custom export is still used later to fetch `ManufacturerName`/`ManufacturerID` for *specific* ProductIds when the API-create path needs the numeric manufacturer id.)

Instead the index is built from **per-brand search slices** — `model.brandIds` IS a real server-side filter and brands are small:
1. `GET /api/Settings/Brands` → brand-name → BrandID (3,487; names may have leading spaces — normalize).
2. For each brand a run touches: `GET /api/Catalog?model.brandIds=<id>` paged at 50 → full rows. Slice sizes observed: Zyrtec 15, Zyliss 33, Tegaderm 596, McKesson 739, 3M 837. Cached to `data/brand_slices/<id>.json` (12h TTL).
3. `build_from_rows` indexes by **UPC / ManufacturerSKU (MPN) / ASIN / ProductID(SKU)** and derives **prefix = mode of leading letters of the brand's mains, with a confidence share** (McKesson→MKS 89%, Tegaderm→MMM 58%, Zyrtec→ZYR 50% — low confidence = generator must ask, not guess).

Nuances baked in: search rows carry the SKU in `ID` (export uses `ProductID`) — `_pid()` handles both. Existence/collision checks are **scoped to loaded brands**, so the generator must load each item's brand slice before checking. Search latency is ~5–6s/request (fine because slices are cached). `Manufacturer` name is null and `ManufacturerID` often 0 in search rows → manufacturer-name→id is deferred to the API-create phase (via targeted custom export).

Client methods (all in `services/sellercloud/client.py`): `search_catalog`/`iter_catalog`, `get_brands`, and for the create phase `create_custom_export` / `get_job` / `wait_for_job` (status `3`/non-empty `OutputFile` = done) / `download_job_output` (base64-decoded).

## 10. Generator + export — BUILT (`services/create_po.py`, `routers/create_po.py`, `#view-create-po`)

Input = **user-uploaded sheet** (headers matched loosely: ASIN, UPC, MPN, Brand, Manufacturer, Product Name, Pack Qty), tagged **Medical** or **CPG** per batch. Per item:
- resolve brand → BrandID (`Settings/Brands`) → load/build the brand slice (index).
- **Precedence:** if the ASIN is already listed (has shadows) → reuse its existing main + shadows; else resolve the main by MPN (medical) / UPC (CPG) → reuse if present, else mint `prefix + identifier` (medical = MPN verbatim incl. dashes; CPG = last-6 UPC digits, `CPG_UPC_TAIL`).
- shadows: reuse the ASIN's `-FBA`/`-FBM` or mint via the collision ladder; kit `_QY[N]-FBA/-FBM` when Pack Qty > 1.
- flag **NeedsReview** on: low prefix confidence (<70%), missing identifier, or unknown brand.

Output: a **`.zip` of `bulk.xlsx` / `kits.xlsx` / `amz_shadows.xlsx`** matching the real SellerCloud templates (bulk = 17-col mains; kits = ParentSKU/ChildSKU/QTY/InventoryDependantOption with FBA=Independent, FBM=All_Components; amz = ParentSKU/ShadowSKU/CompanyID). **UI = a 4-step wizard** (`static/create_po.js` + `create_po.css`, ported from a Claude Design mockup; sidebar dropped) over endpoints `POST /preview` → `POST /brands` → `POST /generate` → `POST /export` (inline edits applied at export time). Global light/dark toggle in the app sidebar; light = milk (`airy`); CompanyID default 164 (medical) / 278 (CPG). Verified end-to-end over HTTP.

**Export column layout is a FIRST CUT** — to be mapped to the user's real SellerCloud import template (the API `…/Template` endpoints 404 on this account and there is no product-create bulk template in the REST API). API auto-create (`POST /api/Products` + `Imports/Shadows|Kits`) remains a later opt-in phase.
