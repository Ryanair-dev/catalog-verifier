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


@mcp.prompt(
    name="report_offer_problem",
    title="Report a problem with an offer",
    description="Send pairs the tool got wrong: Claude checks each one, names the cause, rates urgency, and logs it.",
)
def report_offer_problem(run: str, pairs: str, reported_by: str = "", note: str = "") -> str:
    """Guide Claude from a clerk's complaint about an offer to checked, rated, logged pairs."""
    extra = f"\nWhat the clerk said: {note.strip()}" if note.strip() else ""
    who = reported_by.strip() or "(ask for their name)"
    return f"""A clerk reports problems with an Offer Analysis run.
Run (ID or name): {run.strip()}
Reported by: {who}{extra}
Pairs (one per line: Item ID, ASIN, correct or wrong; or the attached Excel with these 3 columns):
{pairs.strip()}

Your job: check each pair, say honestly what happened, rate it, and log it after the clerk agrees.
You never change a verdict. You only read, explain, and (after a yes) send reports.

STEP 1 - Make sure the input is usable.
If the run, an Item ID, an ASIN, or "correct / wrong" is missing or unclear, ask ONE short question
listing exactly what is missing, then stop. Do not guess.

STEP 2 - Find the run and each pair.
- If the run is a name, call `list_runs` and pick the run with that name; if several fit, ask which one.
- For each pair, call `lookup_product` with item_id=<Item ID> and find the saved match from THIS run
  to get its 1-based row. Then call `explain_pair` with run_id, row and the ASIN.
- If the ASIN is not among that row's listings, the tool never found it: outcome "not found".

STEP 3 - For each pair, decide ONE outcome:
  A. wrong pair approved           (clerk says wrong, the tool approved it)
  B. correct pair rejected         (clerk says correct, the tool rejected it)
  C. correct pair not found        (clerk says correct, the listing is not in the run)
  D. correct pair only in Review   (clerk says correct, the tool sent it to Review)
  E. the tool was actually right   (for example: another listing of the same item, a pack the vendor
                                    also sells, or the clerk's label looks wrong; say why, politely)
Compare the vendor and Amazon titles, brands, sizes, colors, counts and part numbers yourself, and read
the saved reasons from `explain_pair`.

STEP 4 - Name ONE cause per real mistake (A-D), from this list only:
  size, color_or_variant, pack_or_count, brand_or_sub_brand, part_number, bundle, not_found_by_search,
  wrong_label, other

STEP 5 - Urgency of the whole report, by this fixed rule (not by feeling):
  HIGH   = any outcome A, or the same cause in 3 or more pairs of this report
  MEDIUM = otherwise, any outcome B or C
  LOW    = otherwise (only D or E)

STEP 6 - Answer the clerk in plain language, short:
  "You reported N pairs. X were real mistakes (cause ...). Y were correct because ... Urgency: ..."
  Then a small table: Item ID, ASIN, outcome, cause, one-line reason.
  Be honest and friendly. Do not promise a fix date.

STEP 7 - Logging, only after a clear yes.
Show the reports you would send, then ask "Send these N reports?". After a yes, call `report_match`
once per pair with confirm=true:
  - outcome A: label "different"; reasons from: pack_size, size_volume, variant, brand,
    different_product, id_mismatch, other (bundle -> different_product)
  - outcomes B, C, D: label "same"; reasons from: same_product_wording, vendor_title_unclear,
    amazon_title_messy, pack_equivalent, other
  - outcome E: do NOT send a report
  - note: start with "[urgency: <HIGH|MEDIUM|LOW>] [outcome: <A-D>] [cause: <cause>]" and then one
    short sentence
  - reported_by: the clerk's name
Never send a report without that yes. If a report fails, say which pair and what the app answered."""