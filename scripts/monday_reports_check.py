"""
Check the Match Reports Monday board and the app's Monday sending (design v1, build steps 2-5).

    python scripts/monday_reports_check.py              check the key, the board and its columns
    python scripts/monday_reports_check.py --setup      create the missing columns on the board
                                                        (never changes or deletes existing ones)
    python scripts/monday_reports_check.py --fake       send fake reports to the TEST board:
                                                        a new pair, the same pair again, and a
                                                        repeated send that must not count twice
    python scripts/monday_reports_check.py --status     this PC's reports: sent / waiting / errors
    python scripts/monday_reports_check.py --retry      send every waiting report now
    python scripts/monday_reports_check.py --backfill   also send reports made before this change

Reads MONDAY_API_TOKEN and MONDAY_REPORTS_BOARD_ID from .env, like the app.
--fake refuses to run unless the board's name contains "test" (use --real-board to override).
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid 
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from services import monday_reports as mr  # noqa: E402


def check() -> tuple[str, dict] | None:
    if not mr.is_configured():
        print("NOT CONFIGURED: set MONDAY_API_TOKEN and MONDAY_REPORTS_BOARD_ID (a number) in .env")
        return None
    try:
        name, cols, problems = mr.resolve_columns()
    except mr.MondayError as exc:
        print(f"FAILED: {exc}")
        return None
    print(f'Board {mr.board_id()} "{name}": key works, {len(cols)} of {len(mr.COLUMNS)} columns found.')
    for p in problems:
        print(f"  - {p}")
    if not problems:
        print("  All columns are there with the right types.")
    elif all(k in cols for k in mr.REQUIRED):
        print("  The app can send; the columns above are skipped until they are added.")
    return name, cols


def fake(cols: dict) -> bool:
    tag = uuid.uuid4().hex[:6]
    base = {
        "asin": "B0FAKE0000", "vendor_mpn": f"TEST-{tag}", "vendor_upc": "", "row_idx": 0,
        "run_id": 0, "run_name": "Fake report (monday_reports_check)",
        "vendor_title": f"FAKE vendor item {tag} 12pk", "amazon_title": "FAKE Amazon listing 24 count",
        "verdict": "verified", "scoring_method": "cascade", "code_version": "check-script", "model_hash": "",
        "label": 0, "reasons": json.dumps(["pack_size"]), "note": "Fake report from the check script.",
        "correct_asin": "", "created_at": None,
    }
    base["pair_key"] = mr.pair_key(base)
    first = dict(base, report_uid=mr.new_report_uid(), reported_by="Check script A")
    again = dict(base, report_uid=mr.new_report_uid(), reported_by="Check script B", label=1,
                 reasons=json.dumps(["pack_equivalent"]), note="Second fake report, same pair.")
    steps = [
        ("new pair", first, "created"),
        ("same pair, another reporter", again, "counted"),
        ("first report sent again (retry after a lost answer)", first, "already"),
    ]
    ok = True
    item_id = None
    for label, rec, expected in steps:
        try:
            item_id, action = mr.send_record(rec, cols)
        except mr.MondayError as exc:
            print(f"  {label}: FAILED: {exc}")
            return False
        good = action == expected
        ok &= good
        print(f"  {label}: {action} (item {item_id}) {'OK' if good else f'EXPECTED {expected}'}")
    print(f'Look on the board for item "B0FAKE0000 · TEST-{tag}": Times reported should be 2, '
          "with one update. Delete it when done.")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--real-board", action="store_true", help="allow --fake on a board without 'test' in its name")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--retry", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    a = ap.parse_args()

    if a.status or a.retry or a.backfill:
        from services import match_feedback  # noqa: F401  (adds the new columns if needed)
        if a.backfill:
            print(f"Queued {mr.backfill()} older reports.")
        if a.backfill or a.retry:
            mr.retry_now()
            print("Sending:", mr.send_due())
        print(json.dumps(mr.status(), indent=2))
        return 0

    if a.setup:
        if not mr.is_configured():
            print("NOT CONFIGURED: set MONDAY_API_TOKEN and MONDAY_REPORTS_BOARD_ID (a number)")
            return 1
        try:
            print(f"Board {mr.board_id()}:")
            print("\n".join(mr.setup_board_columns()))
        except mr.MondayError as exc:
            print(f"FAILED: {exc}")
            return 1
        print()

    found = check()
    if not found:
        return 1
    name, cols = found
    if a.fake:
        if "test" not in name.lower() and not a.real_board:
            print(f'Not sending fake reports to "{name}": its name has no "test". '
                  "Point MONDAY_REPORTS_BOARD_ID at the test copy, or pass --real-board.")
            return 1
        return 0 if fake(cols) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
