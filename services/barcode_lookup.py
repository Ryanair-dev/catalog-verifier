"""
Barcode database lookup chain.

Priority (first hit wins):
  1. Open Food Facts
  2. Open Beauty Facts
  3. Open Products Facts
  4. UPC Item DB (trial, keyless)
  5. DuckDuckGo HTML scrape (last resort)

Every provider call is wrapped in a timeout and exception guard — a failure in
one source never blocks the chain. All calls are fully async; the caller can
fan many out concurrently via ``asyncio.gather`` without additional coordination.
"""
from __future__ import annotations

import asyncio
import html as _html
import re
from typing import Any

import httpx

OFF_URL = "https://world.openfoodfacts.org/api/v0/product/{upc}.json"
OBF_URL = "https://world.openbeautyfacts.org/api/v0/product/{upc}.json"
OPF_URL = "https://world.openproductsfacts.org/api/v0/product/{upc}.json"
UPCITEMDB_URL = "https://api.upcitemdb.com/prod/trial/lookup"
DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"

USER_AGENT = (
    "CatalogVerifier/1.0 (+https://example.local) "
    "Mozilla/5.0 (compatible; verification-tool)"
)


def _digits_only(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


# --------------------------------------------------------------------------- #
# Individual providers
# --------------------------------------------------------------------------- #

async def _off_style(
    client: httpx.AsyncClient, url: str, source_name: str, barcode: str,
) -> dict | None:
    """All three Open * Facts endpoints share the same response shape."""
    try:
        resp = await client.get(url.format(upc=barcode), timeout=8.0)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("status") != 1:
            return None
        product = data.get("product") or {}
        name = (
            product.get("product_name")
            or product.get("product_name_en")
            or product.get("generic_name")
        )
        if not name:
            return None
        return {
            "source": source_name,
            "name": name,
            "brand": product.get("brands"),
            "size": product.get("quantity"),
            "categories": product.get("categories"),
            "raw_url": url.format(upc=barcode),
        }
    except Exception:  # noqa: BLE001
        return None


async def _lookup_off(client, barcode):
    return await _off_style(client, OFF_URL, "Open Food Facts", barcode)

async def _lookup_obf(client, barcode):
    return await _off_style(client, OBF_URL, "Open Beauty Facts", barcode)

async def _lookup_opf(client, barcode):
    return await _off_style(client, OPF_URL, "Open Products Facts", barcode)


async def _lookup_upcitemdb(client: httpx.AsyncClient, barcode: str) -> dict | None:
    try:
        resp = await client.get(
            UPCITEMDB_URL, params={"upc": barcode}, timeout=8.0,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        items = data.get("items") or []
        if not items:
            return None
        top = items[0]
        return {
            "source": "UPC Item DB",
            "name": top.get("title"),
            "brand": top.get("brand"),
            "size": top.get("size"),
            "categories": top.get("category"),
            "raw_url": UPCITEMDB_URL + f"?upc={barcode}",
        }
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# DuckDuckGo scrape (last resort)
# --------------------------------------------------------------------------- #

DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]*>(?P<title>.*?)</a>.*?'
    r'class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

def _strip_tags(snippet: str) -> str:
    clean = re.sub(r"<[^>]+>", "", snippet or "")
    return _html.unescape(clean).strip()


async def _lookup_duckduckgo(client: httpx.AsyncClient, barcode: str) -> dict | None:
    query = f"UPC {barcode} product"
    try:
        resp = await client.post(
            DUCKDUCKGO_URL,
            data={"q": query, "kl": "us-en"},
            headers={"User-Agent": USER_AGENT},
            timeout=10.0,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return None
        html = resp.text
        match = DDG_RESULT_RE.search(html)
        if not match:
            return None
        title = _strip_tags(match.group("title"))
        snippet = _strip_tags(match.group("snippet"))
        if not title:
            return None
        return {
            "source": "DuckDuckGo",
            "name": title,
            "brand": None,
            "size": None,
            "categories": snippet[:200],
            "raw_url": None,
        }
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Chain driver
# --------------------------------------------------------------------------- #

CHAIN = [
    ("Open Food Facts",   _lookup_off),
    ("Open Beauty Facts", _lookup_obf),
    ("Open Products Facts", _lookup_opf),
    ("UPC Item DB",       _lookup_upcitemdb),
    ("DuckDuckGo",        _lookup_duckduckgo),
]


async def lookup_barcode(barcode: str) -> dict:
    """Run the full priority chain, stopping at the first hit."""
    code = _digits_only(barcode)
    if not code:
        return {"found": False, "reason": "No barcode", "source": None, "chain": []}

    tried: list[str] = []
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        for name, fn in CHAIN:
            tried.append(name)
            try:
                result = await fn(client, code)
            except Exception:  # noqa: BLE001
                result = None
            if result and result.get("name"):
                result["found"] = True
                result["chain"] = tried
                return result

    return {"found": False, "reason": "Not Found", "source": None, "chain": tried}


def aligned_with(lookup: dict, catalog_row: dict) -> bool:
    """Cheap sanity check — does the returned name share tokens with the catalog row?"""
    if not lookup.get("found"):
        return False
    name = (lookup.get("name") or "").lower()
    brand = (lookup.get("brand") or "").lower()
    cat_brand = str(catalog_row.get("Brand") or "").lower()
    cat_title = str(catalog_row.get("Vendor Title") or "").lower()
    if cat_brand and cat_brand in (brand or name):
        return True
    overlap = set(cat_title.split()) & set(name.split())
    return len(overlap) >= 2
