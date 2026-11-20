"""Convert the Excel blacklist (RestockR5.xlsx › BlacklistData) into app/seed_data.json.

    python scripts/import_excel.py [path/to/RestockR5.xlsx]

The sheet holds two side-by-side tables:
  A:C  Tag3_Brands_MainSku | Company | Notes                    -> brand-prefix rules
  E:J  Tag3_Items | ASIN | Company | Notes | DateAdded | AddedBy -> SKU rules
  M    list of companies (stores)                               -> channels

Cleanup applied (each step is counted in the summary):
  * trims whitespace, newlines and non-breaking spaces from identifiers and ASINs
  * merges rows that repeat the same identifier + company (the SQL UNIQUE
    constraint would reject them): distinct notes are joined with " | ",
    the earliest DateAdded and the first ASIN / AddedBy are kept
  * derives blacklist_reason_code_id from the free-text note (see REASON_RULES)
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_XLSX = ROOT.parent / "files" / "RestockR5.xlsx"
OUT = ROOT / "app" / "seed_data.json"

# Stores, as used in the workbooks' store_name column. WFS appears in the restock
# data but has no blacklist rows yet.
CHANNELS = ["Preferred Pharmacy Plus", "Preferred Pharmacy Plus CA", "Harper Wrens", "Turba", "WFS"]

IDENTIFIER_TYPES = [
    (1, "Seller SKU"),
    (2, "ASIN"),
    (3, "Brand prefix"),
]
SKU, ASIN, BRAND = 1, 2, 3

# First matching rule wins. Order matters: specific causes before generic "do not send".
REASONS = [
    (1, "Do not send", None),
    (2, "Duplicate listing", r"duplicat|\bdupe\b|shadow"),
    (3, "Recall / safety", r"recall|safety|policy violation"),
    (4, "Restricted / gated", r"restrict|gated|doesn'?t allow|not allowed"),
    (5, "Brand request / agreement", r"brand'?s request|agreement|contract|prohibit|legal|is blacklisted"),
    (6, "Transparency", r"transparency"),
    (7, "IP / counterfeit complaint", r"\bip\b|counterfeit|infring|authentic"),
    (8, "Pricing error", r"pric|\bhpe\b"),
    (9, "Unprofitable", r"losing money|\broi\b|unprofitable|margin"),
    (10, "Sold on another channel", r"\bww\b|walmart|\bppp\b|selling on|moving to|moved to|wholesale|only turba|don'?t sell|not selling there"),
    (11, "Fulfillment method", r"\bmfn\b|\bfbm\b|listed as fba"),
    (12, "Listing issue (wrong item / qty)", r"listing|listed|wrong|incorrect|qty|uom|kitted|ndc|converted|asin|colou?r"),
    (13, "Internal request", r"request|\bper\b"),
    (14, "Other", r"."),
]
REASON_RULES = [(rid, re.compile(rx, re.I)) for rid, _, rx in REASONS if rx]
DO_NOT_SEND = re.compile(r"^\s*do\s*no[t]?\s*send\b[\s\-–.\d/]*$", re.I)


def classify(note: str) -> int:
    if not note or DO_NOT_SEND.match(note) or note.strip().lower() == "blacklisted":
        return 1
    for rid, rx in REASON_RULES:
        if rx.search(note):
            return rid
    return REASONS[-1][0]


def clean(v) -> str:
    if v is None:
        return ""
    return re.sub(r"[\s ]+", " ", str(v)).strip()


def excel_date(v) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        d = v
    elif isinstance(v, (int, float)):
        d = datetime(1899, 12, 30) + timedelta(days=float(v))
    else:
        try:
            d = datetime.fromisoformat(str(v).strip())
        except ValueError:
            return None
    return d.replace(tzinfo=timezone.utc)


def main(xlsx: Path) -> None:
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    ws = wb["BlacklistData"]
    rows = list(ws.iter_rows(min_row=2, max_col=13, values_only=True))
    wb.close()

    channel_ids = {name: i for i, name in enumerate(CHANNELS, start=1)}
    stats = Counter()
    merged: dict[tuple[str, int, int], dict] = {}
    now = datetime.now(timezone.utc)

    def add(identifier, type_code, company, note, asin=None, date=None, added_by=None):
        identifier, company, note = clean(identifier), clean(company), clean(note)
        if not identifier:
            return
        # blacklist_added_by_initials is VARCHAR(4): keep the leading initials and move
        # anything longer (e.g. "IH (on LF request)") into the notes so nothing is lost.
        added_by = clean(added_by)
        m = re.match(r"[A-Za-z]{1,4}(?![A-Za-z])", added_by)
        initials = m.group(0).upper() if m else None
        if added_by and added_by.upper() != initials:
            note = f"{note} (added by {added_by})".strip()
            stats["AddedBy moved to notes"] += 1
        asin = clean(asin).replace(" ", "").upper()
        if asin and not re.fullmatch(r"[A-Z0-9]{10}", asin):
            stats[f"dropped invalid ASIN '{asin}'"] += 1
            asin = ""
        if company not in channel_ids:
            stats[f"skipped: unknown company '{company}'"] += 1
            return
        key = (identifier.casefold(), type_code, channel_ids[company])
        row = merged.get(key)
        if row is None:
            merged[key] = {
                "identifier": identifier,
                "identifier_type_code": type_code,
                "channel_id": channel_ids[company],
                "blacklist_reason_code_id": classify(note),
                "blacklist_notes": note,
                "asin": clean(asin) or None,
                "created_by": initials,
                "created_at": date,  # None when the sheet has no DateAdded
            }
            stats["dated" if date else "undated"] += 1
            return
        stats["merged duplicates"] += 1
        if note and note.casefold() not in row["blacklist_notes"].casefold():
            row["blacklist_notes"] = f"{row['blacklist_notes']} | {note}" if row["blacklist_notes"] else note
        row["asin"] = row["asin"] or clean(asin) or None
        row["created_by"] = row["created_by"] or initials
        if date and (row["created_at"] is None or date < row["created_at"]):
            row["created_at"] = date

    for r in rows:
        brand, b_company, b_note = r[0], r[1], r[2]
        sku, asin, s_company, s_note, date_added, added_by = r[4:10]
        if clean(brand):
            stats["brand rows"] += 1
            add(brand, BRAND, b_company, b_note)
        if clean(sku):
            stats["sku rows"] += 1
            if sku != clean(sku):
                stats["identifiers trimmed"] += 1
            add(sku, SKU, s_company, s_note, asin, excel_date(date_added), added_by)

    listings = []
    for row in merged.values():
        row["blacklist_notes"] = row["blacklist_notes"][:1000]
        row["created_at"] = row["updated_at"] = row["created_at"].isoformat() if row["created_at"] else None
        listings.append(row)

    out = {
        "source": xlsx.name,
        "generated_at": now.isoformat(),
        "channels": [{"channel_id": i, "channel_name": n} for n, i in channel_ids.items()],
        "identifier_types": [{"identifier_type_code": c, "identifier_type_name": n} for c, n in IDENTIFIER_TYPES],
        "reason_codes": [{"blacklist_reason_code_id": i, "reason_name": n} for i, n, _ in REASONS],
        "listings": listings,
    }
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")

    reason_names = {i: n for i, n, _ in REASONS}
    print(f"Wrote {len(listings)} listings to {OUT.relative_to(ROOT)}")
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")
    print("  by reason:")
    for rid, n in Counter(r["blacklist_reason_code_id"] for r in listings).most_common():
        print(f"    {reason_names[rid]}: {n}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_XLSX)
