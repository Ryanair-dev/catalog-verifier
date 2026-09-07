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

  // ---------- Animation helpers -------------------------------------------

  /**
   * Staggered entrance animation for stat value cards.
   * Each .summary-stat-value fades in and rises from 20px below, staggered
   * by 80ms per card so they ripple in left-to-right.
   * Safe to call on every poll — only runs when values actually changed or
   * the caller passes force=true.
   */
  function _animateStats(container) {
    const els = (container || document).querySelectorAll(".summary-stat-value");
    els.forEach((el, i) => {
      el.style.opacity   = "0";
      el.style.transform = "translateY(20px)";
      setTimeout(() => {
        el.style.opacity   = "1";
        el.style.transform = "translateY(0)";
      }, 80 + i * 80);
    });
  }

  /**
   * Animate progress bar fills from 0 → their target width so the CSS
   * transition (width 0.9s ease) plays visibly instead of jumping.
   * Reads the inline style already set on the element, resets to 0%,
   * then restores after one paint tick.
   */
  function _animateProgressBars(container) {
    const fills = (container || document).querySelectorAll(
      ".rp-fill, .analytics-run-inline-bar .fill, .confidence-bar .fill, .progress .bar"
    );
    fills.forEach(el => {
      const target = el.style.width || "0%";
      el.style.transition = "none";
      el.style.width = "0%";
      // Force a reflow so the browser registers the 0% before restoring
      void el.offsetWidth;
      el.style.transition = "";
      el.style.width = target;
    });
  }

  // ---------- Toast notifications -----------------------------------------
  // Usage: showToast("message")                          — info (blue)
  //        showToast("message", "success")               — green
  //        showToast("message", "error")                 — red
  //        showToast("message", "warning")               — amber
  //        showToast(<html string>, "success", 8000)     — custom duration ms
  (function _initToastContainer() {
    if (document.getElementById("toast-stack")) return;
    const el = document.createElement("div");
    el.id = "toast-stack";
    document.body.appendChild(el);
  })();

  function showToast(message, type = "info", duration = 4500) {
    const stack = document.getElementById("toast-stack");
    if (!stack) { console.warn("[toast]", message); return; }

    const icons = {
      success: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>`,
      error:   `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`,
      warning: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`,
      info:    `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`,
    };

    const t = document.createElement("div");
    t.className = `toast toast-${type}`;
    // Message goes in via textContent (never innerHTML) — toast text routinely
    // echoes server errors and uploaded-file content, which must not execute.
    t.innerHTML = `
      <span class="toast-icon">${icons[type] || icons.info}</span>
      <span class="toast-msg"></span>
      <button class="toast-close" aria-label="Dismiss">×</button>`;
    t.querySelector(".toast-msg").textContent = String(message);

    t.querySelector(".toast-close").addEventListener("click", () => _dismissToast(t));
    stack.appendChild(t);

    // Trigger entrance animation — double RAF ensures the browser has
    // painted the element before adding the class, so the CSS transition fires.
    requestAnimationFrame(() => requestAnimationFrame(() => t.classList.add("toast-visible")));

    const timer = setTimeout(() => _dismissToast(t), duration);
    t._toastTimer = timer;
  }

  function _dismissToast(t) {
    clearTimeout(t._toastTimer);
    t.classList.remove("toast-visible");
    t.classList.add("toast-hiding");
    // Safety net: if transitionend never fires (e.g. hidden tab, no animation),
    // remove the element after 600 ms so it never gets stuck.
    const fallback = setTimeout(() => t.remove(), 600);
    t.addEventListener("transitionend", () => { clearTimeout(fallback); t.remove(); }, { once: true });
  }

  // Promise-based confirmation modal — replaces window.confirm() so destructive
  // actions use the app's styled modal instead of the browser's native dialog.
  // Resolves true on confirm, false on Cancel / Esc / backdrop click.
  //   await showConfirm({ title, message, confirmText, cancelText, danger })
  function showConfirm({ title = "Are you sure?", message = "", confirmText = "Confirm",
                         cancelText = "Cancel", danger = true } = {}) {
    return new Promise(resolve => {
      const accentBg = danger ? "#fee2e2" : "#ede9fe";
      const accentFg = danger ? "var(--red-500)" : "var(--purple-600)";
      const okClass  = danger ? "btn btn-danger" : "btn btn-primary";

      const backdrop = document.createElement("div");
      backdrop.className = "modal-backdrop";
      backdrop.style.cssText = "align-items:center;justify-content:center;padding:0;z-index:200;";
      backdrop.innerHTML = `
        <div class="bg-white rounded-xl w-[440px] p-6 shadow-xl" role="dialog" aria-modal="true" style="box-shadow:0 24px 60px rgba(0,0,0,0.28);">
          <div class="flex items-start gap-3 mb-3">
            <div class="w-10 h-10 rounded-lg flex items-center justify-center" style="background:${accentBg};color:${accentFg};flex:0 0 auto;">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/></svg>
            </div>
            <div class="flex-1 min-w-0">
              <div class="cf-title font-semibold" style="color:var(--navy-800);"></div>
              <div class="cf-msg text-sm mt-1" style="color:#6b7480;white-space:pre-line;"></div>
            </div>
          </div>
          <div class="flex justify-end gap-2 mt-4">
            <button class="btn btn-secondary cf-cancel"></button>
            <button class="${okClass} cf-ok"></button>
          </div>
        </div>`;
      // Text set via textContent (never innerHTML) — titles/messages include run
      // and file names (user/Amazon data) which must not be interpreted as markup.
      backdrop.querySelector(".cf-title").textContent  = title;
      backdrop.querySelector(".cf-msg").textContent    = message;
      backdrop.querySelector(".cf-cancel").textContent = cancelText;
      backdrop.querySelector(".cf-ok").textContent     = confirmText;
      if (!message) backdrop.querySelector(".cf-msg").style.display = "none";

      let done = false;
      function close(result) {
        if (done) return;
        done = true;
        document.removeEventListener("keydown", onKey);
        backdrop.remove();
        resolve(result);
      }
      function onKey(e) {
        if (e.key === "Escape")    { e.preventDefault(); close(false); }
        else if (e.key === "Enter") { e.preventDefault(); close(true); }
      }
      backdrop.querySelector(".cf-cancel").addEventListener("click", () => close(false));
      backdrop.querySelector(".cf-ok").addEventListener("click", () => close(true));
      // Click on the dark backdrop (not the card) cancels.
      backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(false); });
      document.addEventListener("keydown", onKey);
      document.body.appendChild(backdrop);
      backdrop.querySelector(".cf-ok").focus();
    });
  }

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

    // Library panel active tab
    libActiveType: "abbreviations",  // "abbreviations" | "brands" | "manufacturers"

    // Brand Analytics
    brandAnalytics: {
      runs: [],
      runsPoll: null,
      run: {
        id: null,
        data: null,
        poll: null,
        page: 1,
        pageSize: 50,
        search: "",
        sortKey: "bsr",
        sortDir: "asc",
      },
      library: [],
      wizard: {
        step: 1,
        searchType: "brand",
        vettingMode: "cpg",
        inputName: "",
        discoveredBrands: [],  // [{name, selected}]
        minRank: 0,
        maxRank: 0,
        minSold: 0,
        maxSold: 0,
        pagesPerBrand: 10,
        cacheInfo: null,
        saveToLibrary: true,
      },
    },

    // Eligibility Checker
    eligibility: {
      jobId: null,
      poll: null,
      jobStatus: null,   // null | "running" | "complete" | "error"
      downloaded: false,
    },

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

    // Map each view name to its DOM element
    const viewMap = {
      "cpg":            $("#view-cpg"),
      "history":        $("#view-history"),
      "scan":           $("#view-scan"),
      "pairs":          $("#view-pairs"),
      "analytics":      $("#view-analytics"),
      "analytics-run":  $("#view-analytics-run"),
      "brand-analytics":     $("#view-brand-analytics"),
      "brand-analytics-run": $("#view-brand-analytics-run"),
      "quick-search":   $("#view-quick-search"),
      "create-po":      $("#view-create-po"),
      "vendor-offers":  $("#view-vendor-offers"),
    };

    // Views that are never shown directly via switchView (only via openXxxDetail)
    const detailOnly = new Set(["analytics-run", "brand-analytics-run"]);

    Object.entries(viewMap).forEach(([name, el]) => {
      if (!el) return;
      if (detailOnly.has(name)) {
        el.classList.add("hidden");
        return;
      }
      if (name === view) {
        el.classList.remove("hidden");
        // Trigger entrance animation — remove first in case it's still running
        el.classList.remove("view-enter-anim");
        void el.offsetWidth;  // force reflow
        el.classList.add("view-enter-anim");
        el.addEventListener("animationend", () => el.classList.remove("view-enter-anim"), { once: true });
      } else {
        el.classList.add("hidden");
      }
    });

    if (view === "pairs") $("#pairs-search").focus();
    if (view === "history") loadHistory();
    if (view === "analytics") loadAnalyticsRuns();
    if (view === "brand-analytics") loadBrandAnalyticsRuns();
    if (view === "create-po") initCreatePo();
    if (view === "vendor-offers") initVendorOffers();
    // Tear down detail-view poll when leaving Analytics.
    if (view !== "analytics" && typeof _clearAnalyticsRunPoll === "function") {
      try { _clearAnalyticsRunPoll(); } catch {}
    }
    if (view !== "brand-analytics") _clearBARPoll();
  }

  // ========================================================================
  //  Create PO — rendered by the standalone module in /static/create_po.js
  // ========================================================================
  // Vendor Offers (Price Desk) is a self-contained page served with its data
  // inlined; embed it in an iframe, loaded lazily on first open. The page's own
  // "Refresh" button reloads it after an upload/assign.
  function initVendorOffers() {
    const f = document.getElementById("vendor-offers-frame");
    if (f && !f.getAttribute("src")) f.setAttribute("src", "/api/vendor-offers/page");
  }

  function initCreatePo() {
    const el = document.getElementById("view-create-po");
    if (el && window.CreatePO) window.CreatePO.mount(el);
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
      switchView("scan", null);
      if (j.scan?.match_from_keepa) {
        // Load candidates for match-mode scans
        const jc = await api(`/api/scans/${scanId}/candidates`);
        state.candidates = jc.candidates || [];
        renderScanView();
        if (state.candidates.length > 0) renderCandidatesView();
      } else {
        state.candidates = [];
        renderScanView();
      }
    } catch (e) {
      showToast("Could not open scan: " + e.message, "error");
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
    // Match-mode badge
    let matchBadge = $("#scan-match-badge");
    if (!matchBadge) {
      matchBadge = document.createElement("span");
      matchBadge.id = "scan-match-badge";
      matchBadge.style.cssText = "margin-left:8px;padding:2px 9px;font-size:11px;font-weight:700;border-radius:12px;background:#ede9fe;color:#5b21b6;vertical-align:middle;";
      matchBadge.textContent = "Match from Keepa";
      $("#scan-ai-badge")?.parentNode?.appendChild(matchBadge);
    }
    matchBadge.classList.toggle("hidden", !s.match_from_keepa);

    // Banners
    $("#scan-pending-banner").classList.toggle("hidden", s.status !== "pending");
    $("#scan-ready-banner").classList.toggle("hidden",   s.status !== "ready");

    // Progress section
    $("#scan-progress-section").classList.toggle("hidden", s.status !== "verifying");

    const isMatchMode = !!s.match_from_keepa;
    const hasCandidates = isMatchMode && (state.candidates || []).length > 0 && String(s.status).startsWith("verified");
    const hasResults = !isMatchMode && state.results.length > 0 && String(s.status).startsWith("verified");

    // Candidates section (match mode)
    let candSection = $("#scan-candidates-section");
    if (!candSection) {
      candSection = document.createElement("div");
      candSection.id = "scan-candidates-section";
      candSection.className = "hidden";
      const refSection = $("#scan-summary-section");
      refSection?.parentNode?.insertBefore(candSection, refSection);
    }
    candSection.classList.toggle("hidden", !hasCandidates);

    // Summary + results (normal mode)
    $("#scan-summary-section").classList.toggle("hidden", !hasResults);
    $("#scan-results-section").classList.toggle("hidden", !hasResults);
    if (hasResults) {
      renderSummary();
      renderResults();
    }

    $("#scan-review-btn").disabled  = !hasResults;
    $("#scan-export-btn").disabled  = !hasResults;
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
    const isMatchMode = !!s.match_from_keepa;
    updateProgress(0, s.catalog_count || 1, isMatchMode ? "Matching from Keepa…" : "Scoring products…");
    startEta(s.catalog_count || 1, s.ai_mode ? "ai" : "rule");

    try {
      let j;
      if (isMatchMode) {
        j = await api(`/api/scans/${s.id}/match`, { method: "POST" });
        state.scan = j.scan;
        state.candidates = j.candidates || [];
        state.results = [];
        if (j.thresholds) state.thresholds = j.thresholds;
        renderScanView();
        renderCandidatesView();
        return;
      }
      j = await api(`/api/scans/${s.id}/verify`, { method: "POST" });
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
      showToast("Verification failed: " + err.message, "error");
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

  // ---------- Candidates view (match-from-Keepa mode) ---------------------
  function renderCandidatesView() {
    const section = $("#scan-candidates-section");
    if (!section) return;
    section.classList.remove("hidden");

    const candidates = state.candidates || [];
    const scan = state.scan;

    // Group by row_idx
    const byRow = {};
    candidates.forEach(c => {
      const idx = c.row_idx;
      if (!byRow[idx]) byRow[idx] = [];
      byRow[idx].push(c);
    });

    const rowIndices = Object.keys(byRow).map(Number).sort((a, b) => a - b);
    const totalRows  = rowIndices.length;
    const approved   = candidates.filter(c => c.review_status === "Approved").length;
    const pending    = rowIndices.filter(i => !byRow[i].some(c => c.review_status === "Approved")).length;
    const methods    = (scan?.match_methods || []).join(", ") || "upc, item_id, title";

    section.innerHTML = `
      <div style="padding:16px 0 8px;">
        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:12px;">
          <div>
            <span style="font-size:15px;font-weight:700;color:#1e293b;">Candidate Matches</span>
            <span style="margin-left:10px;font-size:12px;color:#64748b;">Methods: ${escapeHtml(methods)}</span>
          </div>
          <div style="display:flex;gap:12px;font-size:12px;color:#64748b;">
            <span><b style="color:#166534;">${approved}</b> approved</span>
            <span><b style="color:#92400e;">${pending}</b> pending review</span>
            <span><b style="color:#374151;">${totalRows}</b> catalog rows</span>
          </div>
        </div>
        <div id="candidates-table-wrap" style="border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;">
          <table style="width:100%;border-collapse:collapse;font-size:13px;">
            <thead>
              <tr style="background:#f8fafc;border-bottom:1px solid #e2e8f0;">
                <th style="padding:8px 12px;text-align:left;color:#64748b;font-weight:600;white-space:nowrap;">#</th>
                <th style="padding:8px 12px;text-align:left;color:#64748b;font-weight:600;">Vendor Title</th>
                <th style="padding:8px 12px;text-align:left;color:#64748b;font-weight:600;">ASIN</th>
                <th style="padding:8px 12px;text-align:left;color:#64748b;font-weight:600;">Amazon Title</th>
                <th style="padding:8px 12px;text-align:center;color:#64748b;font-weight:600;">Method</th>
                <th style="padding:8px 12px;text-align:center;color:#64748b;font-weight:600;">Conf.</th>
                <th style="padding:8px 12px;text-align:center;color:#64748b;font-weight:600;">Status</th>
                <th style="padding:8px 12px;text-align:center;color:#64748b;font-weight:600;">Action</th>
              </tr>
            </thead>
            <tbody id="candidates-tbody">
            </tbody>
          </table>
        </div>
      </div>`;

    _renderCandidateRows(byRow, rowIndices);
  }

  function _renderCandidateRows(byRow, rowIndices) {
    const tbody = $("#candidates-tbody");
    if (!tbody) return;

    const rows = [];
    rowIndices.forEach((rowIdx, i) => {
      const group = byRow[rowIdx] || [];
      const hasApproved = group.some(c => c.review_status === "Approved");
      const vendorTitle = group[0]?.Title || "";

      group.forEach((cand, ci) => {
        const isApproved  = cand.review_status === "Approved";
        const isDiscarded = cand.review_status === "Discarded";
        const isNoMatch   = !cand.asin;
        const conf = Math.round(cand.Confidence || cand.confidence || 0);
        const confColor = conf >= 85 ? "#166534" : conf >= 35 ? "#92400e" : "#b91c1c";
        const methodLabel = { upc: "UPC", item_id: "MPN", title: "Title", none: "—" }[cand.match_method] || cand.match_method || "—";
        const rowBg = isApproved ? "#f0fdf4" : isDiscarded ? "#fafafa" : (ci === 0 && !hasApproved) ? "#fff" : "#fafafa";
        const opacity = isDiscarded ? "0.45" : "1";

        const statusHtml = isApproved
          ? `<span style="padding:2px 8px;border-radius:10px;background:#dcfce7;color:#166534;font-size:11px;font-weight:700;">Approved</span>`
          : isDiscarded
          ? `<span style="padding:2px 8px;border-radius:10px;background:#f1f5f9;color:#94a3b8;font-size:11px;font-weight:700;">Discarded</span>`
          : isNoMatch
          ? `<span style="padding:2px 8px;border-radius:10px;background:#fef2f2;color:#b91c1c;font-size:11px;font-weight:700;">No match</span>`
          : `<span style="padding:2px 8px;border-radius:10px;background:#fef9c3;color:#713f12;font-size:11px;font-weight:700;">Pending</span>`;

        const actionHtml = isNoMatch ? `<span style="color:#94a3b8;font-size:11px;">—</span>`
          : isApproved ? `<button class="btn-cand-discard text-xs" data-cand-id="${cand._cand_id}" style="padding:3px 10px;border-radius:6px;border:1px solid #e2e8f0;background:#fff;color:#64748b;cursor:pointer;font-size:11px;">Undo</button>`
          : isDiscarded ? `<button class="btn-cand-approve text-xs" data-cand-id="${cand._cand_id}" style="padding:3px 10px;border-radius:6px;border:1px solid #bbf7d0;background:#f0fdf4;color:#166534;cursor:pointer;font-size:11px;font-weight:600;">Approve</button>`
          : `<div style="display:flex;gap:4px;justify-content:center;">
               <button class="btn-cand-approve" data-cand-id="${cand._cand_id}" style="padding:3px 10px;border-radius:6px;border:1px solid #bbf7d0;background:#f0fdf4;color:#166534;cursor:pointer;font-size:11px;font-weight:600;">Approve</button>
               <button class="btn-cand-discard" data-cand-id="${cand._cand_id}" style="padding:3px 10px;border-radius:6px;border:1px solid #fecaca;background:#fef2f2;color:#b91c1c;cursor:pointer;font-size:11px;font-weight:600;">Discard</button>
             </div>`;

        // Show vendor title only on first candidate of each row group
        const titleCell = ci === 0
          ? `<td style="padding:8px 12px;color:#1e293b;font-weight:500;max-width:180px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" rowspan="${group.length}">${escapeHtml(vendorTitle)}</td>`
          : "";
        const rowNumCell = ci === 0
          ? `<td style="padding:8px 12px;color:#94a3b8;font-size:11px;text-align:center;" rowspan="${group.length}">${rowIdx + 1}</td>`
          : "";

        rows.push(`<tr style="background:${rowBg};opacity:${opacity};border-bottom:1px solid #e2e8f0;">
          ${rowNumCell}
          ${titleCell}
          <td style="padding:8px 12px;font-family:monospace;font-size:11px;color:#4b5563;white-space:nowrap;">${escapeHtml(cand.asin || "—")}</td>
          <td style="padding:8px 12px;color:#374151;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(cand.AmzTitle || "")}</td>
          <td style="padding:8px 12px;text-align:center;"><span style="padding:2px 7px;border-radius:8px;background:#ede9fe;color:#5b21b6;font-size:10px;font-weight:700;">${escapeHtml(methodLabel)}</span></td>
          <td style="padding:8px 12px;text-align:center;font-weight:700;color:${confColor};">${isNoMatch ? "—" : conf + "%"}</td>
          <td style="padding:8px 12px;text-align:center;">${statusHtml}</td>
          <td style="padding:8px 12px;text-align:center;">${actionHtml}</td>
        </tr>`);
      });
    });

    tbody.innerHTML = rows.join("");

    // Wire approve/discard buttons
    tbody.querySelectorAll(".btn-cand-approve").forEach(btn => {
      btn.addEventListener("click", async () => {
        const candId = Number(btn.dataset.candId);
        await _setCandidateVerdict(candId, "Approved");
      });
    });
    tbody.querySelectorAll(".btn-cand-discard").forEach(btn => {
      btn.addEventListener("click", async () => {
        const candId = Number(btn.dataset.candId);
        await _setCandidateVerdict(candId, "Not Approved");
      });
    });
  }

  async function _setCandidateVerdict(candId, verdict) {
    if (!state.scan) return;
    try {
      const j = await api(`/api/scans/${state.scan.id}/candidates/${candId}/verdict`, {
        method: "POST",
        body: JSON.stringify({ verdict, review_status: verdict === "Approved" ? "Approved" : "Discarded" }),
      });
      // Refresh candidates from server
      const jc = await api(`/api/scans/${state.scan.id}/candidates`);
      state.candidates = jc.candidates || [];
      renderCandidatesView();
    } catch (e) {
      showToast("Could not update candidate: " + e.message, "error");
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
      showToast("Could not apply suggestion: " + e.message, "error");
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
        <td>${statusTag}</td>
        <td style="max-width:220px;">${buildAiSuggestionCell(row, realIdx)}</td>
        <td class="text-xs" style="max-width:200px;color:#6b7480;">${escapeHtml(reasonText)}</td>
        ${signalCell(s.upc)}${itemIdMpnCell(row, s.item_id)}${signalCell(s.brand)}${signalCell(s.title)}
        <td class="text-center font-mono text-xs">${row.amz_pack == null ? '—' : row.amz_pack}</td>
        <td>${barcodeHTML}</td>
        <td>${verdictBadge}</td>
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
        <span class="signal-score ${tone}">${Math.round(s.score)}%</span>
        <span class="text-[11px]" title="${escapeHtml(s.detail || '')}">${escapeHtml((s.detail || "").slice(0, 50))}</span>
      </div>
    </td>`;
  }

  function itemIdMpnCell(row, s) {
    const catId = escapeHtml(row.ItemID || '—');
    let amzMpn = '';
    if (s && s.detail) {
      const m = s.detail.match(/^(?:Exact|Variant|Fuzzy):\s*(.+?)(?:\s+\(\d+%\))?$/);
      if (m) amzMpn = escapeHtml(m[1]);
    }
    const tone = s ? signalTone(s) : '';
    const scoreBadge = s ? `<span class="signal-score ${tone}">${Math.round(s.score)}%</span>` : '';
    return `<td>
      <div class="signal-cell">
        ${scoreBadge}
        <div class="text-[11px]" style="color:#6b7480;line-height:1.4;">
          <span style="font-weight:500;">Cat:</span> ${catId}
          ${amzMpn ? `<br><span style="font-weight:500;">Amz:</span> ${amzMpn}` : ''}
        </div>
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
      fdScan.append("match_from_keepa", cpg.matchFromKeepa ? "true" : "false");
      fdScan.append("match_methods", JSON.stringify(cpg.matchMethods || ["upc", "item_id", "title"]));
      const jScan = await api("/api/scans", { method: "POST", body: fdScan, form: true });
      const scanId = jScan.scan.id;

      // 2) Attach Amazon / Keepa data (status → ready)
      updateProgress(0, 1, "Attaching Amazon data…");
      const fdAmz = new FormData();
      fdAmz.append("amazon_file", cpg.amazonFile);
      fdAmz.append("amazon_source", cpg.amazonSource);
      await api(`/api/scans/${scanId}/amazon`, { method: "POST", body: fdAmz, form: true });

      // 3) Run verification or match pipeline
      const nRows = jScan.scan.catalog_count || 1;
      updateProgress(0, nRows, cpg.matchFromKeepa ? "Matching from Keepa…" : "Scoring products…");
      startEta(nRows, cpg.aiMode ? "ai" : "rule");

      let jVer;
      if (cpg.matchFromKeepa) {
        jVer = await api(`/api/scans/${scanId}/match`, { method: "POST" });
        state.scan = jVer.scan;
        state.candidates = jVer.candidates || [];
        state.results = [];
        if (jVer.thresholds) state.thresholds = jVer.thresholds;
        // Show candidates view instead of results table
        renderCandidatesView();
      } else {
        startEta(nRows, cpg.aiMode ? "ai" : "rule");
        jVer = await api(`/api/scans/${scanId}/verify`, { method: "POST" });
        state.scan = jVer.scan;
        state.results = (jVer.results || []).map(r => ({
          ...r, barcode_db: null, barcode_loading: true,
        }));
        if (jVer.thresholds) state.thresholds = jVer.thresholds;
        if (Array.isArray(jVer.ai_added)) jVer.ai_added.forEach(a => showToast(a));
      }

      state.resultsPage = 1;
      if (!cpg.matchFromKeepa) {
        $("#summary-section")?.classList.remove("hidden");
        $("#results-table-wrap")?.classList.remove("hidden");
        renderSummary();
        renderResults();
      }

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
      showToast("Verification failed: " + err.message, "error");
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
    brandCol: $("#map-brand-col"),
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
        brand: "", brand_col: "", upc_col: "", item_id_col: "",
        title_col: "", asin_col: "", attr_cols: [],
      },
      brandMode: "text",
      name: "", ai_mode: false,
      matchFromKeepa: false,
      matchMethods: ["upc", "item_id", "title"],
    };
    wizEl.drop.classList.remove("loaded");
    wizEl.fileCard.classList.add("hidden");
    wizEl.input.value = "";
    wizEl.brand.value = "";
    wizEl.brand.classList.remove("hidden");
    wizEl.brandCol?.classList.add("hidden");
    document.querySelectorAll(".map-brand-mode-btn").forEach(b => {
      const active = b.dataset.mode === "text";
      b.style.background = active ? "#3b82f6" : "#f8fafc";
      b.style.color      = active ? "#fff"    : "#64748b";
      b.classList.toggle("active", active);
    });
    $("#map-brand-note").textContent = "Free text. Leave empty if your catalog mixes brands and Amazon's brand field is trustworthy.";
    wizEl.detailName.value = "";
    wizEl.detailAI.checked = false;
    // Reset match-from-Keepa toggle
    const matchToggle = $("#map-match-toggle");
    if (matchToggle) matchToggle.checked = false;
    $("#map-match-methods")?.classList.add("hidden");
  }

  wizEl.close.addEventListener("click", closeWizard);

  // Brand mode toggle (free text vs column picker)
  document.querySelectorAll(".map-brand-mode-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.mode;
      state.wizard.brandMode = mode;
      document.querySelectorAll(".map-brand-mode-btn").forEach(b => {
        const active = b.dataset.mode === mode;
        b.style.background = active ? "#3b82f6" : "#f8fafc";
        b.style.color      = active ? "#fff"    : "#64748b";
        b.classList.toggle("active", active);
      });
      wizEl.brand.classList.toggle("hidden", mode === "col");
      wizEl.brandCol?.classList.toggle("hidden", mode !== "col");
      const note = $("#map-brand-note");
      if (note) note.textContent = mode === "col"
        ? "Select the column that contains the brand name for each row."
        : "Free text. Leave empty if your catalog mixes brands and Amazon's brand field is trustworthy.";
      if (mode === "col") populateBrandColSelect();
    });
  });

  // Match-from-Keepa toggle
  $("#map-match-toggle")?.addEventListener("change", (e) => {
    state.wizard.matchFromKeepa = e.target.checked;
    $("#map-match-methods")?.classList.toggle("hidden", !e.target.checked);
    // When toggled on, ASIN column becomes irrelevant — clear it
    if (e.target.checked) {
      state.wizard.mapping.asin_col = "";
      if (wizEl.asin) wizEl.asin.value = "";
    }
    updateWizardNextState();
  });
  ["#match-method-upc", "#match-method-item-id", "#match-method-title"].forEach(sel => {
    $(sel)?.addEventListener("change", () => {
      state.wizard.matchMethods = [
        ...( $("#match-method-upc")?.checked     ? ["upc"]     : [] ),
        ...( $("#match-method-item-id")?.checked ? ["item_id"] : [] ),
        ...( $("#match-method-title")?.checked   ? ["title"]   : [] ),
      ];
    });
  });

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
      showToast("Could not preview file: " + e.message, "error");
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
    if (step === 2) ok = !!w.mapping.upc_col && !!w.mapping.item_id_col && !!w.mapping.title_col
                         && (w.matchFromKeepa || !!w.mapping.asin_col);
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

    // Brand column select (populated separately)
    if (state.wizard.brandMode === "col") populateBrandColSelect();
  }

  function populateBrandColSelect() {
    const sel = wizEl.brandCol;
    if (!sel) return;
    const headers = state.wizard.preview?.headers || [];
    const current = state.wizard.mapping.brand_col || "";
    sel.innerHTML = `<option value="">Select column…</option>` +
      headers.map(h => `<option value="${escapeHtml(h)}"${h === current ? " selected" : ""}>${escapeHtml(h)}</option>`).join("");
    if (!current) {
      const match = headers.find(h => /brand|manufacturer/i.test(h));
      if (match) { state.wizard.mapping.brand_col = match; sel.value = match; }
    }
    sel.onchange = () => {
      state.wizard.mapping.brand_col = sel.value;
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
      ["Brand",        (r) => m.brand_col ? getCell(headers, r, m.brand_col) : (m.brand || "—")],
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
      brand:       w.brandMode === "col" ? "" : (w.mapping.brand || ""),
      brand_col:   w.brandMode === "col" ? (w.mapping.brand_col || "") : "",
      upc_col:     w.mapping.upc_col,
      item_id_col: w.mapping.item_id_col,
      title_col:   w.mapping.title_col,
      asin_col:    w.mapping.asin_col,
      attr_cols:   w.mapping.attr_cols,
      data_start:  w.dataStart || 0,
    };
    state.cpg.scanName       = (wizEl.detailName.value || "").trim() || w.file.name.replace(/\.[^.]+$/, "");
    state.cpg.aiMode         = !!wizEl.detailAI.checked;
    state.cpg.matchFromKeepa = !!w.matchFromKeepa;
    state.cpg.matchMethods   = w.matchMethods || ["upc", "item_id", "title"];
    state.cpg.catalogReady   = true;
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
      showToast("Upload failed: " + e.message, "error");
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
      showToast("Could not delete: " + e.message, "error");
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
      showToast(e.message, "error");
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
      showToast("Export failed: " + err.message, "error");
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
        <td class="text-xs">${s.upc ? Math.round(s.upc.score) + "%" : ""}</td>
        ${itemIdMpnCell(row, s.item_id)}
        <td class="text-xs">${s.brand ? Math.round(s.brand.score) + "%" : ""}</td>
        <td class="text-xs">${s.title ? Math.round(s.title.score) + "%" : ""}</td>
        <td class="text-xs">${s.pack ? Math.round(s.pack.score) + "%" : ""}</td>
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
      showToast("Action failed: " + err.message, "error");
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

  // ── Centralized panel helpers ────────────────────────────────────────────
  let _activePanel = null;
  function _openPanel(panelId) {
    if (_activePanel && _activePanel !== panelId) {
      $("#" + _activePanel)?.classList.remove("open");
    }
    _activePanel = panelId;
    $("#" + panelId)?.classList.add("open");
    $("#panel-overlay")?.classList.add("visible");
  }
  function _closeActivePanel() {
    if (_activePanel) {
      $("#" + _activePanel)?.classList.remove("open");
      _activePanel = null;
    }
    $("#panel-overlay")?.classList.remove("visible");
  }
  $("#panel-overlay")?.addEventListener("click", _closeActivePanel);

  // ========================================================================
  //  Universal Library panel
  // ========================================================================
  $("#open-abbr-btn").addEventListener("click", () => {
    _openPanel("abbr-panel");
    _switchLibTab(state.libActiveType);
    if (state.libActiveType === "abbreviations" && state.categories.length === 0) loadLibrary();
    if (state.libActiveType === "brands" || state.libActiveType === "manufacturers") loadBrandLibrary();
  });
  $("#abbr-close").addEventListener("click", () => _closeActivePanel());

  // Top-level library type tab switching
  $$(".lib-type-tab").forEach(tab => {
    tab.addEventListener("click", () => {
      state.libActiveType = tab.dataset.libType;
      _switchLibTab(state.libActiveType);
      if (state.libActiveType === "abbreviations" && state.categories.length === 0) loadLibrary();
      if (state.libActiveType === "brands" || state.libActiveType === "manufacturers") loadBrandLibrary();
    });
  });

  function _switchLibTab(type) {
    $$(".lib-type-tab").forEach(t => t.classList.toggle("active", t.dataset.libType === type));
    $("#lib-tab-abbreviations").classList.toggle("hidden", type !== "abbreviations");
    $("#lib-tab-brands").classList.toggle("hidden", type !== "brands");
    $("#lib-tab-manufacturers").classList.toggle("hidden", type !== "manufacturers");
  }

  // Brand library CRUD
  async function loadBrandLibrary() {
    try {
      const j = await api("/api/brand-analytics/library");
      state.brandAnalytics.library = j.entries || [];
      renderBrandLibraryTab("brand");
      renderBrandLibraryTab("manufacturer");
    } catch(e) { /* silent */ }
  }

  function renderBrandLibraryTab(type) {
    const entries = state.brandAnalytics.library.filter(e => e.entity_type === type);
    const listEl = type === "brand" ? $("#bl-brands-list") : $("#bl-mfrs-list");
    if (!listEl) return;
    if (!entries.length) {
      listEl.innerHTML = `<div class="text-sm" style="color:#94a3b8;padding:8px 0;">No ${type}s saved yet.</div>`;
      return;
    }
    listEl.innerHTML = entries.map(e => `
      <div class="brand-lib-row">
        <div class="flex-1">
          <div class="brand-lib-row-name">${escapeHtml(e.name)}</div>
          <div class="brand-lib-row-meta">
            ${e.sub_brands?.length ? `Sub-brands: ${e.sub_brands.map(s=>escapeHtml(s)).join(", ")}` : "No sub-brands"}
            ${e.aliases?.length ? ` · Aliases: ${e.aliases.map(a=>escapeHtml(a)).join(", ")}` : ""}
          </div>
        </div>
        <span class="brand-lib-badge ${e.discovered_by === 'ai' ? 'ai' : 'user'}">${e.discovered_by === 'ai' ? 'AI' : 'User'}</span>
        <button class="icon-btn" data-del-lib="${e.id}" title="Delete">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg>
        </button>
      </div>
    `).join("");
    listEl.querySelectorAll("[data-del-lib]").forEach(btn => {
      btn.addEventListener("click", async () => {
        await api(`/api/brand-analytics/library/${btn.dataset.delLib}`, {method:"DELETE"});
        loadBrandLibrary();
      });
    });
  }

  // Add brand
  $("#bl-brand-add")?.addEventListener("click", async () => {
    const name = $("#bl-brand-name").value.trim();
    if (!name) return;
    const aliases = $("#bl-brand-aliases").value.split(",").map(a=>a.trim()).filter(Boolean);
    const parent  = $("#bl-brand-parent").value.trim() || null;
    try {
      await api("/api/brand-analytics/library", {
        method: "POST",
        body: {entity_type:"brand", name, parent_manufacturer: parent, aliases, sub_brands:[]},
      });
      $("#bl-brand-name").value = ""; $("#bl-brand-aliases").value = ""; $("#bl-brand-parent").value = "";
      $("#bl-brand-feedback").textContent = "Brand added.";
      setTimeout(() => { $("#bl-brand-feedback").textContent = ""; }, 2000);
      loadBrandLibrary();
    } catch(e) { $("#bl-brand-feedback").textContent = e.message; }
  });

  // Add manufacturer
  $("#bl-mfr-add")?.addEventListener("click", async () => {
    const name = $("#bl-mfr-name").value.trim();
    if (!name) return;
    const aliases = $("#bl-mfr-aliases").value.split(",").map(a=>a.trim()).filter(Boolean);
    try {
      await api("/api/brand-analytics/library", {
        method: "POST",
        body: {entity_type:"manufacturer", name, aliases, sub_brands:[]},
      });
      $("#bl-mfr-name").value = ""; $("#bl-mfr-aliases").value = "";
      $("#bl-mfr-feedback").textContent = "Manufacturer added.";
      setTimeout(() => { $("#bl-mfr-feedback").textContent = ""; }, 2000);
      loadBrandLibrary();
    } catch(e) { $("#bl-mfr-feedback").textContent = e.message; }
  });

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
          showToast(e.message, "error");
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
  function _showAILearnedToast({ abbr, full, category }) {
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
  async function loadPrepFee() {
    try {
      const j = await api("/api/settings/prep-out-fee");
      $("#prep-out-fee").value = Number(j.prep_out_fee);
    } catch {}
  }
  $("#open-settings-btn").addEventListener("click", () => { _openPanel("settings-panel"); loadPrepFee(); });
  $("#settings-close").addEventListener("click", () => _closeActivePanel());
  $("#settings-save").addEventListener("click", async () => {
    const v = Number($("#threshold-verified").value);
    const r = Number($("#threshold-review").value);
    try {
      const j = await api("/api/settings/thresholds", { method: "POST", body: { verified: v, review: r } });
      state.thresholds = { verified: Number(j.verified), review: Number(j.review) };
      $("#settings-feedback").textContent = "Saved. Re-run any scan to apply.";
      setTimeout(() => $("#settings-feedback").textContent = "", 3000);
    } catch (e) {
      showToast("Failed: " + e.message, "error");
    }
  });
  $("#prep-out-save").addEventListener("click", async () => {
    const fee = Number($("#prep-out-fee").value);
    try {
      const j = await api("/api/settings/prep-out-fee", { method: "POST", body: { prep_out_fee: fee } });
      $("#prep-out-fee").value = Number(j.prep_out_fee);
      $("#prep-out-feedback").textContent = "Saved. Applies to the next Offer Analytics export.";
      setTimeout(() => $("#prep-out-feedback").textContent = "", 3000);
    } catch (e) {
      showToast("Failed: " + e.message, "error");
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
      $("#plib-results").classList.add("hidden");
      return;
    }
    // Search blacklist and Pair Library in parallel.
    let j = { results: [] }, lib = { results: [] };
    try {
      [j, lib] = await Promise.all([
        api("/api/pairs/search",          { method: "POST", body: { query: q } }),
        api("/api/pairs/library/search",  { method: "POST", body: { query: q } }),
      ]);
    } catch (e) { showToast(e.message, "error"); return; }

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
          const { upc, asin } = e.currentTarget.dataset;
          try {
            await api("/api/pairs/unlock", { method: "POST", body: { upc, asin } });
          } catch (err) {
            showToast(`Unlock failed: ${err.message}`, "error");
            return;
          }
          searchPairs();
        }));
    }

    // Pair Library matches — card only shown when there are hits.
    const libBody = $("#plib-results-body");
    if (libBody) {
      libBody.innerHTML = "";
      const rows = lib.results || [];
      rows.forEach(r => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="font-mono">${escapeHtml(r.asin)}</td>
          <td class="font-mono">${escapeHtml(r.upc || "—")}</td>
          <td class="font-mono text-xs">${escapeHtml(r.upc_aliases || "")}</td>
          <td class="font-mono text-xs">${escapeHtml(r.mpn || "")}</td>
          <td class="font-mono text-xs">${escapeHtml(r.ean || "")}</td>
          <td class="text-xs">${escapeHtml(r.brand || "")}</td>
          <td class="text-xs" style="color:#6b7480;">${escapeHtml((r.updated_at || "").slice(0, 10))}</td>
          <td class="text-right"></td>`;
        const btn = document.createElement("button");
        btn.className = "plib-del-btn";
        btn.title = "Remove this ASIN from the Pair Library";
        btn.textContent = "✕";
        btn.addEventListener("click", () => removeLibraryPair(r));
        tr.lastElementChild.appendChild(btn);
        libBody.appendChild(tr);
      });
      $("#plib-results").classList.toggle("hidden", rows.length === 0);
    }

    $("#pairs-empty").classList.add("hidden");
    $("#pairs-results").classList.remove("hidden");
  }

  // ---- Pair Library: remove an incorrect entry ----------------------------
  async function removeLibraryPair(r) {
    const bits = [r.upc && `UPC ${r.upc}`, r.mpn && `MPN ${r.mpn}`, r.ean && `EAN ${r.ean}`]
      .filter(Boolean).join(", ");
    const ok = await showConfirm({
      title: "Remove from Pair Library?",
      message: `Remove ASIN ${r.asin}${bits ? " (" + bits + ")" : ""} and all its identifiers `
        + `from the Pair Library? Runs will stop auto-approving this pair. `
        + `Re-import the correct file to restore it.`,
      confirmText: "Remove",
      danger: true,
    });
    if (!ok) return;
    try {
      await api("/api/pairs/library/delete", { method: "POST", body: { asin: r.asin } });
      showToast(`Removed ${r.asin} from the Pair Library`, "success");
      await loadPairLibStats();
      searchPairs();   // re-run the current search so the row disappears
    } catch (e) { showToast(e.message, "error"); }
  }

  // ---- Pair Library: stats + brand datalist -------------------------------
  async function loadPairLibStats() {
    try {
      const s = await api("/api/pairs/library/stats");
      const el = $("#plib-stats");
      if (el) el.textContent =
        `${(s.pairs || 0).toLocaleString()} ASINs · ${(s.identifiers || 0).toLocaleString()} identifiers · ${(s.brands || []).length} brands`;
      const dl = $("#plib-brands-datalist");
      if (dl) dl.innerHTML = (s.brands || [])
        .map(b => `<option value="${escapeHtml(b.brand)}">${escapeHtml(b.brand)} (${b.count})</option>`)
        .join("");
    } catch (_e) { /* stats are cosmetic — never block the page */ }
  }
  loadPairLibStats();

  // ---- Pair Library: template downloads -----------------------------------
  $("#plib-import-template")?.addEventListener("click", () => {
    window.location.href = "/api/pairs/library/template?kind=import";
  });
  $("#plib-lookup-template")?.addEventListener("click", () => {
    window.location.href = "/api/pairs/library/template?kind=lookup";
  });

  // ---- Pair Library: import ------------------------------------------------
  $("#plib-import-btn")?.addEventListener("click", () => {
    const inp = $("#plib-import-file");
    inp.value = "";
    inp.click();
  });
  $("#plib-import-file")?.addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const resEl = $("#plib-import-result");
    resEl.textContent = "Importing…";
    const fd = new FormData();
    fd.append("pairs_file", file, file.name);
    let j;
    try {
      j = await api("/api/pairs/library/import", { method: "POST", body: fd, form: true });
    } catch (err) {
      resEl.textContent = "";
      showToast(`Import failed: ${err.message}`, "error", 8000);
      return;
    }
    const bits = [
      `${j.pairs_created} new`, `${j.pairs_updated} updated`,
      `${j.ids_added} identifiers added`,
    ];
    if (j.blacklist_cleared)  bits.push(`${j.blacklist_cleared} unblacklisted`);
    if (j.skipped_total)      bits.push(`${j.skipped_total} skipped`);
    resEl.textContent = `Last import: ${j.rows} rows — ${bits.join(", ")}.`;
    if (j.skipped_total) {
      const first = (j.skipped || []).slice(0, 3)
        .map(s => `row ${s.row}: ${s.reason}`).join("; ");
      showToast(`Imported with ${j.skipped_total} skipped row(s) — ${first}`, "warning", 9000);
    } else {
      showToast(`Pair Library import complete — ${j.rows} rows processed.`, "success");
    }
    loadPairLibStats();
  });

  // ---- Pair Library: export by brand ---------------------------------------
  // fetch-based download so a 404 (empty library / unknown brand) shows a
  // toast instead of saving the JSON error body as a garbage file.
  $("#plib-export-btn")?.addEventListener("click", async () => {
    const brand = $("#plib-brand-input").value.trim();
    const url = `/api/pairs/library/export${brand ? "?brand=" + encodeURIComponent(brand) : ""}`;
    let resp;
    try { resp = await fetch(url); }
    catch (err) { showToast(`Export failed: ${err.message}`, "error"); return; }
    if (!resp.ok) {
      let msg = `${resp.status} ${resp.statusText}`;
      try { const ej = await resp.json(); if (ej.detail) msg = String(ej.detail); } catch {}
      showToast(msg, "warning", 7000);
      return;
    }
    const blob = await resp.blob();
    const objUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objUrl;
    a.download = `pair_library_${brand || "all"}.xlsx`;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(objUrl);
  });

  // ---- Pair Library: lookup & export ----------------------------------------
  $("#plib-lookup-btn")?.addEventListener("click", () => {
    const inp = $("#plib-lookup-file");
    inp.value = "";
    inp.click();
  });
  $("#plib-lookup-file")?.addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const resEl = $("#plib-lookup-result");
    resEl.textContent = "Matching…";
    const fd = new FormData();
    fd.append("lookup_file", file, file.name);
    // Raw fetch (not api()) — we need BOTH the xlsx blob and the X-Plib-* headers.
    let resp;
    try { resp = await fetch("/api/pairs/library/lookup-export", { method: "POST", body: fd }); }
    catch (err) { resEl.textContent = ""; showToast(`Lookup failed: ${err.message}`, "error"); return; }
    if (!resp.ok) {
      let msg = `${resp.status} ${resp.statusText}`;
      try { const ej = await resp.json(); if (ej.detail) msg = String(ej.detail); } catch {}
      resEl.textContent = "";
      showToast(`Lookup failed: ${msg}`, "error", 8000);
      return;
    }
    const matched  = resp.headers.get("X-Plib-Matched")  || "0";
    const notFound = resp.headers.get("X-Plib-Notfound") || "0";
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "pair_lookup_results.xlsx";
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    resEl.textContent = `Last lookup: ${matched} matched, ${notFound} not found.`;
    showToast(`Lookup done — ${matched} matched, ${notFound} not found.`, "success", 7000);
  });

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
      showToast("Run a verification first — the AI re-check works on existing results.", "error");
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
        _showAILearnedToast({
          abbr: "No matching rows",
          full: "Check the bucket selection — none of your rows are in those verdict buckets.",
        });
      } else if (j.rechecked > 0) {
        _showAILearnedToast({
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

  // Shared helper — sync CPG/Medical toggle across all wizard steps
  function _awizSetVettingMode(mode) {
    if (state.awiz) state.awiz.vettingMode = mode;

    // --- Step 4 buttons (class="vetting-mode-btn" + data-mode) ---
    document.querySelectorAll(".vetting-mode-btn[data-mode]").forEach(b => {
      const active = b.dataset.mode === mode;
      b.style.background = active ? "#3b82f6" : "#f8fafc";
      b.style.color      = active ? "#fff"    : "#64748b";
      b.classList.toggle("active", active);
    });
    const step4Hint = $("#awiz-mode-hint");
    if (step4Hint) {
      step4Hint.textContent = mode === "medical"
        ? "Medical: MPN is primary identifier (+40 pts exact match, +25 pts partial). Tighter category filter."
        : "CPG: UPC is primary identifier (+50 pts). MPN gives +20 pts bonus when match ≥70%.";
    }

    // --- Step 3 buttons (awiz-map-mode-cpg / awiz-map-mode-medical) ---
    const s3cpg = $("#awiz-map-mode-cpg"), s3med = $("#awiz-map-mode-medical");
    if (s3cpg && s3med) {
      const isMed = mode === "medical";
      s3cpg.style.background = !isMed ? "var(--purple-600,#5b42b8)" : "transparent";
      s3cpg.style.color      = !isMed ? "#fff" : "#64748b";
      s3med.style.background = isMed  ? "var(--purple-600,#5b42b8)" : "transparent";
      s3med.style.color      = isMed  ? "#fff" : "#64748b";
    }

    // --- Step 3 label emphasis: primary gets *, secondary gets "optional" hint ---
    const upcReq    = $("#awiz-upc-req"),    upcOpt    = $("#awiz-upc-opt");
    const itemReq   = $("#awiz-itemid-req"), itemOpt   = $("#awiz-itemid-opt");
    const upcWrap   = $("#awiz-map-upc-wrap"), itemWrap = $("#awiz-map-itemid-wrap");
    const isMedical = mode === "medical";
    if (upcReq)  upcReq.classList.toggle("hidden", isMedical);
    if (upcOpt)  upcOpt.classList.toggle("hidden", !isMedical);
    if (itemReq) itemReq.classList.toggle("hidden", !isMedical);
    if (itemOpt) itemOpt.classList.toggle("hidden", isMedical);
    // Subtle background highlight on the primary field's wrapper
    if (upcWrap && itemWrap) {
      upcWrap.style.padding    = "8px 10px";
      itemWrap.style.padding   = "8px 10px";
      upcWrap.style.borderRadius  = "8px";
      itemWrap.style.borderRadius = "8px";
      upcWrap.style.background  = !isMedical ? "var(--purple-50,#f4f0ff)"  : "transparent";
      itemWrap.style.background = isMedical  ? "var(--purple-50,#f4f0ff)"  : "transparent";
      upcWrap.style.border      = !isMedical ? "1.5px solid var(--purple-100,#e6e0fa)" : "1.5px solid transparent";
      itemWrap.style.border     = isMedical  ? "1.5px solid var(--purple-100,#e6e0fa)" : "1.5px solid transparent";
    }

    // Step 3 hint text
    const s3hint = $("#awiz-map-hint");
    if (s3hint) {
      s3hint.textContent = isMedical
        ? "Medical catalog — Item ID / Part Number is the primary search identifier. Map UPC too if your catalog has it."
        : "CPG catalog — UPC / EAN is the primary search identifier. Map Item ID too if your catalog has it.";
    }

    // Step 3 catalog-type toggle inline hint
    const mapModeHint = $("#awiz-map-mode-hint");
    if (mapModeHint) {
      mapModeHint.innerHTML = isMedical
        ? '<strong style="color:var(--purple-700);">Medical</strong> — Item ID / Part Number is the primary search identifier.'
        : '<strong style="color:var(--purple-700);">CPG</strong> — UPC/EAN is the primary search identifier.';
    }
  }

  // CPG / Medical vetting-mode toggle — step 4 buttons
  document.querySelectorAll(".vetting-mode-btn[data-mode]").forEach(btn => {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.mode;
      if (!mode) return;
      _awizSetVettingMode(mode);
    });
  });

  // CPG / Medical vetting-mode toggle — step 3 buttons
  ["awiz-map-mode-cpg", "awiz-map-mode-medical"].forEach(id => {
    $(`#${id}`)?.addEventListener("click", () => {
      const mode = $(`#${id}`)?.dataset.mode;
      if (mode) _awizSetVettingMode(mode);
    });
  });

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
                <div class="font-medium text-sm truncate" style="color: var(--purple-800);">
                  ${escapeHtml(r.name || "Untitled run")}
                  <span style="display:inline-block;margin-left:6px;padding:1px 7px;font-size:10px;font-weight:700;border-radius:10px;vertical-align:middle;background:${r.vetting_mode === 'medical' ? '#dbeafe' : '#f0fdf4'};color:${r.vetting_mode === 'medical' ? '#1d4ed8' : '#166534'};">${r.vetting_mode === 'medical' ? 'Medical' : 'CPG'}</span>
                </div>
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
              <button class="analytics-run-del" data-run-id="${r.id}" title="Delete run" aria-label="Delete run">✕</button>
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

      // Wire per-row delete buttons. stopPropagation so the row click (which
      // opens the run) doesn't also fire when deleting.
      const _runNameById = Object.fromEntries(rows.map(rr => [rr.id, rr.name || "Untitled run"]));
      body.querySelectorAll(".analytics-run-del").forEach(btn => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          const id = Number(btn.dataset.runId);
          if (id) _deleteAnalyticsRunFromBoard(id, _runNameById[id] || "this run");
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

  // Delete a run straight from the board (no need to open it first). Confirms,
  // calls DELETE (which stops any active search + cascade-deletes its data),
  // then refreshes the list.
  async function _deleteAnalyticsRunFromBoard(id, name) {
    if (!id) return;
    const ok = await showConfirm({
      title: `Delete "${name}"?`,
      message: "This permanently removes the run and all its candidates. This cannot be undone.",
      confirmText: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      await api(`/api/analytics/runs/${id}`, { method: "DELETE" });
      showToast("Run deleted", "success");
      await loadAnalyticsRuns();
    } catch (e) {
      showToast("Could not delete run: " + (e.message || e), "error");
    }
  }

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
    aiFilter: "all",
  };

  function _clearAnalyticsRunPoll() {
    if (state.analyticsRun.poll) {
      clearTimeout(state.analyticsRun.poll);
      state.analyticsRun.poll = null;
    }
  }

  async function openAnalyticsRunDetail(runId) {
    state.analyticsRun.id = runId;
    state.analyticsRun._firstRender = true;   // triggers entrance animations on first render only
    // Reset per-run view state so the previous run's tab/search/sort
    // don't bleed into the next one.
    // CRITICAL: also drop the previous run's candidate data + per-tab page cache —
    // otherwise two runs with the SAME status (both "Complete") reuse the first
    // run's cached candidate rows (fetchAnalyticsRunDetail only clears the cache on a
    // status CHANGE), so the table shows the wrong run's candidates.
    state.analyticsRun.data = null;
    state.analyticsRun.tabPageData = {};
    state.analyticsRun._lastStatus = null;
    state.analyticsRun.tab = "Approved";
    state.analyticsRun.search = "";
    state.analyticsRun.sortKey = "confidence";
    state.analyticsRun.sortDir = "desc";
    state.analyticsRun.page = 1;
    state.analyticsRun.rankMin = 0;
    state.analyticsRun.rankMax = 0;
    state.analyticsRun.skipNullRank = false;
    state.analyticsRun.aiFilter = "all";
    const sb = $("#analytics-run-search"); if (sb) sb.value = "";
    const rrMin = $("#analytics-run-rank-min"); if (rrMin) rrMin.value = "";
    const rrMax = $("#analytics-run-rank-max"); if (rrMax) rrMax.value = "";
    const rrSkip = $("#analytics-run-rank-skip-null"); if (rrSkip) rrSkip.checked = false;
    state.currentView = "analytics-run";

    // Show the loading screen, hide the run detail content.
    $$("main > div").forEach(el => el.classList.add("hidden"));
    const loadingEl = $("#analytics-run-loading");
    if (loadingEl) { loadingEl.classList.remove("hidden"); loadingEl.style.display = "flex"; }

    // Reset UI shell before the first fetch so there's no stale data flash.
    $("#analytics-run-title").textContent = "Loading…";
    $("#analytics-run-subtitle").textContent = "";
    const runView = $("#view-analytics-run");
    if (runView) runView.classList.add("hidden");

    await fetchAnalyticsRunDetail();

    // Hide loading screen, show content with entrance animation.
    if (loadingEl) { loadingEl.classList.add("hidden"); loadingEl.style.display = "none"; }
    if (runView) {
      runView.classList.remove("hidden");
      runView.classList.remove("view-enter-anim");
      void runView.offsetWidth;
      runView.classList.add("view-enter-anim");
      runView.addEventListener("animationend", () => runView.classList.remove("view-enter-anim"), { once: true });
    }
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

      // Auto-fetch current verdict tab when:
      //   • no page data exists yet (initial open, or status change cleared the cache), OR
      //   • page data exists but aiVerdictCounts hasn't been populated (e.g. an AI check
      //     just finished while the user was on this tab — candidates are loaded but
      //     scoped AI counts are stale/missing).
      const currentTab = state.analyticsRun.tab;
      const _curEntry = (state.analyticsRun.tabPageData || {})[currentTab];
      if (currentTab !== "All" && (!_curEntry || _curEntry.aiVerdictCounts == null)) {
        await _fetchVerdictPage(currentTab, state.analyticsRun.page || 1);
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

  // Approval-status badge for the Analytics candidate table.
  function _eligBadge(status) {
    if (!status) return '<span style="color:#cbd5e1;">—</span>';
    const s = String(status).toUpperCase();
    let bg = "#f1f5f9", fg = "#475569", txt = status;
    if (s === "CAN_SELL") { bg = "#dcfce7"; fg = "#166534"; txt = "Can sell"; }
    else if (s === "NEEDS_APPROVAL") { bg = "#fef3c7"; fg = "#92400e"; txt = "Approval"; }
    else if (s === "RESTRICTED") { bg = "#fee2e2"; fg = "#991b1b"; txt = "Restricted"; }
    else if (s.startsWith("ERROR")) { bg = "#fee2e2"; fg = "#991b1b"; txt = "Error"; }
    else if (s === "UNAVAILABLE") { bg = "#f1f5f9"; fg = "#64748b"; txt = "n/a"; }
    return `<span title="${escapeHtml(String(status))}" style="display:inline-block;padding:1px 7px;font-size:0.68rem;font-weight:700;border-radius:4px;background:${bg};color:${fg};">${escapeHtml(txt)}</span>`;
  }

  // Build a short human-readable reason explaining the verdict.
  function _verdictReason(c) {
    const sc = (c.data && c.data.scores) || {};
    const v  = (c.verdict || "").toLowerCase();
    const conf = c.confidence || 0;

    // ASIN conflict: same ASIN matched to multiple catalog rows — can't auto-verify
    if (c.conflict_capped) return "ASIN conflict";

    // Auto-promoted: was stored as not_approved but had no hard-reject flags
    // and confidence ≥ 35 — bumped to review at API response time.
    if (c.auto_promoted) return `Score ${Math.round(conf)}% — needs review`;

    // AI decision applied: review_status is set by apply_ai_decisions and takes
    // priority over the generic fallback so the user sees WHY it changed.
    const rs = (c.review_status || "").toLowerCase();
    if (rs === "ai-rejected") {
      const reasoning = (c.ai_reasoning || "").trim();
      return reasoning ? `AI: ${reasoning.slice(0, 100)}` : "AI rejected";
    }
    if (rs === "ai-accepted") {
      return "AI approved";
    }

    // Hard-reject signals (checked before confidence)
    if (sc.size_mismatch)          return "Size mismatch";
    if (sc.apparel_size_mismatch)  return "Size mismatch (S/M/L)";
    if (sc.count_mismatch)         return "Count mismatch";
    if (sc.shade_mismatch)         return "Shade/color mismatch";
    if (sc.gender_mismatch)        return "Gender mismatch";
    if (sc.color_mismatch)         return "Color mismatch";
    if (sc.scent_mismatch)         return "Scent/variant mismatch";
    if (sc.media_format_mismatch)  return "Media format (DVD/Blu-ray/etc.)";
    if (sc.category_mismatch)      return "Category mismatch";

    // BSR cap: only show rank reason when the run actually has a cap set
    // AND the candidate's rank violates it.
    if (v === "not_approved" && conf >= 35) {
      const rank = c.sales_rank;
      const runMaxRank = state.analyticsRun?.data?.run?.max_rank || 0;
      const runMinRank = state.analyticsRun?.data?.run?.min_rank || 0;
      if (rank != null && runMaxRank > 0 && rank > runMaxRank)
        return `BSR ${Number(rank).toLocaleString()} > max`;
      if (rank != null && runMinRank > 0 && rank < runMinRank)
        return `BSR ${Number(rank).toLocaleString()} < min`;
      return "Category or quality mismatch";
    }

    // Vendor item ID / MPN disagrees with Amazon's part number → capped to review
    if (sc.mpn_field_conflict) return "Item ID / MPN mismatch";
    // Amazon's own title size vs listing size disagree → capped to review
    if (sc.size_conflict) return "Amazon size conflict (title vs listing)";

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

    const modeLabel = run.vetting_mode === "medical" ? "Medical" : "CPG";
    const modeBadgeColor = run.vetting_mode === "medical" ? "#dbeafe" : "#f0fdf4";
    const modeBadgeText  = run.vetting_mode === "medical" ? "#1d4ed8" : "#166534";
    const titleEl = $("#analytics-run-title");
    if (titleEl) {
      titleEl.innerHTML = `${escapeHtml(run.name || "Untitled run")}&nbsp;<span style="display:inline-block;padding:2px 9px;font-size:11px;font-weight:700;border-radius:12px;vertical-align:middle;background:${modeBadgeColor};color:${modeBadgeText};">${modeLabel}</span>`;
    }
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

    // Entrance animations — run once when the view first opens
    if (state.analyticsRun._firstRender) {
      state.analyticsRun._firstRender = false;
      const summaryRow = $("#analytics-run-summary-row");
      if (summaryRow) _animateStats(summaryRow);
      _animateProgressBars(document.getElementById("view-analytics-run"));
    }

    // Dedup warning banner — shown when duplicate catalog rows were stripped
    let dedupBanner = $("#analytics-run-dedup-banner");
    const dedupCount = run.duplicate_rows_removed || 0;
    if (dedupCount > 0) {
      if (!dedupBanner) {
        dedupBanner = document.createElement("div");
        dedupBanner.id = "analytics-run-dedup-banner";
        dedupBanner.style.cssText = "margin:8px 0;padding:8px 14px;background:#fef3c7;border:1px solid #fcd34d;border-radius:8px;font-size:0.82rem;color:#92400e;display:flex;align-items:center;gap:8px;";
        dedupBanner.innerHTML = `<span style="font-size:1.1em;">⚠</span> <span id="analytics-run-dedup-text"></span>`;
        const statsRow = $("#analytics-run-stat-catalog")?.closest(".run-stats-row") || $("#analytics-run-progress-card");
        statsRow?.insertAdjacentElement("afterend", dedupBanner);
      }
      $("#analytics-run-dedup-text").textContent =
        `${dedupCount.toLocaleString()} duplicate catalog row${dedupCount === 1 ? " was" : "s were"} removed before searching — the vendor catalog contained identical Item IDs listed multiple times.`;
      dedupBanner.style.display = "flex";
    } else if (dedupBanner) {
      dedupBanner.style.display = "none";
    }

    // Store ASIN conflicts map for use in renderAnalyticsRunCandidates
    state.analyticsRun._asinConflicts = j.asin_conflicts || {};

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
      const applyBtn = $("#analytics-run-ai-apply");
      if (applyBtn) {
        applyBtn.classList.toggle("hidden", !aiDone);
        const alreadyApplied = run.ai_decisions_applied === 1;
        applyBtn.disabled = alreadyApplied;
        if (alreadyApplied) {
          applyBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> AI Applied`;
          applyBtn.classList.add("btn-applied");
        } else {
          applyBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> Apply AI Decisions`;
          applyBtn.classList.remove("btn-applied");
        }
      }

      // AI progress card
      const aiCard = $("#analytics-run-ai-progress-card");
      if (aiCard) {
        aiCard.classList.toggle("hidden", !aiRunning);
        if (aiRunning) {
          const aiDoneN  = run.ai_check_done  || 0;
          const aiTotalN = run.ai_check_total || 0;
          const pct = aiTotalN > 0 ? Math.min(100, (aiDoneN / aiTotalN) * 100) : 0;
          const bar = $("#analytics-run-ai-progress-bar");
          if (bar) bar.style.width = pct + "%";
          const counts = $("#analytics-run-ai-progress-counts");
          if (counts) counts.textContent = aiTotalN > 0
            ? `${aiDoneN.toLocaleString()} / ${aiTotalN.toLocaleString()}`
            : "Starting…";
          const sub = $("#analytics-run-ai-progress-sub");
          if (sub) sub.textContent = aiTotalN > 0
            ? `${Math.round(pct)}% complete`
            : "Reviewing candidates";
        }
      }

      // Stop AI button — only visible while AI check is running
      const aiStopBtn = $("#analytics-run-ai-stop");
      if (aiStopBtn) aiStopBtn.classList.toggle("hidden", !aiRunning);

      if (aiRunning) {
        const aiDoneN = run.ai_check_done || 0;
        const aiTotalN = run.ai_check_total || 0;
        aiBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0;animation:spin 1.2s linear infinite"><circle cx="12" cy="12" r="10" stroke-opacity="0.3"/><path d="M12 2a10 10 0 0 1 10 10"/></svg> ${aiTotalN > 0 ? `Checking ${aiDoneN.toLocaleString()}/${aiTotalN.toLocaleString()}…` : "Checking…"}`;
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

    // Eligibility & storage button — same visibility rule as AI check.
    const eligBtn = $("#analytics-run-elig-check");
    if (eligBtn) {
      const s3 = (run.status || "").toLowerCase();
      const canElig = s3 === "complete" || s3 === "paused" || s3 === "stopped" || s3 === "error";
      const eStatus = (run.elig_check_status || "").toLowerCase();
      const eRunning = eStatus === "running";
      const eDone = eStatus.startsWith("done");
      eligBtn.classList.toggle("hidden", !canElig);

      const eCard = $("#analytics-run-elig-progress-card");
      if (eCard) {
        eCard.classList.toggle("hidden", !eRunning);
        if (eRunning) {
          const dN = run.elig_check_done || 0, tN = run.elig_check_total || 0;
          const pct = tN > 0 ? Math.min(100, (dN / tN) * 100) : 0;
          const bar = $("#analytics-run-elig-progress-bar"); if (bar) bar.style.width = pct + "%";
          const cnt = $("#analytics-run-elig-progress-counts");
          if (cnt) cnt.textContent = tN > 0 ? `${dN.toLocaleString()} / ${tN.toLocaleString()}` : "Starting…";
        }
      }
      const eStop = $("#analytics-run-elig-stop");
      if (eStop) eStop.classList.toggle("hidden", !eRunning);

      if (eRunning) {
        const dN = run.elig_check_done || 0, tN = run.elig_check_total || 0;
        eligBtn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0;animation:spin 1.2s linear infinite"><circle cx="12" cy="12" r="10" stroke-opacity="0.3"/><path d="M12 2a10 10 0 0 1 10 10"/></svg> ${tN > 0 ? `Checking ${dN.toLocaleString()}/${tN.toLocaleString()}…` : "Checking…"}`;
        eligBtn.disabled = true; eligBtn.style.opacity = "0.7";
        if (!_runIsActive(run)) {
          _clearAnalyticsRunPoll();
          state.analyticsRun.poll = setTimeout(fetchAnalyticsRunDetail, 2000);
        }
      } else {
        eligBtn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0"><path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="10"/></svg> ${eDone ? "Re-check Eligibility" : "Eligibility & Storage"}`;
        eligBtn.disabled = false; eligBtn.style.opacity = "1";
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

  // Navigate to page p — fetches from server when on a verdict tab,
  // or when on "All" tab with an active AI filter.
  async function _onPageClick(p) {
    state.analyticsRun.page = p;
    const tab = state.analyticsRun.tab;
    const aiFilter = state.analyticsRun.aiFilter || "all";
    if (tab !== "All") {
      await _fetchVerdictPage(tab, p);
    } else if (aiFilter !== "all") {
      await _fetchVerdictPage("All", p);
    }
    renderAnalyticsRunCandidates();
    $("#analytics-run-candidates-body")?.closest(".overflow-auto")?.scrollTo(0, 0);
  }

  // Fetch one page of verdict-filtered candidates from the server and store
  // in tabPageData. Shows "Loading…" immediately while the request is in flight.
  async function _fetchVerdictPage(tab, page) {
    const verdict = _TAB_TO_VERDICT[tab]; // undefined for "All" tab — that's ok
    const id = state.analyticsRun.id;
    const pageSize = state.analyticsRun.pageSize || 50;
    const offset = (page - 1) * pageSize;
    const aiFilter = state.analyticsRun.aiFilter || "all";
    const searchQ = (state.analyticsRun.search || "").trim();
    state.analyticsRun.tabLoading = tab;
    renderAnalyticsRunCandidates();
    try {
      let url = `/api/analytics/runs/${id}?limit=${pageSize}&offset=${offset}`;
      if (verdict) url += `&verdict=${encodeURIComponent(verdict)}`;
      if (aiFilter !== "all") url += `&ai_verdict=${encodeURIComponent(aiFilter)}`;
      if (searchQ) url += `&search=${encodeURIComponent(searchQ)}`;
      const j = await api(url);
      if (!state.analyticsRun.tabPageData) state.analyticsRun.tabPageData = {};
      state.analyticsRun.tabPageData[tab] = {
        serverPage: page,
        aiFilter: aiFilter,           // store which filter was active so the count guard works
        candidates: j.candidates || [],
        aiFilteredCount: j.ai_filtered_count ?? null,
        aiVerdictCounts: j.ai_verdict_counts ?? null,
        aiFilter,
      };
    } catch (e) {
      console.warn("[tab fetch]", e);
    } finally {
      state.analyticsRun.tabLoading = null;
      renderAnalyticsRunCandidates();
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

    const _aiFilterActive = (state.analyticsRun.aiFilter || "all") !== "all";
    const _searchActive   = !!(state.analyticsRun.search || "").trim();
    // Use server-fetched tabPageData when: verdict tab, AI filter, OR search active.
    const _useServerPage  = isVerdictTab || _aiFilterActive || _searchActive;
    if (!_useServerPage && !j) return;
    if (_useServerPage && !tabPageEntry && !isLoading) return;

    // When using a server page (verdict/AI filter/search) candidates come from
    // tabPageData; otherwise from the main "All" snapshot.
    const cand = _useServerPage
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

    // ---- AI verdict filter --------------------------------------------
    const _aiFilter = state.analyticsRun.aiFilter || "all";
    // AI verdict pill counts.
    // Verdict tabs (Approved / Review / Not Approved): use tab-scoped counts from
    // tabPageData so the pill labels reflect only THIS tab's AI-checked items.
    // If the scoped data hasn't arrived yet (null pageEntry), fall back temporarily
    // to whole-run counts so the pills remain visible while the fetch is in flight.
    // An empty {} from the server means no items in this tab have AI verdicts —
    // treat as null so the filter section is hidden rather than showing disabled buttons.
    // All tab: whole-run counts are always correct.
    const _pageEntry = isVerdictTab ? (state.analyticsRun.tabPageData || {})[tab] : null;
    let _serverCounts = null;
    if (isVerdictTab) {
      if (_pageEntry?.aiVerdictCounts != null) {
        const scoped = _pageEntry.aiVerdictCounts;
        // Non-empty scoped counts → use them (correct tab-scoped labels).
        // Empty {} → no AI verdicts on this tab → null so section is hidden.
        _serverCounts = Object.keys(scoped).length > 0 ? scoped : null;
      } else {
        // Scoped data not yet fetched → temporarily show whole-run counts.
        _serverCounts = state.analyticsRun.data?.ai_verdict_counts ?? null;
      }
    } else {
      _serverCounts = state.analyticsRun.data?.ai_verdict_counts ?? null;
    }
    const _aiCounts = _serverCounts
      ? { approve: _serverCounts.approve || 0, reject: _serverCounts.reject || 0, uncertain: _serverCounts.uncertain || 0 }
      : (() => {
          const c = { approve: 0, reject: 0, uncertain: 0 };
          rows.forEach(r => { if (r.ai_verdict && c[r.ai_verdict] !== undefined) c[r.ai_verdict]++; });
          return c;
        })();
    const _hasAiBadges = _aiCounts.approve + _aiCounts.reject + _aiCounts.uncertain > 0;
    const aiFilterWrap = $("#analytics-run-ai-filter-wrap");
    if (aiFilterWrap) {
      aiFilterWrap.classList.toggle("hidden", !_hasAiBadges);
      $$(".ai-filter-pill", aiFilterWrap).forEach(btn => {
        const f = btn.dataset.aiFilter;
        btn.classList.toggle("active", f === _aiFilter);
        if (f === "all") {
          btn.textContent = "All";
          btn.disabled = false;
          btn.style.opacity = "1";
          btn.style.cursor = "pointer";
        } else {
          const label = f === "approve" ? "✓ Approved" : f === "reject" ? "✗ Rejected" : "? Uncertain";
          const cnt = _aiCounts[f] || 0;
          btn.textContent = cnt > 0 ? `${label} (${cnt.toLocaleString()})` : label;
          btn.disabled = cnt === 0;
          btn.style.opacity = cnt > 0 ? "1" : "0.4";
          btn.style.cursor = cnt > 0 ? "pointer" : "not-allowed";
          if (_aiFilter === f && cnt === 0) state.analyticsRun.aiFilter = "all";
        }
      });
    }
    // When an AI filter is active on a verdict tab, the server already filtered
    // the returned candidates — no need to re-filter client-side.
    if (_aiFilter !== "all" && !isVerdictTab) {
      rows = rows.filter(c => c.ai_verdict === _aiFilter);
    } else if (_aiFilter !== "all" && isVerdictTab) {
      // Server-filtered: all candidates already have the correct ai_verdict.
      // Still filter in case tabPageData was populated before filter was applied.
      if (_pageEntry?.aiFilter !== _aiFilter) {
        rows = rows.filter(c => c.ai_verdict === _aiFilter);
      }
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
    // When an AI filter is active the server returns ai_filtered_count — use
    // that so pagination reflects only the filtered set.
    const pageSize = state.analyticsRun.pageSize || 50;
    const page     = state.analyticsRun.page;
    let totalRows, totalPages, pageRows;
    if (isVerdictTab || _aiFilterActive) {
      const run = j?.run || {};
      // If the cached page was fetched with the same AI filter, use its count.
      const pageEntry = (state.analyticsRun.tabPageData || {})[tab];
      const aiFilteredCount = (pageEntry?.aiFilter === _aiFilter && pageEntry?.aiFilteredCount != null)
        ? pageEntry.aiFilteredCount : null;
      const dbTotal = tab === "Approved"     ? (run.verified_count     ?? 0)
                    : tab === "Review"        ? (run.review_count       ?? 0)
                    : tab === "Not Approved"  ? (run.not_approved_count ?? 0)
                    : (run.total_candidates_found ?? cand.length);
      totalRows  = _aiFilter !== "all" && aiFilteredCount != null ? aiFilteredCount : dbTotal;
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

    const _asinConflicts = state.analyticsRun._asinConflicts || {};
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

      // ASIN conflict badge: same ASIN matched to more than one catalog row
      const conflictRows = _asinConflicts[c.asin];
      const conflictTooltip = conflictRows
        ? `This ASIN is also matched to row${conflictRows.length > 2 ? "s" : ""} ${conflictRows.filter(r => r !== c.row_idx).map(r => r + 1).join(", ")} — verify which catalog entry is correct`
        : "";
      const conflictBadge = conflictRows
        ? `<span class="has-tooltip" data-tooltip="${escapeHtml(conflictTooltip)}" style="display:inline-block;margin-left:4px;padding:1px 6px;font-size:0.68rem;font-weight:700;border-radius:4px;background:#fef3c7;color:#92400e;border:1px solid #fcd34d;cursor:help;">⚠ Conflict</span>`
        : "";

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
          <td class="font-mono text-xs">${escapeHtml(c.asin || "")}${conflictBadge}</td>
          <td style="max-width:300px;">
            <div class="text-sm" style="color: #475569;">${escapeHtml(amzTitle)}</div>
          </td>
          <td class="text-xs">${escapeHtml(amzBrand)}</td>
          <td class="text-xs">${escapeHtml(sources)}</td>
          <td class="text-xs text-center">${c.amz_pack != null ? escapeHtml(String(c.amz_pack)) : "1"}</td>
          <td class="text-xs text-right" style="color:#64748b;">${c.sales_rank != null ? Number(c.sales_rank).toLocaleString() : "—"}</td>
          <td class="text-center">${_eligBadge(c.eligibility_status)}</td>
          <td class="text-xs text-right" style="color:#64748b;white-space:nowrap;">${c.storage_fee != null ? "$" + Number(c.storage_fee).toFixed(2) : "—"}</td>
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
      case "discard": verdict = "not_approved"; review_status = "Manually Rejected"; break;
      case "promote": verdict = "verified";     review_status = "Manually Approved"; break;
      case "reject":  verdict = "not_approved"; review_status = "Manually Rejected"; break;
      default: return;
    }

    btn.disabled = true;
    btn.style.opacity = "0.5";
    try {
      await applyAnalyticsVerdict(row_idx, asin, verdict, review_status);
    } catch (err) {
      showToast("Couldn't update verdict: " + (err.message || err), "error");
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
      showToast("Control action failed: " + (e.message || e), "error");
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
      showToast("Could not delete run: " + (e.message || e), "error");
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
  let _rescoreBrandMode = "text"; // "text" | "col"

  function _applyRescoreBrandMode(mode) {
    _rescoreBrandMode = mode;
    const isCol = mode === "col";
    $("#analytics-rescore-brand-text")?.classList.toggle("hidden", isCol);
    $("#analytics-rescore-brand-col")?.classList.toggle("hidden", !isCol);
    const btn = $("#analytics-rescore-brand-mode-btn");
    if (btn) btn.textContent = isCol ? "type a brand instead" : "use a column instead";
  }

  $("#analytics-rescore-brand-mode-btn")?.addEventListener("click", () => {
    _applyRescoreBrandMode(_rescoreBrandMode === "text" ? "col" : "text");
  });

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
    // For the brand dropdown, filter out obviously non-brand columns
    // (prices, dates, quantities, percentages) to reduce noise.
    const firstRowForFilter = catRows[0] || {};
    const firstRawForFilter = firstRowForFilter.raw || {};
    function _isBrandLikeCol(key) {
      if (/price|cost|qty|ytd|\$|%|adjustment|change|rank|unit price|special|packaging/i.test(key)) return false;
      const v = firstRawForFilter[key];
      if (v !== null && v !== undefined && v !== "" && !isNaN(parseFloat(String(v)))) return false;
      return true;
    }
    const brandColOpts = [
      ...builtins.map(b => `<option value="${escapeHtml(b.key)}">${escapeHtml(b.label)}</option>`),
      ...rawCols.filter(_isBrandLikeCol).map(k => `<option value="${escapeHtml(k)}">${escapeHtml(k)}</option>`),
    ].join("");
    const brandSel = $("#analytics-rescore-brand-col");
    if (brandSel) brandSel.innerHTML = `<option value="">— none (use wizard brand) —</option>` + brandColOpts;
    // Pre-populate min/max rank from the run's stored values.
    const storedMinRank = state.analyticsRun?.data?.run?.min_rank || 0;
    const storedMaxRank = state.analyticsRun?.data?.run?.max_rank || 0;
    const mrMinInput = $("#analytics-rescore-min-rank");
    if (mrMinInput) mrMinInput.value = storedMinRank > 0 ? String(storedMinRank) : "";
    const mrInput = $("#analytics-rescore-max-rank");
    if (mrInput) mrInput.value = storedMaxRank > 0 ? String(storedMaxRank) : "";
    // Pre-populate brand from saved value (new runs) or auto-detect from catalog rows (old runs).
    const storedBrandCol  = state.analyticsRun?.data?.run?.brand_col  || "";
    const storedBrandMode = state.analyticsRun?.data?.run?.brand_mode || "col";
    if (storedBrandCol) {
      if (storedBrandMode === "text") {
        _applyRescoreBrandMode("text");
        const tb = $("#analytics-rescore-brand-text");
        if (tb) tb.value = storedBrandCol;
      } else {
        _applyRescoreBrandMode("col");
        if (brandSel) brandSel.value = storedBrandCol;
      }
    } else {
      // Auto-detect from catalog rows for runs created before brand persistence was added.
      const firstCatRow = (state.analyticsRun?.data?.catalog_rows || [])[0] || {};
      const firstBrand  = (firstCatRow.brand || "").trim().toLowerCase();
      const firstRaw    = firstCatRow.raw || {};
      let detectedCol   = "";
      if (firstBrand) {
        for (const [key, val] of Object.entries(firstRaw)) {
          if (String(val || "").trim().toLowerCase() === firstBrand) {
            detectedCol = key;
            break;
          }
        }
      }
      if (detectedCol) {
        _applyRescoreBrandMode("col");
        if (brandSel) brandSel.value = detectedCol;
      } else if (firstBrand) {
        // Brand was a fixed text override — use it directly.
        _applyRescoreBrandMode("text");
        const tb = $("#analytics-rescore-brand-text");
        if (tb) tb.value = firstCatRow.brand;
      } else {
        _applyRescoreBrandMode("col");
      }
    }
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
      const brandCol = _rescoreBrandMode === "col"
        ? ($("#analytics-rescore-brand-col")?.value || "")
        : ($("#analytics-rescore-brand-text")?.value.trim() || "");
      const minRankRescore = _parseRank($("#analytics-rescore-min-rank")?.value);
      const maxRankRescore = _parseRank($("#analytics-rescore-max-rank")?.value);
      await api(`/api/analytics/runs/${id}/rescore`, {
        method: "POST",
        body: { title_col: col, brand_col: brandCol, brand_mode: _rescoreBrandMode, min_rank: minRankRescore, max_rank: maxRankRescore },
      });
      // Clear per-tab cache so next tab visit re-fetches with updated scores.
      state.analyticsRun.tabPageData = {};
      // Kick an immediate poll so the progress card appears without waiting 2s.
      await fetchAnalyticsRunDetail();
    } catch (e) {
      showToast("Re-score failed: " + (e.message || e), "error");
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

  // Search box — server-side fetch so ASINs/titles anywhere in the run
  // are found, not just within the currently loaded page.
  let _analyticsSearchTimer = null;
  $("#analytics-run-search")?.addEventListener("input", (e) => {
    state.analyticsRun.search = e.target.value || "";
    state.analyticsRun.page = 1;
    // Clear any pending debounce timer
    if (_analyticsSearchTimer) clearTimeout(_analyticsSearchTimer);
    const q = state.analyticsRun.search.trim();
    if (!q) {
      // Empty — revert to normal tab data (clear search results from cache)
      if (state.analyticsRun.tabPageData) {
        Object.keys(state.analyticsRun.tabPageData).forEach(k => {
          delete state.analyticsRun.tabPageData[k];
        });
      }
      const tab = state.analyticsRun.tab;
      if (tab !== "All") _fetchVerdictPage(tab, 1);
      else renderAnalyticsRunCandidates();
      return;
    }
    // Debounce 350ms then fetch server-side
    _analyticsSearchTimer = setTimeout(async () => {
      const tab = state.analyticsRun.tab;
      // Clear cached page so the search results replace it
      if (state.analyticsRun.tabPageData) {
        Object.keys(state.analyticsRun.tabPageData).forEach(k => {
          delete state.analyticsRun.tabPageData[k];
        });
      }
      await _fetchVerdictPage(tab, 1);
    }, 350);
  });

  // Parse a rank input that may contain commas ("400,000"), spaces ("400 000"),
  // or k/m suffixes ("400k" → 400000, "1.5m" → 1500000).
  function _parseRank(v) {
    const s = (v || "").trim().toLowerCase().replace(/[\s,]/g, "");
    if (!s) return 0;
    const num = parseFloat(s);
    if (isNaN(num)) return 0;
    if (s.endsWith("m")) return Math.max(0, Math.round(num * 1_000_000));
    if (s.endsWith("k")) return Math.max(0, Math.round(num * 1_000));
    return Math.max(0, Math.round(num));
  }

  // BSR range filter — client-side display filter, no round-trip.
  function _applyRankFilter() {
    state.analyticsRun.rankMin = _parseRank($("#analytics-run-rank-min")?.value);
    state.analyticsRun.rankMax = _parseRank($("#analytics-run-rank-max")?.value);
    state.analyticsRun.skipNullRank = !!($("#analytics-run-rank-skip-null")?.checked);
    state.analyticsRun.page = 1;
    renderAnalyticsRunCandidates();
  }
  $("#analytics-run-rank-min")?.addEventListener("change", _applyRankFilter);
  $("#analytics-run-rank-max")?.addEventListener("change", _applyRankFilter);
  $("#analytics-run-rank-skip-null")?.addEventListener("change", _applyRankFilter);

  $("#analytics-run-ai-filter-wrap")?.addEventListener("click", async e => {
    const btn = e.target.closest(".ai-filter-pill");
    if (!btn || btn.disabled) return;
    const newFilter = btn.dataset.aiFilter || "all";
    if (newFilter === state.analyticsRun.aiFilter) return;
    state.analyticsRun.aiFilter = newFilter;
    state.analyticsRun.page = 1;
    // Always refetch from server when filter changes — this covers verdict tabs
    // AND the "All" tab (which also needs server-side filtering for completeness).
    const tab = state.analyticsRun.tab;
    if (state.analyticsRun.tabPageData) delete state.analyticsRun.tabPageData[tab];
    if (newFilter !== "all" || tab !== "All") {
      await _fetchVerdictPage(tab, 1);
    } else {
      // Resetting to "all" on "All" tab — just re-render from cached main data.
      renderAnalyticsRunCandidates();
    }
  });

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
  // Shared server-side bulk: move EVERY candidate in `from_verdict` → `to_verdict`
  // for the current run. Done server-side so it never misses rows the paginated
  // detail view hasn't loaded (the old client-side filter silently hit nothing
  // when the tab's candidates weren't in the in-memory snapshot). Refreshes after.
  async function _runBulkVerdictAll(from_verdict, to_verdict, review_status) {
    const id = state.analyticsRun.id;
    if (!id) return;
    try {
      const j = await api(`/api/analytics/runs/${id}/candidates/bulk_verdict_all`, {
        method: "POST",
        body: { from_verdict, to_verdict, review_status },
      });
      showToast(`Updated ${j.updated} candidate${j.updated === 1 ? "" : "s"}.`, "success");
      state.analyticsRun.tabPageData = {};   // force a clean re-fetch of the current tab
      await fetchAnalyticsRunDetail();
    } catch (err) {
      showToast("Bulk update failed: " + (err.message || err), "error");
    }
  }

  // Per-tab "Approve All / Promote All / Reject All" — now applies to the WHOLE
  // tab bucket (not just the loaded page) via the server-side endpoint.
  $("#analytics-run-bulk")?.addEventListener("click", async () => {
    const data = state.analyticsRun.data;
    if (!state.analyticsRun.id || !data || !data.run) return;
    const tab = state.analyticsRun.tab;
    if (tab === "All") return;

    let from_verdict, to_verdict, review_status, verb, danger;
    if (tab === "Approved")          { from_verdict = "verified";     to_verdict = "not_approved"; review_status = "Manually Rejected"; verb = "Reject";  danger = true;  }
    else if (tab === "Not Approved") { from_verdict = "not_approved"; to_verdict = "verified";     review_status = "Manually Approved"; verb = "Promote"; danger = false; }
    else                             { from_verdict = "review";       to_verdict = "verified";     review_status = "Reviewed";          verb = "Approve"; danger = false; }

    const countOf = { verified: data.run.verified_count || 0, review: data.run.review_count || 0, not_approved: data.run.not_approved_count || 0 };
    const n = countOf[from_verdict] || 0;
    if (n === 0) { showToast(`Nothing to ${verb.toLowerCase()} in ${tab}.`, "info"); return; }

    const ok = await showConfirm({
      title: `${verb} all ${n} ${tab} candidate${n === 1 ? "" : "s"}?`,
      message: tab === "Approved"
        ? "Every candidate in Approved will be moved to Not Approved."
        : "Every candidate in this tab will be moved to Approved.",
      confirmText: verb,
      danger,
    });
    if (!ok) return;

    const btn = $("#analytics-run-bulk");
    if (btn) { btn.disabled = true; btn.style.opacity = "0.5"; }
    try { await _runBulkVerdictAll(from_verdict, to_verdict, review_status); }
    finally { if (btn) { btn.disabled = false; btn.style.opacity = "1"; } }
  });

  // "Bulk approve…" — opens a modal to pick which category to approve everything from.
  $("#analytics-run-bulk-approve")?.addEventListener("click", () => {
    const data = state.analyticsRun.data;
    if (!data || !data.run) return;
    _showBulkApproveModal(data.run);
  });

  function _showBulkApproveModal(run) {
    const cats = [
      { key: "review",       label: "Review",       count: run.review_count || 0 },
      { key: "not_approved", label: "Not Approved", count: run.not_approved_count || 0 },
    ].filter(c => c.count > 0);
    if (!cats.length) { showToast("Nothing to approve — Review and Not Approved are empty.", "info"); return; }

    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    backdrop.style.cssText = "align-items:center;justify-content:center;padding:0;z-index:200;";
    const rowsHtml = cats.map(c => `
      <button class="bulk-cat-row" data-cat="${c.key}">
        <span>Approve all from <b>${c.label}</b></span>
        <span class="bulk-cat-count">${c.count}</span>
      </button>`).join("");
    backdrop.innerHTML = `
      <div class="bg-white rounded-xl w-[420px] p-6 shadow-xl" role="dialog" aria-modal="true" style="box-shadow:0 24px 60px rgba(0,0,0,0.28);">
        <div class="font-semibold mb-1" style="color:var(--navy-800);">Bulk approve</div>
        <div class="text-sm mb-3" style="color:#6b7480;">Choose a category — every candidate in it moves to Approved (marked Manually Approved).</div>
        <div class="bulk-cat-list">${rowsHtml}</div>
        <div class="flex justify-end mt-4">
          <button class="btn btn-secondary bulk-cancel">Cancel</button>
        </div>
      </div>`;

    function close() { document.removeEventListener("keydown", onKey); backdrop.remove(); }
    function onKey(e) { if (e.key === "Escape") close(); }
    backdrop.querySelector(".bulk-cancel").addEventListener("click", close);
    backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(); });
    document.addEventListener("keydown", onKey);
    backdrop.querySelectorAll(".bulk-cat-row").forEach(b => {
      b.addEventListener("click", async () => {
        const cat = cats.find(c => c.key === b.dataset.cat);
        close();
        const ok = await showConfirm({
          title: `Approve all ${cat.count} ${cat.label} candidate${cat.count === 1 ? "" : "s"}?`,
          message: "They'll be moved to Approved and marked Manually Approved. You can still change individual items afterwards.",
          confirmText: "Approve all",
          danger: false,
        });
        if (!ok) return;
        await _runBulkVerdictAll(cat.key, "verified", "Manually Approved");
      });
    });
    document.body.appendChild(backdrop);
  }

  // ==========================================================================
  //  AI Check modal
  // ==========================================================================
  const _aiCheckModal = $("#analytics-ai-check-modal");

  function _aiCheckSelectedVerdicts() {
    const verdicts = [];
    if ($("#ai-check-filter-review")?.checked)       verdicts.push("review");
    if ($("#ai-check-filter-not-approved")?.checked) verdicts.push("not_approved");
    if ($("#ai-check-filter-approved")?.checked)     verdicts.push("verified");
    return verdicts.length === 0 || verdicts.length === 3 ? "all" : verdicts.join(",");
  }

  async function _fetchAiCheckEstimate() {
    const id = state.analyticsRun.id;
    if (!id) return;
    const verdict = _aiCheckSelectedVerdicts();
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

  $("#analytics-run-ai-stop")?.addEventListener("click", async () => {
    const btn = $("#analytics-run-ai-stop");
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    btn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/></svg> Stopping…`;
    try {
      const result = await api(`/api/analytics/runs/${state.analyticsRun.id}/ai_check/stop`, { method: "POST", body: {} });
      console.log("[stop] server response:", result);
    } catch (e) {
      console.error("[stop] request failed:", e);
      showToast("Stop request failed: " + e.message, "error");
    }
    // Re-enable after a tick so it can't be double-clicked
    setTimeout(() => { if (btn) { btn.disabled = false; } }, 1500);
  });

  $("#analytics-run-elig-check")?.addEventListener("click", async () => {
    const id = state.analyticsRun.id; if (!id) return;
    let n = 0;
    try { const est = await api(`/api/analytics/runs/${id}/eligibility/estimate`); n = est.unique_asins || 0; } catch (_) {}
    const mins = Math.max(1, Math.round(n / 5 / 60));
    const ok = await showConfirm({
      title: "Check eligibility & storage?",
      message: `Fetches approval status (CAN_SELL / NEEDS_APPROVAL / RESTRICTED) + storage fee for ${n.toLocaleString()} Approved/Review ASINs. Storage is instant; eligibility makes ~${n.toLocaleString()} live Amazon calls (~${mins} min).`,
      confirmText: "Run check", cancelText: "Cancel",
    });
    if (!ok) return;
    try {
      await api(`/api/analytics/runs/${id}/eligibility`, { method: "POST" });
      _clearAnalyticsRunPoll();
      state.analyticsRun.poll = setTimeout(fetchAnalyticsRunDetail, 800);
    } catch (e) { showToast("Eligibility check failed to start: " + (e.message || e), "error"); }
  });

  $("#analytics-run-elig-stop")?.addEventListener("click", async () => {
    const id = state.analyticsRun.id; if (!id) return;
    try { await api(`/api/analytics/runs/${id}/eligibility/stop`, { method: "POST" }); } catch (_) {}
    _clearAnalyticsRunPoll();
    state.analyticsRun.poll = setTimeout(fetchAnalyticsRunDetail, 500);
  });

  $("#analytics-run-ai-check")?.addEventListener("click", () => {
    _aiCheckModal?.classList.remove("hidden");
    // Reset to default: Review only checked
    const r = $("#ai-check-filter-review");
    const n = $("#ai-check-filter-not-approved");
    const a = $("#ai-check-filter-approved");
    if (r) r.checked = true;
    if (n) n.checked = false;
    if (a) a.checked = false;
    _fetchAiCheckEstimate();
  });

  ["ai-check-filter-review", "ai-check-filter-not-approved", "ai-check-filter-approved"]
    .forEach(id => $("#" + id)?.addEventListener("change", _fetchAiCheckEstimate));

  $("#ai-check-cancel")?.addEventListener("click", () => {
    _aiCheckModal?.classList.add("hidden");
  });
  _aiCheckModal?.addEventListener("click", (e) => {
    if (e.target === _aiCheckModal) _aiCheckModal.classList.add("hidden");
  });

  $("#ai-check-confirm")?.addEventListener("click", async () => {
    const id = state.analyticsRun.id;
    if (!id) return;
    const verdict = _aiCheckSelectedVerdicts();
    _aiCheckModal?.classList.add("hidden");
    try {
      await api(`/api/analytics/runs/${id}/ai_check`, {
        method: "POST",
        body: { verdict },
      });
      state.analyticsRun._aiCheckJustStarted = 15;
      _clearAnalyticsRunPoll();
      state.analyticsRun.poll = setTimeout(fetchAnalyticsRunDetail, 800);
    } catch (e) {
      showToast("AI Check failed to start: " + (e.message || e), "error");
    }
  });

  $("#analytics-run-ai-apply")?.addEventListener("click", (e) => {
    if (e.currentTarget.disabled) return;
    const modal = $("#analytics-ai-apply-modal");
    if (modal) modal.classList.remove("hidden");
  });

  $("#ai-apply-cancel")?.addEventListener("click", () => {
    $("#analytics-ai-apply-modal")?.classList.add("hidden");
  });

  $("#analytics-ai-apply-modal")?.addEventListener("click", (e) => {
    if (e.target === e.currentTarget) e.currentTarget.classList.add("hidden");
  });

  $("#ai-apply-confirm")?.addEventListener("click", async () => {
    const id = state.analyticsRun.id;
    if (!id) return;
    const modal = $("#analytics-ai-apply-modal");
    const btn = $("#ai-apply-confirm");
    if (btn) { btn.disabled = true; btn.textContent = "Applying…"; }
    try {
      const j = await api(`/api/analytics/runs/${id}/ai_check/apply`, { method: "POST", body: {} });
      modal?.classList.add("hidden");
      const approved = j.approved ?? 0;
      const rejected = j.rejected ?? 0;
      // Show loading overlay so the table visibly refreshes before showing updated results
      const loadingEl = $("#analytics-run-loading");
      const runViewEl = $("#view-analytics-run");
      if (loadingEl) loadingEl.classList.remove("hidden");
      if (runViewEl) runViewEl.classList.add("hidden");
      // Clear stale per-tab page cache so the refreshed view shows updated counts.
      state.analyticsRun.tabPageData = {};
      await fetchAnalyticsRunDetail();
      if (loadingEl) loadingEl.classList.add("hidden");
      if (runViewEl) runViewEl.classList.remove("hidden");
      showToast(
        `<strong>AI decisions applied</strong><br>` +
        `<span style="color:#86efac;">✓ ${approved.toLocaleString()} approved</span>&ensp;` +
        `<span style="color:#fca5a5;">✗ ${rejected.toLocaleString()} rejected</span>`,
        "success", 6000
      );
    } catch (e) {
      // Make sure overlay is hidden if something goes wrong
      const loadingEl = $("#analytics-run-loading");
      const runViewEl = $("#view-analytics-run");
      if (loadingEl) loadingEl.classList.add("hidden");
      if (runViewEl) runViewEl.classList.remove("hidden");
      showToast("Failed to apply AI decisions: " + (e.message || e), "error");
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> Apply`;
      }
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
      vettingMode: "cpg",      // "cpg" | "medical"
      sheets: [],              // list of sheet names (Excel only; empty for CSV)
      selectedSheet: "",       // which sheet to read (empty = active sheet)
      passthroughCols: new Set(), // header names the user wants carried into the export
    };

    awizEl.kicker.textContent = "Find & vet ASINs";
    awizEl.title.textContent  = "Upload vendor catalog";
    awizEl.mapHint.textContent= "Tell us which column is which.";

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
    // Reset vetting-mode toggle to CPG default
    document.querySelectorAll(".vetting-mode-btn").forEach(b => {
      const isCpg = b.dataset.mode === "cpg";
      b.style.background = isCpg ? "#3b82f6" : "#f8fafc";
      b.style.color      = isCpg ? "#fff"    : "#64748b";
      b.classList.toggle("active", isCpg);
    });
    const hint = $("#awiz-mode-hint");
    if (hint) hint.textContent = "CPG: UPC is primary identifier (+50 pts). Medical: MPN is primary identifier (+40 pts exact).";
    // Clear passthrough pills so stale selections don't carry over to the next run.
    const ptList = $("#awiz-passthrough-list");
    if (ptList) Array.from(ptList.querySelectorAll(".pt-pill")).forEach(p => p.remove());
    const ptEmpty = $("#awiz-passthrough-empty");
    if (ptEmpty) ptEmpty.classList.remove("hidden");
    // Reset sheet selector — hide the bar, clear options, keep the cs-wrap
    // wrapper intact (enhanceSelect only runs once; rebuildCustomMenu syncs it).
    const sheetBar = $("#awiz-sheet-bar");
    const sheetSel = $("#awiz-sheet-select");
    if (sheetBar) sheetBar.classList.add("hidden");
    if (sheetSel) {
      sheetSel.innerHTML = "";
      delete sheetSel.dataset.wired;
      // Sync the custom trigger label to the now-empty select.
      rebuildCustomMenu(sheetSel);
    }
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
      const j = await _fetchAwizPreview(file, "");
      _applyAwizPreview(j, file);
      // Auto-advance to "pick header row" — the only sensible next action.
      state.awiz.step = 2;
      renderAwizStep();
    } catch (e) {
      awizEl.fileMeta.textContent = `Could not parse file: ${e.message}`;
      state.awiz.file = null;
      state.awiz.preview = null;
    }
  }

  // Call /api/analytics/preview for the given file + optional sheet name.
  async function _fetchAwizPreview(file, sheetName) {
    const bytes = await file.arrayBuffer();
    const blob = new Blob([bytes], { type: file.type || "application/octet-stream" });
    const fd = new FormData();
    fd.append("catalog_file", blob, file.name);
    if (sheetName) fd.append("sheet_name", sheetName);
    return api("/api/analytics/preview", { method: "POST", body: fd, form: true });
  }

  // Apply a preview response to state.awiz.
  function _applyAwizPreview(j, file) {
    if (!state.awiz || !j) return;   // wizard closed mid-load, or empty response
    state.awiz.preview = j;
    state.awiz.sheets = j.sheets || [];
    state.awiz.selectedSheet = j.active_sheet || "";
    const sheetInfo = state.awiz.sheets.length > 1
      ? ` · ${state.awiz.sheets.length} sheets`
      : "";
    awizEl.fileMeta.textContent = `${fmtKB(file.size)} · ${j.total_rows || 0} rows · ${j.max_cols || 0} columns${sheetInfo}`;
    // Best-guess header row: first row with ≥ 3 non-empty cells.
    state.awiz.headerRowIdx = _guessHeaderRow(j.rows);
    state.awiz.runName = file.name.replace(/\.[^.]+$/, "");
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
        showToast("Pick a file first — .xlsx, .xls, .csv or .tsv.", "error");
        return;
      }
      state.awiz.step = 2;
      renderAwizStep();
      return;
    }

    if (step === 2) {
      if (state.awiz.headerRowIdx == null) {
        showToast("Click the row that holds your column headers.", "error");
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

    // ---- Sheet selector ----
    const sheetBar = $("#awiz-sheet-bar");
    const sheetSel = $("#awiz-sheet-select");
    const sheets = state.awiz.sheets || [];
    if (sheetBar && sheetSel) {
      if (sheets.length > 1) {
        sheetBar.classList.remove("hidden");
        // Rebuild native options then sync the custom UI.
        sheetSel.innerHTML = sheets.map(s =>
          `<option value="${escapeHtml(s)}"${s === state.awiz.selectedSheet ? " selected" : ""}>${escapeHtml(s)}</option>`
        ).join("");
        sheetSel.value = state.awiz.selectedSheet || sheets[0];
        // First render: wrap in the shared custom-dropdown component.
        enhanceSelect(sheetSel);
        // Subsequent renders: sync the trigger label + open menu (if any).
        rebuildCustomMenu(sheetSel);
        // Wire change listener once on the native select — the cs-* component
        // dispatches "change" on it when the user picks an option.
        if (!sheetSel.dataset.wired) {
          sheetSel.dataset.wired = "1";
          sheetSel.addEventListener("change", async () => {
            // Capture the file up front — the wizard can be closed/reset
            // (state.awiz → null) while the preview request is in flight, and
            // a stale resolution must not throw "Cannot read properties of
            // null (reading 'file')".
            if (!state.awiz || !state.awiz.file) return;
            const file = state.awiz.file;
            const chosen = sheetSel.value;
            if (!chosen || chosen === state.awiz.selectedSheet) return;
            const loadingEl = $("#awiz-sheet-loading");
            if (loadingEl) loadingEl.classList.remove("hidden");
            if (awizEl.next) awizEl.next.disabled = true;
            try {
              const j = await _fetchAwizPreview(file, chosen);
              if (!state.awiz) return;           // wizard closed during load — abort silently
              _applyAwizPreview(j, file);
              state.awiz.selectedSheet = chosen;
              renderAwizPreview();
            } catch (e) {
              if (state.awiz) {                  // only surface real errors while the wizard is open
                showToast(`Could not load sheet: ${e.message}`, "error");
                sheetSel.value = state.awiz.selectedSheet || "";
                rebuildCustomMenu(sheetSel);
              }
            } finally {
              if (loadingEl) loadingEl.classList.add("hidden");
              if (awizEl.next) awizEl.next.disabled = false;
            }
          });
        }
      } else {
        sheetBar.classList.add("hidden");
      }
    }

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
  // Which columns hold data in the preview sample (rows after the header row)?
  // Shared by the mapping dropdowns and the carry-through pills so both hide
  // dead/empty columns.  Returns hasDataRows=false when there's nothing to
  // judge, so callers fall back to "show everything".
  function _awizPopulatedCols() {
    const previewRows = state.awiz?.preview?.rows || [];
    const headerIdx = state.awiz?.headerRowIdx ?? 0;
    const populated = new Set();
    let hasDataRows = false;
    for (let r = headerIdx + 1; r < previewRows.length; r++) {
      hasDataRows = true;
      (previewRows[r].cells || []).forEach((c, i) => {
        if (String(c ?? "").trim() !== "") populated.add(i);
      });
    }
    return { populated, hasDataRows };
  }

  function renderAwizMapping() {
    // Only list columns that actually contain data — empty/unused columns
    // (e.g. a stray "Column G") just clutter the pickers.  A column that's
    // already mapped is always kept so a selection never silently disappears.
    const { populated, hasDataRows } = _awizPopulatedCols();
    const mapped = new Set();
    ["upc", "itemid", "title", "search_title", "brand"].forEach(k => {
      const v = state.awiz.mapping[k];
      if (v !== "" && v != null) mapped.add(Number(v));
    });
    const opts = [`<option value="">— none —</option>`]
      .concat(state.awiz.headers
        .map((h, i) => ({ h, i }))
        .filter(({ i }) => !hasDataRows || populated.has(i) || mapped.has(i))
        .map(({ h, i }) => `<option value="${i}">${escapeHtml(h)}</option>`))
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

    // Upgrade the native selects to the custom dropdown component so they match
    // the rest of the app's lists.  enhanceSelect wraps once; rebuildCustomMenu
    // re-syncs the trigger label after the options/value were just reset.
    [awizEl.mapUpc, awizEl.mapItemId, awizEl.mapTitle,
     awizEl.mapSearchTitle, awizEl.mapBrandCol].forEach(sel => {
      enhanceSelect(sel, { block: true });
      rebuildCustomMenu(sel);
    });

    // Apply the current brand mode to show/hide the right input.
    function _applyBrandMode(mode) {
      state.awiz.brandMode = mode;
      const isCol = mode === "col";
      awizEl.mapBrand.classList.toggle("hidden", isCol);
      // Toggle the custom-dropdown WRAPPER (the native select is visually
      // hidden inside it after enhanceSelect), falling back to the select.
      const brandColWrap = awizEl.mapBrandCol.closest(".cs-wrap") || awizEl.mapBrandCol;
      brandColWrap.classList.toggle("hidden", !isCol);
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
      renderAwizPassthrough();
    };

    const wireSelect = (sel, key) => {
      sel.onchange = () => {
        state.awiz.mapping[key] = sel.value;
        renderAwizMappingPreview();
        renderAwizPassthrough();  // re-compute available/unavailable pills
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

    // Apply current vetting mode so step-3 labels/highlights are correct
    _awizSetVettingMode(state.awiz.vettingMode || "cpg");

    renderAwizMappingPreview();
    renderAwizPassthrough();
  }

  // ---- Passthrough column picker (step 3) -----------------------------------
  // Renders a row of toggle pills — one per header that is NOT already assigned
  // to a primary mapping field.  Clicking a pill adds/removes it from the set
  // of columns to carry into the export.
  function renderAwizPassthrough() {
    const container = $("#awiz-passthrough-list");
    const emptyMsg  = $("#awiz-passthrough-empty");
    if (!container || !state.awiz) return;

    const headers = state.awiz.headers || [];
    if (!headers.length) {
      if (emptyMsg) emptyMsg.classList.remove("hidden");
      // Clear any existing pills
      Array.from(container.children).forEach(c => { if (c !== emptyMsg) c.remove(); });
      return;
    }

    // Collect header names already used for primary mapping (skip those).
    const usedIndices = new Set();
    const m = state.awiz.mapping || {};
    ["upc", "itemid", "title", "search_title"].forEach(k => {
      if (m[k] !== "" && m[k] != null) usedIndices.add(Number(m[k]));
    });
    if (state.awiz.brandMode === "col" && m.brand !== "" && m.brand != null) {
      usedIndices.add(Number(m.brand));
    }

    // Which columns actually contain data in the preview sample?  An empty
    // column carries no useful info, so we hide it — this keeps a file with
    // hundreds of blank columns from flooding the picker with dead pills.
    // (Judged on the previewed rows; a column blank across all of them is
    // treated as empty.)  Falls back to "show all" when there are no data rows.
    const { populated, hasDataRows } = _awizPopulatedCols();

    const available = headers
      .map((h, i) => ({ h, i }))
      .filter(({ i }) => !usedIndices.has(i))
      .filter(({ i }) => !hasDataRows || populated.has(i));

    if (emptyMsg) emptyMsg.classList.toggle("hidden", available.length > 0);

    // Build pill for each available header.
    const pt = state.awiz.passthroughCols;
    const availableIdx = new Set(available.map(a => a.i));

    // Remove pills for columns no longer available (mapped away or now empty).
    Array.from(container.querySelectorAll(".pt-pill")).forEach(el => {
      const idx = Number(el.dataset.colIdx);
      if (!availableIdx.has(idx)) {
        const hdr = headers[idx];
        if (hdr) pt.delete(hdr);
        el.remove();
      }
    });

    // Add or update pills.
    available.forEach(({ h, i }) => {
      let pill = container.querySelector(`.pt-pill[data-col-idx="${i}"]`);
      if (!pill) {
        pill = document.createElement("button");
        pill.type = "button";
        pill.className = "pt-pill";
        pill.dataset.colIdx = i;
        pill.dataset.header = h;
        pill.addEventListener("click", () => {
          const header = pill.dataset.header;
          if (state.awiz.passthroughCols.has(header)) {
            state.awiz.passthroughCols.delete(header);
          } else {
            state.awiz.passthroughCols.add(header);
          }
          renderAwizPassthrough();
        });
        container.appendChild(pill);
      }
      pill.textContent = h;
      pill.classList.toggle("selected", pt.has(h));
    });
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
    if (!m.title) {
      // Pick the real product-title/description column. Skip supplier/company/number
      // columns (e.g. "Vendor Name" = "Scholls", "Product Number") — a bare "name"
      // match used to grab "Vendor Name" and show the brand as the whole title.
      const bad = (h) => /(vendor|supplier|company|brand)name|number/.test(h);
      const ranked = [/description|title/, /productname|itemname/, /product|name/];
      for (const re of ranked) {
        const idx = state.awiz.headers.findIndex(h => { const n = norm(h); return !bad(n) && re.test(n); });
        if (idx >= 0) { m.title = String(idx); break; }
      }
    }
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
    const m    = state.awiz.mapping;
    const mode = (state.awiz?.vettingMode || "cpg").toLowerCase();
    if (mode === "medical") {
      // Medical: Item ID is the primary identifier — UPC is optional
      if (!m.itemid) return "Item ID / Part Number column is required for Medical catalog searches.";
    } else {
      // CPG: UPC/EAN is the primary identifier — Item ID is optional
      if (!m.upc) return "UPC / EAN column is required — it's the one we search on Amazon.";
    }
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
      showToast("No file attached — please go back to step 1.", "error");
      return;
    }
    const name = (awizEl.runName.value || state.awiz.runName || "Untitled run").trim();

    // Search methods now live in wizard step 4 (not on the Analytics card).
    const methods = [];
    if ($("#awiz-search-upc")?.checked)    methods.push("UPC");
    if ($("#awiz-search-itemid")?.checked) methods.push("ItemID");
    if ($("#awiz-search-title")?.checked)  methods.push("Title");
    if (methods.length === 0) {
      showToast("Pick at least one search method (UPC / Item ID / Title).", "error");
      return;
    }
    // 0 = unlimited (walk every page Amazon returns); positive = explicit cap.
    const _ppRaw = parseInt($("#awiz-title-pages")?.value ?? "0", 10);
    const pagesPerTitle = Number.isNaN(_ppRaw) ? 0 : Math.max(0, _ppRaw);
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
    const minRankWiz = _parseRank($("#awiz-min-rank")?.value);
    const maxRankWiz = _parseRank($("#awiz-max-rank")?.value);
    fd.append("min_rank", String(minRankWiz));
    fd.append("max_rank", String(maxRankWiz));
    fd.append("vetting_mode", state.awiz.vettingMode || "cpg");
    if (state.awiz.selectedSheet) fd.append("sheet_name", state.awiz.selectedSheet);
    const ptCols = Array.from(state.awiz.passthroughCols || []);
    if (ptCols.length) fd.append("passthrough_cols", JSON.stringify(ptCols));

    awizEl.next.disabled = true;
    awizEl.next.textContent = "Starting…";
    try {
      const result = await api("/api/analytics/runs", { method: "POST", body: fd, form: true });
      console.log("[awiz] run created", result);
      if (result?.sp_api_configured === false) {
        showToast(
          "Run created, but SP-API credentials are not configured. " +
          "Add AMZ_CLIENT_ID / AMZ_CLIENT_SECRET / REFRESH_TOKEN to .env and restart " +
          "the server to actually search Amazon."
        , "error");
      }
      closeAwiz();
      // Jump straight to the detail view so the user sees the progress bar.
      if (result?.run_id) {
        openAnalyticsRunDetail(result.run_id);
      } else {
        loadAnalyticsRuns();
      }
    } catch (e) {
      showToast(`Could not start run: ${e.message || e}`, "error");
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

  // =========================================================================
  //  Brand Analytics
  // =========================================================================

  // ---- Poll helpers -------------------------------------------------------
  function _clearBARPoll() {
    const ba = state.brandAnalytics;
    if (ba.run.poll) { clearTimeout(ba.run.poll); ba.run.poll = null; }
    if (ba.runsPoll)  { clearTimeout(ba.runsPoll);  ba.runsPoll = null; }
  }

  // ---- Runs list ----------------------------------------------------------
  async function loadBrandAnalyticsRuns() {
    try {
      const j = await api("/api/brand-analytics/runs");
      state.brandAnalytics.runs = j.runs || [];
      renderBARuns();
      const anyActive = state.brandAnalytics.runs.some(r => ["Pending","Searching"].includes(r.status));
      if (anyActive && state.currentView === "brand-analytics") {
        state.brandAnalytics.runsPoll = setTimeout(() => {
          state.brandAnalytics.runsPoll = null;
          if (state.currentView === "brand-analytics") loadBrandAnalyticsRuns();
        }, 2500);
      }
    } catch(e) { /* silent */ }
  }

  function renderBARuns() {
    const body = $("#ba-runs-body");
    if (!body) return;
    const runs = state.brandAnalytics.runs;
    if (!runs.length) {
      body.innerHTML = `<div class="empty-state" style="padding:24px 12px;"><div class="text-sm" style="color:#6b7480;">No runs yet — click New Search to start.</div></div>`;
      return;
    }
    body.innerHTML = runs.map(r => {
      const terms = Array.isArray(r.search_terms) ? r.search_terms : [];
      const statusColor = r.status === "Complete" ? "#16a34a"
        : r.status === "Error" ? "#dc2626"
        : r.status === "Stopped" ? "#64748b" : "#4f46e5";
      return `<div class="ba-run-row" data-run-id="${r.id}" style="cursor:pointer;">
        <div>
          <div class="font-semibold text-sm" style="color:var(--navy-800);">${escapeHtml(r.name)}</div>
          <div class="text-xs mt-0.5" style="color:#6b7480;">${escapeHtml(terms.join(", "))}</div>
        </div>
        <span class="ba-run-badge ${r.search_type}">${escapeHtml(r.search_type)}</span>
        <span class="text-xs" style="color:#64748b;">${r.item_count ?? 0} ASINs</span>
        <span class="text-xs font-semibold" style="color:${statusColor};">${escapeHtml(r.status)}</span>
        <button class="btn btn-secondary text-xs">Open →</button>
      </div>`;
    }).join("");
    body.querySelectorAll("[data-run-id]").forEach(el => {
      el.addEventListener("click", () => openBARun(parseInt(el.dataset.runId)));
    });
  }

  $("#ba-refresh-runs")?.addEventListener("click", loadBrandAnalyticsRuns);

  // ---- Run detail ---------------------------------------------------------
  async function openBARun(runId) {
    const ba = state.brandAnalytics;
    ba.run.id   = runId;
    ba.run.page = 1;
    ba.run.search = "";
    ba.run._firstRender = true;   // triggers entrance animations on first render only
    if (ba.run.poll) { clearTimeout(ba.run.poll); ba.run.poll = null; }

    $$("main > div").forEach(el => el.classList.add("hidden"));
    const barView = $("#view-brand-analytics-run");
    if (barView) {
      barView.classList.remove("hidden");
      barView.classList.remove("view-enter-anim");
      void barView.offsetWidth;
      barView.classList.add("view-enter-anim");
      barView.addEventListener("animationend", () => barView.classList.remove("view-enter-anim"), { once: true });
    }
    state.currentView = "brand-analytics-run";
    $$(".sidebar .nav-item[data-view]").forEach(n => n.classList.remove("active"));
    $(`.sidebar .nav-item[data-view='brand-analytics']`)?.classList.add("active");

    await fetchBARun();
  }

  async function fetchBARun() {
    const ba = state.brandAnalytics;
    if (!ba.run.id) return;
    const params = new URLSearchParams({
      limit:    ba.run.pageSize,
      offset:   (ba.run.page - 1) * ba.run.pageSize,
      sort_key: ba.run.sortKey,
      sort_dir: ba.run.sortDir,
    });
    if (ba.run.search) params.set("search", ba.run.search);
    try {
      const j = await api(`/api/brand-analytics/runs/${ba.run.id}?${params}`);
      ba.run.data = j;
      renderBARun();
      const active = ["Pending","Searching"].includes(j.run?.status);
      const aiFilling = j.run?.ai_fill_status === "running";
      if (active || aiFilling) {
        ba.run.poll = setTimeout(() => {
          ba.run.poll = null;
          if (state.currentView === "brand-analytics-run") fetchBARun();
        }, 2000);
      }
    } catch(e) { /* silent */ }
  }

  function renderBARun() {
    const ba = state.brandAnalytics;
    const j  = ba.run.data;
    if (!j) return;
    const run = j.run;
    const items = j.items || [];

    // Header
    const titleEl = $("#ba-run-title");
    if (titleEl) titleEl.textContent = run.name || "Run Detail";
    const subEl = $("#ba-run-subtitle");
    if (subEl) {
      const terms = Array.isArray(run.search_terms) ? run.search_terms.join(", ") : "";
      subEl.textContent = terms;
    }

    // Status pill
    const pillEl = $("#ba-run-status-pill");
    const pillTxt = $("#ba-run-status-text");
    if (pillTxt) pillTxt.textContent = run.status;
    if (pillEl) {
      pillEl.style.background = run.status === "Complete" ? "#dcfce7"
        : run.status === "Error" ? "#fee2e2"
        : run.status === "Stopped" ? "#f1f5f9"
        : "#ede9fe";
      pillEl.style.color = run.status === "Complete" ? "#15803d"
        : run.status === "Error" ? "#991b1b"
        : run.status === "Stopped" ? "#475569"
        : "#6d28d9";
    }

    // Toolbar buttons
    const isComplete = run.status === "Complete";
    const isActive   = ["Pending","Searching"].includes(run.status);
    const aiFillActive = run.ai_fill_status === "running";
    const aiFillDone = !aiFillActive;
    const aiFillBtn = $("#ba-run-ai-fill-btn");
    if (aiFillBtn) {
      aiFillBtn.classList.toggle("hidden", !isComplete);
      aiFillBtn.disabled = aiFillActive;
      aiFillBtn.textContent = aiFillActive ? "AI Fill Running…" : "+ AI Fill";
      aiFillBtn.style.opacity = aiFillActive ? "0.6" : "";
      aiFillBtn.style.cursor  = aiFillActive ? "not-allowed" : "";
    }
    $("#ba-run-export-btn")?.classList.toggle("hidden", !isComplete);
    $("#ba-run-filter-categories-btn")?.classList.toggle("hidden", !isComplete);
    $("#ba-run-stop-btn")?.classList.toggle("hidden", !isActive);
    $("#ba-run-delete-btn")?.classList.remove("hidden");

    // Progress card
    const progressCard = $("#ba-run-progress-card");
    if (progressCard) {
      progressCard.classList.toggle("hidden", !isActive);
      if (isActive) {
        const done  = run.progress_done  || 0;
        const total = run.progress_total || 0;
        const unlimited = total === 0;
        const pct   = unlimited ? 100 : Math.round((done / total) * 100);
        const barEl = $("#ba-run-progress-bar");
        if (barEl) {
          barEl.style.width = unlimited ? "100%" : `${pct}%`;
          barEl.style.animation = unlimited ? "pulse-bar 1.5s ease-in-out infinite" : "none";
        }
        const phaseEl = $("#ba-run-progress-phase");
        if (phaseEl) phaseEl.textContent = run.progress_phase || "Searching…";
        const cntEl = $("#ba-run-progress-counts");
        if (cntEl) cntEl.textContent = unlimited ? `${done} pages` : `${done} / ${total}`;
      }
    }

    // AI Fill progress card
    const aiFillCard = $("#ba-run-ai-fill-card");
    if (aiFillCard) {
      const aiFilling = run.ai_fill_status === "running";
      aiFillCard.classList.toggle("hidden", !aiFilling);
      if (aiFilling) {
        const done  = run.ai_fill_done  || 0;
        const total = run.ai_fill_total || 1;
        const pct   = Math.round((done / total) * 100);
        const barEl = $("#ba-run-ai-fill-bar");
        if (barEl) barEl.style.width = `${pct}%`;
        const cntEl = $("#ba-run-ai-fill-counts");
        if (cntEl) cntEl.textContent = `${done} / ${total}`;
      }
    }

    // Stats — mode-aware
    const stats = j.stats || {};
    const isMedical = run.vetting_mode === "medical";
    const statsEl = $("#ba-run-stats");
    if (statsEl) {
      statsEl.classList.toggle("hidden", !isComplete && !stats.total);
      $("#ba-stat-brands").textContent  = Array.isArray(run.search_terms) ? run.search_terms.length : "—";
      $("#ba-stat-asins").textContent   = stats.total ?? "—";
      $("#ba-stat-upc").textContent     = stats.with_upc ?? "—";
      $("#ba-stat-mpn").textContent     = stats.with_mpn ?? "—";
      const missingCount = isMedical ? (stats.missing_mpn ?? "—") : (stats.missing_upc ?? "—");
      const missingEl = $("#ba-stat-missing");
      if (missingEl) {
        missingEl.textContent = missingCount;
        const labelEl = missingEl.previousElementSibling;
        if (labelEl) labelEl.textContent = isMedical ? "Missing MPN" : "Missing UPC/EAN";
      }

      // Entrance animations — once per run open
      if (state.brandAnalytics.run._firstRender) {
        state.brandAnalytics.run._firstRender = false;
        _animateStats(statsEl);
        _animateProgressBars(document.getElementById("view-brand-analytics-run"));
      }
    }

    // Highlight priority columns based on mode
    const thUpc = $("#ba-th-upc"), thEan = $("#ba-th-ean"), thMpn = $("#ba-th-mpn");
    const priStyle = "background:#312e81;color:#fff;";
    const normStyle = "";
    if (thUpc) thUpc.style.cssText = isMedical ? normStyle : priStyle;
    if (thEan) thEan.style.cssText = isMedical ? normStyle : priStyle;
    if (thMpn) thMpn.style.cssText = isMedical ? priStyle : normStyle;

    // Freshness banner
    const freshnessEl = $("#ba-run-freshness-banner");
    if (freshnessEl && run.last_asin_updated_at) {
      try {
        const dt = new Date(run.last_asin_updated_at);
        const ageDays = Math.round((Date.now() - dt.getTime()) / 86400000);
        if (ageDays > 7) {
          freshnessEl.classList.remove("hidden");
          const ftxt = $("#ba-run-freshness-text");
          if (ftxt) ftxt.textContent = `Last updated ${ageDays} day${ageDays === 1 ? "" : "s"} ago`;
        } else {
          freshnessEl.classList.add("hidden");
        }
      } catch { freshnessEl.classList.add("hidden"); }
    } else if (freshnessEl) {
      freshnessEl.classList.add("hidden");
    }

    // Amazon brand names panel — shown only when run is Complete
    // Surfaces the exact brand field values Amazon has stored (e.g. "Hartmann H")
    // so users know what names to add for better recall on a re-run.
    const brandNamesPanel = $("#ba-run-brand-names-panel");
    if (brandNamesPanel && isComplete) {
      brandNamesPanel.classList.remove("hidden");
      const container = $("#ba-run-brand-names-list");
      // Wire toggle button once (guard against re-wiring on every poll tick)
      const toggleBtn = $("#ba-brand-names-toggle");
      const dropdown  = $("#ba-brand-names-dropdown");
      const chevron   = $("#ba-brand-names-chevron");
      if (toggleBtn && !toggleBtn.dataset.wired) {
        toggleBtn.dataset.wired = "1";
        let _bnOpen = false;
        function _closeBrandDropdown() {
          _bnOpen = false;
          if (dropdown) dropdown.style.display = "none";
          if (chevron)  chevron.style.transform = "rotate(0deg)";
        }
        toggleBtn.addEventListener("click", e => {
          e.stopPropagation();
          _bnOpen = !_bnOpen;
          if (dropdown) dropdown.style.display = _bnOpen ? "block" : "none";
          if (chevron)  chevron.style.transform = _bnOpen ? "rotate(180deg)" : "rotate(0deg)";
        });
        document.addEventListener("click", e => {
          if (_bnOpen && brandNamesPanel && !brandNamesPanel.contains(e.target)) {
            _closeBrandDropdown();
          }
        });
      }
      if (container && !container.dataset.loaded) {
        container.dataset.loaded = "1";
        container.innerHTML = `<span style="color:#94a3b8;font-size:12px;">Loading…</span>`;
        api(`/api/brand-analytics/runs/${state.brandAnalytics.run.id}/brand-names`)
          .then(data => {
            const names = (data.brand_names || []).filter(n => n.brand_name && n.brand_name !== "(unknown)");
            const searchTerms = Array.isArray(run.search_terms) ? run.search_terms.map(s => s.toLowerCase()) : [];
            if (!names.length) {
              container.innerHTML = `<span style="color:#94a3b8;font-size:12px;">No brand names recorded (older run)</span>`;
              return;
            }
            // Count "new" names and update the badge on the toggle button
            const newCount = names.filter(n => !searchTerms.includes(n.brand_name.toLowerCase())).length;
            const newBadge = $("#ba-brand-names-new-badge");
            if (newBadge && newCount > 0) {
              newBadge.textContent = `${newCount} new`;
              newBadge.classList.remove("hidden");
            }
            container.innerHTML = names.map(n => {
              const isNew = !searchTerms.includes(n.brand_name.toLowerCase());
              const badge = isNew
                ? `<span style="font-size:10px;background:#fef3c7;color:#92400e;border-radius:4px;padding:1px 5px;margin-left:4px;font-weight:700;">new</span>`
                : "";
              return `<span style="display:inline-flex;align-items:center;background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;padding:4px 10px;font-size:12px;gap:4px;">
                <span style="font-weight:500;color:#1e293b;">${escapeHtml(n.brand_name)}</span>
                <span style="color:#94a3b8;">(${n.count.toLocaleString()})</span>
                ${badge}
              </span>`;
            }).join("");
          })
          .catch(() => { container.innerHTML = `<span style="color:#94a3b8;font-size:12px;">—</span>`; });
      }
    } else if (brandNamesPanel) {
      brandNamesPanel.classList.add("hidden");
    }

    // Items table
    const tbody = $("#ba-run-items-body");
    if (!tbody) return;
    if (!items.length) {
      tbody.innerHTML = `<tr><td colspan="12" class="text-center" style="color:#94a3b8;padding:24px;">${isActive ? "Searching…" : "No results."}</td></tr>`;
    } else {
      tbody.innerHTML = items.map(item => {
        const img = item.image_url
          ? `<img src="${escapeHtml(item.image_url)}" alt="" loading="lazy" />`
          : `<div style="width:36px;height:36px;background:#f1f5f9;border-radius:4px;"></div>`;
        const idCell = (val, aiVal, field) => {
          // AI-filled values appear directly in the main column — no badge, no separate column.
          // Pre-fill the inline editor with the displayed value so the user can confirm/correct it.
          const display = val || aiVal || "";
          const style = `cursor:pointer;min-width:80px;padding:4px 6px;border-radius:4px;transition:background 0.15s;${display ? "" : "color:#94a3b8;"}`;
          return `<td class="ba-inline-cell" data-asin="${escapeHtml(item.asin)}" data-field="${field}" data-val="${escapeHtml(display)}" style="${style}" title="Click to edit">${escapeHtml(display||"—")}</td>`;
        };
        return `<tr>
          <td>${escapeHtml(item.brand_searched)}</td>
          <td><a href="https://amazon.com/dp/${escapeHtml(item.asin)}" target="_blank" style="color:#4f46e5;text-decoration:none;" title="View on Amazon">${escapeHtml(item.asin)}</a></td>
          <td>${img}</td>
          <td style="max-width:220px;"><div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(item.title||'')}">${escapeHtml(item.title||"—")}</div></td>
          <td>${item.bsr != null ? item.bsr.toLocaleString() : "—"}</td>
          <td>${escapeHtml(item.bsr_category||"—")}</td>
          <td style="white-space:nowrap;">${item.pack_qty ? escapeHtml(item.pack_qty) : "<span style='color:#94a3b8'>—</span>"}</td>
          <td style="white-space:nowrap;">${item.uom_qty ? escapeHtml(item.uom_qty) : "<span style='color:#94a3b8'>—</span>"}</td>
          ${idCell(item.upc,  item.ai_upc,  "upc")}
          ${idCell(item.ean,  item.ai_ean,  "ean")}
          ${idCell(item.gtin, item.ai_gtin, "gtin")}
          ${idCell(item.mpn,  item.ai_mpn,  "mpn")}
        </tr>`;
      }).join("");

      // Inline cell click-to-edit
      tbody.querySelectorAll(".ba-inline-cell").forEach(td => {
        td.addEventListener("mouseenter", () => { td.style.background = "#f1f5f9"; });
        td.addEventListener("mouseleave", () => { if (!td.dataset.editing) td.style.background = ""; });
        td.addEventListener("click", () => {
          if (td.dataset.editing) return;
          td.dataset.editing = "1";
          const cur = td.dataset.val || "";
          td.innerHTML = `<input type="text" value="${escapeHtml(cur)}" style="width:100%;min-width:80px;padding:2px 4px;border:1px solid #6366f1;border-radius:4px;font-size:12px;outline:none;" />`;
          const inp = td.querySelector("input");
          inp.focus(); inp.select();
          const save = async () => {
            const newVal = inp.value.trim();
            delete td.dataset.editing;
            td.style.background = "";
            const runId = state.brandAnalytics.run.id;
            const asin  = td.dataset.asin;
            const field = td.dataset.field;
            if (newVal === cur) { td.textContent = cur || "—"; td.dataset.val = cur; return; }
            try {
              await api(`/api/brand-analytics/runs/${runId}/items/${asin}`, {
                method: "PATCH",
                body: { [field]: newVal || null },
              });
              td.dataset.val = newVal;
              td.textContent = newVal || "—";
            } catch(e) {
              td.textContent = cur || "—";
              showToast(`Save failed: ${e.message}`, "error");
            }
          };
          inp.addEventListener("blur", save);
          inp.addEventListener("keydown", e => {
            if (e.key === "Enter") { e.preventDefault(); inp.blur(); }
            if (e.key === "Escape") { delete td.dataset.editing; td.style.background = ""; td.textContent = cur || "—"; inp.removeEventListener("blur", save); }
          });
        });
      });
    }

    // Pagination
    const total = j.filtered_count ?? j.total_items ?? 0;
    const pageSize = ba.run.pageSize;
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    const pagEl = $("#ba-run-pagination");
    if (pagEl) {
      pagEl.innerHTML = _renderBAPagination(ba.run.page, totalPages, total);
      pagEl.querySelectorAll("[data-ba-page]").forEach(btn => {
        btn.addEventListener("click", () => {
          ba.run.page = parseInt(btn.dataset.baPage);
          fetchBARun();
        });
      });
    }
  }

  function _baIdCell(val, aiVal, asin, field) {
    if (val) return `<span>${escapeHtml(val)}</span>`;
    if (aiVal) return `<span class="ba-ai-badge">AI</span> <span style="color:#475569;">${escapeHtml(aiVal)}</span>`;
    return `<span class="ba-id-null">—</span>`;
  }

  function _renderBAPagination(page, totalPages, total) {
    if (totalPages <= 1) return `<span class="text-xs" style="color:#94a3b8;">${total.toLocaleString()} results</span>`;
    let html = `<span class="text-xs" style="color:#94a3b8;margin-right:8px;">${total.toLocaleString()} results</span>`;
    const mkBtn = (p, lbl, disabled) =>
      `<button data-ba-page="${p}" class="page-btn${disabled ? ' disabled' : ''}" ${disabled ? 'disabled' : ''}>${lbl}</button>`;
    html += mkBtn(page - 1, "‹", page === 1);
    const range = [];
    for (let i = Math.max(1, page - 2); i <= Math.min(totalPages, page + 2); i++) range.push(i);
    range.forEach(p => {
      html += `<button data-ba-page="${p}" class="page-btn${p === page ? ' active' : ''}">${p}</button>`;
    });
    html += mkBtn(page + 1, "›", page === totalPages);
    return html;
  }

  // ---- Inline edit modal --------------------------------------------------
  function _openBAEdit(btn) {
    const asin = btn.dataset.baEdit;
    $("#ba-edit-asin").value  = asin;
    $("#ba-edit-upc").value   = btn.dataset.baUpc  || "";
    $("#ba-edit-ean").value   = btn.dataset.baEan  || "";
    $("#ba-edit-gtin").value  = btn.dataset.baGtin || "";
    $("#ba-edit-mpn").value   = btn.dataset.baMpn  || "";
    $("#ba-edit-modal").classList.remove("hidden");
  }
  $("#ba-edit-cancel")?.addEventListener("click", () => $("#ba-edit-modal").classList.add("hidden"));
  $("#ba-edit-save")?.addEventListener("click", async () => {
    const runId = state.brandAnalytics.run.id;
    const asin  = $("#ba-edit-asin").value;
    try {
      await api(`/api/brand-analytics/runs/${runId}/items/${asin}`, {
        method: "PATCH",
        body: {
          upc:  $("#ba-edit-upc").value.trim()  || null,
          ean:  $("#ba-edit-ean").value.trim()  || null,
          gtin: $("#ba-edit-gtin").value.trim() || null,
          mpn:  $("#ba-edit-mpn").value.trim()  || null,
        },
      });
      $("#ba-edit-modal").classList.add("hidden");
      fetchBARun();
    } catch(e) { showToast(e.message, "error"); }
  });

  // ---- Back button --------------------------------------------------------
  $("#ba-run-back")?.addEventListener("click", () => {
    _clearBARPoll();
    state.brandAnalytics.run.id   = null;
    state.brandAnalytics.run.data = null;
    switchView("brand-analytics");
  });

  // ---- Toolbar buttons ----------------------------------------------------
  $("#ba-run-export-btn")?.addEventListener("click", () => {
    const id = state.brandAnalytics.run.id;
    if (id) window.location.href = `/api/brand-analytics/runs/${id}/export`;
  });

  // ---- Category filter modal ----------------------------------------------
  let _baCatFilterCategories = [];  // [{category, count, checked}]

  async function _openBACategoryFilter() {
    const id = state.brandAnalytics.run.id;
    if (!id) return;
    const modal  = $("#ba-category-filter-modal");
    const body   = $("#ba-cat-filter-body");
    const sumEl  = $("#ba-cat-filter-summary");
    if (!modal || !body) return;

    body.innerHTML = `<div class="text-sm text-center" style="color:#94a3b8;padding:24px 0;">Loading categories…</div>`;
    sumEl.textContent = "—";
    modal.classList.remove("hidden");

    try {
      const j = await api(`/api/brand-analytics/runs/${id}/categories`);
      // Default: all unchecked (checked = "mark for removal")
      _baCatFilterCategories = (j.categories || []).map(c => ({...c, checked: false}));
      _renderBACatFilterBody();
    } catch(e) {
      body.innerHTML = `<div class="text-sm" style="color:#b91c1c;">Failed to load: ${escapeHtml(e.message)}</div>`;
    }
  }

  function _renderBACatFilterBody() {
    const body  = $("#ba-cat-filter-body");
    const sumEl = $("#ba-cat-filter-summary");
    if (!body) return;

    const cats = _baCatFilterCategories;
    const total   = cats.reduce((s, c) => s + c.count, 0);
    const removed = cats.filter(c => c.checked).reduce((s, c) => s + c.count, 0);
    const kept    = total - removed;

    sumEl.textContent = removed > 0
      ? `${removed.toLocaleString()} ASIN${removed !== 1 ? "s" : ""} will be removed · ${kept.toLocaleString()} kept`
      : `No categories selected for removal`;

    if (!cats.length) {
      body.innerHTML = `<div class="text-sm text-center" style="color:#94a3b8;padding:16px 0;">No categories found.</div>`;
      return;
    }

    body.innerHTML = cats.map((c, i) => `
      <label class="ba-cat-row" data-cat-idx="${i}" style="${c.checked ? 'background:#fff7ed;' : ''}">
        <input type="checkbox" class="ba-cat-chk" data-cat-idx="${i}" ${c.checked ? "checked" : ""} />
        <span class="ba-cat-name" style="${c.checked ? 'text-decoration:line-through;color:#b45309;' : ''}">${escapeHtml(c.category) || "<em style='color:#94a3b8'>(Blanks)</em>"}</span>
        <span class="ba-cat-count">${c.count.toLocaleString()}</span>
      </label>
    `).join("");

    // Bind change events — re-render to apply strikethrough styling live
    body.querySelectorAll(".ba-cat-chk").forEach(chk => {
      chk.addEventListener("change", () => {
        const idx = parseInt(chk.dataset.catIdx);
        _baCatFilterCategories[idx].checked = chk.checked;
        _renderBACatFilterBody();
      });
    });
  }

  function _renderBACatSummary() {
    const sumEl = $("#ba-cat-filter-summary");
    if (!sumEl) return;
    const cats    = _baCatFilterCategories;
    const total   = cats.reduce((s, c) => s + c.count, 0);
    const removed = cats.filter(c => c.checked).reduce((s, c) => s + c.count, 0);
    const kept    = total - removed;
    sumEl.textContent = removed > 0
      ? `${removed.toLocaleString()} ASIN${removed !== 1 ? "s" : ""} will be removed · ${kept.toLocaleString()} kept`
      : `No categories selected for removal`;
  }

  function _closeBACatFilter() {
    $("#ba-category-filter-modal")?.classList.add("hidden");
  }

  $("#ba-run-filter-categories-btn")?.addEventListener("click", _openBACategoryFilter);
  $("#ba-cat-filter-close")?.addEventListener("click",  _closeBACatFilter);
  $("#ba-cat-filter-cancel")?.addEventListener("click", _closeBACatFilter);

  $("#ba-cat-filter-apply")?.addEventListener("click", async () => {
    const id = state.brandAnalytics.run.id;
    if (!id) return;
    const toRemove = _baCatFilterCategories.filter(c => c.checked).map(c => c.category);
    if (!toRemove.length) {
      _closeBACatFilter();
      return;
    }
    const applyBtn = $("#ba-cat-filter-apply");
    if (applyBtn) { applyBtn.disabled = true; applyBtn.textContent = "Removing…"; }
    try {
      const j = await api(`/api/brand-analytics/runs/${id}/filter-categories`, {
        method: "POST",
        body: { remove_categories: toRemove },
      });
      _closeBACatFilter();
      showToast(`Removed ${j.deleted.toLocaleString()} ASIN${j.deleted !== 1 ? "s" : ""} · ${j.remaining.toLocaleString()} remaining`, "success");
      // Reload the run detail
      state.brandAnalytics.run.page = 1;
      await fetchBARun();
    } catch(e) {
      showToast(`Filter failed: ${e.message}`, "error");
    } finally {
      if (applyBtn) { applyBtn.disabled = false; applyBtn.textContent = "Remove Unchecked"; }
    }
  });

  const _aiFillFields = () => {
    const v = [...document.querySelectorAll(".ba-aifill-field:checked")].map(c => c.value);
    return v.length ? v : ["mpn", "upc", "ean", "gtin"];
  };
  async function _refreshAiFillEstimate() {
    const id = state.brandAnalytics.run?.id;
    if (!id) return;
    const descEl  = $("#ba-ai-fill-modal-desc");
    const cntEl   = $("#ba-ai-fill-modal-count");
    const modelEl = $("#ba-ai-fill-modal-model");
    const costEl  = $("#ba-ai-fill-modal-cost");
    const confirmBtn = $("#ba-ai-fill-modal-confirm");
    const fields = _aiFillFields();
    if (descEl) descEl.textContent = "Loading estimate…";
    if (confirmBtn) confirmBtn.disabled = true;
    try {
      const est = await api(`/api/brand-analytics/runs/${id}/ai_fill/estimate?fields=${fields.join(",")}`);
      if (!est.available) {
        if (descEl) descEl.textContent = "No AI client configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY.";
        return;
      }
      const labels = fields.map(f => f.toUpperCase());
      if (cntEl)   cntEl.textContent   = est.item_count.toLocaleString();
      if (modelEl) modelEl.textContent = est.model;
      if (costEl)  costEl.textContent  = est.item_count === 0 ? "$0.00" : `~$${est.cost_usd_est.toFixed(4)}`;
      if (descEl)  descEl.textContent  = est.item_count === 0
        ? `All items already have ${labels.join(" / ")} filled.`
        : `AI scans each item's title & description for the selected ID(s): ${labels.join(", ")}. MPNs appear as model numbers; UPC/EAN/GTIN only when explicitly present.`;
      if (confirmBtn) confirmBtn.disabled = est.item_count === 0;
    } catch (e) {
      if (descEl) descEl.textContent = `Estimate failed: ${e.message}`;
    }
  }
  $("#ba-run-ai-fill-btn")?.addEventListener("click", async () => {
    const modal = $("#ba-ai-fill-modal");
    if (!modal || !state.brandAnalytics.run?.id) return;
    ["count", "model", "cost"].forEach(k => { const el = $(`#ba-ai-fill-modal-${k}`); if (el) el.textContent = "—"; });
    modal.classList.remove("hidden");
    _refreshAiFillEstimate();
  });
  document.querySelectorAll(".ba-aifill-field").forEach(c =>
    c.addEventListener("change", _refreshAiFillEstimate));

  $("#ba-run-ai-fill-stop")?.addEventListener("click", async () => {
    const id = state.brandAnalytics.run.id;
    if (!id) return;
    try {
      await api(`/api/brand-analytics/runs/${id}/ai_fill/stop`, {method:"POST"});
      fetchBARun();
    } catch(e) { showToast(`Stop error: ${e.message}`, "error"); }
  });

  $("#ba-ai-fill-modal-cancel")?.addEventListener("click", () => {
    $("#ba-ai-fill-modal")?.classList.add("hidden");
  });
  $("#ba-ai-fill-modal")?.addEventListener("click", (e) => {
    if (e.target === $("#ba-ai-fill-modal")) $("#ba-ai-fill-modal").classList.add("hidden");
  });

  $("#ba-ai-fill-modal-confirm")?.addEventListener("click", async () => {
    const id = state.brandAnalytics.run.id;
    if (!id) return;
    const fields = _aiFillFields();
    $("#ba-ai-fill-modal")?.classList.add("hidden");
    try {
      await api(`/api/brand-analytics/runs/${id}/ai_fill`, {method:"POST", body:{fields}});
      fetchBARun();
    } catch(e) { showToast(`AI Fill error: ${e.message}`, "error"); }
  });

  $("#ba-run-stop-btn")?.addEventListener("click", async () => {
    const id = state.brandAnalytics.run.id;
    if (!id) return;
    try {
      await api(`/api/brand-analytics/runs/${id}/control`, {
        method:"POST",
        body: {action:"stop"},
      });
      fetchBARun();
    } catch(e) { showToast(e.message, "error"); }
  });

  $("#ba-run-delete-btn")?.addEventListener("click", async () => {
    if (!(await showConfirm({
      title: "Delete this run?",
      message: "This permanently removes the run and all its data. This cannot be undone.",
      confirmText: "Delete",
      danger: true,
    }))) return;
    const id = state.brandAnalytics.run.id;
    try {
      await api(`/api/brand-analytics/runs/${id}`, {method:"DELETE"});
      _clearBARPoll();
      switchView("brand-analytics");
    } catch(e) { showToast(e.message, "error"); }
  });

  // ---- Freshness refresh --------------------------------------------------
  $("#ba-run-refresh-btn")?.addEventListener("click", () => {
    const wiz = state.brandAnalytics.wizard;
    const run = state.brandAnalytics.run.data?.run;
    if (!run) return;
    wiz.inputName  = run.name;
    wiz.searchType = run.search_type || "brand";
    openBAWizard(true);
  });

  // ---- Search/filter ------------------------------------------------------
  $("#ba-run-search-btn")?.addEventListener("click", () => {
    state.brandAnalytics.run.search = $("#ba-run-search")?.value || "";
    state.brandAnalytics.run.page   = 1;
    fetchBARun();
  });
  $("#ba-run-search")?.addEventListener("keydown", e => {
    if (e.key === "Enter") { state.brandAnalytics.run.search = e.target.value || ""; state.brandAnalytics.run.page = 1; fetchBARun(); }
  });

  // ---- Sort BSR header ---------------------------------------------------
  $("#ba-sort-bsr")?.addEventListener("click", () => {
    const ba = state.brandAnalytics;
    ba.run.sortKey = "bsr";
    ba.run.sortDir = ba.run.sortDir === "asc" ? "desc" : "asc";
    ba.run.page    = 1;
    fetchBARun();
  });

  // ---- Cache modal --------------------------------------------------------
  let _baCachePendingBody = null;
  let _baCacheForceRefresh = false;

  function _showBACacheModal(msg, cachedRunId) {
    const modal = $("#ba-cache-modal");
    const msgEl = $("#ba-cache-msg");
    if (msgEl) msgEl.textContent = msg;
    if (modal) modal.classList.remove("hidden");
    _baCachePendingBody = cachedRunId;
  }

  $("#ba-cache-use")?.addEventListener("click", () => {
    $("#ba-cache-modal").classList.add("hidden");
    if (_baCachePendingBody) openBARun(_baCachePendingBody);
  });
  $("#ba-cache-refresh")?.addEventListener("click", () => {
    $("#ba-cache-modal").classList.add("hidden");
    _baCacheForceRefresh = true;
    _submitBAWiz();
  });

  // ---- New Search button --------------------------------------------------
  $("#ba-new-search-btn")?.addEventListener("click", () => openBAWizard());

  // =========================================================================
  //  Brand Analytics Wizard
  // =========================================================================

  function openBAWizard(forceRefresh = false) {
    _baCacheForceRefresh = forceRefresh;
    const wiz = state.brandAnalytics.wizard;
    // Full reset
    wiz.step = 1;
    wiz.inputName = "";
    wiz.searchType = "brand";
    wiz.vettingMode = "cpg";
    wiz.discoveredBrands = [];
    wiz.cacheInfo = null;
    wiz.minRank = 0;
    wiz.maxRank = 0;
    wiz.minSold = 0;
    wiz.maxSold = 0;
    wiz.pagesPerBrand = 10;

    // Reset type buttons
    $$("[data-batype]").forEach(btn => {
      const isDefault = btn.dataset.batype === "brand";
      btn.classList.toggle("active", isDefault);
      btn.style.background = isDefault ? "#4f46e5" : "#f8fafc";
      btn.style.color = isDefault ? "#fff" : "#64748b";
      btn.style.borderColor = isDefault ? "#4f46e5" : "#e2e8f0";
    });
    // Reset mode buttons
    $$("[data-bamode]").forEach(btn => {
      const isDefault = btn.dataset.bamode === "cpg";
      btn.style.background = isDefault ? "#16a34a" : "#f8fafc";
      btn.style.color = isDefault ? "#fff" : "#64748b";
      btn.style.borderColor = isDefault ? "#16a34a" : "#e2e8f0";
    });

    // Pre-populate autocomplete from library
    const libNames = state.brandAnalytics.library.map(e => e.name);
    const dl = $("#ba-wiz-name-list");
    if (dl) dl.innerHTML = libNames.map(n => `<option value="${escapeHtml(n)}"></option>`).join("");

    // Reset fields
    const nameInp = $("#ba-wiz-name");
    if (nameInp) nameInp.value = "";
    const pfx = new Date().toISOString().slice(0, 10);
    const runNameInp = $("#ba-wiz-run-name");
    if (runNameInp) runNameInp.value = "Brand — " + pfx;
    const feedbackEl = $("#ba-wiz-name-feedback");
    if (feedbackEl) feedbackEl.textContent = "";
    const minInp = $("#ba-wiz-min-rank"); if (minInp) minInp.value = "0";
    const maxInp = $("#ba-wiz-max-rank"); if (maxInp) maxInp.value = "0";
    const minSoldInp = $("#ba-wiz-min-sold"); if (minSoldInp) minSoldInp.value = "0";
    const maxSoldInp = $("#ba-wiz-max-sold"); if (maxSoldInp) maxSoldInp.value = "0";
    const pgsInp = $("#ba-wiz-pages");   if (pgsInp) pgsInp.value = "10";

    _baWizGoTo(1);
    $("#ba-wizard-modal").classList.remove("hidden");
    nameInp?.focus();
  }

  // Step type buttons
  $$("[data-batype]").forEach(btn => {
    btn.addEventListener("click", () => {
      state.brandAnalytics.wizard.searchType = btn.dataset.batype;
      $$("[data-batype]").forEach(b => {
        const active = b.dataset.batype === btn.dataset.batype;
        b.style.background = active ? "#4f46e5" : "#f8fafc";
        b.style.color = active ? "#fff" : "#64748b";
        b.style.borderColor = active ? "#4f46e5" : "#e2e8f0";
      });
    });
  });

  $$("[data-bamode]").forEach(btn => {
    btn.addEventListener("click", () => {
      state.brandAnalytics.wizard.vettingMode = btn.dataset.bamode;
      $$("[data-bamode]").forEach(b => {
        const active = b.dataset.bamode === btn.dataset.bamode;
        b.style.background = active ? "#16a34a" : "#f8fafc";
        b.style.color = active ? "#fff" : "#64748b";
        b.style.borderColor = active ? "#16a34a" : "#e2e8f0";
      });
    });
  });

  function _baWizGoTo(step) {
    state.brandAnalytics.wizard.step = step;
    $$("[data-bawiz-panel]").forEach(p => p.classList.toggle("hidden", parseInt(p.dataset.bawizPanel) !== step));
    $$("[data-bawiz-indicator]").forEach(ind => {
      const n = parseInt(ind.dataset.bawizIndicator);
      ind.classList.toggle("active", n === step);
      ind.classList.toggle("done",   n < step);
    });
    const backBtn = $("#ba-wiz-back");
    const nextBtn = $("#ba-wiz-next");
    if (backBtn) backBtn.classList.toggle("hidden", step === 1);
    if (nextBtn) {
      nextBtn.textContent = step === 4 ? "Start Search" : "Next";
    }
    // Summary on step 4
    if (step === 4) _buildBAWizSummary();
  }

  function _buildBAWizSummary() {
    const wiz = state.brandAnalytics.wizard;
    const selectedSubs    = wiz.discoveredBrands.filter(b => b.selected && b.type !== "alias").map(b => b.name);
    const selectedAliases = wiz.discoveredBrands.filter(b => b.selected && b.type === "alias").map(b => b.name);
    const selected = [...selectedSubs, ...selectedAliases];
    const sumEl = $("#ba-wiz-summary");
    if (!sumEl) return;
    sumEl.innerHTML = `
      <div><b>Name:</b> ${escapeHtml(wiz.inputName)}</div>
      <div><b>Type:</b> ${escapeHtml(wiz.searchType)} · <b>Mode:</b> ${wiz.vettingMode === "medical" ? "Medical (MPN priority)" : "CPG (UPC/EAN priority)"}</div>
      <div><b>Sub-brands to search:</b> ${selectedSubs.map(n=>escapeHtml(n)).join(", ") || "—"}</div>
      ${selectedAliases.length ? `<div><b>Aliases included:</b> <span style="color:#6366f1;">${selectedAliases.map(n=>escapeHtml(n)).join(", ")}</span></div>` : ""}
      <div><b>BSR range:</b> ${wiz.minRank||0} – ${wiz.maxRank||0} (0 = no limit) · filtered in Keepa</div>
      <div><b>Units sold / mo:</b> ${wiz.minSold||0} – ${wiz.maxSold||0} (0 = no limit) · filtered in Keepa</div>
    `;
  }

  // Back / Next
  $("#ba-wiz-close")?.addEventListener("click", () => $("#ba-wizard-modal").classList.add("hidden"));
  $("#ba-wiz-back")?.addEventListener("click", () => {
    const step = state.brandAnalytics.wizard.step;
    if (step > 1) _baWizGoTo(step - 1);
  });
  $("#ba-wiz-next")?.addEventListener("click", async () => {
    const wiz = state.brandAnalytics.wizard;
    if (wiz.step === 1) {
      // Validate name
      const name = $("#ba-wiz-name")?.value.trim();
      if (!name) {
        const fb = $("#ba-wiz-name-feedback");
        if (fb) { fb.textContent = "Please enter a name."; fb.style.color = "#b91c1c"; }
        return;
      }
      wiz.inputName = name;
      const pfx = new Date().toISOString().slice(0, 10);
      const runNameInp = $("#ba-wiz-run-name");
      if (runNameInp && !runNameInp.value.trim()) runNameInp.value = name + " — " + pfx;
      _baWizGoTo(2);
      // Always show the brands list section (add-manually input is always available)
      $("#ba-wiz-brands-list")?.classList.remove("hidden");
      // Auto-discover if cached in library
      const cached = state.brandAnalytics.library.find(e => e.name.toLowerCase() === name.toLowerCase());
      if (cached) {
        const subList   = (cached.sub_brands || []);
        const aliasList = (cached.aliases     || []);
        const uniqueSubs    = [...new Set([cached.name, ...subList])];
        const uniqueAliases = [...new Set(aliasList.filter(a => !uniqueSubs.includes(a)))];
        wiz.discoveredBrands = [
          ...uniqueSubs.map(n    => ({name: n, selected: true,  type: "sub_brand"})),
          ...uniqueAliases.map(n => ({name: n, selected: false, type: "alias"})),
        ];
        _renderBABrandsCheckboxes();
        const statusEl = $("#ba-wiz-discover-status");
        if (statusEl) statusEl.textContent = "Loaded from Library ✓";
      }
    } else if (wiz.step === 2) {
      if (!wiz.discoveredBrands.filter(b => b.selected).length) {
        const fb = $("#ba-wiz-discover-status");
        if (fb) fb.textContent = "Please discover sub-brands or add at least one brand manually.";
        return;
      }
      // Copy configure defaults
      const minEl = $("#ba-wiz-min-rank");
      const maxEl = $("#ba-wiz-max-rank");
      wiz.minRank = parseInt(minEl?.value || "0") || 0;
      wiz.maxRank = parseInt(maxEl?.value || "0") || 0;
      wiz.minSold = Math.max(0, parseInt($("#ba-wiz-min-sold")?.value || "0") || 0);
      wiz.maxSold = Math.max(0, parseInt($("#ba-wiz-max-sold")?.value || "0") || 0);
      _baWizGoTo(3);
    } else if (wiz.step === 3) {
      wiz.minRank = parseInt($("#ba-wiz-min-rank")?.value || "0") || 0;
      wiz.maxRank = parseInt($("#ba-wiz-max-rank")?.value || "0") || 0;
      wiz.minSold = Math.max(0, parseInt($("#ba-wiz-min-sold")?.value || "0") || 0);
      wiz.maxSold = Math.max(0, parseInt($("#ba-wiz-max-sold")?.value || "0") || 0);
      const runName = $("#ba-wiz-run-name")?.value.trim();
      if (!runName) { showToast("Please enter a run name.", "error"); return; }
      _baWizGoTo(4);
    } else if (wiz.step === 4) {
      await _submitBAWiz();
    }
  });

  // Discover sub-brands button
  $("#ba-wiz-discover-btn")?.addEventListener("click", async () => {
    const wiz = state.brandAnalytics.wizard;
    const btn       = $("#ba-wiz-discover-btn");
    const statusEl  = $("#ba-wiz-discover-status");
    const progWrap  = $("#ba-wiz-discover-progress");
    const barEl     = $("#ba-wiz-discover-bar");
    const phaseEl   = $("#ba-wiz-discover-phase");
    const etaEl     = $("#ba-wiz-discover-eta");

    if (btn) { btn.disabled = true; btn.textContent = "Discovering…"; }
    if (statusEl) statusEl.textContent = "";

    // Animate progress: ramp to 90% over ~6s, then hold until done
    const ESTIMATED_MS = 6000;
    let startTime = Date.now();
    let animFrame;
    let pct = 0;

    function _tick() {
      const elapsed = Date.now() - startTime;
      // Ease-out curve: fast at first, slows near 90%
      pct = 90 * (1 - Math.exp(-elapsed / (ESTIMATED_MS * 0.6)));
      if (barEl) barEl.style.width = `${pct.toFixed(1)}%`;
      const remaining = Math.max(0, Math.round((ESTIMATED_MS - elapsed) / 1000));
      if (etaEl) etaEl.textContent = remaining > 0 ? `~${remaining}s remaining` : "Finishing…";
      if (phaseEl) phaseEl.textContent = elapsed < 1500 ? "Contacting AI…" : "Analyzing brand…";
      if (pct < 89.5) animFrame = requestAnimationFrame(_tick);
    }

    if (progWrap) progWrap.classList.remove("hidden");
    if (barEl) barEl.style.width = "0%";
    animFrame = requestAnimationFrame(_tick);

    try {
      const j = await api("/api/brand-analytics/discover", {
        method: "POST",
        body: {name: wiz.inputName, entity_type: wiz.searchType},
      });

      // Complete the bar
      cancelAnimationFrame(animFrame);
      if (barEl) barEl.style.width = "100%";
      if (phaseEl) phaseEl.textContent = j.cached ? "Loaded from Library ✓" : "Done ✓";
      if (etaEl) etaEl.textContent = "";

      const result = j.result || {};
      const subs    = Array.isArray(result.sub_brands) ? result.sub_brands : [wiz.inputName];
      const aliases = Array.isArray(result.aliases)    ? result.aliases    : [];
      const counts        = (result.keepa && result.keepa.counts) || {};
      const uniqueSubs    = [...new Set(subs)];
      const uniqueAliases = [...new Set(aliases.filter(a => !uniqueSubs.includes(a)))];
      wiz.discoveredBrands = [
        ...uniqueSubs.map(n    => ({name: n, selected: true,  type: "sub_brand", count: counts[n]})),
        ...uniqueAliases.map(n => ({name: n, selected: false, type: "alias",     count: counts[n]})),
      ];
      _renderBABrandsCheckboxes();
      const nVerified = uniqueSubs.filter(n => counts[n] != null).length;
      if (statusEl) statusEl.textContent = nVerified
        ? `Found ${uniqueSubs.length} sub-brand(s) — ${nVerified} verified from Keepa catalog data ✓`
        : (j.cached ? "From Library ✓" : `AI discovered ${uniqueSubs.length} sub-brand(s), ${uniqueAliases.length} alias(es) ✓`);

      // Hide progress bar after a moment
      setTimeout(() => { if (progWrap) progWrap.classList.add("hidden"); }, 1200);

      loadBrandLibrary();
    } catch(e) {
      cancelAnimationFrame(animFrame);
      if (progWrap) progWrap.classList.add("hidden");
      if (statusEl) { statusEl.textContent = `Error: ${e.message}`; statusEl.style.color = "#dc2626"; }
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "Discover Sub-brands"; }
    }
  });

  function _renderBABrandsCheckboxes() {
    const wiz = state.brandAnalytics.wizard;
    const container = $("#ba-wiz-brands-checkboxes");
    const listEl = $("#ba-wiz-brands-list");
    if (!container || !listEl) return;
    listEl.classList.remove("hidden");

    const subBrands = wiz.discoveredBrands.filter(b => b.type !== "alias");
    const aliases   = wiz.discoveredBrands.filter(b => b.type === "alias");

    const manuals = wiz.discoveredBrands.filter(b => b.type === "manual");

    const renderGroup = (items, label, labelColor) => {
      if (!items.length) return "";
      const header = `<div class="text-xs font-semibold mt-2 mb-1" style="color:${labelColor};">${label}</div>`;
      const checks = items.map(b => {
        const i = wiz.discoveredBrands.indexOf(b);
        return `<div class="flex items-center gap-2 ba-brand-row">
          <input type="checkbox" data-ba-brand="${i}" ${b.selected ? "checked" : ""} style="flex-shrink:0;" />
          <span class="text-sm" style="color:#1e293b;flex:1;">${escapeHtml(b.name)}${b.count != null ? ` <span style="color:#64748b;font-size:11px;font-weight:600;">· ${Number(b.count).toLocaleString()} on Amazon</span>` : ""}</span>
          <button type="button" data-ba-brand-delete="${i}" title="Remove" class="ba-brand-remove-btn" aria-label="Remove">×</button>
        </div>`;
      }).join("");
      return header + checks;
    };

    container.innerHTML =
      renderGroup(subBrands, "Sub-brands", "#334155") +
      renderGroup(aliases,   "Aliases / alternate names — include to also match products branded under these names", "#6366f1") +
      renderGroup(manuals,   "Manually added", "#0369a1");

    container.querySelectorAll("[data-ba-brand]").forEach(cb => {
      cb.addEventListener("change", () => {
        wiz.discoveredBrands[parseInt(cb.dataset.baBrand)].selected = cb.checked;
      });
    });

    // Delete buttons for manually-added entries
    container.querySelectorAll("[data-ba-brand-delete]").forEach(btn => {
      btn.addEventListener("click", () => {
        const i = parseInt(btn.dataset.baBrandDelete);
        wiz.discoveredBrands.splice(i, 1);
        _renderBABrandsCheckboxes();
      });
    });
  }

  function _addBABrandManually() {
    const input = $("#ba-wiz-add-brand-input");
    if (!input) return;
    const name = input.value.trim();
    if (!name) return;
    const wiz = state.brandAnalytics.wizard;
    // prevent duplicates (case-insensitive)
    const exists = wiz.discoveredBrands.some(b => b.name.toLowerCase() === name.toLowerCase());
    if (exists) {
      input.value = "";
      input.placeholder = "Already in the list";
      setTimeout(() => { input.placeholder = "Add a brand manually…"; }, 1800);
      return;
    }
    wiz.discoveredBrands.push({ name, type: "manual", selected: true });
    input.value = "";
    _renderBABrandsCheckboxes();
    // re-focus so user can keep adding
    $("#ba-wiz-add-brand-input")?.focus();
  }

  $("#ba-wiz-add-brand-btn")?.addEventListener("click", _addBABrandManually);
  $("#ba-wiz-add-brand-input")?.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); _addBABrandManually(); }
  });

  async function _submitBAWiz() {
    const wiz = state.brandAnalytics.wizard;
    const selected = wiz.discoveredBrands.filter(b => b.selected).map(b => b.name);
    if (!selected.length) {
      const fb = $("#ba-wiz-feedback");
      if (fb) fb.textContent = "Please select at least one brand.";
      return;
    }
    const runName = $("#ba-wiz-run-name")?.value.trim() || (wiz.inputName + " — " + new Date().toISOString().slice(0,10));
    const nextBtn = $("#ba-wiz-next");
    if (nextBtn) { nextBtn.disabled = true; nextBtn.textContent = "Starting…"; }
    const fb = $("#ba-wiz-feedback");
    if (fb) fb.textContent = "";
    try {
      const j = await api("/api/brand-analytics/runs", {
        method: "POST",
        body: {
          name:           runName,
          search_type:    wiz.searchType,
          search_terms:   selected,
          min_rank:       wiz.minRank,
          max_rank:       wiz.maxRank,
          min_sold:       wiz.minSold,
          max_sold:       wiz.maxSold,
          pages_per_brand:  wiz.pagesPerBrand,
          vetting_mode:     wiz.vettingMode || "cpg",
          force_refresh:    _baCacheForceRefresh,
        },
      });
      _baCacheForceRefresh = false;
      if (j.cached && !j.run_id) {
        // Server returned partial cached info — should have run_id if truly cached
      }
      if (j.cached && j.run_id) {
        $("#ba-wizard-modal").classList.add("hidden");
        _showBACacheModal(j.message || `Results from ${j.age_days} day(s) ago available.`, j.run_id);
        return;
      }
      if (j.run_id) {
        $("#ba-wizard-modal").classList.add("hidden");
        await loadBrandAnalyticsRuns();
        openBARun(j.run_id);
      }
    } catch(e) {
      if (fb) fb.textContent = `Error: ${e.message}`;
    } finally {
      if (nextBtn) { nextBtn.disabled = false; nextBtn.textContent = "Start Search"; }
    }
  }

  // =========================================================================
  //  QUICK SEARCH
  // =========================================================================

  // ---- State ----
  const _qs = {
    results:    [],        // raw candidates from last search
    filter:     "all",     // "all" | "verified" | "review" | "not_approved"
    searching:  false,
    tags: { upc: [], itemid: [] },  // multi-value chip arrays
    multiMode:  false,              // true when >1 tag was searched
  };

  // ---- Tag/chip input helpers ----
  function _qsAddTag(field, rawValue) {
    const value = rawValue.trim().replace(/,+$/, "").trim();
    if (!value) return;
    if (_qs.tags[field].includes(value)) return; // no duplicates
    _qs.tags[field].push(value);
    _qsRenderTags(field);
  }

  function _qsRemoveTag(field, idx) {
    _qs.tags[field].splice(idx, 1);
    _qsRenderTags(field);
  }

  function _qsRenderTags(field) {
    const wrap  = $(`#qs-${field}-wrap`);
    const input = $(`#qs-${field}`);
    if (!wrap || !input) return;
    // Remove old chips (keep the input element)
    wrap.querySelectorAll(".tag-chip").forEach(el => el.remove());
    // Prepend new chips before the input
    _qs.tags[field].forEach((val, i) => {
      const chip = document.createElement("span");
      chip.className = "tag-chip";
      chip.innerHTML = `${escapeHtml(val)}<button class="tag-chip-remove" title="Remove" type="button">×</button>`;
      chip.querySelector(".tag-chip-remove").addEventListener("click", (e) => {
        e.stopPropagation();
        _qsRemoveTag(field, i);
      });
      wrap.insertBefore(chip, input);
    });
    // Update placeholder
    input.placeholder = _qs.tags[field].length
      ? "add more…"
      : (field === "upc" ? "e.g. 012345678901…" : "e.g. ASL02…");
  }

  // Wire tag-input keydown on both identifier fields
  ["upc", "itemid"].forEach(field => {
    const input = $(`#qs-${field}`);
    if (!input) return;
    input.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === ",") {
        e.preventDefault();
        const val = input.value;
        if (val.trim()) {
          _qsAddTag(field, val);
          input.value = "";
        } else {
          // Empty Enter with no pending text → trigger search
          if (!val.trim()) $("#qs-search-btn")?.click();
        }
      } else if (e.key === "Backspace" && !input.value && _qs.tags[field].length) {
        // Backspace on empty input removes last chip
        _qsRemoveTag(field, _qs.tags[field].length - 1);
      }
    });
    // Also handle paste of comma-separated values
    input.addEventListener("paste", e => {
      const text = e.clipboardData?.getData("text") || "";
      if (text.includes(",") || text.includes("\n")) {
        e.preventDefault();
        text.split(/[,\n]+/).forEach(v => _qsAddTag(field, v));
        input.value = "";
      }
    });
  });

  // ---- Helpers ----
  function _qsVerdictBadge(verdict) {
    const map = {
      verified:     { bg: "#dcfce7", color: "#166534", label: "Verified"     },
      review:       { bg: "#fef9c3", color: "#854d0e", label: "Review"       },
      not_approved: { bg: "#fee2e2", color: "#991b1b", label: "Not Approved" },
    };
    const s = map[verdict] || { bg: "#f1f5f9", color: "#64748b", label: verdict };
    return `<span style="display:inline-block;padding:2px 9px;border-radius:99px;font-size:11px;font-weight:600;background:${s.bg};color:${s.color};">${s.label}</span>`;
  }

  function _qsSourcesBadge(sources) {
    return (sources || []).map(s => {
      const color = s === "UPC" ? "#1d4ed8" : s === "ItemID" ? "#7c3aed" : "#065f46";
      const bg    = s === "UPC" ? "#dbeafe" : s === "ItemID" ? "#ede9fe" : "#d1fae5";
      return `<span style="display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700;background:${bg};color:${color};margin-right:2px;">${s}</span>`;
    }).join("");
  }

  function _qsScoreBadge(conf, verdict) {
    const color = verdict === "verified" ? "#166534"
                : verdict === "review"   ? "#854d0e"
                : "#991b1b";
    const bg    = verdict === "verified" ? "#dcfce7"
                : verdict === "review"   ? "#fef9c3"
                : "#fee2e2";
    return `<span style="display:inline-block;padding:3px 10px;border-radius:6px;font-size:13px;font-weight:700;background:${bg};color:${color};">${conf}</span>`;
  }

  function _renderQsResults() {
    const tbody   = $("#qs-results-tbody");
    const summary = $("#qs-results-summary");
    if (!tbody) return;

    // Show/hide Query column based on multi-mode
    const thQuery = $("#qs-th-query");
    if (thQuery) thQuery.classList.toggle("hidden", !_qs.multiMode);

    const filtered = _qs.filter === "all"
      ? _qs.results
      : _qs.results.filter(c => c.verdict === _qs.filter);

    // Update filter button counts
    $$(".qs-filter-btn").forEach(btn => {
      const f = btn.dataset.filter;
      const count = f === "all"
        ? _qs.results.length
        : _qs.results.filter(c => c.verdict === f).length;
      btn.classList.toggle("active", f === _qs.filter);
      btn.textContent = f === "all" ? `All (${count})`
        : f === "verified"     ? `Verified (${count})`
        : f === "review"       ? `Review (${count})`
        : `Not Approved (${count})`;
    });

    const totalQueries = _qs.multiMode ? (_qs._queryCount || 1) : 1;
    if (summary) {
      summary.textContent = _qs.multiMode
        ? `${filtered.length} of ${_qs.results.length} candidates across ${totalQueries} searches`
        : `${filtered.length} of ${_qs.results.length} candidates`;
    }

    const colSpan = _qs.multiMode ? "10" : "9";
    if (filtered.length === 0) {
      tbody.innerHTML = `<tr><td colspan="${colSpan}" style="text-align:center;color:#94a3b8;padding:24px;">No candidates match this filter.</td></tr>`;
      return;
    }

    tbody.innerHTML = filtered.map(c => {
      const asinUrl  = `https://www.amazon.com/dp/${c.asin}`;
      const bsr      = c.sales_rank ? c.sales_rank.toLocaleString() : "—";
      const category = c.sales_rank_category
        ? `<span title="${escapeHtml(c.sales_rank_category)}" style="max-width:120px;display:inline-block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom;">${escapeHtml(c.sales_rank_category)}</span>`
        : "—";

      // Build reasons tooltip
      const reasons = [];
      const scores = c.scores || {};
      if (scores.size_mismatch)     reasons.push("Size mismatch");
      if (scores.count_mismatch)    reasons.push("Count mismatch");
      if (scores.gender_mismatch)   reasons.push("Gender mismatch");
      if (scores.color_mismatch)    reasons.push("Color mismatch");
      if (scores.scent_mismatch)    reasons.push("Scent/variant mismatch");
      if (scores.media_format_mismatch) reasons.push("Media format (DVD/Blu-ray/etc.)");
      if (scores.category_mismatch) reasons.push("Category mismatch");
      if (scores.mpn_field_conflict) reasons.push("Item ID / MPN mismatch");
      if (scores.size_conflict)     reasons.push("Amazon size conflict (title vs listing)");
      if (scores.pack_mismatch)     reasons.push("Pack mismatch");
      const reasonTip = reasons.length ? ` title="${reasons.join(", ")}"` : "";

      const queryCell = _qs.multiMode
        ? `<td style="font-family:monospace;font-size:11px;color:#6b50d4;">${escapeHtml(c._searchedFor || "—")}</td>`
        : "";

      return `<tr>
        <td>${_qsScoreBadge(c.confidence, c.verdict)}</td>
        <td><span${reasonTip}>${_qsVerdictBadge(c.verdict)}</span></td>
        <td>
          <a href="${asinUrl}" target="_blank" rel="noopener"
             style="font-family:monospace;font-size:12px;color:#4f46e5;text-decoration:none;"
             title="Open on Amazon">${c.asin}</a>
        </td>
        <td style="max-width:260px;">
          <div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:260px;"
               title="${escapeHtml(c.title)}">${escapeHtml(c.title) || "—"}</div>
          <div style="font-size:11px;color:#94a3b8;">${escapeHtml(c.brand) || ""}</div>
        </td>
        <td style="font-size:12px;">${bsr}</td>
        <td style="font-size:11px;">${category}</td>
        <td style="font-family:monospace;font-size:12px;">${escapeHtml(c.upc) || "—"}</td>
        <td style="font-family:monospace;font-size:12px;">${escapeHtml(c.mpn) || "—"}</td>
        <td>${_qsSourcesBadge(c.sources)}</td>
        ${queryCell}
      </tr>`;
    }).join("");
  }

  // ---- Wire-up ----
  $$(".qs-filter-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      _qs.filter = btn.dataset.filter || "all";
      _renderQsResults();
    });
  });

  $$(".qs-mode-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      $$(".qs-mode-btn").forEach(b => b.classList.toggle("active", b === btn));
    });
  });

  $("#qs-clear-btn")?.addEventListener("click", () => {
    // Clear tag arrays and re-render chips
    _qs.tags.upc    = [];
    _qs.tags.itemid = [];
    _qsRenderTags("upc");
    _qsRenderTags("itemid");
    ["qs-upc", "qs-itemid", "qs-title", "qs-brand", "qs-min-rank", "qs-max-rank"].forEach(id => {
      const el = $(`#${id}`); if (el) el.value = "";
    });
    $$(".qs-mode-btn").forEach(b => b.classList.toggle("active", b.dataset.mode === "cpg"));
    _qs.results = [];
    _qs.filter  = "all";
    $("#qs-results-area")?.classList.add("hidden");
    $("#qs-empty-state")?.classList.remove("hidden");
    const st = $("#qs-status-text"); if (st) st.classList.add("hidden");
  });

  $("#qs-search-btn")?.addEventListener("click", async () => {
    if (_qs.searching) return;

    // Flush any pending text in the tag inputs as a tag before searching
    ["upc", "itemid"].forEach(field => {
      const input = $(`#qs-${field}`);
      if (input?.value.trim()) {
        _qsAddTag(field, input.value);
        input.value = "";
      }
    });

    // Collect all search terms
    const upcTags    = [..._qs.tags.upc];
    const itemidTags = [..._qs.tags.itemid];
    const title      = ($("#qs-title")?.value  || "").trim();
    const brand      = ($("#qs-brand")?.value  || "").trim();
    const mode       = Array.from($$(".qs-mode-btn"))
                         .find(b => b.classList.contains("active"))?.dataset?.mode || "cpg";
    const maxRank    = parseInt($("#qs-max-rank")?.value || "0") || 0;
    const minRank    = parseInt($("#qs-min-rank")?.value || "0") || 0;

    // Build a list of individual search jobs
    // Each job: { upc?, itemid?, label } — title+brand used as context for every job
    const jobs = [];
    if (upcTags.length)    upcTags.forEach(u    => jobs.push({ upc: u,         label: u }));
    if (itemidTags.length) itemidTags.forEach(id => jobs.push({ itemid: id,    label: id }));
    if (!upcTags.length && !itemidTags.length)   jobs.push({ label: title || brand || "" });

    if (jobs.length === 0 || (!upcTags.length && !itemidTags.length && !title)) {
      showToast("Please add at least one UPC, Item ID, or Title to search.", "error");
      return;
    }

    _qs.searching  = true;
    _qs.multiMode  = jobs.length > 1;
    _qs._queryCount = jobs.length;

    const btn = $("#qs-search-btn");
    if (btn) { btn.disabled = true; btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="margin-right:6px;animation:spin 1s linear infinite;"><circle cx="12" cy="12" r="10" stroke-opacity="0.3"/><path d="M12 2a10 10 0 0 1 10 10"/></svg> Searching…`; }
    const st = $("#qs-status-text");
    if (st) { st.style.color = "#6b7480"; st.textContent = jobs.length > 1 ? `Searching ${jobs.length} items…` : "Querying Amazon…"; st.classList.remove("hidden"); }
    $("#qs-results-area")?.classList.add("hidden");
    $("#qs-empty-state")?.classList.add("hidden");

    try {
      // Run all jobs in parallel; merge results deduped by ASIN (keep highest confidence)
      const allResults = await Promise.all(jobs.map(async (job) => {
        const j = await api("/api/analytics/quick-search", {
          method: "POST",
          body: {
            upc:          job.upc    || "",
            itemid:       job.itemid || "",
            title, brand,
            vetting_mode: mode, max_rank: maxRank, min_rank: minRank,
          },
        });
        return (j.candidates || []).map(c => ({ ...c, _searchedFor: job.label }));
      }));

      // Merge: dedup by ASIN — keep highest confidence per ASIN
      const byAsin = new Map();
      allResults.flat().forEach(c => {
        const existing = byAsin.get(c.asin);
        if (!existing || c.confidence > existing.confidence) {
          byAsin.set(c.asin, c);
        }
      });
      _qs.results = [...byAsin.values()].sort((a, b) => b.confidence - a.confidence);
      _qs.filter  = "all";

      if (_qs.results.length === 0) {
        if (st) st.textContent = "No candidates found on Amazon.";
        const emptyEl = $("#qs-empty-state");
        if (emptyEl) {
          emptyEl.innerHTML = `
            <div style="width:64px;height:64px;border-radius:50%;background:var(--purple-50,#f4f0ff);display:flex;align-items:center;justify-content:center;margin:0 auto 16px;">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="var(--purple-400,#8b76e5)" stroke-width="1.8"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            </div>
            <div class="text-sm font-semibold" style="color:#475569;margin-bottom:4px;">No matches found</div>
            <div class="text-xs" style="color:#94a3b8;">Try different search terms or check your SP-API connection.</div>`;
          emptyEl.classList.remove("hidden");
        }
      } else {
        const n = _qs.results.length;
        if (st) st.textContent = jobs.length > 1
          ? `${n} candidate${n === 1 ? "" : "s"} found across ${jobs.length} searches.`
          : `${n} candidate${n === 1 ? "" : "s"} found.`;
        _renderQsResults();
        $("#qs-results-area")?.classList.remove("hidden");
      }
    } catch (e) {
      if (st) { st.textContent = "Search failed: " + (e.message || e); st.style.color = "#dc2626"; }
      $("#qs-empty-state")?.classList.remove("hidden");
    } finally {
      _qs.searching = false;
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg> Search Amazon`;
      }
    }
  });

  // Enter in title/brand fields triggers search (UPC/ItemID Enter is handled by tag logic above)
  ["qs-title", "qs-brand"].forEach(id => {
    $(`#${id}`)?.addEventListener("keydown", e => {
      if (e.key === "Enter") $("#qs-search-btn")?.click();
    });
  });

  // =========================================================================
  // TOOLS SIDEBAR PANEL
  // =========================================================================

  // ── State ─────────────────────────────────────────────────────────────────
  state.tools = {
    eligJobId:    null,
    sfJobId:      null,
    genJobId:     null,
    eligResults:  [],
    sfResults:    [],
    genResults:   [],
    eligDone:     false,
    sfDone:       false,
    genDone:      false,
    eligFails:    0,
    sfFails:      0,
    genFails:     0,
    poll:         null,
    options:      { eligibility: true, storage: true, generic: false },
  };

  // A single hiccuped poll (dropped connection, brief server hiccup) must not
  // permanently freeze a progress bar -- the background job keeps running
  // regardless of whether we're still polling it. Only give up after several
  // consecutive failures, and say so when we do.
  const _TOOLS_POLL_MAX_FAILS = 5;

  // ── Open / close ──────────────────────────────────────────────────────────
  $("#open-tools-btn")?.addEventListener("click", () => _openPanel("tools-panel"));
  $("#tools-close")?.addEventListener("click", () => _closeActivePanel());

  // Checkbox label hover highlight
  ["#tools-opt-elig-wrap","#tools-opt-sf-wrap","#tools-opt-generic-wrap"].forEach(sel => {
    const wrap = $(sel);
    if (!wrap) return;
    wrap.addEventListener("mouseenter", () => wrap.style.background = "#f8fafc");
    wrap.addEventListener("mouseleave", () => wrap.style.background = "");
  });

  // ── ASIN count hint ───────────────────────────────────────────────────────
  $("#tools-asin-input")?.addEventListener("input", function() {
    const n = _parseAsins(this.value).length;
    const el = $("#tools-asin-count");
    if (el) el.textContent = n > 0 ? `${n.toLocaleString()} ASIN${n === 1 ? "" : "s"} detected` : "";
  });
  $("#tools-asin-input")?.addEventListener("keydown", function(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") _startToolsRun();
  });

  // ── Run ───────────────────────────────────────────────────────────────────
  $("#tools-run-btn")?.addEventListener("click", _startToolsRun);

  // ── Check Brand (brand-wide gating) ─────────────────────────────────────────
  function _escHtml(v) {
    return String(v ?? "").replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function _parseBrands(raw) {
    // One brand per line; also tolerant of comma-separated CSV rows (takes just
    // the first cell of each line so an uploaded CSV file with extra columns
    // does not get swallowed in as fake brands). Deduplicated case-insensitively.
    const seen = new Set();
    const out = [];
    for (const line of String(raw || "").split(/\r?\n/)) {
      const first = line.split(",")[0].trim().replace(/^"+|"+$/g, "");
      if (!first) continue;
      const key = first.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(first);
    }
    return out;
  }

  $("#brand-check-input")?.addEventListener("input", function() {
    const n = _parseBrands(this.value).length;
    const el = $("#brand-check-count");
    if (el) el.textContent = n > 0 ? `${n} brand${n === 1 ? "" : "s"}` : "";
  });

  // Sample-size mode: fixed count vs percentage of the total Amazon ASINs
  function _syncBrandCheckMode() {
    const pct = $("#brand-check-mode-pct")?.checked;
    const cVal = $("#brand-check-count-val");
    const pVal = $("#brand-check-pct-val");
    if (cVal) { cVal.disabled = !!pct; cVal.style.background = pct ? "#f8fafc" : ""; }
    if (pVal) { pVal.disabled = !pct; pVal.style.background = pct ? "" : "#f8fafc"; }
  }
  $("#brand-check-mode-count")?.addEventListener("change", _syncBrandCheckMode);
  $("#brand-check-mode-pct")?.addEventListener("change", _syncBrandCheckMode);

  function _brandCheckParams() {
    if ($("#brand-check-mode-pct")?.checked) {
      const pct = parseFloat($("#brand-check-pct-val")?.value);
      return { sample_pct: Math.max(1, Math.min(100, isNaN(pct) ? 10 : pct)) };
    }
    const n = parseInt($("#brand-check-count-val")?.value, 10);
    return { sample_size: Math.max(1, Math.min(300, isNaN(n) ? 8 : n)) };
  }

  // Upload a .txt/.csv of brand names -> appended into the textarea
  $("#brand-check-file")?.addEventListener("change", async function() {
    const file = this.files && this.files[0];
    this.value = "";   // allow re-uploading the same filename again later
    if (!file) return;
    try {
      const text = await file.text();
      const input = $("#brand-check-input");
      if (input) {
        const existing = input.value.trim();
        input.value = existing ? existing + "\n" + text : text;
        input.dispatchEvent(new Event("input"));
      }
    } catch (e) {
      showToast("Could not read file: " + e.message, "error");
    }
  });

  const _VERDICT_STYLE = {
    CAN_SELL:       { bg: "#f0fdf4", color: "#16a34a", icon: "\u2713" },
    NEEDS_APPROVAL: { bg: "#fffbeb", color: "#d97706", icon: "\u26a0" },
    RESTRICTED:     { bg: "#fef2f2", color: "#dc2626", icon: "\u2717" },
    MIXED:          { bg: "#fff7ed", color: "#c2410c", icon: "\u26a0" },
    ALL_DOG:        { bg: "#f8fafc", color: "#64748b", icon: "?" },
    ERROR:          { bg: "#fef2f2", color: "#dc2626", icon: "!" },
  };

  const _brandCheckState = { cancel: false, results: [] };

  function _renderSingleBrandResult(r) {
    const out = $("#brand-check-result");
    if (!out) return;
    out.classList.remove("hidden");
    const style = _VERDICT_STYLE[r.verdict] || _VERDICT_STYLE.ALL_DOG;
    out.style.background = style.bg;
    out.style.color = style.color;
    const dogNote = r.dog_skipped > 0
      ? ` (${r.dog_skipped} deactivated ASIN${r.dog_skipped === 1 ? "" : "s"} skipped)` : "";
    out.innerHTML = `<div style="font-weight:700;margin-bottom:2px;">${style.icon} ${_escHtml(r.brand)}</div>` +
      `<div>${_escHtml(r.summary)}${_escHtml(dogNote)}</div>` +
      `<div style="margin-top:4px;font-size:11px;opacity:0.75;">${(r.total_asins_in_catalog || 0).toLocaleString()} ASIN${r.total_asins_in_catalog === 1 ? "" : "s"} for this brand on Amazon; ${r.checked.length} checked.</div>`;
  }

  function _appendBrandRow(r) {
    const tbody = $("#brand-check-table-body");
    if (!tbody) return;
    const style = _VERDICT_STYLE[r.verdict] || _VERDICT_STYLE.ALL_DOG;
    const dogNote = r.dog_skipped > 0 ? ` (${r.dog_skipped} DOG skipped)` : "";
    const tr = document.createElement("tr");
    tr.style.borderBottom = "1px solid #f1f5f9";
    tr.innerHTML =
      `<td style="padding:5px 6px;font-weight:600;color:#1e293b;white-space:nowrap;">${_escHtml(r.brand)}</td>` +
      `<td style="padding:5px 6px;white-space:nowrap;"><span style="background:${style.bg};color:${style.color};border-radius:99px;padding:2px 8px;font-weight:600;font-size:11px;">${style.icon} ${_escHtml(r.verdict)}</span></td>` +
      `<td style="padding:5px 6px;color:#475569;">${_escHtml(r.summary)}${_escHtml(dogNote)}</td>`;
    tbody.appendChild(tr);
  }

  async function _checkBrands() {
    const input = $("#brand-check-input");
    const btn = $("#brand-check-btn");
    const singleOut = $("#brand-check-result");
    const tableWrap = $("#brand-check-table-wrap");
    const tableBody = $("#brand-check-table-body");
    const progress = $("#brand-check-progress");
    const exportBtn = $("#brand-check-export-btn");
    if (!input || !btn) return;

    const brands = _parseBrands(input.value);
    if (!brands.length) { showToast("Enter at least one brand.", "warning"); return; }

    const params = _brandCheckParams();
    _brandCheckState.cancel = false;
    _brandCheckState.results = [];

    singleOut?.classList.add("hidden");
    tableWrap?.classList.add("hidden");
    if (tableBody) tableBody.innerHTML = "";
    exportBtn?.classList.add("hidden");
    progress?.classList.add("hidden");

    const isBulk = brands.length > 1;
    if (isBulk) {
      tableWrap?.classList.remove("hidden");
      progress?.classList.remove("hidden");
      if (progress) progress.textContent = `0 / ${brands.length} checked…`;
      btn.textContent = "Cancel";
      btn.onclick = () => { _brandCheckState.cancel = true; };
    } else {
      btn.disabled = true;
      btn.textContent = "Checking…";
    }

    let done = 0;
    let idx = 0;
    const CONCURRENCY = 2;

    async function worker() {
      while (idx < brands.length) {
        if (_brandCheckState.cancel) return;
        const brand = brands[idx++];
        let r;
        try {
          r = await api("/api/eligibility/check-brand", { method: "POST", body: { brand, ...params } });
        } catch (e) {
          r = { brand, verdict: "ERROR", summary: e.message, total_asins_in_catalog: 0, checked: [], dog_skipped: 0 };
        }
        _brandCheckState.results.push(r);
        if (isBulk) _appendBrandRow(r); else _renderSingleBrandResult(r);
        done++;
        if (progress) progress.textContent = `${done} / ${brands.length} checked…`;
      }
    }

    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, brands.length) }, worker));

    if (isBulk && progress) {
      progress.textContent = _brandCheckState.cancel
        ? `Cancelled — ${done} / ${brands.length} checked.`
        : `Done — ${done} / ${brands.length} checked.`;
    }
    if (isBulk) exportBtn?.classList.remove("hidden");
    btn.disabled = false;
    btn.textContent = "Check";
    btn.onclick = _checkBrands;
  }
  // NOTE: this button's handler is reassigned at runtime (Cancel mid-run, back to
  // Check when done) via `btn.onclick = ...`, not addEventListener -- an
  // addEventListener registered here in ADDITION to those reassignments would
  // fire alongside them on every click instead of being replaced by them. That
  // was the cause of a real bug: a second click during a bulk run (meant as
  // Cancel) also re-triggered a fresh _checkBrands() call through the old
  // addEventListener, whose newly-reset _brandCheckState.cancel got flipped
  // back to true moments later by that very same click's onclick handler --
  // so every run silently stopped after exactly CONCURRENCY (2) brands.
  const _brandCheckBtn = $("#brand-check-btn");
  if (_brandCheckBtn) _brandCheckBtn.onclick = _checkBrands;
  $("#brand-check-input")?.addEventListener("keydown", function(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") _checkBrands();
  });

  $("#brand-check-export-btn")?.addEventListener("click", function() {
    const rows = _brandCheckState.results;
    if (!rows.length) return;
    const _q = v => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const headers = ["brand", "verdict", "summary", "total_asins_on_amazon", "checked", "dog_skipped"];
    const lines = [headers.map(_q).join(",")];
    for (const r of rows) {
      lines.push([r.brand, r.verdict, r.summary, r.total_asins_in_catalog || 0,
                  r.checked.length, r.dog_skipped || 0].map(_q).join(","));
    }
    const blob = new Blob([lines.join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "brand_check.csv";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  async function _startToolsRun() {
    const input = $("#tools-asin-input");
    if (!input) return;
    const asins = _parseAsins(input.value);
    if (!asins.length) { showToast("Paste at least one ASIN.", "error"); return; }
    if (asins.length > 5000) { showToast(`Max 5,000 ASINs (you entered ${asins.length}).`, "error"); return; }

    const wantElig = $("#tools-opt-elig")?.checked ?? true;
    const wantSF   = $("#tools-opt-sf")?.checked ?? true;
    const wantGen  = $("#tools-opt-generic")?.checked ?? false;
    if (!wantElig && !wantSF && !wantGen) { showToast("Select at least one tool to run.", "warning"); return; }

    // Reset state
    const t = state.tools;
    clearTimeout(t.poll); t.poll = null;
    t.eligJobId = null; t.sfJobId = null; t.genJobId = null;
    t.eligResults = []; t.sfResults = []; t.genResults = [];
    t.eligDone = false; t.sfDone = false; t.genDone = false;
    t.eligFails = 0; t.sfFails = 0; t.genFails = 0;
    t.options = { eligibility: wantElig, storage: wantSF, generic: wantGen };

    // Reset UI
    const btn = $("#tools-run-btn");
    if (btn) { btn.disabled = true; btn.textContent = "Running…"; }
    $("#tools-export-btn")?.classList.add("hidden");

    const progSec = $("#tools-progress-section");
    if (progSec) { progSec.classList.remove("hidden"); progSec.style.display = "flex"; }

    const eligProg = $("#tools-elig-progress");
    const sfProg   = $("#tools-sf-progress");
    const genProg  = $("#tools-generic-progress");
    if (eligProg) eligProg.classList.toggle("hidden", !wantElig);
    if (sfProg)   sfProg.classList.toggle("hidden", !wantSF);
    if (genProg)  genProg.classList.toggle("hidden", !wantGen);
    _toolsResetBars();

    // Start jobs
    const starts = [];
    if (wantElig) {
      starts.push(
        api("/api/eligibility/check", { method: "POST", body: { asins } })
          .then(j => { t.eligJobId = j.job_id; })
          .catch(e => { showToast("Eligibility start failed: " + e.message, "error"); t.eligDone = true; })
      );
    } else {
      t.eligDone = true;
    }
    if (wantSF) {
      starts.push(
        api("/api/storage-fees/check", { method: "POST", body: { asins } })
          .then(j => { t.sfJobId = j.job_id; })
          .catch(e => { showToast("Storage fees start failed: " + e.message, "error"); t.sfDone = true; })
      );
    } else {
      t.sfDone = true;
    }
    if (wantGen) {
      starts.push(
        api("/api/generic-check/check", { method: "POST", body: { asins } })
          .then(j => { t.genJobId = j.job_id; })
          .catch(e => { showToast("Generic check start failed: " + e.message, "error"); t.genDone = true; })
      );
    } else {
      t.genDone = true;
    }

    await Promise.all(starts);
    t.poll = setTimeout(_toolsPoll, 800);
  }

  function _toolsResetBars() {
    ["#tools-elig-bar","#tools-sf-bar","#tools-generic-bar"].forEach(sel => {
      const el = $(sel); if (el) el.style.width = "0%";
    });
    ["#tools-elig-label","#tools-sf-label","#tools-generic-label"].forEach(sel => {
      const el = $(sel); if (el) el.textContent = "";
    });
    [["tp-elig-can","tp-elig-appr","tp-elig-rest","tp-elig-dog","tp-elig-err"],
     ["tp-sf-ok","tp-sf-nodims","tp-sf-err"],
     ["tp-gen-generic","tp-gen-not","tp-gen-dog","tp-gen-err"]].flat().forEach(id => {
      const el = document.getElementById(id); if (el) el.textContent = "0";
    });
  }

  async function _toolsPoll() {
    const t = state.tools;

    // Poll eligibility
    if (!t.eligDone && t.eligJobId) {
      try {
        const j = await api(`/api/eligibility/jobs/${t.eligJobId}`);
        t.eligFails = 0;
        const pct = Math.round((j.done / (j.total || 1)) * 100);
        const bar = $("#tools-elig-bar"); if (bar) bar.style.width = pct + "%";
        const lbl = $("#tools-elig-label");
        if (lbl) lbl.textContent = `${j.done.toLocaleString()} / ${(j.total||0).toLocaleString()}`;
        t.eligResults = j.results || [];
        _toolsUpdateEligStats(t.eligResults);
        if (j.status === "complete" || j.status === "error") {
          t.eligDone = true;
          if (bar) bar.style.width = "100%";
          if (lbl) lbl.textContent = j.status === "complete"
            ? `✓ ${(j.total||0).toLocaleString()} done`
            : `⚠ ${j.done} / ${j.total} (error)`;
        }
      } catch (e) {
        t.eligFails++;
        if (t.eligFails >= _TOOLS_POLL_MAX_FAILS) {
          t.eligDone = true;
          const lbl = $("#tools-elig-label");
          if (lbl) lbl.textContent = "⚠ lost connection (" + (e && e.message || "error") + ")";
          showToast("eligibility progress: lost connection after " + _TOOLS_POLL_MAX_FAILS + " retries -- the job may still be running server-side.", "error");
        }
      }
    }

    // Poll storage fees
    if (!t.sfDone && t.sfJobId) {
      try {
        const j = await api(`/api/storage-fees/jobs/${t.sfJobId}`);
        t.sfFails = 0;
        const pct = Math.round((j.done / (j.total || 1)) * 100);
        const bar = $("#tools-sf-bar"); if (bar) bar.style.width = pct + "%";
        const lbl = $("#tools-sf-label");
        if (lbl) lbl.textContent = `${j.done.toLocaleString()} / ${(j.total||0).toLocaleString()}`;
        t.sfResults = j.results || [];
        _toolsUpdateSFStats(t.sfResults);
        if (j.status === "complete" || j.status === "error") {
          t.sfDone = true;
          if (bar) bar.style.width = "100%";
          if (lbl) lbl.textContent = j.status === "complete"
            ? `✓ ${(j.total||0).toLocaleString()} done`
            : `⚠ ${j.done} / ${j.total} (error)`;
        }
      } catch (e) {
        t.sfFails++;
        if (t.sfFails >= _TOOLS_POLL_MAX_FAILS) {
          t.sfDone = true;
          const lbl = $("#tools-sf-label");
          if (lbl) lbl.textContent = "⚠ lost connection (" + (e && e.message || "error") + ")";
          showToast("storage-fees progress: lost connection after " + _TOOLS_POLL_MAX_FAILS + " retries -- the job may still be running server-side.", "error");
        }
      }
    }

    // Poll generic check
    if (!t.genDone && t.genJobId) {
      try {
        const j = await api(`/api/generic-check/jobs/${t.genJobId}`);
        t.genFails = 0;
        const pct = Math.round((j.done / (j.total || 1)) * 100);
        const bar = $("#tools-generic-bar"); if (bar) bar.style.width = pct + "%";
        const lbl = $("#tools-generic-label");
        if (lbl) lbl.textContent = `${j.done.toLocaleString()} / ${(j.total||0).toLocaleString()}`;
        t.genResults = j.results || [];
        _toolsUpdateGenStats(t.genResults);
        if (j.status === "complete" || j.status === "error") {
          t.genDone = true;
          if (bar) bar.style.width = "100%";
          if (lbl) lbl.textContent = j.status === "complete"
            ? `✓ ${(j.total||0).toLocaleString()} done`
            : `⚠ ${j.done} / ${j.total} (error)`;
        }
      } catch (e) {
        t.genFails++;
        if (t.genFails >= _TOOLS_POLL_MAX_FAILS) {
          t.genDone = true;
          const lbl = $("#tools-generic-label");
          if (lbl) lbl.textContent = "⚠ lost connection (" + (e && e.message || "error") + ")";
          showToast("generic-check progress: lost connection after " + _TOOLS_POLL_MAX_FAILS + " retries -- the job may still be running server-side.", "error");
        }
      }
    }

    // All finished?
    if (t.eligDone && t.sfDone && t.genDone) {
      clearTimeout(t.poll); t.poll = null;
      const btn = $("#tools-run-btn");
      if (btn) { btn.disabled = false; btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg> Run`; }
      $("#tools-export-btn")?.classList.remove("hidden");
      showToast("Done — click Download CSV to export.", "success");
    } else {
      t.poll = setTimeout(_toolsPoll, 1000);
    }
  }

  function _toolsUpdateEligStats(results) {
    let can = 0, appr = 0, rest = 0, dog = 0, err = 0;
    for (const r of results) {
      if (r.status === "CAN_SELL") can++;
      else if (r.status === "NEEDS_APPROVAL") appr++;
      else if (r.status === "RESTRICTED") { if (r.dog) dog++; else rest++; }
      else err++;
    }
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    set("tp-elig-can", can); set("tp-elig-appr", appr);
    set("tp-elig-rest", rest); set("tp-elig-dog", dog); set("tp-elig-err", err);
  }

  function _toolsUpdateSFStats(results) {
    let ok = 0, noDims = 0, err = 0;
    for (const r of results) {
      if (r.status === "ok") ok++;
      else if (r.status === "no_dimensions") noDims++;
      else err++;
    }
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    set("tp-sf-ok", ok); set("tp-sf-nodims", noDims); set("tp-sf-err", err);
  }

  function _toolsUpdateGenStats(results) {
    let gen = 0, not = 0, dog = 0, err = 0;
    for (const r of results) {
      if (r.status === "GENERIC") gen++;
      else if (r.status === "NOT_GENERIC") not++;
      else if (r.status === "DOG") dog++;
      else err++;
    }
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    set("tp-gen-generic", gen); set("tp-gen-not", not);
    set("tp-gen-dog", dog); set("tp-gen-err", err);
  }

  // ── Export (client-side CSV generation) ───────────────────────────────────
  $("#tools-export-btn")?.addEventListener("click", _toolsExport);

  function _toolsExport() {
    const t = state.tools;
    const wantElig = t.options.eligibility;
    const wantSF   = t.options.storage;
    const wantGen  = t.options.generic;

    // Build lookup maps
    const eligMap = {};
    for (const r of t.eligResults) {
      eligMap[r.asin] = (r.status === "RESTRICTED" && r.dog) ? "RESTRICTED (DOG)" : (r.status || "");
    }

    const sfMap = {};
    for (const r of t.sfResults) {
      sfMap[r.asin] = {
        offpeak: r.fee_offpeak != null ? r.fee_offpeak : "",
        peak:    r.fee_peak    != null ? r.fee_peak    : "",
      };
    }

    const GEN_LABEL = { GENERIC: "Generic", NOT_GENERIC: "Not generic", DOG: "DOG (delisted)", ERROR: "Error" };
    const genMap = {};
    for (const r of t.genResults) genMap[r.asin] = GEN_LABEL[r.status] || (r.status || "");

    // Collect all ASINs (preserve insertion order, dedup)
    const seen = new Set();
    const allAsins = [];
    for (const r of [...t.eligResults, ...t.sfResults, ...t.genResults]) {
      if (!seen.has(r.asin)) { seen.add(r.asin); allAsins.push(r.asin); }
    }

    // Header row
    const headers = ["asin"];
    if (wantElig) headers.push("eligibility_status");
    if (wantSF)   { headers.push("fee_offpeak_per_unit_mo"); headers.push("fee_peak_q4_per_unit_mo"); }
    if (wantGen)  headers.push("generic_status");

    // Data rows
    const _q = v => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const rows = allAsins.map(asin => {
      const cols = [_q(asin)];
      if (wantElig) cols.push(_q(eligMap[asin] ?? ""));
      if (wantSF) {
        const sf = sfMap[asin] || {};
        cols.push(_q(sf.offpeak ?? ""));
        cols.push(_q(sf.peak ?? ""));
      }
      if (wantGen) cols.push(_q(genMap[asin] ?? ""));
      return cols.join(",");
    });

    const csv  = [headers.map(_q).join(","), ...rows].join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    a.href     = url;
    a.download = "asin_lookup.csv";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // ── Legacy compat: _formatEta still used by old code paths ───────────────
  const STATUS_COLORS = {
    CAN_SELL:       { bg: "#f0fdf4", color: "#16a34a", label: "CAN SELL" },
    NEEDS_APPROVAL: { bg: "#fffbeb", color: "#d97706", label: "NEEDS APPROVAL" },
    RESTRICTED:     { bg: "#fef2f2", color: "#dc2626", label: "RESTRICTED" },
    ERROR:          { bg: "#f8fafc", color: "#94a3b8", label: "ERROR" },
  };

  function _eligStatusBadge(status) {
    const s = STATUS_COLORS[status] || STATUS_COLORS.ERROR;
    return `<span style="display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600;background:${s.bg};color:${s.color};">${s.label}</span>`;
  }

  function _formatEta(seconds) {
    if (seconds == null || seconds <= 0) return "";
    if (seconds < 60) return `~${seconds}s remaining`;
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `~${m}m ${s}s remaining`;
  }

  function _parseAsins(raw) {
    // Accept newline, comma, space, or semicolon delimiters
    return [...new Set(
      raw.split(/[\n,;\s]+/)
        .map(s => s.trim().toUpperCase())
        .filter(s => s.length > 0)
    )];
  }

  // _renderEligibilityRow — kept for any future in-panel preview table
  function _renderEligibilityRow(r) {
    const reasons     = r.reasons || [];
    const types       = reasons.map(x => x.type).join(", ") || "—";
    const hint        = reasons.map(x => x.hint).filter(Boolean).join(" ") || "—";
    const approvalUrl = reasons.find(x => x.approval_url)?.approval_url;
    const approvalLink = approvalUrl
      ? `<a href="${escapeHtml(approvalUrl)}" target="_blank" rel="noopener"
            style="color:#4f46e5;text-decoration:none;font-size:12px;">Request Approval ↗</a>`
      : "—";

    const rowBg = r.status === "CAN_SELL"       ? "#f8fff9" :
                  r.status === "NEEDS_APPROVAL" ? "#fffef5" :
                  r.status === "RESTRICTED"     ? "#fff8f8" : "";

    return `<tr style="border-bottom:1px solid #f1f5f9;${rowBg ? `background:${rowBg};` : ""}">
      <td style="padding:9px 14px;font-family:monospace;font-size:12px;font-weight:600;color:#1e293b;white-space:nowrap;">
        <a href="https://amazon.com/dp/${escapeHtml(r.asin)}" target="_blank" rel="noopener"
           style="color:#4f46e5;text-decoration:none;">${escapeHtml(r.asin)}</a>
      </td>
      <td style="padding:9px 14px;white-space:nowrap;">${_eligStatusBadge(r.status)}</td>
      <td style="padding:9px 14px;font-size:12px;color:#475569;">${escapeHtml(types)}</td>
      <td style="padding:9px 14px;font-size:12px;color:#475569;max-width:260px;">${escapeHtml(hint)}</td>
      <td style="padding:9px 14px;white-space:nowrap;">${approvalLink}</td>
    </tr>`;
  }

})();
