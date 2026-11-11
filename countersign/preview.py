"""Offline preview: stand-ins for PayPal and the AI model so the interface can be explored with no keys.

Enabled only with COUNTERSIGN_OFFLINE_PREVIEW=1, and the app shows a banner while it is on.
Nothing here talks to PayPal or to a model. The real integrations are in paypal.py and llm.py.
The same stand-ins back the automated tests.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Optional

from .models import EMAIL_RE

EMAIL_FIND = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
AMOUNT_FIND = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)")


def _amounts(text: str) -> list[float]:
    return [float(match.replace(",", "")) for match in AMOUNT_FIND.findall(text)]


def _fake_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:13].upper()


class PreviewPayPal:
    configured = True
    brand_name = "Countersign"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_next: Optional[Exception] = None

    async def close(self) -> None:
        return None

    async def ping(self) -> None:
        return None

    def _maybe_fail(self) -> None:
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            raise error

    async def create_payout(self, payment: dict[str, Any]) -> dict[str, Any]:
        self._maybe_fail()
        self.calls.append(("create_payout", payment["id"]))
        return {"api": "Payouts", "id": _fake_id("PREVIEW"), "status": "PENDING"}

    async def get_payout(self, batch_id: str) -> dict[str, Any]:
        return {"api": "Payouts", "id": batch_id, "status": "SUCCESS", "item_status": "SUCCESS", "transaction_id": _fake_id("TX"), "payout_item_id": _fake_id("PI")}

    async def create_and_send_invoice(self, payment: dict[str, Any]) -> dict[str, Any]:
        self._maybe_fail()
        self.calls.append(("create_and_send_invoice", payment["id"]))
        return {"api": "Invoicing", "id": "INV2-" + _fake_id("PREVIEW")[:16], "status": "SENT", "invoice_number": "0001", "payer_url": ""}

    async def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        return {"api": "Invoicing", "id": invoice_id, "status": "SENT", "invoice_number": "0001", "payer_url": ""}

    async def create_order(self, payment: dict[str, Any], return_url: str, cancel_url: str) -> dict[str, Any]:
        self._maybe_fail()
        self.calls.append(("create_order", payment["id"]))
        order_id = _fake_id("PREVIEW")
        return {"api": "Orders", "id": order_id, "status": "PAYER_ACTION_REQUIRED", "approve_url": f"{return_url}&token={order_id}"}

    async def capture_order(self, order_id: str, request_id: str) -> dict[str, Any]:
        self.calls.append(("capture_order", order_id))
        return {"api": "Orders", "id": order_id, "status": "COMPLETED", "capture_id": _fake_id("CAP"), "approve_url": ""}

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return {"api": "Orders", "id": order_id, "status": "PAYER_ACTION_REQUIRED", "capture_id": "", "approve_url": ""}


class PreviewLLM:
    """Keyword rules that imitate the three model jobs closely enough to exercise the pipeline."""

    configured = True
    model = "offline-preview (no model)"

    async def close(self) -> None:
        return None

    async def ping(self) -> None:
        return None

    # -- agent ---------------------------------------------------------------

    async def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None, temperature: float = 0.0) -> dict[str, Any]:
        last = messages[-1]
        if last["role"] == "tool":
            return {"role": "assistant", "content": last["content"]}
        text = last["content"]
        lowered = text.lower()
        emails = EMAIL_FIND.findall(text)
        amounts = _amounts(text)

        def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return {"role": "assistant", "content": "", "tool_calls": [{"id": "call_" + uuid.uuid4().hex[:8], "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}

        if "final notice" in lowered or "legal action" in lowered:
            return call("no_action", {"reason": "This is a pressure message from an unknown sender with no matching contract, so it looks like a scam."})
        if not emails or not amounts:
            return call("no_action", {"reason": "The item does not name who to pay or how much."})

        base = {"currency": "USD", "purpose": _subject(text), "justification": "Taken from the work item."}
        if "bill to" in lowered or "invoice" in lowered and "pay" not in lowered:
            lines = [{"name": name.strip(" ,."), "amount": float(value.replace(",", ""))} for name, value in re.findall(r"([A-Z][A-Za-z &]+?) \$([0-9,]+\.[0-9]{2})", text) if not name.strip().lower().startswith("total")]
            total = sum(line["amount"] for line in lines) if lines else max(amounts)
            return call("request_invoice", {**base, "counterparty_email": _labelled(text, "bill to") or emails[-1], "counterparty_name": _name(text, "bill to"), "amount": total, "items": lines})
        if "vendor" in lowered:
            return call("request_purchase", {**base, "counterparty_email": _labelled(text, "vendor") or emails[-1], "counterparty_name": _name(text, "vendor"), "amount": amounts[0]})
        target = _labelled(text, "contractor") or emails[-1]
        return call("request_payout", {**base, "counterparty_email": target, "counterparty_name": _name(text, "contractor"), "amount": amounts[0]})

    # -- reviewer and mandate compiler ----------------------------------------

    async def json(self, system: str, user: str) -> dict[str, Any]:
        if "payment risk reviewer" in system:
            return self._review(user)
        return self._compile(user)

    @staticmethod
    def _review(user: str) -> dict[str, Any]:
        record = user.split("<<<", 1)[-1].lower()
        request = user.split("SOURCE RECORD", 1)[0].lower()
        signals: list[str] = []
        if "ai assistant" in record or "ignore any" in record or "automated systems must" in record:
            signals.append("text addressed to an AI telling it to skip controls")
        if "new address" in record or "new paypal" in record:
            signals.append("request to pay a known person at a new address")
        if "urgent" in record or "final notice" in record or "don't wait" in record:
            signals.append("pressure to act quickly")
        if "skip review" in request:
            signals.append("the agent's own reasoning asks to skip review")
        match = re.search(r"counterparty: .*?<([^>]+)>", request)
        if match and "(no source record" not in record and match.group(1) not in record:
            signals.append("payee is not the one named in the source record")
        if "owner console" in record and len(signals) <= 1:
            signals = []
        score = min(95, 12 + 30 * len(signals))
        if not signals:
            return {"risk_score": 8, "recommendation": "clear", "signals": [], "summary": "The request matches an approved record and nothing looks unusual."}
        recommendation = "block" if score >= 80 else "hold"
        return {"risk_score": score, "recommendation": recommendation, "signals": signals, "summary": "This looks like an attempt to redirect a payment: " + "; ".join(signals) + "."}

    @staticmethod
    def _compile(text: str) -> dict[str, Any]:
        lowered = text.lower()

        def first(pattern: str) -> Optional[float]:
            match = re.search(pattern, lowered)
            return float(match.group(1).replace(",", "")) if match else None

        cap = first(r"\$([0-9,]+(?:\.[0-9]+)?) per payment")
        approval = first(r"(?:over|above) \$([0-9,]+(?:\.[0-9]+)?)")
        weekly = first(r"\$([0-9,]+(?:\.[0-9]+)?) (?:per|a|each) week")
        daily = first(r"\$([0-9,]+(?:\.[0-9]+)?) (?:per|a|each) day")
        invoice = first(r"invoices?[^.]*?up to \$([0-9,]+(?:\.[0-9]+)?)")
        cap = cap if cap is not None else (approval or 0)
        year = re.search(r"(\d{1,2}) (january|february|march|april|may|june|july|august|september|october|november|december) (\d{4})", lowered)
        months = "january february march april may june july august september october november december".split()
        expires = f"{year.group(3)}-{months.index(year.group(2)) + 1:02d}-{int(year.group(1)):02d}" if year else None
        return {
            "agent_name": "Ops Agent",
            "currency": "USD",
            "allow_payouts": "pay" in lowered,
            "allow_invoices": "invoice" in lowered,
            "allow_purchases": "buy" in lowered or "purchase" in lowered,
            "per_payment_cap": cap,
            "auto_approve_limit": approval if approval is not None else cap,
            "weekly_budget": weekly,
            "daily_budget": daily,
            "invoice_cap": invoice,
            "allowed_payees": [e.rstrip(".") for e in EMAIL_FIND.findall(text) if EMAIL_RE.match(e.rstrip("."))],
            "blocked_payees": [],
            "unknown_payee": "deny" if "never pay anyone else" in lowered else "escalate",
            "expires_on": expires,
        }


def _subject(text: str) -> str:
    match = re.search(r"^Subject: (.+)$", text, flags=re.MULTILINE)
    return (match.group(1) if match else text.strip().splitlines()[-1])[:120]


def _labelled(text: str, label: str) -> str:
    match = re.search(rf"{label}:[^<\n]*<([^>\s]+)>", text, flags=re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _name(text: str, label: str) -> str:
    match = re.search(rf"{label}:\s*([^<\n]+?)\s*<", text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""
