/* Countersign proof page: shows a signed proof and lets anyone check or try to forge it. */

const $ = (id) => document.getElementById(id);
const paymentId = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");
let bundle = null;

const OUTCOME = {
  executed: { title: "Countersigned and paid", tone: "ok", stamp: "Countersigned" },
  blocked: { title: "Stopped by Countersign", tone: "stop", stamp: "Blocked" },
  rejected: { title: "Refused by the owner", tone: "stop", stamp: "Refused" },
  reversed: { title: "Paid, then reversed by the owner", tone: "stop", stamp: "Reversed" },
};
const RAIL = { payout: "PayPal Payouts", invoice: "PayPal Invoicing", purchase: "PayPal Checkout" };
const ROLE = { requester: "Requested by", policy: "Policy", ai_review: "AI review", owner: "Owner" };
const RULE = {
  mandate_active: "Mandate", rail_allowed: "Payment type", currency: "Currency", amount: "Amount",
  per_payment_cap: "Per-payment cap", invoice_cap: "Invoice cap", blocked_payee: "Blocked list",
  known_payee: "Payee list", weekly_budget: "Weekly budget", daily_budget: "Daily budget",
  duplicate: "Duplicate check", auto_approve_limit: "Sign-off threshold", evidence: "Source record", ai_review: "AI review",
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function money(amount, currency) {
  try { return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(Number(amount)); }
  catch { return `${amount} ${currency}`; }
}

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 4500);
}

function signatureHand(s) {
  if (s.role === "requester") return s.by === "agent" ? "AI agent" : s.by;
  if (s.role === "ai_review") return `${s.result}${s.risk === null || s.risk === undefined ? "" : `, risk ${s.risk}`}`;
  return s.result;
}

function signatureState(s) {
  if (s.role === "requester") return "pass";
  if (s.result === "refused") return "deny";
  if (s.result === "needs owner") return "escalate";
  return "pass";
}

function renderCertificate(proof) {
  const meta = OUTCOME[proof.outcome] || { title: proof.outcome, tone: "busy", stamp: proof.outcome };
  const paypal = proof.paypal && proof.paypal.id
    ? `${esc(proof.paypal.api)} ${esc(proof.paypal.id)}${proof.paypal.status ? `, ${esc(proof.paypal.status)}` : ""}`
    : "Nothing was sent to PayPal.";
  const signatures = proof.signatures.map((s) => `
    <div class="signature" data-state="${signatureState(s)}">
      <div class="hand">${esc(signatureHand(s))}</div>
      <div class="who">${esc(ROLE[s.role] || s.role)}</div>
    </div>`).join("");
  $("certificate").dataset.tone = meta.tone;
  $("certificate").innerHTML = `
    <div class="cert-top">
      <div>
        <p class="cert-kicker">${esc(RAIL[proof.rail] || proof.rail)} for ${esc(proof.issuer)}</p>
        <h2 class="cert-title">${esc(meta.title)}</h2>
      </div>
      <div class="stamp land" data-tone="${meta.tone}">${esc(meta.stamp)}</div>
    </div>
    <p class="cert-amount">${esc(money(proof.amount, proof.currency))}</p>
    <p class="cert-party">to ${esc(proof.payee)}</p>
    <p class="cert-purpose">${esc(proof.purpose)}</p>
    <div class="signatures cert-signatures">${signatures}</div>
    <dl class="cert-facts">
      <div><dt>PayPal</dt><dd>${paypal}</dd></div>
      <div><dt>Judged under mandate</dt><dd><code>${esc((proof.mandate_sha256 || "none").slice(0, 16))}</code></dd></div>
      <div><dt>Stopped by</dt><dd>${proof.stopped_by.length ? esc(proof.stopped_by.map((r) => RULE[r] || r).join(", ")) : "Nothing. Every check passed."}</dd></div>
      <div><dt>Requested</dt><dd>${esc(new Date(proof.requested_at).toLocaleString())}</dd></div>
      <div><dt>Signed</dt><dd>${esc(new Date(proof.issued_at).toLocaleString())} with key <code>${esc(proof.key_id)}</code></dd></div>
      <div><dt>Proof id</dt><dd><code>${esc(proof.payment_id)}</code></dd></div>
    </dl>`;
}

function renderChecks(result) {
  $("checks").innerHTML = result.checks.map((c) => `
    <li data-ok="${c.ok}">
      <span class="tick" aria-hidden="true">${c.ok ? "✓" : "✕"}</span>
      <span><b>${esc(c.label)}.</b> ${esc(c.detail)}</span>
    </li>`).join("") + `<li class="verdict" data-ok="${result.valid}">${result.valid ? "This proof is genuine." : "This proof has been changed. Do not trust it."}</li>`;
}

async function check(proof) {
  const response = await fetch("/api/proofs/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ proof, signature: bundle.signature }),
  });
  const result = await response.json();
  renderChecks(result);
  return result;
}

function offlineSnippet() {
  return `# pip install cryptography
import base64, json
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

def b64(text): return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

public_key = "${bundle.public_key}"
signature  = "${bundle.signature}"
proof      = json.load(open("proof.json"))   # the document above

message = json.dumps(proof, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
Ed25519PublicKey.from_public_bytes(b64(public_key)).verify(b64(signature), message)
print("genuine")   # verify() raises InvalidSignature if anything was changed`;
}

async function start() {
  try {
    const response = await fetch(`/api/proofs/${encodeURIComponent(paymentId)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "This proof could not be loaded.");
    bundle = data;
  } catch (error) {
    $("certificate").innerHTML = `<h2 class="cert-title">No proof to show</h2><p class="hint">${esc(error.message)}</p>`;
    document.querySelector(".proof-tools").hidden = true;
    return;
  }
  document.title = `Countersign proof ${paymentId}`;
  renderCertificate(bundle.proof);
  $("proof-json").value = JSON.stringify(bundle.proof, null, 2);
  $("offline-code").textContent = offlineSnippet();
  await check(bundle.proof);

  $("check-edit").addEventListener("click", async () => {
    let edited;
    try { edited = JSON.parse($("proof-json").value); }
    catch { toast("That is no longer valid JSON. Restore the original and try a smaller change."); return; }
    const result = await check(edited);
    toast(result.valid ? "Still genuine: nothing that matters was changed." : "Caught: the signature no longer matches.");
  });
  $("restore").addEventListener("click", async () => {
    $("proof-json").value = JSON.stringify(bundle.proof, null, 2);
    await check(bundle.proof);
  });
}

start();
