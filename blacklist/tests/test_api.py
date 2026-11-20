import pytest
from fastapi.testclient import TestClient

from app import matching
from app.main import LEGACY_REASON_NAME, app, get_repository
from app.models import Channel, IdentifierType, ListingCreate, ReasonCode
from app.repository import InMemoryRepository

# The live lookup names (data/lookups/*.csv). "Brand prefix" is not in the live table; it is
# added here so the brand-rule matching can be tested (see BRAND_PREFIX_TYPE_NAME).
CHANNELS = [
    Channel(channel_id=1, channel_name="Amazon - Preferred Pharmacy Plus"),
    Channel(channel_id=2, channel_name="Amazon - Preferred Pharmacy Plus CA"),
    Channel(channel_id=3, channel_name="Amazon - Harper Wrens"),
    Channel(channel_id=4, channel_name="Walmart"),
]
REASONS = [
    ReasonCode(blacklist_reason_code_id=1, reason_name="Do not send"),
    ReasonCode(blacklist_reason_code_id=2, reason_name="Duplicate"),
    ReasonCode(blacklist_reason_code_id=9, reason_name="Pricing error"),
]
TYPES = [
    IdentifierType(identifier_type_code=1, identifier_type_name="Marketplace Fulfilled SKU"),
    IdentifierType(identifier_type_code=2, identifier_type_name="Listing ID"),
    IdentifierType(identifier_type_code=3, identifier_type_name="Brand prefix"),
    IdentifierType(identifier_type_code=4, identifier_type_name="Merchant Fulfilled SKU"),
]


def row(identifier, type_code=1, channel=1, reason=1, notes="", asin=None):
    return ListingCreate(
        identifier=identifier, identifier_type_code=type_code, channel_id=channel,
        blacklist_reason_code_id=reason, blacklist_notes=notes, asin=asin,
    )


FIXTURE = [
    row("MAJ904699760-FBA", notes="do not send", asin="B00U1H9M1U"),
    row("MAJ904699760-FBA", channel=2),
    row("ANH011309_QY2", reason=2, notes="duplicated ASIN", asin="B00K1JFAVE"),
    row("PDI", type_code=3, notes="New agreement prohibits selling"),
    row("SKIL", type_code=3, channel=3, notes="not selling on Amazon"),
    row("WMT-ITEM-77", type_code=2, channel=4, reason=9, notes="Walmart Item ID"),
    row("CNLF28551", type_code=4, channel=4, reason=9, notes="merchant fulfilled"),
]


def client_for(channels=CHANNELS, reasons=REASONS, types=TYPES, rows=FIXTURE):
    repo = InMemoryRepository(channels, reasons, types, rows)
    app.dependency_overrides[get_repository] = lambda: repo
    return TestClient(app)


@pytest.fixture
def client():
    yield client_for()
    app.dependency_overrides.clear()


@pytest.fixture
def make_client():
    """A client over custom lookups / rows, for the rules that depend on them."""
    yield client_for
    app.dependency_overrides.clear()


NEW = {
    "identifier": "NEW-SKU-1",
    "identifier_type_code": 1,
    "channel_id": 1,
    "blacklist_reason_code_id": 9,
    "blacklist_notes": "test",
    "created_by": "rn",
}


def check(client, *ids, **kw):
    return client.post("/api/blacklist/check", json={"identifiers": list(ids), **kw}).json()


def test_lookups(client):
    body = client.get("/api/lookups").json()
    assert [c["channel_name"] for c in body["channels"]][0] == "Amazon - Preferred Pharmacy Plus"
    assert len(body["identifier_types"]) == 4


def test_create_get_update_delete(client):
    created = client.post("/api/blacklist", json={**NEW, "asin": " b0abcdefgh ", "created_by": "RN"})
    assert created.status_code == 201
    item = created.json()
    assert item["channel_name"] == "Amazon - Preferred Pharmacy Plus"
    assert item["asin"] == "B0ABCDEFGH" and item["created_by"] == "RN"

    got = client.get(f"/api/blacklist/{item['blacklist_id']}").json()
    assert got["identifier"] == "NEW-SKU-1"

    patched = client.patch(f"/api/blacklist/{item['blacklist_id']}", json={"blacklist_notes": "updated", "asin": ""}).json()
    assert patched["blacklist_notes"] == "updated"
    assert patched["asin"] is None and patched["created_by"] == "RN"

    assert client.delete(f"/api/blacklist/{item['blacklist_id']}").status_code == 204
    assert client.get(f"/api/blacklist/{item['blacklist_id']}").status_code == 404


def test_duplicate_is_case_insensitive(client):
    assert client.post("/api/blacklist", json=NEW).status_code == 201
    assert client.post("/api/blacklist", json={**NEW, "identifier": "  new-sku-1 "}).status_code == 409
    assert client.post("/api/blacklist", json={**NEW, "channel_id": 2}).status_code == 201


def test_update_into_duplicate_rejected(client):
    a = client.post("/api/blacklist", json=NEW).json()
    b = client.post("/api/blacklist", json={**NEW, "identifier": "OTHER"}).json()
    assert client.patch(f"/api/blacklist/{b['blacklist_id']}", json={"identifier": a["identifier"]}).status_code == 409


@pytest.mark.parametrize("patch", [{"channel_id": 999}, {"blacklist_reason_code_id": 999}, {"identifier_type_code": 999}])
def test_invalid_reference(client, patch):
    assert client.post("/api/blacklist", json={**NEW, **patch}).status_code == 422


@pytest.mark.parametrize("patch", [{"identifier": ""}, {"identifier": "   "}, {"identifier": "X" * 65},
                                   {"asin": "B00-SHORT"}, {"created_by": ""}, {"created_by": "TOOLONG"}])
def test_invalid_input(client, patch):
    assert client.post("/api/blacklist", json={**NEW, **patch}).status_code == 422


def test_search_filters_and_paging(client):
    assert client.get("/api/blacklist", params={"q": "maj9046"}).json()["total"] == 2
    r = client.get("/api/blacklist", params={"q": "maj9046", "channel_id": 2}).json()
    assert r["total"] == 1 and r["items"][0]["channel_name"] == "Amazon - Preferred Pharmacy Plus CA"
    assert client.get("/api/blacklist", params={"q": "B00K1JFAVE"}).json()["total"] == 1  # ASIN searchable
    r = client.get("/api/blacklist", params={"page_size": 2, "page": 2}).json()
    assert len(r["items"]) == 2 and r["page"] == 2


def test_exact_match_dedupes_input(client):
    r = check(client, "maj904699760-fba", "NOT-THERE", " ", "MAJ904699760-FBA")
    assert r["total_checked"] == 2 and r["total_blacklisted"] == 1
    hit = r["results"][0]
    assert hit["status"] == "blacklisted" and len(hit["matches"]) == 2
    assert {m["matched_by"] for m in hit["matches"]} == {"identifier"}
    assert r["results"][1]["status"] == "clear"


@pytest.mark.parametrize("sku", ["PDI12345-FBA", "pdi-anything", 'amzn.gr.PDI555-FBA-xYz-LN', 'amzn.gr."PDI9"-abc-LN'])
def test_brand_prefix_match(client, sku):
    res = check(client, sku)["results"][0]
    assert res["status"] == "blacklisted"
    assert res["matches"][0]["matched_by"] == "brand" and res["matches"][0]["identifier"] == "PDI"


def test_brand_is_prefix_not_substring_and_respects_channel(client):
    assert check(client, "XPDI123")["results"][0]["status"] == "clear"  # R4's SEARCH() would flag this
    assert check(client, "SKIL4410")["results"][0]["status"] == "blacklisted"  # 4-letter brand (R5's LEFT(,3) misses it)
    assert check(client, "SKIL4410", channel_id=1)["results"][0]["status"] == "clear"  # SKIL is Harper Wrens only


def test_brand_rules_apply_to_any_type_filter_but_the_brand_type(client):
    for type_code in (1, 2, 4):  # every non-brand type, whatever it is called
        res = check(client, "PDI1", identifier_type_code=type_code)["results"][0]
        assert res["status"] == "blacklisted" and res["matches"][0]["matched_by"] == "brand", type_code
    # Filtered to the brand type itself, the input is a brand code: exact match only.
    assert check(client, "PDI1", identifier_type_code=3)["results"][0]["status"] == "clear"
    assert check(client, "PDI", identifier_type_code=3)["results"][0]["matches"][0]["matched_by"] == "identifier"


@pytest.mark.parametrize("ident, type_code", [("MAJ904699760-FBA", 1), ("wmt-item-77", 2), ("CNLF28551", 4)])
def test_exact_match_works_for_every_type(client, ident, type_code):
    for kw in ({}, {"identifier_type_code": type_code}):
        res = check(client, ident, **kw)["results"][0]
        assert res["status"] == "blacklisted" and res["matches"][0]["matched_by"] == "identifier", kw
    other = next(c for c in (1, 2, 4) if c != type_code)
    assert check(client, ident, identifier_type_code=other)["results"][0]["status"] == "clear"


def test_brand_rules_skipped_without_brand_type(make_client):
    # Same rows, but no type is named BRAND_PREFIX_TYPE_NAME (like the live lookups).
    types = [t if t.identifier_type_code != 3 else IdentifierType(identifier_type_code=3, identifier_type_name="Old brand codes")
             for t in TYPES]
    c = make_client(types=types)
    r = c.post("/api/blacklist/check", json={"identifiers": ["PDI12345-FBA", "SKIL4410", "pdi"]})
    assert r.status_code == 200
    by_id = {x["identifier"]: x for x in r.json()["results"]}
    assert by_id["PDI12345-FBA"]["status"] == "clear" and by_id["SKIL4410"]["status"] == "clear"
    assert by_id["pdi"]["matches"][0]["matched_by"] == "identifier"  # exact matching still works


def test_brand_type_found_by_the_constant(make_client, monkeypatch):
    monkeypatch.setattr(matching, "BRAND_PREFIX_TYPE_NAME", "Brand rule")
    types = [t if t.identifier_type_code != 3 else IdentifierType(identifier_type_code=3, identifier_type_name="  BRAND RULE ")
             for t in TYPES]
    res = check(make_client(types=types), "PDI12345-FBA")["results"][0]
    assert res["status"] == "blacklisted" and res["matches"][0]["matched_by"] == "brand"
    # The default name no longer matches anything, so the original types now skip brand rules.
    assert check(make_client(), "PDI12345-FBA")["results"][0]["status"] == "clear"


def test_brand_code_itself_is_exact_match(client):
    res = check(client, "pdi")["results"][0]
    assert res["status"] == "blacklisted" and res["matches"][0]["matched_by"] == "identifier"


def test_asin_match_is_review_not_block(client):
    r = check(client, "b00k1jfave")
    res = r["results"][0]
    assert res["status"] == "review" and res["blacklisted"] is False
    assert res["matches"][0]["identifier"] == "ANH011309_QY2" and res["matches"][0]["matched_by"] == "asin"
    assert r["total_review"] == 1 and r["total_blacklisted"] == 0


def test_asin_review_needs_asin_data(client, make_client):
    # A type filter turns the ASIN review off.
    assert check(client, "b00k1jfave", identifier_type_code=2)["results"][0]["status"] == "clear"
    # Without ASIN data (the live table has no ASIN column) there is nothing to review.
    no_asins = [r.model_copy(update={"asin": None}) for r in FIXTURE]
    assert check(make_client(rows=no_asins), "b00k1jfave")["results"][0]["status"] == "clear"


# ---- the legacy "Do not send" reason ----------------------------------------

def test_legacy_reason_rejected_on_create(client):
    r = client.post("/api/blacklist", json={**NEW, "blacklist_reason_code_id": 1})
    assert r.status_code == 422 and LEGACY_REASON_NAME in r.json()["detail"]
    assert client.get("/api/blacklist", params={"q": NEW["identifier"]}).json()["total"] == 0


def test_legacy_reason_rejected_on_bulk_add_before_anything_is_created(client):
    r = client.post("/api/blacklist/bulk", json={
        "identifiers": ["BULK-LEGACY-1", "BULK-LEGACY-2"], "identifier_type_code": 1, "channel_ids": [1, 4],
        "blacklist_reason_code_id": 1, "blacklist_notes": "", "created_by": "RN",
    })
    assert r.status_code == 422 and LEGACY_REASON_NAME in r.json()["detail"]
    assert client.get("/api/blacklist", params={"q": "BULK-LEGACY"}).json()["total"] == 0


def test_legacy_reason_rejected_when_changing_to_it(client):
    item = client.post("/api/blacklist", json=NEW).json()
    r = client.patch(f"/api/blacklist/{item['blacklist_id']}", json={"blacklist_reason_code_id": 1})
    assert r.status_code == 422 and LEGACY_REASON_NAME in r.json()["detail"]
    assert client.get(f"/api/blacklist/{item['blacklist_id']}").json()["blacklist_reason_code_id"] == 9


def test_entry_with_legacy_reason_may_keep_it(client):
    legacy = client.get("/api/blacklist", params={"q": "MAJ904699760-FBA", "channel_id": 1}).json()["items"][0]
    assert legacy["blacklist_reason_code_id"] == 1  # imported with "Do not send"
    url = f"/api/blacklist/{legacy['blacklist_id']}"
    r = client.patch(url, json={"blacklist_notes": "still do not send", "blacklist_reason_code_id": 1})  # same reason sent back
    assert r.status_code == 200 and r.json()["blacklist_notes"] == "still do not send"
    assert client.patch(url, json={"blacklist_reason_code_id": 9}).status_code == 200  # moving off it is fine
    assert client.patch(url, json={"blacklist_reason_code_id": 1}).status_code == 422  # ...but not back
    assert client.patch("/api/blacklist/99999", json={"blacklist_reason_code_id": 1}).status_code == 404


def test_legacy_reason_matched_ignoring_case_and_spaces(make_client):
    reasons = [ReasonCode(blacklist_reason_code_id=1, reason_name="  DO NOT SEND "), *REASONS[1:]]
    c = make_client(reasons=reasons)
    assert c.post("/api/blacklist", json={**NEW, "blacklist_reason_code_id": 1}).status_code == 422
    assert c.post("/api/blacklist", json=NEW).status_code == 201


def test_bulk_add(client):
    r = client.post("/api/blacklist/bulk", json={
        "identifiers": ["BULK-1", "bulk-1", "MAJ904699760-FBA", "X" * 70],
        "identifier_type_code": 1,
        "channel_ids": [1, 3],
        "blacklist_reason_code_id": 9,
        "blacklist_notes": "bulk",
        "created_by": "RN",
    }).json()
    # BULK-1 on both channels, MAJ… on Harper Wrens; MAJ… on PPP already exists.
    assert len(r["created"]) == 3 and all(c["created_by"] == "RN" for c in r["created"])
    assert len(r["skipped_existing"]) == 1
    assert len(r["errors"]) == 1


def test_export_csv(client):
    r = client.get("/api/blacklist/export.csv", params={"channel_id": 1})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0].startswith("blacklist_id,identifier,identifier_type,asin")


def test_seed_file_loads():
    from app.seed_data import SEED_FILE, load_repository

    if not SEED_FILE.exists():
        pytest.skip("seed_data.json not generated")
    repo = load_repository()
    items, total = repo.search(limit=1)
    assert total > 0


def test_missing_import_dates_stay_empty():
    base = row("UNDATED-1").model_dump()
    repo = InMemoryRepository(CHANNELS, REASONS, TYPES, [{**base, "created_at": None}, row("NEW-1")])
    items, _ = repo.search()
    undated = next(i for i in items if i.identifier == "UNDATED-1")
    assert undated.created_at is None and undated.updated_at is None
    assert next(i for i in items if i.identifier == "NEW-1").created_at is not None  # new rows are stamped
    assert items[-1].identifier == "UNDATED-1"  # undated rows sort last

    app.dependency_overrides[get_repository] = lambda: repo
    try:
        csv_text = TestClient(app).get("/api/blacklist/export.csv").text
    finally:
        app.dependency_overrides.clear()
    assert "UNDATED-1" in csv_text and "Not available" in csv_text


def test_works_under_a_path_prefix(client):
    from fastapi import FastAPI

    site = FastAPI()
    site.mount("/blacklist", app)
    c = TestClient(site)
    r = c.get("/blacklist", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].endswith("/blacklist/")
    page = c.get("/blacklist/").text
    assert 'href="static/styles.css"' in page and 'src="static/app.js"' in page
    assert c.get("/blacklist/static/app.js").status_code == 200
    assert c.get("/blacklist/api/lookups").status_code == 200


def test_frontend_served(client):
    assert "Marketplace Blacklist" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_bulk_rows_each_row_has_its_own_store_and_reason(client):
    r = client.post("/api/blacklist/bulk-rows", json={"created_by": "db", "rows": [
        {"identifier": " GRID-1 ", "identifier_type_code": 1, "channel_id": 1, "blacklist_reason_code_id": 2, "blacklist_notes": "a"},
        {"identifier": "GRID-1", "identifier_type_code": 1, "channel_id": 3, "blacklist_reason_code_id": 9},
        {"identifier": "grid-1", "identifier_type_code": 1, "channel_id": 1, "blacklist_reason_code_id": 9},  # same as row 0
        {"identifier": "MAJ904699760-FBA", "identifier_type_code": 1, "channel_id": 1, "blacklist_reason_code_id": 2},  # in fixture
        {"identifier": "X" * 70, "identifier_type_code": 1, "channel_id": 1, "blacklist_reason_code_id": 2},
        {"identifier": "GRID-2", "identifier_type_code": 1, "channel_id": 99, "blacklist_reason_code_id": 2},
        {"identifier": "GRID-3", "identifier_type_code": 1, "channel_id": 1, "blacklist_reason_code_id": 1},  # legacy
    ]})
    assert r.status_code == 200
    res = r.json()["results"]
    assert [x["row"] for x in res] == list(range(7))
    assert [x["status"] for x in res] == ["created", "created", "existing", "existing", "error", "error", "error"]
    a, b = res[0]["listing"], res[1]["listing"]
    assert (a["identifier"], a["channel_id"], a["reason_name"], a["blacklist_notes"], a["created_by"]) == ("GRID-1", 1, "Duplicate", "a", "DB")
    assert (b["channel_id"], b["reason_name"]) == (3, "Pricing error")
    assert "64" in res[4]["error"]
    assert LEGACY_REASON_NAME in res[6]["error"]
    assert client.get("/api/blacklist", params={"q": "GRID-3"}).json()["total"] == 0


def test_bulk_rows_require_initials(client):
    r = client.post("/api/blacklist/bulk-rows", json={"rows": [
        {"identifier": "GRID-9", "identifier_type_code": 1, "channel_id": 1, "blacklist_reason_code_id": 2}]})
    assert r.status_code == 422
