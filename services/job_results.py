"""
Finished export files on disk, so a completed job's result survives the
in-memory job TTL (1h) and server restarts (2026-10-29).

Before this, Seller Analytics / PO Analytics / Vendor Offers exports kept the
finished xlsx only in the router's in-memory job dict: lost on restart, and
evicted an hour after completion. A Seller Analytics run was lost that way
when the user's browser tab was frozen in the background until after the
hour passed (see docs/infra/job_persistence.md, 2026-10-29).

Each finished file is written to data/job_results/<job_id>.xlsx with a
<job_id>.json sidecar ({job_id, kind, feature, filename, completed_at, size}).
Routers save on completion and fall back to `stub_job()` / `load_blob()` when
a job id is no longer in memory. Files older than KEEP_DAYS are pruned on save.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).resolve().parent.parent / "data" / "job_results"
KEEP_DAYS = 14
_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")


def _paths(job_id: str) -> tuple[Path, Path] | None:
    if not job_id or not _ID_RE.match(job_id):
        return None
    return RESULTS_DIR / f"{job_id}.xlsx", RESULTS_DIR / f"{job_id}.json"


def save(job_id: str, kind: str, feature: str, filename: str, blob: bytes) -> None:
    """Best-effort: a disk failure never fails the job itself."""
    p = _paths(job_id)
    if not p or not blob:
        return
    try:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        p[0].write_bytes(blob)
        p[1].write_text(json.dumps({
            "job_id": job_id, "kind": kind, "feature": feature, "filename": filename,
            "completed_at": time.time(), "size": len(blob),
        }), encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.exception("[job_results] could not save %s", job_id)
    prune()


def meta(job_id: str) -> dict | None:
    p = _paths(job_id)
    if not p or not p[1].exists() or not p[0].exists():
        return None
    try:
        return json.loads(p[1].read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def load_blob(job_id: str) -> bytes | None:
    p = _paths(job_id)
    if not p or not p[0].exists():
        return None
    return p[0].read_bytes()


def stub_job(job_id: str) -> dict | None:
    """A completed-job dict in the routers' own shape, for a job that's no
    longer in memory but whose file is on disk (blob is loaded on download)."""
    m = meta(job_id)
    if not m:
        return None
    t = m.get("completed_at")
    return {"status": "complete", "phase": "done", "done": 0, "total": 0, "error": None,
            "blob": None, "filename": m.get("filename"), "started": t, "phase_started": t,
            "completed_at": t, "from_disk": True}


def list_recent(limit: int = 50) -> list[dict]:
    if not RESULTS_DIR.exists():
        return []
    out = []
    for f in RESULTS_DIR.glob("*.json"):
        m = meta(f.stem)
        if m:
            out.append(m)
    out.sort(key=lambda m: m.get("completed_at") or 0, reverse=True)
    return out[:limit]


def prune() -> None:
    if not RESULTS_DIR.exists():
        return
    cutoff = time.time() - KEEP_DAYS * 86400
    for f in RESULTS_DIR.iterdir():
        try:
            if f.suffix in (".xlsx", ".json") and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass
