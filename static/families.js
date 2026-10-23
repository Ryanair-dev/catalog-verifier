/* ============================================================================
   Families in the Analytics run table (read only: changes no verdict).  v6

   In the normal candidates table:
     - each row: a colored stripe on the left + family tag under the row number ("F3")
     - Reason cell: relation label (exact variant / other variant / ...) + why
     - Vendor title cell: what the AI title reader found for that row, if anything
       ("AI: finish satin nickel · type privacy")
   Above the table, ONE compact bar:
     - "Products with variants": a chip per family that has several vendor rows
       (single-row products are hidden behind "+N single products")
     - click a chip = show ONLY that family (all its pairs, every verdict, on the
       All tab); click again or "show all" to get everything back
     - one short line on what the AI did (details on hover)

   Needs the 2-line change in app.js renderAnalyticsRunCandidates (paste file).
   Data: GET /api/run-families/{run_id}   (routers/run_families.py)
   index.html under app.js:  <script src="/static/families.js?v=7"></script>
   Console check:  window.__familiesPanel -> "v6 loaded"
   ========================================================================== */
(() => {
  "use strict";
  if (window.__familiesPanel) return;
  window.__familiesPanel = "v6 loaded";

  const REL_STYLE = {
    "exact variant":      { bg: "#dcfce7", fg: "#166534" },
    "other variant":      { bg: "#ffedd5", fg: "#9a3412" },
    "variant not stated": { bg: "#fef9c3", fg: "#854d0e" },
    "unclear":            { bg: "#fef9c3", fg: "#854d0e" },
    "other product":      { bg: "#f1f5f9", fg: "#475569" },
    "other brand":        { bg: "#f1f5f9", fg: "#94a3b8" },
  };
  const PALETTE = ["#2563eb", "#db2777", "#16a34a", "#ea580c", "#7c3aed", "#0891b2",
                   "#ca8a04", "#dc2626", "#4f46e5", "#059669", "#c026d3", "#65a30d"];
  const REFRESH_MS = 60000;

  const st = {
    runId: null,
    byKey: null, sharedBy: null, rowFamily: null, aiRows: {}, ai: null,
    families: [], colorOf: {},
    loadedAt: 0, loading: false, observer: null,
    filter: null,
    showSingles: false,
  };
  window.__familyFilterRows = null;            // read by app.js renderAnalyticsRunCandidates

  const esc = (s) => s == null ? "" : String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
  const escRe = (s) => String(s || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  function shortName(f) {
    // "Kwikset · Montara Single Cylinder ..." / "Kwikset® 98150-002 Avalon ..." -> without the brand
    let name = String(f.name || "");
    if (f.brand) name = name.replace(new RegExp("^\\s*" + escRe(f.brand) + "[®™]?\\s*[·\\-–:]?\\s*", "i"), "");
    return name.trim() || f.name || f.id;
  }

  // ---- which run is open --------------------------------------------------
  function noteRun(id) {
    if (!id) return;
    ensureUi();
    watchTable();
    if (id !== st.runId) {
      st.runId = id;
      st.byKey = st.sharedBy = st.rowFamily = null;
      st.families = []; st.colorOf = {}; st.aiRows = {}; st.ai = null;
      st.showSingles = false;
      setFilter(null, false);
      st.loadedAt = 0;
      renderLegend();
      load();
    } else if (Date.now() - st.loadedAt > REFRESH_MS) {
      load();
    }
  }

  window.addEventListener("analytics-run-opened", (e) => noteRun(Number(e.detail && e.detail.runId)));

  const _origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      const method = String((init && init.method) || (input && input.method) || "GET").toUpperCase();
      const m = url.match(/\/api\/analytics\/runs\/(\d+)(?:[?\/#]|$)/);
      if (m && method === "GET") noteRun(Number(m[1]));
    } catch (_) { /* never break the app's own request */ }
    return _origFetch.apply(this, arguments);
  };

  // a verdict tab (not "All") ends the family filter: those tabs load one server page at a time
  document.addEventListener("click", (e) => {
    const tab = e.target.closest && e.target.closest("#view-analytics-run .run-tab");
    if (tab && st.filter && tab.dataset.runTab !== "All") setFilter(null, false);
  }, true);

  ensureUi();
  setInterval(ensureUi, 1500);

  // ---- data ----------------------------------------------------------------
  async function load() {
    const id = st.runId;
    if (!id || st.loading) return;
    st.loading = true;
    try {
      const r = await _origFetch(`/api/run-families/${id}`);
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      const j = await r.json();
      if (st.runId !== id) return;
      const byKey = {}, sharedBy = {}, rowFamily = {}, colorOf = {}, counts = {};
      (j.pairs || []).forEach(p => {
        byKey[`${p.row}|${p.asin}`] = p;
        const c = (counts[p.family] ||= { approved: 0, review: 0, pairs: 0 });
        c.pairs += 1;
        if (p.verdict === "verified") {
          c.approved += 1;
          (sharedBy[`${p.family}|${p.asin}`] ||= []).push(p.row);
        } else if (p.verdict === "review") {
          c.review += 1;
        }
      });
      let multiIndex = 0;
      const families = (j.families || []).map(f => {
        const rows = (f.rows || []).map(r => String(r.row));
        // colors go to the families with variants first, so the few that matter get distinct colors
        const color = rows.length > 1 ? PALETTE[multiIndex++ % PALETTE.length] : "#94a3b8";
        colorOf[f.id] = color;
        rows.forEach(r => { rowFamily[r] = f.id; });
        const c = counts[f.id] || {};
        return { id: f.id, name: f.name, brand: f.brand, rows, varying: f.varying || {}, color,
                 approved: c.approved || 0, review: c.review || 0, pairs: c.pairs || 0 };
      });
      Object.assign(st, { byKey, sharedBy, rowFamily, colorOf, families, loadedAt: Date.now(),
                          ai: j.ai || null, aiRows: j.ai_rows || {} });
      if (st.filter) setFilter(st.filter, false);
      renderLegend();
      decorate(true);
    } catch (e) {
      console.warn("[families] could not load:", e);
    } finally {
      st.loading = false;
    }
  }

  // ---- show one family only -------------------------------------------------------
  function rerenderTable(goToAll) {
    const all = document.querySelector('#view-analytics-run .run-tab[data-run-tab="All"]');
    const active = document.querySelector("#view-analytics-run .run-tab.active");
    const target = goToAll ? all : active;
    if (target) target.click();                       // the app's own tab handler re-draws the table
  }

  function setFilter(fid, redraw = true) {
    const fam = st.families.find(f => f.id === fid);
    st.filter = fam ? fid : null;
    window.__familyFilterRows = fam ? new Set(fam.rows) : null;
    if (redraw) rerenderTable(Boolean(fam));
    renderLegend();
  }

  // ---- the normal table ------------------------------------------------------
  function watchTable() {
    const body = document.getElementById("analytics-run-candidates-body");
    if (!body || st.observer) return;
    st.observer = new MutationObserver(() => decorate(false));
    st.observer.observe(body, { childList: true });
  }

  function decorate(force) {
    const body = document.getElementById("analytics-run-candidates-body");
    if (!body || !st.byKey) return;
    body.querySelectorAll("tr").forEach(tr => {
      const cells = tr.children;
      if (cells.length < 4) return;
      const row = (cells[0].textContent || "").replace(/F\d+$/, "").trim();
      const m = (cells[3].textContent || "").match(/\b[A-Z0-9]{10}\b/);
      if (!row || !m) return;
      const key = `${row}|${m[0]}`;
      if (!force && tr.dataset.famKey === key) return;
      tr.dataset.famKey = key;
      tr.querySelectorAll(".fam-label, .fam-tag, .fam-ai").forEach(el => el.remove());

      const fid = st.rowFamily[row];
      const fam = st.families.find(f => f.id === fid);
      tr.dataset.fam = fid || "";
      if (fam && fam.rows.length > 1) {              // only products with variants get a stripe and a tag
        cells[0].style.boxShadow = `inset 4px 0 0 ${fam.color}`;
        cells[0].insertAdjacentHTML("beforeend",
          `<div class="fam-tag" title="${esc(fam.name)}" style="margin-top:3px;font-size:10px;font-weight:700;color:${fam.color};white-space:nowrap;">${esc(fid)}</div>`);
      } else {
        cells[0].style.boxShadow = "";
      }
      const ai = st.aiRows[row];
      if (ai && cells[2]) {
        const found = Object.entries(ai.attributes || {}).map(([b, v]) => `${b} ${v.value}`).join(" · ");
        const decoded = (ai.pairs || []).map(p => `${p.from} = ${p.to}`).join(", ");
        const tip = [found && `found: ${found}`, decoded && `decoded: ${decoded}`,
                     ai.expanded_title && `read as: ${ai.expanded_title}`,
                     (ai.why || []).length && `why the AI looked: ${ai.why.join("; ")}`].filter(Boolean).join("\n");
        if (found || decoded) {
          cells[2].insertAdjacentHTML("beforeend",
            `<div class="fam-ai" title="${esc(tip)}" style="margin-top:3px;font-size:11px;color:#7c3aed;">AI: ${esc(found || decoded)}</div>`);
        }
      }
      const p = st.byKey[key];
      if (p) {
        const cell = tr.querySelector(".analytics-reason-cell") || cells[13] || cells[cells.length - 2];
        if (cell) cell.insertAdjacentHTML("afterbegin", labelHtml(p));
      }
    });
  }

  function labelHtml(p) {
    const s = REL_STYLE[p.relation] || { bg: "#f1f5f9", fg: "#475569" };
    const others = ((st.sharedBy || {})[`${p.family}|${p.asin}`] || []).filter(r => String(r) !== String(p.row));
    const shared = p.verdict === "verified" && others.length
      ? `<div style="margin-top:2px;color:#92400e;">⚠ also approved for row ${others.map(esc).join(", ")} (same product)</div>`
      : "";
    const variant = p.listing_variant ? ` · listing: ${esc(p.listing_variant)}` : "";
    return `<div class="fam-label" style="margin-bottom:4px;font-size:11px;line-height:1.35;color:#64748b;">
      <span style="display:inline-block;padding:1px 8px;border-radius:99px;font-weight:600;white-space:nowrap;background:${s.bg};color:${s.fg};">${esc(p.relation)}</span>
      <span>${esc(p.why)}${variant}</span>${shared}
    </div>`;
  }

  function hover(fid) {
    if (st.filter) return;
    const body = document.getElementById("analytics-run-candidates-body");
    if (!body) return;
    body.querySelectorAll("tr").forEach(tr => {
      tr.style.opacity = !fid || tr.dataset.fam === fid ? "" : "0.3";
      tr.style.transition = "opacity 120ms";
    });
  }

  // ---- the bar above the table ----------------------------------------------------
  function chipHtml(f, small) {
    const on = st.filter === f.id;
    const varies = Object.keys(f.varying).map(b => b.replace("_code", "")).join(", ");
    const title = `${f.name}\nrows ${f.rows.join(", ")}${varies ? "\nvaries by " + varies : ""}` +
                  `\n${f.approved} approved · ${f.review} in review\nclick: show only this product`;
    const counts = (f.approved ? `<span style="color:#16a34a;font-weight:700;">✓${f.approved}</span>` : "")
                 + (f.review ? `<span style="color:#b45309;font-weight:700;">?${f.review}</span>` : "");
    return `<button type="button" class="fam-chip" data-fid="${esc(f.id)}" title="${esc(title)}"
        style="display:inline-flex;align-items:center;gap:5px;margin:2px 6px 2px 0;padding:${small ? "1px 8px 1px 5px" : "3px 10px 3px 6px"};
               border:1px solid ${on ? f.color : "#e2e8f0"};outline:${on ? `2px solid ${f.color}` : "none"};border-radius:99px;
               background:#fff;cursor:pointer;font-size:${small ? "10.5px" : "11.5px"};color:#334155;white-space:nowrap;max-width:260px;
               opacity:${st.filter && !on ? "0.5" : "1"};">
        <span style="width:9px;height:9px;border-radius:3px;background:${f.color};flex:0 0 auto;"></span>
        <span style="overflow:hidden;text-overflow:ellipsis;">${esc(shortName(f))}</span>
        ${f.rows.length > 1 ? `<span style="color:#94a3b8;">${f.rows.length}</span>` : ""}
        ${counts}
      </button>`;
  }

  function aiLine() {
    const a = st.ai;
    if (!a) return "";
    let text, tip;
    if (!a.switch_on) {
      text = "AI title reading: off";
      tip = "Set ANTHROPIC_API_KEY (and leave MATCH_AI_TITLE_ATTRS unset or =1) to switch it on.";
    } else if (!a.rows_checked) {
      text = "AI title reading: not run for this run yet";
      tip = `Click Re-score: it reads the titles where the rules are not enough.\n` +
            `If it still says this, check the server log for "Attribute AI router failed".`;
    } else {
      text = `AI read ${a.rows_checked} of ${a.rows_total} titles (the rest: rules were enough) · ` +
             `found something in ${a.rows_with_result}`;
      tip = `${a.attributes_found} attributes found, ${a.pairs_ai} abbreviations decoded by the AI, ` +
            `${a.pairs_learned} by learned rules.\nRows with a finding show "AI: ..." under the vendor title.`;
    }
    return `<span title="${esc(tip)}" style="font-size:11px;color:#7c3aed;cursor:help;white-space:nowrap;">✦ ${esc(text)}</span>`;
  }

  function renderLegend() {
    let box = document.getElementById("fam-legend");
    const wrap = document.getElementById("analytics-run-candidates-wrap");
    if (!box && wrap && wrap.parentNode) {
      box = document.createElement("div");
      box.id = "fam-legend";
      box.style.cssText = "padding:8px 16px;border-bottom:1px solid #edf0f5;background:#fff;";
      wrap.parentNode.insertBefore(box, wrap);
      box.addEventListener("mouseover", (e) => {
        const ch = e.target.closest(".fam-chip");
        if (ch) hover(ch.dataset.fid);
      });
      box.addEventListener("mouseout", (e) => {
        const ch = e.target.closest(".fam-chip");
        if (ch && !ch.contains(e.relatedTarget)) hover(null);
      });
      box.addEventListener("click", (e) => {
        if (e.target.closest("[data-fam-clear]")) { setFilter(null); return; }
        if (e.target.closest("[data-fam-singles]")) { st.showSingles = !st.showSingles; renderLegend(); return; }
        const ch = e.target.closest(".fam-chip");
        if (!ch) return;
        hover(null);
        setFilter(st.filter === ch.dataset.fid ? null : ch.dataset.fid);
      });
    }
    if (!box) return;
    if (!st.families.length) { box.style.display = "none"; return; }
    box.style.display = "block";

    const multi = st.families.filter(f => f.rows.length > 1);
    const singles = st.families.filter(f => f.rows.length <= 1);
    const fam = st.families.find(f => f.id === st.filter);

    const multiChips = multi.length
      ? multi.map(f => chipHtml(f, false)).join("")
      : `<span style="font-size:12px;color:#94a3b8;">no product has several rows in this file</span>`;
    const singlesToggle = singles.length
      ? `<button type="button" data-fam-singles class="row-action-btn" style="margin:2px 0;font-size:11px;">
           ${st.showSingles ? "hide" : "+"} ${singles.length} single product${singles.length === 1 ? "" : "s"}</button>`
      : "";
    const singlesRow = st.showSingles && singles.length
      ? `<div style="display:flex;flex-wrap:wrap;margin-top:4px;padding-top:4px;border-top:1px dashed #e2e8f0;">
           ${singles.map(f => chipHtml(f, true)).join("")}</div>`
      : "";
    const banner = fam
      ? `<div style="display:flex;align-items:center;gap:8px;margin-top:6px;padding:5px 10px;border-radius:8px;background:#f8fafc;border:1px solid ${fam.color};font-size:12px;color:#334155;">
           <span style="width:9px;height:9px;border-radius:3px;background:${fam.color};"></span>
           <span>Only <b>${esc(shortName(fam))}</b> · rows ${esc(fam.rows.join(", "))} · ${fam.pairs} pairs, every verdict</span>
           <button type="button" data-fam-clear class="row-action-btn" style="margin-left:auto;">show all</button>
         </div>`
      : "";

    box.innerHTML = `
      <div style="display:flex;align-items:center;flex-wrap:wrap;gap:4px 10px;">
        <span style="font-size:12px;font-weight:700;color:#334155;white-space:nowrap;">Products with variants</span>
        <div style="display:flex;flex-wrap:wrap;align-items:center;flex:1;min-width:200px;">${multiChips}${singlesToggle}</div>
        ${aiLine()}
      </div>
      ${singlesRow}
      ${banner}`;
  }

  // ---- "Export families" button next to "Export Excel" --------------------------
  function ensureUi() {
    if (document.getElementById("fam-export-btn")) return;
    const exportBtn = document.getElementById("analytics-run-export");
    if (!exportBtn) return;
    const dl = document.createElement("button");
    dl.id = "fam-export-btn";
    dl.type = "button";
    dl.className = exportBtn.className;
    dl.textContent = "Export families";
    dl.title = "Download the families workbook (Families + Pairs sheets)";
    dl.addEventListener("click", onExport);
    exportBtn.insertAdjacentElement("afterend", dl);
  }

  async function onExport() {
    const id = st.runId;
    if (!id) { alert("Open a run first (or reload the page), then click Export families again."); return; }
    const btn = document.getElementById("fam-export-btn");
    if (btn) { btn.disabled = true; btn.textContent = "Exporting…"; }
    try {
      const r = await _origFetch(`/api/run-families/${id}/export`);
      if (!r.ok) {
        let msg = `${r.status} ${r.statusText}`;
        try { const j = await r.json(); if (j.detail) msg = String(j.detail); } catch {}
        throw new Error(msg);
      }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `run_${id}_families.xlsx`;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      alert("Families export failed: " + (e.message || e));
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "Export families"; }
    }
  }
})();