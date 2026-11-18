# ML Match Model: Training Plan

**Goal:** stop hand-checking hundreds of "uncertain" AI-check items. A small model learns from
our own cleanup decisions, sorts the uncertain list so false positives sit at the top, and
eventually auto-decides the items it is confident about. Images are checked only for the few
items the model still can't call.

**Why the database alone can't train it:** our workflow bulk-promotes uncertain items to
Approved, and false positives are removed later in Excel. Those removals never reach the DB,
so the DB labels every promoted item as "approved", including the wrong ones. Phase 1 fixes
that before any training happens.

Each phase ends with a check that must pass before the next one starts. Each phase has a
ready-to-paste prompt for Claude Code at the bottom.

---

## Phase 1: Capture the Excel cleanup as labels

**What:** an "Import cleaned export" button on an analytics run.
- Upload the cleaned Excel. The app matches its rows to candidates by `(Row, ASIN)`, both
  already columns in the export (`routers/analytics.py` → `export_analytics_run`).
- Rows **removed** from the Approved sheet become `label = 0` (false positive).
  Rows **kept** become `label = 1`.
- Labels go in a new table `match_labels(run_id, row_idx, asin, label, source, created_at)`
  with `source='excel_cleanup'`. Candidate verdicts stay untouched, so nothing in the
  current workflow changes.
- The import shows a summary before saving, e.g. "412 kept, 38 removed, 3 rows not
  recognised", so a wrong file can't silently corrupt labels.
- **Backfill:** if old export/cleaned file pairs exist, import them too.

**Other label sources** (clean enough to include): `verified_items` and `pair_library`
(label 1), and `blacklisted_pairs` (label 0).

**Done when:** at least ~2,000 labelled candidates with at least ~150 negatives (false
positives), spread across at least 5 different runs.

## Phase 2: Build the dataset

**What:** `services/ml/dataset.py` builds one row per labelled candidate.
- **Features:** taken from `analytics_candidates.data_json.scores`, which already holds
  `confidence_score`, `brand_score`, title ratio, `upc_match`, `size_match`,
  `brand_confirmed`, MPN scores, and every mismatch flag. Also `sales_rank`, `vetting_mode`,
  `ai_verdict`, and the candidate's search sources.
- **Watch out:** older runs stored fewer score keys. Either rescore old runs first or limit
  the dataset to runs that have the full key set. Missing values stay missing (no zero-fill).
- **Exclude** any feature that's computed *from* the human decision (`review_status`,
  final `verdict`), or the model would just learn to read the answer.

**Done when:** a test confirms the feature list is identical for the same candidate whether
it comes from a new run or a rescore, and the leakage columns are absent.

## Phase 3: Train and evaluate

**What:** `services/ml/train.py`.
- **Model:** LightGBM (or scikit-learn's `HistGradientBoostingClassifier`, no extra
  dependency beyond scikit-learn). Neither is installed in `.venv` yet.
- **Split by run, not by row.** Whole runs go to training or testing, never both. Otherwise
  the model is graded on rows that are near-copies of training rows and the score looks
  better than reality.
- **Handle imbalance:** far more approvals than rejections. Use class weights.
- **Metrics:**
  - *Ranking:* in the uncertain bucket, what share of false positives land in the top 20%?
    (This is what Phase 4 delivers.)
  - *Precision at threshold:* the highest score above which at least 99% of items are truly
    correct. This decides Phase 5.
  - *Baseline:* the same metrics for today's `confidence_score` alone. The model must beat it.
- Saves `models/match_model_vN.pkl` plus a `metrics_vN.json`.

**Done when:** the model beats the `confidence_score` baseline on held-out runs, and the
result is written down in `metrics_vN.json`.

## Phase 4: Sort the uncertain list (first real payoff)

**What:**
- Score every candidate with the model and store `ml_score` (probability it's correct).
- Show it as a column in the run view. Sort the uncertain tab by it.
- Excel export: uncertain/review rows sorted **lowest `ml_score` first**, so cleanup means
  checking the top of the sheet, not all of it.
- Behind a setting (`ml_enabled`), default off until checked on a live run.

**Done when:** on 2–3 real runs, most of the false positives you would have removed sit in
the top part of the sorted list.

## Phase 5: Auto-decide high-confidence items

**What:** items above the 99%-precision threshold from Phase 3 are approved automatically;
items below a low threshold are flagged as likely rejects. Only the middle band needs you.
- Hard-reject rules (size, shade, count, colour, gender) stay as overrides. The model never
  approves something a hard rule rejects.
- Thresholds are stored in settings, not hardcoded.

**Done when:** precision on held-out runs is at least 99% for auto-approved items.

## Phase 6: Image check for the middle band only

See `VISION_CHECK_PROMPT.md`. Only items the model can't call (e.g. `ml_score` between 0.3
and 0.8) get a Sonnet 5.5 image comparison, roughly $0.15–0.30 per run.

## Phase 7: Keep it learning

- Every Excel cleanup import adds labels.
- Retrain weekly (same pattern as the Friday health-check scheduler), keep the old model, and
  switch only if the new one scores at least as well on the held-out runs.
- Log the version used to score each run, so a bad model can be rolled back.

---

## Prompts for Claude Code

Run one phase at a time. Review the result before moving on.

**Phase 1**
> Read ML_TRAINING_PLAN.md, Phase 1. Add an "Import cleaned export" feature to analytics runs:
> upload a cleaned Excel, diff it against the run's candidates by (Row, ASIN), and write labels
> to a new match_labels table (removed from the Approved sheet = 0, kept = 1,
> source='excel_cleanup'). Show a preview summary (kept / removed / unrecognised) before
> saving. Don't change candidate verdicts. Add pytest tests for the diff logic, including a
> renamed sheet, extra columns, and rows that don't match. Use the eval-harness and
> python-testing skills if they're installed.

**Phase 2 + 3**
> Read ML_TRAINING_PLAN.md, Phases 2 and 3. Build services/ml/dataset.py and
> services/ml/train.py. Features from data_json.scores plus sales_rank, vetting_mode,
> ai_verdict and sources; labels from match_labels, verified_items, pair_library and
> blacklisted_pairs. Exclude review_status and verdict. Split by run_id. Train a gradient
> boosting model with class weights and report ranking and precision-at-threshold against the
> confidence_score baseline. Save the model and a metrics JSON. Add tests that the feature
> list is stable and that no leakage columns are present. Follow the ml-adoption-playbook
> skill if it's installed.

**Phase 4**
> Read ML_TRAINING_PLAN.md, Phase 4. Score candidates with the latest model, store ml_score,
> show it in the run view, sort the review tab by it, and sort the Excel export's review rows
> lowest ml_score first. Put it behind an ml_enabled setting, default off.
