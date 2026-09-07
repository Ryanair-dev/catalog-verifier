"""
Price Desk → Excel exports.

  build_xlsx(upcs, focus_vendor)  — the price comparison. If `upcs` is given the
      sheet is restricted to (and ordered by) those items — i.e. "export what I
      see"; if `focus_vendor` is given an extra "<vendor> cheapest?" column flags
      the items that vendor wins (sole-source items count as a win).
  build_lookup_xlsx(identifiers)  — a want-list price check: for each UPC/ASIN you
      paste, who is cheapest (or NOT FOUND if the desk doesn't carry it).

Both stream via openpyxl write_only mode (fast for the whole ~16k-row book).
"""
from __future__ import annotations

import datetime as dt
import io
import re

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from services import database
from services import vendor_offers as vo

_MONEY = '"$"#,##0.00##'   # $ with 2–4 decimals
_PCT = "0.0%"
_HDR_FILL = PatternFill("solid", fgColor="4D37A1")   # app purple-700
_HDR_FONT = Font(bold=True, color="FFFFFF", size=11)
_HDR_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
_WIN_FILL = PatternFill("solid", fgColor="D9F2EC")   # teal-tint = cheapest


def _day(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return dt.datetime.strptime(s[:10], "%Y-%m-%d").toordinal()
    except ValueError:
        return None


def _derive(item: list, vidx_latest: dict) -> dict:
    """Per-item figures from the compact ledger row
    [upc, title, asin, pack, status, n_asins, offers[]] where an offer is
    [vendorIdx, cost, qty_per_case, avail_qty, last_seen, first_seen]."""
    upc, title, asin, pack, status, n_asins, offers = item
    costs = [o[1] for o in offers]
    best = min(costs) if costs else None
    worst = max(costs) if costs else None
    best_r = round(best, 4) if best is not None else None
    by_vendor = {o[0]: o[1] for o in offers}
    winners = sorted(vi for vi, c in by_vendor.items() if round(c, 4) == best_r)
    above = sorted(c for c in costs if round(c, 4) > best_r) if best_r is not None else []
    runner = above[0] if above else None
    save = (runner - best) if (runner is not None and best is not None) else None
    stale = 0
    for vi, cost, qpc, aq, last, first in offers:
        if round(cost, 4) == best_r:
            ld = vidx_latest.get(vi)
            d = _day(last)
            if ld is not None and d is not None:
                stale = max(stale, ld - d)
    return {
        "upc": upc, "title": title or "", "asin": asin or "", "pack": pack,
        "status": status or "", "n_asins": n_asins,
        "n_vendors": len(by_vendor), "by_vendor": by_vendor,
        "best": best, "worst": worst, "winners": winners, "runner": runner,
        "save": save, "save_pct": (save / runner) if (save is not None and runner) else None,
        "spread": (worst - best) if (best is not None and worst is not None) else None,
        "spread_pct": ((worst - best) / worst) if worst else None,
        "stale": int(stale),
    }


def _hdr_cells(ws, headers):
    out = []
    for h in headers:
        c = WriteOnlyCell(ws, value=h)
        c.fill, c.font, c.alignment = _HDR_FILL, _HDR_FONT, _HDR_ALIGN
        out.append(c)
    return out


def _num(ws, value, kind, fill=None):
    c = WriteOnlyCell(ws, value=value)
    if kind == "money":
        c.number_format = _MONEY
    elif kind == "pct":
        c.number_format = _PCT
    if fill is not None:
        c.fill = fill
    return c


def _ledger_derived():
    led = vo.build_ledger()
    vendors = led["vendors"]
    vlatest = led.get("vendorLatest") or {}
    vidx_latest = {i: _day(vlatest.get(v)) for i, v in enumerate(vendors)}
    derived = {}
    for it in led["items"]:
        d = _derive(it, vidx_latest)
        derived[d["upc"]] = d
    return vendors, derived


def build_xlsx(upcs: list[str] | None = None, focus_vendor: str | None = None) -> bytes:
    """Price comparison workbook. `upcs` (already normalized ledger UPCs, in the
    desired order) restricts + orders the rows; `focus_vendor` adds a cheapest-flag
    column for that vendor."""
    vendors, derived = _ledger_derived()

    if upcs:
        rows = [derived[u] for u in upcs if u in derived]
    else:
        rows = sorted(derived.values(),
                      key=lambda r: (r["save"] if r["save"] is not None else -1),
                      reverse=True)

    focus_idx = vendors.index(focus_vendor) if focus_vendor in vendors else None

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Price Desk")

    # ── column layout (built dynamically so the optional focus column doesn't
    #    require re-indexing the number formats) ──
    headers = ["UPC", "Title", "ASIN", "Amazon Pack", "Status", "# ASINs",
               "# Vendors", "Cheapest Vendor"]
    widths = [15, 42, 13, 8, 12, 8, 9, 18]
    if focus_idx is not None:
        headers.append(f"{focus_vendor} cheapest?")
        widths.append(16)
    headers += ["Cheapest Cost", "Runner-up", "Saving / unit", "Saving %",
                "Spread", "Spread %", "Stale (days)"]
    widths += [12, 12, 12, 9, 11, 9, 11]
    vendor_start = len(headers)                 # 0-based index of first vendor col
    headers += list(vendors)
    widths += [13] * len(vendors)

    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.append(_hdr_cells(ws, headers))

    def _focus_flag(r):
        if focus_idx in r["winners"]:
            return "Yes (only vendor)" if r["n_vendors"] == 1 else "Yes"
        return "No"

    for r in rows:
        cheapest = ", ".join(vendors[vi] for vi in r["winners"]) if r["winners"] else ""
        line = [r["upc"], r["title"], r["asin"], r["pack"], r["status"],
                r["n_asins"], r["n_vendors"], cheapest]
        if focus_idx is not None:
            line.append(_focus_flag(r))
        line += [
            _num(ws, r["best"], "money"), _num(ws, r["runner"], "money"),
            _num(ws, r["save"], "money"), _num(ws, r["save_pct"], "pct"),
            _num(ws, r["spread"], "money"), _num(ws, r["spread_pct"], "pct"),
            r["stale"],
        ]
        for vi in range(len(vendors)):
            cost = r["by_vendor"].get(vi)
            line.append(_num(ws, cost, "money",
                             fill=_WIN_FILL if vi in r["winners"] else None)
                        if cost is not None else None)
        ws.append(line)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_lookup_xlsx(identifiers: list[str]) -> bytes:
    """Want-list price check. For each pasted UPC or ASIN, report the cheapest
    vendor/cost in the desk — or NOT FOUND when the desk doesn't carry it."""
    vendors, derived = _ledger_derived()

    # ASIN → the desk UPC(s) it maps to (via the vo_asin_by_upc union view)
    asin_to_upcs: dict[str, list[str]] = {}
    try:
        with database._connect() as conn:
            for asin, upc in conn.execute("SELECT asin, upc FROM vo_asin_by_upc"):
                if asin:
                    asin_to_upcs.setdefault(asin.strip().upper(), []).append(upc)
    except Exception:
        pass

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Price Check")
    headers = ["Input", "Type", "Found?", "Matched UPC", "Title",
               "Cheapest Vendor", "Cheapest Cost", "Runner-up", "Saving / unit", "# Vendors"]
    widths = [16, 7, 11, 15, 42, 18, 13, 12, 12, 9]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.append(_hdr_cells(ws, headers))

    for raw in identifiers:
        s = (raw or "").strip()
        if not s:
            continue
        is_asin = bool(re.fullmatch(r"B0[A-Z0-9]{8}", s.upper()))
        matched = None
        if is_asin:
            for u in asin_to_upcs.get(s.upper(), []):
                if u in derived:
                    matched = u
                    break
        else:
            nu = vo.norm_upc(s)
            if nu in derived:
                matched = nu
        if matched:
            d = derived[matched]
            cheapest = ", ".join(vendors[vi] for vi in d["winners"]) if d["winners"] else ""
            ws.append([
                s, "ASIN" if is_asin else "UPC", "found", matched, d["title"],
                cheapest, _num(ws, d["best"], "money"), _num(ws, d["runner"], "money"),
                _num(ws, d["save"], "money"), d["n_vendors"],
            ])
        else:
            ws.append([s, "ASIN" if is_asin else "UPC", "NOT FOUND",
                       "", "", "", None, None, None, 0])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def parse_identifiers(text: str) -> list[str]:
    """Split pasted text (newlines / commas / tabs / whitespace) into identifiers,
    dropping an obvious header token."""
    toks = [t.strip() for t in re.split(r"[\s,;|]+", str(text or "")) if t.strip()]
    return [t for t in toks if t.lower() not in ("upc", "asin", "barcode", "sku")]


def export_filename(prefix: str = "price_desk") -> str:
    return f"{prefix}_{dt.date.today().isoformat()}.xlsx"
