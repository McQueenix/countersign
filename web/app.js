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
  scheduled:        { label: "Undo window",        tone: "busy", stamp: "Sending soon" },
  executed:         { label: "Sent",               tone: "ok",   stamp: "Sent" },
  awaiting_payer:   { label: "Approve in PayPal",  tone: "hold", stamp: "At PayPal" },
  awaiting_client:  { label: "Waits for client",   tone: "hold", stamp: "Pay when paid" },
  pending_approval: { label: "Waiting for you",    tone: "hold", stamp: "Held" },
  blocked:          { label: "Blocked",            tone: "stop", stamp: "Blocked" },
  rejected:         { label: "Refused by you",     tone: "stop", stamp: "Refused" },
  failed:           { label: "PayPal error",       tone: "stop", stamp: "Failed" },
  reversed:         { label: "Reversed",           tone: "stop", stamp: "Reversed" },
};

const RAIL = {
  payout:   { label: "Payout",   verb: "Pay",     api: "PayPal Payouts" },
  invoice:  { label: "Invoice",  verb: "Bill",    api: "PayPal Invoicing" },
  purchase: { label: "Purchase", verb: "Buy from", api: "PayPal Checkout" },
};

const SIGNED_BY = { "countersign": "Expired unsigned", "policy": "Policy", "policy+ai": "Policy and AI review", "ai_reviewer": "AI review", "owner": "You", "": "" };

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
    if (state.preview !== s.preview) { state.preview = s.preview; renderSlips(); }
    document.title = `Countersign for ${s.business}`;
    setPill("st-paypal", s.paypal.ok, s.preview ? "PayPal stand-in" : "PayPal sandbox", s.paypal.detail);
    setPill("st-llm", s.llm.ok, s.llm.ok ? `AI: ${s.llm.model}` : "AI model not answering", s.llm.detail);
    if (deep && !s.llm.ok) toast(`AI model: ${s.llm.detail}`);
    if (deep && !s.paypal.ok) toast(`PayPal: ${s.paypal.detail}`);
    setChain(s.chain);
    $("st-balance").hidden = s.balance === null || s.balance === undefined;
    if (!$("st-balance").hidden) setPill("st-balance", true, `${s.preview ? "Stand-in" : "PayPal"} balance ${money(s.balance, "USD")}`, "Available balance from PayPal's reporting API. A payout that would overdraw it is held for you.");
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
  { key: "undo_seconds", rule: "Undo window before sending", type: "seconds" },
  { key: "unusual_factor", rule: "Hold if above the payee's usual by", type: "factor" },
  { key: "max_per_hour", rule: "Most requests in an hour", type: "count", optional: true },
  { key: "active_hours", rule: "Working hours (HH:MM-HH:MM)", type: "text", optional: true },
  { key: "timezone", rule: "Time zone", type: "text" },
  { key: "quarantine_after", rule: "Freeze the agent after blocks in 10 min", type: "count0" },
  { key: "new_payee_cap", rule: "First payment to someone new, at most", type: "money", optional: true },
  { key: "dual_control_over", rule: "Two people must sign over", type: "money", optional: true },
  { key: "require_registered_agents", rule: "Only agents with a key may ask", type: "bool" },
  { key: "lookalike_guard", rule: "Refuse lookalike addresses, hold changed details", type: "bool" },
  { key: "allowed_domains", rule: "Approve anyone at these domains", type: "domains" },
  { key: "category_budgets", rule: "Weekly budget per category (Marketing: 300)", type: "budgets" },
  { key: "hold_expires_hours", rule: "Refuse unsigned payments after", type: "hours", optional: true },
  { key: "confirm_new_payees", rule: "New payees confirm their own address", type: "bool" },
  { key: "remind_after_days", rule: "Remind unpaid clients every (days)", type: "count", optional: true },
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
  if (type === "seconds") return value ? `${value} seconds` : "Off";
  if (type === "factor") return value ? `${value} times` : "Off";
  if (type === "count") return value ? String(value) : "No limit";
  if (type === "count0") return value ? `${value} blocks` : "Off";
  if (type === "text") return value || (params.data.key === "active_hours" ? "Any time" : "");
  if (type === "domains") return value && value.length ? value.join(", ") : "None";
  if (type === "budgets") {
    const entries = Object.entries(value || {});
    return entries.length ? entries.map(([k, v]) => `${k}: ${money(v, state.mandate.currency)}`).join(", ") : "None";
  }
  if (type === "hours") return value ? `${value} hours` : "Never";
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
  if (type === "seconds" || type === "count0" || type === "count") {
    if ((raw === null || raw === "") && optional) return null;
    const n = Math.floor(Number(raw));
    return Number.isFinite(n) && n >= 0 ? n : params.oldValue;
  }
  if (type === "factor") {
    const n = Number(raw);
    return Number.isFinite(n) && n >= 0 ? n : params.oldValue;
  }
  if (type === "text") return String(raw || "").trim() || (optional ? null : params.oldValue);
  if (type === "domains") {
    if (Array.isArray(raw)) return raw;
    return String(raw || "").split(/[\s,;]+/).map((s) => s.trim().toLowerCase().replace(/^@/, "")).filter((s) => /^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(s));
  }
  if (type === "budgets") {
    if (raw && typeof raw === "object") return raw;
    const out = {};
    String(raw || "").split(/[,;\n]+/).forEach((part) => {
      const m = part.match(/^\s*([^:=]+?)\s*[:=]\s*\$?([\d,.]+)\s*$/);
      if (m) out[m[1].trim()] = Number(m[2].replace(/,/g, ""));
    });
    return out;
  }
  if (type === "hours") {
    if (raw === null || raw === "") return null;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? n : params.oldValue;
  }
  return raw;
}

function editorFor(params) {
  const { type } = params.data || {};
  if (type === "bool") return { component: "agCheckboxCellEditor" };
  if (type === "money") return { component: "agNumberCellEditor", params: { min: 0, precision: 2 } };
  if (type === "choice") return { component: "agSelectCellEditor", params: { values: ["escalate", "deny"] } };
  if (type === "date") return { component: "agDateStringCellEditor" };
  if (type === "emails") return { component: "agLargeTextCellEditor", popup: true, params: { maxLength: 2000, rows: 4, cols: 40 } };
  if (type === "seconds" || type === "count" || type === "count0") return { component: "agNumberCellEditor", params: { min: 0, precision: 0 } };
  if (type === "factor") return { component: "agNumberCellEditor", params: { min: 0, precision: 1 } };
  if (type === "hours") return { component: "agNumberCellEditor", params: { min: 0, precision: 1 } };
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
  parts.push(m.active ? "In force." : `<span class="paused">${!m.paused_reason ? "Paused." : m.paused_reason.includes("emergency stop") ? "Frozen by you." : "Agent quarantined."} Every request is blocked.</span>`);
  parts.push(`${esc(m.agent_name)} may send up to <b>${esc(money(m.per_payment_cap, cur))}</b> at a time`);
  parts.push(`you sign anything over <b>${esc(money(m.auto_approve_limit, cur))}</b>`);
  if (m.weekly_budget !== null) parts.push(`<b>${esc(money(m.weekly_budget, cur))}</b> in any 7 days`);
  parts.push(`<b>${m.allowed_payees.length}</b> approved ${m.allowed_payees.length === 1 ? "payee" : "payees"}`);
  if (m.expires_on) parts.push(`ends ${esc(m.expires_on)}`);
  if (boostActive(m)) parts.push(`<b class="boosted">raised to ${esc(money(m.boost_limit, cur))} until ${esc(new Date(m.boost_until).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}</b>`);
  return parts[0] + " " + parts.slice(1).join(", ") + ".";
}

function boostActive(m) {
  return m.boost_limit !== null && m.boost_limit !== undefined && m.boost_until && new Date(m.boost_until) > new Date();
}

function renderBoost(m) {
  const on = boostActive(m);
  $("boost-end").hidden = !on;
  $("boost-state").textContent = on
    ? `Raised to ${money(m.boost_limit, m.currency)} until ${new Date(m.boost_until).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}. Then your normal limits come back by themselves.`
    : "Raise the per-payment cap and your sign-off threshold for a set time. They drop back by themselves.";
}

function setMandate(mandate) {
  state.mandate = mandate;
  if (document.activeElement !== $("mandate-text")) $("mandate-text").value = mandate.text;
  $("mandate-active").checked = mandate.active;
  $("mandate-active-label").textContent = mandate.active ? "In force" : "Paused";
  $("mandate-summary").innerHTML = mandateSummary(mandate);
  renderBoost(mandate);
  $("quarantine").hidden = mandate.active || !mandate.paused_reason;
  $("quarantine-text").textContent = mandate.paused_reason || "";
  document.body.classList.toggle("frozen", !mandate.active && !!mandate.paused_reason);
  syncWhatIf();
  state.rules.setGridOption("rowData", ruleRows(mandate));
  loadHistory().catch(() => {});
}

// ---- mandate history ---------------------------------------------------------------

async function loadHistory() {
  const versions = await api("/api/mandate/history");
  const current = versions.length ? versions[0].sha256 : "";
  $("history-list").innerHTML = versions.slice(0, 8).map((v) => {
    const m = v.mandate;
    const when = new Date(v.saved_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    const what = `${money(m.per_payment_cap, m.currency)} cap, sign over ${money(m.auto_approve_limit, m.currency)}${m.active ? "" : ", paused"}`;
    return `<li><span><b>${esc(when)}</b> by ${esc(v.by)}<small>${esc(what)} · <code>${esc(v.sha256.slice(0, 12))}</code></small></span>
      ${v.sha256 === current ? `<span class="tag" data-tone="ok">In force</span>` : `<button class="button quiet" data-restore="${esc(v.sha256)}">Restore</button>`}</li>`;
  }).join("");
}

// ---- inbox -----------------------------------------------------------------

const ITEM_STATE = { new: "New", queued: "In line", working: "Agent is reading", done: "Handled", error: "Could not run" };
const CHANNEL = { task: "Project board", email: "Email", owner: "Your instruction" };

function quotesFor(itemId) {
  const quotes = [];
  state.payments.forEach((p) => { if (p.source_ref === itemId && p.review && p.review.quotes) quotes.push(...p.review.quotes); });
  return [...new Set(quotes)];
}

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
      <details><summary>Read the message${quotesFor(item.id).length ? ` <span class="tag" data-tone="stop">${quotesFor(item.id).length} suspicious passage${quotesFor(item.id).length > 1 ? "s" : ""}</span>` : ""}</summary><div class="item-body">${highlight(item.body, quotesFor(item.id))}</div></details>
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
  else if (p.decision === "expired") owner = { state: "deny", hand: "no answer", who: "Owner" };
  else if (p.status === "pending_approval") owner = { state: "wait", hand: "waiting for you", who: "Owner" };
  else if (p.status === "scheduled") owner = { state: "wait", hand: "you can still undo", who: "Owner" };
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
  if (p.status === "awaiting_client" && ref.release_on_invoice) {
    const inv = state.payments.get(ref.release_on_invoice);
    return `<div class="reason" data-status="escalate"><b>Pay when paid.</b> Signed. Goes to PayPal by itself as soon as ${esc(inv ? inv.counterparty_name || inv.counterparty_email : "the client")} pays${inv ? ` the ${esc(money(inv.amount, inv.currency))} invoice` : ""}.</div>`;
  }
  if (!ref.id) return "";
  const bits = [`${esc(RAIL[p.rail].api)} reference <b>${esc(ref.id)}</b>`];
  if (ref.status) bits.push(`status ${esc(ref.item_status || ref.status)}`);
  if (ref.invoice_number) bits.push(`invoice no. ${esc(ref.invoice_number)}`);
  if (ref.transaction_id) bits.push(`transaction ${esc(ref.transaction_id)}`);
  if (ref.capture_id) bits.push(`capture ${esc(ref.capture_id)}`);
  if (ref.reversal) bits.push(`<b>${esc(ref.reversal.action)}</b>${ref.reversal.refund_id ? ` (refund ${esc(ref.reversal.refund_id)})` : ""}`);
  if (ref.reminders) bits.push(`${ref.reminders} reminder${ref.reminders > 1 ? "s" : ""} sent`);
  if (ref.last_event) bits.push(`last PayPal event ${esc(ref.last_event)}`);
  let html = `<div class="paypal-ref">${bits.join(", ")}</div>`;
  if (p.status === "executed") html += `<a class="proof-link" href="/verify/${encodeURIComponent(p.id)}" target="_blank" rel="noopener">Open the signed proof</a>`;
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
  if (p.status === "scheduled") {
    return `<button class="button primary" data-undo="${esc(p.id)}">Undo</button><button class="button quiet" data-sendnow="${esc(p.id)}">Send now</button><span class="countdown" data-release="${esc(ref.scheduled_for || "")}"></span>`;
  }
  if (p.status === "failed") return `<button class="button" data-retry="${esc(p.id)}">Try again</button>`;
  if (p.status === "executed" && ref.id) {
    const reverse = { payout: "Recall payout (if unclaimed)", purchase: "Refund", invoice: "Cancel invoice" }[p.rail];
    const canReverse = p.rail !== "purchase" || ref.capture_id;
    const unpaid = p.rail === "invoice" && !["PAID", "CANCELLED", "MARKED_AS_PAID"].includes(ref.status);
    const chase = unpaid ? `<button class="button quiet" data-remind="${esc(p.id)}">Send a reminder</button>${state.preview ? `<button class="button quiet" data-clientpays="${esc(p.id)}">Client pays (preview)</button>` : ""}` : "";
    return `${chase}<button class="button quiet" data-sync="${esc(p.id)}">Check with PayPal</button>${canReverse ? `<button class="button danger-quiet" data-reverse="${esc(p.id)}">${reverse}</button>` : ""}`;
  }
  return "";
}

function renderSlip(p) {
  const meta = STATUS[p.status] || { label: p.status, tone: "busy", stamp: p.status };
  const landed = state.lastStatus[p.id] !== undefined && state.lastStatus[p.id] !== p.status;
  const first = state.lastStatus[p.id] === undefined && state.live;
  state.lastStatus[p.id] = p.status;
  const reasons = stops(p).map((c) => `<div class="reason" data-status="${esc(c.status)}"><b>${esc(c.label)}.</b> ${esc(c.detail)}</div>`).join("");
  const actions = slipActions(p);
  return `<article class="slip ${landed || first ? "fresh" : ""}" data-tone="${meta.tone}" data-id="${esc(p.id)}">
    <div class="slip-head">
      <span>${esc(RAIL[p.rail].label)} through ${esc(RAIL[p.rail].api)}</span>
      <span>${esc(clock(p.created_at))} <button class="slip-id" data-open="${esc(p.id)}">${esc(p.id)}</button></span>
    </div>
    <div class="slip-main">
      <div class="slip-party">${esc(RAIL[p.rail].verb)} ${esc(p.counterparty_name || p.counterparty_email)}<small>${esc(p.counterparty_email)}</small>${trustBadge(p.counterparty_email)}</div>
      <div class="slip-amount">${esc(money(p.amount, p.currency))}<small>${esc(p.currency)}</small></div>
      <div class="slip-purpose">${esc(p.purpose)}</div>
    </div>
    <div class="sign-row">
      <div class="signatures">${signatureBlocks(p)}</div>
      <div class="stamp ${landed || first ? "land" : ""}" data-tone="${meta.tone}">${esc(meta.stamp)}</div>
    </div>
    <div class="slip-foot">
      ${reasons}${paypalLine(p)}
      ${p.status === "blocked" || p.status === "rejected" ? `<a class="proof-link" href="/verify/${encodeURIComponent(p.id)}" target="_blank" rel="noopener">Open the signed proof of refusal</a>` : ""}
      ${actions ? `<div class="slip-actions">${actions}</div>` : ""}
      ${!reasons && !paypalLine(p) && !actions ? `<span class="paypal-ref">Checking the mandate and reading the source record.</span>` : ""}
    </div>
  </article>`;
}

function trustBadge(email) {
  const t = state.payees && state.payees.get(email);
  if (!t) return "";
  return `<span class="trust" data-trust="${esc(t.trust)}">${esc(t.trust_text)}${t.confirmed ? ", address confirmed" : ""}</span>`;
}

function openInvoices() {
  return sortedPayments().filter((p) => p.rail === "invoice" && p.status === "executed" && !["PAID", "CANCELLED", "MARKED_AS_PAID"].includes((p.paypal || {}).status));
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
    const dual = p.checks.some((c) => c.rule === "dual_control" && c.status === "escalate");
    const unknownPayee = p.checks.some((c) => c.rule === "known_payee" && c.status === "escalate");
    const signers = p.signers || [];
    const dualLine = dual && !inPayPal ? `<p class="dual">${signers.length ? `Signed by ${esc(signers.join(", "))}. One more person must sign.` : "Two different people must sign."}</p>
      <label class="signer">Signing as <input type="text" data-signer="${esc(p.id)}" value="${signers.length ? "" : "Owner"}" placeholder="Your name"></label>` : "";
    const expiry = state.mandate && state.mandate.hold_expires_hours && !inPayPal
      ? `<p class="expiry">Refused by itself ${esc(expiresIn(p.created_at, state.mandate.hold_expires_hours))} if nobody signs.</p>` : "";
    const needsConfirm = p.checks.some((c) => c.rule === "payee_confirmation" && c.status === "escalate")
      && !(state.payees && state.payees.get(p.counterparty_email) && state.payees.get(p.counterparty_email).confirmed);
    const confirmLine = needsConfirm && !inPayPal ? `<p class="confirm-line">${esc(p.counterparty_email)} has not confirmed this address yet.</p>
      <div class="row"><button class="button primary" data-confirmlink="${esc(p.id)}">Ask them to confirm</button><button class="button refuse" data-reject="${esc(p.id)}">Refuse</button></div>` : "";
    const invoices = p.rail === "payout" && !inPayPal && !needsConfirm ? openInvoices() : [];
    const payWhen = invoices.length ? `<div class="pay-when"><label>Or sign now and pay when a client pays
      <select data-after-for="${esc(p.id)}">${invoices.map((i) => `<option value="${esc(i.id)}">${esc(i.counterparty_name || i.counterparty_email)}, ${esc(money(i.amount, i.currency))}</option>`).join("")}</select></label>
      <button class="button quiet" data-paywhen="${esc(p.id)}">Sign, pay when paid</button></div>` : "";
    const remember = unknownPayee && !inPayPal ? `<button class="button quiet" data-remember="${esc(p.id)}">Sign and add to approved payees</button>` : "";
    return `<div class="ask">
      <div class="ask-amount">${esc(money(p.amount, p.currency))}</div>
      <div class="ask-party">${esc(RAIL[p.rail].verb)} ${esc(p.counterparty_name || p.counterparty_email)}, ${esc(p.purpose)}</div>
      ${inPayPal ? `<ul class="ask-why"><li>The order is ready. PayPal will ask you to confirm the payment.</li></ul>` : `<ul class="ask-why">${why}</ul>`}
      ${dualLine}${expiry}
      ${needsConfirm && !inPayPal ? confirmLine : `<div class="row">${slipActions(p)}${inPayPal ? "" : `<button class="button quiet" data-phone="${esc(p.id)}">Sign on phone</button>`}${remember}</div>${payWhen}`}
    </div>`;
  }).join("");
}

function expiresIn(createdAt, hours) {
  const left = new Date(createdAt).getTime() + hours * 3600 * 1000 - Date.now();
  if (left <= 0) return "any moment now";
  const minutes = Math.ceil(left / 60000);
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h ? `in ${h} h ${m} min` : `in ${m} min`;
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
  const c = s.cash || {};
  $("cash").hidden = c.balance === null || c.balance === undefined;
  if (!$("cash").hidden) {
    $("c-balance").textContent = money(c.balance, s.currency);
    $("c-in").textContent = `+ ${money(c.owed_to_you, s.currency)}`;
    $("c-out").textContent = `− ${money(c.going_out, s.currency)}`;
    $("c-after").textContent = money(c.after, s.currency);
    $("c-after").dataset.low = c.after < 0;
  }
  $("forecast").textContent = s.forecast ? s.forecast.text : "";
  $("forecast").dataset.warn = !!(s.forecast && s.forecast.days_left !== null && s.forecast.days_left < 7);
  $("cat-budgets").innerHTML = (s.categories || []).map((c) => {
    const share = c.limit ? Math.min(100, (c.used / c.limit) * 100) : 100;
    return `<li><span>${esc(c.category)}</span><span class="cat-bar"><i style="width:${share}%" data-high="${share >= 80}"></i></span><small>${esc(money(c.used, s.currency))} of ${esc(money(c.limit, s.currency))}</small></li>`;
  }).join("");
}

let statsTimer;
function refreshSoon() {
  clearTimeout(statsTimer);
  statsTimer = setTimeout(() => { loadStats().catch(() => {}); loadStatus(); loadPayees().then(() => { renderSlips(); renderQueue(); }).catch(() => {}); }, 250);
}

function upsertPayment(p) {
  const before = state.payments.get(p.id);
  state.payments.set(p.id, p);
  renderSlips();
  renderQueue();
  state.ledger.applyTransaction(before ? { update: [p] } : { add: [p], addIndex: 0 });
  state.ledger.setGridOption("pinnedBottomRowData", totalsRow());
  renderWire();
  chartsSoon();
  if (p.review && p.review.quotes && p.review.quotes.length && (!before || !before.review || !before.review.quotes)) renderInbox();
  if (state.live && (!before || before.status !== p.status)) pulse(p);
  if (state.live && p.status === "pending_approval" && (!before || before.status !== "pending_approval")) alertOwner(p);
  refreshSoon();
}

function tickCountdowns() {
  document.querySelectorAll("[data-release]").forEach((el) => {
    const left = Math.max(0, Math.ceil((new Date(el.dataset.release) - Date.now()) / 1000));
    el.textContent = left ? `Goes to PayPal in ${left}s` : "Sending now";
  });
}

// ---- agents ----------------------------------------------------------------------------

async function loadAgents() {
  const agents = await api("/api/agents");
  $("agent-list").innerHTML = agents.length ? agents.map((a) => `
    <li data-active="${a.active}">
      <span><b>${esc(a.name)}</b><small>key …${esc(a.key_hint)}${a.weekly_allowance !== null ? `, ${esc(money(a.used_this_week, "USD"))} of ${esc(money(a.weekly_allowance, "USD"))} this week` : ", no allowance set"}</small></span>
      ${a.active ? `<button class="button quiet" data-revoke="${esc(a.id)}">Revoke</button>` : `<span class="tag" data-tone="stop">Revoked</span>`}
    </li>`).join("") : `<li class="hint">No outside agents yet. The built-in Ops Agent does not need a key.</li>`;
}

// ---- standing orders -------------------------------------------------------------------

async function loadStanding() {
  const orders = await api("/api/standing-orders");
  $("standing-list").innerHTML = orders.length ? orders.map((o) => `
    <li data-active="${o.active}">
      <span><b>${esc(money(o.amount, "USD"))} to ${esc(o.name)}</b><small>${esc(o.purpose)}, every ${o.every_days} day${o.every_days > 1 ? "s" : ""}${o.active ? `, next ${esc(new Date(o.next_run).toLocaleDateString())}` : ", stopped"}</small></span>
      ${o.active ? `<span class="row"><button class="button quiet" data-so-run="${esc(o.id)}">Run now</button><button class="button quiet" data-so-stop="${esc(o.id)}">Stop</button></span>` : ""}
    </li>`).join("") : `<li class="hint">None yet.</li>`;
}

// ---- alerts ---------------------------------------------------------------------------

function alertsOn() {
  try { return localStorage.getItem("countersign-alerts") === "on"; } catch { return false; }
}

function renderNotifyButton() {
  $("notify").textContent = alertsOn() ? "Alerts on" : "Alert me";
  $("notify").dataset.on = alertsOn();
}

function chime() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    [660, 880].forEach((freq, i) => {
      const osc = ctx.createOscillator(); const gain = ctx.createGain();
      osc.frequency.value = freq; osc.connect(gain); gain.connect(ctx.destination);
      const t = ctx.currentTime + i * 0.16;
      gain.gain.setValueAtTime(0.0001, t); gain.gain.exponentialRampToValueAtTime(0.18, t + 0.02); gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.3);
      osc.start(t); osc.stop(t + 0.32);
    });
  } catch { /* no audio available */ }
}

function alertOwner(p) {
  if (!alertsOn()) return;
  chime();
  const text = `${money(p.amount, p.currency)} to ${p.counterparty_name || p.counterparty_email} needs your signature.`;
  try {
    if ("Notification" in window && Notification.permission === "granted") new Notification("Countersign", { body: text, tag: p.id });
  } catch { /* notifications unavailable */ }
}

// ---- approve from your phone ---------------------------------------------------------

async function showConfirmLink(button, id) {
  busy(button, async () => {
    const r = await api(`/api/payments/${encodeURIComponent(id)}/confirm-link`, { method: "POST" });
    $("confirm-message").value = r.message;
    $("confirm-url").href = r.url;
    $("confirm-url").textContent = r.url;
    $("confirm-dialog").showModal();
  });
}

async function loadPayees() {
  const list = await api("/api/payees");
  state.payees = new Map(list.map((p) => [p.email, p]));
  $("payee-list").innerHTML = list.length ? list.map((p) => `<li>
    <span class="payee-who"><b>${esc(p.name || p.email)}</b><small>${esc(p.email)}</small></span>
    <span class="trust" data-trust="${esc(p.trust)}">${esc(p.trust_text)}${p.confirmed ? ", address confirmed" : ""}</span>
    <span class="payee-sum">${p.paid_ytd ? `Paid ${esc(money(p.paid_ytd, "USD"))}` : ""}${p.paid_ytd && p.billed_ytd ? " · " : ""}${p.billed_ytd ? `Billed ${esc(money(p.billed_ytd, "USD"))}` : ""}${!p.paid_ytd && !p.billed_ytd ? "Nothing settled yet" : ""}</span>
    ${p.paid_ytd || p.billed_ytd ? `<a class="button quiet" href="/api/payees/${encodeURIComponent(p.email)}/statement" download>Statement</a>` : `<span></span>`}
  </li>`).join("") : `<li class="hint">Nobody yet. People appear here once the agent asks to pay or bill them.</li>`;
}

async function showPhoneLink(button, id) {
  await busy(button, async () => {
    const r = await api(`/api/payments/${encodeURIComponent(id)}/approval-link`, { method: "POST" });
    $("phone-qr").innerHTML = r.qr_svg;
    $("phone-url").href = r.url;
    $("phone-url").textContent = r.url;
    if (!$("phone").open) $("phone").showModal();
  });
}

// ---- insights (AG Charts) -------------------------------------------------------------

const charts = { outcomes: null, payees: null, timer: null };
const CHART_FONT = "Hanken Grotesk, Segoe UI, sans-serif";

function chartData() {
  const all = [...state.payments.values()];
  const bucket = (p) => p.status === "executed" ? "Sent through PayPal"
    : ["pending_approval", "awaiting_payer", "scheduled"].includes(p.status) ? "Waiting"
    : ["blocked", "rejected", "reversed"].includes(p.status) ? "Stopped" : "In progress";
  const counts = {};
  all.forEach((p) => { counts[bucket(p)] = (counts[bucket(p)] || 0) + 1; });
  const outcomes = ["Sent through PayPal", "Waiting", "Stopped", "In progress"].filter((k) => counts[k]).map((k) => ({ outcome: k, count: counts[k] }));
  const byPayee = {};
  all.forEach((p) => {
    const who = p.counterparty_name || p.counterparty_email;
    const row = byPayee[who] || (byPayee[who] = { payee: who.length > 22 ? who.slice(0, 21) + "…" : who, sent: 0, waiting: 0, stopped: 0 });
    const b = bucket(p);
    if (b === "Sent through PayPal") row.sent += p.amount; else if (b === "Waiting") row.waiting += p.amount; else if (b === "Stopped") row.stopped += p.amount;
  });
  const payees = Object.values(byPayee).sort((a, b) => (b.sent + b.waiting + b.stopped) - (a.sent + a.waiting + a.stopped)).slice(0, 7);
  return { outcomes, payees };
}

function chartOptions() {
  const { outcomes, payees } = chartData();
  const base = { background: { visible: false }, padding: { top: 8, right: 8, bottom: 8, left: 8 } };
  const fills = { "Sent through PayPal": "#0a7a4b", Waiting: "#d99a1c", Stopped: "#c0261a", "In progress": "#0061d9" };
  return {
    outcomes: {
      ...base, container: $("chart-outcomes"), data: outcomes,
      series: [{
        type: "donut", angleKey: "count", calloutLabelKey: "outcome", sectorLabelKey: "count",
        innerRadiusRatio: 0.62, fills: outcomes.map((o) => fills[o.outcome]), strokes: ["#ffffff"], strokeWidth: 2,
        calloutLabel: { fontFamily: CHART_FONT, fontSize: 12, color: "#001435" },
        sectorLabel: { fontFamily: CHART_FONT, fontWeight: "bold", color: "#ffffff" },
        innerLabels: [{ text: String(outcomes.reduce((n, o) => n + o.count, 0)), fontSize: 26, fontWeight: "bold", fontFamily: CHART_FONT, color: "#001435" },
                      { text: "requests", fontSize: 12, fontFamily: CHART_FONT, color: "#55607a" }],
      }],
      legend: { enabled: false },
    },
    payees: {
      ...base, container: $("chart-payees"), data: payees,
      series: [
        { type: "bar", xKey: "payee", yKey: "sent", yName: "Sent", stacked: true, fill: "#0a7a4b", cornerRadius: 3 },
        { type: "bar", xKey: "payee", yKey: "waiting", yName: "Waiting", stacked: true, fill: "#d99a1c", cornerRadius: 3 },
        { type: "bar", xKey: "payee", yKey: "stopped", yName: "Stopped", stacked: true, fill: "#c0261a", cornerRadius: 3 },
      ],
      axes: {
        x: { type: "category", label: { fontFamily: CHART_FONT, fontSize: 11, color: "#001435", rotation: -25 } },
        y: { type: "number", label: { fontFamily: CHART_FONT, fontSize: 11, color: "#55607a", formatter: ({ value }) => `$${value}` } },
      },
      legend: { position: "bottom", item: { label: { fontFamily: CHART_FONT, color: "#001435" } } },
    },
  };
}

function renderCharts() {
  if (!window.agCharts) { $("insights").hidden = true; return; }
  const options = chartOptions();
  if (!charts.outcomes) {
    charts.outcomes = agCharts.AgCharts.create(options.outcomes);
    charts.payees = agCharts.AgCharts.create(options.payees);
  } else {
    charts.outcomes.update(options.outcomes);
    charts.payees.update(options.payees);
  }
}

function chartsSoon() {
  clearTimeout(charts.timer);
  charts.timer = setTimeout(renderCharts, 300);
}

// ---- attack drill -------------------------------------------------------------------

function renderDrill(d) {
  const running = !!d.running;
  $("drill-run").disabled = running;
  $("drill-run").textContent = running ? "Attacking" : (d.results && d.results.length ? "Run another drill" : "Let the AI attack my agent");
  $("drill-status").className = d.error ? "note bad" : "note";
  $("drill-status").textContent = d.error ? `The drill could not run: ${d.error}`
    : running ? "Writing new attacks and sending them to the agent's inbox." : "";
  const results = d.results || [];
  $("drill-score").hidden = running || !results.length;
  if (results.length && !running) {
    $("drill-score").textContent = `${d.stopped} of ${d.total} stopped`;
    $("drill-score").dataset.perfect = d.stopped === d.total;
  }
  $("drill-results").innerHTML = results.map((r) => `
    <li data-outcome="${esc(r.outcome)}">
      <span class="drill-mark" aria-hidden="true">${r.stopped === "yes" ? "✓" : "!"}</span>
      <span><b>${esc(r.technique)}</b><small>${esc(r.subject)}</small><em>${esc(r.label)}</em></span>
    </li>`).join("");
}

// ---- live wire: where each request ends up --------------------------------------

const WIRE_STOPS = ["Agent", "Policy", "AI review", "You", "PayPal"];

function wireTarget(p) {
  if (p.status === "executed") return { stop: 4, tone: "ok" };
  if (p.status === "awaiting_payer") return { stop: 4, tone: "hold" };
  if (p.status === "failed") return { stop: 4, tone: "stop" };
  if (p.status === "pending_approval") return { stop: 3, tone: "hold" };
  if (p.status === "rejected") return { stop: 3, tone: "stop" };
  if (p.decision === "blocked_policy") return { stop: 1, tone: "stop" };
  if (p.decision === "blocked_ai") return { stop: 2, tone: "stop" };
  return null;
}

function renderWire() {
  const all = [...state.payments.values()];
  const count = (test) => all.filter(test).length;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  $("w-agent").textContent = plural(all.length, "request", "requests");
  $("w-policy").textContent = `${count((p) => p.decision === "blocked_policy")} stopped`;
  $("w-ai").textContent = `${count((p) => p.decision === "blocked_ai")} stopped`;
  $("w-you").textContent = `${count((p) => p.status === "pending_approval" || p.status === "awaiting_payer")} waiting`;
  $("w-paypal").textContent = `${count((p) => p.status === "executed")} sent`;
}

function pulse(p) {
  const target = wireTarget(p);
  if (!target || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const track = $("wire-track");
  const dot = document.createElement("span");
  dot.className = "wire-dot";
  dot.dataset.tone = target.tone;
  dot.style.left = "10%";
  track.appendChild(dot);
  const travel = 260 * target.stop;
  dot.getBoundingClientRect();
  dot.style.transitionDuration = `${travel}ms`;
  dot.style.left = `${10 + 20 * target.stop}%`;
  const node = track.querySelectorAll(".wire-node")[target.stop];
  setTimeout(() => {
    dot.classList.add("arrive");
    node.dataset.hit = target.tone;
    setTimeout(() => { dot.remove(); delete node.dataset.hit; }, 900);
  }, travel + 40);
}

// ---- what-if: replay past requests against draft limits ---------------------------

const whatIf = { dirty: false, timer: null };

function syncWhatIf() {
  if (whatIf.dirty || !state.mandate) return;
  $("wi-cap").value = state.mandate.per_payment_cap;
  $("wi-limit").value = state.mandate.auto_approve_limit;
  showWhatIfValues();
  $("wi-result").innerHTML = "";
  $("wi-apply").disabled = true;
  $("wi-reset").disabled = true;
}

function showWhatIfValues() {
  $("wi-cap-out").textContent = money(Number($("wi-cap").value), state.mandate.currency);
  $("wi-limit-out").textContent = money(Number($("wi-limit").value), state.mandate.currency);
}

function draftMandate() {
  return { ...state.mandate, per_payment_cap: Number($("wi-cap").value), auto_approve_limit: Number($("wi-limit").value) };
}

async function previewWhatIf() {
  try {
    const r = await api("/api/mandate/preview", { method: "POST", body: draftMandate() });
    if (!r.total) {
      $("wi-result").textContent = "Hand some work to the agent first. Then drag these to see what would change.";
      return;
    }
    const changes = r.rows.filter((row) => row.changed).slice(0, 3)
      .map((row) => `${esc(row.who)} ${esc(money(row.amount, row.currency))}: ${WHATIF_WORD[row.was]} becomes ${WHATIF_WORD[row.now]}`);
    $("wi-result").innerHTML =
      `<span class="tag" data-tone="ok">${r.counts.allow} sent without you</span> ` +
      `<span class="tag" data-tone="hold">${r.counts.escalate} wait for you</span> ` +
      `<span class="tag" data-tone="stop">${r.counts.deny} blocked</span>` +
      `<span class="wi-line">${r.changed ? `${r.changed} of ${r.total} past requests would be decided differently.` : `Same outcome for all ${r.total} past requests.`}</span>` +
      changes.map((c) => `<span class="wi-line wi-change">${c}</span>`).join("");
  } catch (error) {
    $("wi-result").textContent = error.message;
  }
}

const WHATIF_WORD = { allow: "sent without you", escalate: "waits for you", deny: "blocked" };

function onWhatIfInput(event) {
  const cap = $("wi-cap"), limit = $("wi-limit");
  if (Number(limit.value) > Number(cap.value)) {
    if (event.target === cap) limit.value = cap.value; else cap.value = limit.value;
  }
  whatIf.dirty = true;
  showWhatIfValues();
  $("wi-apply").disabled = false;
  $("wi-reset").disabled = false;
  clearTimeout(whatIf.timer);
  whatIf.timer = setTimeout(previewWhatIf, 140);
}

function totalsRow() {
  const all = [...state.payments.values()];
  const sent = all.filter((p) => p.status === "executed" && p.rail !== "invoice");
  const stopped = all.filter((p) => p.status === "blocked" || p.status === "rejected");
  return [{
    id: "totals", pinned: true,
    amount: sent.reduce((sum, p) => sum + p.amount, 0),
    purpose: `Paid out through PayPal (${sent.length}). Stopped: ${stopped.length}.`,
  }];
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
  if (score === null || score === undefined || score === "") return "";
  const wrap = document.createElement("span");
  wrap.className = "risk";
  const level = score >= 80 ? "high" : score >= 40 ? "mid" : "low";
  wrap.innerHTML = `<span class="risk-bar"><i data-level="${level}" style="width:${Math.max(4, score)}%"></i></span>${score}`;
  return wrap;
}

// Wrap each passage the AI reviewer quoted in a highlight. Quotes are checked server-side to exist in the text.
function highlight(text, quotes) {
  let html = esc(text);
  (quotes || []).forEach((q) => {
    const safe = esc(q);
    if (safe) html = html.split(safe).join(`<mark class="scam">${safe}</mark>`);
  });
  return html;
}

// Getters for ledger columns that have nothing to show on the pinned totals row.
const live = (getter) => (p) => (p.data && !p.data.pinned ? getter(p) : "");

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
    defaultColDef: { sortable: true, filter: true, resizable: true, flex: 1, minWidth: 110, enableCellChangeFlash: true },
    rowClassRules: { "row-stopped": (p) => !!p.data && (p.data.status === "blocked" || p.data.status === "rejected") },
    initialState: { sort: { sortModel: [{ colId: "created_at", sort: "desc" }] } },
    columnDefs: [
      { colId: "created_at", headerName: "Time", field: "created_at", minWidth: 185, filter: false,
        valueFormatter: (p) => (p.value ? new Date(p.value).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "Totals") },
      { headerName: "Type", valueGetter: live((p) => RAIL[p.data.rail].label), maxWidth: 120 },
      { headerName: "Counterparty", valueGetter: live((p) => (p.data.counterparty_name ? `${p.data.counterparty_name} <${p.data.counterparty_email}>` : p.data.counterparty_email)), flex: 1.8, minWidth: 220 },
      { headerName: "Amount", field: "amount", type: "rightAligned", filter: "agNumberColumnFilter", maxWidth: 140,
        valueFormatter: (p) => money(p.value, p.data.currency) },
      { headerName: "For", field: "purpose", flex: 1.8, minWidth: 200 },
      { headerName: "Category", field: "category", minWidth: 140, valueFormatter: (p) => (p.data && p.data.pinned ? "" : p.value || "Uncategorised") },
      { headerName: "AI risk", valueGetter: live((p) => (p.data.review ? p.data.review.risk_score ?? null : null)), cellRenderer: riskCell, filter: "agNumberColumnFilter", maxWidth: 130 },
      { headerName: "Outcome", valueGetter: live((p) => (STATUS[p.data.status] || {}).label || p.data.status), minWidth: 160,
        cellRenderer: (p) => (p.value ? tagCell(p.value, (STATUS[p.data.status] || {}).tone || "busy") : "") },
      { headerName: "Signed by", valueGetter: live((p) => SIGNED_BY[p.data.decided_by] ?? p.data.decided_by), minWidth: 150 },
      { headerName: "PayPal reference", valueGetter: live((p) => { const r = p.data.paypal || {}; return r.id ? `${r.api} ${r.id}${r.status ? ` (${r.item_status || r.status})` : ""}` : r.error ? `Error: ${r.error.name}` : ""; }), flex: 1.8, minWidth: 230 },
      { headerName: "Requested by", valueGetter: live((p) => actorName(p.data)), minWidth: 150 },
    ],
    onRowClicked: (event) => { if (event.data && !event.data.pinned) openDetail(event.data.id); },
    overlayNoRowsTemplate: "The ledger is empty. It fills as the agent makes requests.",
  });
}

// ---- detail dialog -------------------------------------------------------------

const EVENT_TEXT = {
  requested: "Request received", policy_evaluated: "Policy checked", ai_reviewed: "AI review finished", auto_approved: "Approved without the owner",
  held_for_owner: "Held for the owner", blocked: "Blocked", owner_approved: "Owner signed", owner_rejected: "Owner refused",
  paypal_executed: "PayPal accepted", paypal_order_created: "PayPal order created", paypal_failed: "PayPal returned an error",
  paypal_status: "PayPal status update", retry: "Retry requested", recovered_after_restart: "Recovered after a restart",
  proof_issued: "Signed proof issued",
  scheduled: "Waiting in the undo window", released: "Undo window over, sent", owner_undid: "Owner pressed Undo",
  owner_reversed: "Owner reversed it through PayPal", paypal_webhook: "Update pushed by PayPal",
  agent_quarantined: "Agent quarantined", hold_expired: "Nobody signed in time, refused", emergency_stop: "Emergency stop",
  mandate_restored: "Earlier mandate restored", pay_when_paid: "Signed, to be paid when the client pays",
  client_paid_released: "Client paid, sent", invoice_reminded: "Reminder sent through PayPal",
  confirmation_link_created: "Confirmation link created", payee_confirmed: "Payee confirmed the address", payee_denied: "Payee said the address is not theirs",
  approval_link_created: "Phone signing link created", first_signature: "First of two signatures",
  attack_drill: "Attack drill finished",
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
    <h3>What Countersign knows about this payee</h3>
    <p>${p.payee.paid_count ? `Paid ${p.payee.paid_count} time${p.payee.paid_count > 1 ? "s" : ""}, ${esc(money(p.payee.paid_total, p.currency))} in total, usually ${esc(money(p.payee.usual, p.currency))}.` : "Never paid before."}
      ${p.payee.stopped ? ` ${p.payee.stopped} request${p.payee.stopped > 1 ? "s" : ""} to this address ${p.payee.stopped > 1 ? "were" : "was"} stopped.` : ""}
      ${p.payee.first_seen ? ` First seen ${esc(new Date(p.payee.first_seen).toLocaleString())}.` : ""}</p>
    <h3>Checks</h3><ul class="checks">${checks}</ul>${signals}
    <h3>PayPal</h3>${paypalLine(p) || `<p class="hint">Nothing was sent to PayPal.</p>`}
    ${["executed", "blocked", "rejected"].includes(p.status) ? `<h3>Signed proof</h3><p><a href="/verify/${encodeURIComponent(p.id)}" target="_blank" rel="noopener">Open the proof anyone can check</a>.${p.status === "executed" && p.rail !== "purchase" ? ` The same link is written into the PayPal ${p.rail === "invoice" ? "invoice" : "payment"} note.` : ""}</p>` : ""}
    <h3>Source record the request was based on</h3>
    ${(p.review && p.review.quotes && p.review.quotes.length) ? `<p class="hint">Highlighted: the exact words the AI reviewer found suspicious.</p>` : ""}
    <div class="record">${p.evidence ? highlight(p.evidence, p.review && p.review.quotes) : "No source record was attached."}</div>
    <h3>Audit trail</h3><ul class="trail">${trail}</ul>
    <p class="detail-actions"><a class="button" href="/api/payments/${encodeURIComponent(p.id)}/evidence" download>Download the evidence pack (for disputes)</a></p>`;
  if (!$("detail").open) $("detail").showModal();
}

// ---- live updates --------------------------------------------------------------

function connect() {
  const stream = new EventSource("/api/events");
  stream.addEventListener("payment", (e) => upsertPayment(JSON.parse(e.data)));
  stream.addEventListener("mandate", (e) => setMandate(JSON.parse(e.data)));
  stream.addEventListener("reset", () => loadAll());
  stream.addEventListener("drill", (e) => renderDrill(JSON.parse(e.data)));
  stream.addEventListener("agents", () => loadAgents());
  stream.addEventListener("standing", () => loadStanding());
  stream.addEventListener("alert", (e) => {
    const alert = JSON.parse(e.data);
    if (alert.kind === "confirmation") {
      toast(alert.text);
      loadPayees().then(() => { renderSlips(); renderQueue(); }).catch(() => {});
      return;
    }
    toast(alert.kind === "quarantine" ? (alert.text.includes("emergency stop") ? "Everything is frozen." : "Countersign froze the agent after repeated blocked requests.") : alert.text);
    $("quarantine").scrollIntoView({ behavior: "smooth", block: "start" });
  });
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
  state.ledger.setGridOption("pinnedBottomRowData", totalsRow());
  renderWire();
  renderCharts();
  renderDrill(await api("/api/drill"));
  renderNotifyButton();
  $("plan-body").hidden = true;
  $("plan-empty").hidden = false;
  await Promise.all([loadAgents(), loadStanding(), loadPayees(), loadGoal(), loadPlans(), loadDocs()]);
  runOversight().catch(() => {});
  renderSlips();
  renderQueue();
  whatIf.dirty = false;
  syncWhatIf();
  state.live = true;
  await Promise.all([loadStats(), loadStatus()]);
}

// ---- payment run planner -----------------------------------------------------------------

const RISK_WORD = { low: "Low", medium: "Medium", high: "High" };

function renderGoal(g) {
  const top = Object.entries(g.priorities).filter(([, w]) => w >= 4).map(([c]) => c);
  const low = Object.entries(g.priorities).filter(([, w]) => w <= 1).map(([c]) => c).filter((c) => c !== "Client billing");
  $("goal-chips").innerHTML = `
    <div><dt>Buffer</dt><dd>${esc(money(g.buffer, "USD"))}</dd></div>
    <div><dt>Risk appetite</dt><dd>${esc(RISK_WORD[g.risk_appetite])}</dd></div>
    <div><dt>Pay first</dt><dd>${esc(top.join(", ") || "No preference")}</dd></div>
    <div><dt>Can wait</dt><dd>${esc(low.join(", ") || "Nothing")}</dd></div>
    <div><dt>Most per run</dt><dd>${g.max_payments}</dd></div>
    <div><dt>Counts on clients for</dt><dd>${g.horizon_days} days</dd></div>`;
  $("plan-scenario").value = g.scenario;
  $("plan-risk").value = g.risk_appetite;
  if (g.text && document.activeElement !== $("goal-text")) $("goal-text").value = g.text;
}

async function loadGoal() { renderGoal(await api("/api/planner/goal")); }

const planCharts = { frontier: null };

function renderFrontier(result) {
  if (!window.agCharts) { $("chart-frontier").textContent = ""; return; }
  const data = result.frontier.map((p) => ({ ...p, label: `${p.count} payment${p.count === 1 ? "" : "s"}, ${money(p.amount, "USD")}`, size: p.chosen ? 16 : 8 }));
  const options = {
    container: $("chart-frontier"), background: { visible: false }, data,
    series: [
      { type: "line", xKey: "risk", yKey: "value", xName: "Risk", yName: "Value", stroke: "#0061d9", marker: { enabled: false }, tooltip: { enabled: false } },
      { type: "bubble", xKey: "risk", yKey: "value", sizeKey: "size", labelKey: "label", fill: "#0061d9", stroke: "#001435",
        itemStyler: ({ datum }) => (datum.chosen ? { fill: "#3df58e", stroke: "#001435", strokeWidth: 3 } : {}),
        tooltip: { renderer: ({ datum }) => ({ heading: datum.label, data: [{ label: "Value", value: String(datum.value) }, { label: "Risk", value: String(datum.risk) }] }) } },
    ],
    axes: {
      x: { type: "number", title: { text: "Risk", fontFamily: CHART_FONT }, label: { fontFamily: CHART_FONT, color: "#55607a" } },
      y: { type: "number", title: { text: "Value of paying now", fontFamily: CHART_FONT }, label: { fontFamily: CHART_FONT, color: "#55607a" } },
    },
    legend: { enabled: false },
  };
  if (planCharts.frontier) planCharts.frontier.update(options);
  else planCharts.frontier = agCharts.AgCharts.create(options);
}

function renderPlan(record) {
  state.plan = record;
  const r = record.result;
  $("plan-empty").hidden = true;
  $("plan-body").hidden = false;
  const paying = r.rows.filter((x) => x.pay_today);
  $("plan-figures").innerHTML = `
    <div><dt>Pay today</dt><dd>${paying.length} · ${esc(money(r.plan.amount, "USD"))}</dd></div>
    <div><dt>Waiting</dt><dd>${r.rows.length - paying.length}</dd></div>
    <div><dt>Cash to plan with</dt><dd>${esc(money(r.cash_available, "USD"))}</dd></div>
    <div><dt>Left once clients pay</dt><dd>${esc(money(r.cash_after, "USD"))}</dd></div>`;
  const ex = record.explanation;
  $("plan-explain").innerHTML = ex ? `${esc(ex.summary)}` : "Nothing is waiting for your signature, so there is nothing to plan.";
  $("plan-rows").innerHTML = r.rows.map((x) => `<li data-pay="${x.pay_today}">
      <span class="plan-mark">${x.pay_today ? "Pay today" : "Wait"}</span>
      <span class="plan-who"><b>${esc(x.who)}</b> ${esc(money(x.amount, "USD"))}<small>${esc(x.why)}</small></span>
      <button class="slip-id" data-open="${esc(x.id)}">${esc(x.id)}</button></li>`).join("") || `<li class="hint">Nothing is waiting for you.</li>`;
  $("plan-sens").innerHTML = r.sensitivity.map((s) => `<li>${esc(s)}</li>`).join("");
  $("plan-scenarios").innerHTML = `<tr><th>Scenario</th><th>Cash to plan with</th><th>Pays</th></tr>` + r.scenarios.map((s) =>
    `<tr data-current="${s.current}"><td>${esc(s.label)}</td><td>${esc(money(s.cash_available, "USD"))}</td><td>${s.count} · ${esc(money(s.amount, "USD"))}</td></tr>`).join("");
  $("plan-fingerprint").textContent = `Inputs fingerprint ${r.inputs_sha256.slice(0, 16)}. The same inputs always give the same plan.`;
  $("plan-apply").disabled = record.status === "applied" || !paying.length;
  $("plan-apply").textContent = record.status === "applied" ? "Signed" : `Sign this plan (${paying.length})`;
  renderFrontier(r);
}

async function loadPlans() {
  const plans = await api("/api/planner/plans");
  $("plan-history").innerHTML = plans.slice(0, 5).map((p) => {
    const when = new Date(p.created_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    const c = p.check;
    const check = c ? `<small>Planned ${c.planned} (${esc(money(c.planned_amount, "USD"))}), sent ${c.sent} (${esc(money(c.sent_amount, "USD"))}). Expected from clients ${esc(money(c.expected_inflow, "USD"))}, received ${esc(money(c.received_inflow, "USD"))}.</small>` : `<small>Proposed, not signed.</small>`;
    return `<li><b>${esc(when)}</b> ${p.pay.length} to pay${check}</li>`;
  }).join("") || `<li class="hint">No plans yet.</li>`;
}

// ---- mandate oversight check ---------------------------------------------------------------

function fixLabel(fix) {
  if (!fix) return "";
  const v = typeof fix.value === "number" ? money(fix.value, "USD") : fix.value;
  return {
    category_budgets: `Set a ${fix.key} budget of ${v} a week`, dual_control_over: `Two people sign over ${v}`,
    per_payment_cap: `Lower the cap to ${v}`, weekly_budget: `Set the weekly limit to ${v}`, daily_budget: `Set a daily limit of ${v}`,
    active_hours: `Pay only between ${v}`, allowed_domains: `Approve ${v}`,
  }[fix.field] || "Fix it";
}

async function loadDocs() {
  const docs = await api("/api/policy-docs");
  $("doc-list").innerHTML = docs.map((d) => `<li>
    <details><summary><b>${esc(d.name)}</b> <small>${d.chunks.length} sections</small></summary>
      <p class="hint">${esc(d.summary.overview)}</p>
      <ul class="doc-index">${d.summary.sections.map((s) => `<li><b>${esc(s.title)}</b> ${esc(s.summary)}<small>${s.keywords.map(esc).join(" · ")}</small></li>`).join("")}</ul>
    </details>
    <button class="button quiet" data-doc-remove="${esc(d.id)}">Remove</button></li>`).join("") || `<li class="hint">No policy documents yet.</li>`;
}

async function runOversight() {
  const r = await api("/api/oversight");
  const s = r.stats;
  $("oversight-stats").textContent = `${s.open} open, ${s.intended} intended · ${s.documents} document${s.documents === 1 ? "" : "s"}, ${s.sections} sections · map of ${s.nodes} items and ${s.edges} links`;
  $("findings").innerHTML = r.findings.map((f) => `<li data-kind="${esc(f.kind)}" data-intended="${!!f.intended}">
      <span class="tag" data-tone="${f.kind === "Conflict" ? "stop" : f.kind === "Silent default" ? "busy" : "hold"}">${esc(f.kind)}</span>
      <b>${esc(f.title)}</b>
      <p>${esc(f.detail)}</p>
      <small>From: ${f.evidence.map(esc).join("; ")}</small>
      ${f.intended ? `<p class="intended">You marked this as intended: ${esc(f.intended)} <button class="link" data-unintend="${esc(f.id)}">Undo</button></p>` : `
      <div class="row">
        ${f.fix ? `<button class="button primary" data-fix="${esc(f.id)}">${esc(fixLabel(f.fix))}</button>` : ""}
        <input type="text" data-note-for="${esc(f.id)}" placeholder="Why it is fine (optional)" aria-label="Why this is intended">
        <button class="button quiet" data-intend="${esc(f.id)}">This is intended</button>
      </div>${f.fix && f.fix.note ? `<small>${esc(f.fix.note)}</small>` : ""}`}
    </li>`).join("") || `<li class="hint">No gaps found. Your rules cover your policy and what the agent does.</li>`;
}

function wirePlannerAndOversight() {
  $("goal-form").addEventListener("submit", (e) => {
    e.preventDefault();
    busy(e.submitter || $("goal-form").querySelector("button"), async () => {
      renderGoal(await api("/api/planner/goal", { method: "POST", body: { text: $("goal-text").value } }));
      toast("Goals set. Make a plan to use them.");
    });
  });
  $("plan-make").addEventListener("click", (e) => busy(e.currentTarget, async () => {
    renderPlan(await api("/api/planner/plan", { method: "POST", body: { scenario: $("plan-scenario").value, risk_appetite: $("plan-risk").value } }));
    loadPlans().catch(() => {});
  }));
  $("plan-apply").addEventListener("click", (e) => busy(e.currentTarget, async () => {
    const record = await api(`/api/planner/plans/${encodeURIComponent(state.plan.id)}/apply`, { method: "POST", body: { signer: $("plan-signer").value.trim() || "Owner" } });
    renderPlan(record);
    const skipped = record.results.filter((r) => r.status === "skipped");
    toast(`Plan signed. ${record.results.length - skipped.length} payment${record.results.length - skipped.length === 1 ? "" : "s"} on their way${skipped.length ? `, ${skipped.length} skipped: ${skipped[0].reason}` : ""}.`);
    loadPlans().catch(() => {});
  }));
  $("plan-rows").addEventListener("click", (e) => { const b = e.target.closest("[data-open]"); if (b) openDetail(b.dataset.open); });

  $("doc-form").addEventListener("submit", (e) => {
    e.preventDefault();
    busy(e.submitter || $("doc-form").querySelector("button"), async () => {
      await api("/api/policy-docs", { method: "POST", body: { name: $("doc-name").value, text: $("doc-text").value } });
      $("doc-text").value = ""; $("doc-name").value = "";
      await loadDocs(); await runOversight();
    });
  });
  $("doc-sample").addEventListener("click", (e) => busy(e.currentTarget, async () => {
    await api("/api/policy-docs/sample", { method: "POST" });
    await loadDocs(); await runOversight();
    toast("Sample spending policy loaded and checked.");
  }));
  $("doc-list").addEventListener("click", (e) => {
    const b = e.target.closest("[data-doc-remove]");
    if (b) busy(b, async () => { await api(`/api/policy-docs/${encodeURIComponent(b.dataset.docRemove)}`, { method: "DELETE" }); await loadDocs(); await runOversight(); });
  });
  $("oversight-run").addEventListener("click", (e) => busy(e.currentTarget, runOversight));
  $("findings").addEventListener("click", (e) => {
    const fix = e.target.closest("[data-fix]");
    const intend = e.target.closest("[data-intend]");
    const undo = e.target.closest("[data-unintend]");
    if (fix) busy(fix, async () => { setMandate(await api("/api/oversight/fix", { method: "POST", body: { text: fix.dataset.fix } })); toast("Rule added. It applies to the next request."); await runOversight(); });
    if (intend) busy(intend, async () => {
      const note = document.querySelector(`[data-note-for="${CSS.escape(intend.dataset.intend)}"]`);
      await api("/api/oversight/intended", { method: "POST", body: { name: intend.dataset.intend, text: note ? note.value : "" } });
      toast("Noted. Countersign will remember why this is fine."); await runOversight();
    });
    if (undo) busy(undo, async () => { await api(`/api/oversight/intended/${encodeURIComponent(undo.dataset.unintend)}`, { method: "DELETE" }); await runOversight(); });
  });
  $("rules-ask-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("rules-ask-text").value.trim();
    if (!q) return;
    const box = $("rules-answer");
    box.hidden = false;
    box.innerHTML = `<p class="hint">Reading your rules.</p>`;
    api("/api/rules/ask", { method: "POST", body: { text: q } }).then((r) => {
      box.innerHTML = `<p class="ask-q">${esc(r.question)}</p><p class="ask-a">${esc(r.answer)}</p>${r.sections.length ? `<div class="ask-cites"><span class="hint">Read from</span>${r.sections.map((s) => `<span class="chip">${esc(s.document)} > ${esc(s.section)}</span>`).join("")}</div>` : ""}`;
    }).catch((error) => { box.innerHTML = `<p class="note bad">${esc(error.message)}</p>`; });
  });
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

  $("trick-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = $("trick-text").value.trim();
    if (!text) { toast("Write the email first."); return; }
    try {
      await api("/api/inbox/email", { method: "POST", body: { body: text } });
      $("trick-text").value = "";
      toast("Delivered to the inbox as an email from a stranger. Watch what the agent and the guard do with it.");
    } catch (error) { toast(error.message); }
  });

  $("wi-cap").addEventListener("input", onWhatIfInput);
  $("wi-limit").addEventListener("input", onWhatIfInput);
  $("wi-reset").addEventListener("click", () => { whatIf.dirty = false; syncWhatIf(); });
  $("wi-apply").addEventListener("click", (e) => busy(e.currentTarget, async () => {
    const next = draftMandate();
    whatIf.dirty = false;
    setMandate(await api("/api/mandate", { method: "PUT", body: next }));
    toast("New limits are in force.");
  }));

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
    const target = e.target.closest("[data-approve],[data-reject],[data-retry],[data-sync],[data-open],[data-undo],[data-sendnow],[data-reverse],[data-phone],[data-remember],[data-remind],[data-confirmlink],[data-paywhen],[data-clientpays]");
    if (!target) return;
    const d = target.dataset;
    if (d.open) return openDetail(d.open);
    if (d.confirmlink) return showConfirmLink(target, d.confirmlink);
    if (d.remind || d.clientpays || d.paywhen) {
      return busy(target, async () => {
        if (d.remind) {
          const p = await api(`/api/payments/${encodeURIComponent(d.remind)}/remind`, { method: "POST" });
          upsertPayment(p);
          toast(`PayPal sent ${p.counterparty_name || p.counterparty_email} a reminder.`);
        } else if (d.clientpays) {
          upsertPayment(await api(`/api/preview/invoices/${encodeURIComponent(d.clientpays)}/paid`, { method: "POST" }));
          toast("The client paid. Anything waiting on this invoice is going to PayPal now.");
        } else {
          const select = document.querySelector(`[data-after-for="${CSS.escape(d.paywhen)}"]`);
          const signerInput = document.querySelector(`[data-signer="${CSS.escape(d.paywhen)}"]`);
          const p = await api(`/api/payments/${encodeURIComponent(d.paywhen)}/approve`, { method: "POST", body: { signer: (signerInput && signerInput.value.trim()) || "Owner", after_invoice: select.value } });
          upsertPayment(p);
          toast(p.status === "awaiting_client" ? "Signed. It goes out by itself when the client pays." : p.status === "pending_approval" ? "Signed. One more person must sign." : "The invoice is already paid, so it went now.");
        }
      });
    }
    if (d.phone) return showPhoneLink(target, d.phone);
    if (d.reverse && !d.armed) {
      d.armed = "1";
      const label = target.textContent;
      target.textContent = "Click again to confirm";
      setTimeout(() => { delete d.armed; target.textContent = label; }, 4000);
      return;
    }
    const [action, id] = d.approve ? ["approve", d.approve] : d.remember ? ["approve", d.remember] : d.reject ? ["reject", d.reject] : d.retry ? ["retry", d.retry]
      : d.undo ? ["undo", d.undo] : d.sendnow ? ["send-now", d.sendnow] : d.reverse ? ["reverse", d.reverse] : ["sync", d.sync];
    const signerInput = document.querySelector(`[data-signer="${CSS.escape(id)}"]`);
    const body = action === "approve" ? { signer: (signerInput && signerInput.value.trim()) || "Owner", remember_payee: !!d.remember } : {};
    busy(target, async () => {
      const p = await api(`/api/payments/${encodeURIComponent(id)}/${action}`, { method: "POST", body });
      if (action === "approve" && p.status === "pending_approval") toast(`Signed by ${body.signer}. One more person must sign.`);
      if (d.remember) toast(`${p.counterparty_email} is now on your approved list.`);
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

  async function askLedger(question) {
    const box = $("ask-answer");
    box.hidden = false;
    box.innerHTML = `<p class="hint">Reading the ledger.</p>`;
    try {
      const r = await api("/api/ask", { method: "POST", body: { text: question } });
      const cites = r.cites.map((c) => `<button type="button" class="chip cite" data-open="${esc(c.id)}">${esc(c.label)}<span class="tag" data-tone="${(STATUS[c.status] || {}).tone || "busy"}">${esc((STATUS[c.status] || {}).label || c.status)}</span></button>`).join("");
      box.innerHTML = `<p class="ask-q">${esc(r.question)}</p><p class="ask-a">${esc(r.answer)}</p>${cites ? `<div class="ask-cites"><span class="hint">Based on</span>${cites}</div>` : ""}`;
    } catch (error) {
      box.innerHTML = `<p class="note bad">${esc(error.message)}</p>`;
    }
  }
  $("briefing").addEventListener("click", async () => {
    const box = $("ask-answer");
    box.hidden = false;
    box.innerHTML = `<p class="hint">Writing your briefing.</p>`;
    try {
      const r = await api("/api/briefing", { method: "POST" });
      const f = r.figures;
      const figures = `<dl class="brief-figures">
        <div><dt>Requests</dt><dd>${f.requests}</dd></div>
        <div><dt>Paid out</dt><dd>${esc(money(f.paid_out, f.currency))}</dd></div>
        <div><dt>Billed</dt><dd>${esc(money(f.billed, f.currency))}</dd></div>
        <div><dt>Stopped</dt><dd>${esc(money(f.stopped, f.currency))}</dd></div>
        <div><dt>Waiting for you</dt><dd>${f.waiting}</dd></div></dl>`;
      const s = r.summary;
      const cites = s && s.cites ? s.cites.map((c) => `<button type="button" class="chip cite" data-open="${esc(c.id)}">${esc(c.label)}</button>`).join("") : "";
      box.innerHTML = `<p class="ask-q">Last 24 hours</p>${figures}${s ? `<p class="ask-a">${esc(s.answer)}</p>` : `<p class="ask-a">Nothing happened in the last 24 hours.</p>`}${cites ? `<div class="ask-cites"><span class="hint">Based on</span>${cites}</div>` : ""}`;
    } catch (error) {
      box.innerHTML = `<p class="note bad">${esc(error.message)}</p>`;
    }
  });

  $("freeze").addEventListener("click", (e) => {
    const button = e.currentTarget;
    if (!button.dataset.armed) {
      button.dataset.armed = "1";
      button.textContent = "Press again to freeze";
      setTimeout(() => { delete button.dataset.armed; button.textContent = "Freeze everything"; }, 4000);
      return;
    }
    delete button.dataset.armed;
    button.textContent = "Freeze everything";
    busy(button, async () => {
    const r = await api("/api/emergency-stop", { method: "POST" });
    setMandate(r.mandate);
    toast(`Everything is frozen. ${r.cancelled} payment${r.cancelled === 1 ? "" : "s"} in the undo window cancelled${r.still_waiting ? `, ${r.still_waiting} still waiting for you cannot be signed until you unfreeze` : ""}.`);
    });
  });

  $("history-list").addEventListener("click", (e) => {
    const button = e.target.closest("[data-restore]");
    if (!button) return;
    busy(button, async () => {
      setMandate(await api(`/api/mandate/history/${button.dataset.restore}/restore`, { method: "POST" }));
      toast("That version of the mandate is back in force.");
    });
  });

  $("passport-open").addEventListener("click", async () => {
    try {
      const p = await api("/api/passport");
      $("passport-json").value = JSON.stringify({ passport: p.passport, signature: p.signature }, null, 2);
      $("passport-result").innerHTML = "";
      $("passport").showModal();
    } catch (error) { toast(error.message); }
  });
  $("passport-check").addEventListener("click", async () => {
    let body;
    try { body = JSON.parse($("passport-json").value); } catch { $("passport-result").innerHTML = `<li data-ok="false"><b>Not valid JSON</b></li>`; return; }
    try {
      const r = await api("/api/passport/verify", { method: "POST", body });
      $("passport-result").innerHTML = r.checks.map((c) => `<li data-ok="${c.ok}"><b>${c.ok ? "✓" : "✗"} ${esc(c.label)}</b><span>${esc(c.detail)}</span></li>`).join("");
    } catch (error) { $("passport-result").innerHTML = `<li data-ok="false"><b>${esc(error.message)}</b></li>`; }
  });
  $("passport-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("passport-json").value); toast("Passport copied."); } catch { toast("Select the text and copy it."); }
  });

  $("ask-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("ask-text").value.trim();
    if (q) askLedger(q);
  });
  document.querySelectorAll(".ask-examples .chip:not(#briefing)").forEach((chip) => chip.addEventListener("click", () => {
    $("ask-text").value = chip.textContent;
    askLedger(chip.textContent);
  }));

  $("drill-run").addEventListener("click", async () => {
    try {
      await api("/api/drill", { method: "POST" });
      renderDrill({ running: true });
    } catch (error) { toast(error.message); }
  });

  $("release").addEventListener("click", (e) => busy(e.currentTarget, async () => {
    setMandate(await api("/api/mandate", { method: "PUT", body: { ...state.mandate, active: true } }));
    toast("The agent is back at work. Its limits are unchanged.");
  }));

  $("notify").addEventListener("click", async () => {
    const turnOn = !alertsOn();
    if (turnOn && "Notification" in window && Notification.permission === "default") {
      try { await Notification.requestPermission(); } catch { /* ignore */ }
    }
    try { localStorage.setItem("countersign-alerts", turnOn ? "on" : "off"); } catch { /* storage unavailable */ }
    renderNotifyButton();
    if (turnOn) { chime(); toast("You will hear a sound and get a notification when something needs your signature."); }
  });

  $("boost-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      setMandate(await api("/api/mandate/boost", { method: "POST", body: { limit: Number($("boost-limit").value), minutes: Number($("boost-minutes").value) } }));
      toast("Limits raised for now. They drop back by themselves.");
    } catch (error) { toast(error.message); }
  });
  $("boost-end").addEventListener("click", async () => {
    try { setMandate(await api("/api/mandate/boost", { method: "DELETE" })); toast("Normal limits are back."); } catch (error) { toast(error.message); }
  });

  $("agent-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const allowance = $("agent-allowance").value;
    try {
      const r = await api("/api/agents", { method: "POST", body: { name: $("agent-name").value, weekly_allowance: allowance === "" ? null : Number(allowance) } });
      $("agent-key").hidden = false;
      $("agent-key").innerHTML = `<p><b>Key for ${esc(r.agent.name)}.</b> Copy it now; it is not shown again. The agent sends it in the <code>X-Countersign-Key</code> header.</p><code class="key">${esc(r.key)}</code>`;
      $("agent-name").value = ""; $("agent-allowance").value = "";
      await loadAgents();
    } catch (error) { toast(error.message); }
  });
  $("agent-list").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-revoke]");
    if (!b) return;
    await busy(b, async () => { await api(`/api/agents/${encodeURIComponent(b.dataset.revoke)}`, { method: "DELETE" }); await loadAgents(); toast("Revoked. Its next request will be refused."); });
  });

  $("standing-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/standing-orders", { method: "POST", body: {
        name: $("so-name").value, email: $("so-email").value, amount: Number($("so-amount").value),
        every_days: Number($("so-every").value), purpose: $("so-purpose").value } });
      ["so-name", "so-email", "so-amount", "so-purpose"].forEach((id) => { $(id).value = ""; });
      await loadStanding();
      toast("Standing order added. Press Run now to see it go through the checks.");
    } catch (error) { toast(error.message); }
  });
  $("standing-list").addEventListener("click", async (e) => {
    const run = e.target.closest("[data-so-run]"); const stop = e.target.closest("[data-so-stop]");
    if (run) await busy(run, async () => { await api(`/api/standing-orders/${encodeURIComponent(run.dataset.soRun)}/run`, { method: "POST" }); await loadStanding(); });
    if (stop) await busy(stop, async () => { await api(`/api/standing-orders/${encodeURIComponent(stop.dataset.soStop)}`, { method: "DELETE" }); await loadStanding(); });
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
  wirePlannerAndOversight();
  try {
    await loadAll();
  } catch (error) {
    toast(`Could not load: ${error.message}`);
  }
  connect();
  setInterval(() => loadStatus(), 30000);
  setInterval(tickCountdowns, 250);
  const opened = new URLSearchParams(location.search).get("payment");
  if (opened) { openDetail(opened); history.replaceState(null, "", "/"); }
})();
