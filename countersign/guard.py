"""The countersignature pipeline.

    agent's request -> policy (code) -> AI risk review -> decision -> PayPal -> ledger

An agent never talks to PayPal. It can only ask the guard, and the guard decides:
  allow     every rule passed and the review is clear  -> sent to PayPal now
  escalate  a rule or the review wants a human         -> waits for the owner's signature
  deny      a hard limit or a likely fraud             -> blocked, nothing is sent
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from . import policy, reviewer
from .events import EventBus
from .llm import LLM
from .models import PaymentIntent
from .paypal import PayPalClient, PayPalError
from .store import Store, new_id, now_iso


class GuardError(Exception):
    pass


# Where a source record came from. Set by the server, never by the message itself,
# so an email cannot claim to be the owner.
CHANNELS = {
    "owner": "owner console (typed by the signed-in owner)",
    "task": "project board (internal system of record)",
    "email": "inbound email (unauthenticated, anyone can send one)",
}


class Guard:
    def __init__(self, store: Store, paypal: PayPalClient, llm: LLM, bus: EventBus, public_base_url: str) -> None:
        self.store = store
        self.paypal = paypal
        self.llm = llm
        self.bus = bus
        self.public_base_url = public_base_url.rstrip("/")
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # ---- helpers -----------------------------------------------------------

    def _emit(self, payment: dict[str, Any]) -> dict[str, Any]:
        self.bus.publish("payment", payment)
        return payment

    def _evidence(self, source_ref: Optional[str]) -> str:
        if not source_ref:
            return ""
        item = self.store.get_inbox_item(source_ref)
        if not item:
            return ""
        channel = CHANNELS.get(item["kind"], "unknown channel (unauthenticated)")
        return f"Channel: {channel}\nFrom: {item['sender']}\nSubject: {item['subject']}\n\n{item['body']}"

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- request -----------------------------------------------------------

    async def submit(self, intent: PaymentIntent, actor: str = "agent") -> dict[str, Any]:
        """Take a payment request through the whole pipeline and return the final record."""
        mandate = self.store.get_mandate()
        evidence = self._evidence(intent.source_ref)

        # Policy and the budget reservation happen together, so two requests
        # arriving at once cannot both squeeze under the same limit.
        async with self._lock:
            checks = policy.evaluate(mandate, intent, self.store, evidence)
            outcome = policy.outcome_of(checks)
            payment = self.store.insert_payment(
                {
                    "id": new_id(),
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                    "actor": actor,
                    "rail": intent.rail,
                    "counterparty_email": intent.counterparty_email,
                    "counterparty_name": intent.counterparty_name,
                    "amount_cents": intent.cents,
                    "currency": intent.currency,
                    "purpose": intent.purpose,
                    "justification": intent.justification,
                    "source_ref": intent.source_ref,
                    "evidence": evidence,
                    "items": [item.model_dump() for item in intent.items],
                    "status": "blocked" if outcome == "deny" else "reviewing",
                    "decision": "blocked_policy" if outcome == "deny" else "",
                    "checks": checks,
                    "review": {},
                    "paypal": {},
                    "decided_by": "policy" if outcome == "deny" else "",
                    "note": "",
                }
            )
        payment_id = payment["id"]
        self.store.audit("requested", actor, intent.model_dump(mode="json"), payment_id)
        self.store.audit("policy_evaluated", "policy", {"outcome": outcome, "checks": checks}, payment_id)
        self._emit(payment)
        if outcome == "deny":
            return payment

        context = {
            "payee_history": self.store.payee_history(intent.counterparty_email),
            "recent": [p for p in self.store.list_payments() if p["id"] != payment_id][:20],
        }
        review = await reviewer.review(self.llm, mandate, intent, evidence, context)
        checks = checks + [reviewer.as_check(review)]
        outcome = policy.outcome_of(checks)
        self.store.audit("ai_reviewed", "ai_reviewer", review, payment_id)

        if outcome == "deny":
            payment = self.store.update_payment(
                payment_id, status="blocked", decision="blocked_ai", decided_by="ai_reviewer", checks=checks, review=review
            )
            self.store.audit("blocked", "ai_reviewer", {"risk_score": review.get("risk_score")}, payment_id)
            return self._emit(payment)

        if outcome == "escalate":
            payment = self.store.update_payment(
                payment_id, status="pending_approval", decision="needs_owner", decided_by="", checks=checks, review=review
            )
            self.store.audit("held_for_owner", "countersign", {"reasons": [c["label"] for c in checks if c["status"] == "escalate"]}, payment_id)
            return self._emit(payment)

        payment = self.store.update_payment(
            payment_id, status="executing", decision="auto_approved", decided_by="policy+ai", checks=checks, review=review
        )
        self.store.audit("auto_approved", "countersign", {}, payment_id)
        self._emit(payment)
        return await self._execute(payment)

    # ---- owner actions -----------------------------------------------------

    async def approve(self, payment_id: str, note: str = "") -> dict[str, Any]:
        payment = self.store.get_payment(payment_id)
        if not payment:
            raise GuardError("No such payment.")
        if payment["status"] != "pending_approval":
            raise GuardError("This payment is not waiting for a signature.")
        payment = self.store.update_payment(
            payment_id, status="executing", decision="owner_approved", decided_by="owner", note=note[:500]
        )
        self.store.audit("owner_approved", "owner", {"note": note[:500]}, payment_id)
        self._emit(payment)
        return await self._execute(payment)

    async def reject(self, payment_id: str, note: str = "") -> dict[str, Any]:
        payment = self.store.get_payment(payment_id)
        if not payment:
            raise GuardError("No such payment.")
        if payment["status"] != "pending_approval":
            raise GuardError("This payment is not waiting for a signature.")
        payment = self.store.update_payment(
            payment_id, status="rejected", decision="owner_rejected", decided_by="owner", note=note[:500]
        )
        self.store.audit("owner_rejected", "owner", {"note": note[:500]}, payment_id)
        return self._emit(payment)

    async def retry(self, payment_id: str) -> dict[str, Any]:
        payment = self.store.get_payment(payment_id)
        if not payment:
            raise GuardError("No such payment.")
        if payment["status"] != "failed":
            raise GuardError("Only a failed payment can be retried.")
        payment = self.store.update_payment(payment_id, status="executing")
        self.store.audit("retry", "owner", {}, payment_id)
        self._emit(payment)
        return await self._execute(payment)

    # ---- PayPal ------------------------------------------------------------

    def _request_key(self, payment: dict[str, Any]) -> dict[str, Any]:
        """The idempotency key for PayPal.

        If the last attempt got a definite error from PayPal, nothing was created and a
        fresh key is used. If the outcome is unknown (network failure, restart), the same
        key is reused so PayPal replays the original result instead of paying twice.
        """
        attempt = int((payment.get("paypal") or {}).get("attempt", 0))
        key = payment["id"] if attempt == 0 else f"{payment['id']}-r{attempt}"
        return {**payment, "id": key}

    async def _execute(self, payment: dict[str, Any]) -> dict[str, Any]:
        payment_id = payment["id"]
        previous = payment.get("paypal") or {}
        attempt = int(previous.get("attempt", 0))
        keyed = self._request_key(payment)
        try:
            if payment["rail"] == "payout":
                result = await self.paypal.create_payout(keyed)
                status = "executed"
            elif payment["rail"] == "invoice":
                result = await self.paypal.create_and_send_invoice(keyed)
                status = "executed"
            else:
                result = await self.paypal.create_order(
                    keyed,
                    return_url=f"{self.public_base_url}/paypal/return?payment_id={payment_id}",
                    cancel_url=f"{self.public_base_url}/paypal/cancel?payment_id={payment_id}",
                )
                status = "awaiting_payer"
        except PayPalError as exc:
            definite = 400 <= exc.status < 500
            error = {"api": {"payout": "Payouts", "invoice": "Invoicing", "purchase": "Orders"}[payment["rail"]], "error": exc.as_dict(), "attempt": attempt + 1 if definite else attempt}
            updated = self.store.update_payment(payment_id, status="failed", paypal=error, note=f"PayPal: {exc.message}"[:500])
            self.store.audit("paypal_failed", "paypal", exc.as_dict(), payment_id)
            return self._emit(updated)

        result["attempt"] = attempt
        updated = self.store.update_payment(payment_id, status=status, paypal=result, note=payment.get("note", ""))
        self.store.audit("paypal_executed" if status == "executed" else "paypal_order_created", "paypal", result, payment_id)
        self._emit(updated)
        if payment["rail"] == "payout":
            self._spawn(self._follow_payout(payment_id))
        return updated

    async def _follow_payout(self, payment_id: str) -> None:
        """Payout batches settle a few seconds after they are accepted. Follow up so the ledger shows the outcome."""
        for delay in (3, 6, 12, 25):
            await asyncio.sleep(delay)
            try:
                payment = await self.sync(payment_id)
            except (GuardError, PayPalError):
                return
            if (payment.get("paypal") or {}).get("status") in {"SUCCESS", "DENIED", "CANCELED"}:
                return

    async def sync(self, payment_id: str) -> dict[str, Any]:
        """Refresh a payment's status from PayPal, the source of truth."""
        payment = self.store.get_payment(payment_id)
        if not payment:
            raise GuardError("No such payment.")
        paypal = payment.get("paypal") or {}
        if not paypal.get("id"):
            return payment
        if payment["rail"] == "payout":
            fresh = await self.paypal.get_payout(paypal["id"])
        elif payment["rail"] == "invoice":
            fresh = await self.paypal.get_invoice(paypal["id"])
        else:
            fresh = await self.paypal.get_order(paypal["id"])
            fresh["approve_url"] = fresh.get("approve_url") or paypal.get("approve_url", "")
        merged = {**paypal, **{k: v for k, v in fresh.items() if v not in ("", None)}}
        if merged == paypal:
            return payment
        updated = self.store.update_payment(payment_id, paypal=merged)
        self.store.audit("paypal_status", "paypal", {"status": merged.get("status"), "item_status": merged.get("item_status", "")}, payment_id)
        return self._emit(updated)

    async def capture_return(self, payment_id: str, order_id: str) -> dict[str, Any]:
        """The owner approved the order inside PayPal and was sent back to us. Capture it."""
        payment = self.store.get_payment(payment_id)
        if not payment or payment["rail"] != "purchase":
            raise GuardError("No such purchase.")
        paypal = payment.get("paypal") or {}
        if payment["status"] != "awaiting_payer" or paypal.get("id") != order_id:
            raise GuardError("This order is not waiting for approval.")
        try:
            result = await self.paypal.capture_order(order_id, payment_id)
        except PayPalError as exc:
            updated = self.store.update_payment(payment_id, status="failed", paypal={**paypal, "error": exc.as_dict()}, note=f"PayPal: {exc.message}"[:500])
            self.store.audit("paypal_failed", "paypal", exc.as_dict(), payment_id)
            return self._emit(updated)
        merged = {**paypal, **{k: v for k, v in result.items() if v not in ("", None)}}
        updated = self.store.update_payment(payment_id, status="executed", paypal=merged)
        self.store.audit("paypal_executed", "paypal", result, payment_id)
        return self._emit(updated)

    async def cancel_return(self, payment_id: str) -> Optional[dict[str, Any]]:
        payment = self.store.get_payment(payment_id)
        if not payment or payment["status"] != "awaiting_payer":
            return payment
        updated = self.store.update_payment(payment_id, status="rejected", decision="owner_rejected", decided_by="owner", note="Cancelled at PayPal checkout.")
        self.store.audit("owner_rejected", "owner", {"note": "Cancelled at PayPal checkout."}, payment_id)
        return self._emit(updated)
