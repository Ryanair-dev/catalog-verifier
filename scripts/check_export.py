"""Compare a run export with a list of correct pairs (reads only, changes nothing).

    python scripts/check_export.py <run export.xlsx> <correct pairs.xlsx or .csv>

The correct-pairs file needs an ASIN column and an Item ID column ("Item ID", "MPN" or "Source Item ID").
The vendor file you ran can be used directly when it has an ASIN column with the right answer.
"""
from __future__ import annotations

import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from openpyxl import load_workbook

ID_NAMES = ("item id", "itemid", "mpn", "source item id", "part number", "sku")


def _norm_id(value) -> str:
    s = str(value if value is not None else "").strip()
    s = re.sub(r"\.0+$", "", s)                     # 7847802.0 (number read from Excel) -> 7847802
    return s.upper()


def _rows(path: Path, sheet: str | None = None) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            return [{(k or "").strip(): v for k, v in r.items()} for r in csv.DictReader(f)]
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    header = [str(h or "").strip() for h in next(it, [])]
    out = []
    for values in it:
        row = {}
        for h, v in zip(header, values):
            if h and h not in row:                  # the FIRST column of a name wins (exports repeat "ASIN")
                row[h] = v
        out.append(row)
    return out


def load_truth(path: Path) -> set[tuple[str, str]]:
    rows = _rows(path)
    if not rows:
        sys.exit(f"{path}: no rows")
    cols = {c.lower(): c for c in rows[0]}
    asin_col = cols.get("asin")
    id_col = next((cols[n] for n in ID_NAMES if n in cols), None)
    if not asin_col or not id_col:
        sys.exit(f"{path}: needs an ASIN column and an Item ID / MPN column (found: {', '.join(rows[0])})")
    return {(str(r[asin_col]).strip().upper(), _norm_id(r[id_col])) for r in rows if r.get(asin_col) and r.get(id_col)}


def load_export(path: Path) -> list[dict]:
    wb = load_workbook(path, read_only=True)
    out = []
    for sheet in ("Approved", "Review", "Not Approved"):
        if sheet not in wb.sheetnames:
            continue
        for r in _rows(path, sheet):
            if not r.get("ASIN"):
                continue
            expl = str(r.get("Scoring explanation") or "")
            out.append({
                "sheet": sheet, "asin": str(r["ASIN"]).strip().upper(), "id": _norm_id(r.get("Source Item ID")),
                "row": r.get("Row"), "vendor_title": str(r.get("Source Title") or ""),
                "amazon_title": str(r.get("Amazon Title") or ""), "brand": str(r.get("Amazon Brand") or ""),
                "confidence": r.get("Confidence"), "expl": expl,
                "mpn_rule": "Brand and MPN match" in expl or expl.startswith("Rule 1: brand + MPN"),
                "brand_no": "Brand agreement: no" in expl,
            })
    return out


def _why(expl: str) -> str:
    for key in ("Variant check:", "Family check:"):
        i = expl.find(key)
        if i >= 0:
            return expl[i:i + 150].split(" Attributes")[0]
    return expl[:120]


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    truth = load_truth(Path(sys.argv[2]))
    cands = load_export(Path(sys.argv[1]))
    for c in cands:
        c["correct"] = (c["asin"], c["id"]) in truth

    found = {(c["asin"], c["id"]): c for c in cands if c["correct"]}
    by_sheet = Counter(c["sheet"] for c in found.values())
    missing = sorted(truth - set(found))
    wrong_ok = [c for c in cands if c["sheet"] == "Approved" and not c["correct"]]
    same_item = [c for c in wrong_ok if c["mpn_rule"]]
    check_these = [c for c in wrong_ok if not c["mpn_rule"]]

    print(f"Correct pairs: {len(truth)}   candidates in export: {len(cands)}\n")
    print("Where the correct pairs are:")
    for s in ("Approved", "Review", "Not Approved"):
        print(f"  {s:13} {by_sheet.get(s, 0)}")
    print(f"  {'Never found':13} {len(missing)}")
    print(f"\nApproved but NOT in your list: {len(wrong_ok)}")
    print(f"  {len(same_item)} by the brand + MPN rule (same MPN + same brand: probably other listings of the same item)")
    print(f"  {len(check_these)} by other rules  <- check these, they may be real mistakes")
    for c in check_these:
        print(f"     {c['id']} -> {c['asin']} ({c['confidence']}): {c['amazon_title'][:90]}")

    not_ok = [c for c in found.values() if c["sheet"] != "Approved"]
    if not_ok:
        print("\nCorrect pairs NOT approved, and why:")
        for c in sorted(not_ok, key=lambda c: c["id"]):
            print(f"  {c['id']} -> {c['asin']} [{c['sheet']}, {c['confidence']}]  {_why(c['expl'])}")
    if missing:
        print("\nCorrect pairs never found as a candidate:")
        for asin, i in missing:
            print(f"  {i} -> {asin}")

    brands_no = defaultdict(int)
    for c in cands:
        if c["brand_no"] and c["brand"]:
            brands_no[c["brand"]] += 1
    if brands_no:
        print("\nAmazon brands the system called a DIFFERENT brand (a sub-brand problem shows up here):")
        for b, n in sorted(brands_no.items(), key=lambda x: -x[1])[:15]:
            print(f"  {n:4}  {b}")
    return 0


if __name__ == "__main__":
    sys.exit(main())