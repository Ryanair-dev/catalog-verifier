"""
Live SellerCloud API probe. Run AFTER adding SELLERCLOUD_USERNAME / _PASSWORD
to catalog-verifier/.env:

    python -m tests.probe_sellercloud

Answers the open questions in create_po_design.md §6:
  1. Does model.keyword match ManufacturerSKU (MPN) and ASIN?
  2. Which response field carries the ProductID/SKU string?
  4. Rate-limit headers + max pageSize behaviour.
  5. Manufacturers / Brands endpoint shapes.
Prints PASS/FAIL per check; safe to re-run.
"""
from __future__ import annotations

import time
from dotenv import load_dotenv

load_dotenv()  # pick up catalog-verifier/.env when run from the project root

from services.sellercloud import (          # noqa: E402
    get_sellercloud_client,
    sellercloud_configured,
    load_sellercloud_credentials,
)


def _p(msg=""):
    print(msg, flush=True)


def _keys(d):
    return list(d.keys()) if isinstance(d, dict) else type(d).__name__


def main() -> None:
    _p("=" * 64)
    _p("SellerCloud API probe")
    _p("=" * 64)

    if not sellercloud_configured():
        _p("FAIL: credentials not configured. Add SELLERCLOUD_USERNAME and "
           "SELLERCLOUD_PASSWORD to catalog-verifier/.env, then re-run.")
        return
    creds = load_sellercloud_credentials()
    _p(f"base_url = {creds.base_url}")
    _p(f"username = {creds.username}")

    client = get_sellercloud_client()

    # 1) token ------------------------------------------------------------
    _p("\n[1] token")
    try:
        tok = client.tokens.get_token()
        ttl = int(client.tokens._expires_at - time.time())
        _p(f"  PASS: got token ({len(tok)} chars), ~{ttl}s to expiry")
    except Exception as e:
        _p(f"  FAIL: {e}")
        return

    # 2) catalog smoke + raw headers -------------------------------------
    _p("\n[2] GET /api/Catalog (pageSize=5) + rate-limit headers")
    sample = []
    try:
        resp = client._request(
            "GET", "/api/Catalog",
            params={"model.pageNumber": 1, "model.pageSize": 5},
        )
        rate_hdrs = {k: v for k, v in resp.headers.items()
                     if any(t in k.lower() for t in ("rate", "limit", "retry", "throttl"))}
        data = resp.json()
        sample = data.get("Items") or []
        _p(f"  PASS: TotalResults={data.get('TotalResults')}, returned={len(sample)}")
        _p(f"  rate-limit headers: {rate_hdrs or 'none'}")
        if sample:
            _p(f"  item field count: {len(_keys(sample[0]))}")
    except Exception as e:
        _p(f"  FAIL: {e}")
        return

    # 3) which field is the ProductID/SKU string? ------------------------
    _p("\n[3] identify the SKU/ProductID field (round-trip via model.sKU)")
    sku_field = None
    if sample:
        it = sample[0]
        candidates = ["ID", "ProductMasterSKU", "MerchantSKU", "MainProductID",
                      "AmazonMerchantSKU"]
        for f in candidates:
            val = it.get(f)
            if not val or not isinstance(val, str):
                continue
            try:
                back = client.search_catalog(sku=val, page_size=3).get("Items") or []
                hit = any(b.get(f) == val for b in back)
                _p(f"  {f}={val!r}: sku-filter returned {len(back)} rows, round-trip={hit}")
                if hit and sku_field is None:
                    sku_field = f
            except Exception as e:
                _p(f"  {f}: query error {e}")
        _p(f"  => SKU field appears to be: {sku_field or 'UNKNOWN (inspect item dump below)'}")
        if not sku_field:
            _p("  first item (for manual inspection):")
            for k, v in it.items():
                if isinstance(v, str) and v:
                    _p(f"      {k} = {v[:40]}")

    # 4) keyword matches MPN / ASIN? -------------------------------------
    _p("\n[4] does model.keyword match ManufacturerSKU (MPN) and ASIN?")
    try:
        # pull a wider page to find items carrying an MPN and an ASIN
        wide = client.search_catalog(page_size=50).get("Items") or []
        mpn_item = next((x for x in wide if x.get("ManufacturerSKU")), None)
        asin_item = next((x for x in wide if x.get("ASIN")), None)
        if not asin_item:
            shadows = client.search_catalog(page_size=50, display_shadows=1).get("Items") or []
            asin_item = next((x for x in shadows if x.get("ASIN")), None)

        if mpn_item:
            mpn = mpn_item["ManufacturerSKU"]
            res = client.search_catalog(keyword=mpn, page_size=10).get("Items") or []
            hit = any(x.get("ManufacturerSKU") == mpn for x in res)
            _p(f"  keyword=MPN {mpn!r}: {len(res)} rows, exact-MPN present={hit}")
        else:
            _p("  (no item with ManufacturerSKU in sample — inconclusive)")

        if asin_item:
            asin = asin_item["ASIN"]
            res = client.search_catalog(keyword=asin, page_size=10).get("Items") or []
            hit = any(x.get("ASIN") == asin for x in res)
            _p(f"  keyword=ASIN {asin!r}: {len(res)} rows, exact-ASIN present={hit}")
        else:
            _p("  (no item with ASIN in sample — inconclusive)")
    except Exception as e:
        _p(f"  FAIL: {e}")

    # 5) manufacturers / brands ------------------------------------------
    _p("\n[5] Settings/Manufacturers and Settings/Brands shapes")
    for name, fn in [("Manufacturers", client.get_manufacturers),
                     ("Brands", client.get_brands)]:
        try:
            d = fn()
            if isinstance(d, list):
                _p(f"  {name}: list len={len(d)}, sample keys={_keys(d[0]) if d else '[]'}")
            elif isinstance(d, dict):
                _p(f"  {name}: dict keys={_keys(d)}")
            else:
                _p(f"  {name}: {type(d).__name__}")
        except Exception as e:
            _p(f"  {name}: FAIL {e}")

    # 6) max pageSize -----------------------------------------------------
    _p("\n[6] max pageSize behaviour (request 500)")
    try:
        data = client.search_catalog(page_size=500)
        _p(f"  requested 500 -> returned {len(data.get('Items') or [])} "
           f"(TotalResults={data.get('TotalResults')})")
    except Exception as e:
        _p(f"  FAIL: {e}")

    _p("\nprobe complete.")


if __name__ == "__main__":
    main()
