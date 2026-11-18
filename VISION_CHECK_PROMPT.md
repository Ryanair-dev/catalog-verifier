# Vision Check: Last Line of Defense

Two parts: (1) a prompt to paste into Claude Code to build the feature, and (2) the prompt the
feature itself sends to Claude with each image.

---

## 1. Prompt for Claude Code (paste this)

> Read VISION_CHECK_PROMPT.md and ML_TRAINING_PLAN.md (Phase 6). Build an image check for
> analytics candidates, used as a last line of defense, not on every item.
>
> **Which items get checked:** only candidates still undecided after everything else.
> For now that means `ai_verdict = 'uncertain'` in the Review bucket. Once `ml_score` exists
> (ML plan, Phase 4), switch to `ml_score` between configurable bounds (default 0.3–0.8).
> Never send items that are UPC-confirmed, already hard-rejected, or already decided by a human.
>
> **Getting the image:** add `images` to the SP-API includedData in the analytics search so
> the main image URL is stored with each candidate (no extra API calls). Use the MAIN variant
> at the smallest size of at least ~500px on the long edge. Pass the image to Claude by URL
> (Amazon image URLs are public). Don't download or base64 it unless a URL fails.
>
> **The call:** one image per call, using the system prompt and user message in section 2 of
> VISION_CHECK_PROMPT.md. Model `claude-sonnet-5-5`, thinking set to `between_tools` (off),
> structured output with the JSON schema in section 2, max_tokens 300. Run it as a background
> job with progress and a Stop button, mirroring `services/analytics/ai_check.py`
> (ThreadPoolExecutor, 4 workers, threading.Event for stop). Use the claude-api skill for
> correct SDK usage.
>
> **Storage:** new columns on analytics_candidates: `vision_verdict` (match / mismatch /
> unclear), `vision_reason`, `vision_checked_at`. Show a small badge in the run view next to
> the AI badge, and add both columns to the Excel export. It must NOT change the candidate's
> verdict automatically. An "Apply vision decisions" button does that, the same way "Apply AI
> decisions" works.
>
> **Cost guard:** before starting, show the item count and an estimated cost (about $0.006 per
> image on Sonnet 5.5) and ask for confirmation. Cache results per ASIN + vendor title so a
> re-run doesn't pay twice.
>
> Add pytest tests for the selection rule (which candidates qualify) and for parsing the JSON
> answer, with the Claude client mocked. Update CLAUDE.md when done.

---

## 2. Prompt the feature sends to Claude

**System prompt:**

```
You verify whether an Amazon product listing is the same product a vendor is selling.
You get the vendor's catalog line (often abbreviated, e.g. "SAFETEC UNSC 8OZ BTL") and the
Amazon listing's main image, title and brand.

Judge whether they are the SAME sellable product. Use the image to check things the text
may hide:
- Brand and product line printed on the packaging
- Variant: colour, shade number, scent, flavour, strength
- Size or volume printed on the package (oz, ml, count)
- Product form: spray vs cream, gel vs liquid, wipes vs pads

Rules:
- Pack and bundle differences are NOT a mismatch. A single unit shown as a 3-pack of the
  same unit is still a match.
- Vendor abbreviations are normal. Expand them (UNSC = unscented, BTL = bottle) before judging.
- Answer "mismatch" only for a clear, visible contradiction: a different shade number,
  scent, size, product form or brand.
- Answer "unclear" if the image is a lifestyle shot, the packaging text isn't readable, or
  the vendor line is too vague to compare. Never guess.
- Keep the reason to one short sentence that names what you saw.
```

**User message (per candidate):**

```
Vendor catalog line: {source_title}
Vendor brand: {source_brand}
Vendor item ID / MPN: {source_itemid}

Amazon title: {amazon_title}
Amazon brand: {amazon_brand}
Matcher notes: {short list of matched / mismatched signals}

[image]
```

**Structured output schema:**

```json
{
  "type": "object",
  "properties": {
    "verdict": { "type": "string", "enum": ["match", "mismatch", "unclear"] },
    "reason":  { "type": "string" },
    "seen_on_package": {
      "type": "object",
      "properties": {
        "brand":   { "type": "string" },
        "variant": { "type": "string" },
        "size":    { "type": "string" }
      },
      "required": ["brand", "variant", "size"],
      "additionalProperties": false
    }
  },
  "required": ["verdict", "reason", "seen_on_package"],
  "additionalProperties": false
}
```

`seen_on_package` records what the model actually read off the image (use "" when not
visible). It makes each decision checkable, and those values can later become extra features
for the ML model.
