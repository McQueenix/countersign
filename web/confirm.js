/* A new payee confirms that a PayPal address is theirs before the business can pay it. */

const token = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");
const card = document.getElementById("ask");

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function money(amount, currency) {
  try { return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(amount); } catch { return `${amount} ${currency}`; }
}
function done(title, text, tone) {
  card.dataset.tone = tone;
  card.innerHTML = `<h2 class="phone-title">${esc(title)}</h2><p>${esc(text)}</p>`;
}

async function act(action) {
  card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  try {
    const response = await fetch(`/api/confirmations/${encodeURIComponent(token)}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "That did not work.");
    if (action === "confirm") done("Thank you", "The business can now pay you at this address. PayPal will email you when the money is sent.", "ok");
    else done("Thanks for telling us", "Nothing will be paid to that address. The business has been told, so they can check who asked.", "stop");
  } catch (error) {
    done("Could not record that", error.message, "stop");
  }
}

async function load() {
  try {
    const response = await fetch(`/api/confirmations/${encodeURIComponent(token)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "This link does not work.");
    card.innerHTML = `
      <p class="hint">${esc(data.business)} is about to pay${data.name ? ` ${esc(data.name)}` : ""}</p>
      <div class="phone-amount">${esc(money(data.amount, data.currency))}</div>
      <p>for ${esc(data.purpose)}, to this PayPal address:</p>
      <p class="phone-email"><b>${esc(data.email)}</b></p>
      <p class="hint">Only confirm if this is your own PayPal account. If you did not ask to be paid here, say so: it protects you both.</p>
      <div class="phone-actions">
        <button class="button primary" id="yes">Yes, this is my PayPal address</button>
        <button class="button refuse" id="no">No, this is not mine</button>
      </div>`;
    document.getElementById("yes").addEventListener("click", () => act("confirm"));
    document.getElementById("no").addEventListener("click", () => act("deny"));
  } catch (error) {
    done("This link does not work", error.message, "stop");
  }
}

load();
