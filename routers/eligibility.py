"""
Listing Eligibility Checker endpoints.

POST /api/eligibility/check              — submit ASINs, start 5-worker background job
GET  /api/eligibility/jobs/{job_id}      — poll progress + live results
GET  /api/eligibility/jobs/{job_id}/export — CSV download (available at any point)
"""
from __future__ import annotations

import csv
import io
import os
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from rapidfuzz import fuzz

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.spapi import get_catalog_api
from services.restrictions import classify, get_restrictions_api
from services.spapi.config import load_sp_api_credentials, sp_api_configured, list_stores
from services.safety import safe_spreadsheet_value
from services import job_persistence

router = APIRouter()

# ── In-memory job store ───────────────────────────────────────────────────────
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 3600  # evict completed/error jobs after 1 hour

WORKERS = 5
MAX_ASINS = 5_000
BRAND_CHECK_MAX = 300   # hard safety cap regardless of sample_size/sample_pct
_BRAND_AI_CACHE: dict[str, str] = {}   # brand (lowered) -> resolved Amazon brand name
# A resolved candidate must have at least this many REAL Amazon results under its own
# name before it is trusted -- confirmed live 2026-09-09: without this floor, a wrong
# candidate with a thin/coincidental presence (e.g. "OUTFIT7" -> "Drag Racing Outfit7",
# verified total=1) could still slip through.
_MIN_VERIFIED_TOTAL = 5


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_job(job_id: str) -> dict:
    now = time.time()
    with _JOBS_LOCK:
        # TTL counts from when the job was first seen finished, not from its
        # start -- measured from start, a run longer than the TTL (a ~200-brand
        # Check Brand) was evicted the instant it completed (2026-10-30).
        for j in _JOBS.values():
            if j.get("status") != "running":
                j.setdefault("finished_seen_at", now)
        stale = [
            jid for jid, j in _JOBS.items()
            if j.get("status") != "running" and now - j["finished_seen_at"] > _JOB_TTL
        ]
        for jid in stale:
            _JOBS.pop(jid, None)
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _eta(job: dict) -> int | None:
    done = job["done"]
    if done == 0 or job["status"] != "running":
        return None
    elapsed = time.time() - job["started_at"]
    return max(0, round(elapsed / done * (job["total"] - done)))


_JOB_KIND_LABELS = {
    "brand_check": "Toolbox: Check Brand",
}


def list_jobs_for_tasks_panel() -> list[dict]:
    """Normalized snapshot of every tracked job (running or recently
    finished) for the Settings > Tasks Running tab -- see routers/settings.py
    tasks_running(). Reads the live dicts directly; no copy/lock needed
    beyond what _JOBS_LOCK already guards against concurrent mutation.

    _JOBS is shared by 2 different job kinds now (plain Eligibility Check,
    and -- since 2026-10-27 -- Check Brand's bulk background jobs), tagged
    via each job's own "kind" field (absent = eligibility, the original job
    type here before Check Brand got one) so each gets its correct label
    instead of both being mislabeled as one feature."""
    with _JOBS_LOCK:
        jobs = list(_JOBS.values())
    return [
        {
            "feature": _JOB_KIND_LABELS.get(j.get("kind"), "Toolbox: Eligibility Check"),
            "job_id": j.get("job_id"),
            "status": j.get("status"),
            "done": j.get("done"),
            "total": j.get("total"),
            "started_at": j.get("started_at"),
        }
        for j in jobs
    ]


# ── Schema ────────────────────────────────────────────────────────────────────

class CheckBody(BaseModel):
    asins: list[str]
    store: str = "default"   # which seller account's restrictions to check — see
                              # services/spapi/config._STORES ("default" = Priority
                              # Pharmacy, "turba" = Turba)


class CheckBrandBody(BaseModel):
    brand: str
    sample_size: int = 8            # fixed count of non-DOG ASINs to check
    sample_pct: float | None = None  # OR: % (0-100) of the brand's total Amazon
                                      # ASINs -- takes precedence over sample_size
                                      # when set (both capped by BRAND_CHECK_MAX)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/eligibility/status")
async def eligibility_status() -> dict:
    """Probe whether SP-API + seller ID are configured."""
    configured = sp_api_configured()
    seller_ok = False
    if configured:
        try:
            creds = load_sp_api_credentials()
            seller_ok = bool(creds.seller_id)
        except RuntimeError:
            pass
    return {"sp_api_configured": configured, "seller_id_configured": seller_ok}


@router.get("/eligibility/stores")
async def eligibility_stores() -> dict:
    """Every known store (seller account) + whether it's usable right now,
    for the Eligibility Check store-switch button."""
    return {"stores": list_stores()}


def _related(query: str, candidate: str) -> bool:
    """True if `candidate` is textually close enough to `query` to trust even
    with weak vote/sample support -- e.g. a generic suffix stripped ("Buzz Bee
    Toys" -> "Buzz Bee"). Confirmed live 2026-09-09 this needs BOTH conditions:
    `partial_ratio` alone (near-full substring containment) is NOT enough --
    "OUTFIT7" is fully contained in the unrelated "Drag Racing Outfit7" too
    (partial_ratio 100, same as the genuinely good cases), so a length-ratio
    floor is required alongside it (0.37 for that pair vs 0.42-0.62 for the
    real "generic suffix stripped" cases like Buzz Bee Toys/Educa Borras)."""
    q, c = query.strip().lower(), candidate.strip().lower()
    if not q or not c:
        return False
    if fuzz.partial_ratio(q, c) < 90:
        return False
    shorter, longer = sorted((len(q), len(c)))
    return longer > 0 and shorter / longer >= 0.4


def _resolve_brand_spapi_votes(catalog_api, brand: str) -> tuple[str, float]:
    """Tally the real Amazon `brand` attribute among a plain SP-API keyword
    search's results (no brandNames filter -- matches on TITLE too). Returns
    (top_candidate, vote_fraction) or ("", 0.0) if nothing usable comes back.

    The vote fraction ALONE is not a reliable accept/reject signal -- confirmed
    live 2026-09-09: "Buzz Bee Toys" -> "Buzz Bee" (CORRECT) and "Alpha Toys" ->
    "Siiziitoo" (WRONG, pure keyword-noise: "Alpha" matched 4,133 unrelated
    alphabet-toy/RC-helicopter/action-figure listings) have the IDENTICAL
    signature (2/20 votes, 10% fraction). The caller must also apply `_related`
    (Buzz Bee is a clean textual prefix of the query; Siiziitoo bears zero
    resemblance to Alpha Toys) before trusting a low-fraction result."""
    try:
        data = catalog_api.search_by_keywords(brand, page_size=20)
    except Exception:
        return "", 0.0
    items = data.get("items", [])
    if not items:
        return "", 0.0
    votes: Counter = Counter()
    for item in items:
        for v in (item.get("attributes") or {}).get("brand") or []:
            name = (v.get("value") or "").strip()
            if name:
                votes[name] += 1
    if not votes:
        return "", 0.0
    top, count = votes.most_common(1)[0]
    return top, count / len(items)


def _resolve_brand_keepa(brand: str) -> tuple[str, float]:
    """Discover a brand's real Amazon `brand`/`manufacturer` value via Keepa's
    Product Finder -- an EXACT match against Keepa's own STRUCTURED brand/
    manufacturer fields (services.keepa.discover_entities), not free-text
    search, so it can't be fooled by an unrelated product merely mentioning the
    search term in its title. Returns (top_candidate, sample_fraction) or
    ("", 0.0) if Keepa isn't configured, errors, or finds nothing.

    Still needs the SAME gate as the SP-API vote resolver, just via a
    different failure mode -- confirmed live 2026-09-09: for "Alpha Toys",
    Keepa's exact match found real, populous, but UNRELATED brands (top:
    "Siku", 894 real Amazon products, 44% of the sample) -- a real company,
    just not the one being asked about. Structured-field exactness prevents
    keyword-noise false positives, but does NOT by itself guarantee semantic
    relevance, so this is gated by `_related`/fraction exactly like the other
    resolver. Trade-off vs the SP-API resolver: Keepa needs an EXACT existing
    seed value, so it cannot fix a genuine spelling typo on its own (confirmed:
    "Bodumm" finds nothing here) -- that's the AI resolver's job."""
    try:
        from services import keepa
        if not keepa.is_configured():
            return "", 0.0
        result = keepa.discover_entities(brand, sample_size=50, max_values=5)
    except Exception:
        return "", 0.0
    entities = result.get("entities") or []
    if not entities:
        return "", 0.0
    top = entities[0]   # already sorted by total desc
    sample_count = top.get("sample_count", 0)
    if sample_count <= 0:
        return "", 0.0
    return top["value"], sample_count / 50.0


def _resolve_brand_ai(brand: str) -> str:
    """Last-resort fallback ONLY -- if the mechanical resolver above also found
    nothing (e.g. Amazon's keyword search returned no matches at all). Asks
    Claude what the real Amazon brand/manufacturer name is for a product-line
    or sub-brand name. Cheap (~30 output tokens, tiny prompt), cached per brand,
    best-effort -- returns "" on any failure so this can never break the check.
    Uses Sonnet (not the Haiku used elsewhere in this file for bulk brand/
    manufacturer guessing) per user request -- this only fires rarely, as a
    fallback of a fallback, so the extra cost is negligible."""
    key = brand.strip().lower()
    if key in _BRAND_AI_CACHE:
        return _BRAND_AI_CACHE[key]
    result = ""
    try:
        import anthropic
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if api_key:
            client = anthropic.Anthropic(api_key=api_key)
            model = os.getenv("BRAND_RESOLUTION_MODEL") or "claude-sonnet-5"
            msg = client.messages.create(
                model=model, max_tokens=30,
                messages=[{"role": "user", "content":
                    f"On Amazon.com, the product line or sub-brand \"{brand}\" is "
                    f"usually sold under which manufacturer or parent brand name "
                    f"(the exact string Amazon's own catalog 'Brand' field would "
                    f"show)? Reply with ONLY that brand name, nothing else. If "
                    f"unsure, reply UNKNOWN."}],
            )
            text = "".join(getattr(b, "text", "") for b in msg.content).strip()
            if text and text.upper() != "UNKNOWN":
                result = text
    except Exception:
        result = ""
    _BRAND_AI_CACHE[key] = result
    return result


def _run_brand_check(brand: str, sample_size: int, sample_pct: float | None) -> dict:
    """
    Brand-wide gating check. Amazon's Listings Restrictions API is per-ASIN only
    -- there is no brand-level endpoint -- but a brand block message ("You are
    not approved to list this brand...") is inherently brand-wide, not per-SKU
    (confirmed live 2026-09-04: 4/4 non-DOG ASINs of a gated brand all came back
    identically RESTRICTED). So: find a handful of the brand's ASINs straight
    from Amazon's own Catalog Items search (SP-API `searchCatalogItems` via
    `search_by_keywords(keywords=brand, brand_names=[brand])`), sample the LIVE
    ones (skip DOG -- a deactivated ASIN says nothing about the brand), and
    report one verdict.

    NOTE (2026-09-04): originally this pulled the brand's ASINs from our own
    SellerCloud/Azure catalog -- unnecessary AND much slower (a live Azure pull
    of the ~180k-row view can take minutes, whereas this Amazon-native search
    takes ~8s) since we don't need OUR inventory at all here: `getListingsRestrictions`
    answers "can this seller list THIS ASIN" for any Amazon ASIN, ours or not.
    Also tried `search_by_brand_names()` (brandNames with no keywords) first --
    Amazon rejects that with 400 "Missing required identifiers or keywords",
    despite its docstring's claim; the working call needs `keywords` too.

    Pure logic, no FastAPI dependency -- shared by the single-brand endpoint
    below and the bulk background-job endpoints (check_brand_start), so a
    bulk run's per-brand work is identical to a standalone check. Raises
    HTTPException on failure either way; the bulk job's worker thread catches
    it and turns it into an ERROR-verdict result instead of crashing.
    """
    try:
        creds = load_sp_api_credentials()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not creds.seller_id:
        raise HTTPException(
            status_code=503,
            detail="AMZ_SELLER_ID is not set. Add it to your .env file and restart.",
        )

    catalog_api = get_catalog_api()
    search_brand = brand   # may get replaced by a resolved Amazon brand name below

    def _search_page(name: str, page_token: str | None):
        return catalog_api.search_by_keywords(
            name, brand_names=[name], page_token=page_token, page_size=20,
        )

    # First page tells us the brand's TOTAL Amazon result count, which a
    # percentage-based sample needs before it can even compute how many ASINs
    # to look for -- so this always fetches page 1 up front, then decides the
    # cap, then keeps paginating (reusing page 1's items, no double-fetch).
    try:
        data = _search_page(search_brand, None)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Amazon catalog search failed: {exc}")

    total_amazon_results = data.get("numberOfResults", 0)
    resolved_as: str | None = None

    if not total_amazon_results:
        # The queried name may be a sub-brand/product-line name that does not
        # match Amazon's own canonical `brand` catalog attribute (confirmed live
        # 2026-09-04: "ColorStay" is a Revlon product line -- Amazon's actual
        # brand field for those ASINs is "REVLON", not "ColorStay"). Try a
        # ranked list of candidate sources -- SP-API keyword-vote first (free,
        # same call already in play), then Keepa's structured field match
        # (free of keyword noise but needs an exact existing seed), then Claude
        # as a last resort for genuine spelling typos neither of the above can
        # fix. Each candidate must pass BOTH a relevance gate (strong vote/
        # sample consensus, OR close textual relation via `_related`) and a
        # final floor (a real, non-trivial Amazon presence under its own name)
        # before being trusted -- confirmed live 2026-09-09 that skipping
        # either check lets a wrong-but-real-looking candidate through (see
        # `_related`'s docstring for the exact cases that motivated this).
        candidates: list[str] = []

        spapi_cand, spapi_frac = _resolve_brand_spapi_votes(catalog_api, brand)
        if spapi_cand and (spapi_frac >= 0.5 or _related(brand, spapi_cand)):
            candidates.append(spapi_cand)

        keepa_cand, keepa_frac = _resolve_brand_keepa(brand)
        if (keepa_cand and keepa_cand not in candidates
                and (keepa_frac >= 0.5 or _related(brand, keepa_cand))):
            candidates.append(keepa_cand)

        ai_cand = _resolve_brand_ai(brand)
        if ai_cand and ai_cand not in candidates:
            candidates.append(ai_cand)   # AI isn't vote-based -- no fraction/relatedness gate,
                                          # but still subject to the final verification below

        for cand in candidates:
            if cand.strip().lower() == brand.strip().lower():
                continue
            try:
                cand_data = _search_page(cand, None)
            except Exception as exc:
                raise HTTPException(status_code=503, detail=f"Amazon catalog search failed: {exc}")
            n = cand_data.get("numberOfResults", 0)
            if n >= _MIN_VERIFIED_TOTAL:
                data = cand_data
                total_amazon_results = n
                search_brand = cand
                resolved_as = cand
                break

    if not total_amazon_results:
        raise HTTPException(
            status_code=404,
            detail=(f"No Amazon ASINs found for brand '{brand}' (0 catalog results, "
                    f"brand-name resolution did not find a confident match either)."),
        )

    if sample_pct is not None and sample_pct > 0:
        wanted = max(1, round(total_amazon_results * min(sample_pct, 100) / 100))
    else:
        wanted = max(1, sample_size or 8)
    cap = min(wanted, BRAND_CHECK_MAX, total_amazon_results)
    max_wanted = cap * 3   # over-fetch candidates since some will turn out DOG

    asins = [it["asin"] for it in data.get("items", []) if it.get("asin")]
    page_token = (data.get("pagination") or {}).get("nextToken")
    # ceil(max_wanted / 20) more pages, +1 buffer, hard-capped so a huge brand
    # with a huge requested sample can't spin forever
    max_pages = min(20, -(-max_wanted // 20) + 1)
    page_count = 1
    try:
        while len(asins) < max_wanted and page_token and page_count < max_pages:
            data = _search_page(search_brand, page_token)
            asins.extend(it["asin"] for it in data.get("items", []) if it.get("asin"))
            page_token = (data.get("pagination") or {}).get("nextToken")
            page_count += 1
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Amazon catalog search failed: {exc}")

    api = get_restrictions_api()
    seller_id = creds.seller_id
    attempt_pool = asins[: cap * 3]

    def check_one(asin: str) -> dict:
        raw = api.get_restrictions(asin, seller_id)
        return classify(asin, raw)

    checked: list[dict] = []
    dog_count = 0
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(check_one, a) for a in attempt_pool]
        for future in as_completed(futures):
            try:
                r = future.result()
            except Exception:  # noqa: BLE001
                continue
            if r.get("dog"):
                dog_count += 1
            else:
                checked.append(r)
            if len(checked) >= cap:
                break   # remaining in-flight futures still finish (small pool, harmless)

    statuses = Counter(r["status"] for r in checked)
    STATUS_LABEL = {
        "CAN_SELL":       "open to sell",
        "NEEDS_APPROVAL": "needs approval",
        "RESTRICTED":     "restricted / not accepting applications",
        "ERROR":          "errored",
    }
    resolved_note = f" (resolved to Amazon brand \"{resolved_as}\")" if resolved_as else ""
    if not checked:
        verdict = "ALL_DOG"
        summary = (f"All {dog_count} sampled ASINs for '{brand}'{resolved_note} are "
                   f"deactivated (DOG) -- inconclusive, try a larger sample_size.")
    elif len(statuses) == 1:
        verdict = next(iter(statuses))
        summary = (f"{len(checked)}/{len(checked)} checked ASINs for "
                   f"'{brand}'{resolved_note} are {STATUS_LABEL.get(verdict, verdict)}.")
    else:
        # Majority wins (2026-10-30, per user): 299 needs-approval + 1 restricted
        # is "needs approval", not MIXED; 286 open + 14 needs-approval is "can
        # sell". MIXED only when no status covers more than half the sample.
        parts = ", ".join(f"{n} {STATUS_LABEL.get(s, s)}" for s, n in statuses.most_common())
        top, top_n = statuses.most_common(1)[0]
        if top_n * 2 > len(checked):
            verdict = top
            summary = (f"Mostly {STATUS_LABEL.get(top, top)} for '{brand}'{resolved_note} "
                       f"({top_n}/{len(checked)}): {parts}.")
        else:
            verdict = "MIXED"
            summary = f"Mixed results for '{brand}'{resolved_note}: {parts} (of {len(checked)} checked)."

    return {
        "brand":                  brand,
        "resolved_as":            resolved_as,
        "total_asins_in_catalog": total_amazon_results,
        "checked":                checked,
        "dog_skipped":            dog_count,
        "verdict":                verdict,
        "summary":                summary,
    }


@router.post("/eligibility/check-brand")
async def check_brand(body: CheckBrandBody) -> dict:
    brand = (body.brand or "").strip()
    if not brand:
        raise HTTPException(status_code=400, detail="Brand name required.")
    return _run_brand_check(brand, body.sample_size, body.sample_pct)


class CheckBrandBulkBody(BaseModel):
    brands: list[str]
    sample_size: int = 8
    sample_pct: float | None = None


BRAND_CHECK_CONCURRENCY = 2   # matches the old client-side worker-pool size exactly


@router.post("/eligibility/check-brand/start")
async def check_brand_start(body: CheckBrandBulkBody) -> dict:
    """Bulk/background version of check-brand -- added 2026-10-27 so a brand
    check (single or multi-brand) shows up in the Settings > Tasks Running
    tab with real progress, instead of running entirely client-side with no
    server-tracked state at all (confirmed live: the old `_checkBrands()`
    frontend loop called the plain /check-brand endpoint once per brand with
    its OWN client-side Promise.all concurrency pool -- nothing on the
    backend ever knew a check was in progress)."""
    brands = list(dict.fromkeys(b.strip() for b in (body.brands or []) if b.strip()))
    if not brands:
        raise HTTPException(status_code=400, detail="At least one brand name required.")

    job_id = str(uuid.uuid4())
    job: dict = {
        "job_id": job_id, "kind": "brand_check", "status": "running",
        "done": 0, "total": len(brands), "results": [], "error": None,
        "cancel": False, "started_at": time.time(),
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    def run() -> None:
        results_lock = threading.Lock()

        def worker(brand: str) -> None:
            if job["cancel"]:
                return
            try:
                r = _run_brand_check(brand, body.sample_size, body.sample_pct)
            except HTTPException as exc:
                r = {"brand": brand, "verdict": "ERROR", "summary": str(exc.detail),
                     "total_asins_in_catalog": 0, "checked": [], "dog_skipped": 0}
            except Exception as exc:  # noqa: BLE001
                r = {"brand": brand, "verdict": "ERROR", "summary": str(exc)[:300],
                     "total_asins_in_catalog": 0, "checked": [], "dog_skipped": 0}
            with results_lock:
                job["results"].append(r)
                job["done"] += 1

        try:
            with ThreadPoolExecutor(max_workers=BRAND_CHECK_CONCURRENCY) as pool:
                futures = [pool.submit(worker, b) for b in brands]
                for f in as_completed(futures):
                    f.result()
            job["status"] = "cancelled" if job["cancel"] else "complete"
        except Exception as exc:  # noqa: BLE001
            job["status"], job["error"] = "error", str(exc)[:400]

    threading.Thread(target=run, name=f"brand-check-{job_id[:8]}", daemon=True).start()
    return {"job_id": job_id, "total": len(brands)}


@router.get("/eligibility/check-brand/jobs/{job_id}")
async def check_brand_job_status(job_id: str) -> dict:
    job = _get_job(job_id)
    return {
        "job_id": job_id, "status": job["status"], "done": job["done"],
        "total": job["total"], "results": job["results"], "error": job.get("error"),
    }


@router.post("/eligibility/check-brand/jobs/{job_id}/cancel")
async def check_brand_job_cancel(job_id: str) -> dict:
    job = _get_job(job_id)
    job["cancel"] = True
    return {"cancelled": True}


@router.post("/eligibility/check")
async def start_check(body: CheckBody) -> dict:
    # Clean + deduplicate
    asins = list(dict.fromkeys(a.strip().upper() for a in body.asins if a.strip()))
    if not asins:
        raise HTTPException(status_code=400, detail="No valid ASINs provided.")
    if len(asins) > MAX_ASINS:
        raise HTTPException(
            status_code=400,
            detail=f"Max {MAX_ASINS} ASINs per request (received {len(asins)}).",
        )

    store = (body.store or "default").strip().lower()

    # Validate credentials early for a clean error message
    try:
        creds = load_sp_api_credentials(store)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    if not creds.seller_id:
        raise HTTPException(
            status_code=503,
            detail=(
                f"No seller ID is configured for the {store!r} store. "
                "Add it to your .env file and restart — "
                "the Listings Restrictions API requires a seller ID."
            ),
        )

    job_id = _launch_check(asins, store, creds.seller_id)
    return {"job_id": job_id, "total": len(asins), "store": store}


def _launch_check(asins: list[str], store: str, seller_id: str | None = None) -> str:
    """The actual job-launch logic, shared by the live POST endpoint and the
    startup relauncher (job_persistence.resume_orphaned_jobs)."""
    if seller_id is None:
        creds = load_sp_api_credentials(store)
        seller_id = creds.seller_id

    job_id = str(uuid.uuid4())
    job: dict = {
        "job_id":     job_id,
        "total":      len(asins),
        "done":       0,
        "status":     "running",
        "results":    [],
        "started_at": time.time(),
        "store":      store,
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job
    job_persistence.save_job(job_id, "eligibility", {"asins": asins, "store": store})

    # ── Background worker ──────────────────────────────────────────────────
    def run() -> None:
        try:
            api  = get_restrictions_api(store)
            lock = threading.Lock()

            def check_one(asin: str) -> dict:
                try:
                    raw = api.get_restrictions(asin, seller_id)
                    return classify(asin, raw)
                except PermissionError as exc:
                    return {
                        "asin": asin, "status": "ERROR", "dog": False,
                        "reasons": [{"type": "PERMISSION_ERROR", "message": str(exc),
                                     "hint": "", "can_request": False, "approval_url": None}],
                    }
                except Exception as exc:  # noqa: BLE001
                    return {
                        "asin": asin, "status": "ERROR", "dog": False,
                        "reasons": [{"type": "API_ERROR", "message": str(exc),
                                     "hint": "", "can_request": False, "approval_url": None}],
                    }

            try:
                with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                    futures = {pool.submit(check_one, asin): asin for asin in asins}
                    for future in as_completed(futures):
                        result = future.result()
                        with lock:
                            job["results"].append(result)
                            job["done"] += 1
                job["status"] = "complete"
            except Exception as exc:  # noqa: BLE001
                job["status"] = "error"
                job["error"]  = str(exc)
        finally:
            job_persistence.finish_job(job_id)

    threading.Thread(target=run, daemon=True).start()
    return job_id


def _relaunch(params: dict) -> None:
    asins = [str(a) for a in (params.get("asins") or []) if str(a).strip()]
    store = params.get("store") or "default"
    if not asins:
        return
    _launch_check(asins, store)


job_persistence.register_relauncher("eligibility", _relaunch)


@router.get("/eligibility/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = _get_job(job_id)
    return {
        "job_id":      job_id,
        "total":       job["total"],
        "done":        job["done"],
        "status":      job["status"],
        "eta_seconds": _eta(job),
        "results":     job["results"],
        "store":       job.get("store", "default"),
    }


@router.get("/eligibility/jobs/{job_id}/export")
async def export_job(job_id: str) -> StreamingResponse:
    """Two-column CSV: ASIN, status."""
    job = _get_job(job_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["asin", "status"])
    for r in job["results"]:
        writer.writerow([
            safe_spreadsheet_value(r["asin"]),
            safe_spreadsheet_value(r["status"]),
        ])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=eligibility_results.csv"},
    )
