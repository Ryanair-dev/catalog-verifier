/* ============================================================================
   Catalog Verifier — frontend logic (v3).

   v3 is scan-centric:
     • Verification History is the default view; every run lives as a "scan".
     • Catalog import is a 4-step wizard (upload → starting row → mapping → detail).
     • Scan detail view shows pending/ready banners, runs verification,
       shows results, and drives review + export.
     • Abbreviation Library is a slide-out panel with category tabs,
       duplicate-detection on add, paginated search, and inline delete.
     • AI auto-learns new abbreviations — a toast stack surfaces each add.
   ========================================================================== */

(() => {
  "use strict";

  // ---------- Query helpers -----------------------------------------------
  const $  = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => root.querySelectorAll(s);

  // ---------- App state ---------------------------------------------------
  const state = {
    // history view
    scans: [],
    scanStatsCache: null,

    // active scan detail
    scan: null,
    results: [],
    thresholds: { verified: 85, review: 35 },

    // wizard
    wizard: {
      step: 1,
      file: null,
      preview: null,     // { headers, preview[], total_rows, filename, size }
      dataStart: null,   // row index in rows[] (0-based)
      mapping: {
        brand: "",
        upc_col: "",
        item_id_col: "",
        title_col: "",
        asin_col: "",
        attr_cols: [],
      },
      name: "",
      ai_mode: false,
    },

    // amazon-attach modal
    amazon: {
      scanId: null,
      source: "keepa",
      file: null,
    },

    // delete modal
    deleteScanId: null,

    // library view
    library: {},
    categories: [],
    activeCategory: null,
    abbrSearch: "",
    abbrPage: 1,
    abbrDeleteCandidate: null, // {id}

    // misc
    reviewTab: "Approved",
    currentView: "cpg",       // "cpg" | "history" | "scan" | "pairs"
    aiMode: false,
    resultsPage: 1,

    // CPG (classic) flow
    cpg: {
      catalogFile: null,
      catalogMapping: null,       // { brand, upc_col, item_id_col, title_col, asin_col, attr_cols[], data_start }
      catalogReady: false,        // true once the wizard has applied a mapping
      scanName: "",
      amazonFile: null,
      amazonSource: "keepa",
      aiMode: false,
    },
  };

  const PAGE_SIZE = 20;

  // ---------- Utilities ----------------------------------------------------
  const fmtConfidence = (n) => (n == null ? "—" : `${Number(n).toFixed(0)}%`);
  const verdictClass = (v) =>
      v === "Approved" ? "row-verified"
    : v === "Review"   ? "row-review"
    : (v === "Not Approved" || v === "Not Verified") ? "row-not" : "";
  const badgeClass = (v) =>
      v === "Approved" ? "badge-verified"
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

  function rowKey(r) { return `${r.UPC || ""}::${r.ASIN || ""}::${r.ItemID || ""}`; }

  // ---------- Custom dropdown (wraps a native <select>) -------------------
  // Visual replacement that animates open + uses brand colours. The native
  // <select> stays in the DOM as the value source (so existing `.value` /
  // `addEventListener("input"|"change")` code elsewhere keeps working).
  //
  // After populating options on a wrapped <select>, call rebuildCustomMenu(sel)
  // so the menu picks up the new options.
  const CS_CARET_SVG = '<svg class="cs-caret" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 4.5 6 7.5 9 4.5"/></svg>';
  const CS_CHECK_SVG = '<svg class="cs-check" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="2.5 6.5 5 9 9.5 3.5"/></svg>';

  let _csOpen = null;  // currently-open dropdown wrap
  function _csCloseOpen() {
    if (!_csOpen) return;
    const wrap = _csOpen;
    const menu = wrap.querySelector(".cs-menu");
    if (menu) {
      menu.classList.add("cs-closing");
      setTimeout(() => { if (menu.parentNode === wrap) menu.remove(); }, 110);
    }
    wrap.classList.remove("open");
    _csOpen = null;
  }
  document.addEventListener("mousedown", (e) => {
    if (_csOpen && !_csOpen.contains(e.target)) _csCloseOpen();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && _csOpen) _csCloseOpen();
  });

  function rebuildCustomMenu(selectEl) {
    const wrap = selectEl.closest(".cs-wrap");
    if (!wrap) return;
    const trigger = wrap.querySelector(".cs-trigger");
    if (!trigger) return;
    const label = trigger.querySelector(".cs-label");
    const selOpt = selectEl.options[selectEl.selectedIndex];
    if (label) label.textContent = selOpt ? selOpt.textContent : "";
    // If the menu is open, refresh it in place.
    const openMenu = wrap.querySelector(".cs-menu");
    if (openMenu) {
      openMenu.innerHTML = _csBuildOptionsHTML(selectEl);
      _csWireOptionClicks(selectEl, wrap, openMenu);
    }
  }

  function _csBuildOptionsHTML(selectEl) {
    const curVal = selectEl.value;
    const iconFor = selectEl._csIconFor;
    return Array.from(selectEl.options).map(o => {
      const icon = typeof iconFor === "function"
        ? (iconFor(o.value, o.textContent) || "")
        : "";
      return `<div class="cs-option${o.value === curVal ? " cs-selected" : ""}" data-value="${escapeHtml(o.value)}">
        ${icon}
        <span class="cs-option-label">${escapeHtml(o.textContent)}</span>
        ${CS_CHECK_SVG}
      </div>`;
    }).join("");
  }

  function _csWireOptionClicks(selectEl, wrap, menu) {
    menu.querySelectorAll(".cs-option").forEach(opt => {
      opt.addEventListener("mousedown", (e) => {
        // mousedown (not click) so we beat the outer mousedown-to-close.
        e.preventDefault();
        e.stopPropagation();
        const v = opt.dataset.value;
        if (selectEl.value !== v) {
          selectEl.value = v;
          selectEl.dispatchEvent(new Event("input",  { bubbles: true }));
          selectEl.dispatchEvent(new Event("change", { bubbles: true }));
        }
        rebuildCustomMenu(selectEl);
        _csCloseOpen();
      });
    });
  }

  function enhanceSelect(selectEl, opts = {}) {
    if (!selectEl || selectEl.dataset.enhanced === "1") return;
    selectEl.dataset.enhanced = "1";
    // Stash the optional per-option icon renderer on the element itself so
    // _csBuildOptionsHTML can read it without closure juggling.
    if (typeof opts.iconFor === "function") selectEl._csIconFor = opts.iconFor;
    const wrap = document.createElement("div");
    wrap.className = "cs-wrap" + (opts.block ? " cs-block" : "");
    // Preserve inline width if the select had one, so layouts don't shift.
    if (selectEl.style.width) wrap.style.width = selectEl.style.width;
    if (selectEl.style.minWidth) wrap.style.minWidth = selectEl.style.minWidth;
    selectEl.parentNode.insertBefore(wrap, selectEl);
    wrap.appendChild(selectEl);
    selectEl.classList.add("cs-native");

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "cs-trigger";
    const selOpt = selectEl.options[selectEl.selectedIndex];
    trigger.innerHTML = `
      <span class="cs-label">${escapeHtml(selOpt ? selOpt.textContent : "")}</span>
      ${CS_CARET_SVG}
    `;
    wrap.appendChild(trigger);

    trigger.addEventListener("mousedown", (e) => {
      e.preventDefault();  // keep focus behavior snappy, no native focus ring flash
      e.stopPropagation();
      if (_csOpen === wrap) { _csCloseOpen(); return; }
      if (_csOpen) _csCloseOpen();
      const menu = document.createElement("div");
      menu.className = "cs-menu";
      if (opts.alignRight) menu.classList.add("cs-align-right");
      menu.innerHTML = _csBuildOptionsHTML(selectEl);
      wrap.appendChild(menu);
      wrap.classList.add("open");
      _csOpen = wrap;
      _csWireOptionClicks(selectEl, wrap, menu);
      trigger.focus({ preventScroll: true });
    });

    // Keep the trigger label in sync when code mutates the native .value
    // externally (e.g. the jump-to-category handler).
    selectEl.addEventListener("change", () => rebuildCustomMenu(selectEl));
  }

  // Semantic dot renderers for the custom dropdown. Each returns an HTML
  // string (a <span class="cs-dot cs-dot-<color>"></span>) or empty string.
  const _csDot = (color) => `<span class="cs-dot cs-dot-${color}"></span>`;

  const verdictIconFor = (value) => {
    const v = String(value || "").toLowerCase();
    if (v === "all")                            return _csDot("grey");
    if (v === "approved" || v === "verified")   return _csDot("green");
    if (v === "review")                         return _csDot("yellow");
    if (v === "not approved" || v === "not verified") return _csDot("red");
    if (v === "duplicate" || v === "duplicate flagged") return _csDot("orange");
    return _csDot("grey");
  };

  const sortIconFor = (value) => {
    // Small neutral dot — the options already have direction arrows in their
    // label (e.g. "Confidence ↓"). Keep the row rhythm consistent with the
    // verdict dropdown's dot size.
    const v = String(value || "").toLowerCase();
    if (v.includes("conf")) return _csDot("purple");
    if (v.includes("brand")) return _csDot("blue");
    if (v.includes("verdict")) return _csDot("teal");
    return _csDot("grey");
  };

  // Each library category gets its own accent dot so the dropdown reads as a
  // palette. Keys match the server-defined VALID_CATEGORIES.
  const _CAT_COLOR = {
    "Colors":             "pink",
    "Sizes":              "blue",
    "UOMs":               "teal",
    "Forms":              "purple",
    "Sterility":          "green",
    "Materials":          "amber",
    "Scents":             "orange",
    "Flavors":            "red",
    "Packaging":          "yellow",
    "Product Attributes": "grey",
  };
  const categoryIconFor = (value) => _csDot(_CAT_COLOR[value] || "grey");

  const aiModelIconFor = (value) => {
    const v = String(value || "").toLowerCase();
    if (v.includes("mini"))  return _csDot("green");
    if (v.includes("4o"))    return _csDot("purple");
    return _csDot("grey");
  };

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

  function fmtKB(bytes) {
    if (bytes == null) return "";
    return bytes > 1024 * 1024
      ? `${(bytes / 1024 / 1024).toFixed(2)} MB`
      : `${(bytes / 1024).toFixed(1)} KB`;
  }

  function fmtDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso.replace(" ", "T") + "Z");
    if (isNaN(d)) return iso;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })
         + " · " + d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  }

  // User-facing status labels. Internal lifecycle has more states
  // (pending/ready/verifying), but Verification History only shows runs —
  // so only the two "done" buckets need human labels.
  const STATUS_LABEL = {
    verified_unreviewed: "To be reviewed",
    verified_partial:    "To be reviewed",
    verified_complete:   "Reviewed",
    verified_reviewed:   "Reviewed",
  };
  function statusBucket(status) {
    if (status === "verified_reviewed" || status === "verified_complete") return "reviewed";
    if (String(status).startsWith("verified")) return "to-review";
    return null;   // pending/ready/verifying — hidden from history
  }

  // ---------- API ----------------------------------------------------------
  async function api(path, { method = "GET", body = null, form = false } = {}) {
    const opts = { method };
    if (form) {
      opts.body = body;
    } else if (body !== null) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetch(path, opts);
    } catch (_e) {
      // Stale keep-alive connection (e.g. after server restart) — retry once.
      await new Promise(r => setTimeout(r, 400));
      resp = await fetch(path, opts);
    }
    if (!resp.ok) {
      let msg = `${resp.status} ${resp.statusText}`;
      try {
        const j = await resp.json();
        if (Array.isArray(j.detail)) {
          msg = j.detail.map(e => `${(e.loc||[]).slice(1).join('.')}: ${e.msg}`).join(' | ');
        } else if (j.detail) {
          msg = String(j.detail);
        }
      } catch {}
      throw new Error(msg);
    }
    const ct = resp.headers.get("content-type") || "";
    return ct.includes("application/json") ? resp.json() : resp.blob();
  }

  async function apiHealth() {
    try {
      const j = await api("/api/health");
      $("#api-status").textContent = j.ai_available
        ? "API ready · AI enabled" : "API ready";
    } catch {
      $("#api-status").textContent = "API offline";
    }
  }

  // ========================================================================
  //  Sidebar navigation
  // ========================================================================
  $$(".sidebar .nav-item").forEach(n => {
    if (n.dataset.view) {
      n.addEventListener("click", () => switchView(n.dataset.view, n));
    }
  });
  function switchView(view, navEl) {
    state.currentView = view;
    $$(".sidebar .nav-item[data-view]").forEach(n => n.classList.remove("active"));
    if (navEl) navEl.classList.add("active");
    else {
      const el = $(`.sidebar .nav-item[data-view='${view}']`);
      if (el) el.classList.add("active");
    }
    $("#view-cpg").classList.toggle("hidden",     view !== "cpg");
    $("#view-history").classList.toggle("hidden", view !== "history");
    $("#view-scan").classList.toggle("hidden",    view !== "scan");
    $("#view-pairs").classList.toggle("hidden",   view !== "pairs");
    const va = $("#view-analytics"); if (va) va.classList.toggle("hidden", view !== "analytics");
    // Detail view is only shown via openAnalyticsRunDetail — sidebar always
    // lands you on the list, never the detail pane.
    const vr = $("#view-analytics-run"); if (vr) vr.classList.add("hidden");
    if (view === "pairs") $("#pairs-search").focus();
    if (view === "history") loadHistory();
    if (view === "analytics") loadAnalyticsRuns();
    // Tear down detail-view poll when leaving Analytics.
    if (view !== "analytics" && typeof _clearAnalyticsRunPoll === "function") {
      try { _clearAnalyticsRunPoll(); } catch {}
    }
  }

  // View-aware DOM id helper: pick the right set of IDs for the current view.
  // CPG uses bare IDs (#sum-total, #results-body, …).
  // Scan-detail uses prefixed IDs (#scan-sum-total, #scan-results-body, …).
  function rid(base) {
    return state.currentView === "scan" ? `#scan-${base}` : `#${base}`;
  }

  // ========================================================================
  //  Verification History view
  // ========================================================================
  async function loadHistory() {
    try {
      const j = await api("/api/scans");
      // History only shows completed runs — pending/ready/verifying scans
      // are transient and never surface to the user.
      state.scans = (j.scans || []).filter(s => String(s.status).startsWith("verified"));
      renderHistory();
    } catch (e) {
      console.error("loadHistory", e);
    }
  }

  function renderHistory() {
    const body  = $("#history-body");
    const wrap  = $("#history-table-wrap");
    const empty = $("#history-empty");

    // Stats strip — Total / To be reviewed / Reviewed / (30-day scans).
    const total = state.scans.length;
    const toReview = state.scans.filter(s => statusBucket(s.status) === "to-review").length;
    const reviewed = state.scans.filter(s => statusBucket(s.status) === "reviewed").length;
    const recent = state.scans.filter(s => {
      const d = new Date((s.created_at || "").replace(" ", "T") + "Z");
      return !isNaN(d) && (Date.now() - d.getTime()) < 30 * 24 * 3600 * 1000;
    }).length;
    $("#hs-total").textContent   = total;
    $("#hs-pending").textContent = toReview;
    $("#hs-ready").textContent   = recent;
    $("#hs-done").textContent    = reviewed;

    if (total === 0) {
      wrap.classList.add("hidden");
      empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden");
    wrap.classList.remove("hidden");

    body.innerHTML = state.scans.map(s => {
      const bucket = statusBucket(s.status) || "to-review";
      const statusClass = bucket === "reviewed" ? "status-reviewed" : "status-to-review";
      const statusLabel = STATUS_LABEL[s.status] || (bucket === "reviewed" ? "Reviewed" : "To be reviewed");

      const actions = `
        <div class="history-actions">
          <button class="act-open" data-scan-id="${s.id}" data-scan-action="open" title="Open scan">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 3h6v6"/><path d="M10 14L21 3"/><path d="M21 14v7H3V3h7"/></svg>
            Open
          </button>
          <button class="act-export" data-scan-id="${s.id}" data-scan-action="export" title="Export to Excel">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M20 21H4"/></svg>
            Export
          </button>
          <button class="act-delete" data-scan-id="${s.id}" data-scan-action="delete" title="Delete">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>
          </button>
        </div>`;

      return `
        <tr data-scan-id="${s.id}">
          <td>
            <div class="scan-name-cell">${escapeHtml(s.name)}</div>
            <div class="scan-sub">${escapeHtml(s.catalog_filename || "")}${s.ai_mode ? ' · <span style="color: var(--teal-600);">AI mode</span>' : ''}</div>
          </td>
          <td>${fmtDate(s.created_at)}</td>
          <td>${escapeHtml(s.marketplace || "US")}</td>
          <td class="num">${s.catalog_count || 0}</td>
          <td class="num" style="color: var(--green-600); font-weight: 600;">${s.verified_count || 0}</td>
          <td class="num" style="color: var(--yellow-600); font-weight: 600;">${s.review_count || 0}</td>
          <td class="num" style="color: var(--red-500); font-weight: 600;">${s.not_approved_count || 0}</td>
          <td><span class="status-badge ${statusClass}">${statusLabel}</span></td>
          <td class="text-right">${actions}</td>
        </tr>`;
    }).join("");

    // wire actions
    body.querySelectorAll("[data-scan-action]").forEach(btn => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const id = Number(btn.dataset.scanId);
        const action = btn.dataset.scanAction;
        if (action === "open")   openScan(id);
        if (action === "export") exportScanDirect(id);
        if (action === "delete") askDeleteScan(id);
      });
    });

    // row click → open
    body.querySelectorAll("tr[data-scan-id]").forEach(tr => {
      tr.addEventListener("click", () => {
        openScan(Number(tr.dataset.scanId));
      });
    });
  }

  $("#history-refresh-btn").addEventListener("click", loadHistory);
  // History is a read-only log in the new flow — the button now jumps to CPG
  // where the user imports a catalog + Amazon data and clicks Run Verification.
  $("#new-scan-btn").addEventListener("click", () => switchView("cpg"));

  // ========================================================================
  //  Scan Detail view
  // ========================================================================
  async function openScan(scanId) {
    try {
      const j = await api(`/api/scans/${scanId}`);
      state.scan = j.scan;
      state.results = (j.results || []).map(r => ({ ...r, barcode_db: r.barcode_db || null, barcode_loading: false }));
      if (j.thresholds) state.thresholds = j.thresholds;
      // switchView MUST run before renderScanView() so rid() resolves to the
      // scan-detail prefixed IDs (#scan-results-body, …) instead of the CPG
      // view's DOM. Previously this was inverted and the data was written
      // into the hidden CPG view — the scan-detail view appeared empty.
      switchView("scan", null);
      renderScanView();
    } catch (e) {
      alert("Could not open scan: " + e.message);
    }
  }

  function renderScanView() {
    const s = state.scan;
    if (!s) return;

    $("#scan-name").textContent = s.name || "Scan";
    const subparts = [
      `${s.catalog_count || 0} products`,
      s.marketplace,
      s.condition,
      fmtDate(s.created_at),
    ].filter(Boolean);
    $("#scan-meta").textContent = subparts.join(" · ");
    $("#scan-ai-badge").classList.toggle("hidden", !s.ai_mode);

    // Banners
    $("#scan-pending-banner").classList.toggle("hidden", s.status !== "pending");
    $("#scan-ready-banner").classList.toggle("hidden",   s.status !== "ready");

    // Progress section
    $("#scan-progress-section").classList.toggle("hidden", s.status !== "verifying");

    // Summary + results
    const hasResults = state.results.length > 0 && String(s.status).startsWith("verified");
    $("#scan-summary-section").classList.toggle("hidden", !hasResults);
    $("#scan-results-section").classList.toggle("hidden", !hasResults);
    if (hasResults) {
      renderSummary();
      renderResults();
    }

    $("#scan-review-btn").disabled = !hasResults;
    $("#scan-export-btn").disabled = !hasResults;
    const scanAiBtn = $("#scan-ai-recheck-btn");
    scanAiBtn.disabled = !hasResults;
    scanAiBtn.classList.toggle("hidden", !hasResults);
  }

  $("#scan-back-btn").addEventListener("click", () => {
    state.scan = null;
    state.results = [];
    switchView("history", $(".sidebar .nav-item[data-view='history']"));
  });

  $("#scan-add-amazon-btn").addEventListener("click", () => {
    if (state.scan) openAmazonModal(state.scan.id);
  });

  $("#scan-run-btn").addEventListener("click", runVerification);

  async function runVerification() {
    if (!state.scan) return;
    const s = state.scan;
    state.scan = { ...s, status: "verifying" };
    $("#scan-ready-banner").classList.add("hidden");
    $("#scan-progress-section").classList.remove("hidden");
    updateProgress(0, s.catalog_count || 1, "Scoring products…");
    startEta(s.catalog_count || 1, s.ai_mode ? "ai" : "rule");

    try {
      const j = await api(`/api/scans/${s.id}/verify`, { method: "POST" });
      state.scan = j.scan;
      state.results = (j.results || []).map(r => ({ ...r, barcode_db: null, barcode_loading: true }));
      if (j.thresholds) state.thresholds = j.thresholds;

      // Toast AI auto-added abbreviations
      if (Array.isArray(j.ai_added)) {
        j.ai_added.forEach(a => showToast(a));
      }

      state.resultsPage = 1;
      renderScanView();
      renderSummary();
      renderResults();

      // Barcode chain — 6 workers with rAF-throttled rendering.
      // See the CPG flow for the rationale. Short version: 40 rows × 20 cols
      // × 20 re-renders = visible stutter; rAF coalescing drops it to ~15fps.
      const total = state.results.length;
      updateProgress(0, total, `Looking up ${total} barcodes…`);
      startEta(Math.ceil(total / 6), "barcode"); // 6 workers → effective per-row cost
      let done = 0;
      let renderPending = false;
      const scheduleRender = () => {
        if (renderPending) return;
        renderPending = true;
        requestAnimationFrame(() => { renderPending = false; renderResults(); });
      };
      const queue = [...state.results.keys()];
      const workers = new Array(6).fill(0).map(async () => {
        while (queue.length) {
          const idx = queue.shift();
          await lookupRowBarcode(state.results[idx]);
          done += 1;
          updateProgress(done, total, `Verifying ${done} of ${total} products…`);
          scheduleRender();
        }
      });
      await Promise.all(workers);
      renderResults();
      stopEta();
      $("#scan-progress-section").classList.add("hidden");
    } catch (err) {
      stopEta();
      alert("Verification failed: " + err.message);
      $("#scan-progress-section").classList.add("hidden");
      openScan(s.id); // reload
    }
  }

  async function lookupRowBarcode(row) {
    row.barcode_loading = true;
    if (!row.UPC) { row.barcode_db = { found: false, reason: "No UPC" }; row.barcode_loading = false; return; }
    try {
      row.barcode_db = await api("/api/barcode/lookup", {
        method: "POST",
        body: { upc: String(row.UPC), brand: row.Brand || "", vendor_title: row.Title || "" },
      });
    } catch {
      row.barcode_db = { found: false, reason: "Lookup error" };
    } finally {
      row.barcode_loading = false;
    }
  }

  // --- ETA estimator ---------------------------------------------------
  //
  // The backend verify endpoint is synchronous — we can't poll it mid-flight
  // for true progress. Instead we start an ETA timer at kickoff based on the
  // catalog size × a baseline per-row time (rule mode ≈ 25ms, AI mode much
  // slower — OpenAI averages ~800ms per call). The countdown ticks every
  // 500ms so the user sees time actually moving, and stopEta() clears it
  // when the verify request resolves.
  const ETA_PER_ROW_MS = { rule: 25, ai: 800, barcode: 180 };
  state.eta = { timer: null, startedAt: 0, totalMs: 0 };

  function startEta(totalRows, phase /* "rule" | "ai" | "barcode" */) {
    stopEta();
    const perRow = ETA_PER_ROW_MS[phase] || ETA_PER_ROW_MS.rule;
    state.eta.startedAt = Date.now();
    state.eta.totalMs   = Math.max(1500, totalRows * perRow);
    tickEta();
    state.eta.timer = setInterval(tickEta, 500);
  }
  function stopEta() {
    if (state.eta.timer) { clearInterval(state.eta.timer); state.eta.timer = null; }
    const el = $(rid("progress-eta"));
    if (el) el.textContent = "";
  }
  function tickEta() {
    const el = $(rid("progress-eta"));
    if (!el) return;
    const elapsed = Date.now() - state.eta.startedAt;
    const remaining = Math.max(0, state.eta.totalMs - elapsed);
    el.textContent = remaining > 0 ? `~${fmtEta(remaining)} remaining` : "finishing up…";
  }
  function fmtEta(ms) {
    const s = Math.ceil(ms / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60), sec = s % 60;
    return `${m}m ${String(sec).padStart(2, "0")}s`;
  }

  function updateProgress(done, total, label) {
    const pct = total ? (done / total) * 100 : 0;
    const bar = $(rid("progress-bar"));
    if (bar) bar.style.width = `${pct}%`;
    const cnt = $(rid("progress-count"));
    if (cnt) cnt.textContent = `${done} of ${total}`;
    if (label) {
      const lbl = $(rid("progress-label"));
      if (lbl) lbl.textContent = label;
    }
  }

  // ---------- Summary ------------------------------------------------------
  function renderSummary() {
    const total = state.results.length;
    const v = state.results.filter(r => r.Verdict === "Approved").length;
    const rev = state.results.filter(r => r.Verdict === "Review").length;
    const n = state.results.filter(r => r.Verdict === "Not Approved" || r.Verdict === "Not Verified").length;
    const elT = $(rid("sum-total"));    if (elT) elT.textContent = total;
    const elV = $(rid("sum-verified")); if (elV) elV.textContent = v;
    const elR = $(rid("sum-review"));   if (elR) elR.textContent = rev;
    const elN = $(rid("sum-not"));      if (elN) elN.textContent = n;
  }

  // ---------- Results table -----------------------------------------------
  // Wire BOTH sets of filter/sort/search inputs — CPG view + scan view.
  ["#verdict-filter", "#sort-by", "#search",
   "#scan-verdict-filter", "#scan-sort-by", "#scan-search"
  ].forEach(sel => {
    const el = $(sel);
    if (el) el.addEventListener("input", () => renderResults(true));
  });

  const RESULTS_PAGE_SIZE = 50;

  function buildReason(row) {
    const s = row.signals || {};
    const parts = [];
    if (!row.ASIN) return "No ASIN in catalog";
    if (s.upc && s.upc.detail === "No Amazon row found") return "ASIN not in Amazon file";
    if (s.upc && s.upc.detail === "No UPC in Amazon record") parts.push("No UPC in Amazon export — scored on title");
    else if (s.upc && !s.upc.matched) parts.push("UPC mismatch");
    if (s.title && s.title.score != null && s.title.score < 60) parts.push(`Low title similarity (${Math.round(s.title.score)}%)`);
    if (s.brand && !s.brand.matched) parts.push(`Brand mismatch`);
    if (row.duplicate) parts.push("Duplicate Item ID");
    if (row.notes) parts.push(row.notes);
    return parts.length ? parts.join(" · ") : (row.Verdict === "Approved" ? "" : "Low confidence score");
  }

  // Render the "Vendor Title (expanded)" cell. Backend returns null when the
  // expanded form matches the original — in that case we dim an em-dash so
  // the column still aligns but doesn't compete with real content.
  function buildExpandedTitleCell(row) {
    const exp = row.TitleExpanded;
    if (!exp) return '<div class="text-xs" style="color: #cbd5e1;">—</div>';
    return `<div class="text-sm" style="color: var(--purple-800); font-weight: 500;">${escapeHtml(exp)}</div>`;
  }

  // Render the AI suggestion cell (or empty if this row hasn't been re-checked).
  // Shows the suggested verdict as a badge + one-line reason + an Accept button
  // when the suggestion disagrees with the current verdict.
  function buildAiSuggestionCell(row, rowIdx) {
    if (!row.ai_suggestion) return '<span class="text-xs" style="color: #cbd5e1;">—</span>';
    const suggest = row.ai_suggestion;
    const current = row.Verdict || "";
    const agrees  = suggest === "keep"
                 || suggest.toLowerCase() === current.toLowerCase()
                 || (suggest === "Approved" && current.toLowerCase() === "verified");
    const cls = suggest === "Approved"     ? "badge-verified"
              : suggest === "Review"       ? "badge-review"
              : suggest === "Not Approved" ? "badge-notverified"
              : "badge-duplicate";
    const label = suggest === "keep" ? "Keep" : suggest;
    const reason = escapeHtml(row.ai_reason || "");
    const acceptBtn = agrees
      ? ""
      : `<button class="row-action-btn ai-accept" data-row-idx="${rowIdx}" title="Apply this suggestion">Accept</button>`;
    return `
      <div class="flex items-start gap-1">
        <div class="flex-1 min-w-0">
          <span class="badge ${cls}" title="${reason}">${label}</span>
          ${reason ? `<div class="text-[11px] mt-0.5" style="color:#6b7480;white-space:normal;">${reason}</div>` : ""}
        </div>
        ${acceptBtn}
      </div>`;
  }

  async function acceptAiSuggestion(rowIdx) {
    const row = state.results[rowIdx];
    if (!row || !row.ai_suggestion || row.ai_suggestion === "keep") return;
    const newVerdict = row.ai_suggestion;
    try {
      await api(`/api/scans/${state.scan.id}/rows/${rowIdx}`, {
        method: "POST",
        body: {
          verdict: newVerdict,
          review_status: "ai-accepted",
          data: { ...row, original_verdict: row.original_verdict || row.Verdict, Verdict: newVerdict, review_status: "ai-accepted" },
        },
      });
      row.original_verdict = row.original_verdict || row.Verdict;
      row.Verdict = newVerdict;
      row.review_status = "ai-accepted";
      renderSummary();
      renderResults();
    } catch (e) {
      alert("Could not apply suggestion: " + e.message);
    }
  }

  function renderResults(resetPage) {
    const vEl = $(rid("verdict-filter"));
    const sEl = $(rid("sort-by"));
    const qEl = $(rid("search"));
    const filter = vEl ? vEl.value : "all";
    const sort   = sEl ? sEl.value : "conf-desc";
    const query  = qEl ? qEl.value.toLowerCase().trim() : "";

    let rows = [...state.results];
    if (filter === "Duplicate") rows = rows.filter(r => r.duplicate);
    else if (filter !== "all")  rows = rows.filter(r => r.Verdict === filter);

    if (query) {
      rows = rows.filter(r =>
        [r.Title, r.Brand, r.ASIN, r.ItemID, r.UPC]
          .map(v => String(v || "").toLowerCase()).some(v => v.includes(query)));
    }

    const sortFns = {
      "conf-desc": (a, b) => b.Confidence - a.Confidence,
      "conf-asc":  (a, b) => a.Confidence - b.Confidence,
      "verdict":   (a, b) => String(a.Verdict).localeCompare(String(b.Verdict)),
      "brand":     (a, b) => String(a.Brand || "").localeCompare(String(b.Brand || "")),
    };
    rows.sort(sortFns[sort] || sortFns["conf-desc"]);

    const body = $(rid("results-body"));
    if (!body) return;

    // CPG view: show the results table (hide empty state) when we have rows.
    if (state.currentView === "cpg") {
      const hasAny = state.results.length > 0;
      const empty = $("#empty-state");
      const wrap  = $("#results-table-wrap");
      if (empty) empty.classList.toggle("hidden", hasAny);
      if (wrap)  wrap.classList.toggle("hidden", !hasAny);
    }

    const total = rows.length;
    const totalPages = Math.max(1, Math.ceil(total / RESULTS_PAGE_SIZE));

    // Reset to page 1 whenever filter/sort/search changes, or explicitly requested.
    if (resetPage) state.resultsPage = 1;
    state.resultsPage = Math.min(Math.max(1, state.resultsPage), totalPages);

    const start = (state.resultsPage - 1) * RESULTS_PAGE_SIZE;
    const pageRows = rows.slice(start, start + RESULTS_PAGE_SIZE);

    body.innerHTML = "";
    for (const row of pageRows) {
      const tr = document.createElement("tr");
      tr.className = "row-anim " + verdictClass(row.Verdict) + (row.duplicate ? " row-duplicate" : "");

      const s = row.signals || {};
      const b = row.barcode_db;
      const barcodeHTML = row.barcode_loading
        ? `<span class="mini-spinner"></span>`
        : b == null ? ""
          : b.found
            ? `<div class="text-xs"><div><b>${escapeHtml(b.source || "")}</b></div><div>${escapeHtml(b.name || "")}</div>${b.aligned === false ? '<div class="text-[11px]" style="color: var(--yellow-500)">✱ not aligned</div>' : ''}</div>`
            : `<span class="text-xs" style="color: #6b7480;">${escapeHtml(b.reason || 'Not Found')}</span>`;

      const statusTag = row.review_status
        ? `<span class="badge ${reviewStatusClass(row.review_status)}">${row.review_status}</span>`
        : '<span class="text-xs" style="color: #6b7480;">—</span>';

      const verdictBadge = row.review_status
        ? `<span class="badge ${badgeClass(row.original_verdict)} strikethrough">${row.original_verdict}</span>
           <span class="badge ${badgeClass(row.Verdict)} ml-1">${row.Verdict}</span>`
        : `<span class="badge ${badgeClass(row.Verdict)}">${row.Verdict}</span>`;

      const confBarClass = row.Verdict === "Approved" ? "verified"
                         : row.Verdict === "Review"   ? "review" : "not";

      const reasonText = buildReason(row);
      const realIdx = state.results.indexOf(row);
      tr.innerHTML = `
        <td>${escapeHtml(row.UPC)}</td>
        <td>${escapeHtml(row.ItemID)}${row.duplicate ? '<div><span class="badge badge-duplicate">Duplicate</span></div>' : ''}</td>
        <td style="max-width: 280px;">
          <div class="text-sm" style="color: var(--navy-800); font-weight: 500;">${escapeHtml(row.Title)}</div>
        </td>
        <td style="max-width: 280px;">${buildExpandedTitleCell(row)}</td>
        <td style="max-width: 280px;">
          <div class="text-sm" style="color: #6b7480;">${escapeHtml(row.AmzTitle || '—')}</div>
        </td>
        <td>${escapeHtml(row.Brand)}</td>
        <td class="font-mono text-xs">${escapeHtml(row.ASIN)}</td>
        <td>
          <div class="signal-score ${signalTone({score: row.Confidence})}">${fmtConfidence(row.Confidence)}</div>
          <div class="confidence-bar"><div class="fill ${confBarClass}" style="width: ${Math.max(3, row.Confidence)}%"></div></div>
        </td>
        <td>${verdictBadge}</td>
        <td>${statusTag}</td>
        <td style="max-width:220px;">${buildAiSuggestionCell(row, realIdx)}</td>
        <td class="text-xs" style="max-width:200px;color:#6b7480;">${escapeHtml(reasonText)}</td>
        ${signalCell(s.upc)}${signalCell(s.item_id)}${signalCell(s.brand)}${signalCell(s.title)}${signalCell(s.pack)}
        <td class="text-center">${row.amz_pack == null ? '—' : row.amz_pack}</td>
        <td>${barcodeHTML}</td>
        <td>
          <button class="row-action-btn clear-cache" data-upc="${escapeHtml(String(row.UPC || ''))}" data-asin="${escapeHtml(row.ASIN || '')}">Clear cache</button>
        </td>`;
      body.appendChild(tr);
    }
    body.querySelectorAll(".clear-cache").forEach(b => b.addEventListener("click", handleClearCache));
    body.querySelectorAll(".ai-accept").forEach(b => b.addEventListener("click", e => {
      acceptAiSuggestion(Number(e.currentTarget.dataset.rowIdx));
    }));

    // Pagination bar
    const pagEl = $(rid("results-pagination"));
    if (pagEl) {
      if (totalPages <= 1) {
        pagEl.classList.add("hidden");
        pagEl.style.display = "none";
      } else {
        pagEl.classList.remove("hidden");
        pagEl.style.display = "flex";
        const s2 = start + 1, e2 = Math.min(start + RESULTS_PAGE_SIZE, total);
        pagEl.innerHTML = `
          <span>${s2}–${e2} of ${total} rows</span>
          <span style="display:flex;gap:6px;">
            <button class="row-action-btn" id="${rid("results-pagination").slice(1)}-prev"
              ${state.resultsPage === 1 ? "disabled" : ""}>← Prev</button>
            <span style="padding:4px 8px;">${state.resultsPage} / ${totalPages}</span>
            <button class="row-action-btn" id="${rid("results-pagination").slice(1)}-next"
              ${state.resultsPage === totalPages ? "disabled" : ""}>Next →</button>
          </span>`;
        pagEl.querySelector("[id$='-prev']")?.addEventListener("click", () => {
          state.resultsPage -= 1; renderResults();
        });
        pagEl.querySelector("[id$='-next']")?.addEventListener("click", () => {
          state.resultsPage += 1; renderResults();
        });
      }
    }

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
    const row = state.results.find(r => r.ASIN === asin && String(r.UPC || "") === upc);
    if (!row) return;
    btn.innerHTML = '<span class="mini-spinner"></span>';
    btn.disabled = true;
    try {
      await api("/api/attributes/clear", { method: "POST", body: { upc, asin } });
      await lookupRowBarcode(row);
    } catch (err) {
      console.error(err);
    } finally {
      renderResults();
    }
  }

  // ========================================================================
  //  CPG Catalog Verification (classic flow)
  // ========================================================================
  //
  // Wires the original CPG view — dual upload cards, AI toggle, Amazon source
  // toggle, reset / load sample / run. Submits the full catalog + Amazon file
  // to the legacy /api/verify endpoint, then normalises the response into the
  // v3 result shape so renderResults/renderSummary can render it.

  const cpg = state.cpg;

  const CATALOG_DROP_HTML = `
    <div class="icon-wrap mx-auto mb-3" style="width:44px;height:44px;border-radius:10px;background:#d8f0f1;color:#0a6f71;display:inline-flex;align-items:center;justify-content:center;">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
    </div>
    <div class="text-sm"><span class="font-medium" style="color: var(--navy-800)">Click to upload</span> or drag &amp; drop</div>
    <div class="text-xs mt-1" style="color: #6b7480;">.xlsx file</div>
    <input type="file" id="catalog-input" accept=".xlsx" class="hidden" />`;
  const AMAZON_DROP_HTML = `
    <div class="icon-wrap mx-auto mb-3" style="width:44px;height:44px;border-radius:10px;background:#d8f0f1;color:#0a6f71;display:inline-flex;align-items:center;justify-content:center;">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
    </div>
    <div class="text-sm"><span class="font-medium" style="color: var(--navy-800)">Click to upload</span> or drag &amp; drop</div>
    <div class="text-xs mt-1" style="color: #6b7480;">.xlsx — Keepa or Amazon Seller Central export</div>
    <input type="file" id="amazon-input" accept=".xlsx" class="hidden" />`;

  function wireCpgDrop(dropSel, inputSel, field) {
    const drop = $(dropSel);
    const input = $(inputSel);
    if (!drop || !input) return;
    drop.addEventListener("click", () => input.click());
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
    drop.addEventListener("drop", (e) => {
      e.preventDefault(); drop.classList.remove("drag");
      const f = e.dataTransfer.files[0];
      if (f) setCpgFile(drop, f, field);
    });
    input.addEventListener("change", (e) => {
      const f = e.target.files[0];
      if (f) setCpgFile(drop, f, field);
    });
  }

  function setCpgFile(drop, file, field) {
    cpg[field] = file;
    drop.classList.add("loaded");
    drop.innerHTML = `
      <div class="icon-wrap mx-auto mb-3" style="width:44px;height:44px;border-radius:10px;background:#d8f0f1;color:#0a6f71;display:inline-flex;align-items:center;justify-content:center;">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>
      </div>
      <div class="text-sm font-medium" style="color: var(--navy-800)">${escapeHtml(file.name)}</div>
      <div class="text-xs mt-1" style="color: #6b7480;">${fmtKB(file.size)} · ready</div>`;
    updateCpgStatus();
  }

  // The Vendor Catalog card opens the 4-step import wizard — this is the
  // only way to import a catalog so the column mapping & scan detail are
  // captured correctly. Drag-drop also opens the wizard, pre-loaded with
  // the dropped file.
  function wireCatalogCard() {
    const drop  = $("#catalog-drop");
    const input = $("#catalog-input");
    if (!drop) return;
    drop.addEventListener("click", (e) => {
      // Ignore clicks on the hidden input itself (the browser dispatches one).
      if (e.target && e.target.id === "catalog-input") return;
      openWizard();
    });
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
    drop.addEventListener("drop", (e) => {
      e.preventDefault(); drop.classList.remove("drag");
      const f = e.dataTransfer.files[0];
      if (!f) return;
      openWizard();
      // loadWizardFile is defined below; call it on the next tick so the
      // wizard DOM is rendered before we push the file through it.
      setTimeout(() => loadWizardFile(f), 0);
    });
    if (input) {
      input.addEventListener("change", (e) => {
        const f = e.target.files[0];
        if (!f) return;
        openWizard();
        setTimeout(() => loadWizardFile(f), 0);
      });
    }
  }
  wireCatalogCard();
  wireCpgDrop("#amazon-drop",  "#amazon-input",  "amazonFile");

  // AI toggle
  const aiToggle = $("#ai-toggle");
  if (aiToggle) aiToggle.addEventListener("click", () => {
    cpg.aiMode = !cpg.aiMode;
    aiToggle.classList.toggle("active", cpg.aiMode);
    $("#ai-banner").classList.toggle("hidden", !cpg.aiMode);
  });
  const aiBannerClose = $("#ai-banner-close");
  if (aiBannerClose) aiBannerClose.addEventListener("click", () => {
    cpg.aiMode = false;
    if (aiToggle) aiToggle.classList.remove("active");
    $("#ai-banner").classList.add("hidden");
  });

  // Amazon source toggle (in-page toggle on CPG view)
  $$("#amazon-source-toggle button").forEach(btn =>
    btn.addEventListener("click", () => {
      $$("#amazon-source-toggle button").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      cpg.amazonSource = btn.dataset.source;
    }));

  // Reset / Load sample / Run + CPG header buttons
  $("#reset-btn")?.addEventListener("click", resetCpgView);
  $("#load-sample-btn")?.addEventListener("click", loadCpgSample);
  $("#run-btn")?.addEventListener("click", runCpgVerification);
  $("#review-btn")?.addEventListener("click", openReview);
  $("#export-btn")?.addEventListener("click", exportFlow);

  function updateCpgStatus() {
    const hint = $("#status-hint");
    if (!hint) return;
    const catalogOK = cpg.catalogReady || !!cpg.catalogFile;
    if (catalogOK && cpg.amazonFile) {
      hint.innerHTML = "Catalog mapped and Amazon data ready — click <b>Run Verification</b> to log it in history.";
    } else if (catalogOK) {
      hint.textContent = "Catalog mapped. Upload Amazon / Keepa data to continue.";
    } else if (cpg.amazonFile) {
      hint.innerHTML = "Amazon data loaded. Click the <b>Vendor Catalog</b> card to import your catalog.";
    } else {
      hint.innerHTML = "Click the <b>Vendor Catalog</b> card to import your catalog — or use <b>Load Sample</b>.";
    }
  }

  function resetCpgView() {
    cpg.catalogFile = null;
    cpg.catalogMapping = null;
    cpg.catalogReady = false;
    cpg.scanName = "";
    cpg.amazonFile = null;
    state.results = [];
    state.scan = null;

    const catalogDrop = $("#catalog-drop");
    const amazonDrop  = $("#amazon-drop");
    if (catalogDrop) {
      catalogDrop.classList.remove("loaded");
      catalogDrop.innerHTML = CATALOG_DROP_HTML;
    }
    if (amazonDrop) {
      amazonDrop.classList.remove("loaded");
      amazonDrop.innerHTML = AMAZON_DROP_HTML;
    }
    // Inputs were replaced — re-wire both cards.
    wireCatalogCard();
    wireCpgDrop("#amazon-drop",  "#amazon-input",  "amazonFile");

    $("#progress-section")?.classList.add("hidden");
    $("#summary-section")?.classList.add("hidden");
    $("#results-table-wrap")?.classList.add("hidden");
    $("#empty-state")?.classList.remove("hidden");
    const rb = $("#review-btn"); if (rb) rb.disabled = true;
    const eb = $("#export-btn"); if (eb) eb.disabled = true;
    updateCpgStatus();
  }

  async function loadCpgSample() {
    const btn = $("#load-sample-btn");
    try {
      if (btn) { btn.disabled = true; btn.innerHTML = '<span class="mini-spinner"></span> Loading…'; }
      const catalogUrl = "/static/sample/sample_catalog.xlsx";
      const amazonUrl  = cpg.amazonSource === "amazon"
        ? "/static/sample/sample_amazon_export.xlsx"
        : "/static/sample/sample_amazon_keepa.xlsx";
      const [catalogResp, amazonResp] = await Promise.all([fetch(catalogUrl), fetch(amazonUrl)]);
      if (!catalogResp.ok || !amazonResp.ok) throw new Error("Sample files not available");
      const [catalogBlob, amazonBlob] = await Promise.all([catalogResp.blob(), amazonResp.blob()]);
      cpg.catalogFile = new File([catalogBlob], "sample_catalog.xlsx");
      cpg.amazonFile  = new File([amazonBlob], `sample_amazon_${cpg.amazonSource}.xlsx`);
      const catalogDrop = $("#catalog-drop");
      const amazonDrop  = $("#amazon-drop");
      if (catalogDrop) setCpgFile(catalogDrop, cpg.catalogFile, "catalogFile");
      if (amazonDrop)  setCpgFile(amazonDrop,  cpg.amazonFile,  "amazonFile");
      $("#status-hint").innerHTML = "Sample loaded — click <b>Run Verification</b>.";
    } catch (e) {
      console.warn("Sample load failed:", e.message);
      const hint = $("#status-hint");
      if (hint) hint.textContent = "Couldn't load sample files.";
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "Load Sample"; }
    }
  }

  async function runCpgVerification() {
    if (!cpg.catalogFile || !cpg.amazonFile) {
      const hint = $("#status-hint");
      if (hint) hint.textContent = "Upload both files first.";
      return;
    }
    state.currentView = "cpg";
    $("#progress-section")?.classList.remove("hidden");
    $("#summary-section")?.classList.add("hidden");
    $("#empty-state")?.classList.add("hidden");
    $("#results-table-wrap")?.classList.add("hidden");
    updateProgress(0, 1, "Creating scan…");

    try {
      // If the user skipped the wizard (dragged a file directly), synthesise
      // a canonical mapping against the standard template headers.
      const mapping = cpg.catalogMapping || {
        brand: "",
        upc_col: "UPC/EAN",
        item_id_col: "Item ID",
        title_col: "Vendor Title",
        asin_col: "ASIN",
        attr_cols: [],
        data_start: 0,
      };
      const scanName = cpg.scanName || cpg.catalogFile.name.replace(/\.[^.]+$/, "");

      // 1) Create the scan (status: pending, not yet user-visible in history)
      const fdScan = new FormData();
      fdScan.append("catalog_file", cpg.catalogFile);
      fdScan.append("mapping", JSON.stringify(mapping));
      fdScan.append("name", scanName);
      fdScan.append("ai_mode", cpg.aiMode ? "true" : "false");
      const jScan = await api("/api/scans", { method: "POST", body: fdScan, form: true });
      const scanId = jScan.scan.id;

      // 2) Attach Amazon / Keepa data (status → ready)
      updateProgress(0, 1, "Attaching Amazon data…");
      const fdAmz = new FormData();
      fdAmz.append("amazon_file", cpg.amazonFile);
      fdAmz.append("amazon_source", cpg.amazonSource);
      await api(`/api/scans/${scanId}/amazon`, { method: "POST", body: fdAmz, form: true });

      // 3) Run verification (status → verified_unreviewed = "To be reviewed")
      // We don't know the row count until the scan is created, so start ETA
      // now based on jScan.scan.catalog_count.
      const nRows = jScan.scan.catalog_count || 1;
      updateProgress(0, nRows, "Scoring products…");
      startEta(nRows, cpg.aiMode ? "ai" : "rule");
      const jVer = await api(`/api/scans/${scanId}/verify`, { method: "POST" });
      state.scan = jVer.scan;
      state.results = (jVer.results || []).map(r => ({
        ...r, barcode_db: null, barcode_loading: true,
      }));
      if (jVer.thresholds) state.thresholds = jVer.thresholds;
      if (Array.isArray(jVer.ai_added)) jVer.ai_added.forEach(a => showToast(a));

      state.resultsPage = 1;
      $("#summary-section")?.classList.remove("hidden");
      $("#results-table-wrap")?.classList.remove("hidden");
      renderSummary();
      renderResults();

      // 4) Barcode lookups — 6 workers, update progress + results as we go.
      //
      // Previously we called renderResults() every 2 completions, which with 6
      // workers finishing in rapid succession meant ~20 full-table re-renders
      // for a 40-row scan. Each render rebuilds ~800 DOM cells (20 columns ×
      // 40 rows) — enough to cause visible stutter / "glitch" on bigger lists.
      //
      // Now we just update the progress bar on every completion (cheap; one
      // attribute write) and rAF-throttle actual table renders so we never
      // re-render more than ~15× per second regardless of how fast lookups
      // resolve.
      const total = state.results.length;
      if (total > 0) {
        updateProgress(0, total, `Looking up ${total} barcodes…`);
        startEta(Math.ceil(total / 6), "barcode");
        let done = 0;
        let renderPending = false;
        const scheduleRender = () => {
          if (renderPending) return;
          renderPending = true;
          requestAnimationFrame(() => {
            renderPending = false;
            renderResults();
          });
        };
        const queue = [...state.results.keys()];
        const workers = new Array(6).fill(0).map(async () => {
          while (queue.length) {
            const idx = queue.shift();
            await lookupRowBarcode(state.results[idx]);
            done += 1;
            // Progress bar is cheap — update every completion so the counter
            // climbs smoothly. Table re-render is coalesced via rAF.
            updateProgress(done, total, `Verifying ${done} of ${total} products…`);
            scheduleRender();
          }
        });
        await Promise.all(workers);
        renderResults();  // guaranteed final pass with all rows settled
      }
      stopEta();
      $("#progress-section")?.classList.add("hidden");

      // Unlock header action buttons + refresh history in the background.
      const hasRows = state.results.length > 0;
      const rb = $("#review-btn"); if (rb) rb.disabled = !hasRows;
      const eb = $("#export-btn"); if (eb) eb.disabled = !hasRows;
      const ab = $("#ai-recheck-btn");
      if (ab) {
        ab.disabled = !hasRows;
        ab.classList.toggle("hidden", !hasRows);
      }
      loadHistory();
    } catch (err) {
      stopEta();
      $("#progress-section")?.classList.add("hidden");
      alert("Verification failed: " + err.message);
    }
  }

  // Initialise the status hint.
  updateCpgStatus();

  // ========================================================================
  //  Import Wizard (4-step)
  // ========================================================================
  const WIZ = state.wizard;
  const wizEl = {
    modal: $("#import-modal"),
    drop: $("#wizard-drop"),
    input: $("#wizard-file-input"),
    fileCard: $("#wizard-file-card"),
    fileName: $("#wizard-file-name"),
    fileSize: $("#wizard-file-size"),
    brand: $("#map-brand"),
    upc: $("#map-upc"),
    itemId: $("#map-item-id"),
    title: $("#map-title"),
    asin: $("#map-asin"),
    attrSelect: $("#map-attr-select"),
    attrPills: $("#map-attr-pills"),
    preview: $("#mapping-preview-table"),
    detailName: $("#detail-name"),
    detailAI: $("#detail-ai-mode"),
    stepLabel: $("#wizard-step-label"),
    back: $("#wizard-back"),
    next: $("#wizard-next"),
    close: $("#wizard-close"),
  };

  function openWizard() {
    resetWizard();
    wizEl.modal.classList.remove("hidden");
    renderWizardStep();
  }

  function closeWizard() {
    wizEl.modal.classList.add("hidden");
  }

  function resetWizard() {
    state.wizard = {
      step: 1, file: null, preview: null, dataStart: null,
      mapping: {
        brand: "", upc_col: "", item_id_col: "",
        title_col: "", asin_col: "", attr_cols: [],
      },
      name: "", ai_mode: false,
    };
    wizEl.drop.classList.remove("loaded");
    wizEl.fileCard.classList.add("hidden");
    wizEl.input.value = "";
    wizEl.brand.value = "";
    wizEl.detailName.value = "";
    wizEl.detailAI.checked = false;
  }

  wizEl.close.addEventListener("click", closeWizard);

  // Step 1: Drop zone
  wizEl.drop.addEventListener("click", () => wizEl.input.click());
  wizEl.drop.addEventListener("dragover", (e) => { e.preventDefault(); wizEl.drop.classList.add("drag"); });
  wizEl.drop.addEventListener("dragleave", () => wizEl.drop.classList.remove("drag"));
  wizEl.drop.addEventListener("drop", (e) => {
    e.preventDefault();
    wizEl.drop.classList.remove("drag");
    if (e.dataTransfer.files[0]) loadWizardFile(e.dataTransfer.files[0]);
  });
  wizEl.input.addEventListener("change", (e) => {
    if (e.target.files[0]) loadWizardFile(e.target.files[0]);
  });

  async function loadWizardFile(file) {
    state.wizard.file = file;
    wizEl.drop.classList.add("loaded");
    wizEl.fileCard.classList.remove("hidden");
    wizEl.fileName.textContent = file.name;
    wizEl.fileSize.textContent = fmtKB(file.size) + " · reading…";
    try {
      // Read fully into memory first — handles OneDrive / network-synced files
      // where the File object exists but bytes aren't locally available yet.
      const bytes = await file.arrayBuffer();
      const blob = new Blob([bytes], { type: file.type || "application/octet-stream" });
      wizEl.fileSize.textContent = fmtKB(file.size) + " · ready to parse";
      const fd = new FormData();
      fd.append("catalog_file", blob, file.name);
      const j = await api("/api/scans/preview", { method: "POST", body: fd, form: true });
      state.wizard.preview = j;
      wizEl.fileSize.textContent = `${fmtKB(file.size)} · ${j.total_rows || 0} rows · ${j.headers.length} columns`;
      state.wizard.dataStart = 0;
      // Infer scan name from filename
      state.wizard.name = file.name.replace(/\.[^.]+$/, "");
      wizEl.detailName.value = state.wizard.name;
      renderWizardStep();
    } catch (e) {
      alert("Could not preview file: " + e.message);
      state.wizard.file = null;
      wizEl.fileCard.classList.add("hidden");
      wizEl.drop.classList.remove("loaded");
    }
  }

  // Navigation
  wizEl.back.addEventListener("click", () => {
    if (state.wizard.step > 1) {
      state.wizard.step -= 1;
      renderWizardStep();
    } else {
      closeWizard();
    }
  });
  wizEl.next.addEventListener("click", async () => {
    const step = state.wizard.step;
    if (step < 3) {
      state.wizard.step += 1;
      renderWizardStep();
    } else {
      await submitWizard();
    }
  });

  function renderWizardStep() {
    const step = state.wizard.step;
    // stepper
    $$(".wizard-stepper .step").forEach(el => {
      const n = Number(el.dataset.stepIndicator);
      el.classList.toggle("active", n === step);
      el.classList.toggle("done",   n <  step);
    });
    $$(".wizard-stepper .step-line").forEach((el, i) => {
      el.classList.toggle("done", (i + 1) < step);
    });
    // panels
    $$(".wizard-step").forEach(el => {
      el.classList.toggle("hidden", Number(el.dataset.stepPanel) !== step);
    });
    wizEl.stepLabel.textContent = `Step ${step} of 3`;
    wizEl.back.textContent = step === 1 ? "Cancel" : "Back";
    wizEl.next.textContent = step === 3 ? "Save & Continue" : "Next";

    if (step === 2) {
      populateMappingSelects();
      renderAttrPills();
      renderMappingPreview();
    }

    updateWizardNextState();
  }

  function updateWizardNextState() {
    const step = state.wizard.step;
    const w = state.wizard;
    let ok = true;
    if (step === 1) ok = !!w.file && !!w.preview;
    if (step === 2) ok = !!w.mapping.upc_col && !!w.mapping.item_id_col && !!w.mapping.title_col;
    if (step === 3) ok = (wizEl.detailName.value || "").trim().length > 0;
    wizEl.next.disabled = !ok;
  }

  function populateMappingSelects() {
    const headers = state.wizard.preview?.headers || [];
    const selects = [
      ["upc", "upc_col", /upc|ean|gtin|barcode/i],
      ["itemId", "item_id_col", /item.?id|sku|mpn|product.?id/i],
      ["title", "title_col", /title|name|description|vendor.?title/i],
      ["asin", "asin_col", /asin/i],
    ];
    selects.forEach(([key, field, regex]) => {
      const sel = wizEl[key];
      const current = state.wizard.mapping[field];
      sel.innerHTML = `<option value="">Select column…</option>` +
        headers.map(h => `<option value="${escapeHtml(h)}"${h === current ? " selected" : ""}>${escapeHtml(h)}</option>`).join("");
      if (!current) {
        const match = headers.find(h => regex.test(h));
        if (match) {
          state.wizard.mapping[field] = match;
          sel.value = match;
        }
      }
      sel.onchange = () => {
        state.wizard.mapping[field] = sel.value;
        renderMappingPreview();
        updateWizardNextState();
      };
    });

    // Attribute column picker (multiselect)
    const attrSel = wizEl.attrSelect;
    const used = new Set(Object.values(state.wizard.mapping).flat());
    const attrOptions = headers.filter(h => !used.has(h) && !state.wizard.mapping.attr_cols.includes(h));
    attrSel.innerHTML = `<option value="">+ Add attribute column…</option>` +
      attrOptions.map(h => `<option value="${escapeHtml(h)}">${escapeHtml(h)}</option>`).join("");
    attrSel.onchange = () => {
      const v = attrSel.value;
      if (v && !state.wizard.mapping.attr_cols.includes(v)) {
        state.wizard.mapping.attr_cols.push(v);
        renderAttrPills();
        populateMappingSelects();
        renderMappingPreview();
      }
      attrSel.value = "";
    };

    // Brand text input
    wizEl.brand.value = state.wizard.mapping.brand || "";
    wizEl.brand.oninput = () => {
      state.wizard.mapping.brand = wizEl.brand.value;
      renderMappingPreview();
    };
  }

  function renderAttrPills() {
    wizEl.attrPills.innerHTML = state.wizard.mapping.attr_cols.map(c => `
      <span class="ms-pill">
        ${escapeHtml(c)}
        <button data-attr="${escapeHtml(c)}" title="Remove">
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M18 6L6 18M6 6l12 12"/></svg>
        </button>
      </span>
    `).join("");
    wizEl.attrPills.querySelectorAll("button").forEach(btn => {
      btn.addEventListener("click", () => {
        const col = btn.dataset.attr;
        state.wizard.mapping.attr_cols = state.wizard.mapping.attr_cols.filter(c => c !== col);
        renderAttrPills();
        populateMappingSelects();
        renderMappingPreview();
      });
    });
  }

  function renderMappingPreview() {
    const m = state.wizard.mapping;
    const { headers = [], preview = [] } = state.wizard.preview || {};
    const start = state.wizard.dataStart || 0;
    const slice = preview.slice(start, start + 3);
    const cols = [
      ["Brand",        () => m.brand || "—"],
      ["UPC/EAN",      (r) => getCell(headers, r, m.upc_col)],
      ["Item ID",      (r) => getCell(headers, r, m.item_id_col)],
      ["Vendor Title", (r) => getCell(headers, r, m.title_col)],
      ["ASIN",         (r) => getCell(headers, r, m.asin_col)],
      ...m.attr_cols.map(c => [c, (r) => getCell(headers, r, c)]),
    ];
    const thead = wizEl.preview.querySelector("thead");
    const tbody = wizEl.preview.querySelector("tbody");
    thead.innerHTML = `<tr>${cols.map(([label]) => `<th>${escapeHtml(label)}</th>`).join("")}</tr>`;
    tbody.innerHTML = slice.length === 0
      ? `<tr><td colspan="${cols.length}" style="text-align:center; color: var(--grey-500); padding: 18px;">No preview rows.</td></tr>`
      : slice.map(r => `<tr>${cols.map(([, fn]) => `<td>${escapeHtml(fn(r))}</td>`).join("")}</tr>`).join("");
  }

  function getCell(headers, row, colName) {
    if (!colName) return "—";
    const idx = headers.indexOf(colName);
    if (idx < 0) return "—";
    return (row.cells || [])[idx] || "";
  }

  // Detail fields
  wizEl.detailName.addEventListener("input", () => {
    state.wizard.name = wizEl.detailName.value;
    updateWizardNextState();
  });
  wizEl.detailAI.addEventListener("change", () => {
    state.wizard.ai_mode = wizEl.detailAI.checked;
  });

  // Wizard "Start/Apply" button — applies the mapping locally to the CPG
  // vendor-catalog slot and closes the modal. The scan itself is only
  // created once the user clicks Run Verification in the CPG view.
  async function submitWizard() {
    const w = state.wizard;
    if (!w.file) return;
    state.cpg.catalogFile = w.file;
    state.cpg.catalogMapping = {
      brand: w.mapping.brand,
      upc_col: w.mapping.upc_col,
      item_id_col: w.mapping.item_id_col,
      title_col: w.mapping.title_col,
      asin_col: w.mapping.asin_col,
      attr_cols: w.mapping.attr_cols,
      data_start: w.dataStart || 0,
    };
    state.cpg.scanName = (wizEl.detailName.value || "").trim() || w.file.name.replace(/\.[^.]+$/, "");
    state.cpg.aiMode   = !!wizEl.detailAI.checked;
    state.cpg.catalogReady = true;
    // Reflect in CPG view
    const drop = $("#catalog-drop");
    if (drop) {
      drop.classList.add("loaded");
      drop.innerHTML = `
        <div class="icon-wrap mx-auto mb-3" style="width:44px;height:44px;border-radius:10px;background:#d8f0f1;color:#0a6f71;display:inline-flex;align-items:center;justify-content:center;">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>
        </div>
        <div class="text-sm font-medium" style="color: var(--navy-800)">${escapeHtml(state.cpg.scanName)}</div>
        <div class="text-xs mt-1" style="color: #6b7480;">${escapeHtml(w.file.name)} · ${fmtKB(w.file.size)} · mapped</div>
        <div class="text-[11px] mt-2" style="color: var(--teal-500); font-weight: 600;">
          ${state.cpg.aiMode ? "AI mode" : "Standard"}
        </div>`;
    }
    closeWizard();
    updateCpgStatus();
  }

  // ========================================================================
  //  Amazon attach modal
  // ========================================================================
  const amEl = {
    modal:  $("#amazon-modal"),
    drop:   $("#amazon-modal-drop"),
    input:  $("#amazon-modal-input"),
    card:   $("#amazon-modal-file"),
    name:   $("#amazon-modal-file-name"),
    size:   $("#amazon-modal-file-size"),
    cancel: $("#amazon-modal-cancel"),
    save:   $("#amazon-modal-save"),
  };

  function openAmazonModal(scanId) {
    state.amazon = { scanId, source: "keepa", file: null };
    amEl.drop.classList.remove("loaded");
    amEl.card.classList.add("hidden");
    amEl.input.value = "";
    amEl.save.disabled = true;
    $$("#amazon-modal-source button").forEach(b => b.classList.toggle("active", b.dataset.source === "keepa"));
    amEl.modal.classList.remove("hidden");
  }

  amEl.cancel.addEventListener("click", () => amEl.modal.classList.add("hidden"));

  $$("#amazon-modal-source button").forEach(btn => btn.addEventListener("click", () => {
    $$("#amazon-modal-source button").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    state.amazon.source = btn.dataset.source;
  }));

  amEl.drop.addEventListener("click", () => amEl.input.click());
  amEl.drop.addEventListener("dragover", (e) => { e.preventDefault(); amEl.drop.classList.add("drag"); });
  amEl.drop.addEventListener("dragleave", () => amEl.drop.classList.remove("drag"));
  amEl.drop.addEventListener("drop", (e) => {
    e.preventDefault(); amEl.drop.classList.remove("drag");
    if (e.dataTransfer.files[0]) handleAmazonFile(e.dataTransfer.files[0]);
  });
  amEl.input.addEventListener("change", (e) => {
    if (e.target.files[0]) handleAmazonFile(e.target.files[0]);
  });

  function handleAmazonFile(file) {
    state.amazon.file = file;
    amEl.drop.classList.add("loaded");
    amEl.card.classList.remove("hidden");
    amEl.name.textContent = file.name;
    amEl.size.textContent = fmtKB(file.size);
    amEl.save.disabled = false;
  }

  amEl.save.addEventListener("click", async () => {
    const { scanId, source, file } = state.amazon;
    if (!scanId || !file) return;
    amEl.save.disabled = true;
    amEl.save.innerHTML = '<span class="mini-spinner"></span> Uploading…';
    try {
      const fd = new FormData();
      fd.append("amazon_file", file);
      fd.append("amazon_source", source);
      await api(`/api/scans/${scanId}/amazon`, { method: "POST", body: fd, form: true });
      amEl.modal.classList.add("hidden");
      if (state.currentView === "scan" && state.scan && state.scan.id === scanId) {
        await openScan(scanId);
      } else {
        await loadHistory();
      }
    } catch (e) {
      alert("Upload failed: " + e.message);
    } finally {
      amEl.save.disabled = false;
      amEl.save.innerHTML = "Attach &amp; continue";
    }
  });

  // ========================================================================
  //  Delete scan modal
  // ========================================================================
  function askDeleteScan(scanId) {
    state.deleteScanId = scanId;
    $("#delete-modal").classList.remove("hidden");
  }
  $("#delete-cancel").addEventListener("click", () => {
    state.deleteScanId = null;
    $("#delete-modal").classList.add("hidden");
  });
  $("#delete-confirm").addEventListener("click", async () => {
    const id = state.deleteScanId;
    if (!id) return;
    try {
      await api(`/api/scans/${id}`, { method: "DELETE" });
      $("#delete-modal").classList.add("hidden");
      state.deleteScanId = null;
      if (state.currentView === "scan" && state.scan && state.scan.id === id) {
        state.scan = null; state.results = [];
        switchView("history", $(".sidebar .nav-item[data-view='history']"));
      } else {
        await loadHistory();
      }
    } catch (e) {
      alert("Could not delete: " + e.message);
    }
  });

  // ========================================================================
  //  Export flows
  // ========================================================================
  $("#scan-export-btn").addEventListener("click", () => exportFlow());
  $("#scan-review-btn").addEventListener("click", openReview);

  async function exportScanDirect(scanId) {
    try {
      const j = await api(`/api/scans/${scanId}`);
      state.scan = j.scan;
      state.results = (j.results || []).map(r => ({ ...r, barcode_db: r.barcode_db || null }));
      if (j.thresholds) state.thresholds = j.thresholds;
      await exportFlow();
    } catch (e) {
      alert(e.message);
    }
  }

  async function exportFlow() {
    const unreviewed = state.results.filter(r => r.Verdict === "Review").length;
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
      const abbrFlat = [];
      Object.entries(state.library).forEach(([cat, entries]) => {
        entries.forEach(e => abbrFlat.push({ abbr: e.abbr, full: e.full, category: cat }));
      });
      // Shape results back into the legacy export contract.
      const exportRows = state.results.map(r => ({
        "UPC/EAN": r.UPC, "Item ID": r.ItemID, "Vendor Title": r.Title,
        "Brand": r.Brand, "ASIN": r.ASIN,
        "confidence": r.Confidence, "verdict": r.Verdict,
        "original_verdict": r.original_verdict, "review_status": r.review_status,
        "signals": r.signals, "amz_pack": r.amz_pack,
        "duplicate": r.duplicate, "blacklisted": r.blacklisted,
        "notes": r.notes, "barcode_db": r.barcode_db,
        "AmzTitle": r.AmzTitle,
      }));
      const blob = await api("/api/export", {
        method: "POST",
        body: { results: exportRows, abbreviations: abbrFlat },
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${(state.scan?.name || "verification").replace(/[^\w\-.]/g, "_")}.xlsx`;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      if (state.scan) {
        try { await api(`/api/scans/${state.scan.id}/mark-exported`, { method: "POST" }); } catch {}
        await loadHistory();
      }
    } catch (err) {
      alert("Export failed: " + err.message);
    }
  }

  // ========================================================================
  //  Review modal
  // ========================================================================
  $("#review-close").addEventListener("click", () => $("#review-modal").classList.add("hidden"));
  $("#confirm-export").addEventListener("click", () => { $("#review-modal").classList.add("hidden"); exportFlow(); });
  $("#review-search").addEventListener("input", renderReview);
  $$(".modal-tab").forEach(t => t.addEventListener("click", () => activateReviewTab(t.dataset.reviewTab)));

  function openReview() {
    $("#review-modal").classList.remove("hidden");
    activateReviewTab("Approved");
  }
  function activateReviewTab(name) {
    state.reviewTab = name;
    $$(".modal-tab").forEach(t => t.classList.toggle("active", t.dataset.reviewTab === name));
    $("#bulk-action").textContent =
      name === "Approved" ? "Reject All"
    : name === "Review"   ? "Approve All"
    :                       "Promote All";
    renderReview();
  }

  function reviewRows() {
    const q = $("#review-search").value.toLowerCase().trim();
    return state.results
      .filter(r => r.Verdict === state.reviewTab)
      .filter(r => !q || [r.Title, r.Brand, r.ASIN]
          .map(v => String(v || "").toLowerCase()).some(v => v.includes(q)));
  }

  function renderReview() {
    const tabs = { "Approved": 0, "Review": 0, "Not Approved": 0 };
    state.results.forEach(r => { if (r.Verdict in tabs) tabs[r.Verdict] += 1; });
    $("#count-Approved").textContent = tabs["Approved"];
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
      body.innerHTML = `<tr><td colspan="11" class="text-center py-10" style="color: #6b7480;">No rows in this tab.</td></tr>`;
      return;
    }

    const actionButton = (row) => {
      if (state.reviewTab === "Approved")
        return `<button class="row-action-btn reject" data-action="reject" data-key="${escapeHtml(rowKey(row))}">Reject</button>`;
      if (state.reviewTab === "Not Approved")
        return `<button class="row-action-btn promote" data-action="promote" data-key="${escapeHtml(rowKey(row))}">Promote to Approved</button>`;
      return `
        <button class="row-action-btn approve mr-1" data-action="approve" data-key="${escapeHtml(rowKey(row))}">Approve</button>
        <button class="row-action-btn discard" data-action="discard" data-key="${escapeHtml(rowKey(row))}">Discard</button>`;
    };

    rows.forEach(row => {
      const s = row.signals || {};
      const tr = document.createElement("tr");
      tr.className = verdictClass(row.Verdict);
      const tagHtml = row.review_status
        ? `<div><span class="badge ${reviewStatusClass(row.review_status)}">${row.review_status}</span></div>` : "";
      tr.innerHTML = `
        <td style="max-width: 360px;">
          <div class="text-sm" style="color: var(--navy-800); font-weight: 500;">${escapeHtml(row.Title)}</div>
          <div class="text-xs" style="color: #6b7480;">${escapeHtml(row.Brand)}</div>
          ${tagHtml}
        </td>
        <td class="font-mono text-xs">${escapeHtml(row.ASIN)}</td>
        <td style="max-width: 300px;"><div class="text-xs" style="color:#475569;">${escapeHtml(row.AmzTitle || "")}</div></td>
        <td class="font-semibold">${fmtConfidence(row.Confidence)}</td>
        <td><span class="badge ${badgeClass(row.Verdict)}">${row.Verdict}</span></td>
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
    const key = btn.dataset.key;
    const action = btn.dataset.action;
    const row = state.results.find(r => rowKey(r) === key);
    if (!row) return;
    try {
      await applyTransition(row, action);
    } catch (err) {
      alert("Action failed: " + err.message);
    }
    renderReview();
    renderResults();
  }

  async function applyTransition(row, action) {
    const upc = String(row.UPC || "");
    const asin = String(row.ASIN || "");
    const idx = state.results.indexOf(row);
    switch (action) {
      case "approve":
        row.Verdict = "Approved";
        row.review_status = "Reviewed";
        await api("/api/verified", { method: "POST", body: { upc, asin, review_status: "Reviewed", data: row } });
        break;
      case "discard":
        row.Verdict = "Not Approved";
        row.review_status = "";
        await api("/api/blacklist", { method: "POST", body: { upc, asin, confidence: row.Confidence, failed_signals: failedSignalsOf(row) } });
        break;
      case "promote":
        row.Verdict = "Approved";
        row.review_status = "Manually Approved";
        await api("/api/verified", { method: "POST", body: { upc, asin, review_status: "Manually Approved", data: row } });
        break;
      case "reject":
        row.Verdict = "Not Approved";
        row.review_status = "Manually Rejected";
        await api("/api/blacklist", { method: "POST", body: { upc, asin, confidence: row.Confidence, failed_signals: failedSignalsOf(row) } });
        break;
    }
    if (state.scan && idx >= 0) {
      try {
        await api(`/api/scans/${state.scan.id}/rows/${idx}`, {
          method: "POST",
          body: { verdict: row.Verdict, review_status: row.review_status || "", data: row },
        });
      } catch {}
    }
  }

  $("#bulk-action").addEventListener("click", async () => {
    const rows = reviewRows();
    if (rows.length === 0) return;
    const action =
        state.reviewTab === "Approved"     ? "reject"
      : state.reviewTab === "Not Approved" ? "promote"
      :                                      "approve";
    for (const r of rows) await applyTransition(r, action);
    renderReview();
    renderResults();
  });

  // ========================================================================
  //  Abbreviation Library (reworked)
  // ========================================================================
  $("#open-abbr-btn").addEventListener("click", () => {
    $("#abbr-panel").classList.add("open");
    if (state.categories.length === 0) loadLibrary();
  });
  $("#abbr-close").addEventListener("click", () => $("#abbr-panel").classList.remove("open"));

  // Synthetic tab value — not a real backend category. When active, the panel
  // shows every entry across every real category. Add-entry still requires
  // picking a concrete destination category.
  const ALL_CATEGORY = "All";

  async function loadLibrary() {
    try {
      const j = await api("/api/library");
      const realCats = j.categories || [];
      state.categories = [ALL_CATEGORY, ...realCats];
      state.realCategories = realCats;
      state.library = j.library || {};
      if (!state.activeCategory) state.activeCategory = ALL_CATEGORY;
      renderAbbrTabs();
      renderAbbrAddCategorySelect();
      renderAbbrBrowse();
    } catch (e) {
      console.error("loadLibrary", e);
    }
  }

  // Flatten every entry across categories into one list, tagging each row with
  // the category it came from so the "All" view can show/sort by it.
  function flattenAllEntries() {
    const out = [];
    const realCats = state.realCategories || [];
    for (const c of realCats) {
      for (const e of (state.library[c] || [])) out.push({ ...e, category: c });
    }
    return out;
  }

  function renderAbbrTabs() {
    const wrap = $("#abbr-tabs");
    const totalAll = flattenAllEntries().length;
    wrap.innerHTML = state.categories.map(cat => {
      const count = cat === ALL_CATEGORY ? totalAll : (state.library[cat] || []).length;
      const active = cat === state.activeCategory ? "active" : "";
      return `<div class="abbr-tab ${active}" data-cat="${escapeHtml(cat)}" role="tab">
                ${escapeHtml(cat)}
                <span class="abbr-tab-count">${count}</span>
              </div>`;
    }).join("");
    wrap.querySelectorAll(".abbr-tab").forEach(el => {
      el.addEventListener("click", () => {
        state.activeCategory = el.dataset.cat;
        state.abbrPage = 1;
        state.abbrSearch = "";
        $("#abbr-search").value = "";
        renderAbbrTabs();
        renderAbbrBrowse();
        // Sync the add form's category dropdown to the current tab — except
        // when "All" is active (All isn't a valid destination to add to).
        if (state.activeCategory !== ALL_CATEGORY) {
          const addSel = $("#abbr-new-cat");
          addSel.value = state.activeCategory;
          rebuildCustomMenu(addSel);
        }
      });
    });
  }

  function renderAbbrAddCategorySelect() {
    const sel = $("#abbr-new-cat");
    // "All" is a browse-only pseudo-category; never offer it as an add target.
    const realCats = state.realCategories || [];
    const currentAdd = sel.value;
    sel.innerHTML = realCats.map(c =>
      `<option value="${escapeHtml(c)}"${c === currentAdd ? " selected" : ""}>${escapeHtml(c)}</option>`
    ).join("");
    // If nothing was previously selected, default to the first real category.
    if (!sel.value && realCats.length > 0) sel.value = realCats[0];
    // Enhance once, then sync the trigger label on every rebuild.
    enhanceSelect(sel, { iconFor: categoryIconFor });
    rebuildCustomMenu(sel);
  }

  function renderAbbrBrowse() {
    const cat = state.activeCategory;
    const isAll = cat === ALL_CATEGORY;
    $("#abbr-browse-cat-label").textContent = cat || "";
    $("#abbr-search").placeholder = isAll
      ? "Search across all categories…"
      : `Search in ${cat || "…"}…`;

    // "All" pools every real-category list; everything else reads its own list.
    const all = isAll
      ? flattenAllEntries().slice().sort((a, b) => a.abbr.localeCompare(b.abbr))
      : (state.library[cat] || []).slice().sort((a, b) => a.abbr.localeCompare(b.abbr));
    const q = state.abbrSearch.trim().toLowerCase();
    const filtered = q ? all.filter(e =>
      (e.abbr + " " + e.full + " " + (e.category || "")).toLowerCase().includes(q)) : all;

    const results = $("#abbr-results");
    if (filtered.length === 0) {
      results.innerHTML = `<div class="abbr-empty">
        ${q
          ? `No matches for <b>${escapeHtml(q)}</b>${isAll ? "" : ` in ${escapeHtml(cat)}`}.`
          : (isAll ? `No entries in the library yet — add one above.` : `No entries in ${escapeHtml(cat)} yet — add one above.`)}
      </div>`;
      $("#abbr-pagination").classList.add("hidden");
      return;
    }

    let pageRows = filtered;
    let showPagination = false;
    let totalPages = 1;
    if (!q && filtered.length > PAGE_SIZE) {
      totalPages = Math.ceil(filtered.length / PAGE_SIZE);
      const page = Math.min(Math.max(1, state.abbrPage), totalPages);
      state.abbrPage = page;
      pageRows = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
      showPagination = true;
    }

    const rows = pageRows.map(e => {
      const by = (e.added_by || "system").toLowerCase();
      const byLabel = by === "ai" ? "AI" : by.charAt(0).toUpperCase() + by.slice(1);
      const deleteUI = state.abbrDeleteCandidate === e.id
        ? `<span class="abbr-row-confirm">
             <button class="yes" data-confirm-yes="${e.id}">Yes</button>
             <button class="no" data-confirm-no="${e.id}">No</button>
           </span>`
        : `<button class="abbr-row-del" data-del-id="${e.id}" title="Delete">
             <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/></svg>
           </button>`;
      // In "All" mode, surface the source category so users can tell where
      // each entry lives. Clicking it jumps to that category tab.
      const categoryCell = isAll
        ? `<td><button class="abbr-cat-chip" data-jump-cat="${escapeHtml(e.category || "")}">${escapeHtml(e.category || "")}</button></td>`
        : "";
      return `<tr class="abbr-row row-anim">
        <td><span class="abbr-short">${escapeHtml(e.abbr)}</span></td>
        <td>${escapeHtml(e.full)}</td>
        ${categoryCell}
        <td><span class="added-by added-by-${by}">${byLabel}</span></td>
        <td class="abbr-actions-cell">${deleteUI}</td>
      </tr>`;
    }).join("");

    results.innerHTML = `<table>
      <thead>
        <tr>
          <th>Short Form</th>
          <th>Full Form</th>
          ${isAll ? "<th>Category</th>" : ""}
          <th>Added By</th>
          <th class="text-right">&nbsp;</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;

    // Jump-to-category click wiring (only rendered in "All" mode).
    results.querySelectorAll("[data-jump-cat]").forEach(btn => {
      btn.addEventListener("click", () => {
        const target = btn.dataset.jumpCat;
        if (!target) return;
        state.activeCategory = target;
        state.abbrPage = 1;
        state.abbrSearch = "";
        $("#abbr-search").value = "";
        renderAbbrTabs();
        renderAbbrBrowse();
        const addSel = $("#abbr-new-cat");
        addSel.value = target;
        rebuildCustomMenu(addSel);
      });
    });

    // Delete wiring
    results.querySelectorAll("[data-del-id]").forEach(btn => {
      btn.addEventListener("click", () => {
        state.abbrDeleteCandidate = Number(btn.dataset.delId);
        renderAbbrBrowse();
      });
    });
    results.querySelectorAll("[data-confirm-no]").forEach(btn => {
      btn.addEventListener("click", () => {
        state.abbrDeleteCandidate = null;
        renderAbbrBrowse();
      });
    });
    results.querySelectorAll("[data-confirm-yes]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const id = Number(btn.dataset.confirmYes);
        try {
          await api("/api/library/delete", { method: "POST", body: { id } });
          state.abbrDeleteCandidate = null;
          await loadLibrary();
        } catch (e) {
          alert(e.message);
        }
      });
    });

    // Pagination
    const pagEl = $("#abbr-pagination");
    if (!showPagination) {
      pagEl.classList.add("hidden");
      return;
    }
    pagEl.classList.remove("hidden");
    const start = (state.abbrPage - 1) * PAGE_SIZE + 1;
    const end = Math.min(state.abbrPage * PAGE_SIZE, filtered.length);
    pagEl.innerHTML = `
      <span>Showing <b>${start}–${end}</b> of <b>${filtered.length}</b></span>
      <div class="abbr-pagination-controls">
        <button ${state.abbrPage === 1 ? "disabled" : ""} data-page="prev">← Prev</button>
        <span style="padding: 4px 10px;">${state.abbrPage} / ${totalPages}</span>
        <button ${state.abbrPage === totalPages ? "disabled" : ""} data-page="next">Next →</button>
      </div>`;
    pagEl.querySelectorAll("[data-page]").forEach(btn => {
      btn.addEventListener("click", () => {
        const dir = btn.dataset.page === "next" ? 1 : -1;
        state.abbrPage += dir;
        renderAbbrBrowse();
      });
    });
  }

  $("#abbr-search").addEventListener("input", (e) => {
    state.abbrSearch = e.target.value;
    state.abbrPage = 1;
    state.abbrDeleteCandidate = null;
    renderAbbrBrowse();
  });

  function showAbbrFeedback(type, htmlMsg) {
    const el = $("#abbr-add-feedback");
    el.classList.remove("success", "warn", "error", "show");
    el.classList.add("show", type);
    const icon = type === "success"
      ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>'
      : type === "warn"
      ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>'
      : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>';
    el.innerHTML = `${icon}<span>${htmlMsg}</span>`;
    clearTimeout(el._to);
    el._to = setTimeout(() => el.classList.remove("show"), 4500);
  }

  $("#abbr-add").addEventListener("click", async () => {
    const cat = $("#abbr-new-cat").value;
    const abbr = $("#abbr-new-key").value.trim();
    const full = $("#abbr-new-val").value.trim() || abbr;
    if (!abbr) {
      showAbbrFeedback("error", "Abbreviation is required.");
      return;
    }
    try {
      const j = await api("/api/library", {
        method: "POST",
        body: { category: cat, abbr, full, added_by: "user" },
      });
      state.library = j.library || state.library;
      renderAbbrTabs();
      if (j.duplicate && j.entry) {
        showAbbrFeedback(
          "warn",
          `<b>'${escapeHtml(j.entry.abbr)}'</b> already exists in <b>${escapeHtml(cat)}</b> → "${escapeHtml(j.entry.full)}"`
        );
      } else if (j.ok) {
        showAbbrFeedback("success", `Added to <b>${escapeHtml(cat)}</b> ✅`);
        $("#abbr-new-key").value = "";
        $("#abbr-new-val").value = "";
        // Switch browse to current category
        state.activeCategory = cat;
        state.abbrPage = 1;
        renderAbbrTabs();
      }
      renderAbbrBrowse();
    } catch (e) {
      showAbbrFeedback("error", escapeHtml(e.message));
    }
  });

  // Enter key submits in add fields
  ["#abbr-new-key", "#abbr-new-val"].forEach(sel => {
    $(sel).addEventListener("keydown", (e) => {
      if (e.key === "Enter") $("#abbr-add").click();
    });
  });

  // ========================================================================
  //  AI auto-add toasts
  // ========================================================================
  const MAX_TOASTS = 3;
  function showToast({ abbr, full, category }) {
    const stack = $("#toast-stack");
    // FIFO cap
    while (stack.children.length >= MAX_TOASTS) {
      stack.firstElementChild?.remove();
    }
    const el = document.createElement("div");
    el.className = "toast";
    el.innerHTML = `
      <div class="toast-sparkle">✨</div>
      <div class="toast-body">
        <div class="toast-head">AI learned</div>
        <div class="toast-msg">AI added <b>'${escapeHtml(abbr)}'</b> → <b>'${escapeHtml(full)}'</b> to ${escapeHtml(category)}</div>
      </div>
      <div class="toast-progress"></div>
    `;
    stack.appendChild(el);
    setTimeout(() => {
      el.classList.add("fade-out");
      setTimeout(() => el.remove(), 300);
    }, 5000);
  }

  // ========================================================================
  //  Settings
  // ========================================================================
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
      $("#settings-feedback").textContent = "Saved. Re-run any scan to apply.";
      setTimeout(() => $("#settings-feedback").textContent = "", 3000);
    } catch (e) {
      alert("Failed: " + e.message);
    }
  });

  // ========================================================================
  //  Pair Manager view
  // ========================================================================
  $("#pairs-search-btn").addEventListener("click", searchPairs);
  $("#pairs-search").addEventListener("keydown", (e) => { if (e.key === "Enter") searchPairs(); });

  async function searchPairs() {
    const q = $("#pairs-search").value.trim();
    if (!q) {
      $("#pairs-empty").classList.remove("hidden");
      $("#pairs-results").classList.add("hidden");
      return;
    }
    let j;
    try { j = await api("/api/pairs/search", { method: "POST", body: { query: q } }); }
    catch (e) { alert(e.message); return; }

    const body = $("#pairs-body");
    body.innerHTML = "";
    if (!j.results || j.results.length === 0) {
      body.innerHTML = `<tr><td colspan="6" class="text-center py-10" style="color: #6b7480;">No blacklisted pairs match "${escapeHtml(q)}".</td></tr>`;
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

  // ========================================================================
  //  AI Re-check (OpenAI second-pass verdicts)
  // ========================================================================
  //
  // Two entry points share this modal: the CPG header's "AI Re-check" button
  // (state.scan may be null if the user ran CPG view directly without
  // reopening a scan — we read state.scan if set, else find it from the
  // most recent scan the user produced) and the scan-detail header's button.
  //
  // Flow:
  //   1. Open modal → populate bucket counts from state.results.
  //   2. User toggles buckets / model → debounced call to /ai-estimate.
  //   3. User clicks Run → POST /ai-recheck, show ETA + progress bar driven
  //      by the same upfront-estimate helper (ETA_PER_ROW_MS.ai).
  //   4. On success → rehydrate state.results from response, re-render table,
  //      toast any library tokens added, close modal.
  state.aiRecheck = { debounce: null, inFlight: false };

  $("#ai-recheck-btn")?.addEventListener("click", openAIRecheckModal);
  $("#scan-ai-recheck-btn")?.addEventListener("click", openAIRecheckModal);
  $("#ai-recheck-cancel")?.addEventListener("click", closeAIRecheckModal);
  $("#ai-recheck-run")?.addEventListener("click", runAIRecheck);
  document.querySelectorAll(".ai-bucket-cb").forEach(cb =>
    cb.addEventListener("change", scheduleAIEstimate));
  $("#ai-recheck-model")?.addEventListener("change", scheduleAIEstimate);

  function openAIRecheckModal() {
    if (!state.scan || state.results.length === 0) {
      alert("Run a verification first — the AI re-check works on existing results.");
      return;
    }
    // Refresh bucket counts. Accept both "Approved" and the legacy "Verified".
    const count = (bucket) => state.results.filter(r => {
      const v = (r.Verdict || "").toLowerCase();
      return bucket === "Approved" ? (v === "approved" || v === "verified") : v === bucket.toLowerCase();
    }).length;
    $("#ai-count-Approved").textContent    = count("Approved");
    $("#ai-count-Review").textContent      = count("Review");
    $("#ai-count-NotApproved").textContent = count("Not Approved");
    $("#ai-recheck-progress").classList.add("hidden");
    $("#ai-recheck-run").disabled = false;
    $("#ai-recheck-run").textContent = "Run AI Re-check";
    $("#ai-recheck-cancel").textContent = "Cancel";
    $("#ai-recheck-modal").classList.remove("hidden");
    $("#ai-recheck-modal").style.display = "flex";
    scheduleAIEstimate();
  }

  function closeAIRecheckModal() {
    if (state.aiRecheck.inFlight) return;  // don't close mid-run
    $("#ai-recheck-modal").classList.add("hidden");
    $("#ai-recheck-modal").style.display = "none";
  }

  function selectedBuckets() {
    return Array.from(document.querySelectorAll(".ai-bucket-cb"))
      .filter(cb => cb.checked)
      .map(cb => cb.value);
  }

  function scheduleAIEstimate() {
    clearTimeout(state.aiRecheck.debounce);
    state.aiRecheck.debounce = setTimeout(fetchAIEstimate, 250);
  }

  async function fetchAIEstimate() {
    if (!state.scan) return;
    const buckets = selectedBuckets();
    const model   = $("#ai-recheck-model").value || "gpt-4o-mini";
    const rowsEl   = $("#ai-recheck-rows");
    const costEl   = $("#ai-recheck-cost");
    const durEl    = $("#ai-recheck-duration");
    const capWarn  = $("#ai-recheck-cap-warning");
    const keyWarn  = $("#ai-recheck-key-warning");
    const runBtn   = $("#ai-recheck-run");

    if (buckets.length === 0) {
      rowsEl.textContent = "0 rows"; costEl.textContent = "$0.0000";
      durEl.textContent = "~0s";
      capWarn.classList.add("hidden"); runBtn.disabled = true;
      return;
    }

    try {
      const j = await api(`/api/scans/${state.scan.id}/ai-estimate`, {
        method: "POST", body: { buckets, model },
      });
      rowsEl.textContent = `${j.row_count} row${j.row_count === 1 ? "" : "s"}`;
      costEl.textContent = `$${j.cost_usd_est.toFixed(4)}`;
      durEl.textContent  = `~${fmtEta(j.duration_ms_est)}`;
      capWarn.classList.toggle("hidden", j.cost_usd_est <= 1.0);
      keyWarn.classList.toggle("hidden", j.openai_configured);
      runBtn.disabled = j.row_count === 0 || !j.openai_configured;
    } catch (e) {
      costEl.textContent = "—"; rowsEl.textContent = "estimate failed";
    }
  }

  async function runAIRecheck() {
    if (!state.scan) return;
    const buckets = selectedBuckets();
    const model   = $("#ai-recheck-model").value || "gpt-4o-mini";
    if (buckets.length === 0) return;

    state.aiRecheck.inFlight = true;
    $("#ai-recheck-run").disabled = true;
    $("#ai-recheck-cancel").disabled = true;
    $("#ai-recheck-progress").classList.remove("hidden");
    $("#ai-recheck-progress-label").textContent = "Asking OpenAI about each row…";

    // Kick off a local ETA (server call is synchronous, so this is our only
    // way to animate progress; the bar fills up based on the estimate).
    const rowCount = Number(($("#ai-recheck-rows").textContent || "").split(" ")[0]) || 1;
    const startedAt = Date.now();
    const totalMs   = Math.max(2000, rowCount * 850);  // AI per-row estimate
    const etaTimer  = setInterval(() => {
      const elapsed = Date.now() - startedAt;
      const pct = Math.min(98, (elapsed / totalMs) * 100);
      $("#ai-recheck-progress-bar").style.width = pct + "%";
      const remaining = Math.max(0, totalMs - elapsed);
      $("#ai-recheck-progress-eta").textContent = remaining > 0
        ? `~${fmtEta(remaining)} remaining` : "finishing up…";
    }, 500);

    try {
      const j = await api(`/api/scans/${state.scan.id}/ai-recheck`, {
        method: "POST", body: { buckets, model },
      });
      clearInterval(etaTimer);
      $("#ai-recheck-progress-bar").style.width = "100%";
      $("#ai-recheck-progress-eta").textContent = "done";

      // Rehydrate state.results so the new ai_suggestion / ai_reason fields
      // appear — preserve the barcode_db + barcode_loading that aren't stored.
      const byKey = {};
      state.results.forEach(r => { byKey[`${r.ASIN}|${r.UPC}`] = r; });
      state.results = (j.results || []).map(r => {
        const prior = byKey[`${r.ASIN}|${r.UPC}`] || {};
        return {
          ...r,
          barcode_db: prior.barcode_db || null,
          barcode_loading: prior.barcode_loading || false,
        };
      });

      // Count rows that actually got an AI suggestion — that's the number
      // the user cares about, not just "rechecked".
      const withSuggestion = state.results.filter(r => r.ai_suggestion).length;
      const disagreements  = state.results.filter(r =>
        r.ai_suggestion && r.ai_suggestion !== "keep" &&
        r.ai_suggestion.toLowerCase() !== (r.Verdict || "").toLowerCase() &&
        !(r.ai_suggestion === "Approved" && (r.Verdict || "").toLowerCase() === "verified")
      ).length;

      $("#ai-recheck-progress-label").innerHTML =
        `<b>Re-checked ${j.rechecked}</b>${j.failed?.length ? ` <span style="color:#b45309;">(${j.failed.length} failed)</span>` : ""} · ` +
        `${disagreements} disagreement${disagreements === 1 ? "" : "s"} · ` +
        `spent $${j.cost_usd.toFixed(4)}`;

      // Toast any new library tokens.
      if (Array.isArray(j.tokens_added)) j.tokens_added.forEach(a => showToast(a));

      renderSummary();
      renderResults();

      // Nudge the results table horizontally so the AI Suggestion column is
      // visible. The table is 20 columns wide so without this the user might
      // just see "Re-checked N" and wonder where the suggestions went.
      setTimeout(() => {
        const wrap = $(rid("results-table-wrap"));
        const hdr  = wrap?.querySelectorAll("thead th") || [];
        // Column 11 (index 10) is AI Suggestion.
        const aiTh = hdr[10];
        if (wrap && aiTh) {
          wrap.scrollTo({
            left: Math.max(0, aiTh.offsetLeft - 140),
            behavior: "smooth",
          });
        }
      }, 120);

      // Swap Run button → Close so the user can dismiss when they're ready.
      // We used to auto-close after 1.2s which hid the result line before the
      // user could read it — especially bad when rechecked=0 (silent no-op).
      state.aiRecheck.inFlight = false;
      $("#ai-recheck-cancel").disabled = false;
      $("#ai-recheck-cancel").textContent = "Close";
      $("#ai-recheck-run").disabled = false;
      $("#ai-recheck-run").textContent = "Run again";
      if (withSuggestion === 0 && !j.failed?.length) {
        showToast({
          abbr: "No matching rows",
          full: "Check the bucket selection — none of your rows are in those verdict buckets.",
        });
      } else if (j.rechecked > 0) {
        showToast({
          abbr: `${j.rechecked} AI suggestion${j.rechecked === 1 ? "" : "s"} added`,
          full: `See the 'AI Suggestion' column. Accept ones you agree with.`,
        });
      }
    } catch (err) {
      clearInterval(etaTimer);
      state.aiRecheck.inFlight = false;
      $("#ai-recheck-run").disabled = false;
      $("#ai-recheck-cancel").disabled = false;
      $("#ai-recheck-progress-label").textContent = "Failed: " + err.message;
      $("#ai-recheck-progress-bar").style.width = "0%";
    }
  }

  // ========================================================================
  //  Boot
  // ========================================================================
  (async function boot() {
    await Promise.all([loadThresholds(), apiHealth()]);
    await loadHistory();

    // Turn the static filter / sort <select>s into animated custom dropdowns.
    // Both CPG and scan-detail views get the same treatment; loadLibrary
    // enhances #abbr-new-cat lazily once categories are ready.
    // Each select gets its own dot renderer so options feel like a palette
    // (verdict = green/yellow/red/orange, category = pink/blue/teal/...)
    // instead of a flat list.
    const verdictSel = $("#verdict-filter");
    if (verdictSel) enhanceSelect(verdictSel, { iconFor: verdictIconFor });
    const sortSel = $("#sort-by");
    if (sortSel) enhanceSelect(sortSel, { iconFor: sortIconFor });
    const scanVerdictSel = $("#scan-verdict-filter");
    if (scanVerdictSel) enhanceSelect(scanVerdictSel, { iconFor: verdictIconFor });
    const scanSortSel = $("#scan-sort-by");
    if (scanSortSel) enhanceSelect(scanSortSel, { iconFor: sortIconFor });
    const aiModelSel = $("#ai-recheck-model");
    if (aiModelSel) enhanceSelect(aiModelSel, { iconFor: aiModelIconFor });

    // Pre-load library so the panel opens instantly the first time.
    loadLibrary();
  })();

  // ==========================================================================
  //  Analytics tab (ROI & Cost)
  // ==========================================================================
  // Two entry points:
  //   1. "Vet Existing Listings"   — catalog has ASINs → straight to vetting.
  //   2. "Find & Vet ASINs"        — catalog has no ASINs → 3-tier SP-API
  //      search (UPC / Item ID / Title) populates a candidate pool, then the
  //      vetting engine scores every (catalog_row × candidate_asin) pair.
  //
  // Backend endpoints (see routers/analytics.py):
  //   GET  /api/analytics/status   → SP-API credential probe
  //   GET  /api/analytics/runs     → past runs list
  //
  // The rest of the pipeline endpoints stream in behind the Upload buttons
  // which re-use the existing 4-step import wizard — wired in a later pass.

  // AI title-clean cost estimator.
  // gpt-4o-mini pricing: $0.15/1M input tokens, $0.60/1M output tokens.
  // Each title call: ~100 tokens in (system prompt + title) + ~20 tokens out.
  function _updateAiCostHint() {
    const hint = $("#awiz-ai-cost-hint");
    if (!hint) return;
    const checked = !!$("#awiz-title-ai-clean")?.checked;
    if (!checked) { hint.style.display = "none"; return; }
    const total   = state.awiz?.preview?.total_rows || 0;
    const header  = (state.awiz?.headerRowIdx ?? 0) + 1;
    const rows    = Math.max(0, total - header);
    if (rows === 0) { hint.style.display = "none"; return; }
    const inputCost  = rows * 100 * (0.15  / 1_000_000);
    const outputCost = rows * 20  * (0.60  / 1_000_000);
    const total_usd  = inputCost + outputCost;
    const label = total_usd < 0.01
      ? "< $0.01"
      : `~$${total_usd.toFixed(2)}`;
    hint.textContent = `est. ${label} for ${rows.toLocaleString()} rows`;
    hint.style.display = "inline";
  }

  // Title-only options panel toggle — now lives inside wizard step 4.
  const _titleCb   = $("#awiz-search-title");
  const _titleOpts = $("#awiz-title-opts");
  if (_titleCb && _titleOpts) {
    _titleCb.addEventListener("change", () => {
      _titleOpts.classList.toggle("hidden", !_titleCb.checked);
      _updateAiCostHint();
    });
  }
  $("#awiz-title-ai-clean")?.addEventListener("change", _updateAiCostHint);

  // Probe SP-API credentials so the header pill matches reality.
  async function probeSPAPIStatus() {
    const okPill   = $("#analytics-sp-api-pill");
    const missPill = $("#analytics-sp-api-missing-pill");
    if (!okPill || !missPill) return;
    try {
      const r = await fetch("/api/analytics/status");
      if (!r.ok) throw new Error("status " + r.status);
      const j = await r.json();
      okPill.classList.toggle("hidden",   !j.sp_api_configured);
      missPill.classList.toggle("hidden",  j.sp_api_configured);
    } catch {
      // Endpoint not yet wired — leave both pills hidden.
      okPill.classList.add("hidden");
      missPill.classList.add("hidden");
    }
  }

  // Runs list — each row is clickable and opens the detail view. Running
  // rows carry an inline progress bar so the user can see activity without
  // drilling in. We re-poll every 2s while any row is active.
  state.analyticsRunsPoll = { timer: null };

  function _runIsActive(r) {
    const s = (r.status || "").toLowerCase();
    return s === "searching" || s === "vetting" || s === "pending" || s === "rescoring";
  }

  async function loadAnalyticsRuns() {
    probeSPAPIStatus();
    const body = $("#analytics-runs-body");
    if (!body) return;
    try {
      const r = await fetch("/api/analytics/runs");
      if (!r.ok) throw new Error("status " + r.status);
      const rows = await r.json();
      if (!Array.isArray(rows) || rows.length === 0) {
        body.innerHTML = `
          <div class="empty-state" style="padding: 24px 12px;">
            <div class="text-sm" style="color: #6b7480;">No runs yet — kick one off above.</div>
          </div>`;
        _clearAnalyticsRunsPoll();
        return;
      }

      body.innerHTML = rows.map(r => {
        const active = _runIsActive(r);
        const pct = r.progress_total
          ? Math.min(100, Math.round((r.progress_done / r.progress_total) * 100))
          : 0;
        const phase = r.progress_phase || r.status || "";
        return `
          <div class="analytics-run-row" data-run-id="${r.id}">
            <div class="flex items-center justify-between gap-3">
              <div class="flex-1 min-w-0">
                <div class="font-medium text-sm truncate" style="color: var(--purple-800);">${escapeHtml(r.name || "Untitled run")}</div>
                <div class="text-xs mt-0.5" style="color: #6b7480;">
                  ${r.total_catalog_items || 0} items · ${r.total_candidates_found || 0} candidates ·
                  <span style="color: var(--green-600);">${r.verified_count || 0} verified</span> ·
                  <span style="color: #b58100;">${r.review_count || 0} review</span> ·
                  <span style="color: #b91c1c;">${r.not_approved_count || 0} rejected</span>
                </div>
              </div>
              <div class="text-xs text-right" style="color: ${active ? 'var(--purple-700)' : '#94a3b8'}; min-width: 130px;">
                ${active ? `<span class="mini-spinner"></span> ${escapeHtml(phase)} ${pct}%` : escapeHtml(r.status || "")}
              </div>
            </div>
            ${active ? `
              <div class="analytics-run-inline-bar"><div class="fill" style="width: ${pct}%"></div></div>
            ` : ""}
          </div>`;
      }).join("");

      // Wire row clicks → open detail view.
      body.querySelectorAll(".analytics-run-row").forEach(el => {
        el.addEventListener("click", () => {
          const id = Number(el.dataset.runId);
          if (id) openAnalyticsRunDetail(id);
        });
      });

      // Keep polling while any run is active.
      if (rows.some(_runIsActive)) {
        _schedAnalyticsRunsPoll();
      } else {
        _clearAnalyticsRunsPoll();
      }
    } catch (e) {
      body.innerHTML = `
        <div class="empty-state" style="padding: 24px 12px;">
          <div class="text-sm" style="color: #6b7480;">Couldn't load runs (${escapeHtml(e.message || "error")}).</div>
        </div>`;
    }
  }
  $("#analytics-refresh-runs")?.addEventListener("click", loadAnalyticsRuns);

  function _schedAnalyticsRunsPoll() {
    if (state.analyticsRunsPoll.timer) return;
    state.analyticsRunsPoll.timer = setTimeout(() => {
      state.analyticsRunsPoll.timer = null;
      // Only repoll if we're still on the Analytics list view.
      if (state.currentView === "analytics") loadAnalyticsRuns();
    }, 2500);
  }
  function _clearAnalyticsRunsPoll() {
    if (state.analyticsRunsPoll.timer) {
      clearTimeout(state.analyticsRunsPoll.timer);
      state.analyticsRunsPoll.timer = null;
    }
  }

  // ==========================================================================
  //  Analytics Run Detail view
  // ==========================================================================
  // Opens when the user clicks a row in the runs list. Shows:
  //   - a progress bar (while the run is Searching / Vetting)
  //   - a 5-up summary row (catalog / candidates / verified / review / reject)
  //   - a candidate table ordered by row_idx, confidence DESC
  //   - an "Export CSV" button that dumps the candidates client-side
  // Polls /api/analytics/runs/{id} every 2s while the run is active so the
  // progress bar + table stream in as ASINs get found and scored.

  state.analyticsRun = {
    id: null,
    data: null,
    poll: null,
    tab: "Approved",
    search: "",
    sortKey: "row_idx",
    sortDir: "asc",
    page: 1,
    pageSize: 50,
    progressHistory: [],
    lastPhase: null,
    rankMin: 0,
    rankMax: 0,
    skipNullRank: false,
  };

  function _clearAnalyticsRunPoll() {
    if (state.analyticsRun.poll) {
      clearTimeout(state.analyticsRun.poll);
      state.analyticsRun.poll = null;
    }
  }

  async function openAnalyticsRunDetail(runId) {
    state.analyticsRun.id = runId;
    // Reset per-run view state so the previous run's tab/search/sort
    // don't bleed into the next one.
    state.analyticsRun.tab = "Approved";
    state.analyticsRun.search = "";
    state.analyticsRun.sortKey = "confidence";
    state.analyticsRun.sortDir = "desc";
    state.analyticsRun.page = 1;
    state.analyticsRun.rankMin = 0;
    state.analyticsRun.rankMax = 0;
    state.analyticsRun.skipNullRank = false;
    const sb = $("#analytics-run-search"); if (sb) sb.value = "";
    const rrMin = $("#analytics-run-rank-min"); if (rrMin) rrMin.value = "";
    const rrMax = $("#analytics-run-rank-max"); if (rrMax) rrMax.value = "";
    const rrSkip = $("#analytics-run-rank-skip-null"); if (rrSkip) rrSkip.checked = false;
    state.currentView = "analytics-run";

    // Hide all other views, show ours. We don't touch the sidebar selection —
    // the user is still under the "Analytics" nav item.
    $$("main > div").forEach(el => el.classList.add("hidden"));
    $("#view-analytics-run")?.classList.remove("hidden");

    // Reset UI shell before the first fetch so there's no stale data flash.
    $("#analytics-run-title").textContent = "Loading…";
    $("#analytics-run-subtitle").textContent = "";
    $("#analytics-run-candidates-body").innerHTML = "";
    $("#analytics-run-candidates-empty").classList.add("hidden");
    $("#analytics-run-export").disabled = true;

    await fetchAnalyticsRunDetail();
  }

  async function fetchAnalyticsRunDetail() {
    const id = state.analyticsRun.id;
    if (!id) return;
    try {
      const j = await api(`/api/analytics/runs/${id}?limit=5000`);
      state.analyticsRun.data = j;
      // If status just changed (e.g. rescore finished), clear per-tab cache.
      const prevStatus = state.analyticsRun._lastStatus;
      if (prevStatus && prevStatus !== j.run?.status) {
        state.analyticsRun.tabPageData = {};
      }
      state.analyticsRun._lastStatus = j.run?.status;
      renderAnalyticsRunDetail(j);

      // Auto-fetch current verdict tab if page data isn't loaded yet.
      // This covers: initial open, and the moment a run/rescore finishes
      // (status change clears tabPageData above, so next poll refills it).
      const currentTab = state.analyticsRun.tab;
      if (currentTab !== "All" && !((state.analyticsRun.tabPageData || {})[currentTab])) {
        await _fetchVerdictPage(currentTab, 1);
      }

      // Poll while active or while AI check is running.
      _clearAnalyticsRunPoll();
      const aiRunning2 = (j.run?.ai_check_status || "").toLowerCase() === "running";
      // _aiCheckJustStarted keeps polling for a few cycles after the user
      // clicks Start AI Check, giving the background thread time to set "Running".
      const aiPending = (state.analyticsRun._aiCheckJustStarted || 0) > 0;
      if (aiPending) state.analyticsRun._aiCheckJustStarted = Math.max(0, (state.analyticsRun._aiCheckJustStarted || 0) - 1);
      if (_runIsActive(j.run || {}) || aiRunning2 || aiPending) {
        state.analyticsRun.poll = setTimeout(fetchAnalyticsRunDetail, 2000);
      }
    } catch (e) {
      $("#analytics-run-title").textContent = "Couldn't load run";
      $("#analytics-run-subtitle").textContent = e.message || "";
    }
  }

  // Map backend verdict ("verified" / "review" / "not_approved") to the
  // three-tab labels used in the UI (matches the vetting review modal).
  const _VERDICT_TO_TAB = {
    "verified":     "Approved",
    "review":       "Review",
    "not_approved": "Not Approved",
  };
  const _TAB_TO_VERDICT = {
    "Approved":     "verified",
    "Review":       "review",
    "Not Approved": "not_approved",
  };
  const _VERDICT_LABEL = {
    "verified":     "Approved",
    "review":       "Review",
    "not_approved": "Not Approved",
  };

  // Build a short human-readable reason explaining the verdict.
  function _verdictReason(c) {
    const sc = (c.data && c.data.scores) || {};
    const v  = (c.verdict || "").toLowerCase();
    const conf = c.confidence || 0;

    // Hard-reject signals (checked before confidence)
    if (sc.size_mismatch)   return "Size mismatch";
    if (sc.gender_mismatch) return "Gender mismatch";
    if (sc.color_mismatch)  return "Color mismatch";

    // BSR cap overrides everything else — check before pack_mismatch so it
    // isn't masked. If conf ≥ 35 the score alone would put this in Review or
    // higher, so something external (rank) must have forced not_approved.
    if (v === "not_approved" && conf >= 35) {
      const rank = c.sales_rank;
      return rank != null ? `BSR ${Number(rank).toLocaleString()} > max` : "Rank cap";
    }

    // Pack mismatch → capped at 80 → review
    if (sc.pack_mismatch) {
      const p = sc.effective_pack;
      return p > 1 ? `Pack ×${p} on Amazon` : "Pack mismatch";
    }

    // UPC match contributed to score
    if (sc.upc_match) {
      return v === "verified" ? "UPC confirmed" : "UPC match";
    }

    // Pure confidence-based
    if (v === "not_approved") return "Low confidence";
    if (v === "review")       return `Score ${Math.round(conf)}%`;
    if (v === "verified")     return conf >= 90 ? "High confidence" : "Above threshold";

    return "";
  }

  // Pull the sortable value for a candidate row, cheap enough to recompute.
  function _candSortValue(c, src, key) {
    const amz = (c.data && c.data.amazon) || {};
    switch (key) {
      case "row_idx":    return c.row_idx;
      case "upc":        return (src.upc || src.itemid || "").toString().toLowerCase();
      case "title":      return (src.title || "").toString().toLowerCase();
      case "asin":       return (c.asin || "").toString().toLowerCase();
      case "amz_title":  return (amz.title || "").toString().toLowerCase();
      case "brand":      return (amz.brand || amz.manufacturer || src.brand || "").toString().toLowerCase();
      case "sources":    return (Array.isArray(c.sources) ? c.sources.join(",") : "").toLowerCase();
      case "amz_pack":   return Number(c.amz_pack ?? 0);
      case "sales_rank": return c.sales_rank != null ? Number(c.sales_rank) : Infinity;
      case "confidence": return Number(c.confidence || 0);
      case "verdict":    return (c.verdict || "").toString().toLowerCase();
      default:           return "";
    }
  }

  function _progressSubLabel(phase, done, total) {
    const p = (phase || "").toLowerCase();
    const d = done.toLocaleString(), t = total.toLocaleString();
    if (p.includes("upc"))                          return `${d} of ${t} UPC batches`;
    if (p.includes("item id"))                      return `${d} of ${t} Item ID lookups`;
    if (p.includes("title search") || p.includes("tier 3")) return `${d} of ${t} title searches`;
    if (p.includes("cleaning") || p.includes("ai")) return `${d} of ${t} titles cleaned`;
    if (p.includes("vetting"))                      return `${d} of ${t} rows vetted`;
    if (p.includes("re-scor") || p.includes("rescor")) return `${d} of ${t} candidates rescored`;
    return `${d} of ${t}`;
  }

  function _calcEta(history, total) {
    if (!history || history.length < 3) return null;
    const oldest = history[0], newest = history[history.length - 1];
    const elapsed = (newest.t - oldest.t) / 1000;
    const delta = newest.done - oldest.done;
    if (delta <= 0 || elapsed <= 0) return null;
    const rate = delta / elapsed;
    const remaining = total - newest.done;
    if (remaining <= 0) return 0;
    return remaining / rate;
  }

  function _fmtEta(sec) {
    if (sec < 5)  return "< 5s";
    if (sec < 60) return `~${Math.ceil(sec)}s`;
    const m = Math.floor(sec / 60), s = Math.ceil(sec % 60);
    if (m < 60)   return `~${m}m ${s}s`;
    const h = Math.floor(m / 60), rm = m % 60;
    return `~${h}h ${rm}m`;
  }

  function renderAnalyticsRunDetail(j) {
    const run  = j.run || {};
    const cand = Array.isArray(j.candidates) ? j.candidates : [];

    $("#analytics-run-title").textContent = run.name || "Untitled run";
    const started = run.created_at ? new Date(run.created_at + "Z") : null;
    const when = started ? `started ${started.toLocaleString()}` : "";
    const methods = Array.isArray(run.search_methods) ? run.search_methods.join(" · ") : "";
    $("#analytics-run-subtitle").textContent =
      [methods, when, `${run.total_catalog_items || 0} catalog rows`]
        .filter(Boolean).join(" — ");

    // Status pill
    const statusText = $("#analytics-run-status-text");
    const pill = $("#analytics-run-status-pill");
    if (statusText && pill) {
      statusText.textContent = run.status || "—";
      const s = (run.status || "").toLowerCase();
      const active = _runIsActive(run);
      pill.style.background = active ? "#ede9fe"
                           : s === "error"   ? "#fee2e2"
                           : s === "paused"  ? "#fef3c7"
                           : s === "stopped" ? "#f1f5f9"
                           : "#dcfce7";
      pill.style.color      = active ? "#5b21b6"
                           : s === "error"   ? "#991b1b"
                           : s === "paused"  ? "#92400e"
                           : s === "stopped" ? "#475569"
                           : "#166534";
    }
    _updateRunControlButtons(run.status);

    // Progress card
    const progCard = $("#analytics-run-progress-card");
    const s_prog = (run.status || "").toLowerCase();
    const showProg = _runIsActive(run) || s_prog === "paused" || s_prog === "stopped";
    if (showProg) {
      progCard?.classList.remove("hidden");
      // Status-aware card styling
      ["is-paused","is-stopped","is-error","is-complete"].forEach(c => progCard?.classList.remove(c));
      if (s_prog === "paused")        progCard?.classList.add("is-paused");
      else if (s_prog === "stopped")  progCard?.classList.add("is-stopped");
      else if (s_prog === "error")    progCard?.classList.add("is-error");
      // Dot
      const dot = $("#analytics-run-progress-dot");
      if (dot) {
        dot.className = "rp-dot";
        if (_runIsActive(run))         dot.classList.add("is-active");
        else if (s_prog === "paused")  dot.classList.add("is-paused");
        else if (s_prog === "stopped") dot.classList.add("is-stopped");
        else if (s_prog === "error")   dot.classList.add("is-error");
      }
      const done = run.progress_done || 0, total = run.progress_total || 0;
      const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
      const phase = run.progress_phase || run.status || "";
      const phaseLabel = s_prog === "paused"  ? "Paused"
                       : s_prog === "stopped" ? "Stopped by user"
                       : phase || "Working…";
      $("#analytics-run-progress-phase").textContent = phaseLabel;
      // Sub-label: contextual description of done/total
      const subEl = $("#analytics-run-progress-sub");
      if (subEl && total > 0 && _runIsActive(run)) {
        subEl.textContent = _progressSubLabel(phase, done, total);
      } else if (subEl) {
        subEl.textContent = "";
      }
      // Counts
      $("#analytics-run-progress-counts").textContent =
        total > 0 ? `${done.toLocaleString()} / ${total.toLocaleString()}` : "";
      $("#analytics-run-progress-bar").style.width = pct + "%";
      // ETA — track history per phase, reset when phase changes
      const ph = state.analyticsRun;
      if (ph.lastPhase !== phase) { ph.lastPhase = phase; ph.progressHistory = []; }
      if (_runIsActive(run) && total > 0 && done > 0) {
        ph.progressHistory.push({ t: Date.now(), done });
        if (ph.progressHistory.length > 12) ph.progressHistory.shift();
      }
      const etaEl = $("#analytics-run-progress-eta");
      if (etaEl) {
        const etaSec = _calcEta(ph.progressHistory, total);
        if (etaSec !== null && _runIsActive(run)) {
          etaEl.textContent = "ETA " + _fmtEta(etaSec);
          etaEl.classList.remove("hidden");
        } else {
          etaEl.classList.add("hidden");
        }
      }
    } else {
      progCard?.classList.add("hidden");
    }

    // Summary tiles
    $("#analytics-run-stat-catalog").textContent    = run.total_catalog_items || 0;
    $("#analytics-run-stat-candidates").textContent = run.total_candidates_found || 0;
    $("#analytics-run-stat-verified").textContent   = run.verified_count || 0;
    $("#analytics-run-stat-review").textContent     = run.review_count || 0;
    $("#analytics-run-stat-rejected").textContent   = run.not_approved_count || 0;

    // Tab badge counts — use the run-level totals from the DB so they're
    // accurate even when only a subset of candidates is loaded.
    $("#analytics-run-count-Approved").textContent = run.verified_count     ?? 0;
    $("#analytics-run-count-Review").textContent   = run.review_count       ?? 0;
    $("#analytics-run-count-Not").textContent      = run.not_approved_count ?? 0;
    $("#analytics-run-count-All").textContent      = run.total_candidates_found ?? cand.length;

    renderAnalyticsRunCandidates();

    // Export button — enabled once we have candidates.
    const exp = $("#analytics-run-export");
    if (exp) exp.disabled = cand.length === 0;

    // AI Check button — visible when run is done/paused/stopped and not currently checking.
    const aiBtn = $("#analytics-run-ai-check");
    if (aiBtn) {
      const s2 = (run.status || "").toLowerCase();
      const canAi = s2 === "complete" || s2 === "paused" || s2 === "stopped" || s2 === "error";
      const aiStatus = (run.ai_check_status || "").toLowerCase();
      const aiRunning = aiStatus === "running";
      const aiDone = aiStatus === "done";
      const aiErr = aiStatus.startsWith("error");

      aiBtn.classList.toggle("hidden", !canAi);
      if (aiRunning) {
        const aiDoneN = run.ai_check_done || 0;
        const aiTotalN = run.ai_check_total || 0;
        aiBtn.textContent = aiTotalN > 0
          ? `Checking ${aiDoneN.toLocaleString()}/${aiTotalN.toLocaleString()}…`
          : "Checking…";
        aiBtn.disabled = true;
        aiBtn.style.opacity = "0.7";
        // Keep polling while AI check is running
        if (!_runIsActive(run)) {
          _clearAnalyticsRunPoll();
          state.analyticsRun.poll = setTimeout(fetchAnalyticsRunDetail, 2000);
        }
      } else {
        aiBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0"><circle cx="12" cy="12" r="10"/><path d="M12 8v4l3 3"/></svg> ${aiDone ? "Re-check AI" : aiErr ? "Retry AI Check" : "AI Check"}`;
        aiBtn.disabled = false;
        aiBtn.style.opacity = "1";
      }
    }
  }

  function _renderPagination(page, totalPages, totalRows) {
    const el = $("#analytics-run-pagination");
    if (!el) return;
    if (totalPages <= 1) { el.innerHTML = ""; return; }
    const pageSize = state.analyticsRun.pageSize || 50;
    const from = (page - 1) * pageSize + 1;
    const to   = Math.min(page * pageSize, totalRows);

    // Build page number buttons with ellipsis for large ranges.
    const pages = [];
    for (let p = 1; p <= totalPages; p++) {
      if (p === 1 || p === totalPages || (p >= page - 2 && p <= page + 2)) {
        pages.push(p);
      } else if (pages[pages.length - 1] !== "…") {
        pages.push("…");
      }
    }
    const btns = pages.map(p =>
      p === "…"
        ? `<span class="pg-ellipsis">…</span>`
        : `<button class="pg-btn${p === page ? " active" : ""}" data-pg="${p}">${p}</button>`
    ).join("");

    el.innerHTML = `
      <span class="pg-info">Showing ${from}–${to} of ${totalRows}</span>
      <button class="pg-btn" data-pg="${page - 1}" ${page <= 1 ? "disabled" : ""}>&#8249;</button>
      ${btns}
      <button class="pg-btn" data-pg="${page + 1}" ${page >= totalPages ? "disabled" : ""}>&#8250;</button>`;

    el.querySelectorAll("button[data-pg]").forEach(btn => {
      btn.addEventListener("click", () => {
        const p = parseInt(btn.dataset.pg);
        if (isNaN(p) || p < 1 || p > totalPages) return;
        _onPageClick(p);
      });
    });
  }

  // Navigate to page p — fetches from server when on a verdict tab.
  async function _onPageClick(p) {
    state.analyticsRun.page = p;
    const tab = state.analyticsRun.tab;
    if (tab !== "All") {
      await _fetchVerdictPage(tab, p);
    }
    renderAnalyticsRunCandidates();
    $("#analytics-run-candidates-body")?.closest(".overflow-auto")?.scrollTo(0, 0);
  }

  // Fetch one page of verdict-filtered candidates from the server and store
  // in tabPageData. Shows "Loading…" immediately while the request is in flight.
  async function _fetchVerdictPage(tab, page) {
    const verdict = _TAB_TO_VERDICT[tab];
    if (!verdict) return;
    const id = state.analyticsRun.id;
    const pageSize = state.analyticsRun.pageSize || 50;
    const offset = (page - 1) * pageSize;
    state.analyticsRun.tabLoading = tab;
    renderAnalyticsRunCandidates();
    try {
      const url = `/api/analytics/runs/${id}?verdict=${encodeURIComponent(verdict)}&limit=${pageSize}&offset=${offset}`;
      const j = await api(url);
      if (!state.analyticsRun.tabPageData) state.analyticsRun.tabPageData = {};
      state.analyticsRun.tabPageData[tab] = { serverPage: page, candidates: j.candidates || [] };
    } catch (e) {
      console.warn("[tab fetch]", e);
    } finally {
      state.analyticsRun.tabLoading = null;
    }
  }

  // Re-render just the table (tab switch, search, sort all go through here
  // — no need to re-fetch from the server).
  function renderAnalyticsRunCandidates() {
    const j = state.analyticsRun.data;
    const tab = state.analyticsRun.tab;
    const isVerdictTab = tab !== "All";
    const tabPageEntry = (state.analyticsRun.tabPageData || {})[tab];
    const isLoading = state.analyticsRun.tabLoading === tab;

    // Need main data for "All", or a server page / loading state for verdict tabs.
    if (!isVerdictTab && !j) return;
    if (isVerdictTab && !tabPageEntry && !isLoading) return;

    const cand = isVerdictTab
      ? (tabPageEntry?.candidates || [])
      : (Array.isArray(j?.candidates) ? j.candidates : []);

    const srcByIdx = {};
    ((j?.catalog_rows) || state.analyticsRun._catalogRows || []).forEach(r => { srcByIdx[r.row_idx] = r; });

    // ---- filter by search (verdict already filtered server-side for verdict tabs) --
    const q = (state.analyticsRun.search || "").toLowerCase().trim();
    const matches = (c) => {
      // "All" tab: also filter by verdict if not truly all
      if (!isVerdictTab && tab !== "All") {
        const wanted = _TAB_TO_VERDICT[tab];
        if ((c.verdict || "").toLowerCase() !== wanted) return false;
      }
      if (!q) return true;
      const src = srcByIdx[c.row_idx] || {};
      const amz = (c.data && c.data.amazon) || {};
      const hay = [
        src.title, src.brand, src.upc, src.itemid,
        c.asin, amz.title, amz.brand, amz.manufacturer,
      ].map(v => String(v || "").toLowerCase()).join(" ");
      return hay.includes(q);
    };
    let rows = cand.filter(matches);

    // ---- filter by BSR range (client-side display filter) -------------
    const _rankMin = state.analyticsRun.rankMin || 0;
    const _rankMax = state.analyticsRun.rankMax || 0;
    const _skipNull = state.analyticsRun.skipNullRank || false;
    if (_skipNull || _rankMin > 0 || _rankMax > 0) {
      rows = rows.filter(c => {
        const rank = c.sales_rank;
        if (rank == null) return !_skipNull;
        if (_rankMin > 0 && rank < _rankMin) return false;
        if (_rankMax > 0 && rank > _rankMax) return false;
        return true;
      });
    }

    // ---- sort ---------------------------------------------------------
    const sortKey = state.analyticsRun.sortKey;
    const dir = state.analyticsRun.sortDir === "desc" ? -1 : 1;
    rows.sort((a, b) => {
      const av = _candSortValue(a, srcByIdx[a.row_idx] || {}, sortKey);
      const bv = _candSortValue(b, srcByIdx[b.row_idx] || {}, sortKey);
      if (typeof av === "number" && typeof bv === "number") {
        if (av === bv) return (a.row_idx - b.row_idx);
        return (av - bv) * dir;
      }
      const as = String(av), bs = String(bv);
      if (as === bs) return (a.row_idx - b.row_idx);
      return (as < bs ? -1 : 1) * dir;
    });

    // ---- header carets ------------------------------------------------
    $$("#analytics-run-thead-row th.sortable").forEach(th => {
      th.classList.remove("asc", "desc");
      if (th.dataset.sortKey === sortKey) {
        th.classList.add(state.analyticsRun.sortDir);
      }
    });

    // ---- tab active state --------------------------------------------
    $$(".run-tab").forEach(t => {
      t.classList.toggle("active", t.dataset.runTab === tab);
    });

    // ---- bulk-action button label ------------------------------------
    const bulkBtn = $("#analytics-run-bulk");
    if (bulkBtn) {
      bulkBtn.textContent =
        tab === "Approved"      ? "Reject All"
      : tab === "Not Approved"  ? "Promote All"
      : tab === "Review"        ? "Approve All"
      :                           "—";
      bulkBtn.disabled = rows.length === 0 || tab === "All";
      bulkBtn.style.opacity = bulkBtn.disabled ? "0.45" : "1";
      bulkBtn.style.pointerEvents = bulkBtn.disabled ? "none" : "auto";
    }

    // ---- pagination ---------------------------------------------------
    // For verdict tabs the server sends exactly one page; use DB counts for
    // the total so page buttons cover the full dataset.
    const pageSize = state.analyticsRun.pageSize || 50;
    const page     = state.analyticsRun.page;
    let totalRows, totalPages, pageRows;
    if (isVerdictTab) {
      const run = j?.run || {};
      const dbTotal = tab === "Approved"     ? (run.verified_count     ?? 0)
                    : tab === "Review"        ? (run.review_count       ?? 0)
                    : tab === "Not Approved"  ? (run.not_approved_count ?? 0)
                    : cand.length;
      totalRows  = dbTotal;
      totalPages = Math.max(1, Math.ceil(totalRows / pageSize));
      pageRows   = rows; // server already sent the right page
    } else {
      totalRows  = rows.length;
      totalPages = Math.max(1, Math.ceil(totalRows / pageSize));
      if (state.analyticsRun.page > totalPages) state.analyticsRun.page = totalPages;
      pageRows   = rows.slice((page - 1) * pageSize, page * pageSize);
    }

    // ---- render rows --------------------------------------------------
    const body  = $("#analytics-run-candidates-body");
    const empty = $("#analytics-run-candidates-empty");
    const count = $("#analytics-run-candidates-count");
    if (count) count.textContent = totalRows;

    if (totalRows === 0) {
      body.innerHTML = "";
      empty.classList.remove("hidden");
      if (state.analyticsRun.tabLoading === tab) {
        empty.querySelector(".text-sm").textContent = "Loading…";
      } else {
        empty.querySelector(".text-sm").textContent = cand.length === 0
          ? "No candidates yet. This table fills up as the search finishes."
          : (q ? `Nothing matches "${q}" in ${tab}.` : `No items in ${tab}.`);
      }
      _renderPagination(0, 0, 0);
      return;
    }
    empty.classList.add("hidden");

    const actionButtons = (c) => {
      const key = `${c.row_idx}|${c.asin}`;
      const v = (c.verdict || "").toLowerCase();
      if (v === "verified") {
        return `<div class="run-action-cell"><button class="row-action-btn reject" data-run-action="reject" data-run-key="${escapeHtml(key)}">Reject</button></div>`;
      }
      if (v === "not_approved") {
        return `<div class="run-action-cell"><button class="row-action-btn promote" data-run-action="promote" data-run-key="${escapeHtml(key)}">Promote</button></div>`;
      }
      return `<div class="run-action-cell">
        <button class="row-action-btn approve" data-run-action="approve" data-run-key="${escapeHtml(key)}">Approve</button>
        <button class="row-action-btn discard" data-run-action="discard" data-run-key="${escapeHtml(key)}">Discard</button>
      </div>`;
    };

    body.innerHTML = pageRows.map(c => {
      const src = srcByIdx[c.row_idx] || {};
      const conf = Math.round((c.confidence || 0) * 10) / 10;
      const confCls = conf >= 90 ? "hi" : conf >= 35 ? "mid" : "lo";
      const v = (c.verdict || "").toLowerCase();
      const verdictCls = v === "verified" ? "badge-verified"
                      : v === "review"   ? "badge-review"
                      :                    "badge-not";
      const amz = (c.data && c.data.amazon) || {};
      const amzTitle = amz.title || "";
      const amzBrand = amz.brand || amz.manufacturer || "";
      const sources = Array.isArray(c.sources) ? c.sources.join(", ") : "";
      const label  = _VERDICT_LABEL[v] || (c.verdict || "");
      const reason = _verdictReason(c);
      return `
        <tr>
          <td class="text-xs">${escapeHtml(String(c.row_idx + 1))}</td>
          <td class="font-mono text-xs">
            <div>${escapeHtml(src.upc || "—")}</div>
            <div style="color:#94a3b8;">${escapeHtml(src.itemid || "")}</div>
          </td>
          <td style="max-width:260px;">
            <div class="text-sm" style="color: var(--navy-800);">${escapeHtml(src.title || "")}</div>
            <div class="text-xs" style="color:#94a3b8;">${escapeHtml(src.brand || "")}</div>
          </td>
          <td class="font-mono text-xs">${escapeHtml(c.asin || "")}</td>
          <td style="max-width:300px;">
            <div class="text-sm" style="color: #475569;">${escapeHtml(amzTitle)}</div>
          </td>
          <td class="text-xs">${escapeHtml(amzBrand)}</td>
          <td class="text-xs">${escapeHtml(sources)}</td>
          <td class="text-xs text-center">${c.amz_pack != null ? escapeHtml(String(c.amz_pack)) : "1"}</td>
          <td class="text-xs text-right" style="color:#64748b;">${c.sales_rank != null ? Number(c.sales_rank).toLocaleString() : "—"}</td>
          <td><span class="conf-pill ${confCls}">${conf}%</span></td>
          <td>
            <span class="badge ${verdictCls}">${escapeHtml(label)}</span>
            ${c.ai_verdict ? `<span class="ai-badge ai-badge-${escapeHtml(c.ai_verdict)}" title="${escapeHtml(c.ai_reasoning || "")}">${c.ai_verdict === "approve" ? "✓ AI" : c.ai_verdict === "reject" ? "✗ AI" : "? AI"}</span>` : ""}
          </td>
          <td class="text-xs" style="color:#64748b; white-space:nowrap;">${escapeHtml(reason)}</td>
          <td class="text-right">${actionButtons(c)}</td>
        </tr>`;
    }).join("");

    body.querySelectorAll("[data-run-action]").forEach(btn => {
      btn.addEventListener("click", handleAnalyticsRunAction);
    });

    _renderPagination(page, totalPages, totalRows);
  }

  // ---- verdict-transition POST + local state update ------------------
  async function applyAnalyticsVerdict(row_idx, asin, verdict, review_status) {
    const id = state.analyticsRun.id;
    if (!id) return;
    const j = await api(`/api/analytics/runs/${id}/candidates/verdict`, {
      method: "POST",
      body: { row_idx, asin, verdict, review_status: review_status || "" },
    });
    // Mirror into local state so we don't need to re-fetch
    const data = state.analyticsRun.data;
    if (data && Array.isArray(data.candidates)) {
      const c = data.candidates.find(x => x.row_idx === row_idx && x.asin === asin);
      if (c) {
        c.verdict = verdict;
        if (review_status) c.review_status = review_status;
        if (!c.data) c.data = {};
        c.data.verdict = verdict;
      }
      // Also mirror run counts returned by the server.
      if (j && j.counts && data.run) {
        data.run.total_candidates_found = j.counts.total_candidates_found;
        data.run.verified_count        = j.counts.verified_count;
        data.run.review_count          = j.counts.review_count;
        data.run.not_approved_count    = j.counts.not_approved_count;
      }
    }
  }

  async function handleAnalyticsRunAction(e) {
    const btn = e.currentTarget;
    const key = btn.dataset.runKey || "";
    const action = btn.dataset.runAction;
    const [rawIdx, asin] = key.split("|");
    const row_idx = parseInt(rawIdx, 10);
    if (Number.isNaN(row_idx) || !asin) return;

    let verdict, review_status;
    switch (action) {
      case "approve": verdict = "verified";     review_status = "Reviewed";          break;
      case "discard": verdict = "not_approved"; review_status = "";                  break;
      case "promote": verdict = "verified";     review_status = "Manually Approved"; break;
      case "reject":  verdict = "not_approved"; review_status = "Manually Rejected"; break;
      default: return;
    }

    btn.disabled = true;
    btn.style.opacity = "0.5";
    try {
      await applyAnalyticsVerdict(row_idx, asin, verdict, review_status);
    } catch (err) {
      alert("Couldn't update verdict: " + (err.message || err));
      btn.disabled = false;
      btn.style.opacity = "1";
      return;
    }

    // Remove the item from the current verdict tab's page cache so it
    // disappears immediately without waiting for a server round-trip.
    const tab = state.analyticsRun.tab;
    const tabEntry = (state.analyticsRun.tabPageData || {})[tab];
    if (tab !== "All" && tabEntry) {
      tabEntry.candidates = tabEntry.candidates.filter(
        x => !(x.row_idx === row_idx && x.asin === asin)
      );
    }

    // Full re-render so counts + tab contents + summary tiles all refresh.
    renderAnalyticsRunDetail(state.analyticsRun.data);
  }

  function exportAnalyticsRunCsv() {
    const id = state.analyticsRun.id;
    if (!id) return;
    const params = new URLSearchParams();
    const rm = state.analyticsRun.rankMin || 0;
    const rx = state.analyticsRun.rankMax || 0;
    const sn = state.analyticsRun.skipNullRank || false;
    if (rm > 0) params.set("min_rank", rm);
    if (rx > 0) params.set("max_rank", rx);
    if (sn)     params.set("skip_null_rank", "true");
    const qs = params.toString();
    const a = document.createElement("a");
    a.href = `/api/analytics/runs/${id}/export${qs ? "?" + qs : ""}`;
    a.download = "";
    document.body.appendChild(a); a.click(); a.remove();
  }

  function _updateRunControlButtons(status) {
    const s = (status || "").toLowerCase();
    const pauseBtn   = $("#analytics-run-pause");
    const stopBtn    = $("#analytics-run-stop");
    const resumeBtn  = $("#analytics-run-resume");
    const rescoreBtn = $("#analytics-run-rescore");
    if (!pauseBtn) return;
    const searching = s === "searching" || s === "vetting";
    const paused    = s === "paused";
    const stopped   = s === "stopped";
    const rescoring = s === "rescoring";
    const done      = s === "complete" || s === "error";
    pauseBtn.classList.toggle("hidden",   !searching);
    stopBtn.classList.toggle("hidden",    !(searching || paused));
    resumeBtn.classList.toggle("hidden",  !(paused || stopped));
    rescoreBtn?.classList.toggle("hidden", !(done || paused || stopped) || rescoring);
  }

  async function _sendRunControl(action) {
    const id = state.analyticsRun.id;
    if (!id) return;
    try {
      await api(`/api/analytics/runs/${id}/control`, { method: "POST", body: { action } });
      await fetchAnalyticsRunDetail();
    } catch (e) {
      alert("Control action failed: " + (e.message || e));
    }
  }

  async function _deleteRun() {
    const id = state.analyticsRun.id;
    if (!id) return;
    try {
      await api(`/api/analytics/runs/${id}`, { method: "DELETE" });
      _clearAnalyticsRunPoll();
      state.analyticsRun.id = null;
      state.analyticsRun.data = null;
      state.currentView = "analytics";
      $$("main > div").forEach(el => el.classList.add("hidden"));
      document.getElementById("view-analytics")?.classList.remove("hidden");
      await loadAnalyticsRuns();
    } catch (e) {
      alert("Could not delete run: " + (e.message || e));
    }
  }

  // Stop & Delete modal
  const _stopModal = $("#analytics-stop-modal");
  $("#analytics-run-stop")?.addEventListener("click", () => {
    _stopModal?.classList.remove("hidden");
  });
  $("#analytics-stop-cancel")?.addEventListener("click", () => {
    _stopModal?.classList.add("hidden");
  });
  $("#analytics-stop-confirm")?.addEventListener("click", async () => {
    _stopModal?.classList.add("hidden");
    await _deleteRun();
  });
  _stopModal?.addEventListener("click", (e) => {
    if (e.target === _stopModal) _stopModal.classList.add("hidden");
  });

  // Re-score modal
  const _rescoreModal = $("#analytics-rescore-modal");
  $("#analytics-run-rescore")?.addEventListener("click", () => {
    // Populate dropdown from the raw column keys of the first catalog row.
    const catRows = state.analyticsRun?.data?.catalog_rows || [];
    const firstRow = catRows[0] || {};
    const raw = firstRow.raw || {};
    // Always include the stored title/search_title fields as options,
    // then append any extra raw columns that aren't already covered.
    const builtins = [
      { key: "title", label: "Vendor Title (mapped at wizard)" },
      ...(firstRow.search_title !== undefined
        ? [{ key: "search_title", label: "Search Title (mapped at wizard)" }] : []),
    ];
    const rawCols = Object.keys(raw).filter(k => k !== "title" && k !== "search_title");
    const colOpts = [
      ...builtins.map(b => `<option value="${escapeHtml(b.key)}">${escapeHtml(b.label)}</option>`),
      ...rawCols.map(k => `<option value="${escapeHtml(k)}">${escapeHtml(k)}</option>`),
    ].join("") || `<option value="">— no columns found —</option>`;
    const sel = $("#analytics-rescore-col");
    if (sel) sel.innerHTML = colOpts;
    const brandSel = $("#analytics-rescore-brand-col");
    if (brandSel) brandSel.innerHTML = `<option value="">— none (use wizard brand) —</option>` + colOpts;
    // Pre-populate min/max rank from the run's stored values.
    const storedMinRank = state.analyticsRun?.data?.run?.min_rank || 0;
    const storedMaxRank = state.analyticsRun?.data?.run?.max_rank || 0;
    const mrMinInput = $("#analytics-rescore-min-rank");
    if (mrMinInput) mrMinInput.value = storedMinRank > 0 ? String(storedMinRank) : "";
    const mrInput = $("#analytics-rescore-max-rank");
    if (mrInput) mrInput.value = storedMaxRank > 0 ? String(storedMaxRank) : "";
    _rescoreModal?.classList.remove("hidden");
  });
  $("#analytics-rescore-cancel")?.addEventListener("click", () => {
    _rescoreModal?.classList.add("hidden");
  });
  _rescoreModal?.addEventListener("click", (e) => {
    if (e.target === _rescoreModal) _rescoreModal.classList.add("hidden");
  });
  $("#analytics-rescore-confirm")?.addEventListener("click", async () => {
    const col = $("#analytics-rescore-col")?.value || "";
    _rescoreModal?.classList.add("hidden");
    const id = state.analyticsRun.id;
    if (!id) return;
    try {
      const brandCol = $("#analytics-rescore-brand-col")?.value || "";
      const minRankRescore = Math.max(0, parseInt($("#analytics-rescore-min-rank")?.value || "0", 10) || 0);
      const maxRankRescore = Math.max(0, parseInt($("#analytics-rescore-max-rank")?.value || "0", 10) || 0);
      await api(`/api/analytics/runs/${id}/rescore`, {
        method: "POST",
        body: { title_col: col, brand_col: brandCol, min_rank: minRankRescore, max_rank: maxRankRescore },
      });
      // Clear per-tab cache so next tab visit re-fetches with updated scores.
      state.analyticsRun.tabPageData = {};
      // Kick an immediate poll so the progress card appears without waiting 2s.
      await fetchAnalyticsRunDetail();
    } catch (e) {
      alert("Re-score failed: " + (e.message || e));
    }
  });

  $("#analytics-run-pause")?.addEventListener("click",  () => _sendRunControl("pause"));
  $("#analytics-run-resume")?.addEventListener("click", () => _sendRunControl("resume"));

  $("#analytics-run-back")?.addEventListener("click", () => {
    _clearAnalyticsRunPoll();
    state.currentView = "analytics";
    $$("main > div").forEach(el => el.classList.add("hidden"));
    $("#view-analytics")?.classList.remove("hidden");
    loadAnalyticsRuns();
  });
  $("#analytics-run-export")?.addEventListener("click", exportAnalyticsRunCsv);

  // Verdict tabs — fetch page 1 from server on every tab switch.
  // Subsequent page navigation also fetches from the server (50 rows/page),
  // so every item in every tab is reachable regardless of total count.
  $$("#view-analytics-run .run-tab").forEach(t => {
    t.addEventListener("click", async () => {
      const tab = t.dataset.runTab || "Approved";
      state.analyticsRun.tab = tab;
      state.analyticsRun.page = 1;
      if (tab !== "All") {
        await _fetchVerdictPage(tab, 1);
      }
      renderAnalyticsRunCandidates();
    });
  });

  // Search box — local filter, no round-trip.
  $("#analytics-run-search")?.addEventListener("input", (e) => {
    state.analyticsRun.search = e.target.value || "";
    state.analyticsRun.page = 1;
    renderAnalyticsRunCandidates();
  });

  // BSR range filter — client-side display filter, no round-trip.
  function _applyRankFilter() {
    state.analyticsRun.rankMin = Math.max(0, parseInt($("#analytics-run-rank-min")?.value || "0", 10) || 0);
    state.analyticsRun.rankMax = Math.max(0, parseInt($("#analytics-run-rank-max")?.value || "0", 10) || 0);
    state.analyticsRun.skipNullRank = !!($("#analytics-run-rank-skip-null")?.checked);
    state.analyticsRun.page = 1;
    renderAnalyticsRunCandidates();
  }
  $("#analytics-run-rank-min")?.addEventListener("change", _applyRankFilter);
  $("#analytics-run-rank-max")?.addEventListener("change", _applyRankFilter);
  $("#analytics-run-rank-skip-null")?.addEventListener("change", _applyRankFilter);

  // Sortable header clicks — toggle asc / desc on the current column, or
  // switch to a new column (default asc, except confidence which makes
  // more sense desc-first).
  $$("#view-analytics-run th.sortable").forEach(th => {
    th.addEventListener("click", () => {
      const key = th.dataset.sortKey;
      if (!key) return;
      if (state.analyticsRun.sortKey === key) {
        state.analyticsRun.sortDir = state.analyticsRun.sortDir === "asc" ? "desc" : "asc";
      } else {
        state.analyticsRun.sortKey = key;
        state.analyticsRun.sortDir = (key === "confidence") ? "desc" : "asc";
      }
      state.analyticsRun.page = 1;
      renderAnalyticsRunCandidates();
    });
  });

  // Bulk action — tab-aware: rejects everything on the Approved tab,
  // promotes everything on Not Approved, approves everything on Review.
  $("#analytics-run-bulk")?.addEventListener("click", async () => {
    const id = state.analyticsRun.id;
    const data = state.analyticsRun.data;
    if (!id || !data) return;
    const tab = state.analyticsRun.tab;
    if (tab === "All") return;

    let verdict, review_status;
    if (tab === "Approved")          { verdict = "not_approved"; review_status = "Manually Rejected"; }
    else if (tab === "Not Approved") { verdict = "verified";     review_status = "Manually Approved"; }
    else                             { verdict = "verified";     review_status = "Reviewed"; }

    // Honour the current search filter — bulk only hits visible rows.
    const q = (state.analyticsRun.search || "").toLowerCase().trim();
    const wanted = _TAB_TO_VERDICT[tab];
    const srcByIdx = {};
    (data.catalog_rows || []).forEach(r => { srcByIdx[r.row_idx] = r; });
    const items = (data.candidates || []).filter(c => {
      if ((c.verdict || "").toLowerCase() !== wanted) return false;
      if (!q) return true;
      const src = srcByIdx[c.row_idx] || {};
      const amz = (c.data && c.data.amazon) || {};
      const hay = [
        src.title, src.brand, src.upc, src.itemid,
        c.asin, amz.title, amz.brand, amz.manufacturer,
      ].map(v => String(v || "").toLowerCase()).join(" ");
      return hay.includes(q);
    }).map(c => ({ row_idx: c.row_idx, asin: c.asin }));

    if (items.length === 0) return;
    const label =
        tab === "Approved"     ? `Reject ${items.length} candidate${items.length === 1 ? "" : "s"}?`
      : tab === "Not Approved" ? `Promote ${items.length} candidate${items.length === 1 ? "" : "s"} to Approved?`
      :                          `Approve ${items.length} candidate${items.length === 1 ? "" : "s"}?`;
    if (!window.confirm(label)) return;

    const btn = $("#analytics-run-bulk");
    if (btn) { btn.disabled = true; btn.style.opacity = "0.5"; }
    try {
      const j = await api(`/api/analytics/runs/${id}/candidates/bulk_verdict`, {
        method: "POST",
        body: { items, verdict, review_status },
      });
      // Mirror the verdict change into local state to avoid a round-trip.
      const touched = new Set(items.map(i => `${i.row_idx}|${i.asin}`));
      (data.candidates || []).forEach(c => {
        const k = `${c.row_idx}|${c.asin}`;
        if (touched.has(k)) {
          c.verdict = verdict;
          c.review_status = review_status;
          if (!c.data) c.data = {};
          c.data.verdict = verdict;
        }
      });
      if (j && j.counts && data.run) {
        data.run.total_candidates_found = j.counts.total_candidates_found;
        data.run.verified_count        = j.counts.verified_count;
        data.run.review_count          = j.counts.review_count;
        data.run.not_approved_count    = j.counts.not_approved_count;
      }
      renderAnalyticsRunDetail(data);
    } catch (err) {
      alert("Bulk update failed: " + (err.message || err));
    } finally {
      if (btn) { btn.disabled = false; btn.style.opacity = "1"; }
    }
  });

  // ==========================================================================
  //  AI Check modal
  // ==========================================================================
  const _aiCheckModal = $("#analytics-ai-check-modal");

  async function _fetchAiCheckEstimate() {
    const id = state.analyticsRun.id;
    if (!id) return;
    const verdict = $("#ai-check-verdict-filter")?.value || "review";
    const el = $("#ai-check-estimate");
    if (el) el.textContent = "Loading estimate…";
    try {
      const j = await api(`/api/analytics/runs/${id}/ai_check/estimate?verdict=${encodeURIComponent(verdict)}`);
      if (el) {
        const cnt = (j.candidate_count ?? 0).toLocaleString();
        const cost = (j.cost_usd_est ?? 0).toFixed(4);
        const secs = Math.ceil((j.duration_ms_est ?? 0) / 1000);
        el.innerHTML = `<strong>${cnt}</strong> candidate${j.candidate_count === 1 ? "" : "s"} · est. <strong>$${cost}</strong> · ~${secs}s`;
      }
    } catch (e) {
      if (el) el.textContent = "Could not load estimate: " + (e.message || e);
    }
  }

  $("#analytics-run-ai-check")?.addEventListener("click", () => {
    _aiCheckModal?.classList.remove("hidden");
    // Reset to default filter
    const vf = $("#ai-check-verdict-filter");
    if (vf) vf.value = "review";
    _fetchAiCheckEstimate();
  });

  $("#ai-check-verdict-filter")?.addEventListener("change", _fetchAiCheckEstimate);

  $("#ai-check-cancel")?.addEventListener("click", () => {
    _aiCheckModal?.classList.add("hidden");
  });
  _aiCheckModal?.addEventListener("click", (e) => {
    if (e.target === _aiCheckModal) _aiCheckModal.classList.add("hidden");
  });

  $("#ai-check-confirm")?.addEventListener("click", async () => {
    const id = state.analyticsRun.id;
    if (!id) return;
    const verdict = $("#ai-check-verdict-filter")?.value || "review";
    _aiCheckModal?.classList.add("hidden");
    try {
      await api(`/api/analytics/runs/${id}/ai_check`, {
        method: "POST",
        body: { verdict },
      });
      // Poll for up to 15 cycles even if the DB status hasn't flipped to
      // "Running" yet — the background thread takes a moment to start.
      state.analyticsRun._aiCheckJustStarted = 15;
      _clearAnalyticsRunPoll();
      state.analyticsRun.poll = setTimeout(fetchAnalyticsRunDetail, 800);
    } catch (e) {
      alert("AI Check failed to start: " + (e.message || e));
    }
  });

  // ==========================================================================
  //  Analytics wizard (#awiz-modal)
  // ==========================================================================
  // Dedicated 4-step wizard for the Analytics tab. This is NOT the CPG
  // wizard — vendor files arrive with junk rows above the real data, so we
  // ask the user to click the header row rather than assuming row 1.
  //
  //  step 1 — Upload file
  //  step 2 — User clicks the row that holds the real headers
  //  step 3 — Map which column is UPC / Item ID / Title / Brand / (ASIN)
  //  step 4 — Name the run, confirm, start
  //
  // state.awiz.origin is one of:
  //   "analytics-vet-existing"  — ASIN column required
  //   "analytics-find-vet"      — no ASIN column; we search Amazon for UPCs
  // --------------------------------------------------------------------------

  state.awiz = null;  // created fresh each openAnalyticsWizard() call

  const awizEl = {
    modal:       $("#awiz-modal"),
    close:       $("#awiz-close"),
    back:        $("#awiz-back"),
    next:        $("#awiz-next"),
    kicker:      $("#awiz-kicker"),
    title:       $("#awiz-title"),
    drop:        $("#awiz-drop"),
    input:       $("#awiz-file-input"),
    fileCard:    $("#awiz-file-card"),
    fileName:    $("#awiz-file-name"),
    fileMeta:    $("#awiz-file-meta"),
    previewTbl:  $("#awiz-preview-table"),
    previewCount:$("#awiz-preview-count"),
    previewTotal:$("#awiz-preview-total"),
    mapHint:     $("#awiz-map-hint"),
    mapUpc:         $("#awiz-map-upc"),
    mapItemId:      $("#awiz-map-itemid"),
    mapTitle:       $("#awiz-map-title"),
    mapSearchTitle: $("#awiz-map-search-title"),
    mapBrand:       $("#awiz-map-brand"),
    mapBrandCol:    $("#awiz-map-brand-col"),
    brandModeBtn:   $("#awiz-brand-mode-btn"),
    brandModeLabel: $("#awiz-brand-mode-label"),
    mapPreview:  $("#awiz-map-preview-table"),
    mapFeedback: $("#awiz-map-feedback"),
    runName:     $("#awiz-run-name"),
    summary:     $("#awiz-summary"),
  };

  function openAnalyticsWizard(analyticsCfg = {}) {
    state.awiz = {
      step: 1,
      analytics: analyticsCfg, // { searchMethods, pagesPerTitle, aiCleanTitles, ... }
      file: null,
      preview: null,           // { rows: [{row_number, cells}], total_rows, max_cols, ... }
      headerRowIdx: null,      // 0-based index into preview.rows
      headers: [],             // cells of the chosen header row (strings)
      mapping: {
        upc: "", itemid: "", title: "", search_title: "",
      },
      brandName: "",           // free-text, applies to every row (text mode)
      brandMode: "text",       // "text" | "col"
      runName: "",
    };

    awizEl.kicker.textContent = "Find & vet ASINs";
    awizEl.title.textContent  = "Upload vendor catalog";
    awizEl.mapHint.textContent= "Tell us which column is which. The UPC / EAN column is the one we'll search for on Amazon.";

    resetAwizUi();
    awizEl.modal.classList.remove("hidden");
    renderAwizStep();
  }

  function closeAwiz() {
    awizEl.modal.classList.add("hidden");
    state.awiz = null;
  }

  function resetAwizUi() {
    awizEl.drop.classList.remove("loaded");
    awizEl.fileCard.classList.add("hidden");
    awizEl.input.value = "";
    awizEl.previewTbl.querySelector("tbody").innerHTML = "";
    awizEl.mapFeedback.textContent = "";
    awizEl.runName.value = "";
    awizEl.summary.innerHTML = "";
    // Reset step indicators
    $$("#awiz-modal .wizard-stepper .step").forEach(el => {
      el.classList.remove("active", "done");
    });
    const first = awizEl.modal.querySelector('[data-awiz-indicator="1"]');
    if (first) first.classList.add("active");
  }

  awizEl.close.addEventListener("click", closeAwiz);

  // ---- Step 1: file picker / drop zone -----------------------------------
  // Reset .value before opening so the `change` event still fires when the
  // user re-picks the same file. Without this, picking the same file twice
  // looks like "nothing happens".
  awizEl.drop.addEventListener("click", () => {
    awizEl.input.value = "";
    awizEl.input.click();
  });
  awizEl.drop.addEventListener("dragover", (e) => {
    e.preventDefault(); awizEl.drop.classList.add("drag");
  });
  awizEl.drop.addEventListener("dragleave", () => awizEl.drop.classList.remove("drag"));
  awizEl.drop.addEventListener("drop", (e) => {
    e.preventDefault();
    awizEl.drop.classList.remove("drag");
    if (e.dataTransfer.files[0]) loadAwizFile(e.dataTransfer.files[0]);
  });
  awizEl.input.addEventListener("change", (e) => {
    if (e.target.files[0]) loadAwizFile(e.target.files[0]);
  });

  async function loadAwizFile(file) {
    if (!state.awiz) return;
    state.awiz.file = file;
    awizEl.drop.classList.add("loaded");
    awizEl.fileCard.classList.remove("hidden");
    awizEl.fileName.textContent = file.name;
    awizEl.fileMeta.textContent = `${fmtKB(file.size)} · parsing…`;

    try {
      const bytes = await file.arrayBuffer();
      const blob = new Blob([bytes], { type: file.type || "application/octet-stream" });
      const fd = new FormData();
      fd.append("catalog_file", blob, file.name);
      const j = await api("/api/analytics/preview", { method: "POST", body: fd, form: true });
      state.awiz.preview = j;
      awizEl.fileMeta.textContent = `${fmtKB(file.size)} · ${j.total_rows || 0} rows · ${j.max_cols || 0} columns`;
      // Best-guess header row: first row with ≥ 3 non-empty cells.
      state.awiz.headerRowIdx = _guessHeaderRow(j.rows);
      state.awiz.runName = file.name.replace(/\.[^.]+$/, "");
      // Auto-advance to "pick header row" — the only sensible next action.
      state.awiz.step = 2;
      renderAwizStep();
    } catch (e) {
      awizEl.fileMeta.textContent = `Could not parse file: ${e.message}`;
      state.awiz.file = null;
      state.awiz.preview = null;
    }
  }

  function _guessHeaderRow(rows) {
    if (!rows || !rows.length) return 0;
    for (let i = 0; i < rows.length; i++) {
      const nonEmpty = (rows[i].cells || []).filter(c => String(c).trim() !== "").length;
      if (nonEmpty >= 3) return i;
    }
    return 0;
  }

  // ---- Navigation --------------------------------------------------------
  awizEl.back.addEventListener("click", () => {
    if (!state.awiz) return;
    if (state.awiz.step > 1) {
      state.awiz.step -= 1;
      renderAwizStep();
    } else {
      closeAwiz();
    }
  });

  awizEl.next.addEventListener("click", async () => {
    if (!state.awiz) return;
    const step = state.awiz.step;

    if (step === 1) {
      if (!state.awiz.file || !state.awiz.preview) {
        alert("Pick a file first — .xlsx, .xls, .csv or .tsv.");
        return;
      }
      state.awiz.step = 2;
      renderAwizStep();
      return;
    }

    if (step === 2) {
      if (state.awiz.headerRowIdx == null) {
        alert("Click the row that holds your column headers.");
        return;
      }
      // Capture headers from the chosen row. Fall back to "Column N" if a
      // cell is blank — it's still selectable in the mapping dropdowns.
      const cells = state.awiz.preview.rows[state.awiz.headerRowIdx].cells || [];
      const maxCols = state.awiz.preview.max_cols || cells.length;
      state.awiz.headers = [];
      for (let i = 0; i < maxCols; i++) {
        const raw = (cells[i] || "").toString().trim();
        state.awiz.headers.push(raw || `Column ${_colLabel(i)}`);
      }
      _bestGuessMapping();
      state.awiz.step = 3;
      renderAwizStep();
      return;
    }

    if (step === 3) {
      const err = _validateMapping();
      if (err) {
        awizEl.mapFeedback.textContent = err;
        return;
      }
      awizEl.mapFeedback.textContent = "";
      state.awiz.step = 4;
      renderAwizStep();
      return;
    }

    // step 4 → start the run
    await submitAwiz();
  });

  // Excel-style column label: 0→A, 25→Z, 26→AA …
  function _colLabel(n) {
    let s = "";
    n = n + 1;
    while (n > 0) {
      const r = (n - 1) % 26;
      s = String.fromCharCode(65 + r) + s;
      n = Math.floor((n - 1) / 26);
    }
    return s;
  }

  // ---- Step renderer -----------------------------------------------------
  function renderAwizStep() {
    const step = state.awiz.step;
    // Update stepper
    $$("#awiz-modal .wizard-stepper .step").forEach(el => {
      const n = Number(el.dataset.awizIndicator);
      el.classList.toggle("active", n === step);
      el.classList.toggle("done",   n <  step);
    });
    // Toggle panels
    $$("#awiz-modal [data-awiz-panel]").forEach(el => {
      el.classList.toggle("hidden", Number(el.dataset.awizPanel) !== step);
    });
    // Back/next button text
    awizEl.back.textContent = step === 1 ? "Cancel" : "Back";
    awizEl.next.textContent = step === 4 ? "Start run" : "Next";

    if (step === 2) renderAwizPreview();
    if (step === 3) renderAwizMapping();
    if (step === 4) renderAwizSummary();
  }

  // ---- Step 2: preview rows (header-row picker) --------------------------
  function renderAwizPreview() {
    const tbody = awizEl.previewTbl.querySelector("tbody");
    const rows = state.awiz.preview?.rows || [];
    const total = state.awiz.preview?.total_rows || 0;
    awizEl.previewCount.textContent = rows.length;
    awizEl.previewTotal.textContent = total;

    tbody.innerHTML = rows.map(r => {
      const selected = r.row_number - 1 === state.awiz.headerRowIdx;
      const cells = (r.cells || []).map(c => `<td title="${escapeHtml(c)}">${escapeHtml(c)}</td>`).join("");
      return `
        <tr data-row-idx="${r.row_number - 1}" class="${selected ? "awiz-row-selected" : ""}">
          <td class="awiz-row-num">${r.row_number}</td>
          ${cells}
        </tr>`;
    }).join("");

    tbody.querySelectorAll("tr").forEach(tr => {
      tr.addEventListener("click", () => {
        const idx = Number(tr.dataset.rowIdx);
        state.awiz.headerRowIdx = idx;
        tbody.querySelectorAll("tr").forEach(row => row.classList.remove("awiz-row-selected"));
        tr.classList.add("awiz-row-selected");
      });
    });
  }

  // ---- Step 3: column mapping --------------------------------------------
  function renderAwizMapping() {
    const opts = [`<option value="">— none —</option>`]
      .concat(state.awiz.headers.map((h, i) =>
        `<option value="${i}">${escapeHtml(h)}</option>`))
      .join("");

    [awizEl.mapUpc, awizEl.mapItemId, awizEl.mapTitle, awizEl.mapSearchTitle]
      .forEach(sel => { sel.innerHTML = opts; });
    awizEl.mapBrandCol.innerHTML = opts;

    // Restore values from state.awiz.mapping (set by _bestGuessMapping or prior Back)
    awizEl.mapUpc.value         = state.awiz.mapping.upc;
    awizEl.mapItemId.value      = state.awiz.mapping.itemid;
    awizEl.mapTitle.value       = state.awiz.mapping.title;
    awizEl.mapSearchTitle.value = state.awiz.mapping.search_title;
    awizEl.mapBrandCol.value    = state.awiz.mapping.brand || "";
    awizEl.mapBrand.value       = state.awiz.brandName || "";

    // Apply the current brand mode to show/hide the right input.
    function _applyBrandMode(mode) {
      state.awiz.brandMode = mode;
      const isCol = mode === "col";
      awizEl.mapBrand.classList.toggle("hidden", isCol);
      awizEl.mapBrandCol.classList.toggle("hidden", !isCol);
      awizEl.brandModeBtn.textContent = isCol ? "type a brand instead" : "use a column instead";
    }
    _applyBrandMode(state.awiz.brandMode);

    awizEl.brandModeBtn.onclick = () => {
      const next = state.awiz.brandMode === "text" ? "col" : "text";
      if (next === "col") {
        // Switching to column — clear typed brand from mapping
        delete state.awiz.mapping.brand;
      } else {
        // Switching to text — clear column mapping
        delete state.awiz.mapping.brand;
        awizEl.mapBrandCol.value = "";
      }
      _applyBrandMode(next);
      renderAwizMappingPreview();
    };

    const wireSelect = (sel, key) => {
      sel.onchange = () => {
        state.awiz.mapping[key] = sel.value;
        renderAwizMappingPreview();
      };
    };
    wireSelect(awizEl.mapUpc,         "upc");
    wireSelect(awizEl.mapItemId,      "itemid");
    wireSelect(awizEl.mapTitle,       "title");
    wireSelect(awizEl.mapSearchTitle, "search_title");
    wireSelect(awizEl.mapBrandCol,    "brand");

    awizEl.mapBrand.oninput = () => {
      state.awiz.brandName = awizEl.mapBrand.value;
    };

    renderAwizMappingPreview();
  }

  // Sample preview on the mapping step: the chosen header row + next 5 rows.
  // Columns assigned to UPC / Item ID / Title get a tinted background so the
  // user can eyeball whether they picked the right ones before hitting Next.
  function renderAwizMappingPreview() {
    const tbody = awizEl.mapPreview?.querySelector("tbody");
    if (!tbody || !state.awiz.preview) return;
    const rows = state.awiz.preview.rows || [];
    const maxCols = state.awiz.preview.max_cols || state.awiz.headers.length;
    const headerIdx = state.awiz.headerRowIdx ?? 0;

    // Collect the header row and up to 5 data rows after it.
    const slice = [];
    const headerRow = rows[headerIdx];
    if (headerRow) slice.push({ r: headerRow, isHeader: true });
    for (let i = headerIdx + 1; i < rows.length && slice.length < 6; i++) {
      slice.push({ r: rows[i], isHeader: false });
    }

    const classFor = (colIdx) => {
      const m = state.awiz.mapping;
      if (String(colIdx) === String(m.upc))          return "awiz-col-mapped-upc";
      if (String(colIdx) === String(m.itemid))       return "awiz-col-mapped-itemid";
      if (String(colIdx) === String(m.title))        return "awiz-col-mapped-title";
      if (m.search_title && String(colIdx) === String(m.search_title)) return "awiz-col-mapped-search-title";
      return "";
    };

    tbody.innerHTML = slice.map(({ r, isHeader }) => {
      const cells = [];
      for (let i = 0; i < maxCols; i++) {
        const raw = (r.cells[i] == null ? "" : String(r.cells[i]));
        const cls = classFor(i);
        cells.push(`<td class="${cls}" title="${escapeHtml(raw)}">${escapeHtml(raw)}</td>`);
      }
      return `
        <tr class="${isHeader ? "awiz-row-header" : ""}">
          <td class="awiz-row-num">${r.row_number}</td>
          ${cells.join("")}
        </tr>`;
    }).join("");
  }

  function _bestGuessMapping() {
    const m = state.awiz.mapping;
    const norm = (s) => (s || "").toString().toLowerCase().replace(/[^a-z0-9]+/g, "");
    const hit = (re) => {
      for (let i = 0; i < state.awiz.headers.length; i++) {
        if (re.test(norm(state.awiz.headers[i]))) return String(i);
      }
      return "";
    };
    if (!m.upc)    m.upc    = hit(/^(upc|ean|gtin|barcode)/);
    if (!m.itemid) m.itemid = hit(/(itemid|itemnum|partnum|partno|sku|manuf|mpn|model)/);
    if (!m.title)  m.title  = hit(/(title|desc|product|name)/);
    // Auto-select brand column and switch to column mode if header found.
    if (!m.brand) {
      const brandHit = hit(/^brand$/);
      if (brandHit) {
        m.brand = brandHit;
        state.awiz.brandMode = "col";
      }
    }
  }

  function _validateMapping() {
    const m = state.awiz.mapping;
    if (!m.upc)   return "UPC / EAN column is required — it's the one we search on Amazon.";
    if (!m.title) return "Vendor Title column is required for scoring.";
    return "";
  }

  // ---- Step 4: summary card ---------------------------------------------
  function renderAwizSummary() {
    _updateAiCostHint();
    if (!awizEl.runName.value) awizEl.runName.value = state.awiz.runName || "";
    awizEl.runName.oninput = () => { state.awiz.runName = awizEl.runName.value; };

    const h = state.awiz.headers;
    const colName = (idx) => {
      if (idx === "" || idx == null) return "—";
      return h[Number(idx)] || `Column ${_colLabel(Number(idx))}`;
    };

    const rows = [
      ["File", state.awiz.file?.name || "—"],
      ["Header row", String((state.awiz.headerRowIdx ?? 0) + 1)],
      ["Marketplace", "US"],
      ["UPC / EAN column", colName(state.awiz.mapping.upc)],
      ["Item ID column", colName(state.awiz.mapping.itemid)],
      ["Title column (scoring)", colName(state.awiz.mapping.title)],
      ...(state.awiz.mapping.search_title ? [["Search Title column (Amazon search)", colName(state.awiz.mapping.search_title)]] : []),
      ["Brand (applied to every row)", state.awiz.brandName?.trim() || "—"],
    ];

    awizEl.summary.innerHTML = rows.map(([k, v]) => `
      <div class="awiz-summary-row">
        <span class="awiz-summary-label">${escapeHtml(k)}</span>
        <span class="awiz-summary-value">${escapeHtml(v)}</span>
      </div>
    `).join("");
  }

  // ---- Submit: start the run --------------------------------------------
  async function submitAwiz() {
    // POST the uploaded file + wizard decisions to /api/analytics/runs.
    // The endpoint parses the file using the chosen header row + column
    // mapping, writes the catalog rows to analytics_catalog_rows, and
    // kicks off a background 3-tier SP-API search (UPC / ItemID / Title).
    // The response gives us the new run_id so we can reload the history.
    if (!state.awiz.file) {
      alert("No file attached — please go back to step 1.");
      return;
    }
    const name = (awizEl.runName.value || state.awiz.runName || "Untitled run").trim();

    // Search methods now live in wizard step 4 (not on the Analytics card).
    const methods = [];
    if ($("#awiz-search-upc")?.checked)    methods.push("UPC");
    if ($("#awiz-search-itemid")?.checked) methods.push("ItemID");
    if ($("#awiz-search-title")?.checked)  methods.push("Title");
    if (methods.length === 0) {
      alert("Pick at least one search method (UPC / Item ID / Title).");
      return;
    }
    const pagesPerTitle = Math.max(1, Math.min(10, parseInt($("#awiz-title-pages")?.value || "1", 10)));
    const aiCleanTitles = !!$("#awiz-title-ai-clean")?.checked;

    const fd = new FormData();
    fd.append("catalog_file", state.awiz.file);
    fd.append("name", name);
    fd.append("marketplace", "US");
    fd.append("header_row", String(state.awiz.headerRowIdx ?? 0));
    fd.append("mapping", JSON.stringify(state.awiz.mapping || {}));
    // In col mode the brand column index is already in mapping.brand;
    // send empty string for the free-text brand field.
    fd.append("brand", state.awiz.brandMode === "col" ? "" : (state.awiz.brandName || "").trim());
    fd.append("search_methods", JSON.stringify(methods));
    fd.append("pages_per_title", String(pagesPerTitle));
    fd.append("ai_clean_titles", aiCleanTitles ? "true" : "false");
    const minRankWiz = Math.max(0, parseInt($("#awiz-min-rank")?.value || "0", 10) || 0);
    const maxRankWiz = Math.max(0, parseInt($("#awiz-max-rank")?.value || "0", 10) || 0);
    fd.append("min_rank", String(minRankWiz));
    fd.append("max_rank", String(maxRankWiz));

    awizEl.next.disabled = true;
    awizEl.next.textContent = "Starting…";
    try {
      const result = await api("/api/analytics/runs", { method: "POST", body: fd, form: true });
      console.log("[awiz] run created", result);
      if (result?.sp_api_configured === false) {
        alert(
          "Run created, but SP-API credentials are not configured. " +
          "Add AMZ_CLIENT_ID / AMZ_CLIENT_SECRET / REFRESH_TOKEN to .env and restart " +
          "the server to actually search Amazon."
        );
      }
      closeAwiz();
      // Jump straight to the detail view so the user sees the progress bar.
      if (result?.run_id) {
        openAnalyticsRunDetail(result.run_id);
      } else {
        loadAnalyticsRuns();
      }
    } catch (e) {
      alert(`Could not start run: ${e.message || e}`);
    } finally {
      awizEl.next.disabled = false;
      awizEl.next.textContent = "Start run";
    }
  }

  // ---- Card button — the single Analytics entry point ------------------
  // The card no longer carries the search-method checkboxes — they're in
  // wizard step 4. The button just opens the wizard in its default config.
  $("#analytics-find-vet-btn")?.addEventListener("click", () => {
    openAnalyticsWizard({});
    // Reset wizard step-4 checkboxes to default: UPC + ItemID checked, Title unchecked.
    // (We only touch the Title-options panel visibility here; submitAwiz reads
    // the actual checkbox state.)
    const ti = $("#awiz-search-title");
    const op = $("#awiz-title-opts");
    if (ti && op) op.classList.toggle("hidden", !ti.checked);
  });

})();
