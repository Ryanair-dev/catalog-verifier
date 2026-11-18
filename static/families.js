/* ============================================================================
   Families of an Analytics run, as an on-demand list (read only: changes no verdict).  v10

   A "Families" button next to "Export Excel" opens a list above the candidates table:
     - products with variants first (several vendor rows), single products behind a toggle
     - each family: name, brand, rows, what varies, approved / in-review counts
     - click a family = its vendor rows and every Amazon pair, with the relation
       (exact variant / other variant / ...) and why
     - "Show in table" = the normal table shows ONLY that family (all its pairs, every
       verdict, on the All tab); "show all" gets everything back

   Nothing is loaded until the button is clicked. The server caches the families of a run
   for 45 days (routers/run_families.py); verdicts in the list are always current.
   "Rebuild" forces a fresh build.

   Needs the 2-line change in app.js renderAnalyticsRunCandidates (window.__familyFilterRows).
   Data: GET /api/run-families/{run_id}[?refresh=1]   (routers/run_families.py)
   index.html under app.js:  <script src="/static/families.js?v=10"></script>
   Console check:  window.__familiesPanel -> "v10 loaded"
   ========================================================================== */
(() => {
  "use strict";
  if (window.__familiesPanel) return;
  window.__familiesPanel = "v10 loaded";

  const REL_STYLE = {
    "exact variant":      { bg: "#dcfce7", fg: "#166534" },
    "other variant":      { bg: "#ffedd5", fg: "#9a3412" },
    "variant not stated": { bg: "#fef9c3", fg: "#854d0e" },
    "unclear":            { bg: "#fef9c3", fg: "#854d0e" },
    "other product":      { bg: "#f1f5f9", fg: "#475569" },
    "other brand":        { bg: "#f1f5f9", fg: "#94a3b8" },
  };
  const VERDICT = {
    verified:     { label: "Approved",     fg: "#16a34a" },
    review:       { label: "Review",       fg: "#b45309" },
    not_approved: { label: "Not approved", fg: "#94a3b8" },
  };
  const PALETTE = ["#2563eb", "#db2777", "#16a34a", "#ea580c", "#7c3aed", "#0891b2",
                   "#ca8a04", "#dc2626", "#4f46e5", "#059669", "#c026d3", "#65a30d"];

  const st = {
    runId: null,
    open: false, loading: false, error: "",
    data: null,                 // the API response for st.runId, with .families decorated
    expanded: new Set(),        // family ids opened in the list
    showSingles: false,
    filter: null,               // family id shown alone in the table
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

  function age(builtAt) {
    const t = Date.parse(String(builtAt || "").replace(" ", "T") + "Z");
    if (!t) return "";
    const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
    if (mins < 2) return "just now";
    if (mins < 90) return `${mins} min ago`;
    const hours = Math.round(mins / 60);
    if (hours < 36) return `${hours} h ago`;
    return `${Math.round(hours / 24)} days ago`;
  }

  // ---- which run is open --------------------------------------------------
  window.addEventListener("analytics-run-opened", (e) => {
    const id = Number(e.detail && e.detail.runId);
    if (!id || id === st.runId) return;
    st.runId = id;
    st.data = null; st.error = ""; st.open = false;
    st.expanded = new Set(); st.showSingles = false;
    setFilter(null, false);
    render();
  });

  // ---- data ----------------------------------------------------------------
  async function load(refresh) {
    const id = st.runId;
    if (!id || st.loading) return;
    st.loading = true; st.error = "";
    render();
    try {
      const r = await fetch(`/api/run-families/${id}${refresh ? "?refresh=1" : ""}`);
      if (!r.ok) {
        let msg = `${r.status} ${r.statusText}`;
        try { const j = await r.json(); if (j.detail) msg = String(j.detail); } catch {}
        throw new Error(msg);
      }
      const j = await r.json();
      if (st.runId !== id) return;
      const pairsBy = {};
      (j.pairs || []).forEach(p => { (pairsBy[p.family] ||= []).push(p); });
      let multiIndex = 0;
      j.families = (j.families || []).map(f => {
        const rows = (f.rows || []).map(r => String(r.row));
        const pairs = pairsBy[f.id] || [];
        return {
          ...f, rowLabels: rows, pairs,
          color: rows.length > 1 ? PALETTE[multiIndex++ % PALETTE.length] : "#94a3b8",
          approved: pairs.filter(p => p.verdict === "verified").length,
          review: pairs.filter(p => p.verdict === "review").length,
        };
      });
      st.data = j;
      if (st.filter) setFilter(st.filter, false);
    } catch (e) {
      if (st.runId === id) st.error = e.message || String(e);
    } finally {
      st.loading = false;
      render();
    }
  }

  // ---- show one family only in the table --------------------------------------------
  function rerenderTable(goToAll) {
    const all = document.querySelector('#view-analytics-run .run-tab[data-run-tab="All"]');
    const active = document.querySelector("#view-analytics-run .run-tab.active");
    const target = goToAll ? all : active;
    if (target) target.click();                       // the app's own tab handler re-draws the table
  }

  function setFilter(fid, redraw = true) {
    const fam = st.data && st.data.families.find(f => f.id === fid);
    st.filter = fam ? fid : null;
    window.__familyFilterRows = fam ? new Set(fam.rowLabels) : null;
    if (redraw) rerenderTable(Boolean(fam));
  }

  // a verdict tab (not "All") ends the family filter: those tabs load one server page at a time
  document.addEventListener("click", (e) => {
    const tab = e.target.closest && e.target.closest("#view-analytics-run .run-tab");
    if (tab && st.filter && tab.dataset.runTab !== "All") { setFilter(null, false); render(); }
  }, true);

  // ---- the list ---------------------------------------------------------------
  function relChip(rel) {
    const s = REL_STYLE[rel] || { bg: "#f1f5f9", fg: "#475569" };
    return `<span style="display:inline-block;padding:1px 8px;border-radius:99px;font-weight:600;white-space:nowrap;background:${s.bg};color:${s.fg};">${esc(rel)}</span>`;
  }

  function aiText(ai) {
    if (!ai) return "";
    const found = Object.entries(ai.attributes || {}).map(([b, v]) => `${b} ${v && v.value}`).join(" · ");
    return found ? `<div style="font-size:11px;color:#7c3aed;">AI: ${esc(found)}</div>` : "";
  }

  function aiLine() {
    const a = st.data && st.data.ai;
    if (!a || !a.switch_on) return "";
    return `<span title="${esc(`${a.attributes_found || 0} attributes found by the AI title reader`)}"
      style="font-size:11px;color:#7c3aed;cursor:help;white-space:nowrap;">✦ AI found something in ${a.rows_with_result || 0} of ${a.rows_total || 0} titles</span>`;
  }

  function familyHtml(f) {
    const open = st.expanded.has(f.id);
    const on = st.filter === f.id;
    const varies = Object.keys(f.varying || {}).map(b => b.replace("_code", "")).join(", ");
    const head = `
      <div data-fam-toggle="${esc(f.id)}" style="display:flex;align-items:center;gap:10px;padding:7px 10px;cursor:pointer;">
        <span style="color:#94a3b8;width:10px;">${open ? "▾" : "▸"}</span>
        <span style="width:10px;height:10px;border-radius:3px;background:${f.color};flex:0 0 auto;"></span>
        <span style="font-weight:600;color:#334155;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(f.name)}">${esc(shortName(f))}</span>
        <span style="color:#64748b;white-space:nowrap;">${esc(f.brand || "")}</span>
        <span style="color:#94a3b8;white-space:nowrap;">${f.rowLabels.length} row${f.rowLabels.length === 1 ? "" : "s"}</span>
        ${varies ? `<span style="color:#94a3b8;white-space:nowrap;">varies by ${esc(varies)}</span>` : ""}
        <span style="white-space:nowrap;min-width:110px;text-align:right;">
          ${f.approved ? `<span style="color:#16a34a;font-weight:700;">✓${f.approved}</span> ` : ""}
          ${f.review ? `<span style="color:#b45309;font-weight:700;">?${f.review}</span> ` : ""}
          <span style="color:#94a3b8;">${f.pairs.length} pairs</span>
        </span>
        <button type="button" data-fam-filter="${esc(f.id)}" class="row-action-btn" style="font-size:11px;${on ? `outline:2px solid ${f.color};` : ""}">
          ${on ? "Showing in table" : "Show in table"}</button>
      </div>`;
    if (!open) return `<div style="border-top:1px solid #edf0f5;">${head}</div>`;

    const rows = (f.rows || []).map(r => `
      <tr><td style="padding:3px 8px;color:#64748b;">${esc(r.row)}</td>
          <td style="padding:3px 8px;">${esc(r.title)}${aiText(r.ai)}</td>
          <td style="padding:3px 8px;color:#64748b;">${esc(r.variant || "")}</td>
          <td style="padding:3px 8px;color:#94a3b8;">${esc(r.upc || "")}</td></tr>`).join("");
    const pairs = f.pairs.map(p => {
      const v = VERDICT[p.verdict] || { label: p.verdict || "", fg: "#64748b" };
      return `
      <tr><td style="padding:3px 8px;color:#64748b;">${esc(p.row)}</td>
          <td style="padding:3px 8px;white-space:nowrap;"><a href="https://www.amazon.com/dp/${esc(p.asin)}" target="_blank" rel="noopener">${esc(p.asin)}</a></td>
          <td style="padding:3px 8px;">${esc(p.amazon_title)}${p.listing_variant ? `<div style="color:#94a3b8;">listing: ${esc(p.listing_variant)}</div>` : ""}</td>
          <td style="padding:3px 8px;">${relChip(p.relation)}<div style="color:#64748b;">${esc(p.why)}</div></td>
          <td style="padding:3px 8px;white-space:nowrap;color:${v.fg};font-weight:600;">${esc(v.label)}${p.review_status ? `<div style="font-weight:400;color:#94a3b8;">${esc(p.review_status)}</div>` : ""}</td>
          <td style="padding:3px 8px;text-align:right;color:#64748b;">${p.confidence == null ? "" : Number(p.confidence).toFixed(1)}</td></tr>`;
    }).join("");
    const th = (t) => `<th style="padding:3px 8px;text-align:left;font-weight:600;color:#64748b;">${t}</th>`;
    return `<div style="border-top:1px solid #edf0f5;background:#fafbfd;">${head}
      <div style="padding:0 10px 10px 30px;font-size:11.5px;">
        <table style="width:100%;border-collapse:collapse;margin-bottom:8px;">
          <tr>${th("Row")}${th("Vendor title")}${th("Variant")}${th("UPC")}</tr>${rows}</table>
        <table style="width:100%;border-collapse:collapse;">
          <tr>${th("Row")}${th("ASIN")}${th("Amazon title")}${th("Relation")}${th("Verdict")}${th("Conf.")}</tr>
          ${pairs || `<tr><td colspan="6" style="padding:3px 8px;color:#94a3b8;">no candidates</td></tr>`}</table>
      </div></div>`;
  }

  function render() {
    ensureUi();
    let box = document.getElementById("fam-list");
    const wrap = document.getElementById("analytics-run-candidates-wrap");
    if (!box && wrap && wrap.parentNode) {
      box = document.createElement("div");
      box.id = "fam-list";
      box.style.cssText = "border-bottom:1px solid #edf0f5;background:#fff;font-size:12px;";
      wrap.parentNode.insertBefore(box, wrap);
      box.addEventListener("click", onListClick);
    }
    const btn = document.getElementById("fam-open-btn");
    if (btn) btn.textContent = st.open ? "Hide families" : "Families";
    if (!box) return;
    if (!st.open) { box.style.display = "none"; return; }
    box.style.display = "block";

    const d = st.data;
    let bar, list = "";
    if (st.loading) {
      bar = `<span style="color:#64748b;">Building families… the first time can take about a minute for a big run.</span>`;
    } else if (st.error) {
      bar = `<span style="color:#dc2626;">Families could not load: ${esc(st.error)}</span>
             <button type="button" data-fam-retry class="row-action-btn" style="font-size:11px;">Try again</button>`;
    } else if (d) {
      const multi = d.families.filter(f => f.rowLabels.length > 1);
      const singles = d.families.filter(f => f.rowLabels.length <= 1);
      const fam = d.families.find(f => f.id === st.filter);
      bar = `
        <span style="font-weight:700;color:#334155;">${multi.length} product${multi.length === 1 ? "" : "s"} with variants</span>
        <span style="color:#94a3b8;">· ${singles.length} single</span>
        ${aiLine()}
        <span style="margin-left:auto;color:#94a3b8;" title="Families are kept for ${d.cache_days} days and rebuilt when the run changes. Verdicts are always current.">built ${esc(age(d.built_at))}</span>
        <button type="button" data-fam-rebuild class="row-action-btn" style="font-size:11px;">Rebuild</button>
        ${fam ? `<button type="button" data-fam-clear class="row-action-btn" style="font-size:11px;">Show all in table</button>` : ""}`;
      list = (multi.length ? multi.map(familyHtml).join("")
                           : `<div style="padding:7px 10px;color:#94a3b8;border-top:1px solid #edf0f5;">No product has several rows in this file.</div>`)
        + (singles.length ? `<div style="border-top:1px solid #edf0f5;padding:6px 10px;">
             <button type="button" data-fam-singles class="row-action-btn" style="font-size:11px;">
               ${st.showSingles ? "Hide" : "Show"} ${singles.length} single product${singles.length === 1 ? "" : "s"}</button></div>` : "")
        + (st.showSingles ? singles.map(familyHtml).join("") : "");
    } else {
      bar = "";
    }
    box.innerHTML = `
      <div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px 10px;padding:8px 16px;">${bar}</div>
      <div style="max-height:420px;overflow:auto;padding:0 6px;">${list}</div>`;
  }

  function onListClick(e) {
    const t = e.target;
    const filt = t.closest("[data-fam-filter]");
    if (filt) {
      e.stopPropagation();
      setFilter(st.filter === filt.dataset.famFilter ? null : filt.dataset.famFilter);
      render(); return;
    }
    if (t.closest("[data-fam-clear]")) { setFilter(null); render(); return; }
    if (t.closest("[data-fam-singles]")) { st.showSingles = !st.showSingles; render(); return; }
    if (t.closest("[data-fam-rebuild]")) { load(true); return; }
    if (t.closest("[data-fam-retry]")) { load(false); return; }
    if (t.closest("a")) return;
    const tog = t.closest("[data-fam-toggle]");
    if (tog) {
      const id = tog.dataset.famToggle;
      st.expanded.has(id) ? st.expanded.delete(id) : st.expanded.add(id);
      render();
    }
  }

  function onOpen() {
    if (!st.runId) { alert("Open a run first (or reload the page), then click Families again."); return; }
    st.open = !st.open;
    if (st.open && !st.data && !st.loading) load(false);
    else render();
  }

  // ---- "Families" + "Export families" buttons next to "Export Excel" --------------
  function ensureUi() {
    const exportBtn = document.getElementById("analytics-run-export");
    if (!exportBtn || document.getElementById("fam-open-btn")) return;
    const open = document.createElement("button");
    open.id = "fam-open-btn";
    open.type = "button";
    open.className = exportBtn.className;
    open.textContent = "Families";
    open.title = "List this run's products and their variants (built on demand, kept 45 days)";
    open.addEventListener("click", onOpen);
    const dl = document.createElement("button");
    dl.id = "fam-export-btn";
    dl.type = "button";
    dl.className = exportBtn.className;
    dl.textContent = "Export families";
    dl.title = "Download the families workbook (Families + Pairs sheets)";
    dl.addEventListener("click", onExport);
    exportBtn.insertAdjacentElement("afterend", dl);
    exportBtn.insertAdjacentElement("afterend", open);
  }
  ensureUi();
  setInterval(ensureUi, 1500);

  async function onExport() {
    const id = st.runId;
    if (!id) { alert("Open a run first (or reload the page), then click Export families again."); return; }
    const btn = document.getElementById("fam-export-btn");
    if (btn) { btn.disabled = true; btn.textContent = "Exporting…"; }
    try {
      const r = await fetch(`/api/run-families/${id}/export`);
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
