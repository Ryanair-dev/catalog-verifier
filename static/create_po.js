/* =========================================================================
   Create PO — 4-step wizard (Upload → Configure & Map → Generate → Review &
   Export). Ported from the Claude Design mockup, wired to /api/create-po/*.
   Self-contained: exposes window.CreatePO.mount(rootEl).
   ========================================================================= */
(function () {
  "use strict";

  const FIELD_OPTS = [
    ["ignore", "Ignore"], ["asin", "ASIN"], ["upc", "UPC"], ["mpn", "MPN / Item ID"],
    ["name", "Product Name"], ["amzpack", "amz pack"], ["brand", "Brand"],
    ["purchaser", "Purchaser"], ["sourcer", "Sourcer"],
  ];
  const MONO = { ASIN: 1, UPC: 1, MPN: 1, Cost: 1 };
  const MONO_FIELD = { upc: 1, mpn: 1, asin: 1 };

  const S = {
    root: null, built: false,
    token: null, filename: "", headers: [], previewRows: [], rowCount: 0, colCount: 0,
    sheets: [], sheet: "", headerRow: 1, history: [],
    stage: 1,
    batchType: "CPG", createBy: "UPC", company: "Ford Medical", forAmazon: true,
    create: { main: true, shadow: true, kit: true },
    colMap: {},
    brandMode: "single", brandCol: "",
    singleBrand: "", singleMfr: "", singlePrefix: "", singlePurchaser: "", singleSourcer: "",
    purchaser: { mode: "value", col: "", value: "" },
    sourcer: { mode: "value", col: "", value: "" },
    brandTable: [], brandOverride: {}, brandConfirmed: {}, brandLoading: false,
    generating: false, progress: 0, review: null,
    filter: "all", query: "", editCell: null, cellOverride: {}, skuChecking: {}, openDD: null, ddReg: {}, _first: true, _lastKey: null,
    exported: false, exporting: false, aiTitles: true, exportId: "", error: "",
    theme: "light", bright: "airy",
    _focus: null,
  };

  const esc = (s) => s == null ? "" : String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");

  // ── API ────────────────────────────────────────────────────────────────
  async function jpost(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
    return r.json();
  }

  // ── column-map auto guess ────────────────────────────────────────────────
  function guessField(header) {
    const k = String(header || "").toLowerCase().replace(/[^a-z0-9]/g, "");
    if (k === "asin") return "asin";
    if (k.includes("upc") || k === "ean" || k === "barcode") return "upc";
    if (k === "mpn" || k.includes("itemid") || k.includes("partnumber") || k.includes("modelnumber") || k === "model") return "mpn";
    if (k === "brand" || k === "brandname") return "brand";
    if (k.includes("productname") || k.includes("title") || k === "itemname" || k === "name" || k === "description" || k === "vendortitle") return "name";
    if (k.includes("amzpack") || k === "pack" || k === "packqty" || k.includes("unitsperpack")) return "amzpack";
    if (k === "purchaser" || k === "buyer") return "purchaser";
    if (k === "supplier" || k === "sourcer" || k === "vendor") return "sourcer";
    if (k.includes("buybox") || k.includes("amazonprice") || k === "price" || k === "listprice" || k === "msrp" || (k.includes("price") && !k.includes("case") && !k.includes("cost"))) return "price";
    if (k.includes("qtypercase") || k.includes("qtycase") || k === "unitspercase" || k.includes("casepack") || k.includes("unitscase")) return "qtycase";
    if (k.includes("costpercase") || k.includes("costcase") || k === "casecost" || k.includes("caseprice")) return "costcase";
    if (k === "cost" || k === "unitcost" || k === "unitprice" || k === "ourcost" || (k.includes("unit") && k.includes("cost"))) return "cost";
    return "ignore";
  }

  // ── helpers for styling parity with the mockup ───────────────────────────
  const seg = (on) => on
    ? "background:var(--panel);color:var(--primary-ink);box-shadow:var(--shadow)"
    : "background:transparent;color:var(--ink-soft)";
  const cellVal = (id, field, def) => { const o = S.cellOverride[id + "." + field]; return o != null ? o : def; };

  // Custom dropdown (styled list) — opts = [[value,label], ...]. `kind`+`arg`
  // identify what changes on pick (see ddPick). Renders ONLY the trigger; the menu
  // is built lazily in JS and portaled to <body> on open (see showMenu) so opening
  // a dropdown never triggers a full re-render (keeps interactions smooth).
  function dd(kind, arg, value, opts) {
    const key = kind + "::" + (arg == null ? "" : arg);
    S.ddReg[key] = { opts, value };
    const open = S.openDD === key;
    const cur = opts.find(o => String(o[0]) === String(value));
    const label = cur ? cur[1] : (value || "—");
    return `<div class="cpo-dd${open ? " open" : ""}"><button type="button" class="cpo-trigger" data-act="dd-toggle" data-arg="${esc(key)}"><span class="cpo-ddlabel">${esc(label)}</span><svg class="cpo-caret" width="11" height="11" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2.4"/></svg></button></div>`;
  }

  // Build + show the portaled menu for a dropdown key without re-rendering.
  function menuHTML(key) {
    const reg = S.ddReg[key]; if (!reg) return "";
    return reg.opts.map(o => {
      const on = String(o[0]) === String(reg.value);
      return `<div class="cpo-opt${on ? " cpo-opt-sel" : ""}" data-act="dd-pick" data-arg="${esc(key)}|${esc(encodeURIComponent(String(o[0])))}">${esc(o[1])}${on ? '<svg class="cpo-check" width="13" height="13" viewBox="0 0 24 24" fill="none"><path d="M20 6L9 17l-5-5" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>' : ""}</div>`;
    }).join("");
  }
  function positionMenu(menu, trig) {
    const r = trig.getBoundingClientRect();
    menu.style.minWidth = r.width + "px";
    menu.style.left = Math.max(8, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8)) + "px";
    const below = window.innerHeight - r.bottom;
    menu.style.top = (below < 280 && r.top > 280 ? Math.max(8, r.top - menu.offsetHeight - 5) : r.bottom + 5) + "px";
  }
  function showMenu(key) {
    removePortal();
    S.root.querySelectorAll(".cpo-dd.open").forEach(d => d.classList.remove("open"));
    const trig = Array.from(S.root.querySelectorAll('[data-act="dd-toggle"]')).find(b => b.getAttribute("data-arg") === key);
    if (!trig) { S.openDD = null; return; }
    const menu = document.createElement("div");
    menu.className = "cpo-menu"; menu.id = "cpo-portal";
    menu.innerHTML = menuHTML(key);
    document.body.appendChild(menu);
    positionMenu(menu, trig);
    trig.closest(".cpo-dd")?.classList.add("open");
  }
  function closeMenu() {
    S.openDD = null; removePortal();
    if (S.root) S.root.querySelectorAll(".cpo-dd.open").forEach(d => d.classList.remove("open"));
  }

  // ═══════════════════════════════ RENDER ═══════════════════════════════
  function render() {
    if (!S.root) return;
    removePortal();
    S.ddReg = {};
    const effStage = S.generating ? 4 : S.stage;
    const _scroll = S.root.scrollTop;
    // Animate (fade-up) only when the step changes — smooth navigation without
    // flashing on every in-step toggle.
    const _key = S.generating ? "gen" : ("s" + S.stage);
    const _anim = (S._first || S._lastKey !== _key) ? " cpo-anim" : "";
    S._first = false; S._lastKey = _key;
    S.root.innerHTML =
      `<div class="cpo${_anim}">
        <div class="cpo-main">
          ${S.error ? `<div style="margin-bottom:14px;padding:12px 15px;border-radius:12px;background:var(--accent-soft);border:1px solid var(--accent);color:var(--ink);font-size:13px">⚠ ${esc(S.error)}</div>` : ""}
          ${header()}
          ${stepper(effStage)}
          ${S.stage === 1 && !S.generating ? stepUpload() : ""}
          ${S.stage === 2 && !S.generating ? stepMap() : ""}
          ${S.stage === 3 && !S.generating ? stepConfigure() : ""}
          ${S.generating ? stepGenerating() : ""}
          ${S.stage === 4 && !S.generating ? stepReview() : ""}
        </div>
      </div>`;
    S.root.scrollTop = _scroll;
    afterRender();
  }

  function header() {
    return `<div class="cpo-fadeup" style="display:flex;align-items:flex-start;justify-content:space-between;gap:20px">
      <div>
        <h1 class="cpo-hf" style="font-weight:800;font-size:27px;letter-spacing:-.03em;margin:0 0 6px;line-height:1.05">Create SKUs</h1>
        <p style="margin:0;color:var(--ink-soft);font-size:14px;max-width:620px">Upload the products you want to list, map the columns, and we generate the SellerCloud SKU families — checking the live catalog so nothing is duplicated.</p>
      </div>
    </div>`;
  }

  function stepper(eff) {
    const defs = [[1, "Upload", "Product file"], [2, "Map columns", "Fields → columns"], [3, "Configure", "Settings + brands"], [4, "Review & Export", "Edit + export"]];
    const items = defs.map(([n, label, sub], i) => {
      const st = n < eff ? "done" : (n === eff ? "active" : "up");
      const badgeBg = st === "done" ? "var(--mint)" : (st === "active" ? "linear-gradient(135deg,var(--primary),#8a5cff)" : "var(--panel-inset)");
      const badgeInk = st === "up" ? "var(--ink-faint)" : "#fff";
      const clickable = false;  // steps are display-only — use the Back button to go back
      const conn = i < defs.length - 1
        ? `<div style="width:26px;align-self:center;height:2px;border-radius:2px;background:${n < eff ? "var(--mint)" : "var(--border-strong)"};margin:0 2px"></div>` : "";
      return `<div ${clickable ? `data-act="goto" data-arg="${n}"` : ""} style="flex:1;display:flex;align-items:center;gap:10px;padding:6px 8px;cursor:${clickable ? "pointer" : "default"}">
          <div class="cpo-hf" style="width:36px;height:36px;flex-shrink:0;border-radius:11px;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:14px;background:${badgeBg};color:${badgeInk};box-shadow:${st === "active" ? "var(--shadow-lg)" : "none"}">${st === "done" ? "✓" : n}</div>
          <div style="min-width:0">
            <div style="font-size:9.5px;font-weight:800;letter-spacing:.12em;color:${st === "up" ? "var(--ink-faint)" : (st === "done" ? "var(--mint)" : "var(--primary-ink)")}">STEP ${n}</div>
            <div class="cpo-hf" style="font-weight:700;font-size:14px;letter-spacing:-.01em;color:${st === "up" ? "var(--ink-faint)" : "var(--ink)"};line-height:1.15">${label}</div>
            <div style="font-size:10.5px;color:var(--ink-faint)">${sub}</div>
          </div>
        </div>${conn}`;
    }).join("");
    return `<div class="cpo-fadeup" style="display:flex;align-items:stretch;margin-top:20px;padding:12px 14px;background:var(--panel);border:1px solid var(--border);border-radius:18px;box-shadow:var(--shadow)">${items}</div>`;
  }

  // ── STEP 1 · Upload (+ sheet/header picker + export history) ───────────────
  function stepUpload() {
    const has = !!S.token;
    return `<div class="cpo-fadeup" style="margin-top:20px;display:flex;flex-direction:column;gap:16px">
      ${has ? uploadedCard() : uploadCard()}
      ${has ? `<div style="display:flex;justify-content:flex-end;padding:15px 20px;background:var(--panel);border:1px solid var(--border);border-radius:20px;box-shadow:var(--shadow)">
        <button data-act="go2" class="cpo-lift" style="display:flex;align-items:center;gap:9px;padding:12px 24px;border-radius:14px;border:none;background:linear-gradient(135deg,var(--primary),#8a5cff);color:#fff;font-weight:800;font-size:14px;cursor:pointer;box-shadow:var(--shadow-lg)">Map columns
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M5 12h14m0 0l-6-6m6 6l-6 6" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
      </div>` : ""}
      ${historyCard()}
    </div>`;
  }

  // preview grid (first 3 rows) shared by the upload card + the map step
  function previewGrid() {
    const cols = S.headers, W = 132;
    const grid = `repeat(${cols.length}, ${W}px)`;
    const head = cols.map(c => `<div style="padding:7px 10px;border-right:1px solid var(--border);font-size:10.5px;font-weight:800;color:var(--ink-soft);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(c)}</div>`).join("");
    const rows = S.previewRows.slice(0, 3).map(r => `<div style="display:grid;grid-template-columns:${grid};border-top:1px solid var(--border)">${cols.map(c => `<div style="padding:6px 10px;border-right:1px solid var(--border);font-size:11px;font-family:${MONO[c] ? "ui-monospace,monospace" : "inherit"};color:var(--ink-soft);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(r[c])}</div>`).join("")}</div>`).join("");
    return `<div style="margin-top:7px;border:1px solid var(--border);border-radius:12px;overflow:hidden"><div style="overflow-x:auto"><div style="min-width:${cols.length * W}px"><div style="display:grid;grid-template-columns:${grid};background:var(--panel-inset)">${head}</div>${rows}</div></div></div>`;
  }

  // Uploaded-file card with the sheet + header-row pickers.
  function uploadedCard() {
    const multi = (S.sheets || []).length > 1;
    return `<div style="background:var(--panel);border:1px solid var(--border);border-radius:22px;padding:18px 22px;box-shadow:var(--shadow)">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
        <div style="display:flex;align-items:center;gap:12px;min-width:0">
          <div style="width:38px;height:38px;border-radius:11px;background:var(--mint-soft);display:flex;align-items:center;justify-content:center;flex-shrink:0">📄</div>
          <div style="min-width:0"><div style="font-weight:800;font-size:14px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(S.filename)}</div><div style="font-size:11.5px;color:var(--mint);font-weight:700">${S.rowCount} rows · ${S.colCount} columns</div></div>
        </div>
        <button data-act="pick" class="cpo-lift" style="padding:8px 14px;border-radius:10px;border:1px solid var(--border);background:var(--panel);color:var(--ink-soft);font-weight:800;font-size:12px;cursor:pointer">Replace</button>
      </div>
      <input type="file" id="cpo-file" accept=".xlsx,.xls,.csv,.tsv" style="display:none">
      <div style="display:flex;gap:16px;align-items:flex-end;flex-wrap:wrap;margin-top:14px">
        ${multi ? `<div style="min-width:180px"><div style="font-size:10.5px;font-weight:800;color:var(--ink-faint);letter-spacing:.06em;margin-bottom:5px">SHEET</div>${dd("sheet", "", S.sheet, S.sheets.map(s => [s, s]))}</div>` : ""}
        <div><div style="font-size:10.5px;font-weight:800;color:var(--ink-faint);letter-spacing:.06em;margin-bottom:5px">HEADER ROW</div>
          <input data-ch="headerrow" type="number" min="1" value="${S.headerRow}" style="width:80px;padding:9px 11px;border-radius:10px;border:1px solid var(--border);background:var(--panel);color:var(--ink);font-weight:700;font-size:13px;outline:none"></div>
        <div style="font-size:11.5px;color:var(--ink-faint);padding-bottom:9px">Pick which sheet + which row holds the column titles.</div>
      </div>
      <div style="margin-top:14px;font-size:10.5px;font-weight:800;letter-spacing:.1em;color:var(--ink-faint)">FILE PREVIEW · first 3 rows</div>
      ${previewGrid()}
    </div>`;
  }

  // Export history (shown on the upload screen).
  function historyCard() {
    const h = S.history || [];
    const head = `<div style="display:grid;grid-template-columns:104px 84px 1.4fr 1.2fr 62px;gap:10px;padding:9px 16px;background:var(--panel-inset);font-size:10px;font-weight:800;letter-spacing:.07em;color:var(--ink-soft)"><div>DATE</div><div>ID</div><div>BRAND · TYPE</div><div>SKUS</div><div style="text-align:right">FILES</div></div>`;
    const body = h.length ? h.map(e => `<div style="display:grid;grid-template-columns:104px 84px 1.4fr 1.2fr 62px;gap:10px;align-items:center;padding:10px 16px;border-top:1px solid var(--border);font-size:12px">
        <div style="color:var(--ink-soft)">${esc(e.export_date || "")}</div>
        <div><span style="font-family:ui-monospace,monospace;font-weight:800;font-size:11px;color:var(--primary-ink);background:var(--primary-soft);padding:2px 7px;border-radius:6px">${esc(e.export_id || "")}</span></div>
        <div style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><b style="color:var(--ink)">${esc(e.company || "—")}</b> <span style="color:var(--ink-faint)">· ${esc(e.batch_type || "")}</span></div>
        <div style="color:var(--ink-soft)">${e.total_mains || 0} main · ${(e.total_fba || 0) + (e.total_fbm || 0)} shadow${e.total_kits ? " · " + e.total_kits + " kit" : ""}</div>
        <div style="color:var(--ink-faint);text-align:right">${(e.files || []).length}</div>
      </div>`).join("") : `<div style="padding:24px;text-align:center;color:var(--ink-faint);font-size:12.5px">No exports yet — your generated batches will show up here.</div>`;
    return `<div style="background:var(--panel);border:1px solid var(--border);border-radius:22px;padding:18px 22px;box-shadow:var(--shadow)">
      <div style="font-size:11px;font-weight:800;letter-spacing:.12em;color:var(--primary-ink)">EXPORT HISTORY</div>
      <div class="cpo-hf" style="font-weight:700;font-size:16px;letter-spacing:-.02em;margin:2px 0 12px">Recent exports</div>
      <div style="border:1px solid var(--border);border-radius:14px;overflow:hidden">${head}${body}</div>
    </div>`;
  }

  // ── STEP 2 · Map columns ──────────────────────────────────────────────────
  function stepMap() {
    const missing = requiredMissing();
    const canNext = missing.length === 0;
    return `<div class="cpo-fadeup" style="margin-top:20px;display:flex;flex-direction:column;gap:16px">
      ${smallFilePreview()}
      ${previewMap()}
      <div style="display:flex;align-items:center;justify-content:space-between;gap:14px;padding:15px 20px;background:var(--panel);border:1px solid var(--border);border-radius:20px;box-shadow:var(--shadow);flex-wrap:wrap">
        <button data-act="back" class="cpo-navbtn" style="display:flex;align-items:center;gap:8px;padding:11px 18px;border-radius:13px;border:1px solid var(--border);background:transparent;color:var(--ink-soft);font-weight:700;font-size:13.5px;cursor:pointer"><svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M19 12H5m0 0l6-6m-6 6l6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>Back</button>
        <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;justify-content:flex-end">
          ${missing.length ? `<span style="font-size:12.5px;color:var(--amber);font-weight:700">⚠ Map ${missing.map(m => esc(m[1])).join(" + ")} to continue</span>` : ""}
          <button data-act="go3" class="cpo-lift" style="display:flex;align-items:center;gap:9px;padding:12px 24px;border-radius:14px;border:none;background:linear-gradient(135deg,var(--primary),#8a5cff);color:#fff;font-weight:800;font-size:14px;cursor:${canNext ? "pointer" : "not-allowed"};opacity:${canNext ? "1" : ".5"};box-shadow:var(--shadow-lg)">Configure &amp; brands
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M5 12h14m0 0l-6-6m6 6l-6 6" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
        </div>
      </div>
    </div>`;
  }

  function uploadCard() {
    return `<div style="background:var(--panel);border:1px solid var(--border);border-radius:22px;padding:22px;box-shadow:var(--shadow)">
      <div style="font-size:11px;font-weight:800;letter-spacing:.12em;color:var(--sky)">PRODUCT FILE</div>
      <div class="cpo-hf" style="font-weight:700;font-size:18px;letter-spacing:-.02em;margin:3px 0">Upload your spreadsheet</div>
      <div style="font-size:12.5px;color:var(--ink-soft);margin-bottom:14px">Drop an .xlsx or .csv — one row per product you want to list.</div>
      <input type="file" id="cpo-file" accept=".xlsx,.xls,.csv,.tsv" style="display:none">
      <div id="cpo-drop" data-act="pick" class="cpo-drop" style="border:2px dashed var(--border-strong);border-radius:18px;background:var(--panel-inset);padding:30px 20px;text-align:center;cursor:pointer">
        <div style="width:52px;height:52px;margin:0 auto 10px;border-radius:16px;background:var(--panel);display:flex;align-items:center;justify-content:center;box-shadow:var(--shadow)">
          <svg width="23" height="23" viewBox="0 0 24 24" fill="none"><path d="M12 16V4m0 0l-4 4m4-4l4 4M5 20h14" stroke="var(--sky)" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </div>
        <div style="font-weight:700;font-size:14.5px"><span style="color:var(--sky)">Click to upload</span> or drag &amp; drop</div>
        <div style="font-size:12px;color:var(--ink-faint);margin-top:4px">.xlsx or .csv — one row per product</div>
      </div>
    </div>`;
  }

  // Block 1: a small, read-only glance at the raw uploaded sheet.
  function smallFilePreview() {
    const cols = S.headers, W = 132;
    const grid = `repeat(${cols.length}, ${W}px)`;
    const head = cols.map(c => `<div style="padding:7px 10px;border-right:1px solid var(--border);font-size:10.5px;font-weight:800;color:var(--ink-soft);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(c)}</div>`).join("");
    const rows = S.previewRows.slice(0, 3).map(r => `<div style="display:grid;grid-template-columns:${grid};border-top:1px solid var(--border)">${cols.map(c => `<div style="padding:6px 10px;border-right:1px solid var(--border);font-size:11px;font-family:${MONO[c] ? "ui-monospace,monospace" : "inherit"};color:var(--ink-soft);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(r[c])}</div>`).join("")}</div>`).join("");
    return `<div style="background:var(--panel);border:1px solid var(--border);border-radius:22px;padding:18px 22px;box-shadow:var(--shadow)">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
        <div style="display:flex;align-items:center;gap:12px;min-width:0">
          <div style="width:38px;height:38px;border-radius:11px;background:var(--mint-soft);display:flex;align-items:center;justify-content:center;flex-shrink:0">📄</div>
          <div style="min-width:0"><div style="font-weight:800;font-size:14px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(S.filename)}</div><div style="font-size:11.5px;color:var(--mint);font-weight:700">${S.rowCount} rows · ${S.colCount} columns</div></div>
        </div>
        <button data-act="pick" class="cpo-lift" style="padding:8px 14px;border-radius:10px;border:1px solid var(--border);background:var(--panel);color:var(--ink-soft);font-weight:800;font-size:12px;cursor:pointer">Replace</button>
      </div>
      <input type="file" id="cpo-file" accept=".xlsx,.xls,.csv,.tsv" style="display:none">
      <div style="margin-top:14px;font-size:10.5px;font-weight:800;letter-spacing:.1em;color:var(--ink-faint)">FILE PREVIEW · first 3 rows</div>
      <div style="margin-top:7px;border:1px solid var(--border);border-radius:12px;overflow:hidden"><div style="overflow-x:auto"><div style="min-width:${cols.length * W}px">
        <div style="display:grid;grid-template-columns:${grid};background:var(--panel-inset)">${head}</div>${rows}
      </div></div></div>
    </div>`;
  }

  // Block 2: one card per SKU FIELD we need to build. Each card = field label +
  // a dropdown that picks which uploaded column feeds it + one sample value. Cards
  // WRAP to fit the width (no horizontal scroll → nothing to reset when you pick).
  function previewMap() {
    const idField = S.createBy === "UPC" ? "upc" : "mpn";
    const otherId = idField === "upc" ? "mpn" : "upc";
    const label = (f) => (f === "upc" ? "UPC" : "Item ID (MPN)");
    const fields = [
      // The ★ Main identifier (chosen by "Create by") builds the SKU and is required.
      { f: idField, label: label(idField), star: true, req: true },
      // The OTHER identifier is optional — "Create by" only decides HOW the SKU is
      // built; if the file also has this column, map it so it still populates.
      { f: otherId, label: label(otherId) },
      ...(S.forAmazon ? [{ f: "asin", label: "ASIN", req: true }] : []),
      { f: "name", label: "Product Name" },
      { f: "brand", label: "Brand" },
      { f: "price", label: "Amazon Price" },
      { f: "qtycase", label: "Qty / Case" },
      { f: "costcase", label: "Cost / Case" },
      { f: "cost", label: "Unit Cost" },
      { f: "amzpack", label: "Pack qty" },
    ];
    const opts = [["", "— Not mapped —"], ...S.headers.map(h => [h, h])];
    const sample = (col) => {
      if (!col) return "";
      const v = S.previewRows.map(r => r[col]).find(x => x != null && String(x).trim() !== "");
      return v == null ? "" : String(v);
    };
    const card = (fd) => {
      const col = columnForField(fd.f);
      const need = fd.req && !col;
      const border = fd.star ? "var(--primary)" : (need ? "var(--accent)" : "var(--border)");
      const bg = fd.star ? "var(--primary-soft)" : "var(--panel-inset)";
      const badge = fd.star
        ? `<span style="font-size:8px;font-weight:900;color:#fff;background:var(--primary);padding:2px 6px;border-radius:5px">★ MAIN</span>`
        : (fd.req ? `<span style="font-size:8.5px;font-weight:900;color:${col ? "var(--mint)" : "var(--accent)"};background:${col ? "var(--mint-soft)" : "var(--accent-soft)"};padding:2px 7px;border-radius:999px">${col ? "✓" : "REQUIRED"}</span>` : "");
      const ex = sample(col);
      return `<div style="border:1px solid ${border};border-radius:14px;padding:12px 13px;background:${bg};min-width:0">
        <div style="display:flex;align-items:center;gap:6px;font-size:12.5px;font-weight:800;color:${fd.star ? "var(--primary-ink)" : "var(--ink)"};white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(fd.label)}${badge}</div>
        <div style="margin-top:8px">${dd("fieldmap", fd.f, col, opts)}</div>
        <div style="margin-top:7px;font-size:11px;font-family:${MONO_FIELD[fd.f] ? "ui-monospace,monospace" : "inherit"};color:${col ? "var(--ink-soft)" : "var(--ink-faint)"};white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${col ? esc(ex) : ""}">${col ? (esc(ex) || "(blank)") : "— not mapped —"}</div>
      </div>`;
    };
    const toggle = (label, act, choices) => `<div style="display:flex;flex-direction:column;gap:6px">
      <span style="font-size:10.5px;font-weight:800;color:var(--ink-faint)">${label}</span>
      <div style="display:flex;padding:4px;border-radius:13px;background:var(--panel-inset);border:1px solid var(--border)">${choices.map(([val, txt, on]) => `<button data-act="${act}" data-arg="${val}" style="padding:9px 15px;border-radius:9px;border:none;cursor:pointer;font-weight:800;font-size:12.5px;${seg(on)}">${txt}</button>`).join("")}</div>
    </div>`;
    return `<div class="cpo-fadeup" style="background:var(--panel);border:1px solid var(--border);border-radius:22px;padding:20px 22px;box-shadow:var(--shadow)">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:18px;flex-wrap:wrap">
        <div style="min-width:280px">
          <div style="font-size:11px;font-weight:800;letter-spacing:.12em;color:var(--primary-ink)">MAP TO SKU FIELDS</div>
          <div class="cpo-hf" style="font-weight:700;font-size:18px;letter-spacing:-.02em;margin:3px 0">Pick the column that feeds each field</div>
          <div style="font-size:12.5px;color:var(--ink-soft);max-width:560px;line-height:1.45">Each card is a field we need. Choose which column from your file supplies it — the <b style="color:var(--primary-ink)">★ Main</b> identifier (<b style="color:var(--ink)">CPG → UPC</b>, <b style="color:var(--ink)">Medical → Item ID</b>) is required.</div>
        </div>
        <div style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
          ${toggle("BATCH TYPE", "batch", [["CPG", "CPG", S.batchType === "CPG"], ["Medical", "Medical", S.batchType === "Medical"]])}
          ${toggle("CREATE BY", "by", [["UPC", "UPC", S.createBy === "UPC"], ["MPN", "Part No.", S.createBy === "MPN"]])}
          ${toggle("FOR AMAZON", "foramz", [["yes", "Yes", S.forAmazon], ["no", "No", !S.forAmazon]])}
        </div>
      </div>
      <div style="margin-top:14px;display:grid;grid-template-columns:repeat(auto-fill,minmax(205px,1fr));gap:12px">${fields.map(card).join("")}</div>
      <div style="margin-top:12px;font-size:11.5px;color:var(--ink-faint)">Set a field to <b style="color:var(--ink-soft)">— Not mapped —</b> to skip it. ${S.forAmazon ? "You need <b style=\"color:var(--ink)\">ASIN</b> + the ★ identifier mapped." : "<b style=\"color:var(--ink-soft)\">For Amazon = No</b> → just the Main SKU (no ASIN needed)."} Purchaser / Sourcer are set in the next step.</div>
    </div>`;
  }

  // ── STEP 2 ────────────────────────────────────────────────────────────────
  function stepConfigure() {
    const suf = suffixes();
    const formula = S.createBy === "UPC" ? "prefix + last 6 of UPC" : "prefix + MPN";
    const createDefs = [["main", "Main SKU", "The identity SKU"], ["shadow", "Child SKUs", `Shadows (${suf.fba} / ${suf.fbm}) + kits (_QY) when pack qty > 1`]];
    const createSummary = createDefs.filter(([k]) => S.create[k]).map(([, l]) => l.replace(" SKUs", "").replace(" SKU", "")).join(" + ") || "nothing selected";
    return `<div class="cpo-fadeup" style="margin-top:20px;display:grid;grid-template-columns:330px 1fr;gap:18px;align-items:start">
      <div style="background:var(--panel);border:1px solid var(--border);border-radius:22px;padding:20px;box-shadow:var(--shadow)">
        <div style="font-size:11px;font-weight:800;letter-spacing:.12em;color:var(--primary-ink)">BATCH SETTINGS</div>
        <div class="cpo-hf" style="font-weight:700;font-size:16px;letter-spacing:-.02em;margin:2px 0 15px">How to build the SKUs</div>
        <div style="font-size:11px;font-weight:800;color:var(--ink-faint);letter-spacing:.06em;margin-bottom:7px">TYPE <span style="font-weight:600;text-transform:none;letter-spacing:0">· set in step 1</span></div>
        <div style="display:flex;gap:8px;margin-bottom:13px;flex-wrap:wrap">
          <span style="font-size:12px;font-weight:800;padding:6px 13px;border-radius:999px;background:var(--primary-soft);color:var(--primary-ink)">${esc(S.batchType)}</span>
          <span style="font-size:12px;font-weight:800;padding:6px 13px;border-radius:999px;background:var(--panel-inset);color:var(--ink-soft)">by ${esc(S.createBy === "MPN" ? "Part No." : "UPC")}</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px;padding:9px 11px;border-radius:11px;background:var(--panel-inset);border:1px dashed var(--border-strong);margin-bottom:15px">
          <span>🧮</span><div style="font-size:11.5px;color:var(--ink-soft)">Main = <span style="font-family:ui-monospace,monospace;font-weight:700;color:var(--ink)">${formula}</span></div>
        </div>
        <div style="font-size:11px;font-weight:800;color:var(--ink-faint);letter-spacing:.06em;margin-bottom:7px">COMPANY</div>
        <div style="margin-bottom:6px">${dd("company", "", S.company, [["Ford Medical", "Ford Medical"], ["Turba", "Turba"]])}</div>
        <div style="display:flex;align-items:center;gap:8px;font-size:11.5px;color:var(--ink-soft);margin-bottom:15px">Shadow suffixes
          <span style="font-family:ui-monospace,monospace;font-weight:700;color:var(--primary-ink);background:var(--primary-soft);padding:2px 7px;border-radius:6px">${suf.fba}</span>
          <span style="font-family:ui-monospace,monospace;font-weight:700;color:var(--primary-ink);background:var(--primary-soft);padding:2px 7px;border-radius:6px">${suf.fbm}</span>
        </div>
        <div style="font-size:11px;font-weight:800;color:var(--ink-faint);letter-spacing:.06em;margin-bottom:7px">WHAT TO CREATE</div>
        <div style="display:flex;flex-direction:column;gap:8px">${createDefs.map(([k, label, desc]) => {
          const disabled = k === "shadow" && !S.forAmazon;
          const on = S.create[k] && !disabled;
          return `<div ${disabled ? "" : `data-act="toggle" data-arg="${k}"`} style="display:flex;align-items:center;gap:11px;padding:11px 13px;border-radius:12px;cursor:${disabled ? "not-allowed" : "pointer"};opacity:${disabled ? ".55" : "1"};background:${on ? "var(--primary-soft)" : "var(--panel-inset)"};border:1px solid ${on ? "var(--primary)" : "var(--border)"}">
            <span style="width:20px;height:20px;border-radius:6px;flex-shrink:0;display:flex;align-items:center;justify-content:center;background:${on ? "var(--primary)" : "transparent"};border:2px solid ${on ? "var(--primary)" : "var(--border-strong)"};color:#fff;font-size:12px;font-weight:900">${on ? "✓" : ""}</span>
            <div><div style="font-weight:700;font-size:13px;color:var(--ink)">${label}</div><div style="font-size:11px;color:var(--ink-faint)">${disabled ? "Turn on “For Amazon” in step 1" : esc(desc)}</div></div>
          </div>`;
        }).join("")}</div>
      </div>

      <div style="display:flex;flex-direction:column;gap:16px;min-width:0">
        ${mappingCard()}
        ${brandCard()}
      </div>
    </div>
    <div class="cpo-fadeup" style="display:flex;align-items:center;justify-content:space-between;gap:16px;margin-top:18px;padding:15px 20px;background:var(--panel);border:1px solid var(--border);border-radius:20px;box-shadow:var(--shadow)">
      <button data-act="back" class="cpo-navbtn" style="display:flex;align-items:center;gap:8px;padding:11px 18px;border-radius:13px;border:1px solid var(--border);background:transparent;color:var(--ink-soft);font-weight:700;font-size:13.5px;cursor:pointer"><svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M19 12H5m0 0l6-6m-6 6l6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>Back</button>
      <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;justify-content:flex-end">
        <div style="font-size:12.5px;color:var(--ink-soft)">A <b style="color:var(--ink)">${S.batchType}</b> batch for <b style="color:var(--ink)">${esc(S.company)}</b> · ${esc(createSummary)}</div>
        <button data-act="generate" class="cpo-lift" style="display:flex;align-items:center;gap:9px;padding:13px 24px;border-radius:14px;border:none;background:linear-gradient(135deg,var(--primary),#8a5cff);color:#fff;font-weight:800;font-size:14px;cursor:pointer;box-shadow:var(--shadow-lg)"><svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z" fill="#fff"/></svg>Generate &amp; check SellerCloud</button>
      </div>
    </div>`;
  }

  function mappingCard() {
    const fieldToCol = {};
    S.headers.forEach(col => { const f = S.colMap[col]; if (f && f !== "ignore" && !(f in fieldToCol)) fieldToCol[f] = col; });
    const recap = [["asin", "ASIN"], ["upc", "UPC"], ["mpn", "MPN"], ["name", "Name"], ["amzpack", "amz pack"], ["brand", "Brand"]];
    const mapped = recap.filter(([id]) => fieldToCol[id]).length;
    const person = (who, label) => {
      const p = S[who]; const isCol = p.mode === "column";
      return `<div style="padding:11px 13px;border-radius:13px;background:var(--panel-inset);border:1px solid var(--border)">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:7px">
          <span style="font-size:12px;font-weight:800;color:var(--ink)">${label}</span>
          <div style="display:flex;padding:2px;border-radius:8px;background:var(--panel);border:1px solid var(--border)">
            <button data-act="pmode" data-arg="${who}:column" style="padding:4px 9px;border-radius:6px;border:none;cursor:pointer;font-weight:800;font-size:10.5px;background:${isCol ? "var(--primary)" : "transparent"};color:${isCol ? "#fff" : "var(--ink-soft)"}">Column</button>
            <button data-act="pmode" data-arg="${who}:value" style="padding:4px 9px;border-radius:6px;border:none;cursor:pointer;font-weight:800;font-size:10.5px;background:${!isCol ? "var(--primary)" : "transparent"};color:${!isCol ? "#fff" : "var(--ink-soft)"}">Value</button>
          </div>
        </div>
        ${isCol
          ? dd("pcol", who, p.col, S.headers.map(h => [h, h]))
          : `<input data-in="pval" data-arg="${who}" value="${esc(p.value)}" placeholder="Type a name for all rows" style="width:100%;padding:8px 11px;border-radius:10px;border:1px solid var(--border);background:var(--panel);color:var(--ink);font-weight:600;font-size:12.5px;outline:none">`}
      </div>`;
    };
    return `<div style="background:var(--panel);border:1px solid var(--border);border-radius:22px;padding:20px;box-shadow:var(--shadow)">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px">
        <div><div style="font-size:11px;font-weight:800;letter-spacing:.12em;color:var(--sky)">COLUMN MAPPING</div><div class="cpo-hf" style="font-weight:700;font-size:16px;letter-spacing:-.02em;margin-top:2px">Mapped from your sheet</div></div>
        <span style="font-size:11.5px;font-weight:800;color:var(--mint);background:var(--mint-soft);padding:5px 11px;border-radius:999px">${mapped}/${recap.length} mapped</span>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:14px">${recap.map(([id, label]) => {
        const col = fieldToCol[id]; const ok = !!col;
        return `<div style="display:flex;align-items:center;gap:7px;padding:7px 11px;border-radius:10px;background:${ok ? "var(--primary-soft)" : "var(--panel-inset)"};border:1px solid ${ok ? "var(--primary)" : "var(--border)"}"><span style="font-size:11.5px;font-weight:800;color:${ok ? "var(--primary-ink)" : "var(--ink-faint)"}">${label}</span><span style="color:var(--ink-faint);font-size:11px">→</span><span style="font-family:ui-monospace,monospace;font-weight:700;font-size:11.5px;color:${ok ? "var(--ink)" : "var(--ink-faint)"}">${esc(col || "—")}</span></div>`;
      }).join("")}</div>
      <div style="margin-top:9px;font-size:11.5px;color:var(--ink-faint)">Fields were mapped in step 1 — go back to change them. <b style="color:var(--ink-soft)">Purchaser &amp; Sourcer</b> are auto-filled per brand from the catalog (edit them in the Brand panel below).</div>
    </div>`;
  }

  function brandCard() {
    const single = S.brandMode === "single";
    return `<div style="background:var(--panel);border:1px solid var(--primary);border-radius:22px;box-shadow:var(--shadow-lg);overflow:hidden">
      <div style="padding:20px 20px 16px;background:linear-gradient(180deg,var(--primary-soft),transparent)">
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:14px;flex-wrap:wrap">
          <div>
            <div style="font-size:11px;font-weight:800;letter-spacing:.12em;color:var(--primary-ink);display:flex;align-items:center;gap:7px"><svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M4 4h9l7 7-9 9-7-7V4z" stroke="var(--primary-ink)" stroke-width="1.9" stroke-linejoin="round"/><circle cx="8.5" cy="8.5" r="1.6" fill="var(--primary-ink)"/></svg>BRAND — THE GROUPING KEY</div>
            <div class="cpo-hf" style="font-weight:800;font-size:18px;letter-spacing:-.02em;margin-top:3px">Each brand's manufacturer, prefix, purchaser &amp; sourcer — auto-filled</div>
          </div>
          <div style="display:flex;padding:4px;border-radius:12px;background:var(--panel);border:1px solid var(--border);box-shadow:var(--shadow)">
            <button data-act="bmode" data-arg="single" style="padding:8px 14px;border-radius:8px;border:none;cursor:pointer;font-weight:800;font-size:12.5px;${seg(single)}">One brand</button>
            <button data-act="bmode" data-arg="column" style="padding:8px 14px;border-radius:8px;border:none;cursor:pointer;font-weight:800;font-size:12.5px;${seg(!single)}">Map column</button>
          </div>
        </div>
      </div>
      ${single ? brandSingle() : brandColumn()}
    </div>`;
  }

  function brandSingle() {
    const L = (t) => `<div style="font-size:11px;font-weight:800;color:var(--ink-faint);letter-spacing:.06em;margin-bottom:6px">${t}</div>`;
    const inp = (key, val, ph, extra) => `<input data-in="${key}" value="${esc(val)}" placeholder="${ph}" style="width:100%;padding:10px 12px;border-radius:12px;border:1px solid var(--border);background:var(--panel-inset);color:var(--ink);font-weight:600;font-size:13px;outline:none;${extra || ""}">`;
    return `<div style="padding:6px 20px 22px">
      <div style="font-size:12.5px;color:var(--ink-soft);margin-bottom:12px">This whole batch is one brand — we auto-fill everything from the catalog; edit if needed.</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
        <div style="grid-column:1 / -1">${L("BRAND")}<input data-in="sbrand" value="${esc(S.singleBrand)}" placeholder="e.g. Zyrtec" style="width:100%;padding:11px 13px;border-radius:12px;border:2px solid var(--primary);background:var(--panel);color:var(--ink);font-weight:800;font-size:14px;font-family:'Bricolage Grotesque',sans-serif;outline:none"></div>
        <div>${L("MANUFACTURER")}${inp("smfr", S.singleMfr, "Manufacturer")}</div>
        <div>${L("PREFIX")}${inp("sprefix", S.singlePrefix, "PFX", "color:var(--primary-ink);font-weight:800;font-family:ui-monospace,monospace;text-transform:uppercase")}</div>
        <div>${L("PURCHASER")}${inp("sbpurch", S.singlePurchaser, "Purchaser")}</div>
        <div>${L("SOURCER")}${inp("sbsourcer", S.singleSourcer, "Sourcer")}</div>
      </div>
      ${S.brandLoading ? `<div style="margin-top:12px;font-size:12px;color:var(--ink-soft)">Looking up brand in the catalog…</div>` : ""}
      <div style="margin-top:12px;display:flex;align-items:center;gap:9px;padding:10px 13px;border-radius:12px;background:var(--sky-soft);border:1px solid var(--sky);font-size:12px;color:var(--ink)"><span>ℹ️</span><span>Type the brand and press Tab — manufacturer, prefix, purchaser &amp; sourcer auto-fill from the catalog (new brands get a unique prefix).</span></div>
    </div>`;
  }

  function brandColumn() {
    const bcol = S.brandCol || firstBrandCol() || "Brand";
    const newCount = S.brandTable.filter(b => b.status === "new").length;
    return `<div style="padding:6px 20px 22px">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:14px">
        <div style="display:flex;align-items:center;gap:9px;font-size:12.5px;color:var(--ink-soft)">Brand column
          <div style="min-width:150px">${dd("brandcol", "", bcol, S.headers.map(h => [h, h]))}</div>
          <span style="color:var(--ink-faint)">·</span><span><b style="color:var(--ink)">${S.brandTable.length}</b> brands found</span>
        </div>
        ${newCount ? `<span style="display:inline-flex;align-items:center;gap:6px;font-size:11.5px;font-weight:800;color:var(--amber);background:var(--amber-soft);padding:5px 11px;border-radius:999px">● ${newCount} new brand${newCount > 1 ? "s" : ""} to confirm</span>` : ""}
      </div>
      ${S.brandLoading ? `<div style="padding:24px;text-align:center;color:var(--ink-soft);font-size:13px">Loading brands from SellerCloud…</div>` : brandTableGrid()}
      <div style="margin-top:12px;font-size:11.5px;color:var(--ink-faint)">New brands get an auto-generated prefix that's unique against the SellerCloud registry — edit any chip to override.</div>
    </div>`;
  }

  function brandTableGrid() {
    if (!S.brandTable.length) return `<div style="padding:20px;text-align:center;color:var(--ink-faint);font-size:12.5px">No brands detected in that column.</div>`;
    const L = (t) => `<div style="font-size:10px;font-weight:800;color:var(--ink-faint);letter-spacing:.06em;margin-bottom:5px">${t}</div>`;
    const inp = (key, brand, val, extra) => `<input data-in="${key}" data-arg="${esc(brand)}" value="${esc(val)}" style="width:100%;padding:8px 10px;border-radius:9px;border:1px solid var(--border);background:var(--panel);color:var(--ink);font-weight:600;font-size:12.5px;outline:none;${extra || ""}">`;
    const cards = S.brandTable.map(b => {
      const isNew = b.status === "new"; const confirmed = !!S.brandConfirmed[b.brand];
      const ov = S.brandOverride[b.brand] || {};
      const bname = ov.brand_name != null ? ov.brand_name : (b.brand_name || b.brand);
      const mfr = ov.manufacturer != null ? ov.manufacturer : (b.manufacturer || "");
      const prefix = ov.prefix != null ? ov.prefix : (b.prefix || "");
      const purch = ov.purchaser != null ? ov.purchaser : (b.purchaser || "");
      const src = ov.sourcer != null ? ov.sourcer : (b.sourcer || "");
      const accent = isNew ? "var(--amber)" : "var(--mint)";
      const sm = isNew ? { bg: "var(--amber-soft)", ink: "var(--amber)" } : { bg: "var(--mint-soft)", ink: "var(--mint)" };
      const attn = isNew && !confirmed;
      return `<div style="border:1px solid ${attn ? "var(--amber)" : "var(--border)"};border-left:3px solid ${accent};border-radius:14px;padding:14px 16px;background:${attn ? "var(--amber-soft)" : "var(--panel)"}">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px">
          <input data-in="bname" data-arg="${esc(b.brand)}" value="${esc(bname)}" class="cpo-bname cpo-hf" title="Rename this brand for the export (the BrandName column). Grouping stays the same." style="font-weight:800;font-size:15px;color:var(--ink);border:1px solid transparent;background:transparent;border-radius:8px;padding:3px 7px;outline:none;min-width:110px;max-width:240px;font-family:inherit">
          <span style="display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:999px;font-weight:800;font-size:10.5px;background:${sm.bg};color:${sm.ink}">● ${isNew ? "New brand" : "In catalog"}</span>
          <span style="font-size:11px;color:var(--ink-faint)">${b.row_count} row${b.row_count === 1 ? "" : "s"}</span>
          <button data-act="bconfirm" data-arg="${esc(b.brand)}" class="cpo-lift" style="margin-left:auto;display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border-radius:9px;cursor:pointer;font-weight:800;font-size:11.5px;border:1px solid ${confirmed ? "var(--mint)" : (isNew ? "var(--amber)" : "var(--border)")};background:${confirmed ? "var(--mint-soft)" : (isNew ? "var(--amber)" : "var(--panel)")};color:${confirmed ? "var(--mint)" : (isNew ? "#fff" : "var(--ink-soft)")}">${confirmed ? "✓ Confirmed" : (isNew ? "Confirm" : "✎ Edit")}</button>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px">
          <div>${L("MANUFACTURER")}${inp("bmfr", b.brand, mfr)}${(b.ai_mfr && !confirmed) ? `<div style="font-size:9.5px;font-weight:800;color:var(--primary-ink);margin-top:4px">✦ AI-suggested</div>` : ""}</div>
          <div>${L("PREFIX")}${inp("bprefix", b.brand, prefix, "color:var(--primary-ink);font-weight:800;font-family:ui-monospace,monospace;text-transform:uppercase")}</div>
          <div>${L("PURCHASER")}${inp("bpurch", b.brand, purch)}</div>
          <div>${L("SOURCER")}${inp("bsourcer", b.brand, src)}</div>
        </div>
      </div>`;
    }).join("");
    return `<div style="display:flex;flex-direction:column;gap:12px">${cards}</div>`;
  }

  // ── STEP 3 ────────────────────────────────────────────────────────────────
  function stepGenerating() {
    return `<div style="margin-top:20px;padding:52px 22px;background:var(--panel);border:1px solid var(--border);border-radius:24px;box-shadow:var(--shadow);animation:cpoFadeIn .3s ease both">
      <div style="max-width:520px;margin:0 auto;text-align:center">
        <div style="position:relative;width:92px;height:92px;margin:0 auto 20px">
          <div style="position:absolute;inset:0;border-radius:50%;border:4px solid var(--primary-soft);border-top-color:var(--primary);animation:cpoSpin .9s linear infinite"></div>
          <div style="position:absolute;inset:22px;border-radius:16px;background:var(--primary-soft);display:flex;align-items:center;justify-content:center;font-size:24px;animation:cpoWiggle 1.2s ease-in-out infinite">🔎</div>
        </div>
        <div class="cpo-hf" style="font-weight:800;font-size:21px">Building SKUs &amp; checking SellerCloud…</div>
        <div style="color:var(--ink-soft);font-size:13px;margin-top:4px">Searching by UPC, UPC with a leading 0, MPN, and ASIN so nothing gets duplicated.</div>
        <div style="max-width:400px;margin:20px auto 0;height:10px;border-radius:999px;background:var(--panel-inset);overflow:hidden;position:relative"><div style="position:absolute;top:0;height:100%;width:45%;border-radius:999px;background:linear-gradient(90deg,var(--primary),var(--accent));animation:cpoIndet 1.1s ease-in-out infinite"></div></div>
      </div>
    </div>`;
  }

  // ── STEP 4 ────────────────────────────────────────────────────────────────
  function stepReview() {
    const r = S.review; if (!r) return "";
    const c = r.counts, suf = r.suffixes;
    const newBrands = c.new_brands || [];
    const allConfirmed = newBrands.every(b => S.brandConfirmed[b]);
    const fseg = (on, ink, bg) => on ? `background:${bg};color:${ink};box-shadow:var(--shadow)` : "background:transparent;color:var(--ink-soft)";
    return `<div class="cpo-fadeup">
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:18px">
        ${statCard("Total items", c.total, "var(--ink)", "var(--panel)", "var(--border)")}
        ${statCard("＋ New mains", c.new, "var(--sky)", "var(--sky-soft)", "var(--sky)")}
        ${statCard("✓ Already on SellerCloud", c.on_sc, "var(--mint)", "var(--mint-soft)", "var(--mint)")}
        ${statCard("● New brands", c.new_brand_count, "var(--amber)", "var(--amber-soft)", "var(--amber)")}
      </div>
      ${c.new_brand_count ? `<div style="margin-top:14px;display:flex;align-items:center;gap:12px;padding:13px 16px;border-radius:16px;background:var(--amber-soft);border:1px solid var(--amber)"><span style="width:30px;height:30px;border-radius:10px;background:#fff;display:flex;align-items:center;justify-content:center;font-size:15px;flex-shrink:0">🏷️</span><div style="font-size:13px;color:var(--ink);flex:1"><b style="color:var(--amber)">New brand${c.new_brand_count > 1 ? "s" : ""}:</b> ${esc(newBrands.join(", "))} <span style="color:var(--ink-soft)">— manufacturer &amp; prefix were AI-suggested. Confirm before export.</span></div><button data-act="confirm-all-new" class="cpo-lift" style="padding:8px 14px;border-radius:10px;border:1px solid var(--amber);background:#fff;color:var(--amber);font-weight:800;font-size:12.5px;cursor:pointer">${allConfirmed ? "✓ All confirmed" : "Confirm all new"}</button></div>` : ""}
      <div style="display:flex;align-items:center;justify-content:space-between;gap:14px;margin-top:16px;flex-wrap:wrap">
        <div style="display:flex;padding:4px;border-radius:12px;background:var(--panel);border:1px solid var(--border);box-shadow:var(--shadow)">
          <button data-act="filter" data-arg="all" style="padding:8px 15px;border-radius:9px;border:none;cursor:pointer;font-weight:700;font-size:12.5px;${fseg(S.filter === "all", "var(--ink)", "var(--panel-inset)")}">All ${c.total}</button>
          <button data-act="filter" data-arg="new" style="padding:8px 15px;border-radius:9px;border:none;cursor:pointer;font-weight:700;font-size:12.5px;${fseg(S.filter === "new", "var(--amber)", "var(--amber-soft)")}">New brands ${c.new_brand_rows}</button>
          <button data-act="filter" data-arg="sc" style="padding:8px 15px;border-radius:9px;border:none;cursor:pointer;font-weight:700;font-size:12.5px;${fseg(S.filter === "sc", "var(--mint)", "var(--mint-soft)")}">On SellerCloud ${c.on_sc}</button>
        </div>
        <div style="display:flex;align-items:center;gap:8px;padding:9px 13px;border-radius:12px;border:1px solid var(--border);background:var(--panel);min-width:260px;box-shadow:var(--shadow)"><svg width="15" height="15" viewBox="0 0 24 24" fill="none"><circle cx="11" cy="11" r="6" stroke="var(--ink-faint)" stroke-width="1.9"/><path d="M20 20l-4-4" stroke="var(--ink-faint)" stroke-width="1.9" stroke-linecap="round"/></svg><input id="cpo-search" data-in="search" value="${esc(S.query)}" placeholder="Search brand, ASIN, or SKU…" style="border:none;background:transparent;outline:none;font-family:inherit;font-size:13px;color:var(--ink);width:100%"></div>
      </div>
      <div id="cpo-board" style="margin-top:14px;background:var(--panel);border:1px solid var(--border);border-radius:20px;box-shadow:var(--shadow);overflow:hidden">${boardInner()}</div>
      ${exportBar()}
    </div>`;
  }

  function statCard(label, val, ink, bg, border) {
    return `<div style="padding:15px 17px;border-radius:18px;background:${bg};border:1px solid ${border}"><div style="font-size:12px;font-weight:800;color:${ink}">${label}</div><div class="cpo-hf" style="font-weight:800;font-size:28px;letter-spacing:-.03em;margin-top:2px;color:${ink}">${val}</div></div>`;
  }

  function boardInner() {
    const suf = S.review.suffixes;
    const cols = "150px 128px 132px 1.1fr 1.5fr 1.5fr 1.35fr";
    const q = (S.query || "").trim().toLowerCase();
    const head = `<div style="display:grid;grid-template-columns:${cols};padding:12px 20px;background:var(--panel-inset);font-size:10.5px;font-weight:800;letter-spacing:.06em;color:var(--ink-soft)"><div>BRAND</div><div>ASIN</div><div>UPC / MPN</div><div>MAIN SKU</div><div>CHILD SKU ${suf.fba}</div><div>CHILD SKU ${suf.fbm}</div><div>NOTES</div></div>`;
    let bodyCount = 0;
    const groups = S.review.groups.map(g => {
      const rows = g.rows.filter(row => {
        if (S.filter === "new" && g.status !== "new") return false;
        if (S.filter === "sc" && !row.main_on_sc) return false;
        if (q) return (g.brand + " " + (g.brand_name || "") + " " + row.asin + " " + (row.main || "") + " " + (row.fba || "") + " " + (row.fbm || "") + " " + (row.id_value || "")).toLowerCase().includes(q);
        return true;
      });
      if (!rows.length) return "";
      bodyCount += rows.length;
      const isNew = g.status === "new"; const confirmed = !!S.brandConfirmed[g.brand];
      const gname = g.brand_name || g.brand;
      const accent = isNew ? "var(--amber)" : "var(--mint)";
      const sm = isNew ? { bg: "var(--amber-soft)", ink: "var(--amber)", label: "New brand" } : { bg: "var(--mint-soft)", ink: "var(--mint)", label: "In catalog" };
      const grpHead = `<div style="display:flex;align-items:center;gap:11px;padding:10px 20px;border-top:1px solid var(--border);background:${isNew && !confirmed ? "var(--amber-soft)" : "var(--panel-soft)"}"><span style="width:22px;height:22px;border-radius:7px;flex-shrink:0;background:${accent};color:#fff;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:900">${esc((gname || "?").charAt(0))}</span><span class="cpo-hf" style="font-weight:800;font-size:14px;color:var(--ink)">${esc(gname)}</span><span style="display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:999px;font-weight:800;font-size:10.5px;background:${sm.bg};color:${sm.ink}">● ${sm.label}</span><span style="font-size:11.5px;color:var(--ink-faint)">${g.row_count} items · ${esc(g.manufacturer || "—")} · ${esc(g.prefix || "—")}</span>${isNew ? `<button data-act="bconfirm" data-arg="${esc(g.brand)}" class="cpo-lift" style="margin-left:auto;display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border-radius:9px;cursor:pointer;font-weight:800;font-size:11.5px;border:1px solid ${confirmed ? "var(--mint)" : "var(--amber)"};background:${confirmed ? "var(--mint-soft)" : "var(--amber)"};color:${confirmed ? "var(--mint)" : "#fff"}">${confirmed ? "✓ Confirmed" : "Confirm all"}</button>` : ""}</div>`;
      const body = rows.map(row => {
        return `<div style="display:grid;grid-template-columns:${cols};align-items:stretch;border-top:1px solid var(--border)">
          <div style="padding:14px 12px 14px 20px;display:flex;align-items:center;font-weight:700;font-size:12.5px;color:var(--ink);border-left:3px solid ${accent}">${esc(gname)}</div>
          <div style="padding:14px 12px;display:flex;align-items:center;font-family:ui-monospace,monospace;font-size:12px;color:var(--ink-soft)">${esc(row.asin)}</div>
          <div style="padding:14px 12px;display:flex;flex-direction:column;justify-content:center;font-family:ui-monospace,monospace;font-size:12px;color:var(--ink-soft)"><span>${esc(row.id_value)}</span><span style="font-size:10px;color:var(--ink-faint);margin-top:2px">${esc(row.id_kind)}</span></div>
          ${editableCell(row.id, "main", cellVal(row.id, "main", row.main), true, row.main_on_sc, true)}
          ${childCell(row, "fba")}
          ${childCell(row, "fbm")}
          <div style="padding:14px 20px 14px 12px;display:flex;align-items:center">${row.note ? `<span style="display:inline-flex;align-items:center;gap:7px;font-size:11.5px;font-weight:600;color:${noteInk(row, g)};background:${noteBg(row, g)};padding:6px 11px;border-radius:9px;line-height:1.3">${noteIcon(row, g)} ${esc(row.note)}</span>` : ""}</div>
        </div>`;
      }).join("");
      return grpHead + body;
    }).join("");
    const empty = bodyCount === 0 ? `<div style="padding:42px 22px;text-align:center;color:var(--ink-soft)"><div style="font-size:28px">🫧</div><div style="font-weight:700;font-size:15px;margin-top:8px;color:var(--ink)">Nothing matches</div><div style="font-size:13px">Clear the search or switch the filter.</div></div>` : "";
    return `<div style="overflow-x:auto"><div style="min-width:1180px">${head}${groups}${empty}</div></div>`;
  }

  // Inner control for an editable SKU cell (no outer padding) — reused by the Main
  // column and by childCell (which stacks a shadow + its kit shadow).
  function editCellInner(id, field, value, present, onSC, bold) {
    const key = id + "." + field;
    if (!present) return `<span style="color:var(--ink-faint);padding-left:9px">—</span>`;
    if (S.skuChecking[key]) {
      return `<div class="cpo-checking" style="width:100%;display:flex;align-items:center;gap:7px;padding:7px 9px;border-radius:9px;background:var(--panel-inset)"><span class="cpo-spin"></span><span style="font-family:ui-monospace,monospace;font-weight:${bold ? "800" : "700"};font-size:12px;color:var(--ink-soft)">${esc(value)}</span><span style="margin-left:auto;font-size:9.5px;font-weight:700;color:var(--ink-faint)">checking…</span></div>`;
    }
    if (S.editCell === key) {
      return `<input data-in="cell" data-arg="${key}" data-focus="1" value="${esc(value)}" style="width:100%;padding:7px 9px;border-radius:9px;border:2px solid var(--primary);background:var(--panel);color:var(--primary-ink);font-family:ui-monospace,monospace;font-weight:800;font-size:12px;outline:none">`;
    }
    return `<div data-act="edit" data-arg="${key}" class="cpo-editcell" title="Click to edit" style="width:100%;display:flex;align-items:center;gap:6px;padding:7px 9px;border-radius:9px;background:${onSC ? "var(--mint-soft)" : "transparent"}"><span style="font-family:ui-monospace,monospace;font-weight:${bold ? "800" : "700"};font-size:12px;color:${bold ? "var(--ink)" : "var(--ink-soft)"}">${esc(value)}</span>${onSC ? `<span style="font-size:9px;font-weight:800;color:var(--mint);background:var(--mint-soft);padding:2px 6px;border-radius:6px;white-space:nowrap">✓ on SC</span>` : ""}<span style="margin-left:auto;opacity:.4;font-size:11px">✎</span></div>`;
  }

  function editableCell(id, field, value, present, onSC, bold) {
    return `<div style="padding:10px;display:flex;align-items:center">${editCellInner(id, field, value, present, onSC, bold)}</div>`;
  }

  // A "Child SKU" column cell: the single child SKU for this ASIN + channel (editable).
  // For a multipack ASIN this SKU IS the _QY{n} kit shadow, tagged "KIT · Pack of n".
  function childCell(row, field) {
    const present = !!row[field];
    const inner = editCellInner(row.id, field, cellVal(row.id, field, row[field]), present, row[field + "_on_sc"], false);
    const tag = (present && row.is_kit)
      ? `<div title="Kit shadow (multipack, from the amz pack qty)" style="align-self:flex-start;display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border-radius:7px;background:var(--primary-soft)"><span style="font-size:8px;font-weight:800;letter-spacing:.05em;color:var(--primary-ink)">KIT</span><span style="font-size:10px;font-weight:700;color:var(--primary-ink)">Pack of ${row.pack}</span></div>`
      : "";
    return `<div style="padding:8px 10px;display:flex;flex-direction:column;gap:5px;justify-content:center">${inner}${tag}</div>`;
  }
  const noteBg = (row, g) => row.main_on_sc ? "var(--mint-soft)" : (g.status === "new" ? "var(--amber-soft)" : "var(--sky-soft)");
  const noteInk = (row, g) => row.main_on_sc ? "var(--mint)" : (g.status === "new" ? "var(--amber)" : "var(--sky)");
  const noteIcon = (row, g) => row.main_on_sc ? "✓" : (g.status === "new" ? "●" : "ℹ");

  function exportBar() {
    const hasKits = (S.review?.groups || []).some(g => (g.rows || []).some(r => r.kit_value));
    const hasShadows = (S.review?.groups || []).some(g => (g.rows || []).some(r => r.fba || r.fbm));
    const files = [["◆", "Bulk import", "All SKUs", "var(--sky-soft)", "var(--sky)"]];
    if (hasKits) files.push(["▣", "Kit file", "Multipack kits", "var(--primary-soft)", "var(--primary-ink)"]);
    if (hasShadows) files.push(["⬡", "Amz template", "Amazon shadows", "var(--mint-soft)", "var(--mint)"]);
    return `<div style="margin-top:20px;background:var(--panel);border:1px solid var(--border);border-radius:22px;box-shadow:var(--shadow);padding:20px 22px">
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <div style="width:42px;height:42px;border-radius:13px;background:var(--mint-soft);color:var(--mint);display:flex;align-items:center;justify-content:center;flex-shrink:0"><svg width="21" height="21" viewBox="0 0 24 24" fill="none"><path d="M12 4v11m0 0l-4-4m4 4l4-4M5 20h14" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
        <div style="flex:1;min-width:200px"><div class="cpo-hf" style="font-weight:800;font-size:16px;letter-spacing:-.02em">Export import files</div><div style="font-size:12.5px;color:var(--ink-soft)">Generates the bulk, kit and amz files, ready to import into SellerCloud.</div></div>
        <div style="display:flex;gap:9px;flex-wrap:wrap">${files.map(([ic, n, d, bg, ink]) => `<div style="display:flex;align-items:center;gap:9px;padding:9px 13px;border-radius:12px;background:var(--panel-inset);border:1px solid var(--border)"><span style="width:28px;height:28px;border-radius:8px;background:${bg};color:${ink};display:flex;align-items:center;justify-content:center;font-size:14px">${ic}</span><div><div style="font-weight:800;font-size:12.5px;color:var(--ink)">${n}</div><div style="font-size:10.5px;color:var(--ink-faint)">${d}</div></div></div>`).join("")}</div>
      </div>
      <div style="display:flex;align-items:center;justify-content:space-between;gap:14px;margin-top:18px;padding-top:16px;border-top:1px solid var(--border);flex-wrap:wrap">
        <div style="display:flex;align-items:center;gap:10px">
          <button data-act="back" class="cpo-navbtn" style="display:flex;align-items:center;gap:8px;padding:11px 18px;border-radius:13px;border:1px solid var(--border);background:transparent;color:var(--ink-soft);font-weight:700;font-size:13.5px;cursor:pointer"><svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M19 12H5m0 0l6-6m-6 6l6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>Back</button>
          <button data-act="home" class="cpo-navbtn" style="display:flex;align-items:center;gap:8px;padding:11px 18px;border-radius:13px;border:1px solid var(--border);background:transparent;color:var(--ink-soft);font-weight:700;font-size:13.5px;cursor:pointer"><svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M3 11l9-8 9 8m-2 2v7a1 1 0 01-1 1h-4v-5H10v5H6a1 1 0 01-1-1v-7" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>Home</button>
        </div>
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <label style="display:flex;align-items:center;gap:7px;font-size:12.5px;font-weight:700;color:var(--ink-soft);cursor:pointer;user-select:none"><input type="checkbox" id="cpo-ai-titles"${S.aiTitles ? " checked" : ""} style="width:15px;height:15px;accent-color:var(--primary);cursor:pointer">✨ Improve titles with AI</label>
          <button title="Coming soon" style="display:flex;align-items:center;gap:8px;padding:12px 18px;border-radius:13px;border:1px dashed var(--border-strong);background:transparent;color:var(--ink-faint);font-weight:800;font-size:13px;cursor:not-allowed">Create directly in SellerCloud<span style="font-size:10px;font-weight:800;background:var(--panel-inset);color:var(--ink-soft);padding:2px 8px;border-radius:999px">SOON</span></button>
          <button data-act="export" class="cpo-lift" style="display:flex;align-items:center;gap:9px;padding:13px 24px;border-radius:14px;border:none;background:linear-gradient(135deg,var(--mint),#0E8E68);color:#fff;font-weight:800;font-size:14px;cursor:${S.exporting ? "wait" : "pointer"};opacity:${S.exporting ? ".7" : "1"};box-shadow:0 12px 26px -12px var(--mint)"><svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M12 4v11m0 0l-4-4m4 4l4-4M5 20h14" stroke="#fff" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>${S.exporting ? "Improving titles…" : (S.exported ? ("✓ Exported" + (S.exportId ? " · " + S.exportId : "")) : "Export files")}</button>
        </div>
      </div>
    </div>`;
  }

  // ── derived helpers ──────────────────────────────────────────────────────
  function suffixes() { return S.company === "Turba" ? { fba: "-FBATRB", fbm: "-FBMTRB" } : { fba: "-FBA", fbm: "-FBM" }; }
  function firstBrandCol() { for (const c of S.headers) if (S.colMap[c] === "brand") return c; return ""; }
  function columnForField(field) { for (const c of S.headers) if (S.colMap[c] === field) return c; return ""; }
  function setFieldColumn(field, col) {
    S.headers.forEach(c => { if (S.colMap[c] === field) S.colMap[c] = "ignore"; });   // one column per field
    if (col) S.colMap[col] = field;
  }
  function requiredMissing() {
    const mapped = new Set(S.headers.map(h => S.colMap[h]).filter(f => f && f !== "ignore"));
    const idField = S.createBy === "UPC" ? "upc" : "mpn";
    const req = [[idField, idField === "upc" ? "UPC" : "Item ID (MPN)"]];
    if (S.forAmazon) req.unshift(["asin", "ASIN"]);
    return req.filter(([f]) => !mapped.has(f));
  }
  function collectConfig() {
    return {
      token: S.token, batch_type: S.batchType, create_by: S.createBy, company: S.company,
      create: { main: S.create.main, shadow: S.create.shadow, kit: S.create.shadow }, col_map: S.colMap, brand_mode: S.brandMode,
      brand_col: S.brandCol || firstBrandCol(),
      single_brand: S.singleBrand, single_mfr: S.singleMfr, single_prefix: S.singlePrefix,
      single_purchaser: S.singlePurchaser, single_sourcer: S.singleSourcer,
      brand_overrides: S.brandOverride,
      purchaser: S.purchaser, sourcer: S.sourcer,
    };
  }

  // ── events ────────────────────────────────────────────────────────────────
  function afterRender() {
    const fileEl = document.getElementById("cpo-file");
    if (fileEl) fileEl.addEventListener("change", onFilePicked);
    const drop = document.getElementById("cpo-drop");
    if (drop) {
      drop.addEventListener("dragover", e => { e.preventDefault(); drop.style.borderColor = "var(--sky)"; drop.style.background = "var(--sky-soft)"; });
      drop.addEventListener("dragleave", () => { drop.style.borderColor = "var(--border-strong)"; drop.style.background = "var(--panel-inset)"; });
      drop.addEventListener("drop", e => { e.preventDefault(); if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]); });
    }
    // focus preservation for edit cell / search
    const f = S.root.querySelector('[data-focus="1"]');
    if (f) { f.focus(); const v = f.value; f.value = ""; f.value = v; }
    // Safety: if a menu was somehow left open across a render, re-show it.
    if (S.openDD) showMenu(S.openDD);
  }

  function removePortal() {
    const p = document.getElementById("cpo-portal");
    if (p) p.remove();
  }

  // Clicks on the portaled menu (now a child of <body>, outside S.root) and
  // outside-to-close are handled here, since S.root's delegate can't see them.
  function onDocClick(e) {
    const opt = e.target.closest("#cpo-portal .cpo-opt");
    if (opt) { ddPick(opt.getAttribute("data-arg")); return; }
    // outside click closes the open menu — no re-render needed
    if (S.openDD && !e.target.closest(".cpo-dd") && !e.target.closest("#cpo-portal")) closeMenu();
  }

  function onClick(e) {
    const t = e.target.closest("[data-act]");
    if (!t || !S.root.contains(t)) return;
    const act = t.getAttribute("data-act"), arg = t.getAttribute("data-arg");
    // any action other than toggling a dropdown dismisses an open menu (no render)
    if (act !== "dd-toggle" && S.openDD) closeMenu();
    switch (act) {
      case "dd-toggle": if (S.openDD === arg) closeMenu(); else { S.openDD = arg; showMenu(arg); } break;
      case "dd-pick": ddPick(arg); break;
      case "pick": document.getElementById("cpo-file").click(); break;
      case "clear-file": resetUpload(); render(); break;
      case "goto": if (+arg <= S.stage) { S.stage = +arg; S.generating = false; render(); } break;
      // Generate is a loading overlay, not a step, so plain back works: 4→3→2→1.
      case "back": S.stage = Math.max(1, S.stage - 1); S.generating = false; render(); break;
      case "go2": if (S.token) { S.stage = 2; render(); } break;                          // Upload → Map
      case "go3": if (S.token && requiredMissing().length === 0) enterConfigure(); break;  // Map → Configure
      case "home": goHome(); break;
      case "batch": S.batchType = arg; S.createBy = arg === "Medical" ? "MPN" : "UPC"; render(); break;
      case "by": S.createBy = arg; render(); break;
      case "toggle": if (!(arg === "shadow" && !S.forAmazon)) { S.create[arg] = !S.create[arg]; } render(); break;
      case "foramz": { const on = arg === "yes"; if (on !== S.forAmazon) { S.forAmazon = on; S.create.shadow = on; } render(); break; }
      case "bmode": S.brandMode = arg; render(); if (arg === "column") loadBrands(); break;
      case "bconfirm": S.brandConfirmed[arg] = !S.brandConfirmed[arg]; render(); break;
      case "confirm-all-new": (S.review.counts.new_brands || []).forEach(b => S.brandConfirmed[b] = true); render(); break;
      case "pmode": { const [who, mode] = arg.split(":"); S[who].mode = mode; render(); break; }
      case "filter": S.filter = arg; render(); break;
      case "edit": S.editCell = arg; render(); break;
      case "generate": doGenerate(); break;
      case "export": doExport(); break;
    }
  }

  function ddPick(arg) {
    const bar = arg.indexOf("|");
    const key = arg.slice(0, bar), val = decodeURIComponent(arg.slice(bar + 1));
    const sep = key.indexOf("::");
    const kind = key.slice(0, sep), a = key.slice(sep + 2);
    if (kind === "colmap") S.colMap[a] = val;
    else if (kind === "fieldmap") setFieldColumn(a, val);
    else if (kind === "company") S.company = val;
    else if (kind === "pcol") S[a].col = val;
    else if (kind === "brandcol") { S.brandCol = val; S.colMap[val] = "brand"; }
    else if (kind === "sheet") S.sheet = val;
    closeMenu();
    render();
    if (kind === "brandcol") loadBrands();
    if (kind === "sheet") reparse();
  }

  function onInput(e) {
    const t = e.target.closest("[data-in]"); if (!t || !S.root.contains(t)) return;
    const kind = t.getAttribute("data-in"), arg = t.getAttribute("data-arg"), v = t.value;
    switch (kind) {
      case "sbrand": S.singleBrand = v; break;
      case "smfr": S.singleMfr = v; break;
      case "sprefix": { const clean = v.toUpperCase().replace(/[^A-Z0-9&]/g, "").slice(0, 8); S.singlePrefix = clean; if (t.value !== clean) t.value = clean; break; }
      case "sbpurch": S.singlePurchaser = v; break;
      case "sbsourcer": S.singleSourcer = v; break;
      case "pval": S[arg].value = v; break;
      case "bname": S.brandOverride[arg] = { ...(S.brandOverride[arg] || {}), brand_name: v }; break;
      case "bmfr": S.brandOverride[arg] = { ...(S.brandOverride[arg] || {}), manufacturer: v }; break;
      case "bprefix": { const clean = v.toUpperCase().replace(/[^A-Z0-9&]/g, "").slice(0, 8); S.brandOverride[arg] = { ...(S.brandOverride[arg] || {}), prefix: clean }; if (t.value !== clean) t.value = clean; break; }
      case "bpurch": S.brandOverride[arg] = { ...(S.brandOverride[arg] || {}), purchaser: v }; break;
      case "bsourcer": S.brandOverride[arg] = { ...(S.brandOverride[arg] || {}), sourcer: v }; break;
      case "cell": S.cellOverride[arg] = v; break;
      case "search": S.query = v; { const b = document.getElementById("cpo-board"); if (b) b.innerHTML = boardInner(); } break;
    }
  }

  function onChange(e) {
    const t = e.target.closest("[data-ch]"); if (!t || !S.root.contains(t)) return;
    const kind = t.getAttribute("data-ch"), arg = t.getAttribute("data-arg"), v = t.value;
    switch (kind) {
      case "company": S.company = v; render(); break;
      case "colmap": S.colMap[arg] = v; render(); break;
      case "brandcol": S.brandCol = v; S.colMap[v] = "brand"; render(); loadBrands(); break;
      case "pcol": S[arg].col = v; break;
      case "headerrow": { const n = Math.max(1, parseInt(v || "1", 10) || 1); if (n !== S.headerRow) { S.headerRow = n; reparse(); } break; }
    }
  }

  function onKey(e) {
    const cell = e.target.closest('[data-in="cell"]');
    if (cell) {
      const key = cell.getAttribute("data-arg");
      if (e.key === "Enter") { e.preventDefault(); commitCell(key); }
      else if (e.key === "Escape") { S.editCell = null; render(); }   // cancel — no lookup
      return;
    }
    const sb = e.target.closest('[data-in="sbrand"]');
    if (sb && e.key === "Enter") { e.preventDefault(); if (S.singleBrand.trim()) loadBrands(); }
  }
  function onBlur(e) {
    const cell = e.target.closest('[data-in="cell"]');
    if (cell) { const key = cell.getAttribute("data-arg"); if (S.editCell === key) commitCell(key); return; }
    // typing the single brand + Tab/blur prefills its manufacturer + prefix
    const sb = e.target.closest('[data-in="sbrand"]');
    if (sb && S.brandMode === "single" && S.singleBrand.trim() && !S.brandLoading) loadBrands();
  }

  // Find a review row + its group by row id.
  function findRow(id) {
    for (const g of (S.review?.groups || [])) {
      const row = (g.rows || []).find(r => r.id === id);
      if (row) return { row, group: g };
    }
    return {};
  }

  // The note a row shows when its main is NOT on SellerCloud (new-brand / kit / none).
  function baseNote(row) {
    if (row._baseNote) return row._baseNote;
    const m = (row.kit_value || "").match(/_QY(\d+)/);
    return m ? `amz pack ${m[1]} → kit` : "";
  }

  // Commit a hand-edited SKU cell, then re-check SellerCloud for that SKU string and
  // reload the row's "on SC" flag + note (cute quick loader while it looks up).
  async function commitCell(key) {
    if (S.editCell !== key) return;   // Enter already committed → skip the blur re-fire
    S.editCell = null; render();
    const dot = key.lastIndexOf(".");
    const rowId = key.slice(0, dot), field = key.slice(dot + 1);
    if (!["main", "fba", "fbm"].includes(field)) return;
    const { row } = findRow(rowId);
    if (!row) return;
    const flag = field + "_on_sc";
    const sku = String(S.cellOverride[key] != null ? S.cellOverride[key] : (row[field] || "")).trim();
    if (!sku) {
      row[flag] = false;
      if (field === "main") { row.main_on_sc = false; row.note = baseNote(row); }
      render(); return;
    }
    S.skuChecking[key] = true; render();
    const started = Date.now();
    try {
      const j = await jpost("/api/create-po/check-sku", { token: S.token, sku, field });
      const wait = 340 - (Date.now() - started);   // keep the loader visible a beat
      if (wait > 0) await new Promise(res => setTimeout(res, wait));
      row[flag] = !!j.on_sc;
      if (field === "main") { row.main_on_sc = !!j.on_sc; row.note = j.on_sc ? (j.note || "") : baseNote(row); }
    } catch (_e) {
      /* leave the row as-is on failure */
    } finally {
      delete S.skuChecking[key];
      render();
    }
  }

  // ── actions ────────────────────────────────────────────────────────────
  function resetUpload() {
    S.token = null; S.headers = []; S.previewRows = []; S.rowCount = 0; S.colCount = 0; S.colMap = {};
    S.review = null; S.brandTable = [];
  }

  async function onFilePicked(e) { if (e.target.files[0]) handleFile(e.target.files[0]); }
  async function handleFile(file) {
    S.error = "";
    const fd = new FormData(); fd.append("catalog_file", file);
    try {
      const r = await fetch("/api/create-po/preview", { method: "POST", body: fd });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
      const j = await r.json();
      S.token = j.token; S.filename = j.filename || file.name; S.headers = j.headers;
      S.previewRows = j.rows; S.rowCount = j.row_count; S.colCount = j.col_count;
      S.sheets = j.sheets || []; S.sheet = j.active_sheet || ""; S.headerRow = j.header_row || 1;
      S.colMap = {}; S.headers.forEach(c => { S.colMap[c] = guessField(c); });
      S.brandCol = firstBrandCol();
      // Auto: if the sheet has a Brand column, use column mode so brands come
      // straight from the file (no need to type a single brand).
      S.brandMode = S.brandCol ? "column" : "single";
    } catch (err) { S.error = "Upload failed: " + err.message; }
    render();
  }

  // Re-read the uploaded file with the chosen sheet / header row (no re-upload).
  async function reparse() {
    if (!S.token) return;
    try {
      const j = await jpost("/api/create-po/reparse", { token: S.token, sheet_name: S.sheet, header_row: S.headerRow });
      S.headers = j.headers; S.previewRows = j.rows; S.rowCount = j.row_count; S.colCount = j.col_count;
      if (j.active_sheet != null) S.sheet = j.active_sheet;
      if (j.header_row != null) S.headerRow = j.header_row;
      S.colMap = {}; S.headers.forEach(c => { S.colMap[c] = guessField(c); });
      S.brandCol = firstBrandCol(); S.brandMode = S.brandCol ? "column" : "single";
    } catch (err) { S.error = "Re-parse failed: " + err.message; }
    render();
  }

  async function loadHistory() {
    try {
      const r = await fetch("/api/create-po/history");
      const j = await r.json();
      S.history = j.exports || [];
    } catch { S.history = []; }
    render();
  }

  // "Home" from Review — reset the wizard to a fresh upload, refresh history.
  function goHome() {
    resetUpload();
    S.stage = 1; S.generating = false; S.exported = false; S.exporting = false; S.exportId = "";
    S.sheets = []; S.sheet = ""; S.headerRow = 1;
    S.singleBrand = ""; S.singleMfr = ""; S.singlePrefix = ""; S.singlePurchaser = ""; S.singleSourcer = "";
    S.brandOverride = {}; S.brandConfirmed = {}; S.brandTable = []; S.review = null; S.cellOverride = {};
    S.batchType = "CPG"; S.createBy = "UPC"; S.forAmazon = true; S.create = { main: true, shadow: true, kit: true };
    S.brandMode = "single"; S.brandCol = ""; S.error = "";
    render();
    loadHistory();
  }

  function enterConfigure() {
    S.stage = 3; render();
    if (S.brandMode === "column") loadBrands();
    else if (S.singleBrand) loadBrands();
  }

  let _brandReq = 0;
  async function loadBrands() {
    S.brandLoading = true; render();
    const my = ++_brandReq;
    try {
      const body = {
        token: S.token, brand_mode: S.brandMode, company: S.company,
        brand_col: S.brandCol || firstBrandCol(), col_map: S.colMap,
        single_brand: S.singleBrand, brand_overrides: S.brandOverride,
      };
      const j = await jpost("/api/create-po/brands", body);
      if (my !== _brandReq) return;
      S.brandTable = j.brands || [];
      if (S.brandMode === "single" && S.brandTable[0]) {
        const b0 = S.brandTable[0];
        if (!S.singleMfr) S.singleMfr = b0.manufacturer || "";
        if (!S.singlePrefix) S.singlePrefix = b0.prefix || "";
        if (!S.singlePurchaser) S.singlePurchaser = b0.purchaser || "";
        if (!S.singleSourcer) S.singleSourcer = b0.sourcer || "";
      }
    } catch (err) { S.error = "Brand lookup failed: " + err.message; }
    S.brandLoading = false; render();
  }

  async function doGenerate() {
    // No progress polling / re-render loop while generating — the bar is a CSS-only
    // indeterminate animation, so the screen stays perfectly still (was flickering
    // because a setInterval re-rendered the whole view 3x/sec).
    S.error = ""; S.generating = true; render();
    try {
      const j = await jpost("/api/create-po/generate", collectConfig());
      S.review = j; S.stage = 4; S.exported = false; S.cellOverride = {}; S.skuChecking = {};
      // remember each row's non-SC note so we can restore it if an edited main
      // turns out NOT to be on SellerCloud.
      (j.groups || []).forEach(g => (g.rows || []).forEach(r => { r._baseNote = r.main_on_sc ? "" : (r.note || ""); }));
    } catch (err) { S.error = "Generate failed: " + err.message; S.stage = 3; }
    S.generating = false; render();
  }

  async function doExport() {
    if (S.exporting) return;
    S.error = "";
    S.aiTitles = document.getElementById("cpo-ai-titles")?.checked !== false;
    S.exporting = true; render();
    try {
      const r = await fetch("/api/create-po/export", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: S.token, edits: S.cellOverride, clean_titles: S.aiTitles }) });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || ("HTTP " + r.status));
      const j = await r.json();
      S.exportId = j.export_id || "";
      const files = j.files || [];
      if (!files.length) throw new Error("no files returned");
      // download each template as its own file (staggered so the browser keeps all 3)
      files.forEach((f, i) => setTimeout(() => {
        const bytes = Uint8Array.from(atob(f.b64), c => c.charCodeAt(0));
        const blob = new Blob([bytes], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob); a.download = f.name;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 1500);
      }, i * 400));
      S.exported = true; S.exporting = false; render();
      loadHistory();   // refresh so the new batch shows on the Home/upload screen
    } catch (err) { S.error = "Export failed: " + err.message; S.exporting = false; render(); }
  }

  // ── mount ────────────────────────────────────────────────────────────────
  function mount(root) {
    if (S.built && S.root === root) return;   // keep state across view switches
    S.root = root; S.built = true;
    root.addEventListener("click", onClick);
    document.addEventListener("click", onDocClick);
    root.addEventListener("input", onInput);
    root.addEventListener("change", onChange);
    root.addEventListener("keydown", onKey);
    root.addEventListener("blur", onBlur, true);
    // Keep an open menu glued to its trigger while scrolling (don't close it — that
    // was making the list "exit" when you scrolled inside it). Ignore the menu's own
    // internal scroll; only close if the trigger scrolled out of view.
    window.addEventListener("scroll", (e) => {
      if (!S.openDD) return;
      if (e.target && e.target.closest && e.target.closest("#cpo-portal")) return;
      const menu = document.getElementById("cpo-portal");
      const trig = Array.from(S.root.querySelectorAll('[data-act="dd-toggle"]')).find(b => b.getAttribute("data-arg") === S.openDD);
      if (!menu || !trig) { closeMenu(); return; }
      const r = trig.getBoundingClientRect();
      if (r.bottom < 0 || r.top > window.innerHeight) closeMenu();
      else positionMenu(menu, trig);
    }, true);
    render();
    loadHistory();   // populate the export-history list on the upload screen
  }

  window.CreatePO = { mount, refresh: render };
})();
