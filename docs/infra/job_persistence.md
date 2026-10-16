<!-- doc-check: job-persistence-c60b -->
# Job Persistence (`background_jobs`)

## What this does

A shared mechanism so in-memory background jobs (PO Analytics report generation, Offer Analytics/Vendor Offers exports, and likely others) survive a server restart instead of silently vanishing mid-run. Confirmed real and shipped (session task list: "Persist + auto-resume all background jobs across server restart" — completed), and referenced from multiple feature docs (`docs/features/po_analytics.md`, `docs/features/vendor_offers.md`) as the system their own job/poll/download endpoints build on.

## Architecture

**Partially documented.** The `background_jobs` table exists (see `docs/infra/database.md`'s schema table) as a "shadow" of whatever in-memory job-state dicts each feature's router keeps (the pattern already established pre-this-feature: `job = {status, phase, done, total, filename, ...}` dicts in module-level `_JOBS`-style maps, polled via `GET .../jobs/{id}`). This persistence layer was evidently added to snapshot that state to SQLite and restore/resume it on the next startup, but the specific mechanics (what triggers a snapshot write, how resume detects an in-flight job and whether it restarts the work from scratch or picks up partway through, which features are actually wired to it) were not present in the source material this documentation restructure was built from — the feature was built in a session after the source `CLAUDE.md`'s last entry (2026-10-16).

**Before changing or extending this**: read `services/database.py`'s `background_jobs` table definition and whichever router(s) actually read/write it (search for `background_jobs` across `routers/`), rather than trusting the summary above as authoritative beyond "the table exists and is used for this purpose."

## Changelog

(No entries — this infra piece's build history was not present in the source material for this doc split. The first real entry here should be whatever change prompted someone to open this file.)
