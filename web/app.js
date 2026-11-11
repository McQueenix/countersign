/* Countersign console. Plain JavaScript, AG Grid for the ledger and the rules table. */

const $ = (id) => document.getElementById(id);

const state = {
  payments: new Map(),
  inbox: [],
  mandate: null,
  notes: {},        // latest agent remark per inbox item
  lastStatus: {},   // to know when a stamp should land
  ledger: null,
  rules: null,
};

const STATUS = {
  reviewing:        { label: "In review",          tone: "busy", stamp: "In review" },
  executing:        { label: "Sending",            tone: "busy", stamp: "Sending" },
  executed:         { label: "Sent",               tone: "ok",   stamp: "Sent" },
  awaiting_payer:   { label: "Approve in PayPal",  tone: "hold", stamp: "At PayPal" },
  pending_approval: { label: "Waiting for you",    tone: "hold", stamp: "Held" },
  blocked:          { label: "Blocked",            tone: "stop", stamp: "Blocked" },
  rejected:         { label: "Refused by you",     tone: "stop", stamp: "Refused" },
  failed:           { label: "PayPal error",       tone: "stop", stamp: "Failed" },
};

const RAIL = {
  payout:   { label: "Payout",   verb: "Pay",     api: "PayPal Payouts" },
  invoice:  { label: "Invoice",  verb: "Bill",    api: "PayPal Invoicing" },
  purchase: { label: "Purchase", verb: "Buy from", api: "PayPal Checkout" },
};

const SIGNED_BY = { "policy": "Policy", "policy+ai": "Policy and AI review", "ai_reviewer": "AI review", "owner": "You", "": "" };

// ---- helpers ---------------------------------------------------------------

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function money(amount, currency = "USD") {
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(amount);
  } catch {
    return `${Number(amount).toFixed(2)} ${currency}`;
  }
}

function clock(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

async function api(path, options = {}) {
  const init = { method: options.method || "GET", headers: {} };
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  let data = null;
  try { data = await response.json(); } catch { /* empty body */ }
  if (!response.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : `Request failed (${response.status})`);
  }
  return data;
}

let toastTimer;
function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 5000);
}

async function busy(button, work) {
  button.disabled = true;
  try { await work(); } catch (error) { toast(error.message); } finally { button.disabled = false; }
}

function actorName(payment) {
  if (payment.actor === "agent") return (state.mandate && state.mandate.agent_name) || "Ops Agent";
  return payment.actor;
}

function stops(payment) {
  return payment.checks.filter((c) => c.status !== "pass");
}

// ---- status pills ----------------------------------------------------------

async function loadStatus(deep = false) {
  try {
    const s = await api(deep ? "/api/status?deep=true" : "/api/status");
    $("preview-banner").hidden = !s.preview;
    document.title = `Countersign for ${s.business}`;
    setPill("st-paypal", s.paypal.ok, s.preview ? "PayPal stand-in" : "PayPal sandbox", s.paypal.detail);
    setPill("st-llm", s.llm.ok, s.llm.ok ? `AI: ${s.llm.model}` : "AI model not answering", s.llm.detail);
    if (deep && !s.llm.ok) toast(`AI model: ${s.llm.detail}`);
    if (deep && !s.paypal.ok) toast(`PayPal: ${s.paypal.detail}`);
    setChain(s.chain);
  } catch (error) {
    setPill("st-paypal", false, "Server unreachable", error.message);
  }
}

function setPill(id, ok, text, title) {
  const el = $(id);
  el.dataset.state = ok ? "ok" : "bad";
  el.querySelector(".pill-text").textContent = text;
  el.title = title || "";
}

function setChain(chain) {
  setPill("st-chain", chain.ok, chain.ok ? `Audit log intact, ${chain.count} entries` : `Audit log broken at entry ${chain.broken_at}`,
    "Every entry carries the hash of the one before it. Click to recompute them all.");
}

// ---- mandate ---------------------------------------------------------------

const RULES = [
  { key: "allow_payouts", rule: "Pay people (PayPal Payouts)", type: "bool" },
  { key: "allow_invoices", rule: "Bill clients (PayPal Invoicing)", type: "bool" },
  { key: "allow_purchases", rule: "Buy from vendors (PayPal Checkout)", type: "bool" },
  { key: "per_payment_cap", rule: "Most for one payment", type: "money" },
  { key: "auto_approve_limit", rule: "You sign anything over", type: "money" },
  { key: "weekly_budget", rule: "Most in any 7 days", type: "money", optional: true },
  { key: "daily_budget", rule: "Most in one day", type: "money", optional: true },
  { key: "invoice_cap", rule: "Largest invoice", type: "money", optional: true },
  { key: "allowed_payees", rule: "Approved payees", type: "emails" },
  { key: "blocked_payees", rule: "Never pay", type: "emails" },
  { key: "unknown_payee", rule: "Someone not on the list", type: "choice" },
  { key: "expires_on", rule: "Ends on", type: "date", optional: true },
];

const CHOICE_TEXT = { escalate: "Ask me first", deny: "Refuse" };

function ruleRows(mandate) {
  return RULES.map((r) => ({ ...r, value: mandate[r.key] }));
}

function showRule(params) {
  if (!params.data) return "";
  const { type, value } = params.data;
  if (type === "money") return value === null || value === undefined ? "No limit" : money(value, state.mandate.currency);
  if (type === "emails") return value && value.length ? value.join(", ") : "Nobody";
  if (type === "choice") return CHOICE_TEXT[value] || value;
  if (type === "date") return value || "No end date";
  return value;
}

function parseRule(params) {
  if (!params.data) return params.newValue;
  const { type, optional } = params.data;
  const raw = params.newValue;
  if (type === "money") {
    if (raw === null || raw === undefined || raw === "") return optional ? null : params.oldValue;
    const n = Number(String(raw).replace(/[$,\s]/g, ""));
    return Number.isFinite(n) && n >= 0 ? n : params.oldValue;
  }
  if (type === "emails") {
    if (Array.isArray(raw)) return raw;
    return String(raw || "").split(/[\s,;]+/).map((s) => s.trim().toLowerCase()).filter((s) => /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(s));
  }
  if (type === "date") return raw || null;
  return raw;
}

function editorFor(params) {
  const { type } = params.data || {};
  if (type === "bool") return { component: "agCheckboxCellEditor" };
  if (type === "money") return { component: "agNumberCellEditor", params: { min: 0, precision: 2 } };
  if (type === "choice") return { component: "agSelectCellEditor", params: { values: ["escalate", "deny"] } };
  if (type === "date") return { component: "agDateStringCellEditor" };
  if (type === "emails") return { component: "agLargeTextCellEditor", popup: true, params: { maxLength: 2000, rows: 4, cols: 40 } };
  return { component: "agTextCellEditor" };
}

function buildRulesGrid() {
  state.rules = agGrid.createGrid($("rules-grid"), {
    theme: gridTheme(),
    rowData: [],
    getRowId: (p) => p.data.key,
    domLayout: "autoHeight",
    singleClickEdit: true,
    stopEditingWhenCellsLoseFocus: true,
    suppressCellFocus: false,
    columnDefs: [
      { field: "rule", headerName: "Rule", flex: 1.15, sortable: false, wrapText: true, autoHeight: true },
      {
        field: "value", headerName: "Limit", flex: 1, editable: true, sortable: false, wrapText: true, autoHeight: true, cellDataType: false,
        valueFormatter: showRule,
        valueParser: parseRule,
        cellEditorSelector: editorFor,
        cellRendererSelector: (p) => (p.data && p.data.type === "bool" ? { component: "agCheckboxCellRenderer" } : undefined),
      },
    ],
    onCellValueChanged: saveRules,
  });
}

async function saveRules() {
  const next = { ...state.mandate };
  state.rules.forEachNode((node) => { next[node.data.key] = node.data.value; });
  try {
    setMandate(await api("/api/mandate", { method: "PUT", body: next }));
    toast("Rules updated. They apply to the next request.");
  } catch (error) {
    toast(error.message);
    setMandate(await api("/api/mandate"));
  }
}

function mandateSummary(m) {
  const cur = m.currency;
  const parts = [];
  parts.push(m.active ? "In force." : `<span class="paused">Paused. Every request is blocked.</span>`);
  parts.push(`${esc(m.agent_name)} may send up to <b>${esc(money(m.per_payment_cap, cur))}</b> at a time`);
  parts.push(`you sign anything over <b>${esc(money(m.auto_approve_limit, cur))}</b>`);
  if (m.weekly_budget !== null) parts.push(`<b>${esc(money(m.weekly_budget, cur))}</b> in any 7 days`);
  parts.push(`<b>${m.allowed_payees.length}</b> approved ${m.allowed_payees.length === 1 ? "payee" : "payees"}`);
  if (m.expires_on) parts.push(`ends ${esc(m.expires_on)}`);
  return parts[0] + " " + parts.slice(1).join(", ") + ".";
}

function setMandate(mandate) {
  state.mandate = mandate;
  if (document.activeElement !== $("mandate-text")) $("mandate-text").value = mandate.text;
  $("mandate-active").checked = mandate.active;
  $("mandate-active-label").textContent = mandate.active ? "In force" : "Paused";
  $("mandate-summary").innerHTML = mandateSummary(mandate);
  state.rules.setGridOption("rowData", ruleRows(mandate));
}

// ---- inbox -----------------------------------------------------------------

const ITEM_STATE = { new: "New", queued: "In line", working: "Agent is reading", done: "Handled", error: "Could not run" };
const CHANNEL = { task: "Project board", email: "Email", owner: "Your instruction" };

function renderInbox() {
  $("inbox").innerHTML = state.inbox.map((item) => {
    const note = state.notes[item.id] || item.summary;
    const canRun = item.status === "new" || item.status === "error";
    return `<li data-id="${esc(item.id)}">
      <div class="item-top">
        <div>
          <div class="item-subject">${esc(item.subject)}</div>
          <div class="item-from">${esc(CHANNEL[item.kind] || item.kind)}, from ${esc(item.sender)}</div>
        </div>
        ${canRun
          ? `<button class="button quiet" data-run="${esc(item.id)}">Hand to agent</button>`
          : `<span class="item-state" data-state="${esc(item.status)}">${esc(ITEM_STATE[item.status] || item.status)}</span>`}
      </div>
      <details><summary>Read the message</summary><div class="item-body">${esc(item.body)}</div></details>
      ${note ? `<div class="item-agent"><b>Agent:</b> ${esc(note)}</div>` : ""}
    </li>`;
  }).join("");
}

// ---- payment slips -----------------------------------------------------------

function signatureBlocks(p) {
  const policyChecks = p.checks.filter((c) => c.source === "policy");
  const worst = policyChecks.some((c) => c.status === "deny") ? "deny" : policyChecks.some((c) => c.status === "escalate") ? "escalate" : "pass";
  const policyHand = { pass: "within limits", escalate: "needs owner", deny: "refused" }[worst];

  let ai = { state: "none", hand: ".", who: "AI review, not consulted" };
  const review = p.review || {};
  if (review.status) {
    const score = review.risk_score === null || review.risk_score === undefined ? "" : `, risk ${review.risk_score} of 100`;
    ai = { state: review.status, hand: { pass: "clear", escalate: "not sure", deny: "refused" }[review.status], who: "AI review" + score };
  } else if (p.status === "reviewing") {
    ai = { state: "wait", hand: "reading the record", who: "AI review" };
  }

  let owner = { state: "none", hand: ".", who: "Owner, not required" };
  if (p.decision === "owner_approved") owner = { state: "pass", hand: "approved", who: "Owner" };
  else if (p.decision === "owner_rejected") owner = { state: "deny", hand: "refused", who: "Owner" };
  else if (p.status === "pending_approval") owner = { state: "wait", hand: "waiting for you", who: "Owner" };
  else if (p.status === "blocked") owner = { state: "none", hand: ".", who: "Owner, never reached" };

  return [
    { state: "pass", hand: actorName(p), who: "Requested by" },
    { state: worst, hand: policyHand, who: "Policy" },
    ai,
    owner,
  ].map((s) => `<div class="signature" data-state="${s.state}"><div class="hand">${esc(s.hand)}</div><div class="who">${esc(s.who)}</div></div>`).join("");
}

function paypalLine(p) {
  const ref = p.paypal || {};
  if (ref.error) {
    const e = ref.error;
    return `<div class="reason" data-status="deny">PayPal said no: ${esc(e.name)}. ${esc(e.message)}${e.debug_id ? ` (debug id ${esc(e.debug_id)})` : ""}</div>`;
  }
  if (!ref.id) return "";
  const bits = [`${esc(RAIL[p.rail].api)} reference <b>${esc(ref.id)}</b>`];
  if (ref.status) bits.push(`status ${esc(ref.item_status || ref.status)}`);
  if (ref.invoice_number) bits.push(`invoice no. ${esc(ref.invoice_number)}`);
  if (ref.transaction_id) bits.push(`transaction ${esc(ref.transaction_id)}`);
  if (ref.capture_id) bits.push(`capture ${esc(ref.capture_id)}`);
  let html = `<div class="paypal-ref">${bits.join(", ")}</div>`;
  if (ref.payer_url) html += `<div><a href="${esc(ref.payer_url)}" target="_blank" rel="noopener">Open the invoice your client sees</a></div>`;
  return html;
}

function slipActions(p) {
  const ref = p.paypal || {};
  if (p.status === "pending_approval") {
    return `<button class="button primary" data-approve="${esc(p.id)}">Sign and send</button><button class="button refuse" data-reject="${esc(p.id)}">Refuse</button>`;
  }
  if (p.status === "awaiting_payer" && ref.approve_url) {
    return `<a class="button primary" href="${esc(ref.approve_url)}">Approve in PayPal</a>`;
  }
  if (p.status === "failed") return `<button class="button" data-retry="${esc(p.id)}">Try again</button>`;
  if (p.status === "executed" && ref.id) return `<button class="button quiet" data-sync="${esc(p.id)}">Check with PayPal</button>`;
  return "";
}

function renderSlip(p) {
  const meta = STATUS[p.status] || { label: p.status, tone: "busy", stamp: p.status };
  const landed = state.lastStatus[p.id] !== undefined && state.lastStatus[p.id] !== p.status;
  const first = state.lastStatus[p.id] === undefined && state.live;
  state.lastStatus[p.id] = p.status;
  const reasons = stops(p).map((c) => `<div class="reason" data-status="${esc(c.status)}"><b>${esc(c.label)}.</b> ${esc(c.detail)}</div>`).join("");
  const actions = slipActions(p);
  return `<article class="slip" data-tone="${meta.tone}" data-id="${esc(p.id)}">
    <div class="slip-head">
      <span>${esc(RAIL[p.rail].label)} through ${esc(RAIL[p.rail].api)}</span>
      <span>${esc(clock(p.created_at))} <button class="slip-id" data-open="${esc(p.id)}">${esc(p.id)}</button></span>
    </div>
    <div class="slip-main">
      <div class="slip-party">${esc(RAIL[p.rail].verb)} ${esc(p.counterparty_name || p.counterparty_email)}<small>${esc(p.counterparty_email)}</small></div>
      <div class="slip-amount">${esc(money(p.amount, p.currency))}<small>${esc(p.currency)}</small></div>
      <div class="slip-purpose">${esc(p.purpose)}</div>
    </div>
    <div class="sign-row">
      <div class="signatures">${signatureBlocks(p)}</div>
      <div class="stamp ${landed || first ? "land" : ""}" data-tone="${meta.tone}">${esc(meta.stamp)}</div>
    </div>
    <div class="slip-foot">
      ${reasons}${paypalLine(p)}
      ${actions ? `<div class="slip-actions">${actions}</div>` : ""}
      ${!reasons && !paypalLine(p) && !actions ? `<span class="paypal-ref">Checking the mandate and reading the source record.</span>` : ""}
    </div>
  </article>`;
}

function sortedPayments() {
  return [...state.payments.values()].sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
}

function renderSlips() {
  const list = sortedPayments();
  $("slips").innerHTML = list.slice(0, 40).map(renderSlip).join("");
  $("slips-empty").hidden = list.length > 0;
}

function renderQueue() {
  const waiting = sortedPayments().filter((p) => p.status === "pending_approval" || p.status === "awaiting_payer");
  $("queue-count").textContent = waiting.length;
  $("queue-count").dataset.zero = waiting.length === 0;
  $("queue-empty").hidden = waiting.length > 0;
  $("queue").innerHTML = waiting.map((p) => {
    const why = stops(p).map((c) => `<li>${esc(c.detail)}</li>`).join("");
    const inPayPal = p.status === "awaiting_payer";
    return `<div class="ask">
      <div class="ask-amount">${esc(money(p.amount, p.currency))}</div>
      <div class="ask-party">${esc(RAIL[p.rail].verb)} ${esc(p.counterparty_name || p.counterparty_email)}, ${esc(p.purpose)}</div>
      ${inPayPal ? `<ul class="ask-why"><li>The order is ready. PayPal will ask you to confirm the payment.</li></ul>` : `<ul class="ask-why">${why}</ul>`}
      <div class="row">${slipActions(p)}</div>
    </div>`;
  }).join("");
}

async function loadStats() {
  const s = await api("/api/stats");
  const budget = s.weekly_budget;
  const share = budget ? Math.min(100, (s.week_committed / budget) * 100) : 0;
  $("meter-fill").style.width = `${share}%`;
  $("meter-fill").dataset.high = share >= 80;
  $("meter-text").textContent = budget
    ? `${money(s.week_committed, s.currency)} of ${money(budget, s.currency)} sent or reserved in the last 7 days`
    : `${money(s.week_committed, s.currency)} sent or reserved in the last 7 days, no weekly limit set`;
  $("t-sent").textContent = s.sent;
  $("t-waiting").textContent = s.waiting;
  $("t-stopped").textContent = s.stopped;
  $("t-saved").textContent = money(s.stopped_amount, s.currency);
}

let statsTimer;
function refreshSoon() {
  clearTimeout(statsTimer);
  statsTimer = setTimeout(() => { loadStats().catch(() => {}); loadStatus(); }, 250);
}

function upsertPayment(p) {
  const known = state.payments.has(p.id);
  state.payments.set(p.id, p);
  renderSlips();
  renderQueue();
  state.ledger.applyTransaction(known ? { update: [p] } : { add: [p], addIndex: 0 });
  refreshSoon();
}

// ---- ledger (AG Grid) ----------------------------------------------------------

function gridTheme() {
  return agGrid.themeQuartz.withParams({
    fontFamily: "inherit",
    fontSize: 14,
    accentColor: "#2343c4",
    backgroundColor: "#fcfdfc",
    foregroundColor: "#15233b",
    headerBackgroundColor: "#eef2f0",
    headerFontWeight: 600,
    borderColor: "#c5d0cb",
    rowHoverColor: "#f1f5f3",
    wrapperBorderRadius: 4,
    spacing: 6,
  });
}

function tagCell(text, tone) {
  const el = document.createElement("span");
  el.className = "tag";
  el.dataset.tone = tone;
  el.textContent = text;
  return el;
}

function riskCell(params) {
  const score = params.value;
  if (score === null || score === undefined) return "";
  const wrap = document.createElement("span");
  wrap.className = "risk";
  const level = score >= 80 ? "high" : score >= 40 ? "mid" : "low";
  wrap.innerHTML = `<span class="risk-bar"><i data-level="${level}" style="width:${Math.max(4, score)}%"></i></span>${score}`;
  return wrap;
}

function buildLedger() {
  state.ledger = agGrid.createGrid($("ledger-grid"), {
    theme: gridTheme(),
    rowData: [],
    getRowId: (p) => p.data.id,
    pagination: true,
    paginationPageSize: 10,
    paginationPageSizeSelector: [10, 25, 50],
    animateRows: true,
    rowSelection: undefined,
    defaultColDef: { sortable: true, filter: true, resizable: true, flex: 1, minWidth: 110 },
    initialState: { sort: { sortModel: [{ colId: "created_at", sort: "desc" }] } },
    columnDefs: [
      { colId: "created_at", headerName: "Time", field: "created_at", minWidth: 185, filter: false,
        valueFormatter: (p) => new Date(p.value).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) },
      { headerName: "Type", valueGetter: (p) => RAIL[p.data.rail].label, maxWidth: 120 },
      { headerName: "Counterparty", valueGetter: (p) => (p.data.counterparty_name ? `${p.data.counterparty_name} <${p.data.counterparty_email}>` : p.data.counterparty_email), flex: 1.8, minWidth: 220 },
      { headerName: "Amount", field: "amount", type: "rightAligned", filter: "agNumberColumnFilter", maxWidth: 140,
        valueFormatter: (p) => money(p.value, p.data.currency) },
      { headerName: "For", field: "purpose", flex: 1.8, minWidth: 200 },
      { headerName: "AI risk", valueGetter: (p) => (p.data.review ? p.data.review.risk_score ?? null : null), cellRenderer: riskCell, filter: "agNumberColumnFilter", maxWidth: 130 },
      { headerName: "Outcome", valueGetter: (p) => (STATUS[p.data.status] || {}).label || p.data.status, minWidth: 160,
        cellRenderer: (p) => tagCell(p.value, (STATUS[p.data.status] || {}).tone || "busy") },
      { headerName: "Signed by", valueGetter: (p) => SIGNED_BY[p.data.decided_by] ?? p.data.decided_by, minWidth: 150 },
      { headerName: "PayPal reference", valueGetter: (p) => { const r = p.data.paypal || {}; return r.id ? `${r.api} ${r.id}${r.status ? ` (${r.item_status || r.status})` : ""}` : r.error ? `Error: ${r.error.name}` : ""; }, flex: 1.8, minWidth: 230 },
      { headerName: "Requested by", valueGetter: (p) => actorName(p.data), minWidth: 150 },
    ],
    onRowClicked: (event) => openDetail(event.data.id),
    overlayNoRowsTemplate: "The ledger is empty. It fills as the agent makes requests.",
  });
}

// ---- detail dialog -------------------------------------------------------------

const EVENT_TEXT = {
  requested: "Request received", policy_evaluated: "Policy checked", ai_reviewed: "AI review finished", auto_approved: "Approved without the owner",
  held_for_owner: "Held for the owner", blocked: "Blocked", owner_approved: "Owner signed", owner_rejected: "Owner refused",
  paypal_executed: "PayPal accepted", paypal_order_created: "PayPal order created", paypal_failed: "PayPal returned an error",
  paypal_status: "PayPal status update", retry: "Retry requested", recovered_after_restart: "Recovered after a restart",
};

async function openDetail(id) {
  let p;
  try { p = await api(`/api/payments/${encodeURIComponent(id)}`); } catch (error) { toast(error.message); return; }
  const meta = STATUS[p.status] || { label: p.status, tone: "busy" };
  const checks = p.checks.map((c) => `<li><span class="tag" data-tone="${{ pass: "ok", escalate: "hold", deny: "stop" }[c.status]}">${{ pass: "Passed", escalate: "Owner", deny: "Refused" }[c.status]}</span><span><b>${esc(c.label)}.</b> ${esc(c.detail)}</span></li>`).join("");
  const signals = (p.review && p.review.signals && p.review.signals.length) ? `<p class="hint">What the AI review noticed: ${esc(p.review.signals.join("; "))}.</p>` : "";
  const trail = p.audit.map((a) => `<li><time>${esc(clock(a.ts))}</time><span>${esc(EVENT_TEXT[a.event] || a.event)} <span class="hash">by ${esc(a.actor)}</span></span><span class="hash" title="${esc(a.hash)}">#${a.seq} ${esc(a.hash.slice(0, 10))}</span></li>`).join("");
  $("detail-body").innerHTML = `
    <p class="detail-title">${esc(RAIL[p.rail].verb)} ${esc(p.counterparty_name || p.counterparty_email)} ${esc(money(p.amount, p.currency))} <span class="tag" data-tone="${meta.tone}">${esc(meta.label)}</span></p>
    <p class="hint">${esc(p.id)}, requested by ${esc(actorName(p))} at ${esc(new Date(p.created_at).toLocaleString())}. ${esc(p.purpose)}</p>
    ${p.justification ? `<h3>What the agent said</h3><p>${esc(p.justification)}</p>` : ""}
    <h3>Checks</h3><ul class="checks">${checks}</ul>${signals}
    <h3>PayPal</h3>${paypalLine(p) || `<p class="hint">Nothing was sent to PayPal.</p>`}
    <h3>Source record the request was based on</h3>
    <div class="record">${esc(p.evidence || "No source record was attached.")}</div>
    <h3>Audit trail</h3><ul class="trail">${trail}</ul>`;
  if (!$("detail").open) $("detail").showModal();
}

// ---- live updates --------------------------------------------------------------

function connect() {
  const stream = new EventSource("/api/events");
  stream.addEventListener("payment", (e) => upsertPayment(JSON.parse(e.data)));
  stream.addEventListener("mandate", (e) => setMandate(JSON.parse(e.data)));
  stream.addEventListener("reset", () => loadAll());
  stream.addEventListener("agent", (e) => {
    const note = JSON.parse(e.data);
    state.notes[note.item_id] = note.text;
    renderInbox();
  });
  stream.addEventListener("inbox", async (e) => {
    const change = JSON.parse(e.data);
    const item = state.inbox.find((i) => i.id === change.id);
    if (!item || change.added) { state.inbox = await api("/api/inbox"); }
    else {
      item.status = change.status;
      if (change.summary) { item.summary = change.summary; state.notes[item.id] = change.summary; }
    }
    renderInbox();
  });
}

async function loadAll() {
  const [mandate, inbox, payments] = await Promise.all([api("/api/mandate"), api("/api/inbox"), api("/api/payments")]);
  state.payments = new Map(payments.map((p) => [p.id, p]));
  state.inbox = inbox;
  state.notes = {};
  state.live = false;
  setMandate(mandate);
  renderInbox();
  renderSlips();
  renderQueue();
  state.ledger.setGridOption("rowData", payments);
  state.live = true;
  await Promise.all([loadStats(), loadStatus()]);
}

// ---- wiring ----------------------------------------------------------------------

function wire() {
  $("compile").addEventListener("click", (e) => busy(e.currentTarget, async () => {
    $("compile-note").className = "note";
    $("compile-note").textContent = "Reading your rules";
    try {
      setMandate(await api("/api/mandate/compile", { method: "POST", body: { text: $("mandate-text").value } }));
      $("compile-note").textContent = "Rules updated and in force.";
    } catch (error) {
      $("compile-note").className = "note bad";
      $("compile-note").textContent = error.message;
    }
  }));

  $("mandate-active").addEventListener("change", async (e) => {
    try {
      setMandate(await api("/api/mandate", { method: "PUT", body: { ...state.mandate, active: e.target.checked } }));
      toast(e.target.checked ? "Mandate in force." : "Mandate paused. Every request will be blocked until you turn it back on.");
    } catch (error) { toast(error.message); }
  });

  $("run-all").addEventListener("click", (e) => busy(e.currentTarget, async () => {
    const result = await api("/api/inbox/run-all", { method: "POST" });
    if (!result.queued.length) toast("The agent has already handled everything in the inbox.");
  }));

  $("inbox").addEventListener("click", (e) => {
    const button = e.target.closest("[data-run]");
    if (button) busy(button, () => api(`/api/inbox/${encodeURIComponent(button.dataset.run)}/run`, { method: "POST" }));
  });

  $("command-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = $("command-text").value.trim();
    if (!text) return;
    try { await api("/api/command", { method: "POST", body: { text } }); $("command-text").value = ""; } catch (error) { toast(error.message); }
  });

  $("redteam").addEventListener("click", (e) => busy(e.currentTarget, async () => {
    const p = await api("/api/redteam", { method: "POST" });
    toast(p.status === "blocked" ? "The hijacked request was blocked. Nothing reached PayPal." : `The hijacked request ended as: ${(STATUS[p.status] || {}).label || p.status}.`);
  }));

  let armed = false;
  $("reset").addEventListener("click", async (e) => {
    const button = e.currentTarget;
    if (!armed) {
      armed = true; button.textContent = "Click again to clear everything";
      setTimeout(() => { armed = false; button.textContent = "Reset demo"; }, 4000);
      return;
    }
    armed = false; button.textContent = "Reset demo";
    await busy(button, () => api("/api/demo/reset", { method: "POST" }));
  });

  document.body.addEventListener("click", (e) => {
    const target = e.target.closest("[data-approve],[data-reject],[data-retry],[data-sync],[data-open]");
    if (!target) return;
    const d = target.dataset;
    if (d.open) return openDetail(d.open);
    const [action, id] = d.approve ? ["approve", d.approve] : d.reject ? ["reject", d.reject] : d.retry ? ["retry", d.retry] : ["sync", d.sync];
    busy(target, async () => {
      const p = await api(`/api/payments/${encodeURIComponent(id)}/${action}`, { method: "POST", body: {} });
      upsertPayment(p);
      if (action === "sync") toast(`PayPal says: ${(p.paypal && (p.paypal.item_status || p.paypal.status)) || "no change"}.`);
    });
  });

  $("st-chain").addEventListener("click", async () => {
    try {
      const chain = await api("/api/audit/verify");
      setChain(chain);
      toast(chain.ok ? `Audit log verified. ${chain.count} entries, every hash matches. Head ${chain.head.slice(0, 12)}.` : `Audit log has been altered at entry ${chain.broken_at}.`);
    } catch (error) { toast(error.message); }
  });

  $("st-llm").addEventListener("click", () => loadStatus(true));
  $("st-paypal").addEventListener("click", () => loadStatus(true));

  $("ledger-search").addEventListener("input", (e) => state.ledger.setGridOption("quickFilterText", e.target.value));
  $("ledger-export").addEventListener("click", () => state.ledger.exportDataAsCsv({ fileName: "countersign-ledger.csv" }));
}

(async function start() {
  buildRulesGrid();
  buildLedger();
  wire();
  try {
    await loadAll();
  } catch (error) {
    toast(`Could not load: ${error.message}`);
  }
  connect();
  setInterval(() => loadStatus(), 30000);
  const opened = new URLSearchParams(location.search).get("payment");
  if (opened) { openDetail(opened); history.replaceState(null, "", "/"); }
})();
