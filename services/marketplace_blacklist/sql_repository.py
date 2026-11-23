"""Azure SQL implementation of BlacklistRepository (analytics.blacklist_listings).

Connection: pure-Python pytds + an Entra ID token for a service principal
(same approach as catalog-verifier), so no ODBC driver is needed. Settings come
from the environment / .env:
    AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET,
    AZURE_SQL_SERVER_HOST, AZURE_SQL_SERVER_DATABASE

Column mapping (live table -> API field):
    identifier_type_id           -> identifier_type_code
    blacklist_added_by_initials  -> created_by   (VARCHAR(4) NOT NULL: initials required)
    pl_blacklist_reason_codes.reason_description -> reason_name
    pl_identifier_types.identifier_type          -> identifier_type_name
The table has no ASIN column, so ASIN "review" matches are unavailable here.
changed_by / changed_by_app are filled by the table defaults (ORIGINAL_LOGIN(),
APP_NAME()); we connect with appname "blacklist-app". blacklist_listings is
system-versioned, so every update/delete is kept in blacklist_listings_history.
"""
from __future__ import annotations

import json
import os
import queue
import threading
from contextlib import contextmanager
from typing import Iterable

from .models import (
    Channel,
    CREATED_BY_MAX_SQL,
    IdentifierType,
    ListingCreate,
    ListingOut,
    ListingUpdate,
    Lookups,
    ReasonCode,
)
from .repository import (
    BlacklistRepository,
    DuplicateError,
    InvalidReferenceError,
    NotFoundError,
    RepositoryError,
)

ENV_KEYS = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
            "AZURE_SQL_SERVER_HOST", "AZURE_SQL_SERVER_DATABASE")
APP_NAME = "blacklist-app"

T = "analytics.blacklist_listings"
SELECT = f"""
SELECT b.blacklist_id, b.identifier, b.identifier_type_id, b.channel_id, b.blacklist_reason_code_id,
       b.blacklist_notes, b.blacklist_added_by_initials, b.created_at, b.updated_at,
       t.identifier_type, c.channel_name, r.reason_description
FROM {T} b
JOIN analytics.pl_identifier_types t ON t.identifier_type_id = b.identifier_type_id
JOIN analytics.pl_channels c ON c.channel_id = b.channel_id
JOIN analytics.pl_blacklist_reason_codes r ON r.blacklist_reason_code_id = b.blacklist_reason_code_id
"""
# OPENJSON returns the database default collation; pin it to the column's so
# comparisons stay case-insensitive and never raise a collation conflict.
JSON_VALUES = "SELECT CAST(value AS NVARCHAR(64)) COLLATE SQL_Latin1_General_CP1_CI_AS FROM OPENJSON(%s)"

# SQL Server error numbers
UNIQUE_VIOLATION = {2627, 2601}
FK_VIOLATION = {547}


def is_configured() -> bool:
    return all(os.getenv(k) for k in ENV_KEYS)


def _row_to_listing(r) -> ListingOut:
    return ListingOut(
        blacklist_id=r[0], identifier=r[1], identifier_type_code=r[2], channel_id=r[3],
        blacklist_reason_code_id=r[4], blacklist_notes=r[5] or "", created_by=r[6],
        created_at=r[7], updated_at=r[8],
        identifier_type_name=r[9], channel_name=r[10], reason_name=r[11],
    )


def _error_number(exc: Exception) -> int | None:
    for attr in ("msg_no", "number"):
        n = getattr(exc, attr, None)
        if isinstance(n, int):
            return n
    text = str(exc)
    if "UNIQUE KEY" in text or "duplicate key" in text:
        return 2627
    if "FOREIGN KEY" in text:
        return 547
    return None


def _like(q: str) -> str:
    return "%" + q.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]") + "%"


def _initials(value: str | None) -> str:
    v = (value or "").strip()
    if not v:
        raise InvalidReferenceError("Initials are required (Added by)")
    if len(v) > CREATED_BY_MAX_SQL:
        raise InvalidReferenceError(f"Initials must be at most {CREATED_BY_MAX_SQL} characters")
    return v.upper()


class SqlRepository(BlacklistRepository):
    def __init__(self, pool_size: int = 4):
        self._pool: queue.LifoQueue = queue.LifoQueue(maxsize=pool_size)
        self._cred = None
        self._cred_lock = threading.Lock()

    # -- connections -------------------------------------------------------

    def _connect(self):
        import urllib3.util.connection as uc

        uc.HAS_IPV6 = False  # azure-identity stalls on login.microsoftonline.com over IPv6
        import certifi
        import pytds
        from azure.identity import ClientSecretCredential

        with self._cred_lock:
            if self._cred is None:
                self._cred = ClientSecretCredential(
                    os.environ["AZURE_TENANT_ID"], os.environ["AZURE_CLIENT_ID"], os.environ["AZURE_CLIENT_SECRET"]
                )
        cred = self._cred
        return pytds.connect(
            server=os.environ["AZURE_SQL_SERVER_HOST"], port=1433,
            database=os.environ["AZURE_SQL_SERVER_DATABASE"],
            access_token_callable=lambda: cred.get_token("https://database.windows.net/.default").token,
            cafile=certifi.where(), validate_host=False,
            login_timeout=30, timeout=60, autocommit=False, appname=APP_NAME,
        )

    def _connect_or_raise(self):
        """_connect(), with any failure turned into a RepositoryError (shown as a 503) carrying the real
        reason. pytds wraps it in a bare TimeoutError after its retries, so unwrap the cause chain:
        e.g. "Client with IP address ... is not allowed to access the server" (Azure firewall)."""
        try:
            return self._connect()
        except Exception as exc:
            root = exc
            while root.__cause__ is not None:
                root = root.__cause__
            msg = (root.args[0] if root.args and isinstance(root.args[0], str) else str(root)) \
                or type(root).__name__
            raise RepositoryError(f"cannot connect to Azure SQL: {msg}") from exc

    @contextmanager
    def _tx(self):
        """A pooled connection wrapped in a transaction: commit on success, rollback on error."""
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            conn = self._connect_or_raise()
        ok = False
        try:
            cur = conn.cursor()
            yield cur
            conn.commit()
            ok = True
        except Exception:
            try:
                conn.rollback()
                ok = True  # connection is still usable
            except Exception:
                pass
            raise
        finally:
            if ok:
                try:
                    self._pool.put_nowait(conn)
                except queue.Full:
                    conn.close()
            else:
                try:
                    conn.close()
                except Exception:
                    pass

    def _raise_for(self, cur, exc: Exception, identifier: str, type_id: int, channel_id: int):
        n = _error_number(exc)
        if n in UNIQUE_VIOLATION:
            raise DuplicateError(self._find_one(cur, identifier, type_id, channel_id)) from exc
        if n in FK_VIOLATION:
            raise InvalidReferenceError("Unknown store, reason or identifier type") from exc
        raise RepositoryError(f"Database error: {exc}") from exc

    def _find_one(self, cur, identifier, type_id, channel_id) -> ListingOut:
        cur.execute(SELECT + " WHERE b.identifier = %s AND b.identifier_type_id = %s AND b.channel_id = %s",
                    (identifier, type_id, channel_id))
        return _row_to_listing(cur.fetchone())

    # -- interface ---------------------------------------------------------

    def lookups(self) -> Lookups:
        with self._tx() as cur:
            cur.execute("SELECT channel_id, channel_name FROM analytics.pl_channels ORDER BY channel_id")
            channels = [Channel(channel_id=a, channel_name=b) for a, b in cur.fetchall()]
            cur.execute("SELECT blacklist_reason_code_id, reason_description FROM analytics.pl_blacklist_reason_codes "
                        "ORDER BY reason_description")
            reasons = [ReasonCode(blacklist_reason_code_id=a, reason_name=b) for a, b in cur.fetchall()]
            cur.execute("SELECT identifier_type_id, identifier_type FROM analytics.pl_identifier_types "
                        "ORDER BY identifier_type_id")
            types = [IdentifierType(identifier_type_code=a, identifier_type_name=b) for a, b in cur.fetchall()]
        return Lookups(channels=channels, reason_codes=reasons, identifier_types=types)

    def search(self, q=None, channel_id=None, blacklist_reason_code_id=None, identifier_type_code=None,
               offset=0, limit=50):
        where, params = [], []
        if q and q.strip():
            like = _like(q.strip())
            where.append("(b.identifier LIKE %s OR b.blacklist_notes LIKE %s OR b.blacklist_added_by_initials LIKE %s)")
            params += [like, like, like]
        for col, val in (("b.channel_id", channel_id), ("b.blacklist_reason_code_id", blacklist_reason_code_id),
                         ("b.identifier_type_id", identifier_type_code)):
            if val is not None:
                where.append(f"{col} = %s")
                params.append(val)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._tx() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {T} b" + clause, tuple(params))
            total = cur.fetchone()[0]
            cur.execute(SELECT + clause + " ORDER BY b.updated_at DESC, b.blacklist_id DESC "
                        "OFFSET %s ROWS FETCH NEXT %s ROWS ONLY", tuple(params + [offset, limit]))
            return [_row_to_listing(r) for r in cur.fetchall()], total

    def get(self, blacklist_id):
        with self._tx() as cur:
            cur.execute(SELECT + " WHERE b.blacklist_id = %s", (blacklist_id,))
            row = cur.fetchone()
        if row is None:
            raise NotFoundError(f"Blacklist entry {blacklist_id} not found")
        return _row_to_listing(row)

    def create(self, data: ListingCreate) -> ListingOut:
        initials = _initials(data.created_by)
        with self._tx() as cur:
            try:
                cur.execute(
                    f"INSERT INTO {T} (identifier, identifier_type_id, channel_id, blacklist_reason_code_id, "
                    "blacklist_notes, blacklist_added_by_initials) OUTPUT INSERTED.blacklist_id "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (data.identifier, data.identifier_type_code, data.channel_id, data.blacklist_reason_code_id,
                     data.blacklist_notes or None, initials),
                )
                new_id = cur.fetchone()[0]
            except (DuplicateError, InvalidReferenceError):
                raise
            except Exception as exc:
                self._raise_for(cur, exc, data.identifier, data.identifier_type_code, data.channel_id)
            cur.execute(SELECT + " WHERE b.blacklist_id = %s", (new_id,))
            return _row_to_listing(cur.fetchone())

    def update(self, blacklist_id, data: ListingUpdate) -> ListingOut:
        sets: dict = {}
        for col, val in (("identifier", data.identifier), ("identifier_type_id", data.identifier_type_code),
                         ("channel_id", data.channel_id), ("blacklist_reason_code_id", data.blacklist_reason_code_id)):
            if val is not None:
                sets[col] = val
        if data.blacklist_notes is not None:
            sets["blacklist_notes"] = data.blacklist_notes or None  # "" clears (column is nullable)
        if data.created_by is not None:
            sets["blacklist_added_by_initials"] = _initials(data.created_by)
        with self._tx() as cur:
            cur.execute(SELECT + " WHERE b.blacklist_id = %s", (blacklist_id,))
            current = cur.fetchone()
            if current is None:
                raise NotFoundError(f"Blacklist entry {blacklist_id} not found")
            assignments = ", ".join(f"{k} = %s" for k in sets)
            try:
                cur.execute(
                    f"UPDATE {T} SET {assignments + ', ' if assignments else ''}updated_at = SYSDATETIMEOFFSET() "
                    "WHERE blacklist_id = %s",
                    tuple(sets.values()) + (blacklist_id,),
                )
            except Exception as exc:
                cur_row = _row_to_listing(current)
                self._raise_for(
                    cur, exc, data.identifier or cur_row.identifier,
                    data.identifier_type_code or cur_row.identifier_type_code, data.channel_id or cur_row.channel_id,
                )
            cur.execute(SELECT + " WHERE b.blacklist_id = %s", (blacklist_id,))
            return _row_to_listing(cur.fetchone())

    def delete(self, blacklist_id):
        with self._tx() as cur:
            cur.execute(f"DELETE FROM {T} WHERE blacklist_id = %s", (blacklist_id,))
            if cur.rowcount == 0:
                raise NotFoundError(f"Blacklist entry {blacklist_id} not found")

    def find_by_identifiers(self, identifiers: Iterable[str], channel_id=None, identifier_type_code=None):
        ids = list(identifiers)
        if not ids:
            return []
        sql = SELECT + f" WHERE b.identifier IN ({JSON_VALUES})"
        params: list = [json.dumps(ids)]
        if channel_id is not None:
            sql += " AND b.channel_id = %s"
            params.append(channel_id)
        if identifier_type_code is not None:
            sql += " AND b.identifier_type_id = %s"
            params.append(identifier_type_code)
        with self._tx() as cur:
            cur.execute(sql, tuple(params))
            return [_row_to_listing(r) for r in cur.fetchall()]

    def find_by_asins(self, asins, channel_id=None):
        return []  # no ASIN column in analytics.blacklist_listings

    def list_by_type(self, identifier_type_code, channel_id=None):
        sql = SELECT + " WHERE b.identifier_type_id = %s"
        params: list = [identifier_type_code]
        if channel_id is not None:
            sql += " AND b.channel_id = %s"
            params.append(channel_id)
        with self._tx() as cur:
            cur.execute(sql, tuple(params))
            return [_row_to_listing(r) for r in cur.fetchall()]
