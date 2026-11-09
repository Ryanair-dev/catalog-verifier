from __future__ import annotations

from mcp_server.app import mcp


@mcp.prompt(
    name="research_asins",
    title="Research Amazon ASINs",
    description="Find relevant Amazon listings from a UPC, item ID, or product description.",
)
def research_asins(product: str, brand: str = "") -> str:
    """Guide Claude through product search and matching saved Catalog Verifier data."""
    brand_context = f"\nBrand, if known: {brand.strip()}" if brand.strip() else ""
    return f"""Research Amazon listings for this product:
{product.strip()}{brand_context}

Use the Catalog Verifier `find_asins` tool. If the user identifies the input as
a UPC, pass it as `upc`; if they identify it as an item ID or MPN, pass it as
`item_id`; otherwise use the product description as `title`. If a code's type
is unclear, ask before searching. Pass the brand only if one was provided.

If the input is a UPC or item ID, also use `lookup_product` to check the Pair
Library, rejected pairs, and saved Offer Analysis history. Do not use that lookup
for a description alone.

Present every candidate returned by the search with its ASIN, product title,
confidence, and verdict when available. Briefly explain why the strongest
candidates match, mention any saved pair/history that may affect the decision,
and flag conflicts. Do not invent ASINs or claim the search is exhaustive; say
when no strong candidate was found."""


@mcp.prompt(
    name="check_asins",
    title="Check ASINs",
    description="Check ASINs for eligibility, storage fees, or generic/DOG status.",
)
def check_asins(asins: str, checks: str = "eligibility, storage, generic") -> str:
    """Guide Claude through starting and reporting Catalog Verifier ASIN checks."""
    return f"""Run Catalog Verifier checks for these ASINs:
{asins.strip()}

Requested checks: {checks.strip() or "eligibility, storage, generic"}

Use the `check_asins` tool. Convert the ASIN text into a list, remove duplicates,
and use only these check names: `eligibility`, `storage`, and `generic`. If the
requested checks do not clearly match those names, ask the user which checks
they want before starting. Do not add checks the user did not request.

The tool starts background jobs. Report which checks started, keep the returned
job IDs, and use `job_status` with the matching kind if a result is not ready.
Summarize the results briefly and clearly distinguish completed checks from
jobs that are still running or failed."""
