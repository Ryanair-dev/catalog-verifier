"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const icon = (name) => `<svg class="icon"><use href="#i-${name}"/></svg>`;

const NA = "Not available";
const fmtDate = (iso) => !iso ? NA : new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
const fmtDateTime = (iso) =>
  !iso ? NA : new Date(iso).toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const plural = (n, one, many = one + "s") => `${n.toLocaleString()} ${n === 1 ? one : many}`;

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Legacy reason kept for imported rows: new entries (Add tab) must pick a specific reason, and
// the edit dialog offers it only to an entry that already has it. It still shows in the Browse
// filter and on existing entries. Matched by name, ignoring case. The API enforces the same rule
// (LEGACY_REASON_NAME in app/main.py).
const ADD_EXCLUDED_REASON = "Do not send";
const isExcludedReason = (name) => name.trim().toLowerCase() === ADD_EXCLUDED_REASON.trim().toLowerCase();

let lookups = { channels: [], reason_codes: [], identifier_types: [] };
const LOOKUP_FIELDS = {
  channels: ["channel_id", "channel_name"],
  reason_codes: ["blacklist_reason_code_id", "reason_name"],
  identifier_types: ["identifier_type_code", "identifier_type_name"],
};

// Hint per identifier type, keyed by lower-case type name: the only place the UI depends on a
// type's name. Types not listed get the fallback, so a new row in pl_identifier_types needs no
// code change.
const TYPE_HINTS = {
  "marketplace fulfilled sku": ["SKU of a listing fulfilled by the marketplace", "MAJ904386575-FBA"],
  "merchant fulfilled sku": ["SKU of a listing fulfilled by us", "CNLF28551"],
  "listing id": ["ID of the listing on the channel: ASIN, Walmart Item ID, eBay Item ID", "B00K1JFAVE"],
};
const TYPE_HINT_FALLBACK = "Enter the identifier exactly as it appears on the channel.";
const typeHint = (name) => (name ? TYPE_HINTS[name.trim().toLowerCase()] ?? null : null);
/** The hint for a type as HTML, with its example in <code>; empty when no type is chosen. */
function typeHintHtml(name) {
  if (!name) return "";
  const known = typeHint(name);
  return known ? `${esc(known[0])}, e.g. <code translate="no">${esc(known[1])}</code>` : esc(TYPE_HINT_FALLBACK);
}
const typePlaceholder = (name) => `e.g. ${typeHint(name)?.[1] ?? "MAJ904386575-FBA"}`;
/** "Amazon - Turba" as the store's name with its marketplace under it; a name without " - " as is. */
function storeCell(name) {
  const i = name.indexOf(" - ");
  return i < 0 ? esc(name) : `${esc(name.slice(i + 3))}<div class="sub-id">${esc(name.slice(0, i))}</div>`;
}

/** How a check result matched, in words. */
function matchLabel(m) {
  if (m.matched_by === "brand") return `Brand rule ${m.identifier}`;
  if (m.matched_by === "asin") return `ASIN of ${m.identifier}`;
  return `Exact ${m.identifier_type_name}`;
}

const store = {
  get(k) { try { return localStorage.getItem(k) ?? ""; } catch { return ""; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};

// ---- TEMPORARY: dev-only mock lookups (?mock=lookups) ----------------------
// Previews the live lookups (the 8 pl_channels names, the new identifier types and reasons)
// without a database. Nothing is saved while it is on.
// To remove: delete this block, the MOCK_LOOKUPS lines in api(), submitAdd() and init(),
// and #mock-banner in index.html.

const MOCK_LOOKUPS = new URLSearchParams(location.search).get("mock") === "lookups"
  ? {
      channels: [
        "Amazon - Preferred Pharmacy Plus", "Amazon - Preferred Pharmacy Plus CA", "Amazon - Turba",
        "Amazon - Harper Wrens", "Walmart", "Temu", "Ebay", "Target",
      ].map((channel_name, i) => ({ channel_id: i + 1, channel_name })),
      identifier_types: ["Marketplace Fulfilled SKU", "Listing ID", "Merchant Fulfilled SKU"]
        .map((identifier_type_name, i) => ({ identifier_type_code: i + 1, identifier_type_name })),
      reason_codes: [
        "Do not send", "Duplicate", "Amazon restriction", "Sold on another store", "Pricing error", "Replaced listing",
        "Amazon compliance", "Quantity error", "Wrong item", "Requires transparency", "Requires invoice", "Replaced SKU",
        "Listing error", "Deactivated listing (DOG)",
      ].map((reason_name, i) => ({ blacklist_reason_code_id: i + 1, reason_name })),
    }
  : null;

/** What /api/marketplace-blacklist/bulk would return for an empty blacklist, so the result view can be previewed. */
function mockBulkAdd(body) {
  const channel = (id) => MOCK_LOOKUPS.channels.find((c) => c.channel_id === id);
  return {
    created: body.identifiers.flatMap((identifier) =>
      body.channel_ids.map((channel_id) => ({ identifier, channel_id, channel_name: channel(channel_id).channel_name }))),
    skipped_existing: [],
    errors: [],
  };
}
/** What /api/marketplace-blacklist/bulk-rows would return for an empty blacklist. */
function mockBulkRows(body) {
  const channel = (id) => MOCK_LOOKUPS.channels.find((c) => c.channel_id === id);
  return {
    results: body.rows.map((r, row) => ({
      row, status: "created", listing: { ...r, channel_name: channel(r.channel_id).channel_name },
    })),
  };
}
// ---- end TEMPORARY ---------------------------------------------------------

// ---- API -------------------------------------------------------------------

async function api(path, { method = "GET", body } = {}) {
  // TEMPORARY (MOCK_LOOKUPS): mock ids don't exist in the database, so never write with them.
  if (MOCK_LOOKUPS && method !== "GET" && path !== "/api/marketplace-blacklist/check")
    throw new Error("Saving is turned off while mock lookups are on. Remove ?mock=lookups from the address to save.");
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = data.detail ?? res.statusText;
    if (Array.isArray(msg)) msg = msg.map((d) => `${d.loc?.slice(-1)[0] ?? ""}: ${d.msg}`).join("; ");
    throw new Error(msg);
  }
  return data;
}

// ---- UI helpers ------------------------------------------------------------

function toast(msg, isError = false) {
  const el = document.createElement("div");
  el.className = `toast ${isError ? "error" : "ok"}`;
  el.innerHTML = `<span class="toast-icon">${icon(isError ? "x" : "check")}</span><span>${esc(msg)}</span>`;
  $("#toasts").append(el);
  setTimeout(() => {
    el.classList.add("out");
    el.addEventListener("animationend", () => el.remove(), { once: true });
    setTimeout(() => el.remove(), 400); // fallback when animations are off
  }, isError ? 5000 : 2800);
}

/** Show a spinner on the button while `fn` runs. */
async function withLoading(btn, fn) {
  btn.classList.add("loading");
  btn.disabled = true;
  try {
    return await fn();
  } finally {
    btn.classList.remove("loading");
    btn.disabled = false;
  }
}

function shake(el) {
  el.classList.remove("shake");
  void el.offsetWidth; // restart animation
  el.classList.add("shake");
}

function countUp(el, target) {
  if (reduceMotion || target === 0) return (el.textContent = target.toLocaleString());
  const start = performance.now();
  const dur = Math.min(900, 300 + target * 4);
  const step = (now) => {
    const t = Math.min(1, (now - start) / dur);
    el.textContent = Math.round(target * (1 - Math.pow(1 - t, 3))).toLocaleString();
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

const emptyRow = (cols, title, hint) =>
  `<tr><td colspan="${cols}" class="empty-state">${icon("inbox")}<strong>${esc(title)}</strong>${esc(hint ?? "")}</td></tr>`;

const skeletonRows = (cols, n = 6) =>
  Array.from({ length: n }, () =>
    `<tr class="skeleton">${Array.from({ length: cols }, (_, c) => `<td><span style="--w:${40 + ((c * 37 + n * 13) % 50)}%"></span></td>`).join("")}</tr>`
  ).join("");

/** Split pasted text on newlines, commas, tabs or semicolons; trim; drop blanks. */
function parseIdentifiers(text) {
  return text
    .split(/[\r\n,\t;]+/)
    .map((s) => s.trim().replace(/^"(.*)"$/, "$1").trim())
    .filter(Boolean);
}

const HEADER_WORDS = new Set(["identifier", "sku", "asin", "upc", "item id", "item_id", "id"]);

/** First column of each CSV/TXT row, skipping an obvious header row. */
function parseFile(text) {
  const rows = text.split(/\r?\n/).map((line) => line.split(/[,\t;]/)[0].trim().replace(/^"(.*)"$/, "$1").trim());
  if (rows.length && HEADER_WORDS.has(rows[0].toLowerCase())) rows.shift();
  return rows.filter(Boolean);
}

const intOrNull = (v) => (v === "" || v == null ? null : Number(v));

// ---- Lookups / selects -----------------------------------------------------

function fillSelects() {
  for (const sel of $$("select[data-lookup]")) {
    const [idKey, nameKey] = LOOKUP_FIELDS[sel.dataset.lookup];
    const any = sel.dataset.any;
    sel.innerHTML =
      (any ? `<option value="">${esc(any)}</option>` : `<option value="" disabled selected>Select…</option>`) +
      lookups[sel.dataset.lookup].map((o) => `<option value="${o[idKey]}">${esc(o[nameKey])}</option>`).join("");
  }
  for (const sel of $$("select")) enhanceSelect(sel);
  for (const sel of $$("select[data-type-select]")) {
    sel.addEventListener("change", () => syncTypeFields(sel.form));
    syncTypeFields(sel.form);
  }
}

/** Show the hint for the chosen identifier type (by its name, with a generic fallback). */
function syncTypeFields(form) {
  const opt = form.elements.identifier_type_code.selectedOptions[0];
  const name = opt?.value ? opt.text : "";
  const hint = $("[data-type-hint]", form);
  if (hint) hint.innerHTML = typeHintHtml(name);
  form.elements.identifier.placeholder = typePlaceholder(name);
}

/** Edit dialog: offer the legacy reason (ADD_EXCLUDED_REASON) only to an entry that already has it. */
function syncLegacyReasonOption(sel, currentReasonId) {
  const index = lookups.reason_codes.findIndex((r) => isExcludedReason(r.reason_name));
  if (index < 0) return;
  const legacy = lookups.reason_codes[index];
  const value = String(legacy.blacklist_reason_code_id);
  const opt = [...sel.options].find((o) => o.value === value);
  if (currentReasonId === legacy.blacklist_reason_code_id) {
    if (!opt) sel.add(new Option(legacy.reason_name, value), index + 1); // +1: the "Select…" placeholder
  } else opt?.remove();
}

// ---- Custom dropdowns ------------------------------------------------------
// Each native <select> stays in the DOM (hidden) as the source of truth, so forms,
// FormData, `required` validation and change listeners keep working unchanged.

const SELECT_ICON = { channels: "store", reason_codes: "tag", identifier_types: "hash" };
const nativeValue = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
let openSelect = null; // { sel, btn, menu, items, index }

function enhanceSelect(sel) {
  const wrap = document.createElement("div");
  wrap.className = "select";
  sel.before(wrap);
  wrap.append(sel);
  sel.tabIndex = -1;
  sel.setAttribute("aria-hidden", "true");

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "select-btn";
  btn.setAttribute("aria-haspopup", "listbox");
  btn.setAttribute("aria-expanded", "false");
  btn.innerHTML = `${icon(SELECT_ICON[sel.dataset.lookup] ?? "list")}<span class="select-label"></span>` +
    `<svg class="icon chev"><use href="#i-chevron"/></svg>`;
  wrap.append(btn);

  const field = sel.closest(".field");
  const name = sel.getAttribute("aria-label") ?? (field && $(".field-label", field)?.firstChild?.textContent.trim());
  const refresh = () => {
    const opt = sel.selectedOptions[0];
    const empty = !opt || (opt.disabled && opt.value === "");
    $(".select-label", btn).textContent = opt?.text ?? "Select…";
    btn.title = empty ? "" : opt.text;
    btn.classList.toggle("placeholder", empty);
    wrap.classList.toggle("is-set", sel.dataset.any !== undefined && sel.value !== "");
    if (!empty) wrap.classList.remove("invalid");
    btn.setAttribute("aria-label", `${name ? name + ": " : ""}${opt?.text ?? "none selected"}`);
  };
  // Programmatic `sel.value = …` (edit dialog, defaults) must update the button too.
  Object.defineProperty(sel, "value", {
    configurable: true,
    get() { return nativeValue.get.call(this); },
    set(v) { nativeValue.set.call(this, v); refresh(); },
  });
  sel.addEventListener("change", refresh);
  sel.addEventListener("invalid", () => { wrap.classList.add("invalid"); shake(wrap); });
  if (sel.id) {
    const label = $(`label[for="${sel.id}"]`);
    if (label) { label.htmlFor = ""; label.addEventListener("click", () => btn.focus()); }
  }

  btn.addEventListener("click", () => (openSelect?.sel === sel ? closeSelect() : openSelectMenu(sel, btn)));
  btn.addEventListener("keydown", (e) => {
    if (openSelect?.sel === sel) return; // handled by the menu's key handler
    if (["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) {
      e.preventDefault();
      openSelectMenu(sel, btn);
    }
  });
  refresh();
}

function openSelectMenu(sel, btn) {
  closeSelect();
  const options = [...sel.options].filter((o) => !(o.disabled && o.value === ""));
  const menu = document.createElement("div");
  menu.className = "select-menu";
  menu.setAttribute("role", "listbox");
  const uid = Math.random().toString(36).slice(2, 8);
  menu.innerHTML = options
    .map((o, i) => `<div role="option" id="opt-${uid}-${i}" data-i="${i}"
        class="select-option${o.value === "" ? " any" : ""}" aria-selected="${o.selected}">
        <span>${esc(o.text)}</span>${icon("check")}</div>`)
    .join("");
  (btn.closest("dialog") ?? document.body).append(menu);

  // Position under the button, flipping up if there isn't room below.
  const r = btn.getBoundingClientRect();
  menu.style.minWidth = `${r.width}px`;
  const h = menu.offsetHeight;
  const below = window.innerHeight - r.bottom;
  const up = below < h + 12 && r.top > below;
  menu.classList.toggle("up", up);
  menu.style.top = `${up ? r.top - h - 6 : r.bottom + 6}px`;
  menu.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8))}px`;

  const items = $$(".select-option", menu);
  openSelect = { sel, btn, menu, items, options, index: Math.max(0, options.findIndex((o) => o.selected)) };
  btn.setAttribute("aria-expanded", "true");
  btn.setAttribute("aria-controls", menu.id = `menu-${uid}`);
  setActiveOption(openSelect.index);

  menu.addEventListener("pointermove", (e) => {
    const item = e.target.closest(".select-option");
    if (item) setActiveOption(Number(item.dataset.i), false);
  });
  menu.addEventListener("click", (e) => {
    const item = e.target.closest(".select-option");
    if (item) chooseOption(Number(item.dataset.i));
  });
}

function setActiveOption(i, scroll = true) {
  const s = openSelect;
  if (!s) return;
  s.index = Math.max(0, Math.min(i, s.items.length - 1));
  s.items.forEach((el, n) => el.classList.toggle("active", n === s.index));
  s.btn.setAttribute("aria-activedescendant", s.items[s.index]?.id ?? "");
  if (scroll) s.items[s.index]?.scrollIntoView({ block: "nearest" });
}

function chooseOption(i) {
  const { sel, btn, options } = openSelect;
  const changed = sel.value !== options[i].value;
  sel.value = options[i].value;
  closeSelect();
  btn.focus();
  if (changed) sel.dispatchEvent(new Event("change", { bubbles: true }));
}

function closeSelect() {
  if (!openSelect) return;
  const { btn, menu } = openSelect;
  openSelect = null;
  btn.setAttribute("aria-expanded", "false");
  btn.removeAttribute("aria-activedescendant");
  menu.classList.add("closing");
  setTimeout(() => menu.remove(), reduceMotion ? 0 : 120);
}

document.addEventListener("keydown", (e) => {
  const s = openSelect;
  if (!s) return;
  const moves = { ArrowDown: s.index + 1, ArrowUp: s.index - 1, Home: 0, End: s.items.length - 1,
                  PageDown: s.index + 6, PageUp: s.index - 6 };
  if (e.key in moves) {
    e.preventDefault();
    setActiveOption(moves[e.key]);
  } else if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    chooseOption(s.index);
  } else if (e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation(); // don't also close the edit dialog
    closeSelect();
    s.btn.focus();
  } else if (e.key === "Tab") {
    closeSelect();
  } else if (e.key.length === 1 && /\S/.test(e.key)) {
    // Type-ahead: jump to the next option starting with the typed letter.
    e.preventDefault();
    e.stopPropagation();
    const k = e.key.toLowerCase();
    const n = s.options.length;
    for (let step = 1; step <= n; step++) {
      const i = (s.index + step) % n;
      if (s.options[i].text.toLowerCase().startsWith(k)) { setActiveOption(i); break; }
    }
  }
}, true);
document.addEventListener("pointerdown", (e) => {
  if (openSelect && !openSelect.menu.contains(e.target) && !openSelect.btn.contains(e.target)) closeSelect();
});
window.addEventListener("resize", closeSelect);
document.addEventListener("scroll", (e) => { if (openSelect && !openSelect.menu.contains(e.target)) closeSelect(); }, true);

// ---- Tabs ------------------------------------------------------------------

function moveThumb() {
  const active = $(".tab.active");
  const thumb = $(".tab-thumb");
  if (!active || !thumb) return;
  thumb.style.width = `${active.offsetWidth}px`;
  thumb.style.transform = `translateX(${active.offsetLeft}px)`;
}

function showTab(name) {
  for (const t of $$(".tab")) {
    const active = t.dataset.tab === name;
    t.classList.toggle("active", active);
    t.setAttribute("aria-selected", active);
  }
  for (const p of $$(".panel")) p.hidden = p.id !== `tab-${name}`;
  history.replaceState(null, "", tabHash(name));
  moveThumb();
  if (name === "browse") loadBrowse();
  const focusTarget = { browse: browseFocusTarget(), add: addFocusTarget() }[name];
  if (focusTarget && window.matchMedia("(hover: hover)").matches) $(focusTarget).focus({ preventScroll: true });
}

/** The address for a tab. The Add tab's hash also says which mode; Browse in Check list mode is #bulk. */
function tabHash(name) {
  if (name === "add") return `#${ADD_MODE_HASH[add.mode]}`;
  return browseChecking ? "#bulk" : location.pathname + location.search;
}

// ---- Browse: Check list mode -----------------------------------------------
// The Check list button in the Browse header swaps the table for the list check (#check-pane,
// code under "Bulk check" below) and back, without leaving the tab.

let browseChecking = false;
const browseFocusTarget = () => (browseChecking ? "#bulk-input" : "#browse-q");

function setBrowseChecking(on, { focus = false } = {}) {
  browseChecking = on;
  $("#browse-pane").hidden = on;
  $("#check-pane").hidden = !on;
  $("#browse-export").hidden = on; // it exports the whole blacklist; the check has its own Download CSV
  $("#browse-sub").hidden = on;
  $("#check-sub").hidden = !on;
  const btn = $("#browse-mode");
  btn.setAttribute("aria-pressed", on);
  btn.classList.toggle("primary", !on);
  btn.innerHTML = on ? `${icon("list")}<span>Back to Manual Search</span>` : `${icon("layers")}<span>Check List</span>`;
  if (currentTab() === "browse") history.replaceState(null, "", tabHash("browse"));
  if (focus && window.matchMedia("(hover: hover)").matches) $(browseFocusTarget()).focus({ preventScroll: true });
}

const currentTab = () => $(".tab.active")?.dataset.tab;

// ---- Check (on the Browse tab) ---------------------------------------------
// The table only finds rows containing the typed text. Check applies the matching
// rules too (brand prefixes, amzn.gr SKUs, ASIN review), so a SKU blocked by a brand
// rule isn't mistaken for clear.

function matchCard(m, i) {
  const meta = `Date added: ${fmtDate(m.created_at)}<br>Added by: ${esc(m.created_by || NA)}`;
  return `<div class="match${m.matched_by === "asin" ? " soft" : ""}" style="--i:${i}">
      <div>
        <div class="match-channel">${esc(m.channel_name)}</div>
        <div class="match-how ${m.matched_by}">${esc(matchLabel(m))}</div>
        <div class="match-meta">${meta}</div>
      </div>
      <div>
        <div class="match-reason">${esc(m.reason_name)}</div>
        ${m.blacklist_notes ? `<div class="match-notes">${esc(m.blacklist_notes)}</div>` : ""}
      </div>
    </div>`;
}

async function browseCheck(e) {
  e?.preventDefault();
  const identifier = $("#browse-q").value.trim();
  const out = $("#browse-verdict");
  if (!identifier) {
    out.innerHTML = "";
    return shake($("#browse-q").closest(".search-field"));
  }
  await withLoading($("#browse-check"), async () => {
    try {
      const res = await api("/api/marketplace-blacklist/check", {
        method: "POST",
        body: { identifiers: [identifier], channel_id: intOrNull($("#browse-channel").value) },
      });
      if ($("#browse-q").value.trim() !== identifier) return; // the text changed while checking
      const r = res.results[0];
      const scope = $("#browse-channel").value ? $("#browse-channel").selectedOptions[0].text : "any store";
      const id = `<span class="verdict-id">${esc(r.identifier)}</span>`;
      const hard = r.matches.filter((m) => m.matched_by !== "asin");
      const stores = [...new Set(hard.map((m) => m.channel_name))];
      const head = {
        blacklisted: ["bad", "ban", `Blacklisted on ${plural(stores.length, "store")}`,
          `Don't buy ${id} for, or send it to, ${esc(stores.join(", "))}.`],
        review: ["review", "alert", "Needs review",
          `${id} is the ASIN of ${plural(r.matches.length, "blacklisted SKU")}. Another SKU may still sell it, so check before sending.`],
        clear: ["good", "check", "All clear", `${id} isn't blacklisted on ${esc(scope)}.`],
      }[r.status];
      // The table may still list rows that merely contain the text (MMM2864 → MMM2864-FBA3CA).
      if (r.status === "clear" && browse.total > 0)
        head[3] += ` The ${plural(browse.total, "row")} below only contain${browse.total === 1 ? "s" : ""} this text.`;
      out.innerHTML = `<div class="verdict ${head[0]}">
          <div class="verdict-head">
            <span class="verdict-icon">${icon(head[1])}</span>
            <div><h3>${head[2]}</h3><p>${head[3]}</p></div>
            <button type="button" class="icon-btn verdict-close" data-close-verdict aria-label="Close the check result">${icon("x")}</button>
          </div>
          ${r.matches.length ? `<div class="match-list">${r.matches.map(matchCard).join("")}</div>` : ""}
        </div>`;
    } catch (err) {
      toast(err.message, true);
    }
  });
}

// ---- Bulk check ------------------------------------------------------------

let bulkResults = [];

function updateBulkCount() {
  const n = new Set(parseIdentifiers($("#bulk-input").value).map((s) => s.toLowerCase())).size;
  $("#bulk-count").textContent = plural(n, "unique identifier");
}

async function bulkCheck(e) {
  e.preventDefault();
  const identifiers = parseIdentifiers($("#bulk-input").value);
  if (!identifiers.length) {
    shake($("#bulk-input"));
    return toast("Paste or drop some identifiers first", true);
  }
  if (identifiers.length > 5000) return toast("Max 5,000 identifiers per check", true);
  await withLoading(e.submitter ?? $("#bulk-form .btn.primary"), async () => {
    try {
      const res = await api("/api/marketplace-blacklist/check", {
        method: "POST",
        body: {
          identifiers,
          channel_id: intOrNull($("#bulk-channel").value),
          identifier_type_code: intOrNull($("#bulk-type").value),
        },
      });
      bulkResults = res.results;
      const box = $("#bulk-results");
      box.hidden = true;
      void box.offsetWidth; // replay entrance animation on re-check
      box.hidden = false;
      countUp($("#stat-checked"), res.total_checked);
      countUp($("#stat-bad"), res.total_blacklisted);
      countUp($("#stat-review"), res.total_review);
      const clear = res.total_checked - res.total_blacklisted - res.total_review;
      countUp($("#stat-good"), clear);
      setStatBars({ checked: res.total_checked, bad: res.total_blacklisted, review: res.total_review, good: clear },
                  res.total_checked);
      renderBulk();
      box.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    } catch (err) {
      toast(err.message, true);
    }
  });
}

function setStatBars(counts, total) {
  for (const [key, n] of Object.entries(counts)) {
    const pct = total ? Math.round((n / total) * 100) : 0;
    const bar = $(`[data-bar="${key}"]`);
    bar.style.width = "0";
    requestAnimationFrame(() => requestAnimationFrame(() => (bar.style.width = `${key === "checked" ? 100 : pct}%`)));
    $(`[data-foot="${key}"]`).innerHTML = key === "checked"
      ? `${plural(total, "unique identifier")}`
      : `<b>${pct}%</b> of checked`;
  }
}

function renderBulk() {
  const onlyBad = $("#bulk-only-bad").checked;
  const rows = bulkResults.filter((r) => !onlyBad || r.status !== "clear");
  const dash = '<span class="muted">—</span>';
  $("#bulk-tbody").innerHTML = rows.length
    ? rows
        .map((r, i) => {
          const uniq = (f) => [...new Set(r.matches.map(f))];
          return `<tr class="row-in" style="--i:${i}">
            <td><div class="id-cell">${STATUS_TILE[r.status]}<span class="mono">${esc(r.identifier)}</span></div></td>
            <td>${STATUS_PILL[r.status]}</td>
            <td>${uniq((m) => m.channel_name).map((c) => `<span class="pill neutral">${esc(c)}</span>`).join("") || dash}</td>
            <td class="nowrap">${uniq(matchLabel).map(esc).join("<br>") || dash}</td>
            <td>${uniq((m) => m.reason_name).map(esc).join("<br>") || dash}</td>
            <td class="notes">${r.matches.map((m) => `<div><strong>${esc(m.channel_name)}:</strong> ${esc(m.blacklist_notes)}</div>`).join("")}</td>
          </tr>`;
        })
        .join("")
    : emptyRow(6, "Nothing blacklisted", "Every identifier in this list is clear.");
}

const STATUS_TILE = {
  blacklisted: `<span class="type-tile bad">${icon("ban")}</span>`,
  review: `<span class="type-tile review">${icon("alert")}</span>`,
  clear: `<span class="type-tile clear">${icon("check")}</span>`,
};
const STATUS_PILL = {
  blacklisted: `<span class="pill bad">${icon("ban")}Blacklisted</span>`,
  review: `<span class="pill warn">${icon("alert")}Review</span>`,
  clear: `<span class="pill good">${icon("check")}Clear</span>`,
};

function csvCell(v) {
  const s = String(v ?? "");
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function exportBulk() {
  const lines = [["identifier", "status", "store", "matched_on", "blacklisted_identifier", "asin", "reason", "notes", "added_by"]];
  for (const r of bulkResults) {
    if (!r.matches.length) lines.push([r.identifier, "clear"]);
    for (const m of r.matches)
      lines.push([
        r.identifier, m.matched_by === "asin" ? "review" : "blacklisted", m.channel_name, matchLabel(m),
        m.identifier, m.asin, m.reason_name, m.blacklist_notes, m.created_by || NA,
      ]);
  }
  const blob = new Blob([lines.map((l) => l.map(csvCell).join(",")).join("\r\n")], { type: "text/csv" });
  const a = Object.assign(document.createElement("a"), {
    href: URL.createObjectURL(blob),
    download: `blacklist_check_${new Date().toISOString().slice(0, 10)}.csv`,
  });
  a.click();
  URL.revokeObjectURL(a.href);
}

async function loadFile(file) {
  if (!file) return;
  const ids = parseFile(await file.text());
  $("#bulk-input").value = ids.join("\n");
  updateBulkCount();
  toast(`Loaded ${plural(ids.length, "identifier")} from ${file.name}`);
}

function wireDropzone() {
  const dz = $("#dropzone");
  const input = $("#bulk-file");
  input.addEventListener("change", () => {
    loadFile(input.files[0]);
    input.value = "";
  });
  let depth = 0;
  dz.addEventListener("dragenter", (e) => { e.preventDefault(); depth++; dz.classList.add("drag"); });
  dz.addEventListener("dragover", (e) => e.preventDefault());
  dz.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; dz.classList.remove("drag"); } });
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    depth = 0;
    dz.classList.remove("drag");
    loadFile(e.dataTransfer.files[0]);
  });
}

// ---- Browse ----------------------------------------------------------------

const browse = { page: 1, pageSize: 25, total: 0, items: [], loadedOnce: false };

function browseParams() {
  const p = new URLSearchParams();
  const q = $("#browse-q").value.trim();
  if (q) p.set("q", q);
  for (const [id, key] of [
    ["#browse-channel", "channel_id"],
    ["#browse-reason", "blacklist_reason_code_id"],
    ["#browse-type", "identifier_type_code"],
  ]) {
    if ($(id).value) p.set(key, $(id).value);
  }
  return p;
}

async function loadBrowse() {
  const p = browseParams();
  const filtered = [...p.keys()].length > 0;
  $("#browse-export").href = `/api/marketplace-blacklist/export.csv?${p}`;
  p.set("page", browse.page);
  p.set("page_size", browse.pageSize);
  if (!browse.loadedOnce) $("#browse-tbody").innerHTML = skeletonRows(6);
  try {
    const res = await api(`/api/marketplace-blacklist?${p}`);
    browse.loadedOnce = true;
    Object.assign(browse, { total: res.total, items: res.items });
    $("#browse-tbody").innerHTML = res.items.length
      ? res.items
          .map(
            (i, n) => `<tr class="row-in" style="--i:${n}">
              <td class="mono">${esc(i.identifier)}${i.asin ? `<div class="sub-id">ASIN ${esc(i.asin)}</div>` : ""}</td>
              <td class="nowrap">${esc(i.identifier_type_name)}</td>
              <td class="nowrap">${storeCell(i.channel_name)}</td>
              <td class="nowrap">${esc(i.reason_name)}</td>
              <td class="notes">${esc(i.blacklist_notes)}</td>
              <td class="muted nowrap" title="Updated ${esc(fmtDateTime(i.updated_at))}">${esc(fmtDate(i.created_at))}<div class="sub-id">by ${esc(i.created_by || NA)}</div></td>
            </tr>`
          )
          .join("")
      : filtered
        ? emptyRow(6, "No matches", "Try a different search or clear the filters.")
        : emptyRow(6, "The blacklist is empty", "Add entries from the Add tab.");
    const start = res.total ? (browse.page - 1) * browse.pageSize + 1 : 0;
    const end = Math.min(browse.page * browse.pageSize, res.total);
    $("#browse-info").textContent = res.total ? `Showing ${start.toLocaleString()}–${end.toLocaleString()} of ${res.total.toLocaleString()}` : "";
    $("#browse-sub").textContent = filtered
      ? `${plural(res.total, "entry", "entries")} match your filters.`
      : `${plural(res.total, "entry", "entries")} across all stores.`;
    $("#browse-prev").disabled = browse.page <= 1;
    $("#browse-next").disabled = end >= res.total;
  } catch (err) {
    toast(err.message, true);
  }
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

const resetAndLoadBrowse = () => {
  browse.page = 1;
  loadBrowse();
};

// ---- Edit dialog -----------------------------------------------------------
// Not reachable for now: the Edit button was taken off the Browse rows until sign-in lands and
// editing is limited to admins (see the add-sign-in branch). To bring it back, add a button with
// data-edit="<blacklist_id>" to a Browse row; the click handler in init() opens this dialog.

function updateCounters() {
  for (const c of $$(".counter[data-for]")) {
    const ta = document.getElementById(c.dataset.for);
    c.textContent = `${ta.value.length} / ${ta.maxLength}`;
  }
}

function openEdit(id) {
  const item = browse.items.find((i) => i.blacklist_id === id);
  if (!item) return;
  const f = $("#edit-form");
  syncLegacyReasonOption(f.elements.blacklist_reason_code_id, item.blacklist_reason_code_id);
  for (const name of ["blacklist_id", "identifier", "identifier_type_code", "channel_id", "blacklist_reason_code_id",
                      "blacklist_notes", "asin", "created_by"])
    f.elements[name].value = item[name] ?? "";
  syncTypeFields(f);
  // ASIN exists only in the in-memory import (the live table has no ASIN column).
  $("[data-asin-field]", f).hidden = !item.asin;
  $("#edit-meta").textContent = `Added ${fmtDateTime(item.created_at)} by ${item.created_by || NA} · Last updated ${fmtDateTime(item.updated_at)}`;
  updateCounters();
  const dlg = $("#edit-dialog");
  dlg.classList.remove("closing");
  dlg.showModal();
  f.elements.identifier.focus();
}

function closeEdit() {
  const dlg = $("#edit-dialog");
  if (!dlg.open || dlg.classList.contains("closing")) return;
  if (reduceMotion) return dlg.close();
  dlg.classList.add("closing");
  setTimeout(() => {
    dlg.close();
    dlg.classList.remove("closing");
  }, 170);
}

function formPayload(form, intFields) {
  const data = Object.fromEntries(new FormData(form));
  for (const k of intFields) data[k] = Number(data[k]);
  return data;
}

async function saveEdit(e) {
  e.preventDefault();
  const f = e.target;
  const { blacklist_id, ...body } = formPayload(f, ["blacklist_id", "identifier_type_code", "channel_id", "blacklist_reason_code_id"]);
  await withLoading(e.submitter ?? $("#edit-form .btn.primary"), async () => {
    try {
      await api(`/api/marketplace-blacklist/${blacklist_id}`, { method: "PATCH", body });
      closeEdit();
      toast("Changes saved");
      loadBrowse();
    } catch (err) {
      shake(f);
      toast(err.message, true);
    }
  });
}

async function deleteEntry() {
  const f = $("#edit-form");
  const id = f.elements.blacklist_id.value;
  const label = `${f.elements.identifier.value} on ${f.elements.channel_id.selectedOptions[0].text}`;
  if (!confirm(`Remove ${label} from the blacklist?`)) return;
  await withLoading($("#edit-delete"), async () => {
    try {
      await api(`/api/marketplace-blacklist/${id}`, { method: "DELETE" });
      closeEdit();
      toast(`Removed ${label}`);
      loadBrowse();
    } catch (err) {
      toast(err.message, true);
    }
  });
}

// ---- Add -------------------------------------------------------------------
// Three modes on one tab: add one identifier, add a list, or check a list (the old Bulk
// tab, whose code lives in "Bulk check" above). Both add modes post to /api/marketplace-blacklist/bulk,
// which skips entries that already exist instead of overwriting them.

const IDENTIFIER_MAX = 64; // blacklist_listings.identifier is NVARCHAR(64)
const BULK_MAX = 5000;     // BULK_MAX in app/models.py
const ADD_MODE_HASH = { one: "add", many: "add-many" };

const add = {
  mode: "one",
  ids: { total: 0, unique: [], duplicates: 0, tooLong: [] },
  combos: {},         // type, store, reason
  submitted: false,   // after the first submit, every field shows its error live
  touched: new Set(), // fields left at least once; only "invalid" (not "required") errors show before submit
  busy: false,
  saved: false,       // what's in the identifier field was submitted; no unsaved-changes warning
};

/**
 * One identifier per line. A row pasted from several Excel columns keeps only its first cell.
 * Then as the server does it: trim, collapse inner whitespace, dedupe case-insensitively.
 */
function analyzeIdentifiers(text) {
  const seen = new Set();
  const unique = [];
  const tooLong = [];
  let total = 0;
  for (const line of text.split(/\r?\n/)) {
    const value = line.split("\t")[0].trim().replace(/^"(.*)"$/, "$1").trim().replace(/\s+/g, " ");
    if (!value) continue;
    total++;
    const key = value.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(value);
    if (value.length > IDENTIFIER_MAX) tooLong.push(value);
  }
  return { total, unique, duplicates: total - unique.length, tooLong };
}

function channelLabel(id) {
  return lookups.channels.find((x) => x.channel_id === id)?.channel_name ?? `Store ${id}`;
}

/**
 * Store options from pl_channels.channel_name alone. "Amazon - Turba" splits at the first " - ":
 * group "Amazon", option "Turba". A name without " - " ("Walmart") is its own group, shown as a
 * single option without a heading. Each option stays bound to its own channel_id, and the field
 * shows the full name once picked.
 */
function storeItems() {
  const items = lookups.channels.map((c) => {
    const i = c.channel_name.indexOf(" - ");
    return i < 0
      ? { id: c.channel_id, name: c.channel_name, label: c.channel_name, section: c.channel_name }
      : { id: c.channel_id, name: c.channel_name.slice(i + 3).trim(), label: c.channel_name,
          group: c.channel_name.slice(0, i).trim(), section: c.channel_name.slice(0, i).trim() };
  });
  // Keep each group together, in the order its first channel appears.
  const order = [...new Set(items.map((it) => it.section))];
  return items.sort((a, b) => order.indexOf(a.section) - order.indexOf(b.section));
}

// ---- Searchable dropdown (combobox): type to filter, arrows + Enter to pick, Esc to close.
// Leaving the field keeps an exact name typed out, restores the last pick for other text,
// and clears the pick when the box is emptied.

function makeCombo(root, items, { key, noun }) {
  const input = $('input[role="combobox"]', root);
  const list = $(".combo-list", root);
  let matches = [];
  let active = -1;
  let value = null;     // the current pick; typing clears it
  let committed = null; // the last real pick, restored on blur
  const label = (it) => it.label ?? it.name;

  const highlight = (text, q) => {
    const i = q ? text.toLowerCase().indexOf(q) : -1;
    return i < 0 ? esc(text) : `${esc(text.slice(0, i))}<mark>${esc(text.slice(i, i + q.length))}</mark>${esc(text.slice(i + q.length))}`;
  };
  function setActive(i) {
    active = i;
    const opts = $$(".combo-option", list);
    opts.forEach((el, n) => el.classList.toggle("active", n === i));
    if (i >= 0) {
      input.setAttribute("aria-activedescendant", opts[i].id);
      opts[i].scrollIntoView({ block: "nearest" });
    } else input.removeAttribute("aria-activedescendant");
  }
  function render() {
    const typed = value && input.value === label(value) ? "" : input.value.trim();
    const q = typed.toLowerCase();
    matches = q ? items.filter((it) => `${it.name} ${label(it)}`.toLowerCase().includes(q)) : items;
    // Sectioned lists (stores): a heading per group, a plain divider before a stand-alone option.
    let section = null;
    list.innerHTML = matches.length
      ? matches.map((it, i) => {
          let head = "";
          if (it.section !== undefined && it.section !== section) {
            head = it.group ? `<div class="combo-group" role="presentation">${esc(it.group)}</div>`
              : i > 0 ? `<div class="combo-divider" role="presentation"></div>` : "";
            section = it.section;
          }
          return `${head}<div role="option" id="${input.id}-opt-${i}" class="combo-option${it.group ? " in-group" : ""}" ` +
            `aria-selected="${it.id === value?.id}"${it.group ? ` aria-label="${esc(label(it))}"` : ""}>` +
            `<span>${highlight(it.name, q)}</span>${icon("check")}</div>`;
        }).join("")
      : `<div class="combo-empty">No ${noun}s match “${esc(typed)}”. Clear the search to see all ${items.length}.</div>`;
    setActive(!matches.length ? -1 : q ? 0 : Math.max(0, matches.findIndex((it) => it.id === value?.id)));
  }
  const isOpen = () => !list.hidden;
  function open() {
    if (!items.length) return;
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    render();
  }
  function close() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }
  function choose(it, { silent = false } = {}) {
    value = committed = it;
    input.value = it ? label(it) : "";
    close();
    if (!silent) input.dispatchEvent(new CustomEvent("combo-change", { bubbles: true, detail: { key, value: it } }));
  }

  input.addEventListener("input", () => {
    value = null;
    open();
  });
  input.addEventListener("click", () => (isOpen() ? close() : open()));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) return; // Ctrl+Enter submits the form
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!isOpen()) return open();
      if (matches.length) setActive((active + (e.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length);
    } else if (e.key === "Enter") {
      e.preventDefault(); // never submit the form from here
      if (isOpen() && active >= 0) choose(matches[active]);
    } else if (e.key === "Escape" && isOpen()) {
      e.preventDefault();
      close();
    }
  });
  input.addEventListener("blur", () => {
    close();
    const text = input.value.trim().toLowerCase();
    const exact = items.find((it) => it.name.toLowerCase() === text || label(it).toLowerCase() === text);
    if (exact) choose(exact);
    else if (text && committed) choose(committed);
    else if (!text) committed = null;
    add.touched.add(key);
    refreshAdd();
  });
  list.addEventListener("pointerdown", (e) => e.preventDefault()); // keep focus in the input
  list.addEventListener("click", (e) => {
    const opt = e.target.closest(".combo-option");
    if (opt) choose(matches[$$(".combo-option", list).indexOf(opt)]);
  });
  const toggle = $(".combo-toggle", root);
  toggle.addEventListener("pointerdown", (e) => e.preventDefault());
  toggle.addEventListener("click", () => {
    input.focus();
    isOpen() ? close() : open();
  });

  return {
    input,
    items,
    get value() { return value; },
    set: (it) => choose(it, { silent: true }),
  };
}

function syncTypeHint() {
  const name = add.combos.type.value?.name;
  $("#add-type-hint").innerHTML = typeHintHtml(name);
  $("#add-one").placeholder = typePlaceholder(name);
}

// ---- Modes

function addFocusTarget() {
  return { one: "#add-one", many: "#add-grid tbody input" }[add.mode];
}

function setAddMode(mode, { focus = false } = {}) {
  add.mode = mode;
  $(`.mode-switch input[value="${mode}"]`).checked = true;
  for (const el of $$("[data-mode-only]")) el.hidden = el.dataset.modeOnly !== mode;
  $("#add-result").hidden = true;
  if (currentTab() === "add") history.replaceState(null, "", `#${ADD_MODE_HASH[mode]}`);
  if (focus && window.matchMedia("(hover: hover)").matches) $(addFocusTarget()).focus({ preventScroll: true });
  refreshAdd();
}

// ---- Validation

/** Every field's problem, if any. kind "required" waits for a submit; "invalid" shows once the field is left. */
function addProblems() {
  const p = {};
  const initialsMissing = !$("#add-initials").value.trim();
  if (add.mode === "many") { // the grid checks its own rows (gridProblems)
    if (initialsMissing) p.initials = { kind: "required", msg: "Enter your initials, up to 4 characters." };
    return p;
  }
  const { unique, tooLong } = add.ids;
  if (!unique.length) p.identifiers = { kind: "required", msg: add.mode === "one" ? "Enter an identifier." : "Paste at least one identifier." };
  else if (tooLong.length) {
    const one = tooLong.length === 1;
    p.identifiers = {
      kind: "invalid",
      msg: add.mode === "one"
        ? `This is longer than ${IDENTIFIER_MAX} characters. Shorten it.`
        : `${plural(tooLong.length, "identifier")} ${one ? "is" : "are"} longer than ${IDENTIFIER_MAX} characters ` +
          `(${tooLong.slice(0, 2).map((v) => `${v.slice(0, 24)}…`).join(", ")}). Shorten or remove ${one ? "it" : "them"}.`,
    };
  } else if (unique.length > BULK_MAX)
    p.identifiers = {
      kind: "invalid",
      msg: `That’s ${unique.length.toLocaleString()} identifiers; the limit is ${BULK_MAX.toLocaleString()} at a time. Split the list and add it in parts.`,
    };
  const pick = (key, noun, extra) => {
    if (add.combos[key].value) return;
    const text = add.combos[key].input.value.trim();
    p[key] = !text ? { kind: "required", msg: `Choose ${noun}.` } : { kind: "invalid", msg: extra?.(text) ?? `Pick ${noun} from the list.` };
  };
  pick("type", "an identifier type");
  pick("store", "a store");
  pick("reason", "a reason", (text) => isExcludedReason(text)
    ? `“${ADD_EXCLUDED_REASON}” is only for imported entries. Pick a specific reason from the list.` : null);
  if (initialsMissing) p.initials = { kind: "required", msg: "Enter your initials, up to 4 characters." };
  return p;
}

const ADD_FIELDS = {
  identifiers: { error: "#add-ids-error", control: () => $("#add-one") },
  type: { error: "#add-type-error", control: () => $("#add-type") },
  store: { error: "#add-store-error", control: () => $("#add-store") },
  reason: { error: "#add-reason-error", control: () => $("#add-reason") },
  initials: { error: "#add-initials-error", control: () => $("#add-initials") },
};

function renderAddErrors(problems) {
  for (const [key, f] of Object.entries(ADD_FIELDS)) {
    const p = problems[key];
    const show = !!p && (add.submitted || (p.kind === "invalid" && add.touched.has(key)));
    const el = $(f.error);
    el.hidden = !show;
    el.innerHTML = show ? `${icon("alert")}<span>${esc(p.msg)}</span>` : "";
    $(`[data-field="${key}"]`).classList.toggle("has-error", show);
    if (show) f.control().setAttribute("aria-invalid", "true");
    else f.control().removeAttribute("aria-invalid");
  }
}

// ---- Live state

function renderAddButton() {
  if (add.busy) return;
  const n = add.mode === "many" ? gridState.ready : 0;
  $("#add-submit .btn-label").textContent = n > 1 ? `Add ${plural(n, "Entry", "Entries")}` : "Add to Blacklist";
}

const addIdentifierText = () => (add.mode === "one" ? $("#add-one").value : "");

let addFrame = 0;
/** Re-derive everything from the form. Batched to one frame so a 5,000-line paste stays smooth. */
function refreshAdd() {
  cancelAnimationFrame(addFrame);
  addFrame = requestAnimationFrame(() => {
    add.ids = analyzeIdentifiers(addIdentifierText());
    if (add.mode === "many") renderGridStatus();
    renderAddButton();
    renderAddErrors(addProblems());
  });
}

function setAddBusy(busy) {
  add.busy = busy;
  const btn = $("#add-submit");
  btn.disabled = busy;
  btn.classList.toggle("busy", busy);
  $("#add-form").setAttribute("aria-busy", busy);
  if (busy) $(".btn-label", btn).textContent = "Adding…";
  else renderAddButton();
}

function showAddAlert(message) {
  const el = $("#add-alert");
  el.hidden = !message;
  el.innerHTML = message
    ? `${icon("alert")}<div><strong>Couldn’t add.</strong> ${esc(message)}` +
      `<p>Check your connection and try again. If some entries were saved before the error, submitting again is safe: they are skipped.</p></div>`
    : "";
}

// ---- Submit + result

async function submitAdd(e) {
  e.preventDefault();
  if (add.busy) return;
  add.submitted = true;
  cancelAnimationFrame(addFrame);
  add.ids = analyzeIdentifiers(addIdentifierText());
  if (add.mode === "many") renderGridStatus();
  renderAddButton();
  const problems = addProblems();
  renderAddErrors(problems);
  if (add.mode === "many") return submitGrid(problems);
  const first = Object.keys(ADD_FIELDS).find((k) => problems[k]);
  if (first) {
    ADD_FIELDS[first].control().focus({ preventScroll: true });
    $(`[data-field="${first}"]`).scrollIntoView({ block: "center", behavior: reduceMotion ? "auto" : "smooth" });
    return;
  }
  showAddAlert("");
  const body = {
    identifiers: add.ids.unique,
    identifier_type_code: add.combos.type.value.id,
    channel_ids: [add.combos.store.value.id],
    blacklist_reason_code_id: add.combos.reason.value.id,
    blacklist_notes: $("#notes-add").value.trim(),
    created_by: $("#add-initials").value.trim(),
  };
  setAddBusy(true);
  try {
    // TEMPORARY (MOCK_LOOKUPS): preview the result without touching the API.
    const res = MOCK_LOOKUPS ? mockBulkAdd(body) : await api("/api/marketplace-blacklist/bulk", { method: "POST", body });
    add.saved = true;
    browse.loadedOnce = false;
    renderAddResult(res, body);
  } catch (err) {
    showAddAlert(err.message);
  } finally {
    setAddBusy(false);
  }
}

const RESULT_LIST_MAX = 200; // keep the DOM small for a 5,000-line list

function resultList(items, row) {
  const shown = items.slice(0, RESULT_LIST_MAX).map((x) => `<li>${row(x)}</li>`).join("");
  const more = items.length > RESULT_LIST_MAX ? `<li class="muted">and ${(items.length - RESULT_LIST_MAX).toLocaleString()} more</li>` : "";
  return `<ul class="result-list">${shown}${more}</ul>`;
}

function renderAddResult(res, body) {
  const added = res.created.length;
  const skipped = res.skipped_existing.length;
  const errors = res.errors.length;
  const single = body.identifiers.length === 1;
  const title = added
    ? single ? `Added ${body.identifiers[0]}` : `Added ${plural(added, "entry", "entries")}`
    : errors ? "Nothing was added" : single ? "Already on the blacklist" : "Nothing new to add";
  const idCell = (v) => `<span class="mono" translate="no">${esc(v)}</span>`;
  const on = (id) => ` <span class="muted">on ${esc(channelLabel(id))}</span>`;
  const section = (n, label, open, rows) =>
    n ? `<details${open ? " open" : ""}><summary>${label} (${n.toLocaleString()})</summary>${rows}</details>` : "";
  const r = $("#add-result");
  r.innerHTML = `
    <h3 tabindex="-1" id="add-result-title" translate="no">${esc(title)}</h3>
    ${MOCK_LOOKUPS ? `<p class="mock-note">${icon("alert")}<span>Mock lookups: a preview only. Nothing was saved.</span></p>` : ""}
    <ul class="result-tiles">
      <li class="good"><span class="tile-icon">${icon("check")}</span><span class="tile-num">${added.toLocaleString()}</span><span>added</span></li>
      <li class="neutral"><span class="tile-icon">${icon("layers")}</span><span class="tile-num">${skipped.toLocaleString()}</span><span>already existed, skipped</span></li>
      <li class="${errors ? "bad" : "neutral"}"><span class="tile-icon">${icon("x")}</span><span class="tile-num">${errors.toLocaleString()}</span><span>${errors === 1 ? "error" : "errors"}</span></li>
    </ul>
    ${section(errors, "Errors", true, resultList(res.errors, (x) =>
      `${idCell(x.identifier)}${x.channel_id ? on(x.channel_id) : ""}<div class="bad-text small">${esc(x.error)}</div>`))}
    ${section(skipped, "Already existed", single, resultList(res.skipped_existing, (x) =>
      `${idCell(x.identifier)}${on(x.channel_id)}<div class="muted small">${esc(x.reason_name ?? "")}` +
      `${x.created_by ? ` · added by ${esc(x.created_by)}` : ""}${x.created_at ? ` · ${esc(fmtDate(x.created_at))}` : ""}</div>`))}
    ${single ? "" : section(added, "Added", false, resultList(res.created, (x) => `${idCell(x.identifier)}${on(x.channel_id)}`))}
    <div class="result-actions">
      <button type="button" class="btn primary round" id="add-more">${icon("plus")}Add Another</button>
      <a class="btn round" href="#browse" id="add-browse">View in Browse</a>
    </div>`;
  r.hidden = false;
  $("#add-result-title").focus({ preventScroll: true });
  r.scrollIntoView({ block: "nearest", behavior: reduceMotion ? "auto" : "smooth" });
  if (added) toast(`Added ${plural(added, "entry", "entries")}`);

  $("#add-more").addEventListener("click", () => {
    $("#add-one").value = ""; // the grid was already emptied of saved rows by submitGrid
    add.submitted = false;
    grid.submitted = false;
    add.touched.clear();
    r.hidden = true;
    refreshAdd();
    $(addFocusTarget()).focus();
  });
  $("#add-browse").addEventListener("click", (e) => {
    e.preventDefault();
    // One identifier: open Browse already searched for it.
    $("#browse-q").value = single ? body.identifiers[0] : "";
    browse.page = 1;
    showTab("browse");
  });
}

function setupAdd() {
  add.combos.type = makeCombo($("#add-type").closest(".combo"),
    lookups.identifier_types.map((t) => ({ id: t.identifier_type_code, name: t.identifier_type_name })), { key: "type", noun: "type" });
  add.combos.store = makeCombo($("#add-store").closest(".combo"), storeItems(), { key: "store", noun: "store" });
  add.combos.reason = makeCombo($("#add-reason").closest(".combo"),
    lookups.reason_codes
      .filter((r) => !isExcludedReason(r.reason_name))
      .map((r) => ({ id: r.blacklist_reason_code_id, name: r.reason_name })), { key: "reason", noun: "reason" });

  // Remember the last type and store used on this browser; never guess them for a first-time user.
  for (const key of ["type", "store"]) {
    const last = store.get(`blacklist.add_${key}`);
    const match = add.combos[key].items.find((it) => (it.label ?? it.name) === last);
    if (match) add.combos[key].set(match);
  }
  syncTypeHint();
  $("#add-kbd-mod").textContent = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl";

  for (const r of $$('.mode-switch input[name="add-mode"]')) r.addEventListener("change", () => setAddMode(r.value, { focus: true }));

  const form = $("#add-form");
  form.addEventListener("submit", submitAdd);
  form.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener("input", refreshAdd);
  form.addEventListener("combo-change", (e) => {
    const { key, value } = e.detail;
    if (key === "type" || key === "store") store.set(`blacklist.add_${key}`, value ? value.label ?? value.name : "");
    if (key === "type") syncTypeHint();
    refreshAdd();
  });
  form.addEventListener("focusout", (e) => {
    const key = e.target.closest("[data-field]")?.dataset.field;
    if (key && !(key in add.combos)) { // the dropdowns record their own touch once the typed text is resolved
      add.touched.add(key);
      refreshAdd();
    }
  });

  $("#add-one").addEventListener("input", () => (add.saved = false));
  setupGrid();

  // Warn before leaving with identifiers that were never submitted.
  window.addEventListener("beforeunload", (e) => {
    if (($("#add-one").value.trim() && !add.saved) || gridHasData()) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
  refreshAdd();
}

// ---- Add many: spreadsheet grid --------------------------------------------
// Each row is one blacklist entry with its own type, store, reason and notes. Every cell is a
// text input; Type, Store and Reason suggest from a datalist and resolve by name, ignoring case
// (a store also by its short name, "Turba"), or to the one option containing the text. A block
// copied in Excel pastes from the focused cell on, adding rows as needed. Rows already on the
// blacklist are reported by the server, not overwritten. Posts to /api/marketplace-blacklist/bulk-rows.

const GRID_COLS = ["identifier", "type", "store", "reason", "notes"];
const GRID_LABEL = { identifier: "SKU / ID", type: "Type", store: "Store", reason: "Reason", notes: "Notes" };
const GRID_PICK_COLS = ["type", "store", "reason"];
const GRID_MIN_ROWS = 10;
const NOTES_MAX = 1000; // blacklist_notes NVARCHAR(1000)
const GRID_HEADER_WORDS = new Set([...HEADER_WORDS, "sku / id", "sku/id", "type", "store", "channel", "reason", "notes"]);

const grid = { rows: [], submitted: false, options: {}, analysis: [] };
const gridState = { ready: 0, bad: 0, dupes: 0, filled: 0 };

const blankRow = () => ({ identifier: "", type: "", store: "", reason: "", notes: "", touched: false, serverError: "" });
const squash = (s) => s.trim().replace(/\s+/g, " ");
const normName = (s) => squash(s).toLowerCase();
const rowHasData = (r) => GRID_COLS.some((c) => r[c].trim());
const gridHasData = () => grid.rows.some(rowHasData);
const gridBody = () => $("#add-grid tbody");

function gridOptions() {
  grid.options = {
    type: lookups.identifier_types.map((t) => ({ id: t.identifier_type_code, label: t.identifier_type_name, names: [t.identifier_type_name] })),
    store: storeItems().map((s) => ({ id: s.id, label: s.label, names: [s.label, s.name] })),
    reason: lookups.reason_codes
      .filter((r) => !isExcludedReason(r.reason_name))
      .map((r) => ({ id: r.blacklist_reason_code_id, label: r.reason_name, names: [r.reason_name] })),
  };
  for (const col of GRID_PICK_COLS)
    $(`#dl-${col}`).innerHTML = grid.options[col].map((o) => `<option value="${esc(o.label)}"></option>`).join("");
}

/** The option a cell's text names: an exact name, else the only option that contains the text. */
function resolveOption(col, text) {
  const q = normName(text);
  if (!q) return null;
  const opts = grid.options[col];
  const exact = opts.filter((o) => o.names.some((n) => normName(n) === q));
  if (exact.length) return exact.length === 1 ? exact[0] : null;
  const partial = opts.filter((o) => o.names.some((n) => normName(n).includes(q)));
  return partial.length === 1 ? partial[0] : null;
}

/** Each row's state: empty, error (with the problems), dupe (same SKU + type + store as an earlier row) or ready. */
function analyzeGrid() {
  const seen = new Map();
  const counts = { ready: 0, bad: 0, dupes: 0, filled: 0 };
  const analysis = grid.rows.map((r, i) => {
    if (!rowHasData(r)) return { kind: "empty" };
    counts.filled++;
    const cols = new Set();
    const msgs = [];
    const identifier = squash(r.identifier);
    if (!identifier) { cols.add("identifier"); msgs.push("Enter a SKU / ID"); }
    else if (identifier.length > IDENTIFIER_MAX) { cols.add("identifier"); msgs.push(`SKU / ID is longer than ${IDENTIFIER_MAX} characters`); }
    const ids = {};
    for (const col of GRID_PICK_COLS) {
      const noun = GRID_LABEL[col].toLowerCase();
      const text = squash(r[col]);
      const opt = text && resolveOption(col, text);
      if (opt) ids[col] = opt.id;
      else {
        cols.add(col);
        msgs.push(!text ? `Choose a ${noun}`
          : col === "reason" && isExcludedReason(text) ? `“${ADD_EXCLUDED_REASON}” is only for imported entries`
          : `“${text}” isn’t a ${noun} from the list`);
      }
    }
    if (r.notes.length > NOTES_MAX) { cols.add("notes"); msgs.push(`Notes are longer than ${NOTES_MAX} characters`); }
    if (r.serverError) msgs.push(r.serverError);
    if (msgs.length) {
      counts.bad++;
      return { kind: "error", msgs, cols };
    }
    const key = `${identifier.toLowerCase()}|${ids.type}|${ids.store}`;
    if (seen.has(key)) {
      counts.dupes++;
      return { kind: "dupe", msgs: [`Same as row ${seen.get(key) + 1}, skipped`] };
    }
    seen.set(key, i);
    counts.ready++;
    return {
      kind: "ready",
      body: { identifier, identifier_type_code: ids.type, channel_id: ids.store, blacklist_reason_code_id: ids.reason, blacklist_notes: r.notes.trim() },
    };
  });
  Object.assign(gridState, counts);
  return analysis;
}

function rowHtml(r, i) {
  const cell = (col, attrs) =>
    `<td><input data-col="${col}" value="${esc(r[col])}" ${attrs} spellcheck="false" autocomplete="off" ` +
    `aria-label="${GRID_LABEL[col]}, row ${i + 1}"></td>`;
  return `<tr><td class="g-num">${i + 1}</td>` +
    cell("identifier", `class="mono" translate="no" maxlength="200"`) +
    cell("type", `list="dl-type"`) + cell("store", `list="dl-store"`) + cell("reason", `list="dl-reason"`) +
    // Notes is a textarea: one clipped line in the grid, opening wider and taller while you type in it.
    `<td class="g-notes"><textarea data-col="notes" rows="1" maxlength="${NOTES_MAX}" spellcheck="true" ` +
    `aria-label="Notes, row ${i + 1}">${esc(r.notes)}</textarea></td>` +
    `<td class="g-check"></td>` +
    `<td class="g-del-col"><button type="button" class="g-del" tabindex="-1" aria-label="Remove row ${i + 1}" title="Remove row">${icon("x")}</button></td></tr>`;
}

/** Keep at least GRID_MIN_ROWS rows and always one empty row at the bottom to type into. */
function padGrid() {
  while (grid.rows.length < GRID_MIN_ROWS) grid.rows.push(blankRow());
  if (rowHasData(grid.rows[grid.rows.length - 1])) grid.rows.push(blankRow());
}

function renderGrid() {
  padGrid();
  gridBody().innerHTML = grid.rows.map(rowHtml).join("");
  renderGridStatus();
}

/** Row colours, the Check column and the counts under the grid. Cheap: no inputs are rebuilt. */
function renderGridStatus() {
  grid.analysis = analyzeGrid();
  const trs = gridBody().rows;
  grid.analysis.forEach((a, i) => {
    const tr = trs[i];
    if (!tr) return;
    const r = grid.rows[i];
    const show = a.kind === "error" && (grid.submitted || r.touched || !!r.serverError);
    tr.className = show ? "g-bad" : a.kind === "dupe" ? "g-dupe" : a.kind === "ready" ? "g-ready" : "";
    for (const input of tr.querySelectorAll("[data-col]")) {
      if (show && a.cols.has(input.dataset.col)) input.setAttribute("aria-invalid", "true");
      else input.removeAttribute("aria-invalid");
    }
    const check = tr.cells[6];
    check.title = show || a.kind === "dupe" ? a.msgs.join("\n") : "";
    check.innerHTML = show
      ? `${icon("alert")}<span>${esc(a.msgs[0])}${a.msgs.length > 1 ? ` <b>+${a.msgs.length - 1}</b>` : ""}</span>`
      : a.kind === "dupe" ? `<span>${esc(a.msgs[0])}</span>`
      : a.kind === "ready" ? `${icon("check")}<span>Ready</span>` : "";
  });
  const { ready, bad, dupes, filled } = gridState;
  const parts = filled ? [`<b>${plural(ready, "entry", "entries")} ready</b>`] : ["No entries yet"];
  if (bad) parts.push(`<span class="bad-text">${plural(bad, "row")} to fix</span>`);
  if (dupes) parts.push(`${plural(dupes, "duplicate")} skipped`);
  if (ready > BULK_MAX) parts.push(`<span class="bad-text">over the ${BULK_MAX.toLocaleString()} limit, add it in parts</span>`);
  $("#grid-stats").innerHTML = parts.join(" · ");
  $("#grid-clear").hidden = !filled;
}

/** An open Notes cell grows with its text (the CSS max-height caps it, then it scrolls). */
function fitNotes(ta) {
  ta.style.height = "auto";
  ta.style.height = `${ta.scrollHeight + 2}px`;
}

function focusCell(i, col) {
  const input = gridBody().rows[i]?.querySelector(`[data-col="${col}"]`);
  if (input) {
    input.focus();
    input.select();
  }
}

/** Excel's clipboard text: rows by line break, cells by tab; a cell holding either is quoted. */
function parseClipboard(text) {
  const rows = [];
  let row = [], cell = "", quoted = false;
  for (let k = 0; k < text.length; k++) {
    const ch = text[k];
    if (quoted) {
      if (ch !== '"') cell += ch;
      else if (text[k + 1] === '"') { cell += '"'; k++; }
      else quoted = false;
    } else if (ch === '"' && cell === "") quoted = true;
    else if (ch === "\t") { row.push(cell); cell = ""; }
    else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[k + 1] === "\n") k++;
      row.push(cell); rows.push(row); row = []; cell = "";
    } else cell += ch;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  return rows.filter((r) => r.some((c) => c.trim()));
}

function pasteIntoGrid(start, colIndex, matrix) {
  // A copied header row (SKU, Type, Store…) is not an entry.
  if (matrix.length && matrix[0].some((c) => GRID_HEADER_WORDS.has(normName(c)))) matrix = matrix.slice(1);
  if (!matrix.length) return;
  if (start + matrix.length > BULK_MAX) {
    matrix = matrix.slice(0, Math.max(0, BULK_MAX - start));
    toast(`Only the first ${BULK_MAX.toLocaleString()} rows were pasted. Add the rest in another batch.`, true);
  }
  while (grid.rows.length < start + matrix.length) grid.rows.push(blankRow());
  matrix.forEach((cells, j) => {
    const r = grid.rows[start + j];
    r.serverError = "";
    cells.forEach((value, k) => {
      const col = GRID_COLS[colIndex + k];
      if (!col) return; // extra columns on the right are ignored
      let v = squash(value);
      const opt = GRID_PICK_COLS.includes(col) && resolveOption(col, v);
      if (opt) v = opt.label; // show the full name, e.g. "Turba" -> "Amazon - Turba"
      r[col] = v;
    });
  });
  add.saved = false;
  renderGrid();
  refreshAdd();
  focusCell(start, GRID_COLS[colIndex]);
  toast(`Pasted ${plural(matrix.length, "row")}`);
}

/** Every empty cell in the column, on a row with a SKU, gets the nearest value above it. */
function fillDown(col) {
  let last = "", n = 0;
  for (const r of grid.rows) {
    if (r[col].trim()) last = r[col];
    else if (last && r.identifier.trim()) { r[col] = last; n++; }
  }
  if (!n) return toast(`No empty ${GRID_LABEL[col]} cells under a filled one`);
  renderGrid();
  refreshAdd();
  toast(`Filled ${plural(n, `${GRID_LABEL[col]} cell`)}`);
}

async function submitGrid(problems) {
  grid.submitted = true;
  renderGridStatus();
  renderAddButton();
  const firstBad = grid.analysis.findIndex((a) => a.kind === "error");
  if (firstBad >= 0) {
    const col = GRID_COLS.find((c) => grid.analysis[firstBad].cols.has(c)) ?? "identifier";
    focusCell(firstBad, col);
    gridBody().rows[firstBad].scrollIntoView({ block: "center", behavior: reduceMotion ? "auto" : "smooth" });
    return toast(`Fix ${plural(gridState.bad, "row")} first: see the Check column`, true);
  }
  if (!gridState.ready) {
    focusCell(0, "identifier");
    return toast("Enter at least one row", true);
  }
  if (gridState.ready > BULK_MAX)
    return toast(`That’s ${gridState.ready.toLocaleString()} entries; the limit is ${BULK_MAX.toLocaleString()}. Add them in parts.`, true);
  if (problems.initials) return $("#add-initials").focus();

  showAddAlert("");
  const sent = grid.analysis.flatMap((a, i) => (a.kind === "ready" ? [{ i, body: a.body }] : []));
  const body = { rows: sent.map((s) => s.body), created_by: $("#add-initials").value.trim() };
  setAddBusy(true);
  try {
    // TEMPORARY (MOCK_LOOKUPS): preview the result without touching the API.
    const res = MOCK_LOOKUPS ? mockBulkRows(body) : await api("/api/marketplace-blacklist/bulk-rows", { method: "POST", body });
    const created = [], existing = [], errors = [], keep = [];
    for (const r of res.results) {
      const { i, body: row } = sent[r.row];
      if (r.status === "created") created.push(r.listing);
      else if (r.status === "existing") existing.push(r.listing);
      else {
        errors.push({ identifier: row.identifier, channel_id: row.channel_id, error: r.error });
        grid.rows[i].serverError = r.error;
        keep.push(i);
      }
    }
    // Saved and duplicate rows leave the grid; rows the server refused stay, with its message.
    grid.rows = keep.map((i) => grid.rows[i]);
    grid.submitted = keep.length > 0;
    renderGrid();
    add.saved = true;
    browse.loadedOnce = false;
    renderAddResult({ created, skipped_existing: existing, errors }, { identifiers: body.rows.map((r) => r.identifier) });
  } catch (err) {
    showAddAlert(err.message);
  } finally {
    setAddBusy(false);
  }
}

function setupGrid() {
  gridOptions();
  renderGrid();
  const tbody = gridBody();
  const where = (e) => {
    const input = e.target.closest("[data-col]");
    return input && { input, i: input.closest("tr").sectionRowIndex, col: input.dataset.col };
  };

  tbody.addEventListener("input", (e) => {
    const at = where(e);
    if (!at) return;
    const r = grid.rows[at.i];
    r[at.col] = at.input.value;
    r.serverError = "";
    add.saved = false;
    if (at.col === "notes") fitNotes(at.input);
    if (at.i === grid.rows.length - 1 && rowHasData(r)) { // typing in the last row adds a fresh one
      grid.rows.push(blankRow());
      tbody.insertAdjacentHTML("beforeend", rowHtml(grid.rows.at(-1), grid.rows.length - 1));
    }
    // the form's own input listener calls refreshAdd()
  });

  tbody.addEventListener("focusout", (e) => {
    const at = where(e);
    if (!at) return;
    const r = grid.rows[at.i];
    if (at.col === "notes") at.input.style.height = ""; // back to one line
    // Show the full name for a recognised pick ("turba" -> "Amazon - Turba").
    const opt = GRID_PICK_COLS.includes(at.col) && resolveOption(at.col, r[at.col]);
    if (opt && r[at.col] !== opt.label) at.input.value = r[at.col] = opt.label;
    // A row shows its problems once you leave it.
    if (!at.input.closest("tr").contains(e.relatedTarget) && rowHasData(r)) r.touched = true;
    refreshAdd();
  });

  tbody.addEventListener("keydown", (e) => {
    const at = where(e);
    if (!at || e.ctrlKey || e.metaKey || e.altKey) return; // Ctrl+Enter submits the form
    // Arrows move between rows only in SKU / ID: the pick columns use them for suggestions, Notes for its lines.
    const vertical = e.key === "Enter" || ((e.key === "ArrowDown" || e.key === "ArrowUp") && at.col === "identifier");
    if (!vertical) return;
    e.preventDefault(); // Enter never submits from a cell
    const up = e.key === "ArrowUp" || (e.key === "Enter" && e.shiftKey);
    const next = at.i + (up ? -1 : 1);
    if (next >= 0) focusCell(Math.min(next, grid.rows.length - 1), at.col);
  });

  tbody.addEventListener("focusin", (e) => {
    if (e.target.matches?.('textarea[data-col="notes"]')) fitNotes(e.target);
  });

  tbody.addEventListener("paste", (e) => {
    const at = where(e);
    if (!at) return;
    const matrix = parseClipboard(e.clipboardData.getData("text/plain"));
    if (matrix.length <= 1 && (matrix[0]?.length ?? 0) <= 1) return; // one cell: a normal paste
    e.preventDefault();
    pasteIntoGrid(at.i, GRID_COLS.indexOf(at.col), matrix);
  });

  tbody.addEventListener("click", (e) => {
    const btn = e.target.closest(".g-del");
    if (!btn) return;
    const i = btn.closest("tr").sectionRowIndex;
    grid.rows.splice(i, 1);
    renderGrid();
    refreshAdd();
    focusCell(Math.min(i, grid.rows.length - 1), "identifier");
  });

  for (const btn of $$("#add-grid .g-fill")) btn.addEventListener("click", () => fillDown(btn.dataset.fill));
  $("#grid-add-rows").addEventListener("click", () => {
    const first = grid.rows.length;
    for (let k = 0; k < 10; k++) grid.rows.push(blankRow());
    renderGrid();
    focusCell(first, "identifier");
  });
  $("#grid-clear").addEventListener("click", () => {
    if (!confirm(`Clear all ${plural(gridState.filled, "row")} from the grid?`)) return;
    grid.rows = [];
    grid.submitted = false;
    renderGrid();
    refreshAdd();
    focusCell(0, "identifier");
  });
}

// ---- Wire-up ---------------------------------------------------------------

async function init() {
  try {
    lookups = MOCK_LOOKUPS ?? (await api("/api/marketplace-blacklist/lookups")); // TEMPORARY (MOCK_LOOKUPS)
  } catch (err) {
    toast(`Couldn't load lookups: ${err.message}`, true);
    return;
  }
  fillSelects();

  // TEMPORARY (MOCK_LOOKUPS): say so on every tab, and link back to the same tab with real data.
  if (MOCK_LOOKUPS) {
    $("#mock-banner").hidden = false;
    const real = new URLSearchParams(location.search);
    real.delete("mock");
    $("#mock-off").addEventListener("click", (e) => {
      e.preventDefault();
      location.href = `${location.pathname}${real.size ? `?${real}` : ""}${location.hash}`;
    });
  }

  // Tabs
  for (const t of $$(".tab")) t.addEventListener("click", () => showTab(t.dataset.tab));
  const thumb = $(".tab-thumb");
  thumb.style.transition = "none"; // place it without sliding in on first load; re-enabled below
  moveThumb();
  window.addEventListener("resize", moveThumb);
  document.fonts?.ready.then(moveThumb);

  // "/" focuses the search on the current tab
  document.addEventListener("keydown", (e) => {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) return;
    const target = { browse: browseFocusTarget(), add: addFocusTarget() }[currentTab()];
    if (target) {
      e.preventDefault();
      $(target).focus();
    }
  });

  for (const btn of $$("[data-goto]")) btn.addEventListener("click", () => showTab(btn.dataset.goto));

  // Bulk check (Browse › Check List)
  $("#browse-mode").addEventListener("click", () => setBrowseChecking(!browseChecking, { focus: true }));
  $("#bulk-form").addEventListener("submit", bulkCheck);
  $("#bulk-input").addEventListener("input", updateBulkCount);
  wireDropzone();
  $("#bulk-clear").addEventListener("click", () => {
    $("#bulk-input").value = "";
    updateBulkCount();
    $("#bulk-results").hidden = true;
    bulkResults = [];
    $("#bulk-input").focus();
  });
  $("#bulk-only-bad").addEventListener("change", renderBulk);
  $("#bulk-export").addEventListener("click", exportBulk);

  // Browse: typing filters the table; Enter / Check adds the rule-based verdict above it.
  $("#browse-form").addEventListener("submit", browseCheck);
  const reloadBrowse = debounce(resetAndLoadBrowse, 220);
  $("#browse-q").addEventListener("input", () => {
    $("#browse-verdict").innerHTML = ""; // the verdict was for the old text
    reloadBrowse();
  });
  for (const id of ["#browse-channel", "#browse-reason", "#browse-type"]) $(id).addEventListener("change", resetAndLoadBrowse);
  // A verdict on screen follows the store filter.
  $("#browse-channel").addEventListener("change", () => { if ($("#browse-verdict").innerHTML) browseCheck(); });
  $("#browse-verdict").addEventListener("click", (e) => {
    if (!e.target.closest("[data-close-verdict]")) return;
    $("#browse-verdict").innerHTML = "";
    $("#browse-q").focus();
  });
  $("#browse-prev").addEventListener("click", () => { browse.page--; loadBrowse(); });
  $("#browse-next").addEventListener("click", () => { browse.page++; loadBrowse(); });
  $("#browse-tbody").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-edit]");
    if (btn) openEdit(Number(btn.dataset.edit));
  });

  // Edit dialog
  const dlg = $("#edit-dialog");
  $("#edit-form").addEventListener("submit", saveEdit);
  $("#edit-cancel").addEventListener("click", closeEdit);
  $("#edit-close").addEventListener("click", closeEdit);
  $("#edit-delete").addEventListener("click", deleteEntry);
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); closeEdit(); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) closeEdit(); }); // backdrop click

  for (const ta of $$("textarea[maxlength]")) ta.addEventListener("input", updateCounters);
  updateCounters();

  // Remember "Added by" initials on this browser until sign-in provides the user.
  const showMe = () => {
    const initials = store.get("blacklist.created_by").toUpperCase();
    $("#me").innerHTML = initials ? esc(initials) : icon("user");
    $("#me").title = initials ? `Adding entries as ${initials}` : "Enter your initials on the Add tab";
  };
  showMe();
  for (const input of $$("[data-remember]")) {
    input.value = store.get(`blacklist.${input.dataset.remember}`);
    input.addEventListener("input", () => {
      store.set(`blacklist.${input.dataset.remember}`, input.value.trim());
      for (const other of $$(`[data-remember="${input.dataset.remember}"]`)) if (other !== input) other.value = input.value;
      showMe();
    });
  }

  // Add (after the remembered initials are filled in)
  setupAdd();

  // Deep links: / or /#browse (also the old /#check), /#add (add one), /#add-many,
  // /#bulk (Browse in Check List mode), or /?q=SKU to search and check it.
  const tab = location.hash.slice(1);
  const mode = Object.keys(ADD_MODE_HASH).find((m) => ADD_MODE_HASH[m] === tab);
  const q = new URLSearchParams(location.search).get("q");
  if (mode) {
    setAddMode(mode);
    showTab("add");
  } else {
    if (tab === "bulk") setBrowseChecking(true);
    if (q) $("#browse-q").value = q;
    showTab("browse");
    if (q) browseCheck();
  }
  requestAnimationFrame(() => requestAnimationFrame(() => (thumb.style.transition = "")));
}

init();
