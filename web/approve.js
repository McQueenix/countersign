/* Sign one held payment from a phone. The link works once and expires. */

const token = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");
const card = document.getElementById("ask");
const RAIL = { payout: "Pay", invoice: "Bill", purchase: "Buy from" };

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function money(amount, currency) {
  try { return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(amount); } catch { return `${amount} ${currency}`; }
}

function done(title, text, tone, paymentId) {
  card.dataset.tone = tone;
  card.innerHTML = `<h2 class="phone-title">${esc(title)}</h2><p>${esc(text)}</p>${paymentId ? `<p><a href="/verify/${encodeURIComponent(paymentId)}">See the signed proof</a></p>` : ""}`;
}

async function act(action) {
  card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  try {
    const response = await fetch(`/api/approvals/${encodeURIComponent(token)}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "That did not work.");
    if (action === "approve") {
      done("Signed", data.status === "executed" ? "PayPal has accepted the payment." : data.status === "awaiting_payer" ? "Signed. Finish the purchase in PayPal on the console." : `Signed. Status: ${data.status}.`, "ok", data.status === "executed" ? data.payment_id : null);
    } else {
      done("Refused", "Nothing was sent. A signed record of your refusal was kept.", "stop", data.payment_id);
    }
  } catch (error) {
    done("Could not sign", error.message, "stop");
  }
}

function tick(expires) {
  const el = document.getElementById("expires");
  if (!el) return;
  const left = Math.max(0, Math.round((new Date(expires) - Date.now()) / 1000));
  el.textContent = left ? `This link expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} and works once.` : "This link has expired.";
}

(async function start() {
  try {
    const response = await fetch(`/api/approvals/${encodeURIComponent(token)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "This link cannot be used.");
    const p = data.payment;
    card.innerHTML = `
      <p class="hint">${esc(data.business)} &middot; requested by ${esc(p.actor === "agent" ? "the AI agent" : p.actor)}</p>
      <p class="phone-amount">${esc(money(p.amount, p.currency))}</p>
      <p class="phone-party">${esc(RAIL[p.rail] || "Pay")} ${esc(p.counterparty_name || p.counterparty_email)}</p>
      <p class="hint">${esc(p.counterparty_email)}</p>
      <p class="phone-purpose">${esc(p.purpose)}</p>
      <h2 class="phone-why">Why it needs you</h2>
      <ul class="ask-why">${data.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
      <div class="phone-actions">
        <button class="button primary" data-act="approve">Sign and send</button>
        <button class="button" data-act="reject">Refuse</button>
      </div>
      <p id="expires" class="hint"></p>`;
    card.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => act(b.dataset.act)));
    tick(data.expires_at);
    setInterval(() => tick(data.expires_at), 1000);
  } catch (error) {
    done("This link cannot be used", error.message, "stop");
  }
})();
