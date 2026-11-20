"""End-to-end check of SqlRepository against the live Azure SQL database - leaves NO data behind.

    python scripts/sql_smoke_test.py

Every statement runs on a single connection inside one transaction that is always
rolled back, so it exercises real INSERT/UPDATE/DELETE permissions, the UNIQUE and
FOREIGN KEY constraints, OPENJSON matching and the temporal table without writing
anything permanent. It then re-counts the tables on a fresh connection to prove it.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from app.matching import BRAND_PREFIX_TYPE_NAME, brand_type_id, check  # noqa: E402
from app.models import ListingCreate, ListingUpdate  # noqa: E402
from app.repository import DuplicateError, InvalidReferenceError, NotFoundError  # noqa: E402
from app.sql_repository import SqlRepository, is_configured  # noqa: E402

TABLES = ["blacklist_listings", "blacklist_listings_history", "pl_channels", "pl_blacklist_reason_codes", "pl_identifier_types"]
TAG = "ZZ-SMOKETEST"


def counts(cur):
    out = {}
    for t in TABLES:
        cur.execute(f"SELECT COUNT(*) FROM analytics.{t}")
        out[t] = cur.fetchone()[0]
    return out


def main() -> int:
    if not is_configured():
        print("Azure SQL settings missing from .env")
        return 2
    repo = SqlRepository()
    conn = repo._connect()
    cur0 = conn.cursor()
    before = counts(cur0)
    print("connected. row counts before:", before)

    @contextmanager
    def one_tx():  # every repository call shares this uncommitted transaction
        yield conn.cursor()

    repo._tx = one_tx
    passed = failed = 0

    def ok(label, cond, detail=""):
        nonlocal passed, failed
        passed += bool(cond)
        failed += not cond
        print(f"  {'PASS' if cond else 'FAIL'}  {label}{'  ' + str(detail) if detail and not cond else ''}")

    try:
        cur = conn.cursor()
        ids = {}
        for table, col, name in [
            ("pl_channels", "channel_name", f"{TAG} Amazon - Store A"), ("pl_channels", "channel_name", f"{TAG} Walmart"),
            ("pl_identifier_types", "identifier_type", f"{TAG} Marketplace Fulfilled SKU"),
            ("pl_blacklist_reason_codes", "reason_description", f"{TAG} Pricing error"),
        ]:
            cur.execute(f"INSERT INTO analytics.{table} ({col}) OUTPUT INSERTED.* VALUES (%s)", (name,))
            ids[name] = cur.fetchone()[0]
        store_a, store_b = ids[f"{TAG} Amazon - Store A"], ids[f"{TAG} Walmart"]
        sku_t = ids[f"{TAG} Marketplace Fulfilled SKU"]
        reason = ids[f"{TAG} Pricing error"]
        ok("insert into all 3 lookup tables", all(ids.values()))

        lk = repo.lookups()
        ok("lookups() reads them back", {store_a, store_b} <= {c.channel_id for c in lk.channels})

        def new(identifier, type_id=sku_t, channel=store_a, notes="smoke test", by="zz"):
            return ListingCreate(identifier=identifier, identifier_type_code=type_id, channel_id=channel,
                                 blacklist_reason_code_id=reason, blacklist_notes=notes, created_by=by)

        a = repo.create(new("ZZSMK-0001-FBA"))
        ok("create listing", a.blacklist_id > 0 and a.channel_name.endswith("Store A") and a.created_by == "ZZ",
           a)
        ok("created_at/updated_at stamped by DB defaults", a.created_at is not None and a.updated_at is not None)
        cur = conn.cursor()  # pytds: one active cursor per connection
        cur.execute("SELECT changed_by, changed_by_app FROM analytics.blacklist_listings WHERE blacklist_id=%s",
                    (a.blacklist_id,))
        changed_by, changed_app = cur.fetchone()
        ok("changed_by / changed_by_app filled", bool(changed_by) and changed_app == "blacklist-app", changed_app)

        try:
            repo.create(new("zzsmk-0001-fba"))
            ok("duplicate (case-insensitive) rejected", False)
        except DuplicateError as e:
            ok("duplicate (case-insensitive) rejected -> DuplicateError", e.existing.blacklist_id == a.blacklist_id)

        try:
            repo.create(new("ZZSMK-0002", channel=-1))
            ok("bad store rejected", False)
        except InvalidReferenceError:
            ok("bad store rejected -> InvalidReferenceError (FK)", True)

        try:
            repo.create(new("ZZSMK-0003", by=""))
            ok("missing initials rejected", False)
        except InvalidReferenceError:
            ok("missing initials rejected", True)

        b = repo.create(new("ZZSMK-0001-FBA", channel=store_b))
        ok("same SKU on another store", b.blacklist_id)

        # Brand rules need a type named exactly BRAND_PREFIX_TYPE_NAME. The live lookups have none,
        # so check() must skip them without an error; then add one (rolled back) and test the rule.
        brand_t = brand_type_id(repo)
        if brand_t is None:
            res = check(repo, ["ZZSMK9999-FBA"])[0]
            ok(f"check(): no '{BRAND_PREFIX_TYPE_NAME}' type -> brand rules skipped, no error", res.status == "clear", res)
            cur = conn.cursor()
            cur.execute("INSERT INTO analytics.pl_identifier_types (identifier_type) OUTPUT INSERTED.* VALUES (%s)",
                        (BRAND_PREFIX_TYPE_NAME,))
            brand_t = cur.fetchone()[0]
        else:
            print(f"  note: a '{BRAND_PREFIX_TYPE_NAME}' type already exists (id {brand_t}); skipping the no-type check")
        brand = repo.create(new("ZZSMK", type_id=brand_t, notes="brand rule"))
        ok("a brand rule", brand.blacklist_id)

        got = repo.get(a.blacklist_id)
        ok("get()", got.identifier == "ZZSMK-0001-FBA")

        items, total = repo.search(q="zzsmk-0001")
        ok("search() substring, case-insensitive", total == 2, total)
        items, total = repo.search(q="ZZSMK", channel_id=store_b)
        ok("search() with store filter", total == 1 and items[0].channel_id == store_b, total)
        items, total = repo.search(q="ZZ%SMK")  # unescaped, % would match the ZZSMK rows
        ok("search() escapes LIKE wildcards", total == 0, total)

        upd = repo.update(a.blacklist_id, ListingUpdate(blacklist_notes="updated by smoke test", created_by="yy"))
        ok("update() notes + initials", upd.blacklist_notes == "updated by smoke test" and upd.created_by == "YY")
        ok("update() bumps updated_at", upd.updated_at >= a.updated_at)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM analytics.blacklist_listings_history WHERE blacklist_id=%s", (a.blacklist_id,))
        ok("temporal history row written on update", cur.fetchone()[0] >= 1)

        try:
            repo.update(b.blacklist_id, ListingUpdate(channel_id=store_a))
            ok("update into duplicate rejected", False)
        except DuplicateError:
            ok("update into duplicate rejected -> DuplicateError", True)

        found = repo.find_by_identifiers(["zzsmk-0001-FBA", "nope"])
        ok("find_by_identifiers() via OPENJSON, case-insensitive", len(found) == 2, len(found))

        res = {r.identifier: r for r in check(repo, ["ZZSMK-0001-FBA", "ZZSMK9999-FBA", "NOT-THERE-123"])}
        ok("check(): exact match", res["ZZSMK-0001-FBA"].status == "blacklisted")
        ok("check(): brand prefix match", res["ZZSMK9999-FBA"].status == "blacklisted"
           and res["ZZSMK9999-FBA"].matches[0].matched_by == "brand")
        ok("check(): clear", res["NOT-THERE-123"].status == "clear")

        repo.delete(b.blacklist_id)
        try:
            repo.get(b.blacklist_id)
            ok("delete()", False)
        except NotFoundError:
            ok("delete()", True)
    except Exception as exc:  # report instead of crashing so the rollback always runs
        failed += 1
        print(f"  FAIL  unexpected {type(exc).__name__}: {exc}")
    finally:
        conn.rollback()
        conn.close()

    check_conn = SqlRepository()._connect()
    after = counts(check_conn.cursor())
    check_conn.close()
    ok("rolled back: row counts unchanged", after == before, after)
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
