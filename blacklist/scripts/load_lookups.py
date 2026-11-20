"""Load the three lookup tables in Azure SQL from CSV (or .xlsx) files.

    python scripts/load_lookups.py            # DRY RUN: validate and show the plan, write nothing
    python scripts/load_lookups.py --apply    # insert and COMMIT (one transaction)

Files (one column, one name per row, header = column name; ids are IDENTITY and assigned by SQL):
    data/lookups/pl_channels.csv                 channel_name
    data/lookups/pl_identifier_types.csv         identifier_type
    data/lookups/pl_blacklist_reason_codes.csv   reason_description
An .xlsx with the same base name is used instead when present (first sheet, first column).
A file with more than one column is refused, so a second column can't be silently dropped.

Safe to re-run: names already in the table are skipped (case-insensitive), nothing is
updated or deleted. Everything is one transaction, so it either all lands or none does.
The dry run inserts nothing, because SQL Server never reuses IDENTITY values consumed by
rolled-back inserts. On --apply, a table that is still empty has its identity reseeded so
ids start at 1 (earlier rolled-back test inserts had advanced it).
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from app.sql_repository import SqlRepository, is_configured  # noqa: E402

DIR = ROOT / "data" / "lookups"
# table, id column, name column, max characters (from sys.columns)
TABLES = [
    ("pl_channels", "channel_id", "channel_name", 100),
    ("pl_identifier_types", "identifier_type_id", "identifier_type", 100),
    ("pl_blacklist_reason_codes", "blacklist_reason_code_id", "reason_description", 200),
]


def read_names(table: str, column: str) -> tuple[list[str], Path]:
    xlsx, csv_path = DIR / f"{table}.xlsx", DIR / f"{table}.csv"
    if xlsx.exists():
        import openpyxl

        ws = openpyxl.load_workbook(xlsx, read_only=True, data_only=True).worksheets[0]
        cells = [list(r) for r in ws.iter_rows(values_only=True)]
        path = xlsx
    elif csv_path.exists():
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            cells = list(csv.reader(f))
        path = csv_path
    else:
        raise SystemExit(f"Missing {csv_path.relative_to(ROOT)} (or .xlsx)")
    # One column only: anything filled in beyond the first cell would otherwise be dropped silently.
    extra = next((i for i, r in enumerate(cells, 1) if any(v is not None and str(v).strip() for v in r[1:])), None)
    if extra:
        raise SystemExit(f"{path.name}: row {extra} has more than one column. "
                         f"Use a single '{column}' column, one name per row.")
    rows = [r[0] if r else None for r in cells]
    names = [" ".join(str(v).split()) for v in rows if v is not None and str(v).strip()]
    if names and names[0].casefold() == column.casefold():
        names = names[1:]  # header row
    return names, path


def main() -> int:
    apply = "--apply" in sys.argv
    if not is_configured():
        print("Azure SQL settings missing from .env")
        return 2

    plan, problems = {}, []
    for table, _, column, max_len in TABLES:
        names, path = read_names(table, column)
        seen, clean = set(), []
        for n in names:
            if len(n) > max_len:
                problems.append(f"{table}: '{n[:40]}…' is longer than {max_len} characters")
            elif n.casefold() in seen:
                problems.append(f"{table}: '{n}' is listed twice")
            else:
                seen.add(n.casefold())
                clean.append(n)
        if not clean:
            problems.append(f"{table}: no values in {path.name}")
        plan[table] = clean
        print(f"{path.relative_to(ROOT)}: {len(clean)} value(s)")

    if problems:
        print("\nFix these first:\n  " + "\n  ".join(problems))
        return 1

    repo = SqlRepository()
    conn = repo._connect()
    inserted = {}
    try:
        for table, id_col, column, _ in TABLES:
            cur = conn.cursor()
            cur.execute(f"SELECT {column} FROM analytics.{table}")
            existing = {r[0].casefold() for r in cur.fetchall()}
            new = [n for n in plan[table] if n.casefold() not in existing]
            print(f"\n{table}: {len(new)} to insert, {len(plan[table]) - len(new)} already present")
            if not apply:
                for name in new:
                    print(f"     +  {name}")
                continue
            if not existing and new:
                cur = conn.cursor()
                cur.execute("SELECT CAST(last_value AS BIGINT) FROM sys.identity_columns WHERE object_id = OBJECT_ID(%s)",
                            (f"analytics.{table}",))
                if cur.fetchone()[0] is not None:  # identity already used by rolled-back test inserts
                    cur = conn.cursor()
                    cur.execute(f"DBCC CHECKIDENT ('analytics.{table}', RESEED, 0) WITH NO_INFOMSGS")
            inserted[table] = []
            for name in new:
                cur = conn.cursor()
                cur.execute(f"INSERT INTO analytics.{table} ({column}) OUTPUT INSERTED.{id_col} VALUES (%s)", (name,))
                inserted[table].append((cur.fetchone()[0], name))
                print(f"  {inserted[table][-1][0]:>4}  {name}")
        if apply:
            conn.commit()
            print("\nCOMMITTED.")
        else:
            conn.rollback()
            print("\nDry run - nothing written. Re-run with --apply to insert and commit.")
    except Exception:
        conn.rollback()
        print("\nError - rolled back, nothing saved.")
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
