/* ============================================================================
   Catalog Verifier — frontend logic (v2).

   Major changes vs. v1:
     • Barcode chain is driven server-side; UI just displays which source hit.
     • Override flow is strict: tab-specific action buttons + tagged transitions.
         Auto Verified           → review_status = ""
         Review → Approve        → review_status = "Reviewed"            (→ Verified)
         Review → Discard        → review_status = ""                    (→ Not Approved, blacklisted)
         Not Approved → Promote  → review_status = "Manually Approved"   (→ Verified)
         Approved → Reject       → review_status = "Manually Rejected"   (→ Not Approved, blacklisted)
     • Thresholds adjustable via a Settings side panel (persisted server-side).
     • Abbreviation library is categorised, SQLite-backed, editable per category.
     • AI Extract toggle lives on the Catalog upload card.
     • Pair Manager is a dedicated view, search-first.
     • Pre-export warning modal when Review still has unreviewed rows.
   ========================================================================== */

(() => {
  "use strict";

  // ---------- App state ----------------------------------------------------
  const state = {
    catalogFile: null,
    amazonFile: null,
    amazonSource: "keepa",
    library: {},              // { category: [{id, abbr, full}, ...] }
    categories: [],
    results: [],
    aiMode: false,
    reviewTab: "Verified",
    thresholds: { verified: 85, review: 35 },
    currentView: "cpg",
  };

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  // ---------- Utilities ----------------------------------------------------
  const fmtConfidence = (n) => (n == null ? "—" : `${Number(n).toFixed(0)}%`);
  const verdictClass = (v) =>
      v === "Verified" ? "row-verified"
    : v === "Review"   ? "row-review"
    : (v === "Not Approved" || v === "Not Verified") ? "row-not" : "";
  const badgeClass = (v) =>
      v === "Verified" ? "badge-verified"
    : v === "Review"   ? "badge-review"
    : (v === "Not Approved" || v === "Not Verified") ? "badge-not" : "badge-overridden";
  const signalTone = (s) => {
    if (!s || s.score == null) return "";
    if (s.score >= state.thresholds.verified) return "ok";
    if (s.score >= state.thresholds.review) return "warn";
    return "bad";
  };
  const escapeHtml = (s) => s == null ? "" : String(s)
      .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
      .replace(/"/g,"&quot;").replace(/'/g,"&#039;");

  const reviewStatusClass = (tag) =>
      tag === "Reviewed"           ? "tag-reviewed"
    : tag === "Manually Approved"  ? "tag-manually-approved"
    : tag === "Manually Rejected"  ? "tag-manually-rejected"
    : "";

  function rowKey(r) { return `${r["UPC/EAN"] || ""}::${r["ASIN"] || ""}::${r["Item ID"] || ""}`; }
  function failedSignalsOf(row) {
    const out = [];
    const s = row.signals || {};
    if (s.upc     && !s.upc.matched)     out.push("UPC");
    if (s.item_id && !s.item_id.matched) out.push("Item ID");
    if (s.brand   && !s.brand.matched)   out.push("Brand");
    if (s.title   && !s.title.matched)   out.push("Title");
    if (s.pack    && !s.pack.matched)    out.push("Pack");
    return out;
  }

  // ---------- API --------------------------------------------------------
  async function api(path, { method = "GET", body = null, form = false } = {}) {
    const opts = { method };
    if (form) {
      opts.body = body;
    } else if (body !== null) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(path, opts);
    if (!resp.ok) throw new Error(await resp.text());
    const ct = resp.headers.get("content-type") || "";
    return ct.includes("application/json") ? resp.json() : resp.blob();
  }

  async function apiHealth() {
    try {
      const j = await api("/api/health");
      $("#api-status").textContent = j.ai_available
        ? "API ready · AI enabled" : "API ready";
      if (!j.ai_available) $("#ai-toggle").title = "OPENAI_API_KEY missing in .env";
    } catch {
      $("#api-status").textContent = "API offline";
    }
  }

  // ---------- Library ----------------------------------------------------
  async function loadLibrary() {
    try {
      const j = await api("/api/library");
      state.categories = j.categories || [];
      state.library = j.library || {};
      populateCategorySelect();
      renderLibrary();
    } catch (e) { console.error("loadLibrary", e); }
  }

  function populateCategorySelect() {
    const sel = $("#abbr-new-cat");
    sel.innerHTML = "";
    state.categories.forEach(c => {
      const opt = document.createElement("option");
      opt.value = c; opt.textContent = c;
      sel.appendChild(opt);
    });
  }

  function renderLibrary() {
    const container = $("#abbr-categories");
    container.innerHTML = "";
    state.categories.forEach(cat => {
      const entries = state.library[cat] || [];
      const block = document.createElement("div");
      block.className = "abbr-cat";
      const rows = entries.map(e => `
        <tr>
          <td class="font-mono">${escapeHtml(e.abbr)}</td>
          <td>${escapeHtml(e.full)}</td>
          <td><button class="del-btn" data-id="${e.id}">✕</button></td>
        </tr>`).join("");
      block.innerHTML = `
        <h4>${escapeHtml(cat)} <span style="color: var(--grey-500); font-weight: 400;">(${entries.length})</span></h4>
        <table>${rows || '<tr><td colspan="3" class="text-xs" style="color:#6b7480;">No entries.</td></tr>'}</table>`;
      container.appendChild(block);
    });

    container.querySelectorAll(".del-btn").forEach(btn =>
      btn.addEventListener("click", async () => {
        await api("/api/library/delete", { method: "POST", body: { id: Number(btn.dataset.id) } });
        loadLibrary();
      }));
  }

  $("#abbr-add").addEventListener("click", async () => {
    const cat = $("#abbr-new-cat").value;
    const abbr = $("#abbr-new-key").value.trim();
    const full = $("#abbr-new-val").value.trim() || abbr;
    if (!abbr) return;
    try {
      await api("/api/library", { method: "POST", body: { category: cat, abbr, full } });
      $("#abbr-new-key").value = "";
      $("#abbr-new-val").value = "";
      loadLibrary();
    } catch (e) { alert(e.message); }
  });

  $("#open-abbr-btn").addEventListener("click", () => $("#abbr-panel").classList.add("open"));
  $("#abbr-close").addEventListener("click", () => $("#abbr-panel").classList.remove("open"));

  // ---------- Settings panel ---------------------------------------------
  async function loadThresholds() {
    try {
      const j = await api("/api/settings/thresholds");
      state.thresholds = { verified: Number(j.verified), review: Number(j.review) };
      $("#threshold-verified").value = state.thresholds.verified;
      $("#threshold-review").value = state.thresholds.review;
    } catch {}
  }
  $("#open-settings-btn").addEventListener("click", () => $("#settings-panel").classList.add("open"));
  $("#settings-close").addEventListener("click", () => $("#settings-panel").classList.remove("open"));
  $("#settings-save").addEventListener("click", async () => {
    const v = Number($("#threshold-verified").value);
    const r = Number($("#threshold-review").value);
    try {
      const j = await api("/api/settings/thresholds", { method: "POST", body: { verified: v, review: r } });
      state.thresholds = { verified: Number(j.verified), review: Number(j.review) };
      $("#settings-feedback").textContent = "Saved. Run Verification again to apply.";
      setTimeout(() => $("#settings-feedback").textContent = "", 3000);
    } catch (e) { alert("Failed: " + e.message); }
  });

  // ---------- Source toggle ----------------------------------------------
  $$("#amazon-source-toggle button").forEach(btn => btn.addEventListener("click", () => {
    $$("#amazon-source-toggle button").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    state.amazonSource = btn.dataset.source;
  }));

  // ---------- AI toggle (button now lives on the Catalog upload card) ----
  $("#ai-toggle").addEventListener("click", () => {
    state.aiMode = !state.aiMode;
    $("#ai-banner").classList.toggle("hidden", !state.aiMode);
    $("#ai-toggle").classList.toggle("active", state.aiMode);
  });
  $("#ai-banner-close").addEventListener("click", () => {
    state.aiMode = false;
    $("#ai-banner").classList.add("hidden");
    $("#ai-toggle").classList.remove("active");
  });

  // ---------- Drop zones --------------------------------------------------
  function wireDropZone(zoneId, inputId, target) {
    const zone = $(zoneId), input = $(inputId);
    zone.addEventListener("click", () => input.click());
    ["dragover"].forEach(ev => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("drag"); }));
    ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("drag"); }));
    zone.addEventListener("drop", (e) => { if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0], target, zone); });
    input.addEventListener("change", (e) => { if (e.target.files[0]) handleFile(e.target.files[0], target, zone); });
  }
  function handleFile(file, target, zone) {
    state[target] = file;
    zone.classList.add("loaded");
    zone.querySelector(".text-sm").innerHTML =
      `<span class="file-name">${escapeHtml(file.name)}</span>`;
    zone.querySelector(".text-xs").innerHTML =
      `${(file.size / 1024).toFixed(1)} KB — click to replace`;
    updateStatusHint();
  }
  wireDropZone("#catalog-drop", "#catalog-input", "catalogFile");
  wireDropZone("#amazon-drop", "#amazon-input", "amazonFile");

  function updateStatusHint() {
    const hasBoth = state.catalogFile && state.amazonFile;
    $("#status-hint").innerHTML = hasBoth
      ? "Both files loaded. Click <b>Run Verification</b>."
      : "Sample CPG dataset pre-loaded — click <b>Run Verification</b> to see the engine in action.";
  }

  // ---------- Verification run -------------------------------------------
  $("#run-btn").addEventListener("click", runVerification);
  $("#reset-btn").addEventListener("click", resetView);
  $("#load-sample-btn").addEventListener("click", loadSample);

  function resetView() {
    state.catalogFile = null;
    state.amazonFile = null;
    state.results = [];
    ["#catalog-drop", "#amazon-drop"].forEach(sel => {
      const el = $(sel);
      el.classList.remove("loaded");
      el.querySelector(".text-sm").innerHTML =
        '<span class="font-medium" style="color: var(--navy-800)">Click to upload</span> or drag &amp; drop';
      el.querySelector(".text-xs").textContent = '.xlsx file';
    });
    $("#review-btn").disabled = true;
    $("#export-btn").disabled = true;
    $("#summary-section").classList.add("hidden");
    renderResults();
    updateStatusHint();
  }

  async function runVerification() {
    if (!state.catalogFile || !state.amazonFile) {
      alert("Please upload both the catalog and Amazon data files, or click Load Sample.");
      return;
    }
    showProgress(true, "Parsing files & scoring…");
    updateProgress(0, 1, "Parsing files & scoring…");

    const fd = new FormData();
    fd.append("catalog_file", state.catalogFile);
    fd.append("amazon_file", state.amazonFile);
    fd.append("amazon_source", state.amazonSource);
    fd.append("ai_mode", state.aiMode ? "true" : "false");
    let payload;
    try {
      payload = await api("/api/verify", { method: "POST", body: fd, form: true });
    } catch (err) {
      alert("Verification failed: " + err.message); showProgress(false); return;
    }

    if (payload.thresholds) state.thresholds = payload.thresholds;
    state.results = payload.results.map(r => ({ ...r, barcode_db: null, barcode_loading: true }));
    renderSummary();
    renderResults();

    // Barcode chain — bounded pool of 6 workers.
    const total = state.results.length;
    updateProgress(0, total, `Looking up ${total} barcodes…`);
    let done = 0;
    const queue = [...state.results.keys()];
    const workers = new Array(6).fill(0).map(async () => {
      while (queue.length) {
        const idx = queue.shift();
        await lookupRowBarcode(state.results[idx]);
        done += 1;
        if (done % 2 === 0 || done === total) {
          updateProgress(done, total, `Verifying ${done} of ${total} products…`);
          renderResults();
        }
      }
    });
    await Promise.all(workers);
    renderResults();
    showProgress(false);
    $("#review-btn").disabled = false;
    $("#export-btn").disabled = false;
  }

  async function lookupRowBarcode(row) {
    row.barcode_loading = true;
    if (!row["UPC/EAN"]) { row.barcode_db = { found: false, reason: "No UPC" }; row.barcode_loading = false; return; }
    try {
      row.barcode_db = await api("/api/barcode/lookup", {
        method: "POST",
        body: { upc: row["UPC/EAN"], brand: row["Brand"] || "", vendor_title: row["Vendor Title"] || "" },
      });
    } catch {
      row.barcode_db = { found: false, reason: "Lookup error" };
    } finally {
      row.barcode_loading = false;
    }
  }

  function showProgress(show, label = "") {
    $("#progress-section").classList.toggle("hidden", !show);
    if (show) $("#progress-label").textContent = label;
  }
  function updateProgress(done, total, label) {
    const pct = total ? (done / total) * 100 : 0;
    $("#progress-bar").style.width = `${pct}%`;
    $("#progress-count").textContent = `${done} of ${total}`;
    if (label) $("#progress-label").textContent = label;
  }

  // ---------- Summary -----------------------------------------------------
  function renderSummary() {
    const total = state.results.length;
    const v = state.results.filter(r => r.verdict === "Verified").length;
    const rev = state.results.filter(r => r.verdict === "Review").length;
    const n = state.results.filter(r => r.verdict === "Not Approved" || r.verdict === "Not Verified").length;
    $("#sum-total").textContent = total;
    $("#sum-verified").textContent = v;
    $("#sum-review").textContent = rev;
    $("#sum-not").textContent = n;
    $("#summary-section").classList.toggle("hidden", total === 0);
  }

  // ---------- Results table ----------------------------------------------
  ["#verdict-filter", "#sort-by", "#search"].forEach(sel => $(sel).addEventListener("input", renderResults));

  function renderResults() {
    const filter = $("#verdict-filter").value;
    const sort = $("#sort-by").value;
    const query = $("#search").value.toLowerCase().trim();

    let rows = [...state.results];
    if (filter === "Duplicate") rows = rows.filter(r => r.duplicate);
    else if (filter !== "all") rows = rows.filter(r => r.verdict === filter);

    if (query) {
      rows = rows.filter(r =>
        [r["Vendor Title"], r["Brand"], r["ASIN"], r["Item ID"], r["UPC/EAN"]]
          .map(v => String(v || "").toLowerCase()).some(v => v.includes(query)));
    }

    const sortFns = {
      "conf-desc": (a, b) => b.confidence - a.confidence,
      "conf-asc":  (a, b) => a.confidence - b.confidence,
      "verdict":   (a, b) => String(a.verdict).localeCompare(String(b.verdict)),
      "brand":     (a, b) => String(a.Brand || "").localeCompare(String(b.Brand || "")),
    };
    rows.sort(sortFns[sort] || sortFns["conf-desc"]);

    const body = $("#results-body");
    body.innerHTML = "";
    if (state.results.length === 0) {
      $("#empty-state").classList.remove("hidden");
      $("#results-table-wrap").classList.add("hidden");
      return;
    }
    $("#empty-state").classList.add("hidden");
    $("#results-table-wrap").classList.remove("hidden");

    for (const row of rows) {
      const tr = document.createElement("tr");
      tr.className = "fade-in " + verdictClass(row.verdict)
        + (row.duplicate ? " row-duplicate" : "");

      const s = row.signals || {};
      const b = row.barcode_db;
      const barcodeHTML = row.barcode_loading
        ? `<span class="mini-spinner" id="sp-${escapeHtml(row.ASIN)}"></span>`
        : b == null ? ""
          : b.found
            ? `<div class="text-xs"><div><b>${escapeHtml(b.source || "")}</b></div><div>${escapeHtml(b.name || "")}</div>${b.aligned === false ? '<div class="text-[11px]" style="color: var(--yellow-500)">✱ not aligned</div>' : ''}</div>`
            : `<span class="text-xs" style="color: #6b7480;">${escapeHtml(b.reason || 'Not Found')}</span>`;

      const statusTag = row.review_status
        ? `<span class="badge ${reviewStatusClass(row.review_status)}">${row.review_status}</span>`
        : '<span class="text-xs" style="color: #6b7480;">—</span>';

      const verdictBadge = row.review_status
        ? `<span class="badge ${badgeClass(row.original_verdict)} strikethrough">${row.original_verdict}</span>
           <span class="badge ${badgeClass(row.verdict)} ml-1">${row.verdict}</span>`
        : `<span class="badge ${badgeClass(row.verdict)}">${row.verdict}</span>`;

      const confBarClass = row.verdict === "Verified" ? "verified"
                         : row.verdict === "Review"   ? "review" : "not";

      tr.innerHTML = `
        <td>${escapeHtml(row["UPC/EAN"])}</td>
        <td>${escapeHtml(row["Item ID"])}${row.duplicate ? '<div><span class="badge badge-duplicate">Duplicate</span></div>' : ''}</td>
        <td style="max-width: 340px;">
          <div class="text-sm" style="color: var(--navy-800); font-weight: 500;">${escapeHtml(row["Vendor Title"])}</div>
        </td>
        <td>${escapeHtml(row["Brand"])}</td>
        <td class="font-mono text-xs">${escapeHtml(row["ASIN"])}</td>
        <td>
          <div class="signal-score ${signalTone({score: row.confidence})}">${fmtConfidence(row.confidence)}</div>
          <div class="confidence-bar"><div class="fill ${confBarClass}" style="width: ${Math.max(3, row.confidence)}%"></div></div>
        </td>
        <td>${verdictBadge}</td>
        <td>${statusTag}</td>
        ${signalCell(s.upc)}${signalCell(s.item_id)}${signalCell(s.brand)}${signalCell(s.title)}${signalCell(s.pack)}
        <td class="text-center">${row.amz_pack == null ? '—' : row.amz_pack}</td>
        <td>${barcodeHTML}</td>
        <td class="text-xs" style="color: #6b7480;">${escapeHtml(row.notes || "")}</td>
        <td>
          <button class="row-action-btn clear-cache" data-upc="${escapeHtml(row['UPC/EAN'] || '')}" data-asin="${escapeHtml(row.ASIN || '')}">Clear cache</button>
        </td>`;
      body.appendChild(tr);
    }
    body.querySelectorAll(".clear-cache").forEach(b => b.addEventListener("click", handleClearCache));
    renderSummary();
  }

  function signalCell(s) {
    if (!s) return '<td></td>';
    const tone = signalTone(s);
    return `<td>
      <div class="signal-cell">
        <span class="signal-score ${tone}">${s.score}%</span>
        <span class="text-[11px]" title="${escapeHtml(s.detail || '')}">${escapeHtml((s.detail || "").slice(0, 50))}</span>
      </div>
    </td>`;
  }

  async function handleClearCache(e) {
    const btn = e.currentTarget;
    const upc = btn.dataset.upc, asin = btn.dataset.asin;
    const row = state.results.find(r => r.ASIN === asin && String(r["UPC/EAN"] || "") === upc);
    if (!row) return;
    btn.innerHTML = '<span class="mini-spinner"></span>';
    btn.disabled = true;
    try {
      await api("/api/attributes/clear", { method: "POST", body: { upc, asin } });
      await lookupRowBarcode(row);   // background re-fetch from the chain
    } catch (err) {
      console.error(err);
    } finally {
      renderResults();
    }
  }

  // ---------- Review modal ------------------------------------------------
  $("#review-btn").addEventListener("click", openReview);
  $("#review-close").addEventListener("click", () => $("#review-modal").classList.add("hidden"));
  $("#confirm-export").addEventListener("click", () => { $("#review-modal").classList.add("hidden"); exportFlow(); });

  function openReview() {
    $("#review-modal").classList.remove("hidden");
    activateReviewTab("Verified");
  }
  $$(".modal-tab").forEach(t => t.addEventListener("click", () => activateReviewTab(t.dataset.reviewTab)));
  function activateReviewTab(name) {
    state.reviewTab = name;
    $$(".modal-tab").forEach(t => t.classList.toggle("active", t.dataset.reviewTab === name));
    const bulkBtn = $("#bulk-action");
    bulkBtn.textContent =
      name === "Verified"     ? "Reject All"
    : name === "Review"       ? "Approve All"
    : /* Not Approved  */       "Promote All";
    renderReview();
  }
  $("#review-search").addEventListener("input", renderReview);

  function reviewRows() {
    const q = $("#review-search").value.toLowerCase().trim();
    return state.results
      .filter(r => r.verdict === state.reviewTab)
      .filter(r => !q || [r["Vendor Title"], r["Brand"], r["ASIN"]]
          .map(v => String(v || "").toLowerCase()).some(v => v.includes(q)));
  }

  function renderReview() {
    const tabs = { "Verified": 0, "Review": 0, "Not Approved": 0 };
    state.results.forEach(r => {
      if (r.verdict in tabs) tabs[r.verdict] += 1;
    });
    $("#count-Verified").textContent = tabs["Verified"];
    $("#count-Review").textContent   = tabs["Review"];
    $("#count-Not").textContent      = tabs["Not Approved"];

    const rows = reviewRows();
    const tagged = rows.filter(r => r.review_status).length;
    $("#review-summary").innerHTML =
      `<b>${rows.length}</b> ${state.reviewTab}` +
      (tagged ? ` — <span style="color: var(--orange-500);">${tagged} with review tag</span>` : "");

    const body = $("#review-body");
    body.innerHTML = "";
    if (rows.length === 0) {
      body.innerHTML = `<tr><td colspan="10" class="text-center py-10" style="color: #6b7480;">No rows in this tab.</td></tr>`;
      return;
    }

    const actionButton = (row) => {
      if (state.reviewTab === "Verified") {
        return `<button class="row-action-btn reject" data-action="reject" data-key="${escapeHtml(rowKey(row))}">Reject</button>`;
      }
      if (state.reviewTab === "Not Approved") {
        return `<button class="row-action-btn promote" data-action="promote" data-key="${escapeHtml(rowKey(row))}">Promote to Approved</button>`;
      }
      // Review tab has two actions.
      return `
        <button class="row-action-btn approve mr-1" data-action="approve" data-key="${escapeHtml(rowKey(row))}">Approve</button>
        <button class="row-action-btn discard" data-action="discard" data-key="${escapeHtml(rowKey(row))}">Discard</button>`;
    };

    rows.forEach(row => {
      const s = row.signals || {};
      const tr = document.createElement("tr");
      tr.className = verdictClass(row.verdict);
      const tagHtml = row.review_status
        ? `<div><span class="badge ${reviewStatusClass(row.review_status)}">${row.review_status}</span></div>` : "";
      tr.innerHTML = `
        <td style="max-width: 360px;">
          <div class="text-sm" style="color: var(--navy-800); font-weight: 500;">${escapeHtml(row["Vendor Title"])}</div>
          <div class="text-xs" style="color: #6b7480;">${escapeHtml(row["Brand"])}</div>
          ${tagHtml}
        </td>
        <td class="font-mono text-xs">${escapeHtml(row["ASIN"])}</td>
        <td class="font-semibold">${fmtConfidence(row.confidence)}</td>
        <td><span class="badge ${badgeClass(row.verdict)}">${row.verdict}</span></td>
        <td class="text-xs">${s.upc ? s.upc.score + "%" : ""}</td>
        <td class="text-xs">${s.item_id ? s.item_id.score + "%" : ""}</td>
        <td class="text-xs">${s.brand ? s.brand.score + "%" : ""}</td>
        <td class="text-xs">${s.title ? s.title.score + "%" : ""}</td>
        <td class="text-xs">${s.pack ? s.pack.score + "%" : ""}</td>
        <td class="text-right">${actionButton(row)}</td>`;
      body.appendChild(tr);
    });

    body.querySelectorAll("[data-action]").forEach(btn => btn.addEventListener("click", handleReviewAction));
  }

  async function handleReviewAction(e) {
    const btn = e.currentTarget;
    const action = btn.dataset.action;
    const key = btn.dataset.key;
    const row = state.results.find(r => rowKey(r) === key);
    if (!row) return;
    await applyTransition(row, action);
    renderReview();
    renderResults();
  }

  /**
   * Apply a verdict transition according to the strict flow rules.
   * Also updates the server-side blacklist/verified caches.
   */
  async function applyTransition(row, action) {
    const upc = row["UPC/EAN"] || "";
    const asin = row["ASIN"] || "";

    switch (action) {
      case "approve":  // Review → Verified (tag: Reviewed)
        row.verdict = "Verified";
        row.review_status = "Reviewed";
        await api("/api/verified", { method: "POST", body: { upc, asin, review_status: "Reviewed", data: row } });
        break;

      case "discard":  // Review → Not Approved (no tag, blacklisted)
        row.verdict = "Not Approved";
        row.review_status = "";
        await api("/api/blacklist", { method: "POST", body: {
          upc, asin, confidence: row.confidence, failed_signals: failedSignalsOf(row),
        }});
        break;

      case "promote":  // Not Approved → Verified (tag: Manually Approved)
        row.verdict = "Verified";
        row.review_status = "Manually Approved";
        await api("/api/verified", { method: "POST", body: { upc, asin, review_status: "Manually Approved", data: row } });
        break;

      case "reject":  // Verified → Not Approved (tag: Manually Rejected)
        row.verdict = "Not Approved";
        row.review_status = "Manually Rejected";
        await api("/api/blacklist", { method: "POST", body: {
          upc, asin, confidence: row.confidence, failed_signals: failedSignalsOf(row),
        }});
        break;
    }
  }

  $("#bulk-action").addEventListener("click", async () => {
    const rows = reviewRows();
    if (rows.length === 0) return;
    const action =
        state.reviewTab === "Verified"      ? "reject"
      : state.reviewTab === "Not Approved"  ? "promote"
      : /* Review */                           "approve";
    for (const r of rows) {
      // eslint-disable-next-line no-await-in-loop
      await applyTransition(r, action);
    }
    renderReview();
    renderResults();
  });

  // ---------- Export ------------------------------------------------------
  $("#export-btn").addEventListener("click", exportFlow);

  async function exportFlow() {
    const unreviewed = state.results.filter(r => r.verdict === "Review").length;
    if (unreviewed > 0) {
      $("#unreviewed-count").textContent = unreviewed;
      $("#export-warn-modal").classList.remove("hidden");
      return;
    }
    await doExport();
  }
  $("#warn-go-back").addEventListener("click", () => {
    $("#export-warn-modal").classList.add("hidden"); openReview(); activateReviewTab("Review");
  });
  $("#warn-export").addEventListener("click", async () => {
    $("#export-warn-modal").classList.add("hidden"); await doExport();
  });

  async function doExport() {
    if (state.results.length === 0) return;
    try {
      // Build the flat abbreviations list for the export sheet.
      const abbrFlat = [];
      Object.entries(state.library).forEach(([cat, entries]) => {
        entries.forEach(e => abbrFlat.push({ abbr: e.abbr, full: e.full, category: cat }));
      });
      const blob = await api("/api/export", {
        method: "POST",
        body: { results: state.results, abbreviations: abbrFlat },
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = "catalog_verification_results.xlsx";
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      alert("Export failed: " + err.message);
    }
  }

  // ---------- Pair Manager view ------------------------------------------
  $$(".sidebar .nav-item").forEach(n => {
    if (n.dataset.view) {
      n.addEventListener("click", () => switchView(n.dataset.view, n));
    }
  });
  function switchView(view, navEl) {
    state.currentView = view;
    $$(".sidebar .nav-item").forEach(n => n.classList.remove("active"));
    if (navEl) navEl.classList.add("active");
    $("#view-cpg").classList.toggle("hidden", view !== "cpg");
    $("#view-pairs").classList.toggle("hidden", view !== "pairs");
    if (view === "pairs") {
      $("#pairs-search").focus();
    }
  }

  $("#pairs-search-btn").addEventListener("click", searchPairs);
  $("#pairs-search").addEventListener("keydown", (e) => { if (e.key === "Enter") searchPairs(); });

  async function searchPairs() {
    const q = $("#pairs-search").value.trim();
    if (!q) { $("#pairs-empty").classList.remove("hidden"); $("#pairs-results").classList.add("hidden"); return; }
    let j;
    try { j = await api("/api/pairs/search", { method: "POST", body: { query: q } }); }
    catch (e) { alert(e.message); return; }

    const body = $("#pairs-body");
    body.innerHTML = "";
    if (!j.results || j.results.length === 0) {
      body.innerHTML = `<tr><td colspan="6" class="text-center py-10" style="color: #6b7480;">No blacklisted pairs match “${escapeHtml(q)}”.</td></tr>`;
    } else {
      j.results.forEach(p => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="font-mono">${escapeHtml(p.upc)}</td>
          <td class="font-mono">${escapeHtml(p.asin)}</td>
          <td>${p.confidence_score == null ? '—' : fmtConfidence(p.confidence_score)}</td>
          <td class="text-xs">${escapeHtml(p.failed_signals || '')}</td>
          <td class="text-xs" style="color: #6b7480;">${escapeHtml(p.date_blacklisted || '')}</td>
          <td class="text-right">
            <button class="row-action-btn unlock" data-upc="${escapeHtml(p.upc)}" data-asin="${escapeHtml(p.asin)}">Unlock Pair</button>
          </td>`;
        body.appendChild(tr);
      });
      body.querySelectorAll(".row-action-btn.unlock").forEach(btn =>
        btn.addEventListener("click", async (e) => {
          await api("/api/pairs/unlock", { method: "POST", body: {
            upc: e.currentTarget.dataset.upc, asin: e.currentTarget.dataset.asin,
          }});
          searchPairs();
        }));
    }
    $("#pairs-empty").classList.add("hidden");
    $("#pairs-results").classList.remove("hidden");
  }

  // ---------- Sample loader -----------------------------------------------
  async function loadSample() {
    try {
      const [c, a] = await Promise.all([
        fetch("/static/sample/sample_catalog.xlsx"),
        fetch("/static/sample/sample_amazon_keepa.xlsx"),
      ]);
      const catalogBlob = await c.blob();
      const amazonBlob  = await a.blob();
      state.catalogFile = new File([catalogBlob], "sample_catalog.xlsx", { type: catalogBlob.type });
      state.amazonFile  = new File([amazonBlob],  "sample_amazon_keepa.xlsx", { type: amazonBlob.type });
      state.amazonSource = "keepa";
      $$("#amazon-source-toggle button").forEach(b => b.classList.toggle("active", b.dataset.source === "keepa"));
      [["#catalog-drop", state.catalogFile], ["#amazon-drop", state.amazonFile]].forEach(([sel, f]) => {
        const el = $(sel);
        el.classList.add("loaded");
        el.querySelector(".text-sm").innerHTML = `<span class="file-name">${escapeHtml(f.name)}</span>`;
        el.querySelector(".text-xs").innerHTML = `${(f.size / 1024).toFixed(1)} KB — click to replace`;
      });
      updateStatusHint();
    } catch (err) {
      alert("Could not load sample data: " + err.message);
    }
  }

  // ---------- Boot --------------------------------------------------------
  (async function boot() {
    await Promise.all([loadLibrary(), loadThresholds(), apiHealth()]);
    await loadSample();
    setTimeout(() => {
      if (state.results.length === 0 && state.catalogFile && state.amazonFile) {
        runVerification();
      }
    }, 400);
  })();

})();
